package otlp

import (
	"encoding/json"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"

	collogspb "go.opentelemetry.io/proto/otlp/collector/logs/v1"
	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
	logspb "go.opentelemetry.io/proto/otlp/logs/v1"
	resourcepb "go.opentelemetry.io/proto/otlp/resource/v1"
)

func strVal(s string) *commonpb.AnyValue {
	return &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: s}}
}

func TestNormalizeDefinitionEvents(t *testing.T) {
	for _, eventType := range []string{"a2a.agent_card", "mcp.tools.discovered", "mcp.tools.published"} {
		for _, state := range []string{"complete", "redacted", "disabled", "truncated", "unavailable"} {
			t.Run(eventType+"/"+state, func(t *testing.T) {
				attrs := map[string]any{
					"event.name": eventType, "event.time": "2026-10-14T14:42:27.947224+00:00",
					"snapshot.version": "0", "capability.capture.state": state,
					"capability.source": "server-published", "capability.provider.id": "http://localhost:9999/a2a/jsonrpc",
				}
				if state == "complete" || state == "redacted" {
					attrs["capability.content"] = `{"name":"Random Number Agent","skills":[{"id":"random_number"}]}`
				}
				if state == "truncated" {
					attrs["capability.content.size"] = int64(1048576)
				}
				event, err := NormalizeEvent(LogRecord{
					Timestamp: time.Now(), ScopeName: "ioa_observe.runtime_events", LogAttributes: attrs,
					ResourceAttributes: map[string]any{"service.name": "agent-service"},
				})
				if err != nil {
					t.Fatal(err)
				}
				if event.Type != eventType || event.Time.UTC().Format(time.RFC3339Nano) != "2026-10-14T14:42:27.947224Z" || event.Session.ID != "" || event.Session.Sequence != 0 || event.Source.ServiceName != "agent-service" {
					t.Fatalf("unexpected envelope: %+v", event)
				}
				if event.Correlation.TraceID != nil || event.Correlation.SpanID != nil || event.Correlation.CallID != nil {
					t.Fatalf("invented correlation: %+v", event.Correlation)
				}
				capability := event.Data["capability"].(map[string]any)
				if capability["source"] != "server-published" || capability["capture"].(map[string]any)["state"] != state || capability["provider"].(map[string]any)["id"] != attrs["capability.provider.id"] {
					t.Fatalf("missing capture metadata: %+v", capability)
				}
				if _, exists := attrs["capability.content"]; exists {
					if content, ok := capability["content"].(map[string]any); !ok || content["name"] != "Random Number Agent" {
						t.Fatalf("content was not decoded: %+v", capability)
					}
				} else if _, exists := capability["content"]; exists {
					t.Fatalf("invented content: %+v", capability)
				}
				if state == "truncated" && capability["content_size"] != int64(1048576) {
					t.Fatalf("missing content size: %+v", capability)
				}
				for _, field := range []string{"agent", "tool", "edge", "input", "output"} {
					if _, exists := event.Data[field]; exists {
						t.Fatalf("definition contains lifecycle field %q", field)
					}
				}
			})
		}
	}
}

func TestNormalizeDefinitionVersions(t *testing.T) {
	tests := []struct {
		version any
		valid   bool
	}{
		{"0", true}, {int(0), true}, {int64(0), true}, {float64(0), true}, {int64(1), true},
		{nil, false}, {int64(-1), false}, {float64(0.5), false}, {"invalid", false}, {map[string]any{}, false},
	}
	for _, test := range tests {
		t.Run(fmt.Sprintf("%T/%v", test.version, test.version), func(t *testing.T) {
			_, err := NormalizeEvent(LogRecord{
				Timestamp: time.Now(), ServiceName: "agents", EventName: "mcp.tools.discovered",
				LogAttributes: map[string]any{"snapshot.version": test.version},
			})
			if (err == nil) != test.valid {
				t.Fatalf("version %v: normalization error = %v, valid = %t", test.version, err, test.valid)
			}
		})
	}
}

