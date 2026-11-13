# Event normalization

How `norm` turns OTel **log records** (events) into knowledge-graph nodes and edges. It covers
the streaming path, `StreamNormalizer.process(record, kind="event")`, used by the
[rt-norm-worker](../../workers/rt-norm-worker/README.md). Completed spans use
[`dispatch_span`](../src/norm/ioa_observe/build.py); both paths share persisted reconciliation.

Code: [`events.py`](../src/norm/ioa_observe/events.py) routes on `LogAttributes["event.name"]`;
the handlers are in [`handlers/`](../src/norm/ioa_observe/handlers/).

## How an event is processed

1. A dry run on an empty registry finds which node ids (and agent names) the event touches.
2. The reader loads them from the graph store, together with the event's session subgraph.
3. The handler merges provisional event data or authoritative completed-span data.
4. Ownership, pending messages/tools/handoffs and derived trajectories are reconciled.
5. Only the difference is returned as a `StreamDelta` and written back.

Nothing is kept between events: the stored graph is the state. Consequences:

- Handlers are **idempotent**. Replaying an event changes nothing.
- Handlers are **order-tolerant**. Most of them create whatever is missing above the node they
  need (MAS, Session, MASCall, Agent, AgentCall), so a `completed` event arriving alone builds
  what its `started` would have.
- Existing identities are reused. Late starts can correct `startTime` and input; completed
  spans replace provisional timing, content and metrics without creating a second call.
  Container start/end times encompass their children, and final states follow the child
  ending last, not the child starting last.
- Compact `rtNormalization` metadata retains pending relationships, canonical trajectory
  identities and processed event ids. Existing persisted worker metadata and the
  `handoffSourceSpanIds` property remain readable; no raw telemetry replay buffer is kept.
- An event with no `session.id` (or no agent / tool name where one is needed) is ignored.

## Identity

