#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Workers that write the same nodes at the same time must not lose data to a Neo4j deadlock."""

from __future__ import annotations

import logging

import pytest
from neo4j.exceptions import ClientError, Neo4jError, TransientError

from scope.client import dal
from scope.connectors import neo4j as connector_module
from scope.connectors.neo4j import (
    TRANSIENT_ATTEMPTS,
    TRANSIENT_WARN_FROM_ATTEMPT,
    Neo4JConnector,
)
from scope.core.exceptions import DatabaseError

DEADLOCK = "Neo.TransientError.Transaction.DeadlockDetected"
INDEX_EXISTS = "Neo.ClientError.Schema.EquivalentSchemaRuleAlreadyExists"


def _error(code: str) -> Neo4jError:
    """The exception that the driver raises for a server error code (its factory is named differently by version)."""
    for name in ("hydrate", "_hydrate_neo4j"):
        factory = getattr(Neo4jError, name, None)
        if factory is not None:
            return factory(code=code, message=f"{code} (test)")
    raise RuntimeError("this neo4j driver has no way to build an error from a code")


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def __iter__(self):
        return iter(self.rows)

    def consume(self):
        return None


class FakeRecord:
    def __init__(self, data):
        self._data = data

    def data(self):
        return self._data


class FakeSession:
    """Fails with ``errors`` (the first calls), then answers."""

    def __init__(self, errors, rows):
        self.errors, self.rows, self.calls = list(errors), rows, 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, parameters=None):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return FakeResult([FakeRecord(r) for r in self.rows])


class FakeDriver:
    def __init__(self, session):
        self._session = session

    def session(self, database=None):
        return self._session


@pytest.fixture
def sleeps(monkeypatch):
    waited: list[float] = []
    monkeypatch.setattr(connector_module._time, "sleep", waited.append)
    return waited


def _connector(errors=(), rows=({"n": 1},)):
    session = FakeSession(errors, list(rows))
    connector = Neo4JConnector()
    connector._client = FakeDriver(session)
    connector.ensure_connected = lambda: None
    return connector, session


def test_a_deadlock_is_retried_until_the_write_goes_through(sleeps) -> None:
    connector, session = _connector([_error(DEADLOCK), _error(DEADLOCK)])

    connector.execute_command("MERGE (n:A {id: 1})")

    assert session.calls == 3 and len(sleeps) == 2


def test_a_read_is_retried_too(sleeps) -> None:
    connector, session = _connector([_error(DEADLOCK)], rows=[{"n": 7}])

    assert connector.execute("MATCH (n) RETURN n") == [{"n": 7}]
    assert session.calls == 2


def test_it_gives_up_after_the_last_attempt(sleeps) -> None:
    connector, session = _connector([_error(DEADLOCK)] * (TRANSIENT_ATTEMPTS + 3))

    with pytest.raises(TransientError):
        connector.execute_command("MERGE (n:A {id: 1})")

    assert session.calls == TRANSIENT_ATTEMPTS and len(sleeps) == TRANSIENT_ATTEMPTS - 1


def test_the_wait_grows_with_each_attempt(sleeps) -> None:
    connector, _ = _connector([_error(DEADLOCK)] * (TRANSIENT_ATTEMPTS - 1))

    connector.execute_command("MERGE (n:A {id: 1})")

    assert sleeps == sorted(sleeps) or sleeps[-1] > sleeps[0]
    assert all(0 < wait < 10 for wait in sleeps)


def test_the_first_retries_are_routine_and_the_later_ones_a_warning(
    sleeps, caplog
) -> None:
    connector, _ = _connector([_error(DEADLOCK)] * (TRANSIENT_ATTEMPTS - 1))

    with caplog.at_level(logging.INFO, logger="scope.connectors.neo4j"):
        connector.execute_command("MERGE (n:A {id: 1})")

    levels = [r.levelno for r in caplog.records if "transient error" in r.getMessage()]
    assert levels == [
        logging.WARNING if attempt >= TRANSIENT_WARN_FROM_ATTEMPT else logging.INFO
        for attempt in range(1, TRANSIENT_ATTEMPTS)
    ]
    assert logging.INFO in levels and logging.WARNING in levels


def test_an_error_that_is_not_transient_is_not_retried(sleeps) -> None:
    connector, session = _connector([_error("Neo.ClientError.Statement.SyntaxError")])

    with pytest.raises(ClientError):
        connector.execute_command("MERGE (")

    assert session.calls == 1 and not sleeps


# ---------------------------------------------------------------- the index created by two sessions at once


class FakeDb:
    def __init__(self, index_error=None):
        self.index_error, self.commands = index_error, []

    def execute_command(self, command, params=None):
        self.commands.append(command)
        if command.startswith("CREATE INDEX") and self.index_error:
            raise self.index_error

    def execute(self, query, params=None):
        return []


@pytest.fixture(autouse=True)
def _forget_indexes():
    dal._indexed_node_types.clear()
    yield
    dal._indexed_node_types.clear()


KG = ([{"id": "n1", "node_type": "Session"}], [])


def test_an_index_that_another_session_just_created_is_not_an_error() -> None:
    db = FakeDb(index_error=_error(INDEX_EXISTS))

    assert dal.ingest_normalized_kg(db, *KG) is True

    assert any(c.startswith("MERGE") or "MERGE" in c for c in db.commands)
    assert "Session" in dal._indexed_node_types


def test_another_error_on_the_index_still_fails() -> None:
    db = FakeDb(index_error=_error("Neo.ClientError.Security.Forbidden"))

    with pytest.raises(DatabaseError, match="ensure index"):
        dal.ingest_normalized_kg(db, *KG)
