from helpers import call, claim, edge, fact, ids, run

from failure_detection import Delta, Engine

MISSING = "CE-1 missing evidence"
CONTRADICTED = "CE-1 contradicted by evidence"
UNRECONCILED = "CE-1 unreconciled contradiction"


def graph(claims, facts=(), edges=(), extra_calls=()):
    """Final call "L" at t=10, a tool call "T" at t=1 (before the claims), plus extras."""
    calls = [call("L", end=10, final=True), call("T", "tool", end=1), *extra_calls]
    return Delta(calls=calls, claims=list(claims), facts=list(facts), edges=list(edges))


# -- missing evidence ----------------------------------------------------------------------------


def test_final_claim_without_fact_is_flagged():
    f = run(graph([claim("c1", "L")]))
    assert ids(f, rule=MISSING) == [("c1", "c1")]
    assert ids(f, "risk", MISSING) == [("c1", "c1")]


def test_claim_derived_from_a_fact_is_supported():
    f = run(graph([claim("c1", "L")], [fact("f1", "T")], [edge("c1", "derivedFrom", "f1")]))
    assert ids(f, rule=MISSING) == []


def test_chain_is_followed_and_the_unsupported_leaf_reported():
    d = graph([claim("c1", "L"), claim("c2", "L")], edges=[edge("c2", "derivedFrom", "c1")])
    assert ids(run(d), rule=MISSING) == [("c1", "c1"), ("c2", "c1")]


def test_chain_ending_on_a_fact_is_supported():
    d = graph(
        [claim("c1", "L"), claim("c2", "L")],
        [fact("f1", "T")],
        [edge("c2", "derivedFrom", "c1"), edge("c1", "supportedBy", "f1")],
    )
    assert ids(run(d), rule=MISSING) == []


def test_restated_claim_inherits_support():
    d = graph(
        [claim("c1", "L"), claim("c2", "L")],
        [fact("f1", "T")],
        [edge("c1", "supportedBy", "f1"), edge("c2", "sameAs", "c1")],
    )
    assert ids(run(d), rule=MISSING) == []


def test_claims_restating_each_other_with_no_fact_are_unsupported():
    d = graph(
        [claim("c1", "L"), claim("c2", "E")],
        extra_calls=[call("E", end=5)],
        edges=[edge("c1", "sameAs", "c2")],
    )
    assert ids(run(d), rule=MISSING) == [
        ("c1", "c2")
    ]  # the earliest claim of the group is the leaf


def test_restating_claims_share_a_fact_found_on_either():
    d = graph(
        [claim("c1", "L"), claim("c2", "E")],
        [fact("f1", "T")],
        extra_calls=[call("E", end=5)],
        edges=[edge("c1", "sameAs", "c2"), edge("c2", "supportedBy", "f1")],
    )
    assert ids(run(d), rule=MISSING) == []


def test_committing_claim_needs_no_fact():
    assert ids(run(graph([claim("c1", "L", force="committing")])), rule=MISSING) == []


def test_non_final_claims_only_raise_a_risk():
    d = graph(
        [claim("c1", "E"), claim("c2", "L", force="committing")], extra_calls=[call("E", end=5)]
    )
    f = run(d)
    assert ids(f, rule=MISSING) == []
    assert ids(f, "risk", MISSING) == [("c1", "c1")]
    assert ids(f, rule="IE-1 missing evidence") == [("c1", "c1")]


# -- contradicted by evidence --------------------------------------------------------------------


def test_fact_contradicting_a_final_claim():
    d = graph([claim("c1", "L")], [fact("f1", "T")], [edge("f1", "contradicts", "c1")])
    assert ids(run(d), rule=CONTRADICTED) == [("c1", "f1")]


# -- unreconciled contradiction ------------------------------------------------------------------


def conflict(extra=()):
    return graph(
        [claim("a", "E1"), claim("b", "E2"), claim("fin", "L")],
        [fact("f1", "T")],
        [edge("a", "contradicts", "b"), edge("fin", "derivedFrom", "a"), *extra],
        extra_calls=[call("E1", end=2), call("E2", end=3)],
    )


