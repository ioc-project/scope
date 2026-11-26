"""Task extraction. Definitions follow ``cognitive-ontology.ttl`` (mas:Task).

The ``Task`` / ``TaskItem`` objects are defined in ``symbolic_extractor.models``.

A task is a unit of work assigned to a specific agent, with an expected deliverable. An AgentCall
is the execution of an agent, and its input is the assignment: the AgentCall that receives the
work owns the task (mas:ownedBy), so only AgentCall nodes apply. The delegator (mas:createdBy)
is not extracted: the input of a call does not say which call created it.

The input of the call is cleaned before the LLM sees it (see ``assignment_text``), and calls that
carry no assignment are skipped without an LLM request (see ``skip_reason``).

TODO: when the input says too little (for example the specialists of the trip planner, whose
input is only the conversation so far), use the system prompt of the agent as extra context.
The link of a task to the task of the previous agent call, or to the goal, is made afterwards by
``TaskRelationExtractor`` (``implements`` / ``achieves``).
"""

from __future__ import annotations

import json
import logging
import re
import uuid

from symbolic_extractor.extractors.base import ExtractionPrompt
from symbolic_extractor.extractors.claims import _clip, _first_text
from symbolic_extractor.models import KGNode, Task, TaskItem
from symbolic_extractor.store import SymbolicSessionStore

logger = logging.getLogger(__name__)

#: How many earlier tasks of the session are shown to the LLM for reuse (the most recent ones).
MAX_EARLIER_TASKS = 10

#: An input at least this long that does not end like a finished text is taken to be cut off (the
#: trace stores long inputs capped, for example at about 2000 characters). Short inputs are often
#: terse commands without a full stop, so they are never taken as cut.
CUT_OFF_MIN_CHARS = 1500

#: "input:<agent>:" / "output:<agent>:" prefix the instrumentation adds to the text of a call.
_PREFIX_RE = re.compile(r"^(?:input|output):[^:\s]*:")
#: "ROUND=1;TARGET=Finance;" header of the debate protocol.
_DEBATE_HEADER_RE = re.compile(r"^\s*(?:[A-Z_]+=[^;\n]*;\s*)+")
#: Bare resume marker of a router ("sre-u1-exec-resume-2-resume-3"): no sentence, no assignment.
_MARKER_RE = re.compile(r"^\S*resume(?:-\d+)*$")
#: Output the instrumentation writes when it recorded nothing.
_NO_CONTENT = "(no content recorded)"
#: Keys of a routing command ({"next": ...} or {"type": "Command", "goto": ...}).
_ROUTING_KEYS = {"next", "goto", "update", "type", "final_answer"}


def _strip_prefix(text: str) -> str:
    return _PREFIX_RE.sub("", text.strip(), count=1).strip()


def _conversation(text: str) -> str | None:
    """A LangGraph-style input (``{"args": [{"messages": [...]}]}``) as readable lines
    ``[sender]: content`` (``user`` for a message without a name), None for any other shape."""
    try:
        messages = json.loads(text)["args"][0]["messages"]
        lines = [
            f"[{message.get('name') or 'user'}]: {str(message['content']).strip()}"
            for message in messages
            if str(message["content"]).strip()
        ]
    except (TypeError, ValueError, KeyError, IndexError, AttributeError):
        return None
    return "\n".join(lines) or None


def assignment_text(raw: str) -> str:
    """The input of an AgentCall without the technical wrapping: the ``input:<agent>:`` prefix and
    the ``ROUND=..;TARGET=..;`` header are dropped, and a LangGraph message list becomes
    ``[sender]: content`` lines. Any other text is kept as it is."""
    text = _strip_prefix(raw)
    conversation = _conversation(text)
    if conversation is not None:
        return conversation
    return _DEBATE_HEADER_RE.sub("", text, count=1).strip()


def output_text(raw: str) -> str:
    """The output of an AgentCall without its prefix; empty when nothing was recorded."""
    text = _strip_prefix(raw)
    return "" if text == _NO_CONTENT else text


def looks_cut_off(text: str) -> bool:
    """True when ``text`` (an assignment) is long and does not end like a finished text (no full
    stop, question or exclamation mark at the end, a closing quote after one does not count), so
    its end, where the instruction usually is, may be missing. The LLM cannot tell this itself."""
    text = text.rstrip()
    return len(text) >= CUT_OFF_MIN_CHARS and not text.endswith((".", "!", "?"))


def _is_routing_command(output: str) -> bool:
    """True when ``output`` is only a routing command: no text for the user in it."""
    try:
        data = json.loads(output)
    except (TypeError, ValueError):
        return False
    if not isinstance(data, dict) or not data.keys() <= _ROUTING_KEYS:
        return False
    if not ({"next", "goto"} & data.keys()):
        return False
    return not str(data.get("final_answer") or "").strip()


