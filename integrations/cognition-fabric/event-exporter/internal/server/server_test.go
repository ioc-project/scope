// Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
// SPDX-License-Identifier: Apache-2.0

package server

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"

	collogspb "go.opentelemetry.io/proto/otlp/collector/logs/v1"
	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
	logspb "go.opentelemetry.io/proto/otlp/logs/v1"
	resourcepb "go.opentelemetry.io/proto/otlp/resource/v1"

	"ioc-event-exporter/internal/config"
	"ioc-event-exporter/internal/rules"
	"ioc-event-exporter/internal/storage"
)

type eventStoreFunc func(context.Context, []storage.Event) error

func (store eventStoreFunc) Write(ctx context.Context, events []storage.Event) error {
	return store(ctx, events)
}

func persistenceRequest() *collogspb.ExportLogsServiceRequest {
	return &collogspb.ExportLogsServiceRequest{ResourceLogs: []*logspb.ResourceLogs{{
		Resource: &resourcepb.Resource{Attributes: []*commonpb.KeyValue{
			{Key: "service.name", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: "agents"}}},
		}},
		ScopeLogs: []*logspb.ScopeLogs{{LogRecords: []*logspb.LogRecord{{
			TimeUnixNano: uint64(time.Date(2026, 10, 14, 12, 0, 0, 0, time.UTC).UnixNano()),
			Attributes: []*commonpb.KeyValue{
				{Key: "event.name", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: "topology.session.started"}}},
				{Key: "session.id", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: "session-1"}}},
				{Key: "snapshot.version", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_IntValue{IntValue: 1}}},
			},
		}}}},
	}}}
}

func TestPersistenceAndPartialSuccess(t *testing.T) {
	var output bytes.Buffer
	var stored []storage.Event
	server := &logsServer{
		logger: log.New(&output, "", 0),
		store: eventStoreFunc(func(ctx context.Context, events []storage.Event) error {
			stored = events
			return nil
		}),
	}
	request := persistenceRequest()
	scope := request.ResourceLogs[0].ScopeLogs[0]
	scope.LogRecords = append(scope.LogRecords, &logspb.LogRecord{})
	response, err := server.Export(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if len(stored) != 1 || stored[0].SessionID != "session-1" || response.GetPartialSuccess().GetRejectedLogRecords() != 1 {
		t.Fatalf("expected valid event persisted without subscribers and invalid record rejected: %+v, %+v", stored, response)
	}
	stored = nil
	if _, err := server.Export(context.Background(), &collogspb.ExportLogsServiceRequest{}); err != nil || stored != nil {
		t.Fatalf("empty requests must not write: %v", err)
	}
}

func TestMessageCompletionRouting(t *testing.T) {
	for _, eventType := range []string{"a2a.message.completed", "mcp.message.completed"} {
		t.Run(eventType, func(t *testing.T) {
			var delivered map[string]any
			callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if err := json.NewDecoder(r.Body).Decode(&delivered); err != nil {
					t.Errorf("decode callback: %v", err)
					w.WriteHeader(http.StatusBadRequest)
					return
				}
				w.WriteHeader(http.StatusOK)
			}))
			defer callback.Close()
			compiled, err := rules.Compile([]config.Rule{{
				Rule:        fmt.Sprintf(`type == %q && data.operation == "tools/call" && data.output.ok == true`, eventType),
				CallbackURL: callback.URL,
			}})
			if err != nil {
				t.Fatal(err)
			}
			request := persistenceRequest()
			record := request.ResourceLogs[0].ScopeLogs[0].LogRecords[0]
			record.EventName = eventType
			for key, value := range map[string]string{
				"event.name": eventType, "source.agent": "planner", "target.agent": "server",
				"operation.name": "tools/call", "message.input": `{"arguments":[1,2]}`,
				"message.output": `{"ok":true}`, "message.id": "msg-1", "fork.id": "fork-1",
				"event.parent.id": strings.Repeat("a", 64),
			} {
				record.Attributes = append(record.Attributes, &commonpb.KeyValue{
					Key: key, Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: value}},
				})
			}
			var stored []storage.Event
			var output bytes.Buffer
			server := &logsServer{
				logger: log.New(&output, "", 0), rules: compiled, httpClient: callback.Client(),
				store: eventStoreFunc(func(ctx context.Context, events []storage.Event) error {
					stored = events
					return nil
				}),
			}
			response, err := server.Export(context.Background(), request)
			if err != nil || response.GetPartialSuccess().GetRejectedLogRecords() != 0 {
				t.Fatalf("completion rejected: response=%+v error=%v output=%s", response, err, output.String())
			}
			if len(stored) != 1 || stored[0].Type != eventType || stored[0].NormalizerVersion != "6" ||
				stored[0].MessageID != "msg-1" || stored[0].ForkID != "fork-1" {
				t.Fatalf("incorrect stored completion: %+v", stored)
			}
			if delivered["type"] != eventType {
				t.Fatalf("completion not routed to callback: %+v; output=%s", delivered, output.String())
			}
			correlation, ok := delivered["correlation"].(map[string]any)
			if !ok || correlation["parent_event_id"] != strings.Repeat("a", 64) {
				t.Fatalf("parent correlation not delivered: %+v", delivered)
			}
			payload, err := json.Marshal(delivered)
			if err != nil {
				t.Fatal(err)
			}
			var persisted map[string]any
			if err := json.Unmarshal([]byte(stored[0].Payload), &persisted); err != nil {
				t.Fatal(err)
			}
			persistedPayload, err := json.Marshal(persisted)
			if err != nil {
				t.Fatal(err)
			}
			if string(payload) != string(persistedPayload) {
				t.Errorf("callback and persistence disagree: callback=%s stored=%s", payload, persistedPayload)
			}
		})
	}
}

