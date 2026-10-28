# SCOPE — Tutorial: Instrument your app

<p class="eyebrow">Instrumenting agents manually or with a coding harness</p>

## Before starting

SCOPE analyses OpenTelemetry traces from a multi-agent system. It is the
**application running the agents** that is instrumented — SCOPE itself does not
need to be installed alongside them. The SDK sends telemetry to an OTLP/HTTP
collector, which stores the spans for SCOPE to process.

## Choose an instrumentation

Two SDKs are supported. Both emit OpenTelemetry to an OTLP/HTTP collector, so the
rest of the path is identical.

| | **AGNTCY Observe SDK** | **Insight Module for Agentic Systems** |
|---|---|---|
| Best for | Python agent apps you own and can edit (LangGraph, LlamaIndex, custom orchestrators) | OpenClaw-style gateways and agent runtimes, where you configure a plugin rather than edit agent code |
| Language | Python | Node.js / TypeScript (OpenClaw plugin) |
| How it hooks in | Decorators (`@graph`, `@agent`, `@task`, `@tool`) and `session_start()` | OpenClaw plugin config, plus an optional `NODE_OPTIONS` preload for provider SDKs |
| Protocol coverage | A2A, SLIM and MCP instrumentors | Request, agent-turn, tool, LLM and outbound-message spans; Anthropic, OpenAI, Bedrock and Vertex AI SDKs |
| Tutorial | This page | [Instrument an OpenClaw-style MAS](instrument-openclaw.md) |

