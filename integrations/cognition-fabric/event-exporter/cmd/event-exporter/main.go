// Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
// SPDX-License-Identifier: Apache-2.0

// Command event-exporter is a minimal OTLP/gRPC logs endpoint: it accepts
// logs over gRPC, prints every request it receives, and evaluates each log
// record's attributes against a configured set of notification rules.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"os"
	"strconv"

	"github.com/joho/godotenv"
	"google.golang.org/grpc"

	"ioc-event-exporter/internal/config"
	"ioc-event-exporter/internal/rules"
	"ioc-event-exporter/internal/server"
	"ioc-event-exporter/internal/storage"
)

const (
	defaultGRPCPort   = 4317
	defaultConfigPath = "config.yaml"
)

func main() {
	var portFlag int
	var configFlag string
	var envFileFlag string
	flag.IntVar(&portFlag, "port", 0, "gRPC port to listen on (overrides GRPC_PORT env var)")
	flag.StringVar(&configFlag, "config", "", "path to the rules config file (overrides CONFIG_FILE env var)")
	flag.StringVar(&envFileFlag, "env-file", "", "environment file to load (defaults to optional .env in the working directory)")
	flag.Parse()

	if err := loadEnvironment(envFileFlag); err != nil {
		log.Fatal(err)
	}

	port, err := resolvePort(portFlag, os.Getenv("GRPC_PORT"))
	if err != nil {
		log.Fatalf("invalid port: %v", err)
	}

	configPath := resolveConfigPath(configFlag, os.Getenv("CONFIG_FILE"))
	cfg, err := config.Load(configPath)
	if err != nil {
		log.Fatalf("failed to load config: %v", err)
	}

	notificationRules, err := rules.Compile(cfg.Rules)
	if err != nil {
		log.Fatalf("failed to compile rules: %v", err)
	}

	databaseConfig, err := storage.ConfigFromEnv()
	if err != nil {
		log.Fatalf("invalid ClickHouse configuration: %v", err)
	}
	store, err := storage.NewClickHouse(databaseConfig)
	if err != nil {
		log.Fatalf("invalid ClickHouse configuration: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), databaseConfig.Timeout)
	err = store.EnsureTable(ctx)
	cancel()
	if err != nil {
		log.Fatalf("failed to initialize normalized_events table: %v", err)
	}

	lis, err := net.Listen("tcp", fmt.Sprintf(":%d", port))
	if err != nil {
		log.Fatalf("failed to listen on port %d: %v", port, err)
	}

	grpcServer := grpc.NewServer()
	server.Register(grpcServer, notificationRules, store)

	log.Printf("OTLP gRPC printer listening on port %d, with %d rules loaded from %s", port, len(notificationRules), configPath)
	if err := grpcServer.Serve(lis); err != nil {
		log.Fatalf("grpc server stopped: %v", err)
	}
}

func loadEnvironment(path string) error {
	explicit := path != ""
	if !explicit {
		path = ".env"
	}
	err := godotenv.Load(path)
	if err == nil || (!explicit && errors.Is(err, os.ErrNotExist)) {
		return nil
	}
	return fmt.Errorf("failed to load environment file %q; check the path, permissions, and dotenv syntax", path)
}

// resolvePort applies the precedence: explicit -port flag, then GRPC_PORT env
// var (which godotenv.Load has already populated from .env if present),
// then a default OTLP port.
func resolvePort(flagPort int, envPort string) (int, error) {
	if flagPort != 0 {
		return flagPort, nil
	}
	if envPort != "" {
		p, err := strconv.Atoi(envPort)
		if err != nil {
			return 0, fmt.Errorf("GRPC_PORT=%q is not a valid port number: %w", envPort, err)
		}
		return p, nil
	}
	return defaultGRPCPort, nil
}

// resolveConfigPath applies the precedence: explicit -config flag, then
// CONFIG_FILE env var, then a default path relative to the working directory.
func resolveConfigPath(flagPath, envPath string) string {
	if flagPath != "" {
		return flagPath
	}
	if envPath != "" {
		return envPath
	}
	return defaultConfigPath
}
