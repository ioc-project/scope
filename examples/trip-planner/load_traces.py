#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Load recorded trip planner traces into SCOPE, without running the application.

traces/trip_planner_sessions.jsonl.gz holds the spans of 33 sessions (eleven questions in each of three
sets, see questions.py), recorded from real runs of trip_planner.py. This sends them to the
OTLP/HTTP endpoint of a collector as new sessions: every replay gets fresh trace, span and session ids
and current timestamps, so SCOPE processes them exactly as if the application had just run.
Standard library only, no environment to set up:

    python load_traces.py                  # the 33 sessions, once
    python load_traces.py --repeat 2       # 66 sessions
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import secrets
import time
import urllib.request
import uuid
from pathlib import Path

DATASET = Path(__file__).resolve().parent / "traces" / "trip_planner_sessions.jsonl.gz"


def retarget(session: dict, now_ns: int) -> dict:
    """The OTLP/JSON request of one recorded session, as a new session that ends at ``now_ns``.

    Every trace, span and session id is replaced by a new one, wherever it appears: in the ids of the
    spans, and also in the attributes where the SDK links an agent to the span or trace it runs in.
    The traces of the session stay distinct, and every timestamp moves by the same shift.
    """
    spans = session["spans"]
    shift = now_ns - max(int(s["endTimeUnixNano"]) for s in spans)

    new_ids = {s["spanId"]: secrets.token_hex(8) for s in spans}
    new_ids.update({t: secrets.token_hex(16) for t in {s["traceId"] for s in spans}})
    prefix, _, _ = session["session_id"].rpartition("_")
    new_ids[session["session_id"]] = (
        f"{prefix}_{uuid.uuid4()}" if prefix else str(uuid.uuid4())
    )

    pattern = re.compile(
        "|".join(re.escape(old) for old in sorted(new_ids, key=len, reverse=True))
    )
    spans = json.loads(
        pattern.sub(lambda match: new_ids[match.group(0)], json.dumps(spans))
    )

    for span in spans:
        span["startTimeUnixNano"] = str(int(span["startTimeUnixNano"]) + shift)
        span["endTimeUnixNano"] = str(int(span["endTimeUnixNano"]) + shift)
        for event in span.get("events", []):
            event["timeUnixNano"] = str(int(event["timeUnixNano"]) + shift)
    return {
        "resourceSpans": [
            {
                "resource": session["resource"],
                "scopeSpans": [
                    {"scope": {"name": "trip-planner-replay"}, "spans": spans}
                ],
            }
        ]
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--endpoint",
        default=os.getenv("OTLP_HTTP_ENDPOINT", "http://localhost:4318"),
        help="OTLP/HTTP endpoint of the collector",
    )
    parser.add_argument(
        "--repeat", type=int, default=1, help="how many times to replay the dataset"
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=0,
        help="seconds between two sessions",
    )
    args = parser.parse_args()

    with gzip.open(DATASET, "rt", encoding="utf-8") as handle:
        sessions = [json.loads(line) for line in handle]

    total = len(sessions) * args.repeat
    for number in range(1, total + 1):
        request = retarget(sessions[(number - 1) % len(sessions)], time.time_ns())
        http = urllib.request.Request(
            args.endpoint.rstrip("/") + "/v1/traces",
            data=json.dumps(request).encode(),
            headers={"content-type": "application/json"},
        )
        urllib.request.urlopen(http, timeout=30).read()
        print(f"\r{number}/{total} sessions sent", end="", flush=True)
        time.sleep(args.pause)
    print(f"\nLoaded {total} sessions into {args.endpoint}")


if __name__ == "__main__":
    main()
