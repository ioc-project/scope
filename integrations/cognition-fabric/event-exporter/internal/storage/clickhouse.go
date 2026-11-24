// Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
// SPDX-License-Identifier: Apache-2.0

package storage

import (
	"bytes"
	"context"
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"time"

	"oxp-event-exporter/internal/otlp"
)

const (
	SchemaVersion     = "1.0.0"
	NormalizerVersion = "6"
	batchSize         = 1000
)

//go:embed schema.sql
var schema string

var databaseIdentifier = regexp.MustCompile(`^[a-zA-Z_][a-zA-Z0-9_]*$`)
var eventIdentifier = regexp.MustCompile(`^[0-9a-f]{64}$`)

type Config struct {
	Endpoint string
	Database string
	Username string
	Password string
	Timeout  time.Duration
}

func ConfigFromEnv() (Config, error) {
	config := Config{
		Endpoint: os.Getenv("CLICKHOUSE_ENDPOINT"),
		Database: os.Getenv("CLICKHOUSE_DATABASE"),
		Username: os.Getenv("CLICKHOUSE_USERNAME"),
		Password: os.Getenv("CLICKHOUSE_PASSWORD"),
		Timeout:  10 * time.Second,
	}
	if config.Endpoint == "" {
		host := os.Getenv("CLICKHOUSE_HOST")
		if host == "" {
			host = "localhost"
		}
		port := os.Getenv("CLICKHOUSE_PORT")
		if port == "" {
			port = "8123"
		}
		config.Endpoint = "http://" + net.JoinHostPort(host, port)
	}
	if config.Database == "" {
		config.Database = "default"
	}
	if config.Username == "" {
		config.Username = "default"
	}
	if value := os.Getenv("CLICKHOUSE_TIMEOUT"); value != "" {
		timeout, err := time.ParseDuration(value)
		if err != nil || timeout <= 0 {
			return Config{}, fmt.Errorf("CLICKHOUSE_TIMEOUT must be a positive duration")
		}
		config.Timeout = timeout
	}
	return config, nil
}

type Event struct {
	ID                string `json:"event_id"`
	TenantID          string `json:"tenant_id"`
	ApplicationID     string `json:"application_id"`
	Time              string `json:"event_time"`
	Type              string `json:"event_type"`
	SessionID         string `json:"session_id"`
	SessionSequence   int    `json:"session_sequence"`
	TraceID           string `json:"trace_id"`
	SpanID            string `json:"span_id"`
	CallID            string `json:"call_id"`
	MessageID         string `json:"message_id"`
	ForkID            string `json:"fork_id"`
	ServiceName       string `json:"service_name"`
	SchemaVersion     string `json:"schema_version"`
	NormalizerVersion string `json:"normalizer_version"`
	Payload           string `json:"payload"`
}

func NewEvent(record otlp.LogRecord, event otlp.ClientEvent) (Event, error) {
	payload, err := json.Marshal(event)
	if err != nil {
		return Event{}, fmt.Errorf("encode normalized event: %w", err)
	}
	id := ""
	if supplied, exists := record.LogAttributes["event.id"]; exists {
		var ok bool
		id, ok = supplied.(string)
		if !ok || !eventIdentifier.MatchString(id) {
			return Event{}, fmt.Errorf("event.id must be a lowercase hexadecimal SHA-256 digest")
		}
	} else {
		source, err := json.Marshal(record)
		if err != nil {
			return Event{}, fmt.Errorf("encode event identity: %w", err)
		}
		identity := sha256.Sum256(source)
		id = hex.EncodeToString(identity[:])
	}
	return Event{
		ID:            id,
		TenantID:      identityAttribute(record, "tenant_id", "tenant.id"),
		ApplicationID: identityAttribute(record, "application_id", "app_id", "application.id"),
		Time:          event.Time.UTC().Format("2006-01-02 15:04:05.000000000"),
		Type:          event.Type, SessionID: event.Session.ID, SessionSequence: event.Session.Sequence,
		TraceID: optionalString(event.Correlation.TraceID), SpanID: optionalString(event.Correlation.SpanID),
		CallID: optionalString(event.Correlation.CallID), MessageID: optionalString(event.Correlation.MessageID),
		ForkID: optionalString(event.Correlation.ForkID), ServiceName: event.Source.ServiceName,
		SchemaVersion: SchemaVersion, NormalizerVersion: NormalizerVersion, Payload: string(payload),
	}, nil
}

