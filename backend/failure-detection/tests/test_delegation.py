from helpers import call, claim, constraint, edge, goal, ids, run, task

from failure_detection import Delta


def agents():
    """A moderator call M that routed work to a worker call W (an LLM call L inside W)."""
    return [
        call("M", "agent", agent="mod"),
        call("W", "agent", agent="worker", routed_by="M"),
        call("L", parent="W", agent="worker"),
    ]


# -- CC-1 overlap ----------------------------------------------------------------------------------


def test_same_task_given_to_two_agent_calls():
    d = Delta(
        calls=agents(),
        tasks=[task("t1", "M"), task("t2", "W"), task("t3", "W")],
        edges=[edge("t1", "sameAs", "t2"), edge("t2", "sameAs", "t3")],
    )
    assert ids(run(d), "risk", "CC-1 overlap") == [("t1", "t2", "t3")]


def test_same_task_in_one_agent_call_or_standing_is_not_overlap():
    d = Delta(
        calls=agents(),
        tasks=[task("t1", "W"), task("t2", "W"), task("s1", standing=True)],
        edges=[edge("t1", "sameAs", "t2"), edge("t1", "sameAs", "s1")],
    )
    assert ids(run(d), "risk", "CC-1 overlap") == []


def overlap_claims(**kw):
    calls = [call("A", "llm", agent="a", **kw), call("B", "llm", agent="b")]
    return Delta(
        calls=calls,
        claims=[claim("x", "A"), claim("y", "B")],
        edges=[edge("x", "sameClaims", "y")],
    )


def test_two_agents_make_the_same_claim_independently():
    assert ids(run(overlap_claims()), rule="CC-1 overlap") == [("x", "y")]


def test_a_claim_the_other_agent_had_seen_is_not_redundant():
    assert ids(run(overlap_claims(input_claims=("y",))), rule="CC-1 overlap") == []


def test_a_claim_derived_from_the_other_is_not_redundant():
    d = overlap_claims()
    d.edges.append(edge("x", "derivedFrom", "y"))
    assert ids(run(d), rule="CC-1 overlap") == []


# -- CC-1 gap --------------------------------------------------------------------------------------


def test_goal_that_no_task_achieves():
    assert ids(run(Delta(goals=[goal("g")])), "risk", "CC-1 gap") == [("g",)]


def test_first_task_that_serves_no_goal():
    d = Delta(
        goals=[goal("g")],
        tasks=[task("t1", "M"), task("t2", "M")],
        edges=[edge("t2", "achieves", "g"), edge("t2", "precededBy", "t1")],
    )
    assert ids(run(d), "risk", "CC-1 gap") == [("t1",)]


def test_goal_achieved_and_task_following_another_are_fine():
    d = Delta(
        goals=[goal("g")],
        tasks=[task("t1", "M"), task("t2", "M")],
        edges=[edge("t1", "achieves", "g"), edge("t2", "precededBy", "t1")],
    )
    assert ids(run(d), "risk", "CC-1 gap") == []


# -- DC-4 ---------------------------------------------------------------------------------------


def test_task_without_acceptance_criteria():
    d = Delta(
        tasks=[task("t1", "W", criteria=()), task("t2", "W"), task("s", criteria=(), standing=True)]
    )
    assert ids(run(d), "risk", "DC-4 AC") == [("t1",)]


def delegated(provided_to, strength="hard", bound_to="goal"):
    return Delta(
        calls=agents(),
        goals=[goal("g")],
        tasks=[task("t1", "W")],
        constraints=[constraint("k", strength)],
        edges=[
            edge("t1", "achieves", "g"),
            edge("g" if bound_to == "goal" else "t1", "boundedBy", "k"),
            *[edge("k", "providedTo", c) for c in provided_to],
        ],
    )


def test_hard_constraint_known_to_the_delegator_but_not_the_worker():
    assert ids(run(delegated(["M"])), "risk", "DC-4 CO") == [("t1", "k", "W")]
    assert ids(run(delegated(["M"], bound_to="task")), "risk", "DC-4 CO") == [("t1", "k", "W")]


def test_constraint_passed_on_or_soft_is_fine():
    assert ids(run(delegated(["M", "W"])), "risk", "DC-4 CO") == []
    assert ids(run(delegated(["M"], strength="soft")), "risk", "DC-4 CO") == []


def test_worker_claim_breaking_a_hard_constraint():
    d = delegated(["M"])
    d.claims.append(claim("c", "L"))
    d.edges.append(edge("c", "contradicts", "k"))
    assert ids(run(d), rule="DC-4 CO") == [("c", "k")]


def test_a_claim_of_another_agent_call_is_not_blamed_on_the_worker():
    d = delegated(["M"])
    d.calls.append(call("L2", parent="M", agent="mod"))
    d.claims.append(claim("c", "L2"))
    d.edges.append(edge("c", "contradicts", "k"))
    assert ids(run(d), rule="DC-4 CO") == []
