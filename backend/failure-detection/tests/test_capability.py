from helpers import call, capability, claim, edge, ids, prompts, run, task

from failure_detection import Delta


def agents():
    return [
        call("A1", "agent", agent="a"),
        call("A2", "agent", agent="b"),
        call("L1", agent="a", parent="A1"),
    ]


def session(supported=(), checked=True, **extra):
    """Agent a (capability ca) and b (cb) were each given the same task; `supported` pairs
    (capability, task) are the supports edges the extractor found."""
    d = Delta(
        calls=agents(),
        system_prompts=prompts("a", "b", "c"),
        capabilities=[capability("ca", "a"), capability("cb", "b")],
        tasks=[task("t1", "A1", checked=checked), task("t2", "A2", checked=checked)],
        edges=[edge(c, "supports", t) for c, t in supported],
        **extra,
    )
    return d


def test_a_task_no_capability_supports_is_a_risk():
    f = run(session(supported=[("cb", "t2")]))
    assert ids(f, "risk", "DC-4 CM") == [("t1",)]


def test_supported_tasks_are_fine():
    assert ids(run(session(supported=[("ca", "t1"), ("cb", "t2")])), "risk", "DC-4 CM") == []


def test_a_task_not_compared_yet_is_not_a_gap():
    assert ids(run(session(checked=False)), "risk", "DC-4 CM") == []


def test_an_agent_with_no_known_capability_is_not_a_gap():
    d = session(supported=[])
    d.capabilities = [capability("cb", "b")]  # nothing known about a
    assert ids(run(d), "risk", "DC-4 CM") == [("t2",)]


def test_standing_tasks_are_ignored():
    d = Delta(
        calls=agents(),
        system_prompts=prompts("a"),
        capabilities=[capability("ca", "a")],
        tasks=[task("s", "A1", standing=True, checked=True)],
    )
    assert ids(run(d), "risk", "DC-4 CM") == []


def test_detect_when_the_agent_without_capability_answered_without_evidence():
    d = session(supported=[("cb", "t2")], claims=[claim("c1", "L1")])
    d.edges.append(edge("c1", "producedBy", "t1"))
    f = run(d)
    assert ids(f, "detect", "DC-4 CM") == [("t1", "c1")]


def test_no_detect_when_its_claims_are_backed():
    from helpers import fact

    d = session(supported=[("cb", "t2")], claims=[claim("c1", "L1")], facts=[fact("f", "L1")])
    d.edges += [edge("c1", "producedBy", "t1"), edge("c1", "supportedBy", "f")]
    assert ids(run(d), "detect", "DC-4 CM") == []


def test_misrouted_when_identical_work_went_to_one_who_cannot_and_one_who_can():
    d = session(supported=[("cb", "t2")])
    d.edges.append(edge("t1", "sameAs", "t2"))
    assert ids(run(d), "detect", "DE-1-MR") == [("t1", "t2")]
    d.edges.append(edge("ca", "supports", "t1"))
    assert ids(run(d), "detect", "DE-1-MR") == []


def test_a_router_is_not_asked_to_do_the_work():
    d = session(supported=[])
    d.calls = [
        call("A1", "agent", agent="a"),
        call("A2", "agent", agent="b", routed_by="A1"),  # a hands work to b ...
        call("A3", "agent", agent="c", routed_by="A1"),  # ... and to c: a is a hub
        call("A4", "agent", agent="a", routed_by="A2"),  # b giving control back is no router
        call("L1", agent="a", parent="A1"),
    ]
    d.capabilities.append(capability("cc", "c"))
    assert ids(run(d), "risk", "DC-4 CM") == [("t2",)]


def test_a_router_is_not_the_agent_who_could_have_done_it():
    d = session(supported=[("cb", "t2")])
    d.calls = [
        call("A1", "agent", agent="a"),
        call("A2", "agent", agent="b", routed_by="A1"),
        call("A3", "agent", agent="c", routed_by="A1"),
        call("L1", agent="a", parent="A1"),
    ]
    d.edges.append(edge("t1", "sameAs", "t2"))
    assert ids(run(d), "detect", "DE-1-MR") == []  # a is a router, so t1 is not a gap at all
