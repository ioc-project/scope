from helpers import call, claim, constraint, edge, ids, run

from failure_detection import Delta

# -- CR-1 ------------------------------------------------------------------------------------------


def conflicting(agent_b):
    return Delta(
        calls=[call("A", "agent", agent="a"), call("B", "agent", agent=agent_b)],
        constraints=[constraint("k1"), constraint("k2")],
        edges=[
            edge("k1", "contradicts", "k2"),
            edge("k1", "providedTo", "A"),
            edge("k2", "providedTo", "B"),
        ],
    )


def test_contradicting_constraints_given_to_different_agents():
    assert ids(run(conflicting("b")), rule="CR-1") == [("k1", "k2")]


def test_contradicting_constraints_given_to_the_same_agent_are_not_cr1():
    assert ids(run(conflicting("a")), rule="CR-1") == []


# -- IR-1 ------------------------------------------------------------------------------------------


def dropped(second_input=(), contradicting=False):
    d = Delta(
        calls=[
            call("A", "agent", agent="a"),
            call("L1", parent="A", start=1, input_constraints=("k",)),
            call("L2", parent="A", start=2, input_constraints=second_input),
        ],
        constraints=[constraint("k")],
        claims=[claim("c", "L2")],
        edges=[edge("k", "providedTo", "A")],
    )
    if contradicting:
        d.edges.append(edge("c", "contradicts", "k"))
    return d


def test_constraint_in_the_first_llm_call_but_not_a_later_one():
    f = run(dropped())
    assert ids(f, "risk", "IR-1") == [("k", "L2")]
    assert ids(f, rule="IR-1") == []


def test_claim_of_that_later_call_breaking_the_constraint():
    assert ids(run(dropped(contradicting=True)), rule="IR-1") == [("c", "k")]


def test_constraint_kept_in_every_call_is_fine():
    assert ids(run(dropped(second_input=("k",))), "risk", "IR-1") == []
