from helpers import call, claim, edge, ids, run, transition

from failure_detection import Delta


def two_agents(receiver_input=()):
    return Delta(
        calls=[
            call("P", "agent", agent="p", state_claims=("x",)),
            call("S", "agent", agent="s", start=1),
            call("LS", parent="S", agent="s", start=1, input_claims=receiver_input),
        ],
        claims=[claim("x", "P"), claim("y", "LS")],  # y is the final answer
        transitions=[transition("P", "S")],
    )


def test_dr2_claim_of_the_sender_not_passed_on():
    assert ids(run(two_agents()), rule="DR-2 dropped") == [("x", "S")]
    assert ids(run(two_agents(receiver_input=("x",))), rule="DR-2 dropped") == []


def restated(before, after):
    return Delta(
        calls=[call("A", end=1), call("B", end=2)],
        claims=[claim("a", "A", text=before), claim("b", "B", text=after)],
        edges=[edge("b", "sameClaims", "a")],
    )


def test_dr2_precision_lost_by_rounding():
    f = run(restated("The fare is $45.20", "The fare is $45"))
    assert ids(f, rule="DR-2 precision") == [("a", "b")]


def test_dr2_same_value_or_other_number_is_not_rounding():
    assert ids(run(restated("It costs $6.00", "It costs $6")), rule="DR-2 precision") == []
    assert (
        ids(run(restated("It takes 3 hours", "It arrives at 09:15")), rule="DR-2 precision") == []
    )


def dilution(conflict=True, new_fact=False):
    calls = [
        call("H", "agent", agent="mod", start=0),
        call("W1", "agent", agent="a", start=1, end=2, state_claims=("r",)),
        call("W2", "agent", agent="b", start=3, end=4, state_claims=("r",)),
        call("L1", parent="W1", agent="a", start=1, end=2, input_claims=("r",)),
        call("L2", parent="W2", agent="b", start=3, end=4, input_claims=("r",)),
        call("LH", parent="H", agent="mod", start=0, end=0.5),
    ]
    claims = [claim("r", "LH"), claim("m1", "L1"), claim("m2", "L2")]
    edges = [edge("m1", "sameAs", "r"), edge("m2", "sameAs", "r")]
    if conflict:
        claims += [claim("u1", "LH"), claim("u2", "LH")]
        edges.append(edge("u1", "contradicts", "u2"))
    if new_fact:
        calls.append(call("T", "tool", parent="W1", start=1.5, end=1.6))
    d = Delta(calls=calls, claims=claims, edges=edges)
    d.transitions = [transition("H", "W1"), transition("H", "W2")]
    return d


def test_dc1_successive_restating_handoffs_with_an_unresolved_conflict():
    # every claim of each worker restates r, and the hub's two claims u1, u2 contradict
    assert ids(run(dilution()), rule="DC-1 restating") == [("W1", "W2")]


def test_dc1_not_without_a_conflict():
    assert ids(run(dilution(conflict=False)), rule="DC-1 restating") == []


def test_dc1_multicast_claim_missing_for_one_target():
    d = dilution(conflict=False)
    d.calls[2] = call("W2", "agent", agent="b", start=3, end=4, state_claims=("r",))
    d.calls[4] = call("L2", parent="W2", agent="b", start=3, end=4, input_claims=())
    assert ids(run(d), rule="DC-1 multicast") == [("r", "W2")]


def test_dr2_dropped_claim_is_fine_when_it_reaches_the_final_answer():
    d = two_agents()
    d.edges.append(edge("y", "derivedFrom", "x"))
    assert ids(run(d), rule="DR-2 dropped") == []


def test_dr2_ignores_claims_that_state_nothing():
    d = two_agents()
    d.claims[0] = d.claims[0].model_copy(update={"kind": "evaluative"})
    assert ids(run(d), rule="DR-2 dropped") == []
