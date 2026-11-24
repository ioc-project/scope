// Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
// SPDX-License-Identifier: Apache-2.0

package storage

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/joho/godotenv"

	"oxp-event-exporter/internal/otlp"
)

func TestNewEvent(t *testing.T) {
	record := otlp.LogRecord{
		Timestamp:   time.Date(2026, 10, 14, 12, 0, 0, 123, time.UTC),
		ServiceName: "agents",
		LogAttributes: map[string]any{
			"event.name": "topology.session.started", "session.id": "session-1", "snapshot.version": 1,
			"application.id": "log-app", "tenant_id": "log-tenant",
		},
		ResourceAttributes: map[string]any{"app_id": "resource-app", "tenant.id": "resource-tenant"},
	}
	event, err := otlp.NormalizeEvent(record)
	if err != nil {
		t.Fatal(err)
	}
	row, err := NewEvent(record, event)
	if err != nil {
		t.Fatal(err)
	}
	retry, err := NewEvent(record, event)
	if err != nil {
		t.Fatal(err)
	}
	if len(row.ID) != 64 || row.ID != retry.ID || row.ApplicationID != "log-app" || row.TenantID != "log-tenant" {
		t.Fatalf("unexpected identity: %+v", row)
	}
	if row.Time != "2026-10-14 12:00:00.000000123" || row.SessionSequence != 1 || row.SchemaVersion != SchemaVersion {
		t.Fatalf("unexpected metadata: %+v", row)
	}
	payload, err := json.Marshal(event)
	if err != nil || row.Payload != string(payload) {
		t.Fatalf("payload differs from push event: %s, %v", row.Payload, err)
	}
	record.LogAttributes["snapshot.version"] = 2
	different, err := NewEvent(record, event)
	if err != nil || different.ID == row.ID {
		t.Fatalf("different source records must have different IDs: %v", err)
	}
	delete(record.LogAttributes, "application.id")
	delete(record.LogAttributes, "tenant_id")
	fallback, err := NewEvent(record, event)
	if err != nil || fallback.ApplicationID != "resource-app" || fallback.TenantID != "resource-tenant" {
		t.Fatalf("resource identity fallback failed: %+v, %v", fallback, err)
	}
	missing, err := NewEvent(otlp.LogRecord{}, event)
	if err != nil || missing.ApplicationID != "" || missing.TenantID != "" {
		t.Fatalf("optional identities should be empty: %+v, %v", missing, err)
	}
}

func TestNewEventApplicationID(t *testing.T) {
	for _, test := range []struct {
		name     string
		logs     map[string]any
		resource map[string]any
		want     string
	}{
		{name: "log application_id", logs: map[string]any{"application_id": "abcd"}, want: "abcd"},
		{name: "canonical key preferred", logs: map[string]any{"application_id": "abcd", "app_id": "legacy", "application.id": "dotted"}, want: "abcd"},
		{name: "log alias before resource", logs: map[string]any{"app_id": "legacy"}, resource: map[string]any{"application_id": "resource"}, want: "legacy"},
		{name: "resource application_id", resource: map[string]any{"application_id": "resource"}, want: "resource"},
		{name: "empty log fallback", logs: map[string]any{"application_id": ""}, resource: map[string]any{"application_id": "resource"}, want: "resource"},
		{name: "non-string log fallback", logs: map[string]any{"application_id": 123}, resource: map[string]any{"application_id": "resource"}, want: "resource"},
	} {
		t.Run(test.name, func(t *testing.T) {
			row, err := NewEvent(otlp.LogRecord{LogAttributes: test.logs, ResourceAttributes: test.resource}, otlp.ClientEvent{})
			if err != nil || row.ApplicationID != test.want {
				t.Fatalf("unexpected application identity: %+v, %v", row, err)
			}
			encoded, err := json.Marshal(row)
			if err != nil {
				t.Fatal(err)
			}
			var columns map[string]any
			if err := json.Unmarshal(encoded, &columns); err != nil {
				t.Fatal(err)
			}
			if columns["application_id"] != test.want {
				t.Fatalf("missing application_id column: %s", encoded)
			}
			if _, exists := columns["app_id"]; exists {
				t.Fatalf("legacy app_id column emitted: %s", encoded)
			}
		})
	}
}

func TestNewEventSuppliedIdentity(t *testing.T) {
	id := strings.Repeat("a", 64)
	record := otlp.LogRecord{LogAttributes: map[string]any{"event.id": id}}
	row, err := NewEvent(record, otlp.ClientEvent{})
	if err != nil || row.ID != id {
		t.Fatalf("supplied identity not preserved: %+v, %v", row, err)
	}
	record.ServiceName = "collector-enriched"
	record.LogAttributes["snapshot.version"] = 2
	retry, err := NewEvent(record, otlp.ClientEvent{})
	if err != nil || retry.ID != id {
		t.Fatalf("supplied identity must not be recomputed: %+v, %v", retry, err)
	}
	for _, invalid := range []any{nil, "", 123, "not-a-digest", strings.Repeat("A", 64), strings.Repeat("a", 65)} {
		record.LogAttributes["event.id"] = invalid
		if _, err := NewEvent(record, otlp.ClientEvent{}); err == nil {
			t.Fatalf("invalid supplied event.id accepted: %v", invalid)
		}
	}
}