| Node | Id |
|---|---|
| `Session` | `session.id` without its `<application_id>_` prefix |
| `MAS` | originating application in a propagated `<application>_<session>`; otherwise `application_id`, then `ServiceName` |
| `MASCall` | `mascall:<session id>` |
| `Agent` | the agent's **name** (`agent.name`, `source.agent`, `target.agent`, `tool.name` of an a2a tool, the card's `name`). The same name in another MAS is another Agent, with id `<mas>:<name>`. The span id is the AgentCall's, not the Agent's. |
| `AgentCall` | hash of the Agent's id and the span id (`topology.node.*` `span.id`, or `ioa_observe.agent.span_id` of the other events) |
| `LLM` | `llm-<provider>-<model>` (from `llm.name`, `<provider>/<model>`) |
| `LLMCall` | hash of provider, model and `llm.call.id` (or the event's span) |
| `Tool` | the tool name |
| `ToolCall` | `tool.call.id` (= the `message.id` of the MCP messages) when there is one, else a hash of tool name and span |
| `State`, `Transition` | hashes of the owner's span and the content |

Events that carry only an `ioa_observe.agent.span_id` (`tool.*`, `mcp.tools.discovered`) find the
Agent through the AgentCall of that span. Ownership is backfilled when that call arrives.
Application names may contain underscores; participating MCP services do not create a
second MAS merely because their application or service name differs.

Span ownership also follows parent chains through MCP/A2A/framework transport spans.
An explicit agent-owner span id takes precedence over that ancestor's ordinary parent.
Intermediate spans need not have a session id: private trace context retains their
`TraceId`, `SpanId`, parent, owner and optional session id, without storing raw payloads
or manufacturing ontology executions. Known session boundaries and trace ids are
respected; missing links and cyclic parent chains remain unresolved rather than guessed.

Streaming adapters must implement `GraphReader.load_span_contexts(trace_id)` and persist
`StreamDelta.span_contexts` as well as ontology deltas. A sessionless transport arriving
last rechecks sessions referenced by that trace. Without `TraceId`, ancestry is scoped
to the explicit session; a span with neither identifier cannot be correlated safely.
Batch normalization uses the same ancestry resolver.

Agent and Tool identities may share a name/id while remaining different ontology types.
Streaming edge endpoints are resolved using their relationship type (and the concrete
execution relationship for generic `executes`), not whichever same-id node was loaded last.
The existing verifier can still report domain mismatches when it collapses such node ids;
that diagnostic limitation is separate from the typed relationships persisted to Neo4j.

Execution graph metadata is retained on the MAS. A graph node becomes a declared Agent
only when it matches an observed agent's identity, name or runtime alias. Routers and
tool-serving graph nodes are not assumed to be agents.

## States

Every execution (`Session`, `AgentCall`, `LLMCall`, `ToolCall`) gets an initial `State`
(`hasInitialState` + `hasState`) when it starts and a final `State` plus a `Transition`
(`hasFinalState`, `inputTo`, `leadsTo`, `correspondsToTransition`, `representsExecution`) when it
completes. The content of a State is the event's input or output text, or
`(no content recorded)` when there is none.

## Events

### `topology.session.started`

Reads `session.id`, `application_id`, `event.time`.

- Creates `Session` and `MAS`, linked by `executesSession`.
- Creates the Session's initial State.

### `topology.session.completed`

Same nodes as `session.started`.

- Moves the Session's `endTime` forward and sets `duration`.
- Creates the initial State if there is none yet.
- Adds a final State and Transition for **each** completion. Only the **latest** one, by
  `event.time` and not by arrival, holds `hasFinalState`; a later event moves it, an earlier or
  repeated one leaves it where it is. The other completions keep `hasState` only.

### `topology.node.started`

Reads `agent.name`, `agent.input`, plus the common ids. The event's own `span.id` is the AgentCall's.

- Creates `Agent` (keyed on its name; one created earlier by a card or a handoff is reused) and
  `belongsToMAS` to the application.
- Creates `AgentCall` with `executesAgent`, and `MASCall` with `hasAgentCall` (plus Session and
  MAS if missing).
- Attaches ToolCalls that arrived before it (`hasToolCall`, `usesTool`).
- Creates the AgentCall's initial State from `agent.input`.

### `topology.node.completed`

Same nodes as `node.started`.

- Moves the AgentCall's `endTime` forward and sets `duration`.
- Creates the initial State if there is none (from `agent.input`).
- Creates the final State from `agent.output` and the Transition, once.

### `topology.edge.updated`

Reads `source.agent.span.id`, `target.agent`.

- Retains a pending handoff hint until both agent calls exist.
- Connects their execution trajectories through the shared handoff heuristic.
- Never adds a second `executesAgent` target to the sender or invents an Agent for a
  tool-serving target. Explicit MCP/tool topology edges are ignored as agent handoffs.

### `llm.started`

Reads `llm.name`, `llm.input`, `llm.call.id`, `agent.name`, `ioa_observe.agent.span_id`.

- Ensures the chain down to the LLM call: MAS, Session, MASCall, Agent (`agent.name`), AgentCall (of
  `ioa_observe.agent.span_id`), `LLM`, `LLMCall`.
- Adds `usesLLM` (Agent to LLM), `executesLLM`, `hasLLMCall` (AgentCall to LLMCall).
- Creates the LLMCall's initial State from `llm.input`.

### `llm.completed`

Same nodes as `llm.started`.

- Moves the LLMCall's `endTime` forward and sets `duration`.
- Creates the final State from `llm.output` and the Transition, once.

### `llm.tools.presented`

Reads `capability.content` (JSON tools, including OpenAI-style function descriptors) and
`ioa_observe.agent.span_id`, falling back to the record's span.

- Creates or enriches Tool descriptions.
- Persists the offered tool ids and owner span, linking them with `usesTool` when the
  owning AgentCall arrives.

### `tool.started`

Reads `tool.name`, `tool.input`, `tool.type`, `tool.call.id`, `ioa_observe.agent.span_id`.

For a regular tool:

- Creates `Tool` and `ToolCall` with `executesTool`, plus MAS, Session and MASCall.
- If the owning AgentCall (found by `ioa_observe.agent.span_id`) exists: `hasToolCall` and
  `usesTool` from its Agent. An AgentCall is not created here; `topology.node.*` links it later.
- Creates the ToolCall's initial State from `tool.input`.

For `tool.type = a2a` (a call to another agent): no Tool or ToolCall. Only makes sure an Agent
named `tool.name` exists, unattached if it has to create it.

### `tool.completed`

Same nodes as `tool.started`.

- Moves the ToolCall's `endTime` forward and sets `duration`.
- Sets `success` from `tool.status`.
- Creates the final State from `tool.output` and the Transition, once.
- An `a2a` tool does nothing.

### `a2a.agent_card`

Reads `session.id`, `capability.content` (JSON): `name`, `description`.

- Ignored without a `session.id`.
- Creates the originating `MAS` and `Session`, linked by `executesSession`.
- Adds the `Agent` named by the card, keyed on its **name** (the card has no span id of the
  agent it describes), with `belongsToMAS`. An Agent of that name already in the MAS is reused
  and only gets its description. The ontology has no
  Agent-to-Session edge, so the Session is not linked to the Agent.
- Ignores the card's own `ioa_observe.agent.span_id`: it is the span of the agent that fetched
  the card, not of the one it describes.
- The rest of the card (version, interfaces, skills, ...) is not stored yet.

Every other event that needs the Agent (`topology.node.*`, `llm.*`, `a2a.message.*`,
`mcp.message.*`) uses the same node, since it is keyed on the same name. Only if an Agent of that
name exists in a different MAS does it create a separate one (`<mas>:<name>`).

### `a2a.message.sent`

Reads `source.agent`, `source.agent.span.id`, `target.agent`, `message.id`, `message.input`.

- Ensures the sender's chain: MAS, Session, MASCall, Agent, AgentCall.
- Ensures an Agent named `target.agent` (unattached if new).
- Keeps `message.input` as a State of the sender's AgentCall (`hasState` only, so its initial
  and final States are untouched).