func TestNormalizeTopologyEdgeObservation(t *testing.T) {
	attrs := map[string]any{
		"event.id":   "57dc9ac85822f1677e3581f82c2ad972640b0fad760508fd5703042de1272527",
		"event.name": "topology.edge.updated", "event.time": "2026-10-15T12:59:34.954725+00:00",
		"message.id": "4", "network.protocol.name": "mcp", "operation.name": "tools/call", "sequence": "1",
		"session.id": "mcp_native_client_054a29a0-2d3c-4c6c-9603-ce4d6006f4a9", "snapshot.version": "5",
		"source.agent": "math_agent", "source.agent.span.id": "759b3adbd6493545",
		"source.agent.trace.id": "0a52b232b83719d8c0012b6d47f2ad5d", "span.id": "3fdb0bd6ac70115f",
		"target.agent": "mcp", "topology.edge.confidence": "1", "topology.edge.evidence": "transport:mcp",
		"topology.edge.id": "mcp:math_agent->mcp", "topology.edge.kind": "mcp_message", "topology.edge.status": "received",
		"trace.id": "0a52b232b83719d8c0012b6d47f2ad5d",
	}
	event, err := NormalizeEvent(LogRecord{
		EventName: "topology.edge.updated", ServiceName: "agents", LogAttributes: attrs,
		TraceId: attrs["trace.id"].(string), SpanId: attrs["span.id"].(string),
	})
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := json.Marshal(event)
	if err != nil {
		t.Fatal(err)
	}
	var envelope map[string]any
	if err := json.Unmarshal(encoded, &envelope); err != nil {
		t.Fatal(err)
	}
	if envelope["event_id"] != attrs["event.id"] || event.Session.Sequence != 5 || event.Session.ID != attrs["session.id"] || event.Time.Format(time.RFC3339Nano) != "2026-10-15T12:59:34.954725Z" {
		t.Errorf("missing source event metadata: %s", encoded)
	}
	want := map[string]any{"edge": map[string]any{
		"id": "mcp:math_agent->mcp", "kind": "mcp_message", "status": "received",
		"source_agent": "math_agent", "target_agent": "mcp", "protocol": "mcp", "operation": "tools/call",
		"message_id": "4", "sequence": 1, "confidence": 1, "evidence": "transport:mcp",
		"source_agent_trace_id": attrs["source.agent.trace.id"], "source_agent_span_id": attrs["source.agent.span.id"],
	}}
	gotData, _ := json.Marshal(event.Data)
	wantData, _ := json.Marshal(want)
	if string(gotData) != string(wantData) {
		t.Errorf("data = %s, want %s", gotData, wantData)
	}
	if event.Correlation.TraceID == nil || *event.Correlation.TraceID != attrs["trace.id"] || event.Correlation.SpanID == nil || *event.Correlation.SpanID != attrs["span.id"] || event.Correlation.MessageID == nil || *event.Correlation.MessageID != "4" {
		t.Errorf("incorrect event correlation: %+v", event.Correlation)
	}
	for _, test := range []struct {
		name  string
		value any
		want  any
	}{
		{"zero confidence", "0", float64(0)},
		{"fractional confidence", "0.75", 0.75},
		{"numeric confidence", float64(0.5), 0.5},
		{"integer confidence", int64(1), float64(1)},
		{"negative confidence", "-1", nil},
		{"excessive confidence", "1.1", nil},
		{"invalid confidence", "invalid", nil},
		{"non-finite confidence", "NaN", nil},
		{"absent confidence", nil, nil},
	} {
		t.Run(test.name, func(t *testing.T) {
			attrs["topology.edge.confidence"] = test.value
			attrs["source.agent.trace.id"] = "invalid"
			attrs["source.agent.span.id"] = "0000000000000000"
			event, err := NormalizeEvent(LogRecord{ServiceName: "agents", LogAttributes: attrs})
			if err != nil {
				t.Fatal(err)
			}
			edge := event.Data["edge"].(map[string]any)
			if edge["confidence"] != test.want {
				t.Errorf("confidence = %v, want %v", edge["confidence"], test.want)
			}
			if edge["source_agent_trace_id"] != nil || edge["source_agent_span_id"] != nil {
				t.Errorf("invalid source-agent context retained: %+v", edge)
			}
		})
	}
}

