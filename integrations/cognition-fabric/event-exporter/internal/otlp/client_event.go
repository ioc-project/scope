// Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
// SPDX-License-Identifier: Apache-2.0

package otlp

//go:generate go run github.com/atombender/go-jsonschema@v0.24.1 --only-models --tags json --capitalization ID --package otlp --output client_event_gen.go realtime-client-event.schema.json

import (
	"fmt"
	"strconv"
	"strings"
	"time"
)

// ClientEvent uses the envelope type generated from the realtime client JSON Schema.
type ClientEvent = Envelope

// NormalizeEvent maps a flattened OTLP log to the realtime client event contract.
// Records missing a required field cannot be represented by that contract.
func NormalizeEvent(record LogRecord) (ClientEvent, error) {
	attrs := record.LogAttributes
	eventType := record.EventName
	if eventType == "" {
		eventType = stringAttr(attrs, "event.name", "type")
	}
	definition := eventType == "a2a.agent_card" || eventType == "mcp.tools.discovered" || eventType == "mcp.tools.published"
	sessionID := stringAttr(attrs, "session.id")
	sequence, ok := positiveInteger(attribute(attrs, "snapshot.version"))
	if definition {
		sequence, ok = nonnegativeInteger(attribute(attrs, "snapshot.version"))
	}
	serviceName := record.ServiceName
	if serviceName == "" {
		serviceName = stringAttr(record.ResourceAttributes, "service.name")
	}
	if eventType == "" || (!definition && sessionID == "") || !ok || serviceName == "" {
		return ClientEvent{}, fmt.Errorf("missing event name, session.id, snapshot.version, or service.name")
	}

	eventTime := record.Timestamp
	if rawTime := stringAttr(attrs, "event.time"); rawTime != "" {
		parsed, err := time.Parse(time.RFC3339Nano, rawTime)
		if err != nil {
			return ClientEvent{}, fmt.Errorf("invalid event.time: %w", err)
		}
		eventTime = parsed
	}
	if eventTime.IsZero() || eventTime.Equal(time.Unix(0, 0)) {
		return ClientEvent{}, fmt.Errorf("missing event.time and OTLP timestamp")
	}
	if int64(int(sequence)) != sequence {
		return ClientEvent{}, fmt.Errorf("snapshot.version exceeds the supported integer range")
	}

	event := ClientEvent{
		EventID:     stringPointer(attrs, "event.id"),
		Type:        eventType,
		Time:        eventTime,
		Session:     EnvelopeSession{ID: sessionID, Sequence: int(sequence)},
		Source:      EnvelopeSource{ServiceName: serviceName},
		Correlation: EnvelopeCorrelation{},
		Data:        EnvelopeData{},
	}
	data := map[string]any(event.Data)
	event.Source.ServiceVersion = stringPointer(record.ResourceAttributes, "service.version")
	if record.ScopeName != "" {
		event.Source.InstrumentationScope = &record.ScopeName
	}
	if record.ScopeVersion != "" {
		event.Source.InstrumentationScopeVersion = &record.ScopeVersion
	}
	if len(record.TraceId) == 32 && isLowerHex(record.TraceId) && record.TraceId != strings.Repeat("0", 32) {
		event.Correlation.TraceID = &record.TraceId
	}
	if len(record.SpanId) == 16 && isLowerHex(record.SpanId) && record.SpanId != strings.Repeat("0", 16) {
		event.Correlation.SpanID = &record.SpanId
	}
	if record.TraceFlags != 0 || event.Correlation.TraceID != nil {
		flags := int(record.TraceFlags)
		event.Correlation.TraceFlags = &flags
	}
	event.Correlation.MessageID = stringPointer(attrs, "message.id", "message_id")
	event.Correlation.ForkID = stringPointer(attrs, "fork.id", "fork_id")
	event.Correlation.ParentEventID = stringPointer(attrs, "event.parent.id")

	agentName := stringAttr(attrs, "agent.name", "data.agent.name")
	switch eventType {
	case "a2a.agent_card", "mcp.tools.discovered", "mcp.tools.published":
		capability := map[string]any{}
		optionalValue(capability, "content", attrs, "capability.content")
		optionalString(capability, "source", attrs, "capability.source")
		if providerID := stringAttr(attrs, "capability.provider.id"); providerID != "" {
			capability["provider"] = map[string]any{"id": providerID}
		}
		if state := stringAttr(attrs, "capability.capture.state"); state != "" {
			capability["capture"] = map[string]any{"state": state}
		}
		optionalNonnegativeInteger(capability, "content_size", attrs, "capability.content.size")
		data["capability"] = capability
	case "topology.session.started", "topology.session.completed":
	case "topology.node.started", "topology.node.completed":
		if agentName == "" {
			return ClientEvent{}, fmt.Errorf("%s requires agent.name", eventType)
		}
		data["agent"] = Agent{Name: agentName}
		optionalValue(data, "input", attrs, "agent.input", "data.input")
		optionalValue(data, "output", attrs, "agent.output", "data.output")
	case "topology.edge.updated":
		edge := map[string]any{}
		for field, keys := range map[string][]string{
			"id":           {"topology.edge.id", "edge.id", "data.edge.id"},
			"kind":         {"topology.edge.kind", "edge.kind", "data.edge.kind"},
			"status":       {"topology.edge.status", "edge.status", "data.edge.status"},
			"source_agent": {"source.agent", "source_agent", "data.edge.source_agent"},
			"target_agent": {"target.agent", "target_agent", "data.edge.target_agent"},
		} {
			optionalString(edge, field, attrs, keys...)
			if edge[field] == nil {
				return ClientEvent{}, fmt.Errorf("%s requires edge.%s", eventType, field)
			}
		}
		if edge["status"] != "sent" && edge["status"] != "received" && edge["status"] != "observed" {
			return ClientEvent{}, fmt.Errorf("invalid edge.status: %v", edge["status"])
		}
		optionalString(edge, "protocol", attrs, "network.protocol.name", "protocol", "data.edge.protocol")
		if protocol, exists := edge["protocol"]; exists && protocol != "agent_handoff" && protocol != "a2a" && protocol != "slim" && protocol != "mcp" {
			return ClientEvent{}, fmt.Errorf("invalid edge.protocol: %v", protocol)
		}
		optionalString(edge, "operation", attrs, "operation.name", "operation", "data.edge.operation")
		optionalString(edge, "message_id", attrs, "message.id", "data.edge.message_id")
		optionalString(edge, "fork_id", attrs, "fork.id", "data.edge.fork_id")
		optionalNonnegativeInteger(edge, "sequence", attrs, "edge.sequence", "sequence", "data.edge.sequence")
		optionalString(edge, "evidence", attrs, "topology.edge.evidence", "edge.evidence", "data.edge.evidence")
		if value := firstAttribute(attrs, "topology.edge.confidence", "edge.confidence", "data.edge.confidence"); value != nil {
			if confidence, err := strconv.ParseFloat(fmt.Sprint(value), 64); err == nil && confidence >= 0 && confidence <= 1 {
				edge["confidence"] = confidence
			}
		}
		if traceID := stringAttr(attrs, "source.agent.trace.id", "data.edge.source_agent_trace_id"); len(traceID) == 32 && isLowerHex(traceID) && traceID != strings.Repeat("0", 32) {
			edge["source_agent_trace_id"] = traceID
		}
		if spanID := stringAttr(attrs, "source.agent.span.id", "data.edge.source_agent_span_id"); len(spanID) == 16 && isLowerHex(spanID) && spanID != strings.Repeat("0", 16) {
			edge["source_agent_span_id"] = spanID
		}
		data["edge"] = edge
	case "llm.started", "llm.completed":
		callID := stringAttr(attrs, "llm.call.id", "gen_ai.call.id", "call.id", "data.call_id")
		if callID == "" && len(record.SpanId) == 16 && isLowerHex(record.SpanId) {
			callID = record.SpanId
		}
		model := stringAttr(attrs, "gen_ai.request.model", "gen_ai.response.model", "llm.model", "data.model")
		if model == "" && stringAttr(attrs, "gen_ai.operation.name", "operation.name", "llm.operation", "data.operation") == "chat" {
			model = stringAttr(attrs, "llm.name")
		}
		if callID == "" {
			return ClientEvent{}, fmt.Errorf("%s requires call_id", eventType)
		}
		if model == "" {
			return ClientEvent{}, fmt.Errorf("%s requires model", eventType)
		}
		event.Correlation.CallID = &callID
		data["call_id"] = callID
		data["model"] = model
		optionalString(data, "operation", attrs, "gen_ai.operation.name", "operation.name", "llm.operation", "data.operation")
		optionalString(data, "provider", attrs, "gen_ai.provider.name", "gen_ai.system", "data.provider")
		optionalString(data, "response_id", attrs, "gen_ai.response.id", "data.response_id")
		optionalValue(data, "input", attrs, "llm.input", "gen_ai.input.messages", "data.input")
		optionalValue(data, "output", attrs, "llm.output", "gen_ai.output.messages", "data.output")
		optionalAgent(data, agentName)
	case "tool.started", "tool.completed":
		toolName := stringAttr(attrs, "tool.name", "gen_ai.tool.name", "data.tool.name")
		if toolName == "" {
			return ClientEvent{}, fmt.Errorf("%s requires tool.name", eventType)
		}
		tool := map[string]any{"name": toolName}
		callID := stringAttr(attrs, "gen_ai.tool.call.id", "tool.call.id", "data.tool.call_id")
		if callID != "" {
			tool["call_id"] = callID
			event.Correlation.CallID = &callID
		}
		data["tool"] = tool
		optionalAgent(data, agentName)
		optionalValue(data, "input", attrs, "tool.input", "gen_ai.tool.call.arguments", "data.input")
		optionalValue(data, "output", attrs, "tool.output", "gen_ai.tool.call.result", "data.output")
	case "a2a.message.sent", "a2a.message.received", "a2a.message.completed", "slim.message.sent", "slim.message.received", "mcp.message.sent", "mcp.message.received", "mcp.message.completed":
		protocol := strings.SplitN(eventType, ".", 2)[0]
		sourceAgent := stringAttr(attrs, "source.agent", "source_agent", "data.source_agent")
		targetAgent := stringAttr(attrs, "target.agent", "target_agent", "data.target_agent")
		if sourceAgent == "" || targetAgent == "" {
			return ClientEvent{}, fmt.Errorf("%s requires source.agent and target.agent", eventType)
		}
		data["protocol"] = protocol
		data["source_agent"] = sourceAgent
		data["target_agent"] = targetAgent
		optionalString(data, "operation", attrs, "operation.name", "operation", "message.operation", "data.operation")
		optionalValue(data, "input", attrs, "message.input", "data.input")
		optionalValue(data, "output", attrs, "message.output", "data.output")
		optionalString(data, "message_id", attrs, "message.id", "data.message_id")
		optionalString(data, "fork_id", attrs, "fork.id", "data.fork_id")
		optionalNonnegativeInteger(data, "sequence", attrs, "message.sequence", "data.sequence")
	default:
		return ClientEvent{}, fmt.Errorf("unsupported event type %q", eventType)
	}
	return event, nil
}

