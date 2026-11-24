# Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Subscription endpoint tests backed by isolated PostgreSQL schemas."""

import json
import logging
from copy import deepcopy
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest
import requests
from fastapi.testclient import TestClient

from scope.client import cognition_store
from scope.models.cognition_store import StoreItem, Subscription

logger = logging.getLogger(__name__)
PREFIX = "/api/v1/cognition-store"
CE_ID = "00000000-0000-4000-8000-000000000001"


def _create_subscription(client: TestClient, **overrides: Any) -> dict[str, Any]:
    payload = {
        "namespace": "security-detections",
        "type": "detection-finding",
        "filter": {},
        "callback_url": "https://subscriber.example/callback",
        **overrides,
    }
    logger.info("Creating subscription:\n%s", json.dumps(payload, indent=2))
    response = client.post(f"{PREFIX}/subscriptions", json=payload)
    assert response.status_code == 201, response.text
    subscription = response.json()
    assert str(UUID(subscription["id"])) == subscription["id"]
    assert {key: subscription[key] for key in payload if key != "filter"} == {
        key: value for key, value in payload.items() if key != "filter"
    }
    assert subscription["filter"] == {
        key: value for key, value in payload["filter"].items() if value is not None
    }
    logger.info("Created subscription:\n%s", json.dumps(subscription, indent=2))
    return subscription


