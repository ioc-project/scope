"""Input and intermediate data models for the symbolic extractor."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from symbolic_extractor.store import Edge
from symbolic_extractor.symbolic_state import SymbolicStateTrace
from symbolic_extractor.usage import LLMUsage


class ExtractionRequest(BaseModel):
    """Input of the extractor: the session whose KG data should be processed."""

    model_config = ConfigDict(str_strip_whitespace=True)

    session_id: str = Field(min_length=1)


class KGNode(BaseModel):
    """An original KG node together with its type (KG label)."""

    node_type: str
    node: dict[str, Any]

    @property
    def node_id(self) -> str | None:
        return self.node.get("id")


#: node_type of the synthetic node mapping the user interaction of a session (not a KG label):
#: its ``query`` is the initial State of the Session (what the user asked), its ``response`` the
#: final State (what the system answered).
CONVERSATION_NODE_TYPE = "Conversation"


def _epoch(value: Any) -> float | None:
    """``startTime`` as epoch seconds. The KG stores a float epoch; datetimes and ISO
    strings are accepted too. Returns None when there is no usable timestamp."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def _timestamp_sort_key(node: KGNode) -> tuple[bool, float]:
    """Nodes without a usable ``startTime`` sort last; the sort is stable so ties keep KG order."""
    epoch = _epoch(node.node.get("startTime"))
    return (epoch is None, epoch if epoch is not None else 0.0)


class SessionData(BaseModel):
    """Session data retrieved from the KG through the SCOPE API.

    Mirrors the payload returned by ``scope.providers.kg.SCOPEKGProvider.fetch``:
    the Session node properties, its AgentCall / LLMCall / ToolCall nodes
    (enriched with names and input/output content), and the session-level
    conversation (initial / final State content). Any other key of the payload
    is preserved as an extra field, so dumping this model saves the full content
    returned by the KG.
    """

    model_config = ConfigDict(extra="allow")

    session_id: str
    session: dict[str, Any] = Field(default_factory=dict)
    agent_spans: list[dict[str, Any]] = Field(default_factory=list)
    llm_spans: list[dict[str, Any]] = Field(default_factory=list)
    tool_spans: list[dict[str, Any]] = Field(default_factory=list)
    input_text: str = ""
    output_text: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SessionData:
        """Build from the raw provider payload, keeping unmodelled keys as extras."""
        return cls(**payload)

    def conversation_node(self) -> KGNode | None:
        """The user interaction as a ``Conversation`` node: ``query`` (the session's input text) and
        ``response`` (its output text), None when the session has neither.

        TODO: multi-turn sessions. The Session only holds the first request and the last answer;
        once the traces contain several user interactions, return one node per turn.
        """
        if not (self.input_text or self.output_text):
            return None
        return KGNode(
            node_type=CONVERSATION_NODE_TYPE,
            node={
                "id": f"conversation:{self.session_id}",
                "sessionId": self.session_id,
                "query": self.input_text,
                "response": self.output_text,
            },
        )

    def nodes(self) -> list[KGNode]:
        """The ``Conversation`` node (when there is one) followed by all call nodes of the session,
        sorted by ``startTime`` when they have one."""
        nodes = [
            KGNode(node_type=node_type, node=node)
            for node_type, group in (
                ("AgentCall", self.agent_spans),
                ("LLMCall", self.llm_spans),
                ("ToolCall", self.tool_spans),
            )
            for node in group
        ]
        nodes = sorted(nodes, key=_timestamp_sort_key)
        conversation = self.conversation_node()
        return [conversation, *nodes] if conversation is not None else nodes


