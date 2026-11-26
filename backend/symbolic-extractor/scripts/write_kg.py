"""Write saved extraction outputs to the KG: ``uv run python scripts/write_kg.py output/*.json [--replace]``."""

from __future__ import annotations

import argparse
import sys

from symbolic_extractor.kg_writer import writer_from_env
from symbolic_extractor.output import read_result_file


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("outputs", nargs="+", help="Extraction output JSON files")
    parser.add_argument(
        "--replace", action="store_true", help="Delete the session's symbolic state first"
    )
    args = parser.parse_args()
    writer = writer_from_env()
    incomplete = 0
    for path in args.outputs:
        report = writer.write(read_result_file(path), replace=args.replace)
        print(f"{report.session_id}: {report.summary()}")
        incomplete += not report.complete
    return 1 if incomplete else 0


if __name__ == "__main__":
    sys.exit(main())
