"""CLI: ``python -m symbolic_extractor <session_id> [-o out.json]``."""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from symbolic_extractor.extractor import SymbolicExtractor
from symbolic_extractor.comparison import (
    DEFAULT_SAME_CLAIM_THRESHOLD,
    DEFAULT_SIMILARITY_THRESHOLD,
)
from symbolic_extractor.embeddings import TextEmbedder
from symbolic_extractor.kg_reader import KGSessionReader
from symbolic_extractor.llm import OpenAIChatClient
from symbolic_extractor.pipeline import build_pipeline
from symbolic_extractor.models import ExtractionRequest, ExtractionResult, SessionData, SessionTrajectory
from symbolic_extractor.output import (
    embeddings_file,
    read_session_file,
    read_trajectory_file,
    session_file,
    trajectory_file,
    write_json,
)
from symbolic_extractor.usage import LLMUsage


def run_live(
    pipeline: SymbolicExtractor,
    data: SessionData,
    request: ExtractionRequest,
    trajectory: SessionTrajectory | None,
) -> ExtractionResult:
    """Replay the session node by node (in start time order) through ``live_process``.

    Each node is processed as if it had just appeared in the KG. The returned result gathers
    the annotated nodes, the edges recorded along the way, the total usage and the symbolic state
    after the last node. A relation that arrives after its node was returned (the capabilities of an agent
    are read after its first AgentCall node) is put on that node by ``live_process`` at the step that
    records it, in place, so the nodes gathered here are up to date without a final pass.
    """
    annotated = []
    edges = []
    evaluations: dict[str, list] = {}
    trace = None
    total = LLMUsage()
    by_element: dict[str, LLMUsage] = {}
    for node in data.nodes():
        live = pipeline.live_process(node, request, trajectory)
        # ``annotated`` keeps the node objects themselves, and ``live_process`` later modifies the earlier ones in
        # place (see ``live.updated_nodes``), so what is written at the end is up to date.
        # A consumer that handles each result as soon as it gets it (writes the nodes to the KG, streams them, ...)
        # holds a copy that can become stale: it must also process ``live.updated_nodes`` at each step, the
        # earlier nodes whose annotations changed because of this one, and replace what it has for each of them
        # (matching on ``node_id``, the latest version wins). For example:
        #
        #     for n in live.nodes + live.updated_nodes:
        #         sink.upsert(n)
        #
        # Without it, an AgentCall would keep the capabilities and standing tasks it had when it was returned
        # (``[]`` for the first call of an agent), and the goal status and ``fulfils`` of the claims would keep
        # their values from before the final answer was judged.
        annotated.extend(live.nodes)
        edges.extend(live.edges)
        for key, items in live.evaluations.items():
            evaluations.setdefault(key, []).extend(items)
        trace = live.symbolic_state
        # The usage of each call, including the relation extractors (not held by the nodes).
        total = total + live.llm_usage
        for key, usage in live.llm_usage_by_element.items():
            by_element[key] = by_element.get(key, LLMUsage()) + usage
    return ExtractionResult(
        session_id=data.session_id,
        session=data.session,
        nodes=annotated,
        edges=edges,
        evaluations=evaluations,
        llm_usage=total,
        llm_usage_by_element=by_element,
        symbolic_state=trace,
    )


