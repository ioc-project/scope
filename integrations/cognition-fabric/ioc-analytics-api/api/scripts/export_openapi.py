#!/usr/bin/env python3
"""Export the OpenAPI spec of the OXP API without starting the server.

The FastAPI lifespan is not run and Neo4j connecting is disabled, so no Redis
or database connection is needed.

Usage:
    python scripts/export_openapi.py                       # YAML to stdout
    python scripts/export_openapi.py -o openapi.yaml
    python scripts/export_openapi.py --format json -o openapi.json
"""

import argparse
import json
import sys

import yaml

from scope.connectors.neo4j import Neo4JConnector

# Some endpoint modules build their Neo4j-backed provider at import time. Make
# connecting a no-op so exporting the spec never touches the network.
Neo4JConnector.connect = lambda self, *a, **k: None

from scope.api import app  # noqa: E402


def render(spec: dict, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(spec, indent=2) + "\n"
    return yaml.safe_dump(spec, sort_keys=False, allow_unicode=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-o", "--output", help="output file (default: stdout)")
    parser.add_argument(
        "--format", choices=["yaml", "json"], default="yaml", help="default: yaml"
    )
    args = parser.parse_args()

    text = render(app.openapi(), args.format)
    if args.output:
        with open(args.output, "w") as f:
            f.write(text)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
