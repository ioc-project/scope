from helpers import call, claim, edge, fact, ids, run, transition

from failure_detection import Delta

# A hub "mod" routes to two workers a and b; each has one LLM call.
HUB = [
    call("H", "agent", agent="mod", start=0, end=10),
    call("W1", "agent", agent="a", start=1, end=3),
    call("W2", "agent", agent="b", start=1, end=4),
    call("L1", parent="W1", agent="a", start=1, end=2),
    call("L2", parent="W2", agent="b", start=1, end=3),
]
ROUTES = [transition("H", "W1"), transition("H", "W2")]


def hub(final_agent_call="LH", final_agent="mod", **extra):
    """Claims a (worker a) and b (worker b) contradict; the final answer derives from a."""
    calls = [*HUB, call(final_agent_call, final=True, agent=final_agent, start=5, end=6)]
    return Delta(
        calls=calls,
        claims=[claim("a", "L1"), claim("b", "L2"), claim("h", final_agent_call)],
        edges=[edge("a", "contradicts", "b"), edge("h", "derivedFrom", "a")],
        transitions=ROUTES,
        **extra,
    )


def test_cc3_hub_picks_a_side():
    assert ids(run(hub()), rule="CC-3") == [("a", "b")]


def test_cc3_not_when_the_hub_reconciles():
    d = hub()
    d.edges.append(edge("h", "derivedFrom", "b"))
    assert ids(run(d), rule="CC-3") == []


def test_dc3_peers_disagree_and_nobody_commits():
    d = Delta(
        calls=HUB[1:],
        claims=[claim("a", "L1"), claim("b", "L2")],
        edges=[edge("a", "contradicts", "b")],
        transitions=[transition("W1", "W2", "handoff")],
    )
    assert ids(run(d), rule="DC-3") == [("a", "b")]
    d.claims.append(claim("k", "L1", force="committing"))  # a commits after considering b
    d.edges.append(edge("k", "derivedFrom", "b"))
    assert ids(run(d), rule="DC-3") == []


def query_graph(**kw):
    """The final answer (agent "c", not the hub) takes claim a, which has no fact; b has one."""
    d = hub("LF", "c")
    d.facts.append(fact("f", "T"))
    d.edges += [
        edge("b", "supportedBy", "f"),
        edge("a", "producedBy", "t"),
        edge("b", "producedBy", "t"),
    ]
    return d


def test_de1_cm_less_supported_answer_adopted_by_a_non_hub():
    assert ids(run(query_graph()), rule="DE-1-CM") == [("a", "b")]


def test_de1_cm_not_when_the_adopted_side_is_supported():
    d = query_graph()
    d.edges.append(edge("a", "supportedBy", "f"))
    assert ids(run(d), rule="DE-1-CM") == []


def test_cs1_dissent_minority_dropped_unevaluated():
    d = hub()
    d.claims.append(claim("a2", "L2"))  # another agent says what a says
    d.edges.append(edge("a2", "sameAs", "a"))
    d.calls = [c for c in d.calls]
    assert ids(run(d), rule="CS-1 dissent") == [("a", "b")]
    d.edges.append(edge("x", "derivedFrom", "b"))  # someone evaluated b
    assert ids(run(d), rule="CS-1 dissent") == []


# -- CC-4, IC-1, IC-2 ----------------------------------------------------------------------


def stale_graph(in_input=False):
    return Delta(
        calls=[
            call("A", agent="a", start=1, end=2),
            call("B", agent="b", start=1, end=3),
            call("L", agent="a", start=5, end=6, input_claims=("x", "b") if in_input else ("x",)),
        ],
        claims=[claim("x", "A"), claim("b", "B")],
        edges=[edge("b", "contradicts", "x")],
    )


def test_cc4_call_never_received_the_update():
    assert ids(run(stale_graph()), rule="CC-4") == [("b", "x")]
    assert ids(run(stale_graph(in_input=True)), rule="CC-4") == []


def plan_graph(**kw):
    """Agent a relies on x (via commitment p), gets fact f against x, and still uses x."""
    return Delta(
        calls=[
            call("L0", agent="a", start=0, end=1),
            call("L1", agent="a", start=2, end=3, input_facts=("f",), **kw),
            call("L2", agent="a", start=4, end=5),
        ],
        claims=[claim("x", "L0"), claim("p", "L0", force="committing"), claim("c", "L2")],
        facts=[fact("f", "T")],
        edges=[
            edge("p", "derivedFrom", "x"),
            edge("c", "derivedFrom", "x"),
            edge("f", "contradicts", "x"),
        ],
    )


def test_ic2_plan_kept_after_contradicting_input():
    assert ids(run(plan_graph()), rule="IC-2") == [("f", "x", "c")]


def commitment_graph(second_facts=("f",)):
    return Delta(
        calls=[
            call("A", agent="a", start=0, end=1, input_facts=("f",)),
            call("B", agent="a", start=2, end=3, input_facts=second_facts),
        ],
        claims=[claim("k", "A", force="committing"), claim("c", "B")],
        edges=[edge("k", "contradicts", "c")],
    )


def test_ic1_agent_contradicts_its_own_commitment():
    assert ids(run(commitment_graph()), rule="IC-1") == [("k", "c")]


def test_ic1_not_when_a_new_fact_arrived():
    assert ids(run(commitment_graph(second_facts=("f", "g"))), rule="IC-1") == []
