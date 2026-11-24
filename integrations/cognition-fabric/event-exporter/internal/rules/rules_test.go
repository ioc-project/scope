// Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
// SPDX-License-Identifier: Apache-2.0

package rules

import (
	"slices"
	"testing"

	"oxp-event-exporter/internal/config"
)

func TestRuleMatches(t *testing.T) {
	attrs := map[string]any{
		"type": "tool.completed",
		"data": map[string]any{
			"protocol": "mcp",
			"edge": map[string]any{
				"kind":         "agent_handoff",
				"source_agent": "researcher",
				"target_agent": "writer",
			},
			"agent": map[string]any{
				"name": "researcher",
			},
		},
		"source": map[string]any{
			"service_name": "weather-agent",
		},
	}

	tests := []struct {
		name string
		rule string
		want bool
	}{
		{"all tool events", `type startsWith "tool."`, true},
		{"completed tool calls", `type == "tool.completed"`, true},
		{"all llm events", `type startsWith "llm."`, false},
		{"mcp traffic", `data.protocol == "mcp"`, true},
		{"agent handoffs excluding self-loops", `data.edge.kind == "agent_handoff" && data.edge.source_agent != data.edge.target_agent`, true},
		{"events from one service", `source.service_name == "weather-agent"`, true},
		{"events from another service", `source.service_name == "billing-agent"`, false},
		{"events for one agent", `data.agent.name == "researcher"`, true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			compiled, err := Compile([]config.Rule{{Rule: tt.rule}})
			if err != nil {
				t.Fatalf("Compile returned error: %v", err)
			}
			got, err := compiled[0].Matches(attrs)
			if err != nil {
				t.Fatalf("Matches returned error: %v", err)
			}
			if got != tt.want {
				t.Errorf("rule %q against %v = %v, want %v", tt.rule, attrs, got, tt.want)
			}
		})
	}
}

func TestRuleMatchesMissingNestedField(t *testing.T) {
	// A record that carries none of the attributes these rules dot into.
	// Config rules use plain "." (not expr-lang's "?." optional chaining),
	// so Compile must make every member access nil-safe itself: these must
	// resolve to false rather than error, since not every log record
	// carries every attribute.
	tests := []struct {
		name string
		rule string
	}{
		{"missing data entirely", `data.protocol == "mcp"`},
		{"missing intermediate edge object", `data.edge.kind == "agent_handoff"`},
		{"missing source entirely", `source.service_name == "weather-agent"`},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			compiled, err := Compile([]config.Rule{{Rule: tt.rule}})
			if err != nil {
				t.Fatalf("Compile returned error: %v", err)
			}
			got, err := compiled[0].Matches(map[string]any{})
			if err != nil {
				t.Fatalf("Matches returned error for a missing field, want a clean false: %v", err)
			}
			if got {
				t.Error("expected no match when a referenced nested field is entirely absent")
			}
		})
	}
}

func TestCompileInvalidRule(t *testing.T) {
	if _, err := Compile([]config.Rule{{Rule: "type ==="}}); err == nil {
		t.Fatal("expected a compile error for an invalid rule expression")
	}
}

func TestMatchesNonBooleanExpression(t *testing.T) {
	compiled, err := Compile([]config.Rule{{Rule: `type`}})
	if err != nil {
		t.Fatalf("Compile returned error: %v", err)
	}
	if _, err := compiled[0].Matches(map[string]any{"type": "tool.completed"}); err == nil {
		t.Fatal("expected an error when the expression does not evaluate to a boolean")
	}
}

func TestMatchesMissingField(t *testing.T) {
	compiled, err := Compile([]config.Rule{{Rule: `type == "tool.completed"`}})
	if err != nil {
		t.Fatalf("Compile returned error: %v", err)
	}
	got, err := compiled[0].Matches(map[string]any{})
	if err != nil {
		t.Fatalf("Matches returned error: %v", err)
	}
	if got {
		t.Error("expected no match when the referenced field is absent")
	}
}

func TestRuleMatchesFlatAndNestedAttributes(t *testing.T) {
	compiled, err := Compile([]config.Rule{{Rule: `event.name == "llm.completed"`}})
	if err != nil {
		t.Fatal(err)
	}
	for _, tt := range []struct {
		name  string
		attrs map[string]any
		want  bool
	}{
		{"flat", map[string]any{"event.name": "llm.completed"}, true},
		{"nested", map[string]any{"event": map[string]any{"name": "llm.completed"}}, true},
		{"nested wins conflict", map[string]any{"event": map[string]any{"name": "llm.started"}, "event.name": "llm.completed"}, false},
		{"missing", map[string]any{"agent.name": "code"}, false},
	} {
		t.Run(tt.name, func(t *testing.T) {
			got, err := compiled[0].Matches(tt.attrs)
			if err != nil {
				t.Fatal(err)
			}
			if got != tt.want {
				t.Errorf("Matches(%v) = %v, want %v", tt.attrs, got, tt.want)
			}
		})
	}

	attrs := map[string]any{"event": map[string]any{"source": "sdk"}, "event.name": "llm.completed"}
	if matched, err := compiled[0].Matches(attrs); err != nil || !matched {
		t.Fatalf("partially nested attrs: matched = %v, err = %v", matched, err)
	}
	if _, exists := attrs["event"].(map[string]any)["name"]; exists {
		t.Error("Matches mutated the caller's nested attributes")
	}
}

func TestConfiguredRulesMatchRuntimeEvents(t *testing.T) {
	cfg, err := config.Load("../../config.yaml")
	if err != nil {
		t.Fatal(err)
	}
	compiled, err := Compile(cfg.Rules)
	if err != nil {
		t.Fatal(err)
	}

	tests := []struct {
		name      string
		attrs     map[string]any
		wantRules []int
	}{
		{
			name:      "tool completed",
			attrs:     map[string]any{"type": "tool.completed", "data": map[string]any{"agent": map[string]any{"name": "code"}}},
			wantRules: []int{1, 2},
		},
		{
			name:      "llm completed",
			attrs:     map[string]any{"type": "llm.completed"},
			wantRules: []int{3, 4},
		},
		{
			name:      "mcp message",
			attrs:     map[string]any{"type": "mcp.message.sent", "data": map[string]any{"protocol": "mcp"}},
			wantRules: []int{5},
		},
		{
			name: "agent handoff",
			attrs: map[string]any{
				"type": "topology.edge.updated",
				"data": map[string]any{"edge": map[string]any{
					"kind": "agent_handoff", "source_agent": "code", "target_agent": "supervisor",
				}},
			},
			wantRules: []int{6},
		},
		{
			name:      "service and agent",
			attrs:     map[string]any{"type": "tool.completed", "source": map[string]any{"service_name": "weather-agent"}, "data": map[string]any{"agent": map[string]any{"name": "researcher"}}},
			wantRules: []int{1, 2, 7, 8},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var matchedRules []int
			for _, rule := range compiled {
				matched, err := rule.Matches(tt.attrs)
				if err != nil {
					t.Fatalf("rule %d: %v", rule.Number, err)
				}
				if matched {
					matchedRules = append(matchedRules, rule.Number)
				}
			}
			if !slices.Equal(matchedRules, tt.wantRules) {
				t.Errorf("matched rules = %v, want %v", matchedRules, tt.wantRules)
			}
		})
	}
}
