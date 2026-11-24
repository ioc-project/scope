# event-exporter

A minimal OTLP/gRPC logs endpoint. It accepts logs exported by an OpenTelemetry
Collector, persists normalized client events in ClickHouse, prints them, and
evaluates each normalized event against a configured set of notification rules. A rule match triggers
a POST of the normalized event to that rule's `callback_url`; records that cannot
be normalized produce a diagnostic instead of an event payload.

## Running it

```sh
go run ./cmd/event-exporter
```

It starts a gRPC server implementing the OTLP `LogsService`
(`opentelemetry.proto.collector.logs.v1.LogsService`) — this is exactly the
service a Collector's `otlp` exporter talks to, so event-exporter is a valid
target for one, not a Collector itself.

Configuration, in order of precedence (flag, then env var, then default):

| Setting            | Flag       | Env var       | Default        |
|---------------------|------------|---------------|----------------|
| gRPC listen port    | `-port`    | `GRPC_PORT`   | `4317`         |
| Rules config path   | `-config`  | `CONFIG_FILE` | `config.yaml`  |
| Environment file    | `-env-file` | None         | Optional `.env` in the working directory |

A `.env` file in the working directory is loaded automatically if present
(see `.env.template`). The repository-root `.env` is not loaded implicitly. To use
its ClickHouse credentials when running from `event-exporter/`:

```sh
go run ./cmd/event-exporter -port 4417 -env-file ../.env
```

Existing shell environment variables take precedence over file values. An explicit
missing or malformed environment file fails startup without printing its contents.
HTTP 401 from ClickHouse means authentication failed; check `CLICKHOUSE_USERNAME`
and `CLICKHOUSE_PASSWORD` in the selected file and any overriding shell variables.

ClickHouse must be reachable before the exporter starts. Startup creates
`normalized_events` in the configured, existing database with `CREATE TABLE IF NOT EXISTS`;
it does not create the database or migrate an existing table. The database user
needs `CREATE TABLE` and `INSERT` permissions. Startup fails if initialization fails.

| Setting | Environment variable | Default |
|---------|----------------------|---------|
| HTTP(S) endpoint override | `CLICKHOUSE_ENDPOINT` | Constructed from host and port |
| Host | `CLICKHOUSE_HOST` | `localhost` |
| HTTP port (not native port 9000) | `CLICKHOUSE_PORT` | `8123` |
| Database | `CLICKHOUSE_DATABASE` | `default` |
| Username | `CLICKHOUSE_USERNAME` | `default` |
| Password | `CLICKHOUSE_PASSWORD` | Empty |
| Total database write budget per OTLP request | `CLICKHOUSE_TIMEOUT` | `10s` |

Use HTTPS for remote deployments; endpoints must not embed credentials or query
parameters. Compose uses the same database settings as the analytics API, with
`claris` as its default database.

## Normalized Event Storage

Every valid event is stored, including events with no matching subscription.
The embedded DDL is in [`internal/storage/schema.sql`](internal/storage/schema.sql).
It uses `ReplacingMergeTree`, monthly event-time partitions, and no automatic TTL.

- `event_id`: the supplied log attribute `event.id`, stored as `FixedString(64)`.
  It must be a lowercase hexadecimal SHA-256 digest. Invalid supplied IDs reject
  the record; absent IDs fall back to SHA-256 of the flattened source record.
- `event_time`, `ingested_at`: UTC `DateTime64(9)` source and insertion timestamps.
- `tenant_id`, `application_id`, `session_id`, `trace_id`, `span_id`, `call_id`,
  `message_id`, `fork_id`: `String` columns, empty when optional values are absent.
- `session_sequence`: `UInt64` snapshot version, not a unique event offset.
- `event_type`, `service_name`, `schema_version`, `normalizer_version`:
  `LowCardinality(String)` columns.
- `payload`: `String` containing the normalized JSON delivered to webhooks.

