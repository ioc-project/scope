package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestLoadEnvironment(t *testing.T) {
	t.Chdir(t.TempDir())
	const key = "EVENT_EXPORTER_ENV_TEST"
	t.Setenv(key, "")
	if err := os.Unsetenv(key); err != nil {
		t.Fatal(err)
	}
	if err := loadEnvironment(""); err != nil {
		t.Fatalf("missing default .env should be optional: %v", err)
	}
	path := filepath.Join(t.TempDir(), "shared.env")
	if err := os.WriteFile(path, []byte(key+"=from-file\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := loadEnvironment(path); err != nil || os.Getenv(key) != "from-file" {
		t.Fatalf("explicit environment file was not loaded: %v", err)
	}
	t.Setenv(key, "from-shell")
	if err := loadEnvironment(path); err != nil || os.Getenv(key) != "from-shell" {
		t.Fatalf("shell environment must take precedence: %v", err)
	}
	if err := loadEnvironment("missing.env"); err == nil {
		t.Fatal("missing explicit environment file must fail")
	}
	if err := os.WriteFile(path, []byte("!SECRET_INVALID_KEY=value\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := loadEnvironment(path); err == nil || strings.Contains(err.Error(), "SECRET_INVALID_KEY") {
		t.Fatalf("malformed files must fail without exposing their contents: %v", err)
	}
}

func TestResolvePort(t *testing.T) {
	tests := []struct {
		name     string
		flagPort int
		envPort  string
		want     int
		wantErr  bool
	}{
		{name: "flag takes precedence over env", flagPort: 9000, envPort: "4417", want: 9000},
		{name: "flag takes precedence over empty env", flagPort: 9000, envPort: "", want: 9000},
		{name: "falls back to env when flag unset", flagPort: 0, envPort: "4417", want: 4417},
		{name: "falls back to default when both unset", flagPort: 0, envPort: "", want: defaultGRPCPort},
		{name: "invalid env port is an error", flagPort: 0, envPort: "not-a-port", wantErr: true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := resolvePort(tt.flagPort, tt.envPort)
			if tt.wantErr {
				if err == nil {
					t.Fatalf("resolvePort(%d, %q) = %d, expected an error", tt.flagPort, tt.envPort, got)
				}
				return
			}
			if err != nil {
				t.Fatalf("resolvePort(%d, %q) returned unexpected error: %v", tt.flagPort, tt.envPort, err)
			}
			if got != tt.want {
				t.Errorf("resolvePort(%d, %q) = %d, want %d", tt.flagPort, tt.envPort, got, tt.want)
			}
		})
	}
}
