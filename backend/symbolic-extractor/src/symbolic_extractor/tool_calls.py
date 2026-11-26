"""Tool calls an LLM asked for, and the ToolCall nodes that ran them.

An LLM output in the OTel message format lists the tool calls it requests as ``tool_call`` parts:
``{"type": "tool_call", "id": "call_...", "name": "calculate", "arguments": {...}}``. The id is the one
of the provider; the ToolCall node of the KG does not keep it, so ``match_tool_calls`` finds the node of
each request from the trajectory: same agent call, ran after the LLM call (and before its next one), same
arguments, compatible name. No LLM is involved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from symbolic_extractor.models import SessionTrajectory


@dataclass(frozen=True)
class ToolCallRequest:
    """One tool call requested in the output of an LLM call."""

    id: str
    name: str
    arguments: dict[str, Any]


def _arguments(value: Any) -> dict[str, Any]:
    """The arguments of a tool call as a dict (they may be a JSON string); ``{}`` when there are none."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def requested_tool_calls(output: str) -> list[ToolCallRequest]:
    """The tool calls requested in ``output``, in order. ``[]`` when it is not an OTel message list or
    requests none. A request without an id is skipped: nothing could refer to it."""
    try:
        messages = json.loads(output)
    except (TypeError, ValueError):
        return []
    if not isinstance(messages, list):
        return []
    return [
        ToolCallRequest(part["id"], str(part.get("name") or ""), _arguments(part.get("arguments")))
        for message in messages
        if isinstance(message, dict)
        for part in message.get("parts") or []
        if isinstance(part, dict) and part.get("type") == "tool_call" and isinstance(part.get("id"), str)
    ]


def _time(step: dict[str, Any]) -> float:
    try:
        return float(step.get("start_time"))
    except (TypeError, ValueError):
        return float("inf")


def _step_arguments(step: dict[str, Any]) -> dict[str, Any] | None:
    """The keyword arguments a tool step ran with (``{"args": [], "kwargs": {...}}`` in its input), None
    when it was called with positional arguments or its input is not in that form."""
    try:
        data = json.loads(step.get("input_text") or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("args"):
        return None
    kwargs = data.get("kwargs", {})
    return kwargs if isinstance(kwargs, dict) else None


def _same_tool(requested: str, ran: str) -> bool:
    """The name an LLM used and the name of the tool step: equal, or one extends the other (the span of
    ``list_documents`` of an agent may be named ``list_documents_schedule_agent``)."""
    return bool(requested) and bool(ran) and (ran.startswith(requested) or requested.startswith(ran))


def match_tool_calls(trajectory: SessionTrajectory, llm_call_id: str) -> dict[str, str]:
    """Provider tool call id -> id of the ToolCall node that ran it, for the requests of LLM call
    ``llm_call_id``. Only the requests that find exactly their tool step are in the result (a request
    whose tool has not run yet, in a session in progress, is simply missing).

    A tool step belongs to the request when it is a step of the same agent call that started after the
    LLM call and before its next LLM call, has the same arguments and a compatible name. Of the steps
    that fit, the earliest one not taken by an earlier request is used, so two identical requests get
    two different steps.
    """
    llm = trajectory.step_for(llm_call_id)
    if llm is None or llm.get("kind") != "llm":
        return {}
    requests = requested_tool_calls(str(llm.get("output_text") or ""))
    if not requests:
        return {}
    parent = llm.get("parent_id")
    start = _time(llm)
    later_llm = [
        _time(s)
        for s in trajectory.steps
        if s.get("kind") == "llm" and s.get("parent_id") == parent and _time(s) > start
    ]
    end = min(later_llm, default=float("inf"))
    candidates = sorted(
        (
            s
            for s in trajectory.steps
            if s.get("kind") == "tool" and s.get("parent_id") == parent and start <= _time(s) < end
        ),
        key=_time,
    )
    taken: set[str] = set()
    matched: dict[str, str] = {}
    for request in requests:
        for step in candidates:
            call_id = step.get("call_id")
            if call_id in taken or not call_id:
                continue
            if _same_tool(request.name, str(step.get("name") or "")) and _step_arguments(step) == request.arguments:
                taken.add(call_id)
                matched[request.id] = call_id
                break
    return matched
