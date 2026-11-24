"""Health-check utilities."""

from __future__ import annotations


def healthz() -> dict:
    """Return the health status of the server."""
    return {"status": "ok"}
