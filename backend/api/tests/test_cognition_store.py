# Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scope.client import cognition_store
from scope.connectors.sqlalchemy import SQLAlchemyConnector
from scope.dependencies import get_postgres_db

logger = logging.getLogger(__name__)


def test_cognition_info(monkeypatch: pytest.MonkeyPatch, cognition_fabric_app) -> None:
    db = SQLAlchemyConnector()
    cognition_fabric_app.dependency_overrides[get_postgres_db] = lambda: db
    monkeypatch.setattr(cognition_store, "get_item_count", lambda db: 0)
    response = TestClient(cognition_fabric_app).get("/api/v1/cognition-store/info")

    assert response.status_code == 200
    assert response.json()["version"]
    assert response.json()["uptime"] >= 0
    assert response.json()["item_count"] == 0


def _insert_store_item(payload: dict[str, Any], client: TestClient) -> dict[str, Any]:
    logger.info("StoreItem being stored:\n%s", json.dumps(payload, indent=2))
    db = client.app.dependency_overrides[get_postgres_db]()
    # Use the database clock to avoid host/container clock skew.
    before = db.execute("SELECT clock_timestamp()")[0][0]
    response = client.post("/api/v1/cognition-store/store", json=payload)
    after = db.execute("SELECT clock_timestamp()")[0][0]
    logger.info("Insertion HTTP status: %s", response.status_code)
    if response.status_code != 200:
        logger.error("Insertion failed: %s", response.text)

    assert response.status_code == 200, response.text
    stored = response.json()
    assert {k: v for k, v in stored.items() if k != "timestamp"} == payload
    timestamp = datetime.fromisoformat(stored["timestamp"])
    assert before <= timestamp <= after

    retrieved = client.get(
        "/api/v1/cognition-store/store",
        params={"namespace": payload["namespace"], "key": payload["key"]},
    )
    assert retrieved.status_code == 200, retrieved.text
    assert retrieved.json() == [stored]
    logger.info("Insertion successful: stored item verified by GET /store.")
    return stored


def test_store_item(store_client: TestClient) -> None:
    payload = {
        "ce_id": "00000000-0000-4000-8000-000000000001",
        "namespace": "security-detections",
        "key": "finding-001",
        "type": "detection-finding",
        "value": {"outcome": "DENY", "score": 0.95},
        "metadata": {
            "mas_id": "mas-123",
            "session_id": "session-789",
            "trace_id": "trace-456",
            "span_id": "span-001",
            "tag": "anomaly",
        },
    }
    _insert_store_item(payload, store_client)