func attribute(attrs map[string]any, key string) any {
	if value, exists := attrs[key]; exists {
		return value
	}
	parts := strings.Split(key, ".")
	var current any = attrs
	for _, part := range parts {
		object, ok := current.(map[string]any)
		if !ok {
			return nil
		}
		current = object[part]
	}
	return current
}

func firstAttribute(attrs map[string]any, keys ...string) any {
	for _, key := range keys {
		if value := attribute(attrs, key); value != nil {
			return value
		}
	}
	return nil
}

func stringAttr(attrs map[string]any, keys ...string) string {
	for _, key := range keys {
		if value, ok := attribute(attrs, key).(string); ok && value != "" {
			return value
		}
	}
	return ""
}

func stringPointer(attrs map[string]any, keys ...string) *string {
	if value := stringAttr(attrs, keys...); value != "" {
		return &value
	}
	return nil
}

func optionalString(target map[string]any, name string, attrs map[string]any, keys ...string) {
	if value := stringAttr(attrs, keys...); value != "" {
		target[name] = value
	}
}

func optionalValue(target map[string]any, name string, attrs map[string]any, keys ...string) {
	if value := firstAttribute(attrs, keys...); value != nil {
		if text, ok := value.(string); ok {
			value = parseIfJSON(text)
		}
		target[name] = value
	}
}

