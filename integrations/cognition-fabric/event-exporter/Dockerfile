# Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

FROM golang:1.27-bookworm AS builder

WORKDIR /src

# Separate layer for caching: dependency resolution is independent of source code changes.
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/root/.cache/go-build \
    --mount=type=cache,target=/root/go/pkg/mod \
  go mod download

COPY cmd cmd
COPY internal internal

RUN --mount=type=cache,target=/root/.cache/go-build \
    --mount=type=cache,target=/root/go/pkg/mod \
  CGO_ENABLED=0 go build -o /out/event-exporter ./cmd/event-exporter

FROM gcr.io/distroless/static-debian12 AS runner

USER nonroot:nonroot

COPY --from=builder --chown=nonroot:nonroot /out/event-exporter /app/event-exporter

EXPOSE 4317

ENTRYPOINT ["/app/event-exporter"]
