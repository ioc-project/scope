import json

from symbolic_extractor import SymbolicExtractor, SymbolicSessionStore
from symbolic_extractor.extractors import Fact, ToolResultFactExtractor
from symbolic_extractor.models import SessionData, SessionTrajectory
from symbolic_extractor.relations.argument_fact_links import ArgumentFactLinker, string_arguments
from symbolic_extractor.relations.fact_claim_links import SUPPORTED_BY


def link_argument_facts(store, trajectory, tool_call_id=None):
    """Run the linker on the facts of the store (or only those of one tool call); returns the links."""
    facts = [s.element for s in store.by_kind("facts") if tool_call_id in (None, s.node_id)]
    return len(ArgumentFactLinker().extract(store, facts, trajectory))


def _tool(call_id, owner, start, duration_ms=100.0):
    return {
        "call_id": call_id,
        "kind": "tool",
        "parent_id": owner,
        "start_time": start,
        "duration_ms": duration_ms,
    }


def _llm(call_id, owner, start, duration_ms=100.0):
    return {
        "call_id": call_id,
        "kind": "llm",
        "parent_id": owner,
        "start_time": start,
        "duration_ms": duration_ms,
    }


def _store(**facts):
    """facts: tool_call_id=(output, arguments)."""
    store = SymbolicSessionStore("s")
    for tool_id, (output, arguments) in facts.items():
        store.add(
            "facts",
            Fact(
                id=f"f-{tool_id}",
                fact_text=output,
                verbatim_source=output,
                grounded_in=tool_id,
                tool_arguments=arguments,
            ),
            tool_id,
        )
    return store


def _telemetry():
    """list_services, then three parallel calls (requested by llm2), then two more (requested by llm3)."""
    steps = [
        _llm("llm1", "a1", 0.0, 100),
        _tool("list", "a1", 0.2, 50),  # ends 0.25
        _llm("llm2", "a1", 0.3, 1000),  # ends 1.3
        _tool("health", "a1", 1.31, 100),  # ends 1.41
        _tool("metrics", "a1", 1.31, 100),
        _llm("llm3", "a1", 1.5, 1000),  # ends 2.5
        _tool("cache", "a1", 2.51, 100),
        _tool("metrics2", "a1", 2.51, 100),
    ]
    store = _store(
        list=(
            '{"services": [{"name": "profile-service"}, {"name": "profile-cache"}]}',
            {"include_health": True},
        ),
        health=(
            '{"service": "profile-service", "dependencies": {"profile-cache": "healthy"}}',
            {"service": "profile-service", "include_dependencies": True},
        ),
        metrics=(
            '{"service": "profile-service", "window": "15m"}',
            {"service": "profile-service", "window": "15m"},
        ),
        cache=(
            '{"service": "profile-cache", "window": "15m"}',
            {"service": "profile-cache", "window": "15m"},
        ),
        metrics2=(
            '{"service": "profile-service", "window": "15m"}',
            {"service": "profile-service", "window": "15m"},
        ),
    )
    return store, SessionTrajectory(session_id="s", steps=steps)


def _edges(store):
    """(tool call id of the fact that is supported, source fact id) -> arguments."""
    return {
        (e.source_id.removeprefix("f-"), e.target_id): e.attributes["arguments"]
        for e in store.edges()
        if e.edge_type == SUPPORTED_BY
    }


def test_string_arguments_keeps_only_searchable_strings():
    args = {
        "service": " profile-cache ",
        "window": "15m",
        "limit": 50,
        "flag": True,
        "level": "E",
        "n": {"a": "bbbb"},
    }
    assert string_arguments(args) == {"service": "profile-cache", "window": "15m"}
    assert string_arguments(None) == {}


def test_value_is_linked_to_the_earliest_result_that_holds_it():
    store, trajectory = _telemetry()
    link_argument_facts(store, trajectory)
    edges = _edges(store)
    # profile-service comes from list_services, not from the later results that repeat it
    assert edges[("health", "f-list")] == {"service": "profile-service"}
    # profile-cache is in list_services and in the dependencies of health: the earliest wins
    assert edges[("cache", "f-list")] == {"service": "profile-cache"}
    assert ("cache", "f-health") not in edges


def test_results_that_echo_the_value_are_not_sources():
    store, trajectory = _telemetry()
    link_argument_facts(store, trajectory)
    edges = _edges(store)
    # window=15m is only in the results of calls made with window=15m
    assert all("window" not in arguments for arguments in edges.values())
    assert ("metrics2", "f-metrics") not in edges and ("metrics2", "f-cache") not in edges


def test_parallel_and_later_results_were_not_visible_to_the_requesting_call():
    store, trajectory = _telemetry()
    link_argument_facts(store, trajectory)
    edges = _edges(store)
    # health and metrics ran in parallel (requested by llm2): neither saw the other's result
    assert not [
        k for k in edges if k[0] in ("health", "metrics") and k[1] in ("f-health", "f-metrics")
    ]
    # list_services has nothing before it
    assert not [k for k in edges if k[0] == "list"]


def test_results_of_another_agent_call_are_not_sources():
    steps = [_tool("t1", "a1", 0.0), _llm("l", "a2", 0.2), _tool("t2", "a2", 0.4)]
    store = _store(t1=("profile-cache", {}), t2=("x", {"service": "profile-cache"}))
    link_argument_facts(store, SessionTrajectory(session_id="s", steps=steps))
    assert not store.edges()