func TestNormalizeEventFamilies(t *testing.T) {
	tests := []struct {
		name  string
		attrs map[string]any
		data  map[string]any
	}{
		{"session", map[string]any{"event.name": "topology.session.started"}, map[string]any{}},
		{"agent", map[string]any{"event.name": "topology.node.completed", "agent.name": "planner", "agent.output": `{"ok":true}`}, map[string]any{"agent": map[string]any{"name": "planner"}, "output": map[string]any{"ok": true}}},
		{"edge", map[string]any{"event.name": "topology.edge.updated", "topology.edge.id": "edge-1", "topology.edge.kind": "agent_handoff", "topology.edge.status": "sent", "source.agent": "planner", "target.agent": "executor", "edge.sequence": int64(0)}, map[string]any{"edge": map[string]any{"id": "edge-1", "kind": "agent_handoff", "status": "sent", "source_agent": "planner", "target_agent": "executor", "sequence": int64(0)}}},
		{"llm", map[string]any{"event.name": "llm.completed", "gen_ai.request.model": "gpt-4o", "llm.input": []any{map[string]any{"role": "user"}}}, map[string]any{"call_id": "aabbccddeeff0011", "model": "gpt-4o", "input": []any{map[string]any{"role": "user"}}}},
		{"observe chat model", map[string]any{"event.name": "llm.completed", "llm.call.id": "85b7499a0a39747e", "llm.name": "gpt-4o-2024-08-06", "operation.name": "chat", "agent.name": "research"}, map[string]any{"call_id": "85b7499a0a39747e", "model": "gpt-4o-2024-08-06", "operation": "chat", "agent": map[string]any{"name": "research"}}},
		{"tool", map[string]any{"event.name": "tool.started", "tool.name": "lookup", "gen_ai.tool.call.id": "call-1"}, map[string]any{"tool": map[string]any{"name": "lookup", "call_id": "call-1"}}},
		{"message", map[string]any{"event.name": "mcp.message.sent", "source_agent": "planner", "target.agent": "server", "message.id": "msg-1"}, map[string]any{"protocol": "mcp", "source_agent": "planner", "target_agent": "server", "message_id": "msg-1"}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			attrs := map[string]any{"session.id": "session-1", "snapshot.version": int64(2), "event.time": "2026-09-11T11:38:33Z"}
			for key, value := range test.attrs {
				attrs[key] = value
			}
			record := LogRecord{
				Timestamp:     time.Date(2020, 1, 1, 0, 0, 0, 0, time.UTC),
				ServiceName:   "agent-service",
				SpanId:        "aabbccddeeff0011",
				LogAttributes: attrs,
			}
			event, err := NormalizeEvent(record)
			if err != nil {
				t.Fatal(err)
			}
			if event.Type != attrs["event.name"] || event.Time.Format(time.RFC3339Nano) != attrs["event.time"] {
				t.Errorf("event type/time = %q/%q", event.Type, event.Time)
			}
			if event.Session.ID != "session-1" || event.Session.Sequence != 2 || event.Source.ServiceName != "agent-service" {
				t.Errorf("invalid envelope: %+v", event)
			}
			got, _ := json.Marshal(event.Data)
			want, _ := json.Marshal(test.data)
			if string(got) != string(want) {
				t.Errorf("data = %s, want %s", got, want)
			}
			if test.name == "tool" && (event.Correlation.CallID == nil || *event.Correlation.CallID != "call-1") {
				t.Errorf("tool call correlation missing: %+v", event.Correlation)
			}
			if test.name == "observe chat model" && (event.Correlation.CallID == nil || *event.Correlation.CallID != "85b7499a0a39747e") {
				t.Errorf("LLM call correlation missing: %+v", event.Correlation)
			}
		})
	}
}

