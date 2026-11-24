// Package server implements the OTLP/gRPC logs collector service: it prints
// normalized events and evaluates them against notification rules.
package server

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	_ "google.golang.org/grpc/encoding/gzip"
	"google.golang.org/grpc/status"

	collogspb "go.opentelemetry.io/proto/otlp/collector/logs/v1"

	"oxp-event-exporter/internal/otlp"
	"oxp-event-exporter/internal/rules"
	"oxp-event-exporter/internal/storage"
)

// callbackTimeout bounds how long Export waits for a rule's callback_url to
// respond, so a slow or unreachable webhook can't stall log ingestion.
const callbackTimeout = 5 * time.Second

// Register wires the logs OTLP service into grpcServer, evaluating normalized
// events against notificationRules.
func Register(grpcServer *grpc.Server, notificationRules []rules.Rule, store EventStore) {
	logger := log.New(os.Stdout, "", log.LstdFlags)
	httpClient := &http.Client{Timeout: callbackTimeout}
	collogspb.RegisterLogsServiceServer(grpcServer, &logsServer{logger: logger, rules: notificationRules, httpClient: httpClient, store: store})
}

func printEvent(logger *log.Logger, event otlp.ClientEvent) {
	b, err := json.MarshalIndent(event, "", "  ")
	if err != nil {
		logger.Printf("failed to marshal client event: %v", err)
		return
	}
	logger.Printf("===== EVENT =====\n%s", b)
}

func printRawRecord(logger *log.Logger, record otlp.LogRecord) {
	b, err := json.MarshalIndent(record, "", "  ")
	if err != nil {
		logger.Printf("failed to marshal raw OTLP log: %v", err)
		return
	}
	logger.Printf("===== RAW OTLP LOG =====\n%s", b)
}

func evaluateRules(logger *log.Logger, httpClient *http.Client, notificationRules []rules.Rule, event otlp.ClientEvent) {
	if len(notificationRules) == 0 {
		return
	}
	encoded, err := json.Marshal(event)
	if err != nil {
		logger.Printf("marshal event for rules: %v", err)
		return
	}
	var attrs map[string]any
	if err := json.Unmarshal(encoded, &attrs); err != nil {
		logger.Printf("decode event for rules: %v", err)
		return
	}
	for _, rule := range notificationRules {
		matched, err := rule.Matches(attrs)
		if err != nil {
			logger.Printf("rule %d: evaluation error: %v", rule.Number, err)
			continue
		}
		if !matched {
			continue
		}
		if err := postCallback(httpClient, rule.CallbackURL, event); err != nil {
			logger.Printf("rule %d: callback to %s failed: %v", rule.Number, rule.CallbackURL, err)
		}
	}
}

// postCallback sends a normalized client event to the matched rule's callback_url.
func postCallback(httpClient *http.Client, url string, event otlp.ClientEvent) error {
	body, err := json.Marshal(event)
	if err != nil {
		return fmt.Errorf("marshal client event: %w", err)
	}

	resp, err := httpClient.Post(url, "application/json", bytes.NewReader(body))
	if err != nil {
		return err
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode >= 300 {
		return fmt.Errorf("callback returned status %d", resp.StatusCode)
	}
	return nil
}

type EventStore interface {
	Write(context.Context, []storage.Event) error
}

type logsServer struct {
	collogspb.UnimplementedLogsServiceServer
	logger     *log.Logger
	rules      []rules.Rule
	httpClient *http.Client
	store      EventStore
}

func (s *logsServer) Export(ctx context.Context, req *collogspb.ExportLogsServiceRequest) (*collogspb.ExportLogsServiceResponse, error) {
	var events []otlp.ClientEvent
	var rows []storage.Event
	var rejected int64
	for _, record := range otlp.FlattenLogs(req) {
		event, err := otlp.NormalizeEvent(record)
		if err != nil {
			rejected++
			s.logger.Printf("normalize event: %v", err)
			if os.Getenv("DEBUG_RAW_EVENTS") == "1" {
				printRawRecord(s.logger, record)
			}
			continue
		}
		if s.store != nil {
			row, err := storage.NewEvent(record, event)
			if err != nil {
				rejected++
				s.logger.Printf("encode event for persistence: %v", err)
				continue
			}
			rows = append(rows, row)
		}
		events = append(events, event)
	}
	var persisted chan error
	if len(rows) > 0 {
		persisted = make(chan error, 1)
		go func() { persisted <- s.store.Write(ctx, rows) }()
	}
	for _, event := range events {
		printEvent(s.logger, event)
		evaluateRules(s.logger, s.httpClient, s.rules, event)
	}
	if persisted != nil {
		if err := <-persisted; err != nil {
			s.logger.Printf("persist normalized events: %v", err)
			if ctx.Err() != nil {
				return nil, status.FromContextError(ctx.Err()).Err()
			}
			var failure *storage.HTTPError
			if errors.As(err, &failure) && !failure.Retryable() {
				return nil, status.Error(codes.FailedPrecondition, "ClickHouse rejected event persistence; check database configuration and schema")
			}
			return nil, status.Error(codes.Unavailable, "normalized event persistence failed; retry the batch")
		}
	}
	response := &collogspb.ExportLogsServiceResponse{}
	if rejected > 0 {
		response.PartialSuccess = &collogspb.ExportLogsPartialSuccess{
			RejectedLogRecords: rejected,
			ErrorMessage:       "records could not be normalized or encoded; see exporter diagnostics",
		}
	}
	return response, nil
}