func TestDefinitionEventPersistence(t *testing.T) {
	for _, eventType := range []string{"a2a.agent_card", "mcp.tools.discovered", "mcp.tools.published"} {
		for _, state := range []string{"complete", "redacted", "disabled", "truncated", "unavailable"} {
			t.Run(eventType+"/"+state, func(t *testing.T) {
				stringAttribute := func(key, value string) *commonpb.KeyValue {
					return &commonpb.KeyValue{Key: key, Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: value}}}
				}
				request := persistenceRequest()
				resource := request.ResourceLogs[0].Resource
				resource.Attributes = append(resource.Attributes, stringAttribute("app_id", "resource-app"), stringAttribute("tenant.id", "tenant-1"))
				scope := request.ResourceLogs[0].ScopeLogs[0]
				scope.Scope = &commonpb.InstrumentationScope{Name: "ioa_observe.runtime_events"}
				record := scope.LogRecords[0]
				record.EventName = eventType
				source := "server-published"
				if eventType == "mcp.tools.discovered" {
					source = "client-discovered"
				}
				record.Attributes = []*commonpb.KeyValue{
					stringAttribute("event.name", "other.event"),
					stringAttribute("event.time", "2026-10-14T14:42:27.947224+00:00"),
					stringAttribute("snapshot.version", "0"),
					stringAttribute("application_id", "log-app"),
					stringAttribute("capability.source", source),
					stringAttribute("capability.capture.state", state),
				}
				switch state {
				case "complete", "redacted":
					content := `{"name":"Random Number Agent","skills":[{"id":"random_number"}]}`
					if eventType != "a2a.agent_card" {
						content = `[{"name":"random_number","inputSchema":{"type":"object"}}]`
					}
					record.Attributes = append(record.Attributes, stringAttribute("capability.content", content), stringAttribute("capability.provider.id", "http://localhost:9999"))
					record.TraceId = bytes.Repeat([]byte{0xab}, 16)
					record.SpanId = bytes.Repeat([]byte{0xcd}, 8)
					record.Flags = 1
				case "disabled":
					record.TraceId = make([]byte, 16)
					record.SpanId = make([]byte, 8)
				}
				if state == "truncated" {
					record.Attributes = append(record.Attributes, &commonpb.KeyValue{Key: "capability.content.size", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_IntValue{IntValue: 1048576}}})
				}
				var output bytes.Buffer
				var stored []storage.Event
				server := &logsServer{
					logger: log.New(&output, "", 0),
					store: eventStoreFunc(func(ctx context.Context, events []storage.Event) error {
						stored = events
						return nil
					}),
				}
				response, err := server.Export(context.Background(), request)
				if err != nil || response.GetPartialSuccess().GetRejectedLogRecords() != 0 || len(stored) != 1 {
					t.Fatalf("definition was not persisted: rows=%+v response=%+v error=%v", stored, response, err)
				}
				row := stored[0]
				if row.Type != eventType || row.Time != "2026-10-14 14:42:27.947224000" || row.SessionID != "" || row.SessionSequence != 0 || row.ServiceName != "agents" || row.ApplicationID != "log-app" || row.TenantID != "tenant-1" {
					t.Fatalf("incorrect normalized row: %+v", row)
				}
				if row.CallID != "" || row.MessageID != "" || row.ForkID != "" {
					t.Fatalf("invented correlation: %+v", row)
				}
				var payload map[string]any
				if err := json.Unmarshal([]byte(row.Payload), &payload); err != nil {
					t.Fatal(err)
				}
				if payload["source"].(map[string]any)["instrumentation_scope"] != "ioa_observe.runtime_events" {
					t.Fatalf("scope lost: %s", row.Payload)
				}
				capability := payload["data"].(map[string]any)["capability"].(map[string]any)
				if capability["source"] != source || capability["capture"].(map[string]any)["state"] != state {
					t.Fatalf("metadata lost: %s", row.Payload)
				}
				if state == "complete" || state == "redacted" {
					if row.TraceID != strings.Repeat("ab", 16) || row.SpanID != strings.Repeat("cd", 8) || payload["correlation"].(map[string]any)["trace_flags"] != float64(1) {
						t.Fatalf("valid trace correlation lost: %+v", row)
					}
					if capability["provider"].(map[string]any)["id"] != "http://localhost:9999" {
						t.Fatalf("provider lost: %s", row.Payload)
					}
					if eventType == "a2a.agent_card" {
						if content, ok := capability["content"].(map[string]any); !ok || content["name"] != "Random Number Agent" {
							t.Fatalf("agent card not structured: %s", row.Payload)
						}
					} else if content, ok := capability["content"].([]any); !ok || len(content) != 1 || content[0].(map[string]any)["name"] != "random_number" {
						t.Fatalf("tools not structured: %s", row.Payload)
					}
				} else {
					if row.TraceID != "" || row.SpanID != "" || len(payload["correlation"].(map[string]any)) != 0 {
						t.Fatalf("invalid trace context retained: %+v", row)
					}
					if _, exists := capability["content"]; exists {
						t.Fatalf("invented content: %s", row.Payload)
					}
					if _, exists := capability["provider"]; exists {
						t.Fatalf("invented provider: %s", row.Payload)
					}
				}
				if state == "truncated" && capability["content_size"] != float64(1048576) {
					t.Fatalf("content size lost: %s", row.Payload)
				}
			})
		}
	}
}