func TestNormalizeMessageEvents(t *testing.T) {
	for _, eventType := range []string{
		"a2a.message.sent", "a2a.message.received", "a2a.message.completed",
		"mcp.message.sent", "mcp.message.received", "mcp.message.completed",
		"slim.message.sent", "slim.message.received",
	} {
		for _, payload := range []struct {
			name  string
			attrs map[string]any
			want  map[string]any
		}{
			{
				name: "structured content",
				attrs: map[string]any{
					"message.input":  `{"arguments":{"count":2},"enabled":true}`,
					"message.output": `[{"text":"done","values":[1,2]}]`,
				},
				want: map[string]any{
					"input":  map[string]any{"arguments": map[string]any{"count": float64(2)}, "enabled": true},
					"output": []any{map[string]any{"text": "done", "values": []any{float64(1), float64(2)}}},
				},
			},
			{name: "content capture disabled", attrs: map[string]any{}, want: map[string]any{}},
			{
				name:  "input only",
				attrs: map[string]any{"message.input": `{"prompt":"hello"}`},
				want:  map[string]any{"input": map[string]any{"prompt": "hello"}},
			},
			{
				name:  "output only",
				attrs: map[string]any{"message.output": `{"ok":true}`},
				want:  map[string]any{"output": map[string]any{"ok": true}},
			},
			{
				name:  "legacy data aliases",
				attrs: map[string]any{"data.input": `["hello"]`, "data.output": "plain text"},
				want:  map[string]any{"input": []any{"hello"}, "output": "plain text"},
			},
		} {
			t.Run(eventType+"/"+payload.name, func(t *testing.T) {
				attrs := map[string]any{
					"session.id": "session-1", "snapshot.version": int64(2),
					"source.agent": "planner", "target.agent": "server",
					"operation.name": "tools/call", "operation": "legacy-operation",
					"message.id": "msg-1", "fork.id": "fork-1", "message.sequence": int64(0),
					"event.parent.id": strings.Repeat("a", 64),
				}
				for key, value := range payload.attrs {
					attrs[key] = value
				}
				record := LogRecord{
					EventName: eventType, Timestamp: time.Now(), ServiceName: "agents",
					TraceId: "0a52b232b83719d8c0012b6d47f2ad5d", SpanId: "aabbccddeeff0011",
					TraceFlags: 1, LogAttributes: attrs,
				}
				event, err := NormalizeEvent(record)
				if err != nil {
					t.Fatal(err)
				}
				for _, field := range []string{"input", "output"} {
					got, exists := event.Data[field]
					want, wantExists := payload.want[field]
					if exists != wantExists || !reflect.DeepEqual(got, want) {
						t.Errorf("data.%s = %#v (present %t), want %#v (present %t)", field, got, exists, want, wantExists)
					}
				}
				if event.Type != eventType || event.Data["protocol"] != strings.SplitN(eventType, ".", 2)[0] ||
					event.Data["source_agent"] != "planner" || event.Data["target_agent"] != "server" ||
					event.Data["operation"] != "tools/call" || event.Data["message_id"] != "msg-1" ||
					event.Data["fork_id"] != "fork-1" || event.Data["sequence"] != int64(0) {
					t.Errorf("incorrect message data: %+v", event)
				}
				wantCorrelation := EnvelopeCorrelation{
					TraceID: &record.TraceId, SpanID: &record.SpanId,
					TraceFlags: new(1), MessageID: new("msg-1"), ForkID: new("fork-1"),
					ParentEventID: new(strings.Repeat("a", 64)),
				}
				if !reflect.DeepEqual(event.Correlation, wantCorrelation) {
					t.Errorf("correlation = %+v, want %+v", event.Correlation, wantCorrelation)
				}
			})
		}
	}
}

func TestNormalizeNodePayloads(t *testing.T) {
	for _, eventType := range []string{"topology.node.started", "topology.node.completed"} {
		for _, capture := range []bool{true, false} {
			t.Run(fmt.Sprintf("%s/capture=%t", eventType, capture), func(t *testing.T) {
				attrs := map[string]any{
					"session.id": "session-1", "snapshot.version": int64(2), "agent.name": "planner",
				}
				if capture {
					attrs["agent.input"] = `{"messages":[{"role":"user","content":"hello"}]}`
					attrs["agent.output"] = `{"result":{"ok":true}}`
				}
				event, err := NormalizeEvent(LogRecord{
					EventName: eventType, Timestamp: time.Now(), ServiceName: "agents", LogAttributes: attrs,
				})
				if err != nil {
					t.Fatal(err)
				}
				if capture {
					wantInput := map[string]any{"messages": []any{map[string]any{"role": "user", "content": "hello"}}}
					wantOutput := map[string]any{"result": map[string]any{"ok": true}}
					if !reflect.DeepEqual(event.Data["input"], wantInput) || !reflect.DeepEqual(event.Data["output"], wantOutput) {
						t.Errorf("incorrect node payload: %+v", event.Data)
					}
				} else {
					for _, field := range []string{"input", "output"} {
						if _, exists := event.Data[field]; exists {
							t.Errorf("invented data.%s: %+v", field, event.Data)
						}
					}
				}
			})
		}
	}
}

