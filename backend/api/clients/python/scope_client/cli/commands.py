# Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Curated CLI commands. Each one wraps a generated endpoint module's ``sync_detailed``."""

import argparse
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from ..api.health import health_check_healthz_get
from ..api.v1 import (
    cancel_subscription_api_v1_cognition_store_subscriptions_id_delete,
    compute_metrics_api_v1_metrics_compute_post,
    create_store_item_api_v1_cognition_store_store_post,
    create_subscription_api_v1_cognition_store_subscriptions_post,
    features_info_api_v1_features_info_get,
    get_applications_api_v1_ui_applications_get,
    get_cognition_store_info_api_v1_cognition_store_info_get,
    get_metrics_catalog_api_v1_metrics_catalog_get,
    get_session_spans_api_v1_sessions_session_id_spans_get,
    healthz_api_v1_healthz_get,
    list_sessions_api_v1_sessions_get,
    list_store_items_api_v1_cognition_store_store_get,
    list_subscriptions_api_v1_cognition_store_subscriptions_get,
)
from ..client import AuthenticatedClient, Client
from ..models.metrics_compute_request import MetricsComputeRequest
from ..models.store_item_request import StoreItemRequest
from ..models.store_metadata import StoreMetadata
from ..models.subscription_filter import SubscriptionFilter
from ..models.subscription_request import SubscriptionRequest
from ..types import UNSET, Response


@dataclass(frozen=True)
class Command:
    help: str
    run: Callable[[AuthenticatedClient | Client, argparse.Namespace], Response[Any]]
    add_arguments: Callable[[argparse.ArgumentParser], None] = lambda parser: None


def _paging(parser: argparse.ArgumentParser, limit: int) -> None:
    parser.add_argument("--limit", type=int, default=limit)
    parser.add_argument("--offset", type=int, default=0)


def _health(client: AuthenticatedClient | Client, args: argparse.Namespace) -> Response[Any]:
    module = healthz_api_v1_healthz_get if args.v1 else health_check_healthz_get
    return module.sync_detailed(client=client)


def _health_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--v1", action="store_true", help="Probe /api/v1/healthz instead of /healthz")


def _sessions(client: AuthenticatedClient | Client, args: argparse.Namespace) -> Response[Any]:
    return list_sessions_api_v1_sessions_get.sync_detailed(
        client=client,
        start_time=args.start_time or UNSET,
        end_time=args.end_time or UNSET,
        limit=args.limit,
        offset=args.offset,
    )


def _sessions_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start-time")
    parser.add_argument("--end-time")
    _paging(parser, 50)


def _applications(client: AuthenticatedClient | Client, args: argparse.Namespace) -> Response[Any]:
    return get_applications_api_v1_ui_applications_get.sync_detailed(
        client=client,
        start_time=args.start_time or "",
        end_time=args.end_time or "",
        limit=args.limit,
        offset=args.offset,
    )


def _session_spans(client: AuthenticatedClient | Client, args: argparse.Namespace) -> Response[Any]:
    return get_session_spans_api_v1_sessions_session_id_spans_get.sync_detailed(
        args.session_id,
        client=client,
        span_types=args.span_type or UNSET,
        limit=args.limit,
        offset=args.offset,
        order=args.order,
    )


def _session_spans_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("session_id")
    parser.add_argument("--span-type", action="append", help="Filter by span type (repeatable)")
    parser.add_argument("--order", choices=["asc", "desc"], default="asc")
    _paging(parser, 500)


def _metrics_compute(client: AuthenticatedClient | Client, args: argparse.Namespace) -> Response[Any]:
    body = MetricsComputeRequest(
        session_ids=args.session_id,
        metric_ids=args.metric or UNSET,
        recursive=args.recursive,
    )
    return compute_metrics_api_v1_metrics_compute_post.sync_detailed(client=client, body=body)


def _metrics_compute_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session-id", action="append", required=True, help="Session ID (repeatable)")
    parser.add_argument("--metric", action="append", help="Metric ID (repeatable); omit for all")
    parser.add_argument("--recursive", action="store_true", help="Also compute metrics for child spans")