func TestPersistenceFailureStatus(t *testing.T) {
	for _, test := range []struct {
		name string
		err  error
		code codes.Code
	}{
		{"network", errors.New("connection refused"), codes.Unavailable},
		{"overloaded", &storage.HTTPError{StatusCode: 429}, codes.Unavailable},
		{"unavailable", &storage.HTTPError{StatusCode: 503}, codes.Unavailable},
		{"authentication", &storage.HTTPError{StatusCode: 401}, codes.FailedPrecondition},
		{"schema", &storage.HTTPError{StatusCode: 400}, codes.FailedPrecondition},
	} {
		t.Run(test.name, func(t *testing.T) {
			var output bytes.Buffer
			server := &logsServer{
				logger: log.New(&output, "", 0),
				store: eventStoreFunc(func(ctx context.Context, events []storage.Event) error {
					return test.err
				}),
			}
			response, err := server.Export(context.Background(), persistenceRequest())
			if response != nil || status.Code(err) != test.code {
				t.Fatalf("got response %v and error %v, want %v", response, err, test.code)
			}
		})
	}
}

func TestPushIndependentOfPersistence(t *testing.T) {
	for _, failedWrite := range []bool{false, true} {
		t.Run(fmt.Sprintf("failed_write_%t", failedWrite), func(t *testing.T) {
			started := make(chan struct{})
			callbackDone := make(chan struct{})
			callback := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
				<-started
				close(callbackDone)
				response.WriteHeader(http.StatusServiceUnavailable)
			}))
			defer callback.Close()
			compiled, err := rules.Compile([]config.Rule{{Rule: `type == "topology.session.started"`, CallbackURL: callback.URL}})
			if err != nil {
				t.Fatal(err)
			}
			var output bytes.Buffer
			server := &logsServer{
				logger: log.New(&output, "", 0), rules: compiled, httpClient: callback.Client(),
				store: eventStoreFunc(func(ctx context.Context, events []storage.Event) error {
					close(started)
					select {
					case <-callbackDone:
					case <-ctx.Done():
						return ctx.Err()
					}
					if failedWrite {
						return errors.New("database unavailable")
					}
					return nil
				}),
			}
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			_, err = server.Export(ctx, persistenceRequest())
			want := codes.OK
			if failedWrite {
				want = codes.Unavailable
			}
			if status.Code(err) != want {
				t.Fatalf("got %v, want %v", err, want)
			}
			select {
			case <-callbackDone:
			default:
				t.Fatal("callback did not proceed independently of persistence")
			}
		})
	}
}

