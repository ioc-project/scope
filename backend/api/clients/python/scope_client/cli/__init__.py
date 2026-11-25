# Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Command line interface for making simple requests to the SCOPE API.

Usage: ``python -m scope_client.cli [options] <command> [command options]``.

Exit codes: 0 success, 1 unexpected status or failed assertion, 2 usage error, 3 connection error or timeout.
"""

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from typing import Any

import httpx

from ..client import AuthenticatedClient, Client
from .commands import COMMANDS

DEFAULT_BASE_URL = "http://localhost:8006"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_CONNECTION = 3


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scope_client.cli", description="Make simple requests to the Cognition Fabric API."
    )
    parser.add_argument(
        "--base-url", default=os.environ.get("SCOPE_BASE_URL", DEFAULT_BASE_URL), help="env: SCOPE_BASE_URL"
    )
    parser.add_argument("--token", default=os.environ.get("SCOPE_TOKEN"), help="Bearer token; env: SCOPE_TOKEN")
    parser.add_argument("--timeout", type=float, default=30.0, help="Request timeout in seconds")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification")
    parser.add_argument("--ca-bundle", help="Path to a CA bundle for TLS verification")
    parser.add_argument("--output", choices=["json", "raw"], default="json", help="Body format (default: pretty json)")
    parser.add_argument(
        "--expect-status", type=int, help="Expected HTTP status (default: any 2xx); mismatch exits with 1"
    )
    parser.add_argument(
        "--expect-json-key", action="append", default=[], help="Top-level key required in the JSON body (repeatable)"
    )
    parser.add_argument("--retries", type=int, default=0, help="Retries on connection error or failed expectation")
    parser.add_argument("--retry-delay", type=float, default=2.0, help="Seconds between retries")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print status line to stderr")

    sub = parser.add_subparsers(dest="command", required=True, metavar="command")
    for name, command in COMMANDS.items():
        command.add_arguments(sub.add_parser(name, help=command.help, description=command.help))
    return parser


def _make_client(args: argparse.Namespace) -> AuthenticatedClient | Client:
    verify: bool | str = args.ca_bundle or (not args.insecure)
    kwargs: dict[str, Any] = {
        "base_url": args.base_url,
        "timeout": httpx.Timeout(args.timeout),
        "verify_ssl": verify,
        "raise_on_unexpected_status": False,
    }
    if args.token:
        return AuthenticatedClient(token=args.token, **kwargs)
    return Client(**kwargs)


def _check(args: argparse.Namespace, status: int, content: bytes) -> str | None:
    """Return a failure reason, or None if the response meets expectations."""
    if args.expect_status is not None:
        if status != args.expect_status:
            return f"expected status {args.expect_status}, got {status}"
    elif not 200 <= status < 300:
        return f"unexpected status {status}"
    if args.expect_json_key:
        try:
            body = json.loads(content)
        except ValueError:
            return "body is not valid JSON"
        if not isinstance(body, dict):
            return "body is not a JSON object"
        missing = [key for key in args.expect_json_key if key not in body]
        if missing:
            return f"missing JSON key(s): {', '.join(missing)}"
    return None


def _format_body(args: argparse.Namespace, content: bytes) -> str:
    text = content.decode("utf-8", errors="replace")
    if args.output == "json":
        try:
            return json.dumps(json.loads(text), indent=2)
        except ValueError:
            pass
    return text


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    command = COMMANDS[args.command]

    attempts = max(args.retries, 0) + 1
    failure = "no attempt made"
    exit_code = EXIT_FAILED
    with _make_client(args) as client:
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                time.sleep(args.retry_delay)
            try:
                response = command.run(client, args)
            except httpx.HTTPError as exc:
                failure, exit_code = f"{type(exc).__name__}: {exc}", EXIT_CONNECTION
                print(f"attempt {attempt}/{attempts}: {failure}", file=sys.stderr)
                continue
            except (KeyError, TypeError, ValueError) as exc:
                # The generated parsers raise when a 2xx body doesn't match the spec'd schema.
                failure, exit_code = (
                    f"response does not match the OpenAPI schema ({type(exc).__name__}: {exc})",
                    EXIT_FAILED,
                )
                print(f"attempt {attempt}/{attempts}: {failure}", file=sys.stderr)
                continue
            status = int(response.status_code)
            if args.verbose:
                print(f"HTTP {status}", file=sys.stderr)
            reason = _check(args, status, response.content)
            if response.content:
                print(_format_body(args, response.content))
            if reason is None:
                return EXIT_OK
            failure, exit_code = reason, EXIT_FAILED
            print(f"attempt {attempt}/{attempts}: {failure}", file=sys.stderr)

    print(f"error: {failure}", file=sys.stderr)
    return exit_code