func TestNormalizeParentEventID(t *testing.T) {
	for _, parentID := range []string{"", strings.Repeat("b", 64)} {
		t.Run("parent="+parentID, func(t *testing.T) {
			attrs := map[string]any{"session.id": "session-1", "snapshot.version": int64(1)}
			if parentID != "" {
				attrs["event.parent.id"] = parentID
			}
			event, err := NormalizeEvent(LogRecord{
				EventName: "topology.session.started", Timestamp: time.Now(), ServiceName: "agents", LogAttributes: attrs,
			})
			if err != nil {
				t.Fatal(err)
			}
			encoded, err := json.Marshal(event.Correlation)
			if err != nil {
				t.Fatal(err)
			}
			var correlation map[string]any
			if err := json.Unmarshal(encoded, &correlation); err != nil {
				t.Fatal(err)
			}
			got, exists := correlation["parent_event_id"]
			if (parentID != "") != exists || (exists && got != parentID) {
				t.Errorf("parent ID %q: correlation = %s", parentID, encoded)
			}
		})
	}
}

func TestNormalizeEventRejectsIncompleteRecords(t *testing.T) {
	base := map[string]any{"event.name": "tool.started", "session.id": "session-1", "snapshot.version": int64(1), "tool.name": "lookup"}
	tests := []struct {
		name   string
		change func(map[string]any)
	}{
		{"missing session", func(attrs map[string]any) { delete(attrs, "session.id") }},
		{"invalid sequence", func(attrs map[string]any) { attrs["snapshot.version"] = 0 }},
		{"missing tool", func(attrs map[string]any) { delete(attrs, "tool.name") }},
		{"unsupported type", func(attrs map[string]any) { attrs["event.name"] = "other.event" }},
		{"bad time", func(attrs map[string]any) { attrs["event.time"] = "yesterday" }},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			attrs := make(map[string]any, len(base))
			for key, value := range base {
				attrs[key] = value
			}
			test.change(attrs)
			_, err := NormalizeEvent(LogRecord{Timestamp: time.Now(), ServiceName: "agent-service", LogAttributes: attrs})
			if err == nil {
				t.Fatal("expected a normalization error")
			}
		})
	}
}

func TestNormalizeEventRejectsMissingTimestamp(t *testing.T) {
	record := LogRecord{
		Timestamp:     time.Unix(0, 0).UTC(),
		ServiceName:   "agent-service",
		LogAttributes: map[string]any{"event.name": "topology.session.started", "session.id": "session-1", "snapshot.version": int64(1)},
	}
	if _, err := NormalizeEvent(record); err == nil {
		t.Fatal("expected an error for a missing OTLP and event timestamp")
	}
}

func TestNormalizeEventReportsMissingLLMModel(t *testing.T) {
	record := LogRecord{
		Timestamp:   time.Date(2026, 10, 7, 18, 22, 32, 0, time.UTC),
		ServiceName: "multi-agent-service",
		SpanId:      "b4098d776707810d",
		LogAttributes: map[string]any{
			"event.name":       "llm.completed",
			"event.time":       "2026-10-07T18:22:32.942032+00:00",
			"llm.call.id":      "b4098d776707810d",
			"llm.name":         "execute_task coder",
			"operation.name":   "execute_task",
			"session.id":       "multi-agent-service_5c73a2c8-e9ce-4859-b046-4859ba4affcb",
			"snapshot.version": int64(38),
		},
	}
	_, err := NormalizeEvent(record)
	if err == nil || err.Error() != "llm.completed requires model" {
		t.Fatalf("expected missing-model error (not a missing-call-ID error), got %v", err)
	}
	record.LogAttributes["gen_ai.request.model"] = "gpt-4o"
	event, err := NormalizeEvent(record)
	if err != nil {
		t.Fatal(err)
	}
	if event.Correlation.CallID == nil || *event.Correlation.CallID != record.LogAttributes["llm.call.id"] || event.Data["model"] != "gpt-4o" || event.Data["operation"] != "execute_task" {
		t.Errorf("expected correlated LLM event after supplying the model, got %+v", event)
	}
}