@pytest.fixture
def callback_post(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    post = MagicMock()
    post.return_value.__enter__.return_value.status_code = 204
    monkeypatch.setattr(cognition_store.requests, "post", post)
    return post


def test_create_and_list_subscriptions(store_client: TestClient) -> None:
    response = store_client.get(f"{PREFIX}/subscriptions")
    assert response.status_code == 200, response.text
    assert response.json() == []

    first = _create_subscription(
        store_client, filter={"metadata.session_id": "session-1"}
    )
    second = _create_subscription(store_client, filter={"metadata.mas_id": "mas-1"})
    third = _create_subscription(
        store_client, namespace="regression", type="regression-finding"
    )
    casa = _create_subscription(store_client, namespace="CASA", type="tool-call-block")
    active = _create_subscription(store_client, namespace="active-investigations")
    global_namespace = _create_subscription(
        store_client, namespace="global", type="regression-finding"
    )
    assert (
        len(
            {
                first["id"],
                second["id"],
                third["id"],
                casa["id"],
                active["id"],
                global_namespace["id"],
            }
        )
        == 6
    )

    for params, expected in [
        ({}, [first, second, third, casa, active, global_namespace]),
        ({"namespace": "security-detections"}, [first, second]),
        ({"namespace": "regression"}, [third]),
        ({"namespace": "CASA"}, [casa]),
        ({"namespace": "active-investigations"}, [active]),
        ({"namespace": "global"}, [global_namespace]),
    ]:
        response = store_client.get(f"{PREFIX}/subscriptions", params=params)
        assert response.status_code == 200, response.text
        assert response.json() == expected
        logger.info(
            "Subscription filters %s returned:\n%s",
            params,
            json.dumps(response.json(), indent=2),
        )


@pytest.mark.parametrize(
    ("filter_value", "expected_filter"),
    [
        (None, {}),
        ({}, {}),
        (
            {
                "metadata.mas_id": None,
                "metadata.session_id": None,
                "metadata.trace_id": None,
                "metadata.span_id": None,
            },
            {},
        ),
        (
            {
                "metadata.mas_id": None,
                "metadata.session_id": "session-1",
                "metadata.trace_id": None,
                "metadata.span_id": "span-1",
            },
            {"metadata.session_id": "session-1", "metadata.span_id": "span-1"},
        ),
        (
            {
                "metadata.mas_id": "",
                "metadata.session_id": "session-1",
                "metadata.trace_id": "trace-1",
                "metadata.span_id": "span-1",
            },
            {
                "metadata.mas_id": "",
                "metadata.session_id": "session-1",
                "metadata.trace_id": "trace-1",
                "metadata.span_id": "span-1",
            },
        ),
    ],
    ids=["omitted", "empty", "all-null", "mixed", "all-present"],
)
def test_subscription_responses_omit_null_filters(
    store_client: TestClient,
    filter_value: dict[str, str | None] | None,
    expected_filter: dict[str, str],
) -> None:
    payload: dict[str, Any] = {
        "namespace": "CASA",
        "type": "tool-call-block",
        "callback_url": "https://subscriber.example/callback",
    }
    if filter_value is not None:
        payload["filter"] = filter_value

    created = store_client.post(f"{PREFIX}/subscriptions", json=payload)
    assert created.status_code == 201, created.text
    subscription = created.json()
    assert subscription["filter"] == expected_filter

    listed = store_client.get(f"{PREFIX}/subscriptions")
    assert listed.status_code == 200, listed.text
    assert listed.json() == [subscription]


def test_cancel_subscription(
    store_client: TestClient, callback_post: MagicMock
) -> None:
    subscription = _create_subscription(store_client)
    remaining = _create_subscription(
        store_client, namespace="regression", type="regression-finding"
    )
    path = f"{PREFIX}/subscriptions/{subscription['id']}"
    response = store_client.delete(path)
    assert response.status_code == 204, response.text
    assert response.content == b""
    logger.info("Cancelled subscription %s", subscription["id"])
    response = store_client.get(f"{PREFIX}/subscriptions")
    assert response.status_code == 200, response.text
    assert response.json() == [remaining]
    assert store_client.delete(path).status_code == 404
    assert (
        store_client.delete(
            f"{PREFIX}/subscriptions/00000000-0000-4000-8000-000000000099"
        ).status_code
        == 404
    )
    assert store_client.delete(f"{PREFIX}/subscriptions/not-a-uuid").status_code == 422

    assert (
        store_client.delete(f"{PREFIX}/subscriptions/{remaining['id']}").status_code
        == 204
    )
    response = store_client.post(
        f"{PREFIX}/store",
        json={
            "ce_id": CE_ID,
            "namespace": "security-detections",
            "key": "item-1",
            "type": "detection-finding",
            "value": {},
        },
    )
    assert response.status_code == 200, response.text
    callback_post.assert_not_called()


def test_subscription_namespace_and_type_select_items(
    store_client: TestClient, callback_post: MagicMock
) -> None:
    subscription = _create_subscription(
        store_client, namespace="CASA", type="tool-call-block"
    )
    for namespace, item_type, matches in [
        ("CASA", "tool-call-block", True),
        ("security-detections", "tool-call-block", False),
        ("CASA", "detection-finding", False),
        ("active-investigations", "regression-finding", False),
        ("global", "tool-call-block", False),
        ("CASA", None, False),
    ]:
        response = store_client.post(
            f"{PREFIX}/store",
            json={
                "ce_id": CE_ID,
                "namespace": namespace,
                "key": "tool-1",
                "type": item_type,
                "value": {"tool": "example-tool", "reason": "blocked"},
            },
        )
        assert response.status_code == 200, response.text
        if matches:
            callback_post.assert_called_once_with(
                subscription["callback_url"],
                json=response.json(),
                timeout=(3, 10),
                allow_redirects=False,
            )
        else:
            callback_post.assert_not_called()
        callback_post.reset_mock()


@pytest.mark.parametrize(
    "changes, filters, matches",
    [
        ({}, {}, True),
        ({"namespace": "security-detections"}, {}, False),
        ({"type": "detection-finding"}, {}, False),
        ({"type": None}, {}, False),
        ({}, {"metadata.mas_id": "mas-1"}, True),
        ({"metadata": {"mas_id": "mas-2"}}, {"metadata.mas_id": "mas-1"}, False),
        ({"metadata": None}, {"metadata.mas_id": "mas-1"}, False),
        (
            {"metadata": {"session_id": "session-1"}},
            {"metadata.mas_id": None, "metadata.session_id": "session-1"},
            True,
        ),
        (
            {"metadata": {"mas_id": "mas-1", "trace_id": "trace-2"}},
            {"metadata.mas_id": "mas-1", "metadata.trace_id": "trace-1"},
            False,
        ),
    ],
)
def test_subscription_matching_combines_all_selectors(
    changes: dict[str, Any], filters: dict[str, str | None], matches: bool
) -> None:
    subscription = Subscription.model_validate(
        {
            "id": CE_ID,
            "namespace": "CASA",
            "type": "tool-call-block",
            "filter": filters,
            "callback_url": "https://subscriber.example/callback",
        }
    )
    item = StoreItem.model_validate(
        {
            "ce_id": CE_ID,
            "namespace": "CASA",
            "key": "tool-1",
            "type": "tool-call-block",
            "value": {"tool": "example-tool", "reason": "blocked"},
            "metadata": {"mas_id": "mas-1"},
            "timestamp": "2026-11-03T00:00:00Z",
            **changes,
        }
    )
    assert subscription.matches(item) is matches


@pytest.mark.parametrize(
    "filters",
    [
        {},
        {"metadata.mas_id": "mas-1"},
        {"metadata.session_id": "session-1"},
        {"metadata.trace_id": "trace-1"},
        {"metadata.span_id": "span-1"},
        {"metadata.mas_id": "mas-1", "metadata.span_id": "span-1"},
    ],
)
def test_subscription_filters_trigger_callbacks(
    store_client: TestClient, callback_post: MagicMock, filters: dict[str, str]
) -> None:
    subscription = _create_subscription(store_client, filter=filters)
    callback_post.assert_not_called()
    payload = {
        "ce_id": CE_ID,
        "namespace": "security-detections",
        "key": "item-1",
        "type": "detection-finding",
        "value": {"score": 0.95},
        "metadata": {
            "mas_id": "mas-1",
            "session_id": "session-1",
            "trace_id": "trace-1",
            "span_id": "span-1",
            "tag": "anomaly",
        },
    }
    for value in ({"score": 0.95}, {"score": 0.5}):
        response = store_client.post(
            f"{PREFIX}/store", json={**payload, "value": value}
        )
        assert response.status_code == 200, response.text
        callback_post.assert_called_once_with(
            subscription["callback_url"],
            json=response.json(),
            timeout=(3, 10),
            allow_redirects=False,
        )
        logger.info(
            "Filters %s triggered callback with:\n%s",
            {
                "namespace": subscription["namespace"],
                "type": subscription["type"],
                **filters,
            },
            json.dumps(response.json(), indent=2),
        )
        callback_post.reset_mock()

    for field in filters:
        mismatching = deepcopy(payload)
        mismatching["metadata"][field.removeprefix("metadata.")] = "other"
        response = store_client.post(f"{PREFIX}/store", json=mismatching)
        assert response.status_code == 200, response.text
        callback_post.assert_not_called()


def test_subscription_filters_are_combined_with_and(
    store_client: TestClient, callback_post: MagicMock
) -> None:
    _create_subscription(
        store_client,
        filter={"metadata.mas_id": "mas-1", "metadata.session_id": "session-1"},
    )
    for namespace, mas_id, session_id in [
        ("active-investigations", "mas-1", "session-1"),
        ("security-detections", "mas-2", "session-1"),
        ("security-detections", "mas-1", "session-2"),
    ]:
        response = store_client.post(
            f"{PREFIX}/store",
            json={
                "ce_id": CE_ID,
                "namespace": namespace,
                "key": "item-1",
                "type": "detection-finding",
                "value": {},
                "metadata": {"mas_id": mas_id, "session_id": session_id},
            },
        )
        assert response.status_code == 200, response.text
        callback_post.assert_not_called()

    response = store_client.post(
        f"{PREFIX}/store",
        json={
            "ce_id": CE_ID,
            "namespace": "security-detections",
            "key": "item-1",
            "type": "detection-finding",
            "value": {},
            "metadata": {"mas_id": "mas-1", "session_id": "session-1"},
        },
    )
    assert response.status_code == 200, response.text
    callback_post.assert_called_once()


def test_callback_failure_preserves_item_and_notifies_next_subscriber(
    store_client: TestClient,
    callback_post: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = _create_subscription(store_client)
    second = _create_subscription(
        store_client,
        callback_url="https://second.example/callback",
    )
    callback_post.side_effect = [
        requests.Timeout("Callback unavailable"),
        callback_post.return_value,
    ]
    response = store_client.post(
        f"{PREFIX}/store",
        json={
            "ce_id": CE_ID,
            "namespace": "security-detections",
            "key": "item-1",
            "type": "detection-finding",
            "value": {},
        },
    )
    assert response.status_code == 200, response.text
    assert callback_post.call_count == 2
    assert callback_post.call_args.args == (second["callback_url"],)
    assert first["id"] in caplog.text
    retrieved = store_client.get(f"{PREFIX}/store")
    assert retrieved.status_code == 200, retrieved.text
    assert retrieved.json() == [response.json()]


@pytest.mark.parametrize(
    "changes",
    [
        {"namespace": ""},
        {"type": ""},
        {"callback_url": "not-a-url"},
        {"callback_url": "file:///tmp/callback"},
        {"filter": {"unknown": "value"}},
        {"filter": {"metadata.tag": "unsupported"}},
        {"filter": {"key": "item-1"}},
        {"filter": {"ce_id": CE_ID}},
        {"filter": {"namespace": "security-detections"}},
        {"filter": {"type": "detection-finding"}},
    ],
)
def test_invalid_subscription_requests(
    store_client: TestClient, changes: dict[str, Any]
) -> None:
    response = store_client.post(
        f"{PREFIX}/subscriptions",
        json={
            "namespace": "security-detections",
            "type": "detection-finding",
            "callback_url": "https://subscriber.example/callback",
            **changes,
        },
    )
    assert response.status_code == 422, response.text
    logger.info("Rejected invalid subscription: %s", response.text)
    listed = store_client.get(f"{PREFIX}/subscriptions")
    assert listed.status_code == 200, listed.text
    assert listed.json() == []
