from helpers import call, claim, edge, fact, ids, run

from failure_detection import Delta


def test_cs1_herding_two_agents_repeat_a_claim_without_evidence():
    d = Delta(
        calls=[
            call("A", agent="a", end=1),
            call("B", agent="b", end=2),
            call("C", agent="c", end=3),
        ],
        claims=[claim("x", "A"), claim("c1", "B"), claim("c2", "C")],
        edges=[edge("c1", "sameAs", "x"), edge("c2", "sameAs", "x")],
    )
    assert ids(run(d), rule="CS-1 herding") == [("x", "c1", "c2")]
    d.facts.append(fact("f", "T"))
    d.edges.append(edge("c1", "supportedBy", "f"))  # one copy has evidence of its own
    assert ids(run(d), rule="CS-1 herding") == []


def test_cs1_sycophantic_revision():
    d = Delta(
        calls=[
            call("A1", agent="x", start=0, end=1),
            call("Y", agent="y", start=0, end=2),
            call("L2", agent="x", start=3, end=4, input_claims=("b",)),
        ],
        claims=[claim("a", "A1"), claim("b", "Y"), claim("c", "L2")],
        edges=[edge("b", "contradicts", "a"), edge("c", "sameAs", "b")],
    )
    assert ids(run(d), rule="CS-1 revision") == [("a", "b", "c")]
    d.calls[2] = call("L2", agent="x", start=3, end=4, input_claims=("b",), input_facts=("f",))
    assert ids(run(d), rule="CS-1 revision") == []  # a new fact justifies the change


def chain(parent_force, child_force, parent_kind=None, child_kind=None, facts=False):
    d = Delta(
        calls=[call("A", end=1)],
        claims=[
            claim("p", "A", force=parent_force),
            claim("c", "A", force=child_force),
        ],
        edges=[edge("c", "derivedFrom", "p")],
    )
    d.claims[0] = d.claims[0].model_copy(update={"kind": parent_kind})
    d.claims[1] = d.claims[1].model_copy(update={"kind": child_kind})
    if facts:
        d.facts.append(fact("f", "T"))
        d.edges.append(edge("c", "supportedBy", "f"))
    return d


def test_cs1_certainty_rises_with_no_new_fact():
    assert ids(run(chain("hedged", "certain")), rule="CS-1 certainty") == [("p", "c")]
    assert ids(run(chain("hedged", "certain", facts=True)), rule="CS-1 certainty") == []
    assert ids(run(chain("certain", "hedged")), rule="CS-1 certainty") == []


def test_dc2_a_proposal_restated_as_a_fact():
    assert ids(run(chain("directive", "assertive")), rule="DC-2") == [("p", "c")]
    assert ids(run(chain("committing", "committing")), rule="DC-2") == []


def test_dc2_a_suggestion_hardens_into_an_order():
    f = run(chain("suggestion", "order", "coordinative", "coordinative"))
    assert ids(f, rule="DC-2") == [("p", "c")]


def test_ie1_any_claim_without_evidence_certain_or_not():
    d = Delta(calls=[call("A")], claims=[claim("c", "A", force="hedged")])
    assert ids(run(d), rule="IE-1 missing evidence") == [("c", "c")]
    f = run(Delta(calls=[call("A")], claims=[claim("k", "A", force="certain")]))
    (hit,) = [x for x in f if x.rule == "IE-1 missing evidence"]
    assert "as certain" in hit.message
    d.facts.append(fact("f", "T"))
    d.edges.append(edge("c", "supportedBy", "f"))
    assert ids(run(d), rule="IE-1 missing evidence") == []


def test_an_evaluative_claim_needs_no_evidence():
    c = claim("c", "A").model_copy(update={"kind": "evaluative"})
    assert ids(run(Delta(calls=[call("A")], claims=[c])), rule="IE-1 missing evidence") == []


def test_ie1_claim_made_after_a_tool_failed():
    d = Delta(
        calls=[
            call("AC", "agent", agent="a"),
            call("L0", parent="AC", start=0, end=1),
            call("T", "tool", parent="AC", start=1, end=5, failed=True),
            call("L1", parent="AC", start=6, end=7),
        ],
        claims=[claim("before", "L0"), claim("after", "L1")],
    )
    assert ids(run(d), rule="IE-1 after failure") == [("after",)]


def test_findings_name_the_agents_of_their_claims():
    d = Delta(calls=[call("L", agent="itinerary")], claims=[claim("c", "L")])
    (f,) = [x for x in run(d) if x.rule == "IE-1 missing evidence"]
    assert f.agents == ("itinerary",)
