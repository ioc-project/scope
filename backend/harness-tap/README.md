# harness-tap

A small, local tap for coding-agent harnesses (Codex, Claude Code). It turns
hook events and Codex rollout files into OpenTelemetry spans and ships them
over OTLP/HTTP to the same collector that feeds SCOPE's ClickHouse, so `norm`
gets evidence the harnesses' native telemetry does not carry.

Status: trial. Stdlib-only Python ≥ 3.9; no install step required.

## Why it exists

From the signal inventory (R01 Claude Code, R02 Codex):

| Gap in native telemetry | What the tap adds |
|---|---|
| Codex model calls have no request ID; `norm` produces no `LLMCall` and no tokens | `chat` spans keyed by the rollout's `response_id`, with `turn.id` and exact usage (`token_usage_record`) |
| Codex per-request tokens have no trace context | every tap span sits in one trace per session, parented to its turn |
| A cross-harness hop (agentbridge) cannot reach the receiving harness's spans | a session **anchor** span that is a child of an inherited `TRACEPARENT` (e.g. set by `agentbridge register`) |
| Claude Code tool arguments absent when content capture is off | optional `full` mode with opt-in content |

## Design

```
 harness hook ──stdin──▶ harness-tap hook ──▶ spool/pending.jsonl ─┐
                          │ (Codex Stop)                           │  flush (background)
                          └──▶ reconcile ◀── ~/.codex/sessions/…    ├──▶ OTLP/HTTP JSON /v1/traces
                                  rollout-*.jsonl                  │    → collector → ClickHouse
                                                                    └──▶ dump (rows norm can load)
```

### Span model

```
ioc.session <harness>      zero-length anchor; parent = inherited TRACEPARENT, if any
  invoke_agent <harness>   one per turn            (turn.id / ioc.turn.id)
    chat <model>           one per model request   (gen_ai.response.id = request_id = response_id)
    execute_tool <tool>    one per tool call       (gen_ai.tool.call.id = call_id / tool_use_id)
    invoke_agent <type>    one per subagent        (gen_ai.agent.id)
```

* Resource: `service.name=harness-tap`, `ioc.agent.harness=codex|claude-code`, plus any
  `ioc.*` you configure (`ioc.agent.id`, `ioc.mas.id`, `ioc.session.id`). Scope name
  `ioc.harness_tap`, so norm can tell tap evidence from native harness telemetry.
* Each span also carries the harness's own session key (`conversation.id` for Codex,
  `session.id` for Claude) so existing native joins line up.
* IDs are deterministic (`sha256(harness, session, kind, native id)`), so the hook path
  and the reconciler name the same entity identically and the spool never emits it twice.
* Codex turn spans keep Codex's own trace as `ioc.codex.trace_id` (from `task_started`),
  which matches the `TraceId` on Codex's native OTel log events for that turn.

### Codex rollout fields used (observed on CLI 0.167)

`session_meta` (id, session_id, originator, source, cli_version, model_provider),
`turn_context` (turn_id, model, approval_policy, sandbox_policy.type),
`event_msg/task_started` (turn_id, trace_id, started_at, turn_attribution.*),
`event_msg/task_complete` (completed_at, duration_ms, time_to_first_token_ms),
`token_usage_record` (response_id, turn_id, usage.*). Older rollouts without
`token_usage_record` fall back to `event_msg/token_count` `last_token_usage`.

## Trial

```bash
cd scope/backend/harness-tap
export PYTHONPATH=$PWD/src

# 1. point at the hub collector, tag the agent, register hooks for both harnesses
python3 -m harness_tap install --endpoint http://localhost:4318 \
    --resource ioc.agent.id=codex-dev --harness codex claude-code
#    Codex: open the TUI once and run /hooks to trust them (codex exec skips untrusted hooks)

# 2. use the harnesses as usual, then check
python3 -m harness_tap doctor
python3 -m harness_tap dump > /tmp/tap_rows.jsonl     # unsent spans, ClickHouse row shape

# 3. backfill existing Codex sessions without hooks
python3 -m harness_tap reconcile --flush

# remove
python3 -m harness_tap uninstall --harness codex claude-code
```

Useful options: `--capture-content on` (prompts, responses, tool I/O; off by default),
`--claude-mode full` (Claude tool spans from hooks; default `anchor` because Claude
already exports tool spans natively), `--no-async` (if your Codex rejects `async: true`).
Environment overrides: `HARNESS_TAP_HOME`, `HARNESS_TAP_ENDPOINT` / `OTEL_EXPORTER_OTLP_ENDPOINT`,
`OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_RESOURCE_ATTRIBUTES`, `IOC_AGENT_ID`, `IOC_MAS_ID`,
`IOC_SESSION_ID`, `HARNESS_TAP_CAPTURE_CONTENT`.

Query the result in ClickHouse:

```sql
SELECT SpanName, SpanAttributes['turn.id'] AS turn, SpanAttributes['request_id'] AS req,
       SpanAttributes['gen_ai.usage.input_tokens'] AS input_tokens, ParentSpanId
FROM otel_traces WHERE ServiceName = 'harness-tap' ORDER BY Timestamp;
```

## Not yet verified — check during the trial

* **Codex hook payloads** (`PreModelRequest`/`PostModelRequest` `token_usage`,
  `SubagentStart`/`SubagentStop`) follow the current Codex hooks reference; confirm your
  Codex version fires them.
* **Codex hook `session_id` vs rollout `session_meta.id`**: assumed equal. `doctor` lists
  sessions by harness; a mismatch shows up as two sessions for one run.
* **Rollout tool lines** (`function_call` / `custom_tool_call` + `*_output` by `call_id`)
  were absent from the inspected rollouts; parsing follows public descriptions.
* **Hook `tool_use_id` == rollout `call_id`** for Codex is assumed (it is what dedupes a
  hooked tool against the rollout).
* **Codex matcher**: hooks are registered without a matcher (all tools). Confirm in `/hooks`.
* **`TRACEPARENT` inheritance**: only works if the process that launches the harness
  (e.g. `agentbridge register`) sets it; that needs a change in agntcy/shadi.

## Next steps

1. Run the trial for S3 (Codex), S2 (Claude subagent) and S6 (agentbridge) and add the
   tap spans to the signal inventory.
2. Add a `harness-tap` source to `norm.harness` (scope `ioc.harness_tap`): `chat` → LLMCall,
   `execute_tool` → ToolCall, `invoke_agent` → AgentCall, anchor → hop callee.
3. Propose the `TRACEPARENT` hand-off in `agentbridge register` upstream.

## Tests

```bash
python3 -m pytest        # 17 tests, fixtures use the observed rollout field names
```
