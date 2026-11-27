from conftest import SESSION, state, trajectory

from failure_detection import Engine, Finding, build_delta, call_of, detections


def run():
    engine = Engine(SESSION)
    delta = build_delta(state(), trajectory())
    return engine, engine.process(delta) + engine.finalize()


def finding(rule, failure, level, evidence, message="m"):
    return Finding(
        evidence=evidence, message=message, rule=rule, failure=failure, level=level, session_id="s"
    )


def test_the_detectors_run_on_what_was_read():
    engine, findings = run()
    assert findings  # c2 (final) has no evidence of its own: CE-1 and friends
    assert all(f.session_id == SESSION for f in findings)


def test_a_finding_is_placed_at_the_call_of_its_first_element():
    engine, _ = run()
    g = engine.graph
    assert call_of(g, finding("r", "CE-1", "detect", ("c2",))) == "L2"
    assert call_of(g, finding("r", "CE-1", "detect", ("f1",))) == "T1"
    assert call_of(g, finding("r", "DC-4", "risk", ("t1",))) == "A2"
    assert call_of(g, finding("r", "DC-4", "risk", ("k1",))) is None
    assert call_of(g, finding("r", "DC-4", "risk", ("zz", "c1"))) == "L1"


def test_each_finding_is_one_report_with_a_level_and_a_rule():
    engine, _ = run()
    out = detections(
        [
            finding("CE-1 a", "CE-1", "risk", ("c2",), "open"),
            finding("CE-1 b", "CE-1", "detect", ("c2",), "seen"),
            finding("CE-1 b", "CE-1", "detect", ("c2",), "seen"),  # the same finding twice
            finding("CE-1 b", "CE-1", "detect", ("c1",), "seen"),  # same rule, other evidence
            finding("DR-2", "DR-2", "detect", ("c1",), "dropped"),
        ],
        engine.graph,
    )
    assert len(out) == 4
    assert len({d["key"] for d in out}) == 4
    by_rule = {(d["rule"], d["call_id"]): d for d in out}
    risk = by_rule[("CE-1 a", "L2")]
    assert (risk["level"], risk["confidence"], risk["reasoning"]) == ("risk", "low", "open")
    seen = by_rule[("CE-1 b", "L2")]
    assert (seen["level"], seen["confidence"], seen["reasoning"]) == ("detection", "high", "seen")
