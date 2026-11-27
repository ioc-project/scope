# failure-detection

Deterministic cognitive-failure detectors over the neurosymbolic graph (claims, facts, goals,
tasks, constraints). A library of Python objects: no knowledge-graph access, no command line, no
LLM. The [failure-detection-worker](../workers/failure-detection-worker) reads the graph through
the API library and writes the findings back.

```
graph.py        Delta (an increment, or a whole graph) and SymbolicGraph (elements + typed edges)
primitives.py   shared computations: plain functions, cached until the graph changes
rules/          one function per rule, registered with @rule
engine.py       Engine.process(delta) -> new findings, Engine.finalize() -> end-of-session findings
adapters.py     extractor output (the symbolic-extractor's result) -> Delta
state.py        rows of the neurosymbolic layer (nodes, edges) + a SessionTrajectory -> Delta
reports.py      findings -> detections, the input of the API's failure-report writer
models.py       the objects the rules work on (a view of the ontology's nodes, see below)
```

```python
engine = Engine(session_id)
findings = engine.process(delta_from_extraction(result))  # a whole session, or an increment
findings += engine.finalize()  # when the session ends
```

From the rows the API returns for a session:

```python
state = SymbolicState.from_rows(node_rows, edge_rows)
engine = Engine(session_id)
findings = engine.process(build_delta(state, trajectory)) + engine.finalize()
reports = detections(findings, engine.graph)
```

`models.py` keeps the shape the rules need (one `Call` for agent, LLM and tool calls, ids instead of
edges); the labels, properties and relations it is built from are those of
`neurosymbolic-ontology.ttl`, and `tests/test_ontology.py` checks them against the generated models.

## Adding a rule

```python
@rule("CE-1 missing evidence", "CE-1", "detect", phase="final")
def missing_evidence(g, delta):
    for cid in final_claims(g):
        if leaves := unsupported(g, cid):
            yield Hit(evidence=(cid, *leaves), message="...")
```

- `level`: `risk` (precondition open, a warning) or `detect` (consequence seen).
- `phase`: `live` runs on each delta, only if it holds one of the kinds in `on`
  (`"claim"`, `"fact"`, `"call"`, `"edge:<relation>"`); `final` runs once from `finalize()`.
- A finding is reported once: its identity is the rule, the level and the evidence ids.

## Adding a primitive

Write a function `f(g, *ids)` in `primitives.py` decorated with `@primitive`.

## Tests

From `backend/`:

```bash
uv run --frozen --package failure-detection python -m pytest failure-detection/tests
```

## Rules

| Module | Rules |
|---|---|
| `rules/evidence.py` | CE-1: missing evidence, contradicted by evidence, unreconciled contradiction |
| `rules/conflicts.py` | CC-3, DC-3, DE-1-CM, CS-1 dissent, CC-4, IC-1, IC-2 |
| `rules/handoffs.py` | DC-1 (restating, multicast), DR-2 (dropped, precision) |
| `rules/claims.py` | CS-1 (herding, revision, certainty), DC-2, IE-1 |
| `rules/delegation.py` | CC-1 (overlap, gap), DC-4 (AC, CO) |
| `rules/capability.py` | DC-4 CM, DE-1-MR (from the agents' capabilities and the `supports` edges) |
| `rules/constraints.py` | CR-1, IR-1 |

Not implemented, because they need data the extractor does not produce yet: CC-2, CC-1 gap `detect`
and DC-4 SS (links from tasks and claims to the goal's success criteria), CR-1 against the user
request (where a constraint was stated). Not per trace: DR-1, CS-2.
