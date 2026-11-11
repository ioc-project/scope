# Event normalization

How `norm` turns OTel **log records** (events) into knowledge-graph nodes and edges. It covers
the streaming path, `StreamNormalizer.process(record, kind="event")`, used by the
[rt-norm-worker](../../workers/rt-norm-worker/README.md). Spans take a different path
(`dispatch_span`, see [normalization.md](normalization.md)).

Code: [`events.py`](../src/norm/ioa_observe/events.py) routes on `LogAttributes["event.name"]`;
the handlers are in [`handlers/`](../src/norm/ioa_observe/handlers/).

## How an event is processed

1. A dry run on an empty registry finds which node ids (and agent names) the event touches.
2. The reader loads them from the graph store, together with the event's session subgraph.
3. The handler runs against the loaded graph.
4. Only the difference is returned as a `StreamDelta` and written back.

Nothing is kept between events: the stored graph is the state. Consequences:

- Handlers are **idempotent**. Replaying an event changes nothing.
- Handlers are **order-tolerant**. Most of them create whatever is missing above the node they
  need (MAS, Session, MASCall, Agent, AgentCall), so a `completed` event arriving alone builds
  what its `started` would have.
- A node that already exists is **reused, not overwritten**. `startTime` never changes;
  `endTime` only moves forward. Initial and final States are set once, by whichever event
  arrives first.
- An event with no `session.id` (or no agent / tool name where one is needed) is ignored.

## Identity

| Node | Id |
|---|---|
| `Session` | `session.id` without its `<application_id>_` prefix |
| `MAS` | `application_id` (falls back to `ServiceName`) |
| `MASCall` | `mascall:<session id>` |
| `Agent` | the agent's **name** (`agent.name`, `source.agent`, `target.agent`, `tool.name` of an a2a tool, the card's `name`). The same name in another MAS is another Agent, with id `<mas>:<name>`. The span id is the AgentCall's, not the Agent's. |
| `AgentCall` | hash of the Agent's id and the span id (`topology.node.*` `span.id`, or `ioa_observe.agent.span_id` of the other events) |
| `LLM` | `llm-<provider>-<model>` (from `llm.name`, `<provider>/<model>`) |
| `LLMCall` | hash of provider, model and `llm.call.id` (or the event's span) |
| `Tool` | the tool name |
| `ToolCall` | `tool.call.id` (= the `message.id` of the MCP messages) when there is one, else a hash of tool name and span |
| `State`, `Transition` | hashes of the owner's span and the content |

Events that carry only an `ioa_observe.agent.span_id` (`tool.*`, `mcp.tools.discovered`) find the
Agent through the AgentCall of that span.

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

Reads `source.agent.span.id`, `target.agent`. Creates nothing.

- Adds `executesAgent` from the **AgentCall** of the source span to the **Agent** named
  `target.agent`.
- Ignored when either does not exist: a target that is not an agent (an MCP server such as
  `debug`) matches nothing.

`executesAgent` otherwise means "this call is an instance of that agent"; here it also records
the handoff. A call's own agent is therefore the one it was created for, not any `executesAgent`
target (`_own_agent_id`).

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
- Creates `MAS` (the first part of `session.id`, `<masname>_<sessionid>`) and `Session` (the
  rest), linked by `executesSession`.
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

### `a2a.message.received`

Same nodes as `a2a.message.sent`.

- Keeps `message.input` as a State of the **recipient's** latest AgentCall, only if that call
  already exists. The event does not carry the recipient's span id, and its call is normally
  created a few milliseconds later by its own `topology.node.started`, so usually nothing of
  the message is kept.

### `mcp.message.sent` / `mcp.message.received`

Reads `message.input` (JSON), `message.id`, `source.agent`, `source.agent.span.id`.

- Ignored when `message.input` is empty or has no `name` (a `tools/list` request).
- Ensures the sender's chain: MAS (from `application_id`, else the first part of `session.id`),
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
- Creates `MAS` (named by the first part of `session.id`, `<masname>_<sessionid>`) and `Session`.
- Creates a `Tool` for every entry in `tools` with its `description`, and fills the description
  of a Tool that already exists without one.
- The event names the agent only by span id, so the agent is the one of the AgentCall of that
  span, and every tool is linked to it with `usesTool`. If that call does not exist yet (the
  event arrives before the agent's `topology.node.started`), the tools are created **without**
  the link: nothing is remembered to link them later.

## Not handled

These are ignored; `dispatch_event` returns `False` for names it does not know.

| Event | Status |
|---|---|
| `slim.message.sent`, `slim.message.received` | `TODO` |
| `mcp.tools.published` | `TODO` |
| `a2a.message.completed` | no case |
| `llm.tools.presented` | no case |

## Known gaps

- The handoff between agents is only the `executesAgent` edge from `topology.edge.updated`. The
  ontology has no Agent-to-Agent relationship.
- `tool.*` events without `tool.call.id` and without `ioa_observe.agent.span_id` create a
  ToolCall that nothing links to an agent.
- When an agent runs twice it has one `Agent` and two `AgentCall` nodes. The same agent name in
  another MAS is a different Agent.
- `mcp.tools.discovered` arriving before the agent's `topology.node.started` does not link its tools.
