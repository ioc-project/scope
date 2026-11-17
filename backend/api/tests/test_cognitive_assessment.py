# Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0
from unittest.mock import Mock

import pytest

from scope.client.failures import write_cognitive_assessment
from scope.core.exceptions import DatabaseError


def test_assessment_uses_session_id_and_parameterized_coverage():
    db = Mock()
    write_cognitive_assessment(
        db,
        "session-1",
        "incomplete",
        "Evidence pending",
        {"required": 10, "reviewed": 8},
    )
    query, params = db.execute.call_args.args
    assert "sessionId:$session" in query
    assert "cognitiveAssessmentStatus=$status" in query
    assert params == {
        "session": "session-1",
        "status": "incomplete",
        "reason": "Evidence pending",
        "required": 10,
        "reviewed": 8,
    }


@pytest.mark.parametrize(
    "session,status", [("", "complete"), ("s", "clean"), (None, "complete")]
)
def test_invalid_state_is_rejected_before_database_access(session, status):
    db = Mock()
    with pytest.raises(ValueError):
        write_cognitive_assessment(db, session, status)
    db.execute.assert_not_called()


def test_database_failures_are_not_silently_treated_as_complete():
    db = Mock()
    db.execute.side_effect = RuntimeError("unavailable")
    with pytest.raises(DatabaseError):
        write_cognitive_assessment(db, "s", "complete")