def test_without_an_llm_call_the_results_finished_before_the_tool_call_are_visible():
    steps = [_tool("t1", "a1", 0.0, 100), _tool("t2", "a1", 0.5)]
    store = _store(t1=("profile-cache", {}), t2=("x", {"service": "profile-cache"}))
    link_argument_facts(store, SessionTrajectory(session_id="s", steps=steps))
    assert _edges(store) == {("t2", "f-t1"): {"service": "profile-cache"}}


def test_one_source_for_several_arguments_and_one_tool_call_only():
    steps = [_tool("t1", "a1", 0.0), _tool("t2", "a1", 1.0), _tool("t3", "a1", 2.0)]
    store = _store(
        t1=("alpha-1 beta-2", {}),
        t2=("y", {"a": "alpha-1", "b": "beta-2"}),
        t3=("z", {"a": "alpha-1"}),
    )
    trajectory = SessionTrajectory(session_id="s", steps=steps)
    assert link_argument_facts(store, trajectory, "t2") == 1
    assert _edges(store) == {("t2", "f-t1"): {"a": "alpha-1", "b": "beta-2"}}
    link_argument_facts(store, trajectory)  # idempotent, now t3 too
    link_argument_facts(store, trajectory)
    assert len(_edges(store)) == 2


def test_no_trajectory_no_link():
    assert link_argument_facts(_store(), None) == 0


def test_pipeline_records_the_edges():
    outputs = {"t1": json.dumps("service profile-cache is up"), "t2": json.dumps("ok")}
    data = SessionData(
        session_id="s1",
        tool_spans=[
            {
                "id": "t1",
                "startTime": 0.0,
                "outputContent": outputs["t1"],
                "toolArguments": json.dumps({"x": 1}),
            },
            {
                "id": "t2",
                "startTime": 1.0,
                "outputContent": outputs["t2"],
                "toolArguments": json.dumps({"service": "profile-cache"}),
            },
        ],
    )
    trajectory = SessionTrajectory(
        session_id="s1", steps=[_tool("t1", "a1", 0.0), _tool("t2", "a1", 1.0)]
    )
    pipeline = SymbolicExtractor(
        extractors=[ToolResultFactExtractor()], relation_extractors=[ArgumentFactLinker()]
    )
    result = pipeline.process(data, trajectory=trajectory)
    links = [e for e in result.edges if e.edge_type == SUPPORTED_BY]
    (fact_t1,) = pipeline.store.for_node("t1", "facts")
    (fact_t2,) = pipeline.store.for_node("t2", "facts")
    assert [(e.source_id, e.target_id, e.attributes) for e in links] == [
        (fact_t2.id, fact_t1.id, {"arguments": {"service": "profile-cache"}})
    ]


def test_pipeline_without_the_linker_records_nothing():
    data = SessionData(
        session_id="s1",
        tool_spans=[
            {
                "id": "t1",
                "startTime": 0.0,
                "outputContent": json.dumps("profile-cache"),
                "toolArguments": "{}",
            },
            {
                "id": "t2",
                "startTime": 1.0,
                "outputContent": json.dumps("ok"),
                "toolArguments": json.dumps({"service": "profile-cache"}),
            },
        ],
    )
    trajectory = SessionTrajectory(
        session_id="s1", steps=[_tool("t1", "a1", 0.0), _tool("t2", "a1", 1.0)]
    )
    result = SymbolicExtractor(extractors=[ToolResultFactExtractor()]).process(
        data, trajectory=trajectory
    )
    assert not [e for e in result.edges if e.edge_type == SUPPORTED_BY]


def test_live_process_links_when_the_tool_call_arrives():
    from symbolic_extractor.models import ExtractionRequest, KGNode

    trajectory = SessionTrajectory(
        session_id="s1", steps=[_tool("t1", "a1", 0.0), _tool("t2", "a1", 1.0)]
    )
    pipeline = SymbolicExtractor(
        extractors=[ToolResultFactExtractor()], relation_extractors=[ArgumentFactLinker()]
    )
    request = ExtractionRequest(session_id="s1")
    nodes = [
        KGNode(
            node_id="t1",
            node_type="ToolCall",
            node={
                "id": "t1",
                "startTime": 0.0,
                "outputContent": json.dumps("profile-cache"),
                "toolArguments": "{}",
            },
        ),
        KGNode(
            node_id="t2",
            node_type="ToolCall",
            node={
                "id": "t2",
                "startTime": 1.0,
                "outputContent": json.dumps("ok"),
                "toolArguments": json.dumps({"service": "profile-cache"}),
            },
        ),
    ]
    first = pipeline.live_process(nodes[0], request, trajectory)
    assert not [e for e in first.edges if e.edge_type == SUPPORTED_BY]
    second = pipeline.live_process(nodes[1], request, trajectory)
    (fact_t2,) = pipeline.store.for_node("t2", "facts")
    assert [
        (e.source_id, e.attributes["arguments"])
        for e in second.edges
        if e.edge_type == SUPPORTED_BY
    ] == [(fact_t2.id, {"service": "profile-cache"})]