func TestLogsServerExport(t *testing.T) {
	var buf bytes.Buffer
	s := &logsServer{logger: log.New(&buf, "", 0)}

	req := &collogspb.ExportLogsServiceRequest{
		ResourceLogs: []*logspb.ResourceLogs{
			{
				ScopeLogs: []*logspb.ScopeLogs{
					{
						LogRecords: []*logspb.LogRecord{
							{
								Body: &commonpb.AnyValue{
									Value: &commonpb.AnyValue_StringValue{StringValue: "hello world"},
								},
							},
						},
					},
				},
			},
		},
	}

	resp, err := s.Export(context.Background(), req)
	if err != nil {
		t.Fatalf("Export returned error: %v", err)
	}
	if resp == nil {
		t.Fatal("Export returned nil response")
	}

	out := buf.String()
	if !strings.Contains(out, "normalize event:") || strings.Contains(out, "hello world") || strings.Contains(out, "===== LOG =====") {
		t.Errorf("expected only a diagnostic for an incomplete log, got: %q", out)
	}
}

func TestLogsServerExportEmptyRequest(t *testing.T) {
	var buf bytes.Buffer
	s := &logsServer{logger: log.New(&buf, "", 0)}

	resp, err := s.Export(context.Background(), &collogspb.ExportLogsServiceRequest{})
	if err != nil {
		t.Fatalf("Export returned error: %v", err)
	}
	if resp == nil {
		t.Fatal("Export returned nil response")
	}
	if buf.Len() != 0 {
		t.Errorf("expected no output for a request with no log records, got: %q", buf.String())
	}
}

func TestRegisterOnlyExposesLogsService(t *testing.T) {
	grpcServer := grpc.NewServer()
	Register(grpcServer, nil, nil)

	info := grpcServer.GetServiceInfo()
	if _, ok := info["opentelemetry.proto.collector.logs.v1.LogsService"]; !ok {
		t.Errorf("expected LogsService to be registered, got services: %v", info)
	}
	if len(info) != 1 {
		t.Errorf("expected only the logs service to be registered, got: %v", info)
	}
}

func TestRegisterAcceptsGzipCompressedLogs(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	grpcServer := grpc.NewServer()
	Register(grpcServer, nil, nil)
	go func() { _ = grpcServer.Serve(listener) }()
	defer grpcServer.Stop()

	conn, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := conn.Close(); err != nil {
			t.Errorf("close gRPC connection: %v", err)
		}
	})

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	client := collogspb.NewLogsServiceClient(conn)
	_, err = client.Export(ctx, &collogspb.ExportLogsServiceRequest{
		ResourceLogs: []*logspb.ResourceLogs{{
			ScopeLogs: []*logspb.ScopeLogs{{
				LogRecords: []*logspb.LogRecord{{Body: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: "compressed event"}}}},
			}},
		}},
	}, grpc.UseCompressor("gzip"))
	if err != nil {
		t.Fatalf("gzip-compressed Export failed: %v", err)
	}
}

