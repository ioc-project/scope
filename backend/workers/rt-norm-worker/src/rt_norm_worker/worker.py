#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol

from norm import GraphReader, Kind, StreamNormalizer

DEFAULT_TRACES_PATH = Path("~/claris/otel_traces.json").expanduser()


def read_records(path: Path = DEFAULT_TRACES_PATH) -> Iterator[dict[str, Any]]:
    """Yield the records of an OTel export file (a JSON array of span or log objects) one by one."""
    with open(Path(path).expanduser(), encoding="utf-8") as f:
        records = json.load(f)
    if not isinstance(records, list):
        raise ValueError(f"Expected a JSON array in {path}")
    yield from records


class Graph(GraphReader, Protocol):
    """Where the KG lives: read what a span needs, write back what changed."""

    def apply(self, delta: Any) -> None: ...


def run(graph: Graph, path: Path = DEFAULT_TRACES_PATH, kind: Kind = "span") -> int:
    """Print every record, normalize it on its own and write only what changed to ``graph``.

    Nothing is kept between records: each one is normalized against the graph as stored.

    Returns the number of records processed.
    """
    count = 0
    for record in read_records(path):
        print(json.dumps(record))
        delta = StreamNormalizer(graph).process(record, kind)
        print(
            f"  -> +{len(delta.nodes)} nodes, +{len(delta.edges)} edges, "
            f"-{len(delta.removed_nodes)} nodes, -{len(delta.removed_edges)} edges"
        )
        graph.apply(delta)
        count += 1
    return count