The Observe SDK is the reference instrumentation. The
[Insight Module](https://github.com/outshift-open/insight-module-for-agentic-systems)
is the alternative when the MAS is an OpenClaw-style system. The module can also
emit `ioa_observe.*` attributes, so its spans carry the same topology fields as
Observe SDK spans.

Instrumentation and analysis are separate responsibilities. Both SDKs run inside
(or beside) the agent application and emit OpenTelemetry data. Exporting spans is
the input to analysis, not proof that an execution has been ingested or an insight
produced. This site's [SCOPE overview](../index.md#how-scope-works) describes the
session reconstruction, knowledge graph and analysis pipeline. Do not install the
analysis platform in each agent to enable instrumentation.

The reference data path is:

```text
Agent application + Observe SDK (or Insight Module)
  -> OTLP/HTTP collector
  -> telemetry store (ClickHouse in the reference deployment)
  -> SCOPE ingestion and analysis
  -> knowledge graph, insights, API and dashboard
```

For the Observe SDK, use the
[SDK setup guide](https://github.com/agntcy/observe/blob/main/GETTING-STARTED.md).
For the analysis deployment, use the
[SCOPE repository](https://github.com/ioc-project/ioc-scope). Other OpenTelemetry
emitters are admissible through the [ingestion pipeline](../ingestion.md#ingestion);
neither SDK is a prerequisite for adopting IoC.

The sections below cover the **Observe SDK**. For OpenClaw, go to
[Instrument an OpenClaw-style MAS](instrument-openclaw.md).

## Manual instrumentation (Observe SDK)

### 1. Prepare the collector and application

Make an OpenTelemetry collector reachable, with an OTLP/HTTP receiver and an
exporter to the telemetry store used by your analysis deployment. For a local
collector and ClickHouse, follow the
[Observe SDK development stack instructions](https://github.com/agntcy/observe#dev).
That stack collects telemetry; it does not deploy SCOPE's analysis pipeline.

Install the SDK in the agent application's environment:

```bash
pip install "ioa-observe-sdk==1.0.56"
export OTLP_HTTP_ENDPOINT=http://localhost:4318
```

These examples use the API documented for **SDK 1.0.56**, the version covered by
the source guide. Use your project's dependency manager and lockfile, and verify
compatibility before adopting a newer release. The distribution name
`ioa-observe-sdk` is equivalent to `ioa_observe_sdk`; Python imports use
`ioa_observe`.

If the application runs in a container, `localhost` refers to that container.
Use the collector's Compose service name, or `host.docker.internal` for a
collector running on the macOS host.

### 2. Identify the instrumentation boundaries

| Boundary | Instrumentation | Placement |
|---|---|---|
| Application process | `Observe.init(...)` | Once at startup, before serving requests |
| Independent request or conversation | `session_start()` | Around the complete execution |
| Graph or workflow | `@graph` | Top-level orchestration entry point |
| Logical agent | `@agent` | Each agent's execution function |
| Delegation or handoff | `@task` | The operation that invokes a specialist |
| Business tool | `@tool` | Externally meaningful tool calls |

Use stable names rather than request IDs in decorator names. Preserve existing
framework decorators; their ordering needs an executable check because a framework
may replace a function with a tool object. For MCP tool registration, the
registration decorator wraps the Observe-decorated function.

### 3. Add startup, session, agent and tool instrumentation

This small, executable example uses deterministic arithmetic instead of a model
provider, so no model API key is required. In an existing application, retain the
original tool and agent implementations and add only instrumentation.

```python
import os

from ioa_observe.sdk import Observe
from ioa_observe.sdk.decorators import agent, graph, tool
from ioa_observe.sdk.tracing import session_start


@tool(name="convert_currency")
def convert_currency(amount: float, rate: float) -> float:
    return round(amount * rate, 2)


@agent(name="currency_assistant")
def assistant(amount: float) -> str:
    converted = convert_currency(amount, 0.859)
    return f"{amount:.2f} USD = {converted:.2f} EUR (example rate)"


@graph(name="currency_conversion")
def run(amount: float) -> str:
    return assistant(amount)


def handle_request(amount: float) -> str:
    with session_start():
        return run(amount)


if __name__ == "__main__":
    Observe.init("currency-demo", api_endpoint=os.environ["OTLP_HTTP_ENDPOINT"])
    print(handle_request(100))
```

Run the example as a Python script. Its result should be
`100.00 USD = 85.90 EUR (example rate)`. In a web service, initialise Observe in the
process startup hook, not inside the request handler. `session_start()` takes no
session-name argument in this SDK version and records completion when its context
exits.

!!! warning "Keep `session_start()` at the entry point"
    Do not create a separate session inside each agent or tool. One independent
    request needs one shared session identity so its work can be reconstructed.
    Keep the context open until async or streaming work finishes, including
    cancellation and error paths.

### 4. Preserve context across agents and services

Same-process child operations inherit the active trace context. Across processes,
decorators alone are insufficient: the sender must propagate context and the
receiver must continue it before executing its agent.

1. Enable the SDK's relevant transport instrumentors once at startup, before
   creating clients or servers. See the
   [SDK integration examples](https://github.com/agntcy/observe/blob/main/GETTING-STARTED.md).
2. When the transport does not propagate context automatically, use OpenTelemetry
   `inject()` on the sender and `extract()` on the receiver with the same metadata
   carrier format. Attach the extracted context before invoking the specialist
   and detach it in a `finally` block.
3. Do not replace a valid context already established by transport instrumentation.
   Verify that the receiver also retains the expected session correlation;
   propagating a trace ID alone does not establish every SDK-specific attribute.
4. Check that the specialist span continues the delegation span's trace and has
   the correct parent. Do not create duplicate spans for an operation already
   covered by auto-instrumentation.

Protocol spans show message delivery; logical agent, handoff and tool spans show
what the application did. Both are useful, but they are not interchangeable.

## Instrumentation with coding harnesses

Use the same boundaries and acceptance checks when delegating the edits to a coding
assistant. Start with an existing working agent and a passing behaviour test.
Provide the SDK guide, the installed version and the prompt below to your chosen
harness:

| Harness | One-off use | Reusable guidance |
|---|---|---|
| Claude Code | Submit the prompt with the target files | Project-local skill or custom command |
| GitHub Copilot | Submit the prompt in agent mode with the target files | Repository instructions and reusable prompt files |
| Cursor | Submit the prompt to the agent with the target files | Project rules and reusable prompts |
| Other coding agents | Supply the prompt and relevant project context | The harness's supported project instructions or workflow mechanism |

### Shared instrumentation prompt

```text
Instrument this Python agent application with AGNTCY Observe SDK.

1. Inspect the project, dependency manager and installed SDK version first.
2. Identify the framework, process startup, request/conversation boundary,
   graph entry points, agents, delegation functions and tools.
3. Add ioa-observe-sdk if missing, using the project's dependency manager.
4. Initialise Observe once per process using OTLP_HTTP_ENDPOINT. For SDK
   1.0.56, import Observe from ioa_observe.sdk, decorators agent/graph/task/tool
   from ioa_observe.sdk.decorators and session_start from sdk.tracing.
5. Start one session around each independent request. Use graph for workflows,
   agent for agents, task for handoffs and tool for business tools.
6. Preserve framework decorators, prompts, business logic, exception behaviour
   and async/streaming semantics. Test decorator ordering.
7. Preserve distributed trace and session context across A2A, SLIM and MCP.
   Reuse transport instrumentation; do not duplicate its spans or overwrite
   valid current context.
8. Do not capture credentials or unrestricted prompt/response content.
9. Run the existing behaviour tests and a small instrumentation test covering
   span names, one completed session, parent-child links and an error path.
10. Report changed files, test results and how to verify exported traces and
    downstream session ingestion. State anything not verified.
```

### Reuse and validate the workflow

For repeated use, package this standard using the chosen harness's guidance
mechanism from the table above, and test it on a small fixture before applying it
broadly. For Claude Code specifically, the
source guide's `/observe-instrument instrument <path>` is an example **custom
command**, not a built-in Claude Code or Observe SDK command; it works only after
you have created or installed that workflow. Do not assume this command exists
in another harness. The shared prompt is sufficient for a one-off change in any
of the listed harnesses.

The harness changes; the instrumentation contract and verification do not.

## Verify traces and analysis ingestion

### 1. Inspect the exported spans

Run a successful request and an intentional tool-failure test. In the reference
ClickHouse exporter schema, this query finds recent instrumented sessions:

```sql
SELECT
    SpanAttributes['session.id'] AS session_id,
    min(Timestamp) AS first_span,
    count() AS span_count,
    groupUniqArray(SpanName) AS span_names
FROM otel_traces
WHERE Timestamp > now() - INTERVAL 1 HOUR
    AND ServiceName = 'currency-demo'
    AND SpanAttributes['session.id'] != ''
GROUP BY session_id
ORDER BY first_span DESC
LIMIT 20;
```

Use the returned session ID to inspect relationships and errors:

```sql
SELECT Timestamp, TraceId, SpanId, ParentSpanId, ServiceName, SpanName, StatusCode
FROM otel_traces
WHERE SpanAttributes['session.id'] = 'REPLACE_WITH_SESSION_ID'
ORDER BY Timestamp;
```

Adapt the database, table and service name to your deployment. Confirm that:

- Each request creates and completes one session, with agent, graph and tool spans.
- Distributed delegation and specialist spans share the expected trace identity
  and parent-child links, not just similar timestamps.
- Tool errors are recorded on the failing operation and original exceptions still
  propagate; cancellation and streaming completion close spans correctly.
- Existing behaviour tests still pass, framework tools remain callable and
  auto-instrumentation does not duplicate manually instrumented operations.
- Captured attributes and content comply with the deployment's access and
  retention policy. Inspect SDK capture settings, not only attributes you add.

### 2. Confirm the analysis side received the session

!!! note "Exporting spans does not by itself start analysis"
    The completion-driven SCOPE pipeline needs the deployment's
    **session-completion notification**. The SDK emits session completion, but
    the collector/store-to-ingestion integration must also be configured. If
    ClickHouse contains spans but SCOPE has no session, check this integration
    before changing agent decorators. Follow the deployed ingestion mode rather
    than assuming that the SDK deploys or triggers the entire analysis stack.

Locate the same session in the analysis deployment's API or dashboard. Verify its
reconstructed agents and tools, then inspect the available analysis results and
their source events. A successful request need not produce a cognitive-failure
finding; validate a detector with a controlled failure fixture rather than
assuming that the absence of a finding means telemetry was lost.

For a reusable coding-harness workflow, keep a small fixture set: a single-process agent,
a moderator and specialist, a distributed handoff, an MCP tool, and an error path.
Evaluate both unchanged application behaviour and the expected span relationships.

## Related

- [Instrument an OpenClaw-style MAS](instrument-openclaw.md) — the Insight Module alternative
- [AGNTCY Observe SDK](https://github.com/agntcy/observe) - SDK source, installation and local collector stack
- [Instrumentation and ingestion](../ingestion/index.md) — overview of SCOPE's ingestion pipeline and modes
- [SDK integration guide](https://github.com/agntcy/observe/blob/main/GETTING-STARTED.md) - framework and transport examples
- [Tutorials and Demos](index.md) — the other SCOPE walkthroughs