App and tenant IDs are read from log attributes first, then resource attributes:
`application_id` (preferred), `app_id`, or `application.id`, and `tenant_id` or `tenant.id`. They are storage
metadata, not additions to the realtime event schema. Incoming attributes are not
an authorization mechanism. The analytics API's
[realtime pull endpoints](../ioc-analytics-api/api/README.md#realtime-event-pull-api)
accept optional tenant/MAS scope filters; omitting them broadens the query.
A trusted gateway must authenticate callers and authorize the requested scope,
including queries with omitted scope filters, before allowing access.

The sort key is `(tenant_id, application_id, session_id, event_time, event_id)`, optimized
for scoped session history. Trace/span bloom-filter indexes support additional
lookups; constrain time ranges where possible. Supplied IDs are preserved without
recomputation, including across retries, enrichment, or different OTLP batches.
The Observe SDK hashes finalized log attributes (excluding `event.id`) immediately
before emission, using UTF-8 JSON with sorted keys, compact separators, ASCII
escaping, preserved scalar types, and no non-finite numbers. Resource, scope, and
transport-envelope fields are excluded. A2A/MCP receive events have distinct IDs
and may reference the send event via `event.parent.id`, preserved as
`correlation.parent_event_id` in normalized payloads. Absent parent IDs are omitted.
For older producers, identical flattened source records produce the same ID.
Two genuinely distinct records with identical source content are indistinguishable
and collapse; producers should include a unique event attribute if that matters.

Merges remove duplicate sort keys eventually, not immediately. Pull queries must
use `FINAL` (or equivalent explicit deduplication), for example:

```sql
SELECT payload
FROM normalized_events FINAL
WHERE tenant_id = {tenant_id:String}
  AND application_id = {application_id:String}
  AND session_id = {session_id:String}
ORDER BY event_time, event_id
LIMIT 100;
```

Existing tables using `app_id` require an explicit migration to the new
`application_id` schema before deploying this exporter. Startup uses
`CREATE TABLE IF NOT EXISTS` and does not migrate existing tables. Preserve
existing application IDs when migrating, including in the sort key.

Schema and normalizer versions are stored with each row. Bump `NormalizerVersion`
when conversion behavior changes; plan explicit migrations/backfills rather than
assuming source IDs alone deduplicate events across conversion changes that alter
sort-key fields. Do not use event time or snapshot version alone as a polling cursor:
late arrivals and equal timestamps require a separately defined pull consistency policy.

### Persistence And Retry Policy

- Each OTLP request is written synchronously in chunks of at most 1,000 events,
  within one database timeout budget. There is no in-process background write queue.
- ClickHouse async inserts are disabled and responses wait for query completion.
  An OTLP success means all valid records received database acknowledgement, not
  that replicas/quorum or disk-fsync guarantees have been configured.
- Network errors, database timeouts, HTTP 408/429, and HTTP 5xx return gRPC
  `Unavailable`. The Collector owns retry backoff; the exporter does not multiply
  those attempts with another retry loop. Caller cancellation preserves its gRPC status.
- Other HTTP errors return `FailedPrecondition` and require operator action.
  These are not retryable by OTLP clients, so monitor them: affected batches can be dropped.
- Writes are at-least-once. A timeout may follow a committed insert, or a later
  chunk may fail after earlier chunks succeeded. Retrying the request is safe for
  logical reads only when readers explicitly deduplicate as described above.
- Invalid/unsupported records are skipped and counted in OTLP `partial_success`;
  valid records in the same request are persisted. Clients must not retry partial
  successes. A database failure instead fails the request so valid records can retry.
- Webhook evaluation runs concurrently with persistence and does not wait for it.
  Webhooks may arrive before persistence succeeds, including during a database
  outage. Ingestion retries can repeat callbacks. Callback failures are logged and
  never cause database/ingestion retries; callbacks still have no dedicated retry queue.
- The handler waits for both the database result and the existing bounded-timeout
  callback loop before returning. Set the Collector request timeout accordingly.

Configure a Collector sending queue and exponential retries. A persistent queue
is recommended to survive Collector restarts; mount its directory on durable storage:

```yaml
extensions:
  file_storage/event-exporter:
    directory: /var/lib/otelcol/event-exporter
exporters:
  otlp/event-exporter:
    endpoint: event-exporter:4417
    tls:
      insecure: true
    timeout: 30s
    sending_queue:
      enabled: true
      storage: file_storage/event-exporter
      num_consumers: 4
      queue_size: 1000
    retry_on_failure:
      enabled: true
      initial_interval: 1s
      max_interval: 30s
      max_elapsed_time: 0s
service:
  extensions: [file_storage/event-exporter]
```

Merge this with the Collector's existing pipeline configuration. Queue capacity
is finite even with unlimited retry duration; monitor queue saturation, failed
exports, exporter persistence/rejection logs, and ClickHouse disk usage. Outages
beyond available buffering can still lose events.

Run unit tests with `go test ./...`. To additionally validate DDL, real inserts,
payload round-trip, ingestion timestamps, and retry deduplication:

```sh
CLICKHOUSE_TEST_ENDPOINT=http://localhost:8123 \
CLICKHOUSE_TEST_ENV_FILE=../.env go test ./internal/storage -run TestClickHouseIntegration -v
```

The optional environment file supplies test credentials without printing them.
The integration test creates and drops only its uniquely named temporary database;
the test user needs database creation/deletion and read permissions as well.

To inspect a log that cannot be normalized (for example, an LLM event missing
`model`), start the exporter with `DEBUG_RAW_EVENTS=1 go run ./cmd/event-exporter`.
On normalization errors it prints `===== RAW OTLP LOG =====` with the full
flattened incoming record, including its `LogAttributes`, alongside the error.
This is off by default; use it only in a safe environment because log bodies and
attributes may contain sensitive data. Valid events still print only the
normalized `===== EVENT =====` payload.

## Configuring the OTel Collector to reach it

In the Collector's own config, add an `otlp` **exporter** pointing at
event-exporter's host and gRPC port, and wire it into a `logs` pipeline:

```yaml
exporters:
  otlp/event-exporter:
    endpoint: localhost:4317   # host:port event-exporter is listening on, or host.docker.internal:4317 for Docker on macOS/Windows
    tls:
      insecure: true            # event-exporter serves plain gRPC, no TLS

service:
  pipelines:
    logs:
      receivers: [otlp]          # whatever already feeds this pipeline
      processors: []
      exporters: [otlp/event-exporter]
```

Notes:
- Use the `otlp` exporter (gRPC), not `otlphttp` — event-exporter only
  registers the gRPC logs service.
- `endpoint` must be reachable from wherever the Collector runs: `localhost:4317`
  for both running on the same host, or the container/service name and
  port (e.g. `event-exporter:4417` for this repo's Compose stack) when the
  Collector runs in the same Docker network / Kubernetes cluster. Compose
  sets `GRPC_PORT=4417`; standalone runs still default to `4317`.
- `tls.insecure: true` is required since event-exporter doesn't terminate TLS.
- Gzip-compressed OTLP/gRPC exports are supported. If running an older
  event-exporter build that rejects `grpc-encoding "gzip"`, set
  `compression: none` under the Collector's `otlp/event-exporter` exporter
  until the updated server is deployed.

## Notification rules (`config.yaml`)

```yaml
rules:
  - rule: 'type == "llm.completed"'
    callback_url: "http://localhost:9100/callback"
```

- `rule` is a boolean filter expression (evaluated with
  [expr-lang/expr](https://github.com/expr-lang/expr)) checked against the
  normalized event's JSON fields. Use paths such as `type`, `source.service_name`,
  `data.protocol`, and `data.edge.kind`, not incoming OTLP attribute names.
- Supported operators: `==`, `!=`, `&&`, `||`, `startsWith(...)`.
- Not every event carries every field; event-exporter makes every
  member access nil-safe automatically (equivalent to expr-lang's `?.`
  optional chaining), so a rule referencing a missing field evaluates to
  `false` rather than erroring.
- Every rule is checked against every successfully normalized event; on a
  match, event-exporter POSTs a normalized [realtime client event](https://cisco-eti.atlassian.net/wiki/spaces/IST/pages/3055976462/Realtime+Client+Event+Schema+Overview+and+Structure)
  (`type`, `time`, `session`, `source`, `correlation`, `data`) to that rule's
  `callback_url`.
- Lifecycle normalization requires an event name (OTLP `EventName`, `event.name`, or `type`), `session.id`,
  positive `snapshot.version`, a service name, a valid event time (from
  `event.time` or the OTLP log timestamp), and each event family's required
  fields. Unsupported or incomplete events are logged and not POSTed; OTLP
  ingestion still succeeds. No raw log attributes are forwarded as a fallback.
- Supported types are session/node lifecycle, topology edge updates, LLM/tool
  lifecycle, A2A/SLIM/MCP message send/receive events, and
  `a2a.message.completed` / `mcp.message.completed`. JSON-encoded
  input/output attributes are decoded into `data`; absent optional fields are
  omitted. Callback failures are logged, with no retry or replay.
- Message events map `message.input` and `message.output` to decoded
  `data.input` and `data.output`, and `operation.name` to `data.operation`.
  Legacy operation aliases remain supported. When content capture is disabled
  and payload attributes are absent, input/output remain omitted; existing
  trace/span, message, fork, and sequence metadata is retained.
- Definition observations `a2a.agent_card`, `mcp.tools.discovered`, and
  `mcp.tools.published` arrive as logs under `ioa_observe.runtime_events`.
  They accept `snapshot.version=0` without a session or trace/span context.
  Sessionless observations retain an empty session ID and sequence zero;
  no correlation identifiers are invented and no lifecycle counters are updated.
  `event.time` supplies the event time when present. `data.capability` contains
  decoded JSON `content`, `source`, optional `provider.id`, `capture.state`,
  and optional `content_size` (from `capability.content.size`, in bytes).
  Missing content does not discard an observation: capture states `complete`,
  `redacted`, `disabled`, `truncated`, and `unavailable` are preserved.
- LLM events require both a call ID and a model. The exporter accepts
  `llm.call.id` (or a valid span ID) for the call ID, and
  `gen_ai.request.model`, `gen_ai.response.model`, `llm.model`, or `data.model`
  for the model. For Observe `chat` events without an explicit model attribute,
  `llm.name` supplies the model. For other operations, `llm.name` can be a task
  label rather than a model, so the producer must provide an explicit model.

Producer `event.id` is included as optional top-level `event_id` in normalized
events. For `topology.edge.updated`, `data.edge` also preserves
`network.protocol.name` as `protocol`, `operation.name` as `operation`, and
`sequence` as the edge sequence (separate from `session.sequence`, which remains
the snapshot version). `topology.edge.evidence` and `topology.edge.confidence`
map to `evidence` and numeric `confidence` between zero and one. Valid
`source.agent.trace.id` and `source.agent.span.id` map to
`source_agent_trace_id` and `source_agent_span_id` without replacing the event's
own correlation context. Missing or invalid optional confidence/context values
are omitted.

The client contract is checked in as
[`internal/otlp/realtime-client-event.schema.json`](internal/otlp/realtime-client-event.schema.json).
To regenerate the Go models in `internal/otlp/client_event_gen.go` after changing
the schema, run `go generate ./internal/otlp` from this directory. The generator
version is pinned in `client_event.go`. The converter uses the generated
`Envelope` and nested types; event-specific `data` is mapped from OTLP attributes
according to the schema's event families. Generation does not replace the
converter's required-field checks.

## Webhook receiver (dev client)

`cmd/webhook-receiver` is a throwaway local client for exercising
`callback_url`s without standing up a real consumer: it exposes a single
endpoint and prints the raw body of every request it receives.

```sh
go run ./cmd/webhook-receiver
```

| Setting      | Flag    | Env var        | Default              |
|--------------|---------|----------------|-----------------------|
| Listen port  | `-port` | `WEBHOOK_PORT` | `9100`                |
| Endpoint     | —       | —              | `POST /callback`      |

The shipped `config.yaml` points every rule's `callback_url` at
`http://localhost:9100/callback`.

## Docker

```sh
docker build -t event-exporter .
docker run -p 4317:4317 \
  --env-file .env \
  -v "$(pwd)/config.yaml:/config.yaml" \
  event-exporter
```

The image only bakes in the Go binary — mount your rules file and, if it's
not at `/config.yaml`, set `CONFIG_FILE` to its path in the container.
