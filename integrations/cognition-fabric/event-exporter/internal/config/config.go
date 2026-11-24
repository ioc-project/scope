// Package config loads the notification rules file: a list of
// (filter expression, callback URL) entries.
package config

import (
	"fmt"
	"os"

	"gopkg.in/yaml.v3"
)

// Rule is one configured notification rule, as read from the config file.
type Rule struct {
	Rule        string `yaml:"rule"`
	CallbackURL string `yaml:"callback_url"`
}

// Config is the top-level shape of the rules config file.
type Config struct {
	Rules []Rule `yaml:"rules"`
}

// Load reads and parses the rules config file at path.
func Load(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read config file %q: %w", path, err)
	}

	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parse config file %q: %w", path, err)
	}

	return &cfg, nil
}