func identityAttribute(record otlp.LogRecord, keys ...string) string {
	for _, attributes := range []map[string]any{record.LogAttributes, record.ResourceAttributes} {
		for _, key := range keys {
			if value, ok := attributes[key].(string); ok && value != "" {
				return value
			}
		}
	}
	return ""
}

func optionalString(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

type ClickHouse struct {
	config Config
	client *http.Client
}

func NewClickHouse(config Config) (*ClickHouse, error) {
	endpoint, err := url.Parse(config.Endpoint)
	if err != nil || endpoint == nil || (endpoint.Scheme != "http" && endpoint.Scheme != "https") || endpoint.Host == "" || endpoint.User != nil || endpoint.RawQuery != "" || endpoint.Fragment != "" {
		return nil, fmt.Errorf("ClickHouse endpoint must be an HTTP(S) URL without credentials, query, or fragment")
	}
	if !databaseIdentifier.MatchString(config.Database) {
		return nil, fmt.Errorf("invalid ClickHouse database identifier")
	}
	if config.Timeout <= 0 {
		return nil, fmt.Errorf("ClickHouse timeout must be positive")
	}
	return &ClickHouse{
		config: config,
		client: &http.Client{
			Timeout: config.Timeout,
			CheckRedirect: func(request *http.Request, via []*http.Request) error {
				return http.ErrUseLastResponse
			},
		},
	}, nil
}

func (store *ClickHouse) EnsureTable(ctx context.Context) error {
	return store.execute(ctx, fmt.Sprintf(schema, "`"+store.config.Database+"`"), nil)
}

func (store *ClickHouse) Write(ctx context.Context, events []Event) error {
	ctx, cancel := context.WithTimeout(ctx, store.config.Timeout)
	defer cancel()
	for start := 0; start < len(events); start += batchSize {
		end := min(start+batchSize, len(events))
		var body bytes.Buffer
		encoder := json.NewEncoder(&body)
		for _, event := range events[start:end] {
			if err := encoder.Encode(event); err != nil {
				return fmt.Errorf("encode ClickHouse batch: %w", err)
			}
		}
		query := fmt.Sprintf("INSERT INTO `%s`.normalized_events FORMAT JSONEachRow", store.config.Database)
		if err := store.execute(ctx, query, &body); err != nil {
			return err
		}
	}
	return nil
}

type HTTPError struct {
	StatusCode int
}

func (err *HTTPError) Error() string {
	return fmt.Sprintf("ClickHouse returned HTTP %d", err.StatusCode)
}

func (err *HTTPError) Retryable() bool {
	return err.StatusCode == http.StatusRequestTimeout || err.StatusCode == http.StatusTooManyRequests || err.StatusCode >= 500
}

func (store *ClickHouse) execute(ctx context.Context, query string, body io.Reader) error {
	endpoint, _ := url.Parse(store.config.Endpoint)
	parameters := endpoint.Query()
	parameters.Set("query", query)
	parameters.Set("async_insert", "0")
	parameters.Set("wait_end_of_query", "1")
	parameters.Set("input_format_defaults_for_omitted_fields", "1")
	endpoint.RawQuery = parameters.Encode()
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint.String(), body)
	if err != nil {
		return fmt.Errorf("create ClickHouse request: %w", err)
	}
	request.Header.Set("Content-Type", "application/x-ndjson")
	request.SetBasicAuth(store.config.Username, store.config.Password)
	response, err := store.client.Do(request)
	if err != nil {
		return fmt.Errorf("ClickHouse request failed: %w", err)
	}
	defer func() { _ = response.Body.Close() }()
	_, readErr := io.Copy(io.Discard, io.LimitReader(response.Body, 64<<10))
	if response.StatusCode != http.StatusOK {
		return &HTTPError{StatusCode: response.StatusCode}
	}
	if readErr != nil {
		return fmt.Errorf("read ClickHouse acknowledgement: %w", readErr)
	}
	return nil
}
