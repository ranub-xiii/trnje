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
    with patch(
        "trnje.task.run_local_model",
        return_value=ModelResult("PASS", output="not json", returncode=0, elapsed_seconds=1.25),
    ):
        result = run_task(tmp_path, "sample.py", "change x", expected_text="x = 2")
    assert result.status == "BLOCKED"
    assert result.phase == "proposal"
    metadata = result.as_dict()["model"]
    assert metadata == {
        "status": "PASS",
        "returncode": 0,
        "elapsed_seconds": 1.25,
        "output_bytes": 8,
        "error": "",
        "rejection_reason": "invalid_edit_json",
    }
    assert "output" not in metadata
    assert "not json" not in str(metadata)
    assert (tmp_path / "sample.py").read_text(encoding="utf-8") == "x = 1\n"


def test_task_rejects_reported_edit_without_content_change(tmp_path):
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n", encoding="utf-8")
    model_json = '{"old":"x = 1","new":"x = 2","summary":"correct value"}'

    with patch("trnje.task.run_local_model", return_value=ModelResult("PASS", output=model_json)), patch(
        "trnje.task.replace_exact",
        return_value=OperationResult("PASS", "Applied one unique exact replacement."),
    ), patch("trnje.task.run_checks") as checks:
        result = run_task(tmp_path, "sample.py", "correct value", expected_text="x = 2")

    assert result.status == "FAIL"
    assert result.phase == "edit_verify"
    assert result.changed is False
    assert target.read_text(encoding="utf-8") == "x = 1\n"
    checks.assert_not_called()


def test_task_applies_one_edit_and_reports_check_failure(tmp_path):
    target = tmp_path / "sample.py"
    target.write_text("x = 1\\n", encoding="utf-8")
    target.chmod(0o600)
    model_json = '{"old":"x = 1","new":"x = 2","summary":"correct value"}'
    with patch("trnje.task.run_local_model", return_value=ModelResult("PASS", output=model_json)), patch(
        "trnje.task.run_checks", return_value=OperationResult("FAIL", "pytest failed", output="exit=1")
    ):
        result = run_task(tmp_path, "sample.py", "correct value", expected_text="x = 2")
    assert result.status == "FAIL"
    assert result.phase == "checks"
    assert result.detail == "Checks failed; original content was restored and verified."
    assert result.checks == "exit=1"
    assert result.changed is False
    assert target.read_text(encoding="utf-8") == "x = 1\\n"
    assert target.stat().st_mode & 0o777 == 0o600


def test_task_rejects_edit_that_misses_acceptance_text(tmp_path):
    target = tmp_path / "sample.py"
    target.write_text("x = 1\\n", encoding="utf-8")
    model_json = '{"old":"x = 1","new":"x = 3","summary":"change value"}'

    with patch(
        "trnje.task.run_local_model",
        return_value=ModelResult("PASS", output=model_json),
    ), patch("trnje.task.run_checks") as checks:
        result = run_task(
            tmp_path,
            "sample.py",
            "change x to 2",
            expected_text="x = 2",
        )

    assert result.status == "FAIL"
    assert result.phase == "acceptance"
    assert result.changed is False
    assert "restored and verified" in result.detail
    assert target.read_text(encoding="utf-8") == "x = 1\\n"
    checks.assert_not_called()


def test_task_rolls_back_edit_that_misses_acceptance_text(tmp_path):
    target = tmp_path / "sample.py"
    original = "x = 1\n"
    target.write_text(original, encoding="utf-8")
    model_json = '{"old":"x = 1","new":"x = 3","summary":"wrong value"}'

    with patch(
        "trnje.task.run_local_model",
        return_value=ModelResult("PASS", output=model_json),
    ), patch("trnje.task.run_checks") as checks:
        result = run_task(
            tmp_path, "sample.py", "change x to 2", expected_text="x = 2"
        )

    assert result.status == "FAIL"
    assert result.phase == "acceptance"
    assert "restored and verified" in result.detail
    assert result.changed is False
    assert target.read_text(encoding="utf-8") == original
    checks.assert_not_called()


def test_task_reports_rollback_failure_without_claiming_restoration(tmp_path):
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n", encoding="utf-8")
    model_json = '{"old":"x = 1","new":"x = 3","summary":"wrong value"}'

    with patch(
        "trnje.task.run_local_model",
        return_value=ModelResult("PASS", output=model_json),
    ), patch(
        "trnje.task.restore_if_unchanged",
        return_value=OperationResult("BLOCKED", "simulated rollback refusal"),
    ), patch("trnje.task.run_checks") as checks:
        result = run_task(
            tmp_path, "sample.py", "change x to 2", expected_text="x = 2"
        )

    assert result.status == "FAIL"
    assert result.phase == "rollback"
    assert "could not be confirmed" in result.detail
    assert result.changed is True
    assert target.read_text(encoding="utf-8") == "x = 3\n"
    checks.assert_not_called()
