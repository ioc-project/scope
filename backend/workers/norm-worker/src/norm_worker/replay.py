#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Manual, opt-in replay of ClickHouse harness exports."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import click
from norm import HarnessReplay, normalize_harness


def _read_rows(path: Path | None, signal: str) -> list[dict[str, Any]]:
    if path is None:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise click.ClickException(f"Cannot read {signal} export as UTF-8.") from None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        rows = []
        for line_num, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                raise click.ClickException(f"Malformed {signal} JSON at line {line_num}.") from None
        value = rows
    if isinstance(value, dict):
        value = value["data"] if "data" in value else [value]
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise click.ClickException(f"Expected {signal} rows as JSON objects.")
    return value


def _persist(replay: HarnessReplay) -> bool:
    # Preview never imports the API or initializes a connector.
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
    from scope.client.dal import ingest_normalized_kg
    from scope.dependencies import close_all, get_neo4j_connector

    try:
        return ingest_normalized_kg(get_neo4j_connector(), replay.nodes, replay.edges, override=False)
    finally:
        close_all()


@click.command("norm-worker-replay")
@click.option("--capture-id", required=True, help="Stable identifier for this capture.")
@click.option("--traces", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--logs", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--include-content", is_flag=True, help="Include allowlisted I/O from an approved sanitized capture.")
@click.option("--write", is_flag=True, help="Merge the preview into the configured Neo4j database.")
def main(capture_id: str, traces: Path | None, logs: Path | None, include_content: bool, write: bool) -> None:
    """Preview a harness KG and its coverage as JSON. No database write by default."""
    if traces is None and logs is None:
        raise click.UsageError("Provide --traces and/or --logs.")
    try:
        replay = normalize_harness(
            capture_id,
            traces=_read_rows(traces, "trace"),
            logs=_read_rows(logs, "log"),
            include_content=include_content,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    if not replay.nodes:
        click.echo(json.dumps({"coverage": replay.coverage}, indent=2))
        raise click.ClickException("Capture contains no normalizable rows.")
    if write:
        # The connector renders bound parameters at DEBUG. Never log the preview.
        previous_level = logging.root.manager.disable
        logging.disable(max(previous_level, logging.CRITICAL))
        try:
            written = _persist(replay)
        except Exception:
            # Connector errors can include query parameters. Do not echo them.
            raise click.ClickException("KG write failed; verify Neo4j configuration and connectivity.") from None
        finally:
            logging.disable(previous_level)
        if not written:
            raise click.ClickException("KG write did not succeed.")
    click.echo(
        json.dumps(
            {"nodes": replay.nodes, "edges": replay.edges, "coverage": replay.coverage, "written": write},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
