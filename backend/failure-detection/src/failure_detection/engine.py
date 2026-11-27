"""The processing pipe: add a delta to the graph, run the rules it triggers, report what is new."""

from __future__ import annotations

from failure_detection.finding import Finding
from failure_detection.graph import Delta, SymbolicGraph
from failure_detection.primitives import agent_of
from failure_detection.rules import RULES, Rule


class Engine:
    """One engine per session. Its state is the graph and the findings already reported."""

    def __init__(self, session_id: str = "", rules: list[Rule] | None = None) -> None:
        self.session_id = session_id
        self.rules = RULES if rules is None else rules
        self.graph = SymbolicGraph()
        self.reported: set[tuple] = set()

    def process(self, delta: Delta) -> list[Finding]:
        """Add an increment (or a whole graph) and run the live rules it triggers."""
        self.graph.add(delta)
        kinds = delta.kinds()
        return self._run(delta, lambda r: r.phase == "live" and (not r.on or r.on & kinds))

    def finalize(self) -> list[Finding]:
        """The session is over: run the rules that need the whole picture."""
        return self._run(Delta(), lambda r: r.phase == "final")

    def _agents(self, evidence: tuple[str, ...]) -> tuple[str, ...]:
        names = (agent_of(self.graph, e) for e in evidence if e in self.graph.claims)
        return tuple(dict.fromkeys(n for n in names if n))

    def _run(self, delta: Delta, selected) -> list[Finding]:
        new = []
        for r in filter(selected, self.rules):
            for hit in r.fn(self.graph, delta):
                f = Finding(
                    **hit.model_dump(),
                    agents=self._agents(hit.evidence),
                    rule=r.id,
                    failure=r.failure,
                    level=r.level,
                    session_id=self.session_id,
                )
                if f.key not in self.reported:
                    self.reported.add(f.key)
                    new.append(f)
        return new
