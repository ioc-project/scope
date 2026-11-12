# rt-norm-worker

Standalone worker (no `worker-base`) that normalizes OTel **spans** and **logs**, one record at a
time, into the Neo4j knowledge graph, and calls back the URL of every rule a log record matches.

Nothing is kept in process memory between records: each is normalized against the graph as
stored. The shared handlers are described in
[`backend/norm/docs/events.md`](../../norm/docs/events.md). Identity, lifecycle and ownership
reconciliation live in shared `norm`; the worker provides ingestion, persistence and callbacks.

All commands run from `backend/`.

## Reconciliation

- Child LLM/tool calls retain their owning agent span id. Ownership and capability-use edges
  are backfilled when the agent arrives, including when its span finishes after its children.
  Parent chains can cross transport/framework spans, including sessionless MCP server spans.
  Private trace context preserves these links across restarts and arbitrary arrival order.
- Lifecycle events create provisional calls. Completed spans enrich those same calls with
  authoritative timing, content and metrics; an event sharing the span id does not suppress
  the span. Late started events also correct event-only calls' input and start time.
- A propagated `<application>_<session>` identifies the originating MAS. A different
  participating service name (for example an MCP server) does not create another MAS.
- Graph metadata is retained on the MAS, but only graph nodes matched to observed agents are
  declared as Agents. Routers and tool-serving graph nodes are not assumed to be agents.
- Topology handoff hints are retained until both agent calls exist. They connect execution
  trajectories, not a second `executesAgent` target on the sender. MCP/tool edges do not
  manufacture recipient Agents.
- `llm.tools.presented` retains offered tools and links them to the owning agent when known.
  A2A incoming messages and completion replies are retained as message States; they do not
  replace an agent's own final State.

Compact reconciliation hints and processed event ids are persisted as `rtNormalization`
properties on the relevant graph nodes. No raw telemetry replay buffer is stored. A new
worker instance can continue reconciliation, and redelivered events/completed spans are
idempotent. Both Neo4j and the dry-run store accept earlier start-time corrections.

These guarantees also apply to direct users of `norm.StreamNormalizer`. Batch span
normalization uses the same identity, ownership, graph-role and trajectory reconciliation.
This is not a migration or cleanup of previously misclassified graph data. Use a fresh graph
for an end-to-end comparison; the worker does not delete unrelated existing MAS/Agent nodes.
Ontology/SHACL checks are diagnostic and are not bypassed or changed by this worker.

Neo4j stores private ancestry under `_NormalizationTrace`, one record per trace (or explicit
session when no trace id is available). These records have no ontology relationships and
are not MAS, Agent or execution nodes. Exclude them from KG exports and visualizations:

```cypher
MATCH (n) WHERE NOT n:_NormalizationTrace
RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS properties
```

Replaying the original spans rebuilds this context and repairs missing ownership on
previously normalized calls. It does not remove older duplicate MAS/Agent identities.
Private context currently has no automatic expiry; include it in graph retention/cleanup.

## Setup

Neo4j must be running. The worker reads its connection from environment variables (there is no
`.env` file in this repo): `NEO4J_URI` (or `NEO4J_HOST` and `NEO4J_PORT`), `NEO4J_USERNAME`,
`NEO4J_PASSWORD` and, optionally, `NEO4J_DATABASE`.

```bash
cd backend
export NEO4J_URI=bolt://localhost:7687 NEO4J_USERNAME=neo4j NEO4J_PASSWORD=...
```

Not needed with `--dry-run`.

## Run

The CLI is `rt-norm-worker-cli` (or `python -m rt_norm_worker.cli`).

### From a file

```bash
# log records (events)
uv run --package rt-norm-worker rt-norm-worker-cli --input ~/claris/otel_logs.json --kind event

# spans (the default kind)
uv run --package rt-norm-worker rt-norm-worker-cli --input ~/claris/otel_traces.json --kind span
```

### Without the rules (no callbacks)

```bash
uv run --package rt-norm-worker rt-norm-worker-cli --no-rules --input ~/claris/otel_logs.json --kind event
```

### Dry run (in-memory graph, nothing written to Neo4j)

```bash
uv run --package rt-norm-worker rt-norm-worker-cli --dry-run --no-rules --input ~/claris/otel_logs.json --kind event
```

### From the OTel collector

```bash
uv run --package rt-norm-worker rt-norm-worker-cli --serve                       # 0.0.0.0:4318
uv run --package rt-norm-worker rt-norm-worker-cli --serve --host 127.0.0.1 --port 4318
```

### Options