func optionalAgent(data map[string]any, name string) {
	if name != "" {
		data["agent"] = Agent{Name: name}
	}
}

func positiveInteger(value any) (int64, bool) {
	switch number := value.(type) {
	case int64:
		return number, number > 0
	case int:
		return int64(number), number > 0
	case float64:
		return int64(number), number >= 1 && number <= 9007199254740991 && number == float64(int64(number))
	case string:
		parsed, err := strconv.ParseInt(number, 10, 64)
		return parsed, err == nil && parsed > 0
	default:
		return 0, false
	}
}

func nonnegativeInteger(value any) (int64, bool) {
	if integer, ok := positiveInteger(value); ok {
		return integer, true
	}
	switch number := value.(type) {
	case int64:
		if number == 0 {
			return 0, true
		}
	case int:
		if number == 0 {
			return 0, true
		}
	case float64:
		if number == 0 {
			return 0, true
		}
	case string:
		if number == "0" {
			return 0, true
		}
	}
	return 0, false
}

func optionalNonnegativeInteger(target map[string]any, name string, attrs map[string]any, keys ...string) {
	if integer, ok := nonnegativeInteger(firstAttribute(attrs, keys...)); ok {
		target[name] = integer
	}
}

func isLowerHex(value string) bool {
	for _, char := range value {
		if (char < '0' || char > '9') && (char < 'a' || char > 'f') {
			return false
		}
	}
	return true
}