func TestLogsServerExportEvaluatesRules(t *testing.T) {
	var mu sync.Mutex
	var paths []string
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		paths = append(paths, r.URL.Path)
		mu.Unlock()
		w.WriteHeader(http.StatusOK)
	}))
	defer callback.Close()
	compiled, err := rules.Compile([]config.Rule{
		{Rule: `type == "tool.completed"`, CallbackURL: callback.URL + "/rule-1"},
		{Rule: `type == "llm.completed"`, CallbackURL: callback.URL + "/rule-2"},
	})
	if err != nil {
		t.Fatalf("Compile returned error: %v", err)
	}

	var buf bytes.Buffer
	s := &logsServer{logger: log.New(&buf, "", 0), rules: compiled, httpClient: &http.Client{}}

	req := &collogspb.ExportLogsServiceRequest{
		ResourceLogs: []*logspb.ResourceLogs{
			{
				Resource: &resourcepb.Resource{Attributes: []*commonpb.KeyValue{
					{Key: "service.name", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: "agent-service"}}},
				}},
				ScopeLogs: []*logspb.ScopeLogs{
					{
						LogRecords: []*logspb.LogRecord{
							{
								TimeUnixNano: uint64(time.Date(2026, 9, 11, 11, 38, 33, 0, time.UTC).UnixNano()),
								Body:         &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: "event"}},
								Attributes: []*commonpb.KeyValue{
									{Key: "type", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: "tool.completed"}}},
									{Key: "session.id", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: "session-1"}}},
									{Key: "snapshot.version", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_IntValue{IntValue: 2}}},
									{Key: "tool.name", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: "lookup_record"}}},
								},
							},
						},
					},
				},
			},
		},
	}

	if _, err := s.Export(context.Background(), req); err != nil {
		t.Fatalf("Export returned error: %v", err)
	}

	out := buf.String()
	mu.Lock()
	defer mu.Unlock()
	if !slices.Equal(paths, []string{"/rule-1"}) {
		t.Errorf("expected only rule 1 callback, got: %v", paths)
	}
	if strings.Contains(out, "rule 1: ok") || strings.Contains(out, "rule 2: ko") {
		t.Errorf("unexpected rule match logging: %q", out)
	}
}

func TestLogsServerExportPostsCallbackOnMatch(t *testing.T) {
	var mu sync.Mutex
	var gotPath string
	var gotEvent map[string]any

	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		gotPath = r.URL.Path
		_ = json.NewDecoder(r.Body).Decode(&gotEvent)
		mu.Unlock()
		w.WriteHeader(http.StatusOK)
	}))
	defer ts.Close()

	compiled, err := rules.Compile([]config.Rule{
		{Rule: `type == "tool.completed"`, CallbackURL: ts.URL + "/webhooks/tool-completed"},
		{Rule: `type == "llm.completed"`, CallbackURL: ts.URL + "/webhooks/llm-completed"},
	})
	if err != nil {
		t.Fatalf("Compile returned error: %v", err)
	}

	var buf bytes.Buffer
	s := &logsServer{logger: log.New(&buf, "", 0), rules: compiled, httpClient: &http.Client{}}

	req := &collogspb.ExportLogsServiceRequest{
		ResourceLogs: []*logspb.ResourceLogs{
			{
				Resource: &resourcepb.Resource{Attributes: []*commonpb.KeyValue{
					{Key: "service.name", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: "agent-service"}}},
				}},
				ScopeLogs: []*logspb.ScopeLogs{
					{
						LogRecords: []*logspb.LogRecord{
							{
								TimeUnixNano: uint64(time.Date(2026, 9, 11, 11, 38, 33, 0, time.UTC).UnixNano()),
								Body:         &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: "event"}},
								Attributes: []*commonpb.KeyValue{
									{Key: "event.name", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: "tool.completed"}}},
									{Key: "session.id", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: "session-1"}}},
									{Key: "snapshot.version", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_IntValue{IntValue: 2}}},
									{Key: "tool.name", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: "lookup_record"}}},
								},
							},
						},
					},
				},
			},
		},
	}

	if _, err := s.Export(context.Background(), req); err != nil {
		t.Fatalf("Export returned error: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	if gotPath != "/webhooks/tool-completed" {
		t.Errorf("expected callback to the matched rule's URL, got path %q", gotPath)
	}
	if gotEvent["type"] != "tool.completed" || gotEvent["time"] != "2026-09-11T11:38:33Z" {
		t.Errorf("expected normalized event type and time, got: %+v", gotEvent)
	}
	if session, ok := gotEvent["session"].(map[string]any); !ok || session["id"] != "session-1" || session["sequence"] != float64(2) {
		t.Errorf("expected normalized session, got: %+v", gotEvent)
	}
	if data, ok := gotEvent["data"].(map[string]any); !ok || data["tool"].(map[string]any)["name"] != "lookup_record" {
		t.Errorf("expected normalized tool data, got: %+v", gotEvent)
	}
	if _, exists := gotEvent["LogAttributes"]; exists {
		t.Errorf("unexpected raw OTLP fields in callback: %+v", gotEvent)
	}
	if !strings.Contains(buf.String(), "===== EVENT =====") || !strings.Contains(buf.String(), `"type": "tool.completed"`) || strings.Contains(buf.String(), "LogAttributes") {
		t.Errorf("expected normalized event in console output, got: %s", buf.String())
	}
}