class SessionTrajectory(BaseModel):
    """The session's calls in execution order, as returned by
    ``scope.client.dal.get_session_trajectory``.

    Each step has ``call_id`` (the ``id`` of an AgentCall / LLMCall / ToolCall node),
    ``kind`` (``agent`` / ``llm`` / ``tool``), ``name``, ``agent_id`` (the acting agent's
    name), ``parent_id`` (for an LLM or tool call, the ``id`` of the AgentCall that owns it,
    empty for an AgentCall), ``start_time``, ``duration_ms``, ``input_text`` and
    ``output_text``. Unlisted keys are kept.

    ``handoffs`` are the handoffs between agent calls that the normalisation wrote in the KG (see
    ``kg_reader.fetch_handoffs``): dicts with ``from_call``, ``to_call`` (AgentCall ids) and ``kind``
    (``handoff``, ``handoff_fallback`` or ``shared_state``). ``None`` when they were not fetched
    (a file saved before they existed, or a reader without a handoff fetcher), ``[]`` when the KG
    has none.

    ``final_agent_call`` is the id of the AgentCall that produced the final answer of the session:
    the one sharing its final State with the Session (``kg_reader.fetch_final_agent_call``). ``None``
    when it is unknown: not fetched (a file saved before it existed, or a reader without the
    fetcher), or the KG has no final State for the session (yet).
    """

    model_config = ConfigDict(extra="allow")

    session_id: str
    steps: list[dict[str, Any]] = Field(default_factory=list)
    handoffs: list[dict[str, Any]] | None = None
    final_agent_call: str | None = None

    def final_llm_call(self) -> str | None:
        """Id of the LLMCall that wrote the final answer. None when the final agent call is unknown
        or has no LLM call. The steps are in execution order, so the order inside one agent call
        is reliable: the answer is the last LLM call of ``final_agent_call`` (the earlier ones are
        tool-use turns).
        """
        if not self.final_agent_call:
            return None
        calls = [
            s["call_id"]
            for s in self.steps
            if s.get("kind") == "llm" and s.get("parent_id") == self.final_agent_call
        ]
        return calls[-1] if calls else None

    def step_for(self, call_id: str) -> dict[str, Any] | None:
        return next((s for s in self.steps if s.get("call_id") == call_id), None)

    def agent_call_id(self, call_id: str) -> str | None:
        """Id of the AgentCall owning the LLM / tool call ``call_id`` (None if unknown)."""
        step = self.step_for(call_id)
        return (step.get("parent_id") or None) if step else None

    def agent_name(self, call_id: str) -> str | None:
        """Name of the agent that executed ``call_id`` (None if unknown)."""
        step = self.step_for(call_id)
        return (step.get("agent_id") or None) if step else None


class SymbolicStateElement(BaseModel):
    """Base of every symbolic state element extracted from a session (Claim, Fact, Goal, Task).

    What the store, the embeddings and the comparison need from an element is its ``id``:
    deterministic, so re-running an extraction yields the same ids.
    """

    id: str


ClaimForce = Literal["assertive", "committing", "directive"]
ClaimKind = Literal["factual", "evaluative", "recommendation"]
FactSourceType = Literal["tool_result"]  # the other mas:factSourceType values are not handled yet
GoalStatus = Literal["set", "decomposed", "achieved", "abandoned"]
TaskStatus = Literal["assigned", "accepted", "in_progress", "complete", "failed"]


def _clean_criteria(criteria: list[str]) -> list[str]:
    """Criteria without the blank ones, whitespace stripped."""
    return [c.strip() for c in criteria if c.strip()]


class ClaimItem(BaseModel):
    """A claim as returned by the LLM (mas:claimText / mas:claimForce / mas:claimKind).

    ``verbatim_source`` is not part of the ontology: an exact excerpt of the LLM call's
    output that expresses the claim, meant to be searched for in the inputs of other
    agents to follow the claim across the trajectory.

    ``tool_call_ids`` is not part of the ontology either: for a committing claim that announces tool
    calls the LLM call requested ("I will calculate the total time"), the ids those requests have in
    the output of the LLM call (the ids of the provider, ``call_...``). The extractor keeps only the
    ids that occur in that output; ``SymbolicExtractor`` turns them into ``announces`` relations to
    the ToolCall nodes. Empty for every other claim.
    """

    claim_text: str = Field(min_length=1)
    claim_force: ClaimForce
    claim_kind: ClaimKind | None = None
    verbatim_source: str | None = None
    tool_call_ids: list[str] = Field(default_factory=list)

    @field_validator("tool_call_ids", mode="before")
    @classmethod
    def _no_ids_as_empty(cls, value: Any) -> Any:
        # the LLM may answer null (or a single id) when the claim announces nothing
        if value is None:
            return []
        return [value] if isinstance(value, str) else value

    @model_validator(mode="after")
    def _check_kind_against_force(self) -> Self:
        # mas:ClaimKindShape: kind is required for assertive claims, absent for committing.
        if self.claim_force == "assertive" and self.claim_kind is None:
            raise ValueError("claim_kind is required when claim_force is 'assertive'")
        if self.claim_force == "committing":
            self.claim_kind = None
        else:
            # only a commitment announces a tool call
            self.tool_call_ids = []
        return self