def skip_reason(node: KGNode) -> str | None:
    """Why ``node`` carries no task (None when it may). Skipped calls cost no LLM request.

    * the input is empty or a bare resume marker (``sre-u1-exec-resume-2``): the router is
      woken up, nothing is assigned to it;
    * the output is only a routing command (``{"next": "itinerary_agent"}``): a turn of the
      moderator that routes the work to another agent. The work is owned by the agent receiving it.
    """
    text = assignment_text(_first_text(node.node, "inputContent", "prompt"))
    if not text:
        return "empty input"
    if _MARKER_RE.match(text):
        return "resume marker"
    if _is_routing_command(output_text(_first_text(node.node, "outputContent", "completion"))):
        return "routing command"
    return None


def same_task_key(task: Task) -> str:
    """Key under which two tasks count as the same: the text without case, extra spaces and
    trailing punctuation (the criteria are not compared: the same work is listed once)."""
    return " ".join(task.task_text.casefold().split()).rstrip(" .;:!")


def earlier_tasks(store: SymbolicSessionStore, node_id: str | None) -> list[Task]:
    """The tasks already extracted in the session from other agent calls, oldest first, at most
    ``MAX_EARLIER_TASKS`` (the most recent). A task is listed once however many calls have it
    (agent A has T1, agent B has T2 = T1: only T1), with the wording and criteria of the first
    one, which is the one later calls are asked to reuse. The nodes are processed in time order,
    so these are the tasks of the calls that ran before ``node_id``."""
    seen: dict[str, Task] = {}
    for stored in store.by_kind("tasks"):
        task = stored.element
        if stored.node_id == node_id or not isinstance(task, Task):
            continue
        key = same_task_key(task)
        # first wording wins; the position moves to the end so the cap keeps recent work
        seen[key] = seen.pop(key, task)
    return list(seen.values())[-MAX_EARLIER_TASKS:]


def _earlier_block(earlier: list[Task]) -> str:
    """The prompt section listing ``earlier`` tasks for reuse ('' when there are none)."""
    if not earlier:
        return ""
    lines = []
    for number, task in enumerate(earlier, 1):
        lines.append(f"{number}. task_text: {task.task_text}")
        if task.acceptance_criteria:
            lines.append(f"   acceptance_criteria: {json.dumps(task.acceptance_criteria, ensure_ascii=False)}")
    return "\n=== TASKS ALREADY EXTRACTED IN THIS SESSION (only to reuse their exact wording) ===\n" + "\n".join(lines) + "\n"


