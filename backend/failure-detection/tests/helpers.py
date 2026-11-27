from failure_detection import Engine
from failure_detection.models import (
    Call,
    Claim,
    Constraint,
    Edge,
    Fact,
    Goal,
    Task,
    Transition,
)


def call(id, kind="llm", end=0.0, final=False, parent=None, **kw):
    return Call(id=id, kind=kind, end=end, final=final, parent=parent, **kw)


def claim(id, by, force="assertive", text=None):
    return Claim(id=id, text=text or id, force=force, generated_by=by)


def fact(id, by):
    return Fact(id=id, text=id, grounded_in=by)


def edge(source, relation, target):
    return Edge(source=source, relation=relation, target=target)


def run(delta):
    """Process a whole delta then finalize: the rule ids of the findings, as (level, rule)."""
    engine = Engine()
    findings = engine.process(delta) + engine.finalize()
    return findings


def ids(findings, level="detect", rule=None):
    return [f.evidence for f in findings if f.level == level and (rule is None or f.rule == rule)]


def goal(id, criteria=()):
    return Goal(id=id, text=id, success_criteria=criteria)


def task(id, owned_by=None, criteria=("done",), standing=False, checked=False):
    return Task(
        id=id,
        text=id,
        acceptance_criteria=criteria,
        owned_by=owned_by,
        source="system" if standing else None,
        capability_checked=checked,
    )


def constraint(id, strength="hard"):
    return Constraint(id=id, text=id, strength=strength)


def transition(from_call, to_call, via="route"):
    return Transition(from_call=from_call, to_call=to_call, via=via)


def capability(id, agent):
    """A capability defined in the system prompt `sp-<agent>` (see `prompts`)."""
    from failure_detection.models import Capability

    return Capability(id=id, text=id, system_prompt=f"sp-{agent}")


def prompts(*agents):
    from failure_detection.models import SystemPrompt

    return [SystemPrompt(id=f"sp-{a}", agent=a) for a in agents]