class Claim(SymbolicStateElement, ClaimItem):
    """A claim anchored to the KG node it was extracted from (mas:Claim)."""

    #: Id of the LLMCall that generated the claim (mas:generatedBy).
    generated_by: str | None = None


class DelegationClaim(Claim):
    """A claim made by an agent in the brief it sent to another agent when it delegated work.

    The brief is generated by the LLM call of the caller (``generated_by``, as for any claim), but the
    trace does not record it there (the tool-call arguments are lost, and the output of the call is
    often only a reference): it survives only as the input of the AgentCall that received it, which
    is the node the claim is extracted from and where it is listed. ``verbatim_source`` is therefore
    an excerpt of that input, not of the output of ``generated_by``. No ontology class for it yet.
    """

    #: Name of the agent that wrote the brief.
    caller_agent: str = Field(min_length=1)
    #: Id of the AgentCall of the caller.
    caller_call: str
    #: Id of the AgentCall that received the brief (the node the claim was extracted from).
    delegated_to: str


class Fact(SymbolicStateElement):
    """A fact grounded in the output of a tool call (mas:Fact)."""

    #: mas:factText: the proposition as text (currently the whole tool output).
    fact_text: str = Field(min_length=1)
    #: mas:factSourceType.
    fact_source_type: FactSourceType = "tool_result"
    #: Exact text of the tool output, to be searched for in other calls' inputs.
    verbatim_source: str = Field(min_length=1)
    #: Id of the ToolCall the fact comes from (mas:groundedIn).
    grounded_in: str
    #: Name of the tool that produced the output (None when the ToolCall node does not say). Not in the
    #: ontology; context that tells what the output is about, e.g. which values a bare number results from.
    tool_name: str | None = None
    #: What the tool was called with (the keyword arguments; positional ones, if any, under ``"args"``).
    #: Empty when the tool took none or the node does not say. Not in the ontology.
    tool_arguments: dict[str, Any] = Field(default_factory=dict)


class GoalItem(BaseModel):
    """A goal as returned by the LLM (mas:goalText / mas:successCriteria).

    A desired end state with the criteria its combined outcome is judged against. It has no
    assignee and persists after the work is done.
    """

    #: mas:goalText: the desired end state.
    goal_text: str = Field(min_length=1)
    #: mas:successCriteria: what must hold for the goal to be met. At least one: without criteria
    #: the outcome cannot be checked yes/no.
    success_criteria: list[str]
    #: Free text for humans and judges only: why this is the goal / how the criteria were read.
    rationale: str | None = None
    #: True when the goal or its criteria are not stated in the text but deduced from it.
    inferred: bool = False
    status: GoalStatus = "set"

    @field_validator("success_criteria")
    @classmethod
    def _at_least_one_criterion(cls, value: list[str]) -> list[str]:
        value = _clean_criteria(value)
        if not value:
            raise ValueError("a goal needs at least one success criterion")
        return value


class Goal(SymbolicStateElement, GoalItem):
    """A goal extracted from a KG node (mas:Goal)."""


class TaskItem(BaseModel):
    """A task as returned by the LLM (mas:taskText / mas:acceptanceCriteria).

    A unit of work assigned to, or taken on by, an agent in service of a goal, with the
    deliverable expected from it.
    """

    #: mas:taskText: the work to be done and the deliverable expected.
    task_text: str = Field(min_length=1)
    #: mas:acceptanceCriteria: the local success definition; may be empty.
    acceptance_criteria: list[str] = Field(default_factory=list)
    #: True when the assignment is not stated in the input of the call but deduced from its context
    #: (the instruction is missing or the input is cut off), so the task is a best guess.
    inferred: bool = False
    status: TaskStatus = "assigned"

    @field_validator("acceptance_criteria")
    @classmethod
    def _drop_blank_criteria(cls, value: list[str]) -> list[str]:
        return _clean_criteria(value)


class Task(SymbolicStateElement, TaskItem):
    """A task owned by the AgentCall it was assigned to (mas:Task)."""

    #: Id of the AgentCall that owns the task (mas:ownedBy).
    owned_by: str


class SystemTaskItem(BaseModel):
    """A standing task as returned by the LLM: a duty the system prompt of an agent gives it every
    time it is called, with the deliverable expected. Unlike ``TaskItem`` there is no ``status`` (it
    is not assigned in one call) and no ``inferred`` (it is stated in the prompt)."""

    task_text: str = Field(min_length=1)
    #: Conditions the system prompt states for the deliverable; may be empty.
    acceptance_criteria: list[str] = Field(default_factory=list)

    @field_validator("acceptance_criteria")
    @classmethod
    def _drop_blank_criteria(cls, value: list[str]) -> list[str]:
        return _clean_criteria(value)