func TestClickHouseCreateAndWrite(t *testing.T) {
	var queries []string
	var batches []int
	endpoint := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		username, password, ok := request.BasicAuth()
		if !ok || username != "writer" || password != "secret" {
			t.Error("missing database authentication")
		}
		parameters := request.URL.Query()
		if parameters.Get("async_insert") != "0" || parameters.Get("wait_end_of_query") != "1" || parameters.Get("input_format_defaults_for_omitted_fields") != "1" {
			t.Error("writes must wait for database acknowledgement and apply defaults")
		}
		query := parameters.Get("query")
		queries = append(queries, query)
		if strings.HasPrefix(query, "INSERT") {
			decoder := json.NewDecoder(request.Body)
			count := 0
			for {
				var event Event
				if err := decoder.Decode(&event); errors.Is(err, io.EOF) {
					break
				} else if err != nil {
					t.Error(err)
					break
				}
				count++
			}
			batches = append(batches, count)
		}
	}))
	defer endpoint.Close()
	store, err := NewClickHouse(Config{Endpoint: endpoint.URL, Database: "test_db", Username: "writer", Password: "secret", Timeout: time.Second})
	if err != nil {
		t.Fatal(err)
	}
	if err := store.EnsureTable(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := store.Write(context.Background(), make([]Event, batchSize+1)); err != nil {
		t.Fatal(err)
	}
	if len(queries) != 3 || !strings.Contains(queries[0], "CREATE TABLE IF NOT EXISTS `test_db`.normalized_events") || !strings.Contains(queries[0], "ReplacingMergeTree") || strings.Contains(queries[0], "TTL") {
		t.Fatalf("unexpected queries: %v", queries)
	}
	if !strings.Contains(queries[0], "application_id String") || !strings.Contains(queries[0], "ORDER BY (tenant_id, application_id,") || strings.Contains(queries[0], "app_id") {
		t.Fatalf("incorrect application column in DDL: %s", queries[0])
	}
	if len(batches) != 2 || batches[0] != batchSize || batches[1] != 1 {
		t.Fatalf("unexpected batch sizes: %v", batches)
	}
	if err := store.Write(context.Background(), nil); err != nil || len(queries) != 3 {
		t.Fatal("empty batches must not issue a request")
	}
}

func TestClickHouseFailures(t *testing.T) {
	for _, code := range []int{http.StatusBadRequest, http.StatusUnauthorized, http.StatusTooManyRequests, http.StatusServiceUnavailable} {
		t.Run(http.StatusText(code), func(t *testing.T) {
			requests := 0
			endpoint := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
				requests++
				response.WriteHeader(code)
			}))
			defer endpoint.Close()
			store, err := NewClickHouse(Config{Endpoint: endpoint.URL, Database: "default", Timeout: time.Second})
			if err != nil {
				t.Fatal(err)
			}
			var failure *HTTPError
			if err := store.Write(context.Background(), []Event{{}}); !errors.As(err, &failure) || failure.StatusCode != code {
				t.Fatalf("unexpected write error: %v", err)
			}
			if failure.Retryable() != (code == 429 || code >= 500) || requests != 1 {
				t.Fatal("incorrect retry classification or unexpected internal retry")
			}
		})
	}
}

func TestClickHouseCanceledWrite(t *testing.T) {
	store, err := NewClickHouse(Config{Endpoint: "http://localhost:8123", Database: "default", Timeout: time.Second})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := store.Write(ctx, []Event{{}}); !errors.Is(err, context.Canceled) {
		t.Fatalf("expected cancellation, got %v", err)
	}
}

func TestClickHouseConfigValidation(t *testing.T) {
	for _, config := range []Config{
		{Endpoint: "file:///tmp/db", Database: "default", Timeout: time.Second},
		{Endpoint: "http://user:secret@localhost", Database: "default", Timeout: time.Second},
		{Endpoint: "http://localhost?query=DROP", Database: "default", Timeout: time.Second},
		{Endpoint: "http://localhost", Database: "default; DROP TABLE events", Timeout: time.Second},
		{Endpoint: "http://localhost", Database: "default"},
	} {
		if _, err := NewClickHouse(config); err == nil {
			t.Fatalf("accepted invalid config: %+v", config)
		}
	}
}

