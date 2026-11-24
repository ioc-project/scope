from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from scope.api import cognition_fabric, create_app
from scope.api.api_v1.endpoints import applications
from scope.client.mock.client import GoldenClient


@pytest.mark.parametrize("enabled", [None, "false", "true"])
def test_cognition_fabric_flag_controls_routes_and_postgres(monkeypatch, enabled):
    if enabled is None:
        monkeypatch.delenv("SCOPE_ENABLE_COGNITION_FABRIC_ENDPOINTS", raising=False)
    else:
        monkeypatch.setenv("SCOPE_ENABLE_COGNITION_FABRIC_ENDPOINTS", enabled)
    monkeypatch.setattr("scope.api._ensure_tables", Mock())
    monkeypatch.setattr("scope.api._check_redis", Mock())
    db = Mock()
    postgres = Mock(side_effect=lambda: iter((db,)))
    monkeypatch.setattr(cognition_fabric, "get_postgres_db", postgres)

    with TestClient(create_app()) as client:
        paths = client.get("/openapi.json").json()["paths"]
        operations = {
            (path, method)
            for path, methods in paths.items()
            if path.startswith(("/api/v1/realtime/", "/api/v1/cognition-store/"))
            for method in methods
        }
        assert "/api/v1/sessions/{session_id}/conversation" in paths
        if enabled == "true":
            assert len(operations) == 11
            postgres.assert_called_once()
            assert db.execute_command.call_count == 3
        else:
            assert not operations
            postgres.assert_not_called()
            assert client.get("/api/v1/cognition-store/info").status_code == 404
            assert client.get("/api/v1/realtime/events/" + "a" * 64).status_code == 404


def test_cognition_fabric_application_details_preserves_legacy_traces(
    cognition_fabric_app,
):
    details = GoldenClient().get_application_details("application-1")
    cognition_fabric_app.dependency_overrides[applications.application_details] = (
        lambda: details.model_dump()
    )

    response = TestClient(cognition_fabric_app).get(
        "/api/v1/applications/application-1"
    )

    assert response.status_code == 200
    assert response.json() == {
        **details.model_dump(mode="json"),
        "traces": {"value": 0, "unit": "SCALAR"},
    }