func TestConfiguredRulesMatchNormalizedEvents(t *testing.T) {
	configuration, err := config.Load("../../config.yaml")
	if err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	var paths []string
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		paths = append(paths, r.URL.Path)
		mu.Unlock()
		w.WriteHeader(http.StatusOK)
	}))
	defer callback.Close()
	for index := range configuration.Rules {
		configuration.Rules[index].CallbackURL = fmt.Sprintf("%s/rule-%d", callback.URL, index+1)
	}
	compiled, err := rules.Compile(configuration.Rules)
	if err != nil {
		t.Fatal(err)
	}
	stringAttribute := func(key, value string) *commonpb.KeyValue {
		return &commonpb.KeyValue{Key: key, Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: value}}}
	}

	for _, test := range []struct {
		name        string
		serviceName string
		attributes  []*commonpb.KeyValue
		wantRules   []int
	}{
		{
			name: "tool from service and agent", serviceName: "weather-agent",
			attributes: []*commonpb.KeyValue{
				stringAttribute("type", "tool.completed"), stringAttribute("tool.name", "lookup"), stringAttribute("agent.name", "researcher"),
			},
			wantRules: []int{1, 2, 7, 8},
		},
		{
			name: "mcp message", serviceName: "agent-service",
			attributes: []*commonpb.KeyValue{
				stringAttribute("event.name", "mcp.message.sent"), stringAttribute("source.agent", "researcher"), stringAttribute("target.agent", "writer"),
			},
			wantRules: []int{5},
		},
		{
			name: "agent handoff", serviceName: "agent-service",
			attributes: []*commonpb.KeyValue{
				stringAttribute("event.name", "topology.edge.updated"), stringAttribute("edge.id", "edge-1"),
				stringAttribute("edge.kind", "agent_handoff"), stringAttribute("edge.status", "sent"),
				stringAttribute("source.agent", "researcher"), stringAttribute("target.agent", "writer"),
			},
			wantRules: []int{6},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			mu.Lock()
			paths = nil
			mu.Unlock()
			var output bytes.Buffer
			server := &logsServer{logger: log.New(&output, "", 0), rules: compiled, httpClient: callback.Client()}
			attributes := append([]*commonpb.KeyValue{
				stringAttribute("session.id", "session-1"),
				{Key: "snapshot.version", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_IntValue{IntValue: 1}}},
			}, test.attributes...)
			request := &collogspb.ExportLogsServiceRequest{ResourceLogs: []*logspb.ResourceLogs{{
				Resource: &resourcepb.Resource{Attributes: []*commonpb.KeyValue{stringAttribute("service.name", test.serviceName)}},
				ScopeLogs: []*logspb.ScopeLogs{{LogRecords: []*logspb.LogRecord{{
					TimeUnixNano: uint64(time.Date(2026, 9, 11, 11, 38, 33, 0, time.UTC).UnixNano()),
					Attributes:   attributes,
				}}}},
			}}}
			if _, err := server.Export(context.Background(), request); err != nil {
				t.Fatal(err)
			}
			var matchedRules []int
			mu.Lock()
			defer mu.Unlock()
			for _, rule := range compiled {
				if slices.Contains(paths, fmt.Sprintf("/rule-%d", rule.Number)) {
					matchedRules = append(matchedRules, rule.Number)
				}
			}
			if !slices.Equal(matchedRules, test.wantRules) {
				t.Errorf("matched rules = %v, want %v; output: %s", matchedRules, test.wantRules, output.String())
			}
		})
	}
}