SYSTEM_PROMPT = """\
You extract TASKS from the input of one agent call in a multi-agent system trajectory.

A task is a unit of work assigned to, or taken on by, the agent that runs the call, with an \
expected deliverable. The input of the call is the assignment and the agent is its owner. \
Test: does it have an owner and an expected deliverable?
Example: "Find round-trip transport to Lisbon, Fri evening to Sun evening, for 2."

Not a task: context given to the agent with no work asked, greetings, a bare technical marker \
(for example "agent-resume-2"), and the agent's own plan or answer (that is the output).

Do not take background for the assignment. Background (the situation, who said or wants what, \
the case under discussion) only gives context. The assignment is the instruction addressed to \
this agent ("Analyze ...", "Rate ...", "State only your opening ...", "Propose ..."), usually \
in the last instructions of the input. Write the task from that instruction and use the \
background only to make it self-contained.
Example: input "The site got slow after the update. The manager wants a full report by Friday. \
List only the three most likely causes, one line each." -> task "List the three most likely \
causes of the slowdown, one line each", not "Write a full report by Friday".

The input may be a conversation, one message per line as "[sender]: text" ("user" is the human). \
Then the assignment is what is asked of this agent given the conversation, usually the request \
of the user; messages from other agents are context, not assignments for this agent.

An instruction to answer is an assignment. In a debate round the input holds what the other \
agents said, usually followed by a short instruction ("Respond from your criteria only", "State \
only your position", "Keep to 4-8 sentences"). The task is then the response itself: what this \
agent is asked to produce, from its own role or criteria, with the stated limits as acceptance \
criteria. Example: "Respond to the positions of the other agents from your own criteria, in 4-8 \
sentences." The input can be cut off before its instruction; when it only holds statements of \
other agents addressed to this agent, the implied assignment is still to respond to them (and \
"inferred" is true). Never \
turn what the other agents said, demanded or decided into the task of this agent.

For each task return:
- task_text: the work to be done and the deliverable expected, as one short, self-contained \
sentence written as an assignment. Stay faithful to the wording and meaning of the input and do \
not add information it does not contain.
- acceptance_criteria: a list of the conditions the input states for the deliverable to count \
as done (scope, format, limits, checks to make). Use only what the input states: an empty list \
when it states none. Never invent criteria.
- inferred: true when the assignment itself is not stated in the input and you deduced it from \
the context: there is no instruction addressed to this agent, or the input ends before it (the \
header says "Input looks cut off: yes"), so you wrote the task from what the other agents said or \
from the topic. False when the input states the assignment. When in doubt, use true: a guessed \
task must never look like a stated one. When the input looks cut off, still return the task you \
can deduce, with inferred true; return no task only when nothing at all can be deduced.
- status: where the task stands, judged from the output and the call flags:
    * "assigned": no sign that the agent started (no output, or "(no output recorded)" with no \
failure flagged: say nothing more than that the work was assigned).
    * "accepted": the agent acknowledges or commits to the work but delivers no result yet.
    * "in_progress": the output is partial, or the agent is waiting on something.
    * "complete": the output delivers what the task asked for.
    * "failed": the call failed, or the output says the agent cannot deliver.

Rules:
- Extract only from the section "INPUT OF THE AGENT CALL". The output section is only there to \
set the status; never extract a task from it.
- One task per distinct piece of work. When the input assigns several independent jobs, return \
one task for each; never repeat a task. Usually there is one.
- If the input assigns no work, return an empty list.
- A section "TASKS ALREADY EXTRACTED IN THIS SESSION" may follow the output. It is there only so \
that the same work gets the same wording. It never decides whether this call has a task: first \
extract the task of this input as usual, even if the same work was already done or extracted \
before. Then, when that task is the same piece of work as one of the listed ones (same \
deliverable, even if this input is longer or comes with another message history), return the \
listed task EXACTLY: the same task_text, character for character, and the same \
acceptance_criteria. When the deliverable is different, keep your own wording; a similar topic \
is not enough to reuse one.

Reply with a JSON object only:
{"tasks": [{"task_text": "...", "acceptance_criteria": ["..."], "inferred": true|false, \
"status": "assigned|accepted|in_progress|complete|failed"}]}
"""


class TaskExtractionPrompt(ExtractionPrompt[TaskItem, Task]):
    """Tasks are assigned to an agent execution (mas:ownedBy), so only AgentCall nodes apply."""

    element_key = "tasks"
    node_types = frozenset({"AgentCall"})
    item_model = TaskItem
    system_prompt = SYSTEM_PROMPT

    def render(self, node: KGNode) -> str | None:
        return self._render(node, [])

    def render_in_session(self, node: KGNode, store: SymbolicSessionStore | None) -> str | None:
        return self._render(node, earlier_tasks(store, node.node_id) if store is not None else [])

    def _render(self, node: KGNode, earlier: list[Task]) -> str | None:
        reason = skip_reason(node)
        if reason is not None:
            logger.debug("AgentCall %s skipped (%s)", node.node_id, reason)
            return None
        if not node.node_id:
            # A task needs the call that owns it.
            logger.warning("AgentCall without an id, no task extracted")
            return None
        input_text = assignment_text(_first_text(node.node, "inputContent", "prompt"))
        output = output_text(_first_text(node.node, "outputContent", "completion"))
        return (
            f"Agent call id: {node.node_id}\n"
            f"Agent: {_first_text(node.node, 'agentName', 'name') or 'unknown'}\n"
            f"Call success: {_first_text(node.node, 'success') or 'unknown'}\n"
            f"Call contains an error: {_first_text(node.node, 'contains_error') or 'unknown'}\n"
            + ("Input looks cut off: yes\n" if looks_cut_off(input_text) else "")
            + "\n"
            f"=== INPUT OF THE AGENT CALL (extract tasks from this) ===\n{_clip(input_text)}\n\n"
            f"=== OUTPUT OF THE AGENT CALL (only to set the status) ===\n{_clip(output) or '(no output recorded)'}\n"
            + _earlier_block(earlier)
        )

    def build_element(self, item: TaskItem, node: KGNode) -> Task:
        # Deterministic id: re-running on the same session yields the same task ids.
        task_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"{node.node_id}|task|{item.task_text}"))
        data = item.model_dump()
        # The LLM cannot see that an input was cut, so a cut input forces the flag.
        if looks_cut_off(assignment_text(_first_text(node.node, "inputContent", "prompt"))):
            data["inferred"] = True
        return Task(**data, id=task_id, owned_by=node.node_id or "")