def _ids(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mas-id")
    parser.add_argument("--session-id")
    parser.add_argument("--trace-id")
    parser.add_argument("--span-id")


def _store_list(client: AuthenticatedClient | Client, args: argparse.Namespace) -> Response[Any]:
    return list_store_items_api_v1_cognition_store_store_get.sync_detailed(
        client=client,
        key=args.key or UNSET,
        ce_id=UUID(args.ce_id) if args.ce_id else UNSET,
        namespace=args.namespace or UNSET,
        type_=args.type or UNSET,
        metadata_mas_id=args.mas_id or UNSET,
        metadata_session_id=args.session_id or UNSET,
        metadata_trace_id=args.trace_id or UNSET,
        metadata_span_id=args.span_id or UNSET,
        metadata_tag=args.tag or UNSET,
    )


def _store_list_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--key")
    parser.add_argument("--ce-id", help="CE instance UUID")
    parser.add_argument("--namespace")
    parser.add_argument("--type")
    parser.add_argument("--tag")
    _ids(parser)


def _store_put(client: AuthenticatedClient | Client, args: argparse.Namespace) -> Response[Any]:
    text = args.value
    if text.startswith("@"):
        with open(text[1:], encoding="utf-8") as f:
            text = f.read()
    metadata = StoreMetadata(
        mas_id=args.mas_id or UNSET,
        session_id=args.session_id or UNSET,
        trace_id=args.trace_id or UNSET,
        span_id=args.span_id or UNSET,
        tag=args.tag or UNSET,
    )
    body = StoreItemRequest(
        ce_id=UUID(args.ce_id),
        namespace=args.namespace,
        key=args.key,
        value=json.loads(text),
        type_=args.type or UNSET,
        metadata=metadata if any([args.mas_id, args.session_id, args.trace_id, args.span_id, args.tag]) else UNSET,
    )
    return create_store_item_api_v1_cognition_store_store_post.sync_detailed(client=client, body=body)


def _store_put_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ce-id", required=True, help="CE instance UUID")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--value", required=True, help="JSON value, or @file to read it from a file")
    parser.add_argument("--type")
    parser.add_argument("--tag")
    _ids(parser)


def _subscriptions(client: AuthenticatedClient | Client, args: argparse.Namespace) -> Response[Any]:
    return list_subscriptions_api_v1_cognition_store_subscriptions_get.sync_detailed(
        client=client, namespace=args.namespace or UNSET
    )


def _subscriptions_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--namespace")


def _subscribe(client: AuthenticatedClient | Client, args: argparse.Namespace) -> Response[Any]:
    body = SubscriptionRequest(
        namespace=args.namespace,
        type_=args.type,
        callback_url=args.callback_url,
        filter_=SubscriptionFilter(
            metadata_mas_id=args.mas_id or UNSET,
            metadata_session_id=args.session_id or UNSET,
            metadata_trace_id=args.trace_id or UNSET,
            metadata_span_id=args.span_id or UNSET,
        ),
    )
    return create_subscription_api_v1_cognition_store_subscriptions_post.sync_detailed(client=client, body=body)


def _subscribe_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--type", required=True, help="Store-item value type to match")
    parser.add_argument("--callback-url", required=True)
    _ids(parser)


def _unsubscribe(client: AuthenticatedClient | Client, args: argparse.Namespace) -> Response[Any]:
    return cancel_subscription_api_v1_cognition_store_subscriptions_id_delete.sync_detailed(
        UUID(args.id), client=client
    )


def _unsubscribe_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("id", help="Subscription UUID")


COMMANDS: dict[str, Command] = {
    "health": Command("Liveness probe (/healthz)", _health, _health_args),
    "features-info": Command(
        "Describe the features endpoint", lambda c, a: features_info_api_v1_features_info_get.sync_detailed(client=c)
    ),
    "metrics-catalog": Command(
        "List discoverable metrics", lambda c, a: get_metrics_catalog_api_v1_metrics_catalog_get.sync_detailed(client=c)
    ),
    "metrics-compute": Command("Compute metrics for sessions", _metrics_compute, _metrics_compute_args),
    "sessions": Command("List sessions", _sessions, _sessions_args),
    "session-spans": Command("List spans of a session", _session_spans, _session_spans_args),
    "applications": Command(
        "List known applications",
        _applications,
        _sessions_args,
    ),
    "cognition-info": Command(
        "Describe the cognition store (/api/v1/cognition-store/info)",
        lambda c, a: get_cognition_store_info_api_v1_cognition_store_info_get.sync_detailed(client=c),
    ),
    "store-list": Command("List cognition-store items", _store_list, _store_list_args),
    "store-put": Command("Create or update a cognition-store item", _store_put, _store_put_args),
    "subscriptions": Command("List cognition-store subscriptions", _subscriptions, _subscriptions_args),
    "subscribe": Command("Register a cognition-store callback subscription", _subscribe, _subscribe_args),
    "unsubscribe": Command("Cancel a cognition-store subscription", _unsubscribe, _unsubscribe_args),
}
