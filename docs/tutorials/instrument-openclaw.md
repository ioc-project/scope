# SCOPE — Tutorial: Instrument an OpenClaw-style MAS

<p class="eyebrow">Instrumenting agent gateways and runtimes you configure rather than edit</p>

## Before starting

Some multi-agent systems are not Python applications with agent functions you can
decorate. An OpenClaw-style system is a **gateway** that runs agents, tools and
model calls for you, and is extended through plugins. For these, use the
[Insight Module for Agentic Systems](https://github.com/outshift-open/insight-module-for-agentic-systems),
an OpenTelemetry plugin for OpenClaw. It is the alternative to the
[Observe SDK](instrument-your-app.md).

As with the Observe SDK, SCOPE is **not** installed in the gateway. The plugin
emits OpenTelemetry to an OTLP/HTTP collector, and the
[ingestion pipeline](../ingestion.md#ingestion) reads the stored spans.

```text
OpenClaw gateway + Insight Module plugin
  -> OTLP/HTTP collector (port 4318)
  -> telemetry store (ClickHouse in the reference deployment)
  -> SCOPE ingestion and analysis
```

## What the module captures

- Connected request, agent, tool and outbound-message traces.
- LLM usage, model, token and cost enrichment.
- Gateway diagnostics: queue, webhook, session and tool-loop signals.
- Optional auto-instrumentation of the Anthropic, OpenAI, Bedrock and Vertex AI
  provider SDKs.

A typical trace has this shape:

```text
openclaw.request              (root span)
├── openclaw.agent.turn
│   ├── llm.<model>
│   ├── tool.Read / tool.exec / tool.Write / tool.web_search
└── openclaw.message.sent
```

## 1. Install and register the plugin

The plugin is available on [ClawHub](https://clawhub.ai/outshift-open/plugins/insight-module).
Install it with the OpenClaw CLI (OpenClaw 2026.9.3 or later):

```bash
openclaw plugins install @outshift-open/insight-module
```

OpenClaw resolves this scoped package from ClawHub. Configure it in
`~/.openclaw/openclaw.json`:

```json
{
  "plugins": {
    "entries": {
      "insight-module": {
        "enabled": true,
        "hooks": { "allowConversationAccess": true },
        "config": {
          "endpoint": "http://host.docker.internal:4318",
          "serviceName": "openclaw-gateway",
          "protocol": "http",
          "traces": true,
          "metrics": true,
          "captureContent": true,
          "emitIoaObserveAttributes": true,
          "customAttributes": {
            "workspace-id": "<workspace-uuid>",
            "mas-id": "<mas-uuid>"
          }
        }
      }
    }
  }
}
```

| Key | Purpose |
|---|---|
| `endpoint` | OTLP/HTTP receiver. `host.docker.internal:4318` on macOS/Windows, `172.17.0.1:4318` on Linux Docker |
| `serviceName` | Service name on every span; use it to filter in the store |
| `traces` / `metrics` | Enable each signal |
| `captureContent` | Capture payloads on agent, request, LLM, tool and message spans |
| `emitIoaObserveAttributes` | Emit `ioa_observe.*` attributes (fork/join topology, handoff sequence, entity payloads). `false` keeps only OTel GenAI fields |
| `customAttributes` | Tags added to the root `openclaw.request` span only, such as `workspace-id` and `mas-id` |
| `hooks.allowConversationAccess` | Must be `true` for conversation-level capture |

!!! warning "Content capture"
    `captureContent` records prompts, responses and tool arguments and results.
    Disable it, or check the store's access and retention policy, before pointing
    a production gateway at a shared collector.

Restart the gateway:

```bash
openclaw gateway restart
```

## 2. Start a collector and store

The module's repository ships a ClickHouse and OpenTelemetry Collector stack:

```bash
cd observability-plugin/deploy
docker-compose up -d
```

Any OTLP/HTTP collector that forwards to a store SCOPE can read also works. See
[Instrumentation and ingestion](../ingestion/index.md)

## 3. Optional: auto-instrument provider SDKs

To get LLM spans from the provider SDKs, preload the instrumentation **before**
the gateway starts, using an absolute path to the installed plugin's
`instrumentation/preload.mjs` file. For a ClawHub installation, the package is
under OpenClaw's managed plugin project in `~/.openclaw/npm/projects/`:

```bash
export NODE_OPTIONS="--import /absolute/path/to/managed-plugin-project/node_modules/@outshift-open/insight-module/instrumentation/preload.mjs"
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export OTEL_SERVICE_NAME=openclaw-gateway
openclaw gateway start
```

The log should show
`[insight-module] ✅ GenAI instrumentation active via NODE_OPTIONS preload`. For a
systemd-managed gateway, put the same variables in a user override at
`~/.config/systemd/user/openclaw-gateway.service.d/override.conf` and run
`systemctl --user daemon-reload`.

## 4. Verify the traces

Run one request through the gateway, then query the store. In the reference
ClickHouse schema:

```sql
SELECT Timestamp, TraceId, SpanId, ParentSpanId, SpanName, StatusCode
FROM otel_traces
WHERE ServiceName = 'openclaw-gateway'
  AND Timestamp > now() - INTERVAL 1 HOUR
ORDER BY Timestamp;
```

Confirm that:

- One `openclaw.request` root span exists per request, with `workspace-id` and
  `mas-id` on it.
- `openclaw.agent.turn`, `tool.*` and LLM spans share the root's `TraceId` and have
  correct parents.
- Tool failures show an error status on the failing span.
- Content attributes (`gen_ai.input.messages`, `gen_ai.tool.call.arguments`) are
  present or absent as `captureContent` dictates.

Then locate the session in SCOPE's API or dashboard. As with the Observe SDK,
spans in the store do not by themselves start analysis; the deployment's
[ingestion mode](../ingestion/index.md#ingestion) and session-completion notification
must be configured.

!!! note "Not yet verified here"
    Which span attributes carry the session identity that SCOPE correlates on, and
    how the module signals session completion, are not documented in the module's
    README. Check `session.start`, `session.end` and `openclaw.runtime.session.*`
    spans in your store against your ingestion configuration before relying on
    completion-driven analysis.

## Other OpenClaw-like systems

For a gateway or runtime that is not OpenClaw, the module itself does not apply,
but the contract does. SCOPE needs OpenTelemetry spans that show:

1. **One trace per request**, with a root span and a stable session identity.
2. **Agent turns** as spans, parented under the request.
3. **Tool and LLM calls** as child spans, with model, token and tool-argument
   attributes following the OTel GenAI conventions.
4. **Handoffs between agents** with context propagated across the boundary.

Options, from least to most effort:

| Situation | Approach |
|---|---|
| The runtime has a plugin or hook API | Write a plugin like the Insight Module, which is the pattern to copy |
| The runtime is Python and you can edit it | Use the [Observe SDK](instrument-your-app.md) |
| You can only wrap the process | Use OTel auto-instrumentation for the provider SDKs; expect black-box fidelity |

Fields that are not convention-compliant are inferred on a best-effort basis, so
analysis coverage is wider where instrumentation is richer. See
[Fidelity tracks instrumentation richness](../ingestion/index.md#instrumentation).

## Related

- [Insight Module for Agentic Systems](https://github.com/outshift-open/insight-module-for-agentic-systems) — source, plugin README and an SRE-triage sample MAS
- [Instrument your app (Observe SDK)](instrument-your-app.md)
- [Instrumentation and ingestion](../ingestion.md)
- [Tutorials and Demos](index.md)