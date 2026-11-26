"""Follow claims across LLM calls: which already extracted claims are in each call's input.

A *symbolic state* is the set of claim ids an agent call "knows": the claims present in the
input of its LLM calls plus the claims those calls produce. It is tracked per AgentCall:

* An agent call inherits the final state of its predecessor agent calls, but only the claims
  that actually show up in the input of its first LLM call(s) are kept (what the agent was
  really given).
* Inside an agent call the state evolves with its LLM calls. Call X sees the contributions of
  the LLM calls of the same agent call that finished before X started; a contribution is
  ``input_claims(call) + claims extracted from the call``. LLM calls that overlap in time do
  not see each other.
* ``input_claims(X)`` is the subset of the candidate claims whose ``verbatim_source`` occurs in
  the conversation part of X's input (the system prompt is ignored).

Wall-clock time alone cannot order agent calls (they may run in parallel), so the order comes
from the handoffs the normalisation wrote in the KG when there are any (``handoffs`` of the
trajectory, see ``kg_reader.fetch_handoffs``), and otherwise from the trajectory: the router's
``goto`` message names the next agent (matched to the first unused call of that agent starting
later), the worker hands control back to the first router call that starts after it, and a call
without any link follows the previous call by start time. A router that goes to ``__end__`` /
``end`` / ``finish`` has no successor. The KG handoffs win for the calls they cover; the
heuristics only decide the calls the KG says nothing about.

Assumptions of the heuristics (see the notes in ``output.py`` for the KG side): a ``goto`` in an
agent call's output marks a router, and workers return to the router implicitly.

Facts (tool results) are simpler, see ``track_input_facts``: they live and die inside the
agent call whose tool call produced them, so no state is handed from one agent call to
another.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field

from symbolic_extractor.store import SymbolicSessionStore

logger = logging.getLogger(__name__)

# Keys of a router's output naming the next agent. Deliberately few: a plain "next" also
# appears in worker inputs.
_ROUTE_KEYS = ("goto", "next_agent")
_TERMINAL_ROUTES = frozenset({"end", "__end__", "finish"})
#: Kinds of KG handoff used as such; ``handoff_fallback`` (start-time order) is not, see
#: ``_handoff_transitions``.
_EXPLICIT_HANDOFFS = frozenset({"handoff", "shared_state"})
_ROUTE_RE = re.compile(r'"(?:%s)"\s*:\s*"([^"]+)"' % "|".join(_ROUTE_KEYS))


class AgentTransition(BaseModel):
    """Control (and so claims) passing from one agent call to the next."""

    from_call: str
    to_call: str
    from_agent: str = ""
    to_agent: str = ""
    #: ``route`` router -> worker (goto), ``return`` worker -> router,
    #: ``sequence`` fallback: previous call by start time, ``handoff`` the KG says so (the
    #: normalisation found the successor's input to follow the predecessor's output).
    via: Literal["route", "return", "sequence", "handoff"]


class SymbolicStateTrace(BaseModel):
    """Result of the claim tracking over a session."""

    #: LLMCall id -> ids of the extracted claims present in its input.
    input_claims: dict[str, list[str]] = Field(default_factory=dict)
    #: LLMCall id -> ids of the extracted facts present in its input.
    input_facts: dict[str, list[str]] = Field(default_factory=dict)
    #: AgentCall id -> ids of the claims in its final symbolic state.
    agent_states: dict[str, list[str]] = Field(default_factory=dict)
    transitions: list[AgentTransition] = Field(default_factory=list)


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _message_texts(message: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    content = message.get("content")
    if isinstance(content, str):
        texts.append(content)
    for part in message.get("parts") or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and isinstance(part.get("content"), str):
            texts.append(part["content"])
        elif part.get("type") == "tool_call_response":
            response = part.get("response")
            if isinstance(response, str):
                texts.append(response)
            elif response is not None:
                texts.append(json.dumps(response, ensure_ascii=False))
    return texts


def input_texts(raw: str | None) -> list[str]:
    """Texts of the conversation in an LLM call input, whitespace-normalised.

    The input is OTel-style JSON: the system instructions array followed by the messages
    array (``[{"role", "parts": [...]}]``), glued by blank lines, so it is not valid JSON as a
    whole and each value is decoded separately. Messages without a role (system parts) and
    ``system`` messages are skipped, so a claim is never "found" in the system prompt. An
    input that is not in this format is searched as a plain string.
    """
    raw = raw or ""
    decoder = json.JSONDecoder()
    values: list[Any] = []
    position = 0
    while True:
        while position < len(raw) and raw[position].isspace():
            position += 1
        if position >= len(raw):
            break
        try:
            value, position = decoder.raw_decode(raw, position)
        except ValueError:
            return [_normalize_ws(raw)] if raw.strip() else []
        values.append(value)

    messages = [m for value in values if isinstance(value, list) for m in value if isinstance(m, dict)]
    if not any("role" in m or "parts" in m for m in messages):
        return [_normalize_ws(raw)] if raw.strip() else []
    return [
        _normalize_ws(text)
        for message in messages
        if "role" in message and message["role"] != "system"
        for text in _message_texts(message)
    ]


def route_decision(output_text: str | None) -> tuple[str, bool] | None:
    """``(target agent, is_terminal)`` when ``output_text`` is a router's routing command
    (``{"type": "Command", "goto": "x", ...}``), else None."""
    if not output_text:
        return None
    target: Any = None
    try:
        parsed = json.loads(output_text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        for source in (parsed, parsed.get("update")):
            if isinstance(source, dict):
                target = next((source[k] for k in _ROUTE_KEYS if isinstance(source.get(k), str)), None)
                if target:
                    break
    else:  # truncated or non-JSON output
        match = _ROUTE_RE.search(output_text)
        target = match.group(1) if match else None
    if not isinstance(target, str) or not target.strip():
        return None
    target = target.strip()
    return target, target.casefold() in _TERMINAL_ROUTES


def _handoff_transitions(agents: list[dict[str, Any]], handoffs: list[dict[str, Any]]) -> list[AgentTransition]:
    """The transitions the KG handoffs give (``via="handoff"``), each pair once.

    Only the explicit kinds count: ``handoff`` (the successor span names its predecessor) and
    ``shared_state`` (the two calls share a State). ``handoff_fallback`` is the normalisation's own
    start-time guess, no better than ours, so it is left to the heuristics. A handoff whose source
    starts after its target is dropped: a predecessor cannot start later than its successor. That
    happens when a sub-agent runs inside its caller's span, which the normalisation sees as a flat
    sibling; the call is then left to the heuristics.
    """
    by_id = {a["call_id"]: a for a in agents}
    transitions: list[AgentTransition] = []
    seen: set[tuple[str, str]] = set()
    backwards = 0
    for handoff in handoffs:
        src, dst = by_id.get(handoff.get("from_call")), by_id.get(handoff.get("to_call"))
        if src is None or dst is None or src is dst or handoff.get("kind", "handoff") not in _EXPLICIT_HANDOFFS:
            continue
        pair = (src["call_id"], dst["call_id"])
        if pair in seen:
            continue
        seen.add(pair)
        if (src.get("start_time") or 0.0) > (dst.get("start_time") or 0.0):
            backwards += 1
            continue
        transitions.append(
            AgentTransition(
                from_call=src["call_id"],
                to_call=dst["call_id"],
                from_agent=src.get("agent_id") or src.get("name") or "",
                to_agent=dst.get("agent_id") or dst.get("name") or "",
                via="handoff",
            )
        )
    if backwards:
        logger.info("%d KG handoff(s) ignored: the source agent call starts after the target", backwards)
    return transitions


def build_transitions(
    agent_steps: list[dict[str, Any]], handoffs: list[dict[str, Any]] | None = None
) -> list[AgentTransition]:
    """Links between the agent calls (steps sorted by ``start_time``), see module docstring.

    ``handoffs`` (``{"from_call", "to_call", "kind"}``, from the KG) give the transitions into the
    calls they cover (``via="handoff"``); the heuristics (route, return, sequence) only add the
    transitions into the calls with none. Without ``handoffs`` it is the heuristics alone."""
    agents = sorted(agent_steps, key=lambda s: s.get("start_time") or 0.0)
    routes = {a["call_id"]: d for a in agents if (d := route_decision(a.get("output_text")))}
    transitions: list[AgentTransition] = []
    routed_to: set[str] = set()

    def link(src: dict[str, Any], dst: dict[str, Any], via: Literal["route", "return", "sequence"]) -> None:
        transitions.append(
            AgentTransition(
                from_call=src["call_id"],
                to_call=dst["call_id"],
                from_agent=src.get("agent_id") or src.get("name") or "",
                to_agent=dst.get("agent_id") or dst.get("name") or "",
                via=via,
            )
        )

    # router -> worker: the earliest unused call of the named agent starting later
    for index, router in enumerate(agents):
        decision = routes.get(router["call_id"])
        if decision is None or decision[1]:
            continue
        target = next(
            (
                b
                for b in agents[index + 1 :]
                if (b.get("agent_id") or b.get("name")) == decision[0] and b["call_id"] not in routed_to
            ),
            None,
        )
        if target is None:
            logger.warning("goto %r of agent call %s matches no later agent call", decision[0], router["call_id"])
            continue
        routed_to.add(target["call_id"])
        link(router, target, "route")

    # worker -> router: control returns to the first router call starting after the worker
    for index, worker in enumerate(agents):
        if worker["call_id"] not in routed_to or worker["call_id"] in routes:
            continue
        router = next((b for b in agents[index + 1 :] if b["call_id"] in routes), None)
        if router is not None:
            link(worker, router, "return")

    # anything still without a predecessor follows the previous call (unless that one ended the run)
    has_predecessor = {t.to_call for t in transitions}
    for index in range(1, len(agents)):
        call, previous = agents[index], agents[index - 1]
        previous_route = routes.get(previous["call_id"])
        if call["call_id"] in has_predecessor or (previous_route and previous_route[1]):
            continue
        link(previous, call, "sequence")
    from_kg = _handoff_transitions(agents, handoffs or [])
    if from_kg:
        covered = {t.to_call for t in from_kg}
        return from_kg + [t for t in transitions if t.to_call not in covered]
    return transitions


def track_input_facts(
    trajectory_steps: list[dict[str, Any]],
    store: SymbolicSessionStore,
    inputs: Mapping[str, str] | None = None,
    match_facts: bool = True,
) -> dict[str, list[str]]:
    """``input_facts`` of every LLM call of the trajectory: LLMCall id -> fact ids.

    A fact is produced by a ToolCall and stays inside the AgentCall that owns it. The
    candidates of an LLM call are therefore the facts of the tool calls of its own agent call
    that had finished when it started (tool calls running in parallel, or later, are not
    visible). Of those, the ones whose ``verbatim_source`` occurs in the conversation part of
    the call's input (tool responses included, system prompt ignored) are the input facts,
    in the order of their tool calls. Nothing is inherited across agent calls or agents.

    With ``match_facts=False`` the textual check is skipped: every candidate is an input fact
    (the facts of the tool calls of the same agent call that finished before the LLM call).

    ``inputs`` maps an LLMCall id to its input text; the trajectory's ``input_text`` is used
    for calls that are not in it. LLM calls without a known agent call are left out.
    """
    inputs = inputs or {}
    # AgentCall id -> [(end time, ToolCall id)] in start order
    tools_by_agent: dict[str, list[tuple[float, str]]] = {}
    tool_steps = sorted(
        (s for s in trajectory_steps if s.get("kind") == "tool"),
        key=lambda s: s.get("start_time") or 0.0,
    )
    for step in tool_steps:
        owner = step.get("parent_id") or ""
        end = (step.get("start_time") or 0.0) + (step.get("duration_ms") or 0.0) / 1000.0
        tools_by_agent.setdefault(owner, []).append((end, step["call_id"]))

    snippets: dict[str, str] = {}  # fact id -> normalised verbatim_source
    for stored in store.by_kind("facts"):
        snippet = getattr(stored.element, "verbatim_source", None)
        if snippet and _normalize_ws(snippet):
            snippets[stored.id] = _normalize_ws(snippet)

    result: dict[str, list[str]] = {}
    for call in trajectory_steps:
        owner = call.get("parent_id") or ""
        if call.get("kind") != "llm" or not owner:
            continue
        start = call.get("start_time") or 0.0
        candidates = [
            fact.id
            for end, tool_id in tools_by_agent.get(owner, [])
            if end <= start
            for fact in store.for_node(tool_id, "facts")
        ]
        if not match_facts:
            result[call["call_id"]] = candidates
            continue
        texts = input_texts(inputs.get(call["call_id"]) or call.get("input_text"))
        result[call["call_id"]] = [
            fid for fid in candidates if fid in snippets and any(snippets[fid] in t for t in texts)
        ]
    return result


def track_symbolic_state(
    trajectory_steps: list[dict[str, Any]],
    store: SymbolicSessionStore,
    inputs: Mapping[str, str] | None = None,
    handoffs: list[dict[str, Any]] | None = None,
    match_facts: bool = True,
) -> SymbolicStateTrace:
    """Compute ``input_claims`` for every LLM call of the trajectory.

    ``match_facts`` is passed to ``track_input_facts`` (False: no textual check of the facts).

    ``inputs`` maps an LLMCall id to its input text; the trajectory's ``input_text`` is used
    for calls that are not in it. ``handoffs`` are the KG handoffs of the trajectory (see
    ``build_transitions``). Claims without a ``verbatim_source`` cannot be followed (they
    are never reported as input claims) but still belong to the state of the call that made
    them.
    """
    inputs = inputs or {}
    agents = [s for s in trajectory_steps if s.get("kind") == "agent"]
    llm_by_agent: dict[str, list[dict[str, Any]]] = {}
    for step in sorted((s for s in trajectory_steps if s.get("kind") == "llm"), key=lambda s: s.get("start_time") or 0.0):
        llm_by_agent.setdefault(step.get("parent_id") or "", []).append(step)

    transitions = build_transitions(agents, handoffs)
    predecessors: dict[str, list[str]] = {}
    for t in transitions:
        predecessors.setdefault(t.to_call, []).append(t.from_call)

    snippets: dict[str, str] = {}  # claim id -> normalised verbatim_source
    for stored in store.by_kind("claims"):
        snippet = getattr(stored.element, "verbatim_source", None)
        if snippet and _normalize_ws(snippet):
            snippets[stored.id] = _normalize_ws(snippet)
    skipped = len(store.by_kind("claims")) - len(snippets)
    if skipped:
        logger.info("%d claims have no verbatim_source and cannot be followed across calls", skipped)

    trace = SymbolicStateTrace(transitions=transitions)
    trace.input_facts = track_input_facts(trajectory_steps, store, inputs, match_facts)
    final_state: dict[str, dict[str, None]] = {}  # AgentCall id -> ordered set of claim ids

    for agent in sorted(agents, key=lambda s: s.get("start_time") or 0.0):
        agent_id = agent["call_id"]
        inherited: dict[str, None] = {}
        for predecessor in predecessors.get(agent_id, []):
            inherited.update(final_state.get(predecessor, {}))

        calls = llm_by_agent.get(agent_id, [])
        # (end time, contribution) of the LLM calls already processed in this agent call
        done: list[tuple[float, dict[str, None]]] = []
        for call in calls:
            call_id = call["call_id"]
            start = call.get("start_time") or 0.0
            earlier = [contribution for end, contribution in done if end <= start]
            candidates: dict[str, None] = {}
            if earlier:
                for contribution in earlier:
                    candidates.update(contribution)
            else:  # first call(s) of the agent call: filter what was handed over
                candidates.update(inherited)

            texts = input_texts(inputs.get(call_id) or call.get("input_text"))
            present = [cid for cid in candidates if cid in snippets and any(snippets[cid] in t for t in texts)]
            trace.input_claims[call_id] = present

            contribution = dict.fromkeys(present)
            contribution.update(dict.fromkeys(s.id for s in store.claims_for_node(call_id)))
            done.append((start + (call.get("duration_ms") or 0.0) / 1000.0, contribution))

        if calls:
            state: dict[str, None] = {}
            for _, contribution in done:
                state.update(contribution)
        else:
            state = inherited  # no LLM call: nothing to filter against, pass through
        final_state[agent_id] = state
        trace.agent_states[agent_id] = list(state)

    # LLM calls whose AgentCall is unknown cannot be placed in the state flow
    known = {s["call_id"] for s in agents}
    for owner, calls in llm_by_agent.items():
        if owner not in known:
            logger.warning("%d LLM call(s) have no known AgentCall (%r); no input_claims", len(calls), owner)
    return trace