func TestConfigFromEnv(t *testing.T) {
	for _, key := range []string{"CLICKHOUSE_ENDPOINT", "CLICKHOUSE_HOST", "CLICKHOUSE_PORT", "CLICKHOUSE_DATABASE", "CLICKHOUSE_USERNAME", "CLICKHOUSE_PASSWORD", "CLICKHOUSE_TIMEOUT"} {
		t.Setenv(key, "")
	}
	config, err := ConfigFromEnv()
	if err != nil || config.Endpoint != "http://localhost:8123" || config.Database != "default" || config.Username != "default" || config.Timeout != 10*time.Second {
		t.Fatalf("unexpected default configuration: %+v, %v", config, err)
	}
	t.Setenv("CLICKHOUSE_HOST", "::1")
	t.Setenv("CLICKHOUSE_PORT", "8443")
	config, err = ConfigFromEnv()
	if err != nil || config.Endpoint != "http://[::1]:8443" {
		t.Fatalf("unexpected endpoint: %s, %v", config.Endpoint, err)
	}
	t.Setenv("CLICKHOUSE_ENDPOINT", "https://database.example:8443")
	t.Setenv("CLICKHOUSE_TIMEOUT", "3s")
	config, err = ConfigFromEnv()
	if err != nil || config.Endpoint != "https://database.example:8443" || config.Timeout != 3*time.Second {
		t.Fatalf("unexpected overridden configuration: %+v, %v", config, err)
	}
	for _, value := range []string{"invalid", "0s", "-1s"} {
		t.Setenv("CLICKHOUSE_TIMEOUT", value)
		if _, err := ConfigFromEnv(); err == nil {
			t.Fatalf("accepted invalid timeout %q", value)
		}
	}
}

func TestClickHouseIntegration(t *testing.T) {
	endpoint := os.Getenv("CLICKHOUSE_TEST_ENDPOINT")
	if endpoint == "" {
		t.Skip("set CLICKHOUSE_TEST_ENDPOINT to run against a real ClickHouse server")
	}
	if path := os.Getenv("CLICKHOUSE_TEST_ENV_FILE"); path != "" {
		values, err := godotenv.Read(path)
		if err != nil {
			t.Fatal("cannot read ClickHouse test environment file")
		}
		for _, key := range []string{"CLICKHOUSE_USERNAME", "CLICKHOUSE_PASSWORD", "CLICKHOUSE_TIMEOUT"} {
			if value, ok := values[key]; ok {
				t.Setenv(key, value)
			}
		}
	}
	config, err := ConfigFromEnv()
	if err != nil {
		t.Fatal(err)
	}
	config.Endpoint = endpoint
	config.Database = fmt.Sprintf("event_exporter_test_%d", time.Now().UnixNano())
	store, err := NewClickHouse(config)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := store.execute(ctx, "CREATE DATABASE `"+config.Database+"`", nil); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		cleanup, cancel := context.WithTimeout(context.Background(), config.Timeout)
		defer cancel()
		if err := store.execute(cleanup, "DROP DATABASE `"+config.Database+"`", nil); err != nil {
			t.Errorf("clean up temporary database: %v", err)
		}
	})
	for range 2 {
		if err := store.EnsureTable(ctx); err != nil {
			t.Fatal(err)
		}
	}
	record := otlp.LogRecord{
		Timestamp: time.Now().UTC(), ServiceName: "integration",
		LogAttributes: map[string]any{"event.name": "topology.session.started", "session.id": "integration-session", "snapshot.version": 1, "application_id": "abcd"},
	}
	event, err := otlp.NormalizeEvent(record)
	if err != nil {
		t.Fatal(err)
	}
	row, err := NewEvent(record, event)
	if err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if err := store.Write(ctx, []Event{row}); err != nil {
			t.Fatal(err)
		}
	}
	query := fmt.Sprintf("SELECT event_id, application_id, payload, toString(ingested_at) AS ingested_at FROM `%s`.normalized_events FINAL FORMAT JSONEachRow", config.Database)
	address, _ := url.Parse(endpoint)
	parameters := address.Query()
	parameters.Set("query", query)
	address.RawQuery = parameters.Encode()
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, address.String(), nil)
	if err != nil {
		t.Fatal(err)
	}
	request.SetBasicAuth(config.Username, config.Password)
	response, err := store.client.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		if err := response.Body.Close(); err != nil {
			t.Errorf("close ClickHouse response body: %v", err)
		}
	}()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("read failed with HTTP %d", response.StatusCode)
	}
	decoder := json.NewDecoder(response.Body)
	var result struct {
		ID            string `json:"event_id"`
		ApplicationID string `json:"application_id"`
		Payload       string `json:"payload"`
		IngestedAt    string `json:"ingested_at"`
	}
	if err := decoder.Decode(&result); err != nil {
		t.Fatal(err)
	}
	if result.ID != row.ID || result.ApplicationID != "abcd" || result.Payload != row.Payload || !strings.HasPrefix(result.IngestedAt, time.Now().UTC().Format("2006-01-02")) {
		t.Fatalf("round-trip mismatch or missing ingestion timestamp: %+v", result)
	}
	if err := decoder.Decode(&result); !errors.Is(err, io.EOF) {
		t.Fatalf("FINAL should return one logical event after retries, got %v", err)
	}
}