def test_task_reports_check_failure_when_rollback_is_blocked(tmp_path):
    target = tmp_path / "sample.py"
    target.write_text("x = 1\\n", encoding="utf-8")
    model_json = '{"old":"x = 1","new":"x = 2","summary":"correct value"}'
    with patch("trnje.task.run_local_model", return_value=ModelResult("PASS", output=model_json)), patch(
        "trnje.task.run_checks", return_value=OperationResult("FAIL", "pytest failed", output="exit=1")
    ), patch(
        "trnje.task.restore_if_unchanged",
        return_value=OperationResult("BLOCKED", "simulated rollback refusal"),
    ):
        result = run_task(tmp_path, "sample.py", "correct value", expected_text="x = 2")
    assert result.status == "FAIL"
    assert result.phase == "rollback"
    assert "could not be confirmed" in result.detail
    assert result.checks == "exit=1"
    assert result.changed is True
    assert target.read_text(encoding="utf-8") == "x = 2\\n"



def test_task_rolls_back_when_post_edit_inspection_is_blocked(tmp_path):
    from trnje.task import run_task as execute_task
    from trnje.workspace import inspect_file as real_inspect_file

    target = tmp_path / "sample.py"
    original = "x = 1\\n"
    target.write_text(original, encoding="utf-8")
    model_json = '{"old":"x = 1","new":"x = 2","summary":"correct value"}'
    calls = 0

    def blocked_second_inspection(workspace, relative_file, *, max_bytes=20000):
        nonlocal calls
        calls += 1
        if calls == 2:
            return OperationResult("BLOCKED", "simulated post-edit read failure")
        return real_inspect_file(workspace, relative_file, max_bytes=max_bytes)

    with patch(
        "trnje.task.run_local_model",
        return_value=ModelResult("PASS", output=model_json),
    ), patch("trnje.task.inspect_file", side_effect=blocked_second_inspection), patch(
        "trnje.task.run_checks"
    ) as checks:
        result = execute_task(
            tmp_path, "sample.py", "change x to 2", expected_text="x = 2"
        )

    assert result.status == "BLOCKED"
    assert result.phase == "edit_verify"
    assert result.changed is False
    assert "restored and verified" in result.detail
    assert target.read_text(encoding="utf-8") == original
    checks.assert_not_called()


def test_task_does_not_rollback_over_concurrent_change_after_blocked_inspection(tmp_path):
    from trnje.task import run_task as execute_task
    from trnje.workspace import inspect_file as real_inspect_file

    target = tmp_path / "sample.py"
    target.write_text("x = 1\\n", encoding="utf-8")
    model_json = '{"old":"x = 1","new":"x = 2","summary":"correct value"}'
    calls = 0

    def blocked_after_concurrent_write(workspace, relative_file, *, max_bytes=20000):
        nonlocal calls
        calls += 1
        if calls == 2:
            target.write_text("external change\\n", encoding="utf-8")
            return OperationResult("BLOCKED", "simulated post-edit read failure")
        return real_inspect_file(workspace, relative_file, max_bytes=max_bytes)

    with patch(
        "trnje.task.run_local_model",
        return_value=ModelResult("PASS", output=model_json),
    ), patch("trnje.task.inspect_file", side_effect=blocked_after_concurrent_write), patch(
        "trnje.task.run_checks"
    ) as checks:
        result = execute_task(
            tmp_path, "sample.py", "change x to 2", expected_text="x = 2"
        )

    assert result.status == "FAIL"
    assert result.phase == "rollback"
    assert result.changed is True
    assert "could not be confirmed" in result.detail
    assert target.read_text(encoding="utf-8") == "external change\\n"
    checks.assert_not_called()


def test_task_rejects_preexisting_acceptance_text_before_model_call(tmp_path):
    target = tmp_path / "sample.py"
    original = "x = 2\\n# unrelated value\\n"
    target.write_text(original, encoding="utf-8")

    with patch("trnje.task.run_local_model") as model, patch(
        "trnje.task.run_checks"
    ) as checks:
        result = run_task(
            tmp_path,
            "sample.py",
            "change another value",
            expected_text="x = 2",
        )

    assert result.status == "FAIL"
    assert result.phase == "acceptance"
    assert result.changed is False
    assert "already existed before the edit" in result.detail
    assert target.read_text(encoding="utf-8") == original
    model.assert_not_called()
    checks.assert_not_called()