class SystemTask(SymbolicStateElement, SystemTaskItem):
    """A standing task of an agent, read from its system prompt. One set per agent and session, shared
    by every call of the agent (``has_system_task`` relation of the store). Not a ``Task``: it has
    no owner call, no status, and is not part of the task relations (``preceded_by``, ``implements``,
    ``achieves``, ``fulfils_task``). No ontology class for it yet."""

    #: Name of the agent whose system prompt holds the task.
    agent_id: str = Field(min_length=1)
    #: Id of the ``AgentSystemPrompt`` it was extracted from.
    system_prompt_id: str
    #: Id of the LLMCall the prompt was read from (the node the task is anchored to).
    read_from: str
    #: True when the system prompt was cut by the KG, so duties after the cut are missing.
    truncated: bool = False


class AgentSystemPrompt(SymbolicStateElement):
    """The system prompt of an agent, as it was sent to the LLM. One per agent and session.

    It is context for the other extractors (what an agent is for), not something an agent says or
    does. There is no ontology class for it yet (``mas:SystemPrompt`` does not exist), so it only
    lives in the store and in the output, anchored to the LLM call it was read from.
    """

    #: Name of the agent (``agent_id`` of the trajectory steps).
    agent_id: str = Field(min_length=1)
    #: The raw text, not summarised. Several system messages or parts are joined by a newline.
    system_prompt: str = Field(min_length=1)
    #: True when the KG cut the input in the middle of the system prompt (the SRE LLM inputs are
    #: capped at 4000 characters), so only its beginning is here.
    truncated: bool = False
    #: Id of the LLMCall whose input it was read from.
    read_from: str


