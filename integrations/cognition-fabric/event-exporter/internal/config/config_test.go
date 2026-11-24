package config

import (
	"os"
	"path/filepath"
	"testing"
)

func writeTempConfig(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("failed to write temp config: %v", err)
	}
	return path
}

func TestLoad(t *testing.T) {
	path := writeTempConfig(t, `
rules:
  - rule: 'type == "tool.completed"'
    callback_url: "http://localhost:9001/hook"
  - rule: 'data.protocol == "mcp"'
    callback_url: "http://localhost:9002/hook"
`)

	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("Load returned error: %v", err)
	}
	if len(cfg.Rules) != 2 {
		t.Fatalf("expected 2 rules, got %d", len(cfg.Rules))
	}
	if cfg.Rules[0].Rule != `type == "tool.completed"` || cfg.Rules[0].CallbackURL != "http://localhost:9001/hook" {
		t.Errorf("unexpected first rule: %+v", cfg.Rules[0])
	}
	if cfg.Rules[1].Rule != `data.protocol == "mcp"` {
		t.Errorf("unexpected second rule: %+v", cfg.Rules[1])
	}
}

func TestLoadMissingFile(t *testing.T) {
	if _, err := Load(filepath.Join(t.TempDir(), "does-not-exist.yaml")); err == nil {
		t.Fatal("expected an error for a missing config file")
	}
}

func TestLoadInvalidYAML(t *testing.T) {
	path := writeTempConfig(t, "rules: [this is not valid yaml")
	if _, err := Load(path); err == nil {
		t.Fatal("expected an error for malformed YAML")
	}
}
