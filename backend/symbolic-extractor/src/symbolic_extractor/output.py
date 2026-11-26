"""Persisting an extraction result.

Only a local JSON file for now.

The KG writer is ``kg_writer.py`` (queries in ``kg_queries.py``); the notes below are the design it follows.

Design notes for the KG writer (modelled on how SCOPE writes metrics, see
``scope.query_builders.metric_graph.write_metric_result_query``):

* Linking: there is no separate linking mechanism. The id of the original node is carried
  through every step (here: ``AnnotatedNode.node["id"]`` and, on each element, a field such
  as ``Claim.generated_by``) and the writer matches the target node by that id, then MERGEs
  the new node and the edge. For claims the edge is ``mas:generatedBy``
  (Claim -> LLMCall); the other relations are defined in ``cognitive-ontology.ttl``.
* Match the target on its ``id`` (e.g. ``llmcall:...``), not on ``spanId``: the metric
  writer has both paths (``spanId`` for ``save_metrics`` / ``save_span_metrics``, ``id`` for
  ``save_call_metrics``) and it is not clear which one produced the existing links.
* Idempotency: MERGE the new node by its deterministic id (``Claim.id`` is a hash of the
  call id, force and text) so a re-run overwrites instead of duplicating.
* The metric writer writes nothing, silently, when the target node does not exist. Do not
  copy that: report claims whose target ``LLMCall`` was not found.
* Not every relation is written at extraction time: ``supportedBy``, ``groundedOn``,
  ``derivedFrom`` and ``contradicts`` belong to a later linking step.
* Starting point for the write itself: ``scope.client.dal.ingest_normalized_kg`` (used by
  norm-worker) takes generic nodes and edges and validates them against the ontology models;
  check it before writing Cypher by hand.
* The ``node`` dicts in this output are not faithful copies of the KG nodes: the SCOPE API
  merges in fields from neighbouring nodes (``prompt``, ``completion``, ``modelName``, ...).
  Only the node ``id`` is needed to link back.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from symbolic_extractor.models import ExtractionResult, SessionData, SessionTrajectory


def session_file(directory: str | Path, session_id: str) -> Path:
    """``<directory>/<session_id>.json``; input and output share the file name."""
    if not session_id or Path(session_id).name != session_id or session_id in {".", ".."}:
        raise ValueError(f"Invalid session id for a file name: {session_id!r}")
    return Path(directory) / f"{session_id}.json"


def trajectory_file(directory: str | Path, session_id: str) -> Path:
    """``<directory>/trajectory_<session_id>.json``: the trajectory saved next to the input."""
    path = session_file(directory, session_id)
    return path.with_name(f"trajectory_{path.name}")


def embeddings_file(directory: str | Path, session_id: str) -> Path:
    """``<directory>/embeddings_<session_id>.json``: vectors, kept out of the extraction output."""
    path = session_file(directory, session_id)
    return path.with_name(f"embeddings_{path.name}")


def read_result_file(path: str | Path) -> ExtractionResult:
    """Load an extraction output previously saved by ``write_json(ExtractionResult, ...)``."""
    return ExtractionResult.model_validate_json(Path(path).read_text(encoding="utf-8"))


def read_session_file(path: str | Path) -> SessionData:
    """Load a session previously saved by ``write_json(SessionData, ...)`` (the input dump)."""
    return SessionData.model_validate_json(Path(path).read_text(encoding="utf-8"))


def read_trajectory_file(path: str | Path) -> SessionTrajectory:
    """Load a trajectory previously saved by ``write_json(SessionTrajectory, ...)``."""
    return SessionTrajectory.model_validate_json(Path(path).read_text(encoding="utf-8"))


def write_json(model: BaseModel, path: str | Path) -> Path:
    """Write a pydantic model (``SessionData``, ``SessionTrajectory`` or ``ExtractionResult``) as JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.model_dump_json(indent=2), encoding="utf-8")
    return path