func TestLogsServerSkipsInvalidClientEvent(t *testing.T) {
	t.Setenv("DEBUG_RAW_EVENTS", "")
	called := false
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
	}))
	defer callback.Close()

	compiled, err := rules.Compile([]config.Rule{
		{Rule: `type == "tool.completed"`, CallbackURL: callback.URL},
		{Rule: `type == "tool.completed"`, CallbackURL: callback.URL},
	})
	if err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	server := &logsServer{logger: log.New(&output, "", 0), rules: compiled, httpClient: callback.Client()}
	request := &collogspb.ExportLogsServiceRequest{ResourceLogs: []*logspb.ResourceLogs{{ScopeLogs: []*logspb.ScopeLogs{{LogRecords: []*logspb.LogRecord{{Attributes: []*commonpb.KeyValue{{Key: "type", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: "tool.completed"}}}}}}}}}}}

	if _, err := server.Export(context.Background(), request); err != nil {
		t.Fatal(err)
	}
	if called {
		t.Fatal("incomplete event was sent to callback")
	}
	if strings.Count(output.String(), "normalize event") != 1 || strings.Contains(output.String(), "===== EVENT =====") || strings.Contains(output.String(), "LogAttributes") {
		t.Errorf("expected one normalization failure and no event payload: %s", output.String())
	}
}

func TestLogsServerDebugsUnnormalizedLLMRecord(t *testing.T) {
	t.Setenv("DEBUG_RAW_EVENTS", "1")
	var output bytes.Buffer
	server := &logsServer{logger: log.New(&output, "", 0)}
	stringValue := func(value string) *commonpb.AnyValue {
		return &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: value}}
	}
	request := &collogspb.ExportLogsServiceRequest{
		ResourceLogs: []*logspb.ResourceLogs{{
			Resource: &resourcepb.Resource{Attributes: []*commonpb.KeyValue{{Key: "service.name", Value: stringValue("multi-agent-service")}}},
			ScopeLogs: []*logspb.ScopeLogs{{
				LogRecords: []*logspb.LogRecord{{
					TimeUnixNano: uint64(time.Date(2026, 10, 7, 18, 22, 32, 0, time.UTC).UnixNano()),
					Body:         stringValue("llm.completed"),
					Attributes: []*commonpb.KeyValue{
						{Key: "event.name", Value: stringValue("llm.completed")},
						{Key: "session.id", Value: stringValue("session-1")},
						{Key: "snapshot.version", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_IntValue{IntValue: 38}}},
						{Key: "llm.call.id", Value: stringValue("b4098d776707810d")},
						{Key: "llm.name", Value: stringValue("execute_task coder")},
					},
				}},
			}},
		}},
	}
	if _, err := server.Export(context.Background(), request); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(output.String(), "llm.completed requires model") || !strings.Contains(output.String(), "===== RAW OTLP LOG =====") || !strings.Contains(output.String(), `"llm.call.id": "b4098d776707810d"`) || !strings.Contains(output.String(), `"llm.name": "execute_task coder"`) || strings.Contains(output.String(), "===== EVENT =====") {
		t.Errorf("expected failed normalization with its raw input, got: %s", output.String())
	}
}