def test_final_answer_rests_on_one_side_of_a_conflict():
    f = run(conflict())
    assert ids(f, rule=UNRECONCILED) == [("a", "b")]
    assert ids(f, "risk", UNRECONCILED) == [("a", "b")]


def test_conflict_acknowledged_by_a_later_claim_is_not_reported():
    d = conflict([edge("fin", "derivedFrom", "b")])  # fin is derived from both sides
    f = run(d)
    assert ids(f, rule=UNRECONCILED) == [] and ids(f, "risk", UNRECONCILED) == []


# -- the pipe ------------------------------------------------------------------------------------


def test_same_delta_twice_reports_nothing_new():
    engine, d = Engine(), graph([claim("c1", "L")])
    engine.process(d)
    assert engine.finalize()
    assert engine.process(d) == [] and engine.finalize() == []


def test_increments_give_the_same_findings_as_the_whole_graph():
    whole = conflict()
    pieces = [
        Delta(calls=whole.calls),
        Delta(claims=whole.claims[:1]),
        Delta(claims=whole.claims[1:]),
        Delta(facts=whole.facts),
        Delta(edges=whole.edges),
    ]
    engine = Engine()
    inc = [f for p in pieces for f in engine.process(p)] + engine.finalize()
    full = run(whole)
    assert {f.key for f in inc if f.level == "detect"} == {
        f.key for f in full if f.level == "detect"
    }


SEEN = "CE-1 revised without evidence"


def with_input(d, claims):
    d.calls = [c if c.id != "E2" else call("E2", end=3, input_claims=claims) for c in d.calls]
    return d


def test_conflict_the_later_call_saw_is_a_low_severity_risk_only():
    f = run(with_input(conflict(), ("a",)))
    assert ids(f, rule=UNRECONCILED) == [] and ids(f, "risk", UNRECONCILED) == []
    assert ids(f, "risk", SEEN) == [("a", "b")]
    assert ids(f, "detect", SEEN) == []


def test_conflict_the_later_call_never_saw_is_still_a_detect():
    f = run(with_input(conflict(), ()))
    assert ids(f, rule=UNRECONCILED) == [("a", "b")]
    assert ids(f, "risk", SEEN) == []


def test_later_claim_derived_from_the_earlier_is_acknowledged():
    d = with_input(conflict(), ("a",))
    d.edges.append(edge("b", "derivedFrom", "a"))
    f = run(d)
    assert ids(f, "risk", SEEN) == [] and ids(f, rule=UNRECONCILED) == []


def seen_with(backed):
    """Conflict a (earlier) / b (later), seen by the later call; `backed` get a fact."""
    d = with_input(conflict(), ("a",))
    d.edges += [edge(c, "supportedBy", "f1") for c in backed]
    return run(d)


def test_correction_only_the_later_claim_has_evidence():
    f = seen_with(["b"])
    assert ids(f, "risk", "CE-1 corrected by evidence") == [("a", "b")]
    assert ids(f, "risk", SEEN) == []
    assert ids(f, "detect", "CE-1 overrode evidence") == []


def test_override_only_the_earlier_claim_has_evidence_is_a_detect():
    f = seen_with(["a"])
    assert ids(f, "detect", "CE-1 overrode evidence") == [("a", "b")]
    assert ids(f, "risk", SEEN) == []
    assert ids(f, "risk", "CE-1 corrected by evidence") == []


def test_both_backed_is_sources_disagree():
    f = seen_with(["a", "b"])
    assert ids(f, "risk", "CE-1 sources disagree") == [("a", "b")]
    assert ids(f, "risk", "CE-1 revised without evidence") == []


def test_neither_backed_is_revised_without_evidence():
    f = seen_with([])
    assert ids(f, "risk", "CE-1 revised without evidence") == [("a", "b")]
    assert ids(f, "risk", "CE-1 sources disagree") == []