def _test_store_ten_different_items(store_client: TestClient) -> None:
    values = [
        {"outcome": "DENY", "score": 0.95},
        {"outcome": "ALLOW", "score": 0.1},
        ["detection", "anomaly"],
        "unstructured finding",
        42,
        0.75,
        True,
        False,
        None,
        {"details": {"signals": [1, None, "alert"]}},
    ]
    stored_items = []
    for index, value in enumerate(values, start=1):
        payload = {
            "ce_id": f"00000000-0000-4000-8000-{index:012d}",
            "namespace": (
                "security-detections",
                "active-investigations",
                "CASA",
                "global",
                "regression",
            )[(index - 1) % 5],
            "key": f"item-{index:03d}",
            "type": (
                ("detection-finding", "tool-call-block", "regression-finding")[
                    (index // 2) % 3
                ]
                if index % 2
                else None
            ),
            "value": value,
            "metadata": {
                "mas_id": "mas-1" if index <= 5 else "mas-2",
                "session_id": f"session-{index}",
                "trace_id": f"trace-{index}",
                "span_id": f"span-{index}",
                "tag": "tag-3and5" if index in (3, 5) else f"tag-{index}",
            }
            if index % 2
            else None,
        }
        stored_items.append(_insert_store_item(payload, store_client))

    response = store_client.get("/api/v1/cognition-store/store")
    assert response.status_code == 200, response.text
    assert len(response.json()) == 10
    assert response.json() == stored_items
    logger.info("Successfully stored and retrieved all 10 items in insertion order.")


def test_retrieve_all_ten_items(store_client: TestClient) -> None:
    _test_store_ten_different_items(store_client)

    response = store_client.get("/api/v1/cognition-store/store")
    assert response.status_code == 200, response.text
    items = response.json()
    assert len(items) == 10
    assert [item["key"] for item in items] == [
        f"item-{index:03d}" for index in range(1, 11)
    ]
    logger.info("Retrieved all 10 StoreItems:\n%s", json.dumps(items, indent=2))


def test_retrieve_filtered_items(store_client: TestClient) -> None:
    _test_store_ten_different_items(store_client)

    all_response = store_client.get("/api/v1/cognition-store/store")
    assert all_response.status_code == 200, all_response.text
    items_by_key = {item["key"]: item for item in all_response.json()}
    cases = [
        ({"namespace": "security-detections"}, [1, 6]),
        ({"namespace": "active-investigations"}, [2, 7]),
        ({"namespace": "CASA"}, [3, 8]),
        ({"namespace": "global"}, [4, 9]),
        ({"namespace": "regression"}, [5, 10]),
        ({"key": "item-004"}, [4]),
        ({"ce_id": "00000000-0000-4000-8000-000000000006"}, [6]),
        ({"type": "detection-finding"}, [1, 7]),
        ({"type": "tool-call-block"}, [3, 9]),
        ({"type": "regression-finding"}, [5]),
        ({"metadata.mas_id": "mas-1"}, [1, 3, 5]),
        ({"metadata.mas_id": "mas-2"}, [7, 9]),
        ({"metadata.session_id": "session-3"}, [3]),
        ({"metadata.trace_id": "trace-7"}, [7]),
        ({"metadata.span_id": "span-9"}, [9]),
        ({"metadata.tag": "tag-1"}, [1]),
        ({"metadata.tag": "tag-2"}, []),
        ({"metadata.tag": "tag-3and5"}, [3, 5]),
        (
            {
                "namespace": "security-detections",
                "type": "detection-finding",
                "metadata.mas_id": "mas-1",
            },
            [1],
        ),
        ({"namespace": "global", "metadata.mas_id": "mas-1"}, []),
        ({"namespace": "CASA", "metadata.mas_id": "mas-2"}, []),
        ({"metadata.mas_id": "mas-unknown"}, []),
        ({"namespace": "regression", "type": "tool-call-block"}, []),
        ({"key": "item-00"}, []),
        ({"namespace": "CASA", "type": "regression-finding"}, []),
    ]

    for filters, expected_indices in cases:
        response = store_client.get("/api/v1/cognition-store/store", params=filters)
        assert response.status_code == 200, response.text
        items = response.json()
        expected = [items_by_key[f"item-{index:03d}"] for index in expected_indices]
        assert items == expected, f"Unexpected results for filters: {filters}"
        logger.info(
            "Filters %s retrieved %d StoreItems:\n%s",
            filters,
            len(items),
            json.dumps(items, indent=2),
        )


def test_store_and_get_ce_findings(store_client: TestClient) -> None:
    first_finding = {
        "finding_id": "finding-001",
        "mas_id": "mas-123",
        "trace_id": "trace-001",
        "session_id": "session-a",
        "outcome": "ALLOW",
        "reason": "Policy allows this interaction.",
        "source_agent_id": "agent-001",
        "destination_agent_id": "agent-002",
        "action_class": "ACTION_CLASS_MCP_TOOL_CALL",
        "direction": "DIRECTION_REQUEST",
        "timestamp": "2026-09-22T12:00:00+00:00",
    }
    second_finding = {
        "finding_id": "finding-002",
        "mas_id": "mas-123",
        "trace_id": "trace-001",
        "session_id": "session-b",
        "outcome": "DENY",
        "reason": "Destination is not in allow-list.",
        "source_agent_id": "agent-003",
        "destination_agent_id": "agent-004",
        "action_class": "ACTION_CLASS_A2A_DELEGATION",
        "direction": "DIRECTION_RESPONSE",
        "timestamp": "2026-09-22T12:05:00+00:00",
    }

    first_store = store_client.post(
        "/api/v1/cognition-store/findings",
        json=first_finding,
    )
    assert first_store.status_code == 200
    assert first_store.json() == {"success": True, "finding_id": "finding-001"}

    second_store = store_client.post(
        "/api/v1/cognition-store/findings",
        json=second_finding,
    )
    assert second_store.status_code == 200
    assert second_store.json() == {"success": True, "finding_id": "finding-002"}

    all_findings = store_client.get(
        "/api/v1/cognition-store/findings",
        params={"mas_id": "mas-123", "limit": 100},
    )
    assert all_findings.status_code == 200
    all_body = all_findings.json()
    assert all_body["mas_id"] == "mas-123"
    assert all_body["context_complete"] is True
    assert [finding["finding_id"] for finding in all_body["findings"]] == [
        "finding-002",
        "finding-001",
    ]

    session_scoped = store_client.get(
        "/api/v1/cognition-store/findings",
        params={"mas_id": "mas-123", "session_id": "session-a", "limit": 100},
    )
    assert session_scoped.status_code == 200
    assert [finding["finding_id"] for finding in session_scoped.json()["findings"]] == [
        "finding-001",
    ]
