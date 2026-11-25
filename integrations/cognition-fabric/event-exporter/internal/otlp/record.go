// Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
// SPDX-License-Identifier: Apache-2.0

// Package otlp flattens OTLP log payloads into a structured shape that
// mirrors the ClickHouse otel_logs table:
//
//	CREATE TABLE "otel_logs" (
//	  "Timestamp" DateTime64(9), "TraceId" String, "SpanId" String,
//	  "TraceFlags" UInt8, "SeverityText" LowCardinality(String),
//	  "SeverityNumber" UInt8, "ServiceName" LowCardinality(String), "Body" String,
//	  "ResourceSchemaUrl" LowCardinality(String), "ResourceAttributes" Map(...),
//	  "ScopeSchemaUrl" LowCardinality(String), "ScopeName" String,
//	  "ScopeVersion" LowCardinality(String), "ScopeAttributes" Map(...),
//	  "LogAttributes" Map(...)
//	)
//
// (TimestampTime is a ClickHouse-side default derived from Timestamp, so it
// has no counterpart here.)
package otlp

import (
	"encoding/hex"
	"encoding/json"
	"strconv"
	"strings"
	"time"

	collogspb "go.opentelemetry.io/proto/otlp/collector/logs/v1"
	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
)

// LogRecord is one flattened OTLP log record.
type LogRecord struct {
	Timestamp          time.Time      `json:"Timestamp"`
	TraceId            string         `json:"TraceId"`
	SpanId             string         `json:"SpanId"`
	TraceFlags         uint8          `json:"TraceFlags"`
	SeverityText       string         `json:"SeverityText"`
	SeverityNumber     uint8          `json:"SeverityNumber"`
	ServiceName        string         `json:"ServiceName"`
	Body               string         `json:"Body"`
	EventName          string         `json:"EventName,omitempty"`
	ResourceSchemaUrl  string         `json:"ResourceSchemaUrl"`
	ResourceAttributes map[string]any `json:"ResourceAttributes"`
	ScopeSchemaUrl     string         `json:"ScopeSchemaUrl"`
	ScopeName          string         `json:"ScopeName"`
	ScopeVersion       string         `json:"ScopeVersion"`
	ScopeAttributes    map[string]any `json:"ScopeAttributes"`
	LogAttributes      map[string]any `json:"LogAttributes"`
}

// FlattenLogs converts an OTLP ExportLogsServiceRequest into one LogRecord
// per log record it contains.
func FlattenLogs(req *collogspb.ExportLogsServiceRequest) []LogRecord {
	var records []LogRecord

	for _, rl := range req.GetResourceLogs() {
		resourceAttrs := keyValuesToMap(rl.GetResource().GetAttributes())
		serviceName, _ := resourceAttrs["service.name"].(string)

		for _, sl := range rl.GetScopeLogs() {
			scopeAttrs := keyValuesToMap(sl.GetScope().GetAttributes())

			for _, lr := range sl.GetLogRecords() {
				records = append(records, LogRecord{
					Timestamp:          time.Unix(0, int64(lr.GetTimeUnixNano())).UTC(),
					TraceId:            hex.EncodeToString(lr.GetTraceId()),
					SpanId:             hex.EncodeToString(lr.GetSpanId()),
					TraceFlags:         uint8(lr.GetFlags()),
					SeverityText:       lr.GetSeverityText(),
					SeverityNumber:     uint8(lr.GetSeverityNumber()),
					ServiceName:        serviceName,
					Body:               anyValueToString(lr.GetBody()),
					EventName:          lr.GetEventName(),
					ResourceSchemaUrl:  rl.GetSchemaUrl(),
					ResourceAttributes: resourceAttrs,
					ScopeSchemaUrl:     sl.GetSchemaUrl(),
					ScopeName:          sl.GetScope().GetName(),
					ScopeVersion:       sl.GetScope().GetVersion(),
					ScopeAttributes:    scopeAttrs,
					LogAttributes:      keyValuesToMap(lr.GetAttributes()),
				})
			}
		}
	}

	return records
}

func keyValuesToMap(attrs []*commonpb.KeyValue) map[string]any {
	m := make(map[string]any, len(attrs))
	for _, kv := range attrs {
		m[kv.GetKey()] = anyValueToAny(kv.GetValue())
	}
	return m
}

// anyValueToAny renders an OTLP AnyValue as a native Go value, preserving
// structure instead of flattening it to a string: nested key/value lists
// become map[string]any, arrays become []any, and a string that is itself
// JSON (object or array) is parsed rather than kept as an escaped blob.
func anyValueToAny(v *commonpb.AnyValue) any {
	switch val := v.GetValue().(type) {
	case *commonpb.AnyValue_StringValue:
		return parseIfJSON(val.StringValue)
	case *commonpb.AnyValue_BoolValue:
		return val.BoolValue
	case *commonpb.AnyValue_IntValue:
		return val.IntValue
	case *commonpb.AnyValue_DoubleValue:
		return val.DoubleValue
	case *commonpb.AnyValue_BytesValue:
		return hex.EncodeToString(val.BytesValue)
	case *commonpb.AnyValue_ArrayValue:
		items := val.ArrayValue.GetValues()
		arr := make([]any, len(items))
		for i, item := range items {
			arr[i] = anyValueToAny(item)
		}
		return arr
	case *commonpb.AnyValue_KvlistValue:
		return keyValuesToMap(val.KvlistValue.GetValues())
	default:
		return nil
	}
}

// parseIfJSON returns the parsed value when s looks like a JSON object or
// array, and s unchanged otherwise.
func parseIfJSON(s string) any {
	trimmed := strings.TrimSpace(s)
	if trimmed == "" || (trimmed[0] != '{' && trimmed[0] != '[') {
		return s
	}
	var parsed any
	if err := json.Unmarshal([]byte(trimmed), &parsed); err != nil {
		return s
	}
	return parsed
}

// anyValueToString renders an OTLP AnyValue as a plain string, used for the
// Body field, which is a String column regardless of the value's shape.
func anyValueToString(v *commonpb.AnyValue) string {
	switch val := v.GetValue().(type) {
	case *commonpb.AnyValue_StringValue:
		return val.StringValue
	case *commonpb.AnyValue_BoolValue:
		return strconv.FormatBool(val.BoolValue)
	case *commonpb.AnyValue_IntValue:
		return strconv.FormatInt(val.IntValue, 10)
	case *commonpb.AnyValue_DoubleValue:
		return strconv.FormatFloat(val.DoubleValue, 'f', -1, 64)
	case *commonpb.AnyValue_BytesValue:
		return hex.EncodeToString(val.BytesValue)
	case *commonpb.AnyValue_ArrayValue, *commonpb.AnyValue_KvlistValue:
		b, err := json.Marshal(anyValueToAny(v))
		if err != nil {
			return ""
		}
		return string(b)
	default:
		return ""
	}
}
