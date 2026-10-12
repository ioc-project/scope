#!/usr/bin/env python3
#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Refresh the vendored LLM price table from LiteLLM.

LiteLLM publishes ``model_prices_and_context_window.json``; this repo vendors a
copy for estimating LLM costs offline. It is written in two places:

* the full file, used by stateful-evals (``cost_estimator.py``), and
* a trimmed copy for the MCE (``mce.engine.pricing``): input/output cost per
  token and aliases of every model that has both, nothing else.

Both are rewritten deterministically (sorted keys), so a refresh with no
upstream change produces no diff. Nothing is written if the downloaded file
fails validation.

    update_model_prices.py [--source URL_OR_PATH] [--summary FILE] [--check]

``--check`` writes nothing and exits 1 if the trimmed copy is not what the full
file produces (i.e. someone edited one by hand).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FULL_FILE = ROOT / "backend/stateful-evals/stateful_evals_be/data/model_prices_and_context_window.json"
TRIMMED_FILE = ROOT / "backend/mce/mce-core/src/mce/engine/data/model_prices.json"
DEFAULT_SOURCE = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"

# Models the cost estimators price by default or document; a download without
# them is almost certainly broken.
REQUIRED_MODELS = ("gpt-4o", "gpt-4o-mini")
MIN_ENTRIES = 1000


def load(source: str) -> dict:
    if source.startswith(("http://", "https://")):
        request = urllib.request.Request(source, headers={"User-Agent": "oxp-model-prices-updater"})
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    return json.loads(Path(source).read_text(encoding="utf-8"))


def _priced(meta: object) -> bool:
    return (
        isinstance(meta, dict)
        and isinstance(meta.get("input_cost_per_token"), (int, float))
        and isinstance(meta.get("output_cost_per_token"), (int, float))
    )


def validate(prices: object) -> dict:
    """Raise ``ValueError`` unless ``prices`` looks like the LiteLLM price table."""
    if not isinstance(prices, dict):
        raise ValueError("the price file is not a JSON object")
    if len(prices) < MIN_ENTRIES:
        raise ValueError(f"only {len(prices)} entries (expected at least {MIN_ENTRIES})")
    for model in REQUIRED_MODELS:
        if not _priced(prices.get(model)):
            raise ValueError(f"no per-token price for required model {model!r}")
    return prices


def trim(prices: dict) -> dict:
    """The MCE copy: per-token prices and aliases of every priced model."""
    trimmed = {}
    for name, meta in prices.items():
        if name == "sample_spec" or not _priced(meta):
            continue
        entry = {
            "input_cost_per_token": float(meta["input_cost_per_token"]),
            "output_cost_per_token": float(meta["output_cost_per_token"]),
        }
        aliases = [a for a in meta.get("aliases") or [] if isinstance(a, str)]
        if aliases:
            entry["aliases"] = aliases
        trimmed[name] = entry
    return trimmed


def render_full(prices: dict) -> str:
    return json.dumps(prices, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def render_trimmed(prices: dict) -> str:
    return json.dumps(trim(prices), separators=(",", ":"), sort_keys=True) + "\n"


def describe_change(old: dict, new: dict) -> str:
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(k for k in set(old) & set(new) if old[k] != new[k])
    repriced = sorted(
        k
        for k in changed
        if _priced(old[k])
        and _priced(new[k])
        and (
            old[k]["input_cost_per_token"] != new[k]["input_cost_per_token"]
            or old[k]["output_cost_per_token"] != new[k]["output_cost_per_token"]
        )
    )
    lines = [
        f"- {len(new)} models upstream (was {len(old)}): {len(added)} added, {len(removed)} removed, "
        f"{len(changed)} changed, of which {len(repriced)} with a new per-token price.",
    ]
    if repriced:
        lines.append("- Re-priced (first 15): " + ", ".join(f"`{k}`" for k in repriced[:15]))
    if removed:
        lines.append(
            "- Removed upstream (first 15): "
            + ", ".join(f"`{k}`" for k in removed[:15])
            + ". Cost estimates for these now fall back to the default model price."
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="URL or local path of the LiteLLM price file")
    parser.add_argument("--summary", help="write a markdown summary of the change to this file")
    parser.add_argument("--check", action="store_true", help="verify the trimmed copy matches the full file; write nothing")
    parser.add_argument("--full-file", type=Path, default=FULL_FILE, help=argparse.SUPPRESS)
    parser.add_argument("--trimmed-file", type=Path, default=TRIMMED_FILE, help=argparse.SUPPRESS)
    args = parser.parse_args()

    current = json.loads(args.full_file.read_text(encoding="utf-8"))

    if args.check:
        expected = render_trimmed(current)
        if not args.trimmed_file.exists() or args.trimmed_file.read_text(encoding="utf-8") != expected:
            print(f"{args.trimmed_file} is not the trimmed form of {args.full_file}; run this script to regenerate it")
            return 1
        print("trimmed price table is in sync")
        return 0

    try:
        new = validate(load(args.source))
    except (OSError, ValueError) as exc:
        print(f"refusing to update: {exc}", file=sys.stderr)
        return 1

    outputs = ((args.full_file, render_full(new)), (args.trimmed_file, render_trimmed(new)))
    changed = False
    for path, content in outputs:
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
            changed = True

    summary = describe_change(current, new) if changed else "No change upstream."
    print(summary)
    if args.summary:
        Path(args.summary).write_text(summary + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