func TestFlattenLogs(t *testing.T) {
	const timeUnixNano = 1_700_000_000_123_456_789

	req := &collogspb.ExportLogsServiceRequest{
		ResourceLogs: []*logspb.ResourceLogs{
			{
				SchemaUrl: "https://opentelemetry.io/schemas/1.20.0",
				Resource: &resourcepb.Resource{
					Attributes: []*commonpb.KeyValue{
						{Key: "service.name", Value: strVal("weather-agent")},
					},
				},
				ScopeLogs: []*logspb.ScopeLogs{
					{
						SchemaUrl: "https://opentelemetry.io/schemas/1.20.0",
						Scope: &commonpb.InstrumentationScope{
							Name:    "weather-agent-logger",
							Version: "1.2.3",
							Attributes: []*commonpb.KeyValue{
								{Key: "scope.attr", Value: strVal("scope-value")},
							},
						},
						LogRecords: []*logspb.LogRecord{
							{
								TimeUnixNano:   timeUnixNano,
								TraceId:        []byte{0x01, 0x02, 0x03, 0x04},
								SpanId:         []byte{0xaa, 0xbb},
								Flags:          1,
								SeverityText:   "INFO",
								SeverityNumber: logspb.SeverityNumber_SEVERITY_NUMBER_INFO,
								Body:           strVal("hello world"),
								Attributes: []*commonpb.KeyValue{
									{Key: "log.attr", Value: strVal("log-value")},
								},
							},
						},
					},
				},
			},
		},
	}

	records := FlattenLogs(req)
	if len(records) != 1 {
		t.Fatalf("expected 1 record, got %d", len(records))
	}
	got := records[0]

	wantTimestamp := time.Unix(0, timeUnixNano).UTC()
	if !got.Timestamp.Equal(wantTimestamp) {
		t.Errorf("Timestamp = %v, want %v", got.Timestamp, wantTimestamp)
	}
	if got.TraceId != "01020304" {
		t.Errorf("TraceId = %q, want %q", got.TraceId, "01020304")
	}
	if got.SpanId != "aabb" {
		t.Errorf("SpanId = %q, want %q", got.SpanId, "aabb")
	}
	if got.TraceFlags != 1 {
		t.Errorf("TraceFlags = %d, want 1", got.TraceFlags)
	}
	if got.SeverityText != "INFO" {
		t.Errorf("SeverityText = %q, want %q", got.SeverityText, "INFO")
	}
	if got.SeverityNumber != uint8(logspb.SeverityNumber_SEVERITY_NUMBER_INFO) {
		t.Errorf("SeverityNumber = %d, want %d", got.SeverityNumber, logspb.SeverityNumber_SEVERITY_NUMBER_INFO)
	}
	if got.ServiceName != "weather-agent" {
		t.Errorf("ServiceName = %q, want %q", got.ServiceName, "weather-agent")
	}
	if got.Body != "hello world" {
		t.Errorf("Body = %q, want %q", got.Body, "hello world")
	}
	if got.ResourceSchemaUrl != "https://opentelemetry.io/schemas/1.20.0" {
		t.Errorf("ResourceSchemaUrl = %q", got.ResourceSchemaUrl)
	}
	if got.ResourceAttributes["service.name"] != "weather-agent" {
		t.Errorf("ResourceAttributes[service.name] = %q", got.ResourceAttributes["service.name"])
	}
	if got.ScopeName != "weather-agent-logger" {
		t.Errorf("ScopeName = %q", got.ScopeName)
	}
	if got.ScopeVersion != "1.2.3" {
		t.Errorf("ScopeVersion = %q", got.ScopeVersion)
	}
	if got.ScopeAttributes["scope.attr"] != "scope-value" {
		t.Errorf("ScopeAttributes[scope.attr] = %q", got.ScopeAttributes["scope.attr"])
	}
	if got.LogAttributes["log.attr"] != "log-value" {
		t.Errorf("LogAttributes[log.attr] = %q", got.LogAttributes["log.attr"])
	}
}