def build_html(output_path: Path, trajectory_path: Path) -> None:
    """Run scripts/claims_graph.py on the final output JSON."""
    script = Path(__file__).resolve().parents[2] / "scripts" / "claims_graph.py"
    command = [sys.executable, str(script), str(output_path)]
    if trajectory_path.exists():
        command += ["--trajectory", str(trajectory_path)]
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        print(f"claims_graph.py failed with exit code {completed.returncode}", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="symbolic-extractor")
    parser.add_argument("session_id", help="Session to process")
    parser.add_argument(
        "-o",
        "--output",
        help="Output JSON path (default: output/<session_id>.json)",
    )
    parser.add_argument(
        "--input-dir",
        default="input",
        help="Directory where the data fetched from the KG is saved: <session_id>.json and "
        "trajectory_<session_id>.json",
    )
    parser.add_argument(
        "--from-file",
        nargs="?",
        const=True,
        metavar="PATH",
        help="Load the session from a saved input file instead of the KG (no DB access). "
        "Without PATH, uses <input-dir>/<session_id>.json",
    )
    parser.add_argument(
        "--fetch-only",
        action="store_true",
        help="Only fetch from the KG and save the input file; no LLM calls",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Process the session node by node with live_process (as if it was in progress); "
        "the session is fetched as usual",
    )
    parser.add_argument(
        "--html",
        action="store_true",
        help="After the extraction, build the claims graph page by running scripts/claims_graph.py "
        "on the output JSON (written next to it as <session_id>_claims.html)",
    )
    parser.add_argument(
        "--write-kg",
        action="store_true",
        help="After the extraction, write the symbolic state to the KG (see kg_writer.py)",
    )
    parser.add_argument(
        "--replace-kg",
        action="store_true",
        help="With --write-kg: delete the symbolic state already written for the session first "
        "(ids change when claims are worded differently)",
    )
    parser.add_argument(
        "--no-same-claims",
        action="store_true",
        help="Skip the embedding comparison that adds same_claims to each claim",
    )
    parser.add_argument(
        "--same-claim-threshold",
        type=float,
        default=DEFAULT_SAME_CLAIM_THRESHOLD,
        help="Minimum probability for an input claim to be listed in same_claims "
        f"(default {DEFAULT_SAME_CLAIM_THRESHOLD})",
    )
    parser.add_argument(
        "--similarity-threshold",
        type=float,
        default=DEFAULT_SIMILARITY_THRESHOLD,
        help="Embedding similarity at which two claims have probability 0.5 of being the "
        f"same; lower it to accept looser paraphrases (default {DEFAULT_SIMILARITY_THRESHOLD})",
    )
    args = parser.parse_args(argv)
    if args.fetch_only and args.from_file:
        parser.error("--fetch-only and --from-file cannot be combined")

    logging.basicConfig(level=logging.INFO)
    request = ExtractionRequest(session_id=args.session_id)

    if args.from_file:
        source = (
            session_file(args.input_dir, request.session_id)
            if args.from_file is True
            else args.from_file
        )
        data = read_session_file(source)
        if data.session_id != request.session_id:
            parser.error(f"{source} holds session {data.session_id!r}, not {request.session_id!r}")
        print(f"Loaded {source}")
        trajectory_path = trajectory_file(args.input_dir, request.session_id)
        if trajectory_path.exists():
            trajectory = read_trajectory_file(trajectory_path)
            print(f"Loaded {trajectory_path}")
        else:
            trajectory = None
            print(f"No trajectory file at {trajectory_path}; continuing without it")
        reader = None
    else:
        reader = KGSessionReader.from_env()
        data = reader.read(request.session_id)
        input_path = write_json(data, session_file(args.input_dir, request.session_id))
        print(f"Wrote {input_path}")
        # Right after the session: the trajectory ties each LLM / tool call to its AgentCall.
        trajectory = reader.read_trajectory(request.session_id)
        trajectory_path = write_json(
            trajectory, trajectory_file(args.input_dir, request.session_id)
        )
        print(f"Wrote {trajectory_path}")
        if args.fetch_only:
            return

    llm = OpenAIChatClient.from_env()
    embedder = None if args.no_same_claims else TextEmbedder.from_env()
    pipeline = build_pipeline(
        reader,
        llm,
        embedder=embedder,
        same_claim_threshold=args.same_claim_threshold,
        similarity_threshold=args.similarity_threshold,
    )
    if args.live:
        result = run_live(pipeline, data, request, trajectory)
    else:
        result = pipeline.process(data, trajectory=trajectory)

    path = write_json(result, args.output or session_file("output", request.session_id))
    print(f"Wrote {path}")
    if pipeline.embeddings is not None:
        embeddings_path = write_json(
            pipeline.embeddings, embeddings_file(path.parent, request.session_id)
        )
        print(f"Wrote {embeddings_path}")

    if args.write_kg:
        from symbolic_extractor.kg_writer import writer_from_env

        print("KG:", writer_from_env().write(result, replace=args.replace_kg).summary())

    if args.html:
        build_html(path, trajectory_file(args.input_dir, request.session_id))

    if result.symbolic_state is not None:
        counts = [len(n.input_claims or []) for n in result.nodes if n.node_type == "LLMCall"]
        print(
            f"Input claims: {sum(1 for c in counts if c)} of {len(counts)} LLM calls "
            f"have some ({sum(counts)} links); {len(result.symbolic_state.transitions)} agent transitions"
        )

    facts = sum(len(n.symbolic_state_elements.get("facts", [])) for n in result.nodes)
    print(f"Facts: {facts} from {sum(1 for n in result.nodes if n.node_type == 'ToolCall')} tool calls")
    prompts = [p for n in result.nodes for p in n.symbolic_state_elements.get("system_prompts", [])]
    agents = {s.get("agent_id") for s in (pipeline.trajectory.steps if pipeline.trajectory else []) if s.get("kind") == "llm"}
    agents.discard(None)
    agents.discard("")
    cut = [p["agent_id"] for p in prompts if p["truncated"]]
    print(
        f"System prompts: {len(prompts)} of {len(agents)} agents"
        + (f" (cut by the KG: {', '.join(cut)})" if cut else "")
    )
    system_tasks = [t for n in result.nodes for t in n.symbolic_state_elements.get("system_tasks", [])]
    print(
        f"System tasks: {len(system_tasks)} standing tasks of {len({t['agent_id'] for t in system_tasks})} agents, "
        f"linked to {sum(1 for n in result.nodes if n.system_tasks)} agent calls"
    )
    capabilities = [c for n in result.nodes for c in n.symbolic_state_elements.get("capabilities", [])]
    print(
        f"Capabilities: {len(capabilities)} capabilities of {len({c['agent_id'] for c in capabilities})} agents, "
        f"linked to {sum(1 for n in result.nodes if n.capabilities)} agent calls"
    )
    for label, kind in ("tasks", "tasks"), ("system tasks", "system_tasks"):
        linked_tasks = [
            t
            for n in result.nodes
            for t in n.symbolic_state_elements.get(kind, [])
            if t.get("supported_by_capabilities") is not None
        ]
        if linked_tasks:
            print(
                f"Supports: {sum(1 for t in linked_tasks if t['supported_by_capabilities'])} of {len(linked_tasks)} "
                f"{label} are linked to a capability of their agent "
                f"({sum(len(t['supported_by_capabilities']) for t in linked_tasks)} links)"
            )
    delegation_claims = [c for n in result.nodes for c in n.symbolic_state_elements.get("delegation_claims", [])]
    print(
        f"Delegation claims: {len(delegation_claims)} from {len({c['delegated_to'] for c in delegation_claims})} "
        f"briefs (callers: {', '.join(sorted({c['caller_agent'] for c in delegation_claims})) or 'none'}); "
        f"{sum(1 for c in delegation_claims if c.get('same_as') or c.get('derived_from') or c.get('conflicts_with'))} "
        "related to another claim"
    )
    goals = [g for n in result.nodes for g in n.symbolic_state_elements.get("goals", [])]
    print(f"Goals: {len(goals)}" + "".join(f" [{g['status']}] {g['goal_text']}" for g in goals))
    constraints = [c for n in result.nodes for c in n.symbolic_state_elements.get("constraints", [])]
    user_constraints = [c for c in constraints if c["origin"] == "user"]
    system_constraints = [c for c in constraints if c["origin"] == "system"]
    bounded_tasks = sum(
        1
        for n in result.nodes
        for t in n.symbolic_state_elements.get("tasks", [])
        if t.get("bounded_by")
    )
    print(
        f"Constraints: {len(user_constraints)} from the user (bound {sum(1 for g in goals if g.get('bounded_by'))} "
        f"of {len(goals)} goals), {len(system_constraints)} from the system prompt of "
        f"{len({c['agent_id'] for c in system_constraints})} agents (bound {bounded_tasks} tasks)"
    )
    claims = [c for n in result.nodes for c in n.symbolic_state_elements.get("claims", [])]
    if any(c.get("answers") is not None for c in claims):
        print(
            f"Answers: {sum(1 for c in claims if c.get('answers'))} claims of the final answer "
            f"answer the goal ({sum(len(c.get('answers') or []) for c in claims)} links)"
        )
    for evaluation in pipeline.goal_evaluations:
        print(
            f"Fulfils: the final answer {'satisfies' if evaluation.satisfied else 'does not satisfy'} the goal "
            f"({len(evaluation.fulfils)} of {len(evaluation.claim_ids)} claims fulfil it), goal status "
            f"{evaluation.previous_status} -> {evaluation.status}: {evaluation.reasoning}"
        )
    tasks = [t for n in result.nodes for t in n.symbolic_state_elements.get("tasks", [])]
    if any(c.get("produced_by") is not None for c in claims):
        print(
            f"Produced by: {sum(1 for c in claims if c.get('produced_by'))} of {len(claims)} claims "
            f"come from the task of their agent call"
        )
    if pipeline.task_evaluations:
        judged_claims = sum(len(e.claim_ids) for e in pipeline.task_evaluations)
        print(
            f"Fulfils (tasks): {sum(len(e.fulfils) for e in pipeline.task_evaluations)} of {judged_claims} claims "
            f"fulfil the task they were produced by ({len(pipeline.task_evaluations)} judgements, "
            f"one per LLM call and task)"
        )
    by_status: dict[str, int] = {}
    for task in tasks:
        by_status[task["status"]] = by_status.get(task["status"], 0) + 1
    print(f"Tasks: {len(tasks)} from {sum(1 for n in result.nodes if n.node_type == 'AgentCall')} agent calls {by_status}")
    ordered = [t for t in tasks if t.get("preceded_by") is not None]
    if ordered:
        print(
            f"Preceded by: {sum(1 for t in ordered if t['preceded_by'])} of {len(ordered)} tasks follow "
            f"the task of the preceding agent call ({sum(len(t['preceded_by']) for t in ordered)} links)"
        )
    judged = [t for t in tasks if t.get("implements") is not None]
    if judged:
        print(
            f"Relations: {sum(1 for t in judged if t['achieves'])} tasks achieve the goal, "
            f"{sum(1 for t in judged if t['implements'])} implement a task "
            f"({sum(len(t['implements']) for t in judged)} implements links, "
            f"{sum(len(t['achieves']) for t in judged)} achieves links, of {len(judged)} tasks judged)"
        )

    same = [
        c.get("same_claims")
        for n in result.nodes
        for c in n.symbolic_state_elements.get("claims", [])
        if c.get("same_claims") is not None
    ]
    if same:
        print(f"Same claims: {sum(1 for s in same if s)} of {len(same)} claims match an input claim")

    usage = result.llm_usage
    cost = f"${usage.cost_usd:.6f}" if usage.cost_usd is not None else "unknown (see LLM_*_COST_PER_1M)"
    print(
        f"LLM usage: {usage.calls} calls, {usage.prompt_tokens} prompt + "
        f"{usage.completion_tokens} completion = {usage.total_tokens} tokens, cost {cost}"
    )
    if usage.estimated_cost_calls:
        print(
            f"WARNING: the endpoint reported no cost for {usage.estimated_cost_calls} of {usage.calls} calls: "
            "they are priced with LLM_INPUT_COST_PER_1M / LLM_OUTPUT_COST_PER_1M, so the cost is an estimate"
        )
    if usage.unpriced_calls:
        print(
            f"WARNING: {usage.unpriced_calls} of {usage.calls} calls have no cost (none reported, no price "
            "configured): the cost above is a lower bound"
        )


if __name__ == "__main__":
    main()
