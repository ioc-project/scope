#!/bin/sh
# Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0
#
# Adds the analytics columns SCOPE expects on otel_traces once the collector has created it.
# Extracted from k8s/charts/scope/templates/clickhouse-init-job.yaml.
set -e

endpoint="tcp://${CLICKHOUSE_HOST}:${CLICKHOUSE_PORT}"
echo "Waiting for ClickHouse at ${endpoint}"
until clickhouse-client --host "${CLICKHOUSE_HOST}" --port "${CLICKHOUSE_PORT}" --user "${CLICKHOUSE_USER}" --password "${CLICKHOUSE_PASSWORD}" --query "SELECT 1" >/dev/null 2>&1; do
  sleep 2
done

# otel_traces is created lazily by the otel-collector's clickhouse exporter on
# its first connection, which can race with this post-install hook. Wait for it
# rather than failing on UNKNOWN_TABLE.
echo "Waiting for table ${CLICKHOUSE_DATABASE}.otel_traces to exist"
table_wait_elapsed=0
table_wait_timeout=120
until [ "$(clickhouse-client --host "${CLICKHOUSE_HOST}" --port "${CLICKHOUSE_PORT}" --user "${CLICKHOUSE_USER}" --password "${CLICKHOUSE_PASSWORD}" --query "EXISTS TABLE ${CLICKHOUSE_DATABASE}.otel_traces")" = "1" ]; do
  if [ "${table_wait_elapsed}" -ge "${table_wait_timeout}" ]; then
    echo "Timed out after ${table_wait_timeout}s waiting for ${CLICKHOUSE_DATABASE}.otel_traces to exist (has the otel-collector received any spans yet?)"
    exit 1
  fi
  sleep 2
  table_wait_elapsed=$((table_wait_elapsed + 2))
done

# Ensure analytics columns exist even when traces were created without explicit denormalized columns.
clickhouse-client \
  --host "${CLICKHOUSE_HOST}" \
  --port "${CLICKHOUSE_PORT}" \
  --user "${CLICKHOUSE_USER}" \
  --password "${CLICKHOUSE_PASSWORD}" \
  --query "ALTER TABLE ${CLICKHOUSE_DATABASE}.otel_traces ADD COLUMN IF NOT EXISTS application_id String DEFAULT SpanAttributes['application_id'];"

clickhouse-client \
  --host "${CLICKHOUSE_HOST}" \
  --port "${CLICKHOUSE_PORT}" \
  --user "${CLICKHOUSE_USER}" \
  --password "${CLICKHOUSE_PASSWORD}" \
  --query "ALTER TABLE ${CLICKHOUSE_DATABASE}.otel_traces ADD COLUMN IF NOT EXISTS session_id String DEFAULT multiIf(position(SpanAttributes['session.id'], '_') > 0, substring(SpanAttributes['session.id'], position(SpanAttributes['session.id'], '_') + 1), SpanAttributes['session.id']);"

clickhouse-client \
  --host "${CLICKHOUSE_HOST}" \
  --port "${CLICKHOUSE_PORT}" \
  --user "${CLICKHOUSE_USER}" \
  --password "${CLICKHOUSE_PASSWORD}" \
  --query "ALTER TABLE ${CLICKHOUSE_DATABASE}.otel_traces ADD COLUMN IF NOT EXISTS agent_id String DEFAULT SpanAttributes['agent_id'];"

# otel_traces is a plain (non-Replicated) MergeTree, so ClickHouse does not
# deduplicate inserts by default. The otel-collector's clickhouse exporter can
# retry an insert that actually landed (e.g. after a client-side timeout),
# producing byte-identical blocks; this setting makes ClickHouse drop those.
clickhouse-client \
  --host "${CLICKHOUSE_HOST}" \
  --port "${CLICKHOUSE_PORT}" \
  --user "${CLICKHOUSE_USER}" \
  --password "${CLICKHOUSE_PASSWORD}" \
  --query "ALTER TABLE ${CLICKHOUSE_DATABASE}.otel_traces MODIFY SETTING non_replicated_deduplication_window = 100;"

# The API reads otel_logs.EventName (live topology). Collectors older than the chart's 0.161.0 create
# otel_logs without it, and the exporter only creates a table that does not exist, so a ClickHouse volume
# left by an older stack needs the column added. A no-op when the collector already created it.
logs_wait_elapsed=0
until [ "$(clickhouse-client --host "${CLICKHOUSE_HOST}" --port "${CLICKHOUSE_PORT}" --user "${CLICKHOUSE_USER}" --password "${CLICKHOUSE_PASSWORD}" --query "EXISTS TABLE ${CLICKHOUSE_DATABASE}.otel_logs")" = "1" ]; do
  if [ "${logs_wait_elapsed}" -ge 60 ]; then
    echo "Timed out waiting for ${CLICKHOUSE_DATABASE}.otel_logs to exist"
    exit 1
  fi
  sleep 2
  logs_wait_elapsed=$((logs_wait_elapsed + 2))
done
clickhouse-client \
  --host "${CLICKHOUSE_HOST}" \
  --port "${CLICKHOUSE_PORT}" \
  --user "${CLICKHOUSE_USER}" \
  --password "${CLICKHOUSE_PASSWORD}" \
  --query "ALTER TABLE ${CLICKHOUSE_DATABASE}.otel_logs ADD COLUMN IF NOT EXISTS EventName String;"

echo "ClickHouse setup completed"