### `a2a.message.received` / `a2a.message.completed`

- Retains incoming `message.input` and completion `message.output` as separate message
  States, even before the corresponding AgentCall exists.
- Received messages are reconciled with the recipient's call by name and event time;
  completion replies attach to the source agent span.
- Does not fabricate a recipient AgentCall or replace an agent's own final State.

### `mcp.message.sent` / `mcp.message.received`

Reads `message.input` (JSON), `message.id`, `source.agent`, `source.agent.span.id`.

- Ignored when `message.input` is empty or has no `name` (a `tools/list` request).
- Ensures the sender's chain: originating MAS,
  Session, MASCall, Agent, AgentCall.
- Creates `Tool` (`message.input.name`) and `ToolCall` keyed on `message.id`, with
  `executesTool`, `hasToolCall` and `usesTool`.
- Creates the ToolCall's initial State from `message.input`.

`sent` and `received` of one message, and the `tool.*` events of the same call, share the
`message.id` / `tool.call.id`, so they land on one ToolCall.

### `mcp.message.completed`

Same nodes as `mcp.message.sent`.

- Moves the ToolCall's `endTime` forward and sets `duration`.
- Sets `success` to `not isError` when `message.output` says so.
- Creates the final State from `message.output` and the Transition, once.

### `mcp.tools.discovered`

Reads `ioa_observe.agent.span_id`, `session.id`, `capability.content` (JSON with a `tools` list).

- Ignored without `ioa_observe.agent.span_id` or a session.
- Creates the originating `MAS` and `Session`.
- Creates a `Tool` for every entry in `tools` with its `description`, and fills the description
  of a Tool that already exists without one.
- The event names the agent only by span id, so the agent is the one of the AgentCall of that
  span, and every tool is linked to it with `usesTool`. If that call does not exist yet (the
  event arrives before the agent's `topology.node.started` or completed span), owner hints
  and tool ids are persisted so the link is backfilled.

## Not handled

These are ignored; `dispatch_event` returns `False` for names it does not know.

| Event | Status |
|---|---|
| `slim.message.sent`, `slim.message.received` | `TODO` |
| `mcp.tools.published` | `TODO` |

## Known gaps

- `tool.*` events without `tool.call.id` and without `ioa_observe.agent.span_id` create a
  ToolCall that nothing links to an agent.
- When an agent runs twice it has one `Agent` and two `AgentCall` nodes. The same agent name in
  another MAS is a different Agent.
- Previously misclassified graph data is not automatically migrated or deleted. Compare
  end-to-end runs on a fresh graph. Ontology/SHACL findings are separate diagnostics;
  normalization does not weaken the verifier to hide them.