| Option | Default | Meaning |
|---|---|---|
| `--input FILE` | `~/claris/otel_traces.json` | JSON array of spans or log records to read |
| `--kind span\|event` | `span` | what each record of the input is |
| `--rules FILE` | `rules.yaml` next to `pyproject.toml` | rules loaded into memory at startup |
| `--no-rules` | off | do not load the rules; no callbacks |
| `--serve` | off | receive records from the collector instead of reading `--input` |
| `--host`, `--port` | `0.0.0.0`, `4318` | where `--serve` listens |
| `--dry-run` | off | throwaway in-memory graph instead of Neo4j |

## Receiving from the collector (`--serve`)

The worker is an OTLP/HTTP receiver. Only **OTLP/JSON** is accepted (gzip is fine), so the
collector's exporter needs `encoding: json`:

```yaml
exporters:
  otlphttp/rt-norm:
    endpoint: http://rt-norm-worker:4318
    encoding: json
service:
  pipelines:
    traces:
      exporters: [clickhouse, otlphttp/rt-norm]
    logs:
      exporters: [clickhouse, otlphttp/rt-norm]
```

| Request | Does |
|---|---|
| `POST /v1/traces` | normalizes the spans |
| `POST /v1/logs` | normalizes the log records, then runs the rules |
| `GET /healthz` | liveness, `200` |

`200` once every record of the request is in the graph; `400` malformed JSON; `415` not JSON;
`500` normalization failed (the collector retries). Requests are processed one at a time.

```bash
# try it by hand
curl -s -X POST localhost:4318/v1/logs -H 'Content-Type: application/json' -d @logs-request.json
curl -s localhost:4318/healthz
```

## Rules and callbacks

A rule is a `name`, a `callback_url` and, for the rules that take one, a `value`. What a name
selects is built into the worker. After a log record is normalized, it is checked against every
rule; for each match the worker **POSTs** to the rule's `callback_url`:

```json
{"rule": "agent_events", "value": "researcher", "event": { "...the log record..." }}
```

Calls run in the background (5 s timeout); a failure is logged, not retried. Spans never call back.

| Rule | Matches when (on `LogAttributes`) |
|---|---|
| `all` | always |
| `tool_events` / `llm_events` | `event.name` starts with `tool.` / `llm.` |
| `tool_completed` / `llm_completed` | `event.name` is `tool.completed` / `llm.completed` |
| `mcp_traffic` | `network.protocol.name` is `mcp` |
| `agent_handoffs` | `topology.edge.kind` is `agent_handoff` and `source.agent` ≠ `target.agent` |
| `agent_events` (`value`) | `value` is `agent.name`, `source.agent` or `target.agent` |
| `service_events` (`value`) | `value` is the record's `ServiceName` |

### `rules.yaml`

See [`rules.example.yaml`](rules.example.yaml). A rule that takes a value can be listed once per value.

```yaml
rules:
  - name: all
    callback_url: https://audit.example.com/hooks/events
  - name: agent_events
    value: researcher
    callback_url: https://agents.example.com/hooks/researcher
```

### Managing the rules while `--serve` runs

Each rule has a key: its name, or `name=value`. Rules added this way live only in memory; a
restart goes back to the file.

```bash
curl -s localhost:4318/rules                                   # list
curl -s -X POST localhost:4318/rules -H 'Content-Type: application/json' \
  -d '{"name": "agent_events", "value": "planner", "callback_url": "http://localhost:9000/planner"}'
curl -s -X POST localhost:4318/rules -H 'Content-Type: application/json' \
  -d '{"rules": [{"name": "llm_events", "callback_url": "http://localhost:9000/llm"}]}'   # several
curl -s -X DELETE 'localhost:4318/rules/agent_events%3Dplanner'  # remove (key, URL-encoded)
```

## Neo4j

```bash
# wipe the graph (deletes everything!)
uv run --package rt-norm-worker python -I - <<'PY'
from scope.dependencies import get_neo4j_connector
c = get_neo4j_connector()
c.execute_command("MATCH (n) DETACH DELETE n")
c.close()
PY

# count the nodes by label
uv run --package rt-norm-worker python -I - <<'PY'
from scope.dependencies import get_neo4j_connector
c = get_neo4j_connector()
for r in c.execute("MATCH (n) RETURN labels(n)[0] AS l, count(*) AS n ORDER BY n DESC"):
    print(r["l"], r["n"])
c.close()
PY
```

## Tests

```bash
uv run --package rt-norm-worker --with pytest pytest workers/rt-norm-worker/tests -q   # the worker
uv run --package ioc-scope-norm --extra dev pytest norm/tests -q                       # the normalization
```

## Code

| File | What |
|---|---|
| `cli.py` | command line |
| `worker.py` | reads files; `process_record` normalizes one record, then notifies |
| `server.py` | OTLP/HTTP receiver and the `/rules` endpoints |
| `otlp.py` | flattens OTLP/JSON into the rows the files hold |
| `rules.py` | the rules in memory (`RuleSet`) and how each one matches |
| `callbacks.py` | POSTs to the callback URLs |
| `store.py` | Neo4j read/write |