class AgentCapabilityItem(BaseModel):
    """A capability as returned by the LLM: something an agent is able to do, according to its system
    prompt (the kind of work it is specialised in, or what its tools let it do)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    #: What the agent is able to do, as one short self-contained sentence.
    capability_text: str = Field(min_length=1)


class AgentCapability(SymbolicStateElement, AgentCapabilityItem):
    """A capability of an agent, read from its system prompt. One set per agent and session, shared by
    every call of the agent (``has_capability`` relation of the store, AgentCall -> AgentCapability), like the
    ``SystemTask``. It says what the agent can do, not what it is told to do (that is the ``SystemTask``)
    nor how it must behave (the system ``Constraint``). The system prompt is only an approximation of what
    the agent can really do (it may be cut, and says nothing of what the underlying model can do). No
    relation to the other elements and no ontology class yet."""

    #: Name of the agent whose system prompt holds the capability.
    agent_id: str = Field(min_length=1)
    #: Id of the ``AgentSystemPrompt`` it was extracted from.
    system_prompt_id: str
    #: Id of the LLMCall the prompt was read from (the node the capability is anchored to).
    read_from: str
    #: True when the system prompt was cut by the KG, so capabilities after the cut are missing.
    truncated: bool = False


ConstraintStrength = Literal["hard", "soft"]
ConstraintOrigin = Literal["user", "system"]


class ConstraintItem(BaseModel):
    """A constraint as returned by the LLM (mas:constraintText / mas:constraintStrength): something
    that forbids, requires or prefers something specific about the solution or about the behaviour
    of an agent."""

    #: mas:constraintText: what the constraint forbids, requires or prefers, as text.
    constraint_text: str = Field(min_length=1)
    #: mas:constraintStrength: ``hard`` must hold (a boundary or a rule), ``soft`` is a preference.
    constraint_strength: ConstraintStrength = "hard"


class Constraint(SymbolicStateElement, ConstraintItem):
    """A constraint (mas:Constraint). One class for both origins, told apart by ``origin``:

    * ``user``: stated by the user in the request of the session. Read from the ``Conversation`` node
      (``read_from``), linked to the goals of that node (``bounded_by``: Goal -> Constraint).
    * ``system``: written in the system prompt of an agent. Extracted once per agent, from the LLM call
      its prompt was read from; ``agent_id`` and ``system_prompt_id`` say which, and it is linked to the
      tasks of the agent calls of that agent (``bounded_by``: Task -> Constraint).
    """

    origin: ConstraintOrigin
    #: Id of the node it was read from: the Conversation node (user) or the LLMCall (system).
    read_from: str
    #: Name of the agent whose system prompt holds the constraint (system only).
    agent_id: str | None = None
    #: Id of the ``AgentSystemPrompt`` it was extracted from (system only).
    system_prompt_id: str | None = None
    #: True when the system prompt was cut by the KG, so constraints after the cut are missing.
    truncated: bool = False

    @model_validator(mode="after")
    def _a_system_constraint_has_an_agent(self) -> Self:
        if self.origin == "system" and not self.agent_id:
            raise ValueError("a system constraint needs the agent whose system prompt holds it")
        return self


class AnnotatedNode(KGNode):
    """An original KG node plus the symbolic state elements extracted from it.

    ``symbolic_state_elements`` is keyed by element kind (``"claims"``, ``"facts"``, ``"goals"``,
    ``"tasks"``, ``"system_prompts"``, ``"system_tasks"``, ``"constraints"``, ``"capabilities"``,
    ``"delegation_claims"``) and only holds the kinds whose extractor ran on this node.
    ``llm_usage`` has the tokens / cost spent on this node, under the same keys.
    ``input_claims`` (LLMCall only) lists the ids of the already extracted claims present in
    the call's input; None when it could not be computed (no trajectory). It also lists the delegation
    claims of the brief received by the agent call of an LLM call that generated claims (that brief is in
    its context even when its text is cut from the recorded input). The same goes for the claims that
    travel back to an agent that delegated: the claims of the delegated agent call found in its reply
    (see ``reply`` below) are listed in the ``input_claims`` of the next LLM call of the caller that
    generated claims.
    ``input_facts`` is the same for facts (tool results of the call's own agent call).
    ``system_tasks`` (AgentCall only) lists the ids of the standing tasks of the agent that made the
    call (``SystemTask``, read from its system prompt); None when not computed.
    ``capabilities`` (AgentCall only) lists the ids of the capabilities of that agent (``AgentCapability``, read from
    its system prompt); None when not computed.
    ``source_claims`` (AgentCall only, one that has delegation claims) lists the ids of the claims those
    delegation claims could restate: the claims its caller generated, the ones in the caller's own
    brief, and the other claims generated by LLM calls before the delegation (see ``source_claims`` in
    ``extractors/delegation_claims.py``). Not checked to be present in the brief; None otherwise.
    Each claim of an LLM call of an agent call that was delegated to has a ``reply`` flag (a key of the
    claim dict): True when it occurs in the output of that agent call, the reply its caller gets back,
    False when it stays inside. The claims of other calls have no such key.
    """

    symbolic_state_elements: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    llm_usage: dict[str, LLMUsage] = Field(default_factory=dict)
    input_claims: list[str] | None = None
    input_facts: list[str] | None = None
    system_tasks: list[str] | None = None
    capabilities: list[str] | None = None
    source_claims: list[str] | None = None


class ExtractionResult(BaseModel):
    """Outcome of one extraction run (currently written to a local JSON file)."""

    session_id: str
    session: dict[str, Any] = Field(default_factory=dict)
    nodes: list[AnnotatedNode] = Field(default_factory=list)
    llm_usage: LLMUsage = Field(default_factory=LLMUsage)
    llm_usage_by_element: dict[str, LLMUsage] = Field(default_factory=dict)
    #: Agent-call transitions and per-agent-call claim states (None without a trajectory).
    symbolic_state: SymbolicStateTrace | None = None
    #: The relations between ids (``supports``, ``bounded_by``, ``produced_by``, ...), as edges. ``process``: all
    #: of the store. ``live_process``: the edges recorded while processing that node, which can join elements of
    #: nodes returned earlier (a late link): a consumer appends them to the ones it already has.
    edges: list[Edge] = Field(default_factory=list)
    #: ``live_process`` only: nodes returned by an earlier call whose annotations changed because of the
    #: edges of this call (the capabilities of an agent arrive after its first AgentCall node). They are the
    #: objects already returned, modified in place and listed here so a consumer knows to read them again.
    updated_nodes: list[AnnotatedNode] = Field(default_factory=list)
    #: The verdicts of the LLM judgements, with their reasoning, for debugging: ``goals`` (do the claims of the
    #: final answer satisfy the goal), ``tasks`` (do the claims of an LLM call fulfil its task) and
    #: ``capabilities`` (which capabilities support a task). ``process``: all of them. ``live_process``: the ones
    #: made in that step (a consumer appends them).
    evaluations: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
