#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path
from unittest.mock import Mock

from click.testing import CliRunner

from norm_worker import replay

LOGS = Path(__file__).resolve().parents[3] / "norm/tests/fixtures/harness_codex_logs.jsonl"


def test_replay_previews_without_write(monkeypatch):
    persist = Mock()
    monkeypatch.setattr(replay, "_persist", persist)
    result = CliRunner().invoke(replay.main, ["--capture-id", "test", "--logs", str(LOGS)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert not data["written"] and data["nodes"] and data["edges"]
    assert "Synthetic tool output" not in result.output
    persist.assert_not_called()


def test_replay_write_failure_is_nonzero_and_content_free(monkeypatch):
    persist = Mock(side_effect=RuntimeError("Synthetic tool output"))
    monkeypatch.setattr(replay, "_persist", persist)
    result = CliRunner().invoke(
        replay.main, ["--capture-id", "test", "--logs", str(LOGS), "--include-content", "--write"]
    )
    persist.assert_called_once()
    assert result.exit_code == 1
    assert "KG write failed" in result.output
    assert "Synthetic tool output" not in result.output


def test_replay_rejects_malformed_and_empty_capture(tmp_path):
    path = tmp_path / "logs.jsonl"
    args = ["--capture-id", "test", "--logs", str(path)]
    path.write_text('{"Body":"private input"}\nnot-json private input', encoding="utf-8")
    result = CliRunner().invoke(replay.main, args)
    assert result.exit_code == 1 and "line 2" in result.output
    assert "private input" not in result.output
    path.write_text('{"data":[{"ServiceName":"muse","Body":"ignored"}]}', encoding="utf-8")
    result = CliRunner().invoke(replay.main, args)
    assert result.exit_code == 1 and "no normalizable rows" in result.output