func TestFlattenLogsNestedAttributes(t *testing.T) {
	req := &collogspb.ExportLogsServiceRequest{
		ResourceLogs: []*logspb.ResourceLogs{
			{
				ScopeLogs: []*logspb.ScopeLogs{
					{
						LogRecords: []*logspb.LogRecord{
							{
								Body: strVal("event"),
								Attributes: []*commonpb.KeyValue{
									{
										Key: "http.request",
										Value: &commonpb.AnyValue{
											Value: &commonpb.AnyValue_KvlistValue{
												KvlistValue: &commonpb.KeyValueList{
													Values: []*commonpb.KeyValue{
														{Key: "method", Value: strVal("GET")},
														{Key: "status", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_IntValue{IntValue: 200}}},
													},
												},
											},
										},
									},
									{
										Key: "tags",
										Value: &commonpb.AnyValue{
											Value: &commonpb.AnyValue_ArrayValue{
												ArrayValue: &commonpb.ArrayValue{
													Values: []*commonpb.AnyValue{strVal("a"), strVal("b")},
												},
											},
										},
									},
									{Key: "payload", Value: strVal(`{"nested":{"key":"value"},"count":3}`)},
								},
							},
						},
					},
				},
			},
		},
	}

	records := FlattenLogs(req)
	if len(records) != 1 {
		t.Fatalf("expected 1 record, got %d", len(records))
	}
	attrs := records[0].LogAttributes

	httpReq, ok := attrs["http.request"].(map[string]any)
	if !ok {
		t.Fatalf("LogAttributes[http.request] = %T, want map[string]any", attrs["http.request"])
	}
	if httpReq["method"] != "GET" {
		t.Errorf("http.request.method = %v, want GET", httpReq["method"])
	}
	if httpReq["status"] != int64(200) {
		t.Errorf("http.request.status = %v, want 200", httpReq["status"])
	}

	tags, ok := attrs["tags"].([]any)
	if !ok {
		t.Fatalf("LogAttributes[tags] = %T, want []any", attrs["tags"])
	}
	if len(tags) != 2 || tags[0] != "a" || tags[1] != "b" {
		t.Errorf("tags = %v, want [a b]", tags)
	}

	payload, ok := attrs["payload"].(map[string]any)
	if !ok {
		t.Fatalf("LogAttributes[payload] = %T, want map[string]any (parsed JSON), got raw string", attrs["payload"])
	}
	nested, ok := payload["nested"].(map[string]any)
	if !ok || nested["key"] != "value" {
		t.Errorf("payload.nested = %v, want map[key:value]", payload["nested"])
	}
	if payload["count"] != float64(3) {
		t.Errorf("payload.count = %v, want 3", payload["count"])
	}
}

func TestAnyValueToString(t *testing.T) {
	tests := []struct {
		name string
		in   *commonpb.AnyValue
		want string
	}{
		{"string", strVal("hi"), "hi"},
		{"bool", &commonpb.AnyValue{Value: &commonpb.AnyValue_BoolValue{BoolValue: true}}, "true"},
		{"int", &commonpb.AnyValue{Value: &commonpb.AnyValue_IntValue{IntValue: 42}}, "42"},
		{"double", &commonpb.AnyValue{Value: &commonpb.AnyValue_DoubleValue{DoubleValue: 3.5}}, "3.5"},
		{"bytes", &commonpb.AnyValue{Value: &commonpb.AnyValue_BytesValue{BytesValue: []byte{0xde, 0xad}}}, "dead"},
		{"nil", nil, ""},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := anyValueToString(tt.in); got != tt.want {
				t.Errorf("anyValueToString(%v) = %q, want %q", tt.in, got, tt.want)
			}
		})
	}
}

func TestParseIfJSON(t *testing.T) {
	if got := parseIfJSON("plain text"); got != "plain text" {
		t.Errorf("parseIfJSON(plain text) = %v, want unchanged string", got)
	}
	if got := parseIfJSON("{not valid json"); got != "{not valid json" {
		t.Errorf("parseIfJSON(invalid) = %v, want unchanged string", got)
	}
	if got, ok := parseIfJSON(`{"a":1}`).(map[string]any); !ok || got["a"] != float64(1) {
		t.Errorf("parseIfJSON(object) = %v, want parsed map", got)
	}
	if got, ok := parseIfJSON(`[1,2]`).([]any); !ok || len(got) != 2 {
		t.Errorf("parseIfJSON(array) = %v, want parsed slice", got)
	}
}

func TestFlattenLogsEmptyRequest(t *testing.T) {
	records := FlattenLogs(&collogspb.ExportLogsServiceRequest{})
	if len(records) != 0 {
		t.Errorf("expected no records, got %d", len(records))
	}
}
