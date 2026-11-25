// Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
// SPDX-License-Identifier: Apache-2.0

// Command webhook-receiver is a minimal HTTP server for locally exercising
// the notification rules in config.yaml: it exposes a single callback
// endpoint, prints the body of every request it receives, and responds
// 200 OK.
package main

import (
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
)

const (
	defaultPort  = 9100
	callbackPath = "/callback"
)

func main() {
	var portFlag int
	flag.IntVar(&portFlag, "port", 0, "port to listen on (overrides WEBHOOK_PORT env var)")
	flag.Parse()

	port, err := resolvePort(portFlag, os.Getenv("WEBHOOK_PORT"))
	if err != nil {
		log.Fatalf("invalid port: %v", err)
	}

	http.HandleFunc(callbackPath, handleCallback)

	log.Printf("webhook receiver listening on port %d, path %s", port, callbackPath)
	if err := http.ListenAndServe(fmt.Sprintf(":%d", port), nil); err != nil {
		log.Fatalf("server stopped: %v", err)
	}
}

func handleCallback(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, "failed to read body", http.StatusBadRequest)
		return
	}

	fmt.Println(string(body))
	w.WriteHeader(http.StatusOK)
}

// resolvePort applies the precedence: explicit -port flag, then WEBHOOK_PORT
// env var, then a default port.
func resolvePort(flagPort int, envPort string) (int, error) {
	if flagPort != 0 {
		return flagPort, nil
	}
	if envPort != "" {
		p, err := strconv.Atoi(envPort)
		if err != nil {
			return 0, fmt.Errorf("WEBHOOK_PORT=%q is not a valid port number: %w", envPort, err)
		}
		return p, nil
	}
	return defaultPort, nil
}
