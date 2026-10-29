from unittest.mock import patch

from trnje.local_model import ModelResult
from trnje.workspace import OperationResult
from trnje.task import _extract_edit, run_task


def test_extract_edit_requires_exact_schema():
    assert _extract_edit('{"old":"a","new":"b","summary":"fix"}') == {
        "old": "a", "new": "b", "summary": "fix"
    }
    assert _extract_edit('{"old":"a","new":"b","summary":"fix","path":"x"}') is None
    assert _extract_edit("not json") is None


def test_task_stops_when_model_does_not_return_edit(tmp_path):
    (tmp_path / "sample.py").write_text("x = 1\n", encoding="utf-8")
    with patch("trnje.task.run_local_model", return_value=ModelResult("PASS", output="not json")):
        result = run_task(tmp_path, "sample.py", "change x")
    assert result.status == "BLOCKED"
    assert result.phase == "proposal"
    assert (tmp_path / "sample.py").read_text(encoding="utf-8") == "x = 1\n"


def test_task_applies_one_edit_and_reports_check_failure(tmp_path):
    (tmp_path / "sample.py").write_text("x = 1\n", encoding="utf-8")
    model_json = '{"old":"x = 1","new":"x = 2","summary":"correct value"}'
    with patch("trnje.task.run_local_model", return_value=ModelResult("PASS", output=model_json)), patch(
        "trnje.task.replace_exact", return_value=OperationResult("PASS", "Applied one unique exact replacement.")
    ) as edit, patch(
        "trnje.task.run_checks", return_value=OperationResult("FAIL", "pytest failed", output="exit=1")
    ):
        result = run_task(tmp_path, "sample.py", "correct value")
    assert result.status == "FAIL"
    assert result.phase == "checks"
    assert result.changed is True
    edit.assert_called_once()
