"""One-file, one-edit task loop. Deliberately no retries or arbitrary commands."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from trnje.local_model import run_local_model
from trnje.workspace import inspect_file, _replace_exact_unlocked as replace_exact, _restore_if_unchanged_unlocked as restore_if_unchanged, run_checks, workspace_lock


@dataclass(frozen=True)
class TaskReport:
    status: str
    phase: str
    detail: str
    file: str
    summary: str = ""
    checks: str = ""
    changed: bool = False
    model: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "phase": self.phase,
            "detail": self.detail,
            "file": self.file,
            "summary": self.summary,
            "checks": self.checks,
            "changed": self.changed,
            "model": self.model,
        }


def _extract_edit(output: str, source_text: str | None = None) -> dict[str, str] | None:
    """Extract the first valid, uniquely applicable JSON edit."""
    decoder = json.JSONDecoder()

    for index, char in enumerate(output):
        if char != "{":
            continue

        try:
            value, _ = decoder.raw_decode(output[index:])
        except (json.JSONDecodeError, TypeError):
            continue

        if not isinstance(value, dict):
            continue
        if set(value) != {"old", "new", "summary"}:
            continue
        if not all(
            isinstance(value[key], str)
            for key in ("old", "new", "summary")
        ):
            continue

        old = value["old"]
        if not old or not value["summary"].strip():
            continue
        if source_text is not None and source_text.count(old) != 1:
            continue

        return value

    return None


def _run_task_locked(
    workspace: str | Path,
    relative_file: str,
    task_description: str,
    *,
    expected_text: str,
    model_timeout_seconds: int = 10800,
    check_timeout_seconds: int = 120,
) -> TaskReport:
    """Inspect one user-selected file, request one exact edit, and verify once."""
    if not task_description.strip():
        return TaskReport("FAIL", "input", "Task description must not be empty.", relative_file)
    if not expected_text.strip():
        return TaskReport("FAIL", "input", "Expected text must not be empty.", relative_file)
    inspected = inspect_file(workspace, relative_file, max_bytes=12000)
    if inspected.status != "PASS":
        return TaskReport(inspected.status, "inspect", inspected.detail, relative_file)

    # Existing text cannot prove that this task produced the requested result.
    if expected_text in inspected.output:
        return TaskReport(
            "FAIL",
            "acceptance",
            "Expected text already existed before the edit; it cannot prove this task made the required change.",
            relative_file,
        )

    prompt = (
        "You are proposing one minimal source-code edit. Return exactly one JSON object "
        'with string keys "old", "new", and "summary". "old" must be a non-empty exact '
        "substring of the supplied file, occurring once. If the task requests replacing "
        "an entire line, the old value MUST be the complete original line and the new "
        "value MUST be the complete replacement line, including its prefix. Never change "
        "only a value token when the task requires changing the whole line. Do not include "
        "a file path, commands, markdown fences, or any other keys. Do not claim tests were "
        "run. Make only the smallest change that fully satisfies the task.\n\n"
        f"TASK:\n{task_description[:2000]}\n\n"
        f"ACCEPTANCE CONDITION: the final file must contain this exact text:\n{expected_text[:2000]}\n\n"
        f"FILE: {relative_file}\nFILE CONTENT:\n{inspected.output}"
    )
    proposed = run_local_model(prompt, timeout_seconds=model_timeout_seconds, max_tokens=512, context_size=4096)
    model_metadata: dict[str, object] = {
        "status": proposed.status,
        "returncode": proposed.returncode,
        "elapsed_seconds": round(proposed.elapsed_seconds, 3),
        "output_bytes": len(proposed.output.encode("utf-8")),
        "error": proposed.error[:300],
    }
    if proposed.status != "PASS" or proposed.output.find("Model output preview:") != -1:
        rejection_reason = "inference_failed" if proposed.status != "PASS" else "output_preview_guard"
        model_metadata["rejection_reason"] = rejection_reason
        return TaskReport(
            proposed.status if proposed.status != "PASS" else "BLOCKED",
            "model",
            proposed.error or "Model output was rejected by the proposal guard.",
            relative_file,
            model=model_metadata,
        )
    edit = _extract_edit(proposed.output, inspected.output)
    if edit is None:
        model_metadata["rejection_reason"] = "invalid_edit_json"
        return TaskReport(
            "BLOCKED",
            "proposal",
            "Model did not return the required edit JSON.",
            relative_file,
            model=model_metadata,
        )
    applied = replace_exact(workspace, relative_file, edit["old"], edit["new"], max_file_bytes=20000)
    if applied.status != "PASS":
        return TaskReport(applied.status, "edit", applied.detail, relative_file, edit["summary"])

    verified = inspect_file(workspace, relative_file, max_bytes=20000)
    if verified.status != "PASS":
        # We know the exact content our atomic replacement should have produced.
        # Roll back only if the current file still equals that content; this
        # refuses to overwrite any concurrent, non-cooperating writer.
        expected_edited = inspected.output.replace(edit["old"], edit["new"], 1)
        rollback_limit = max(
            20000,
            len(expected_edited.encode("utf-8")),
            len(inspected.output.encode("utf-8")),
        )
        rollback = restore_if_unchanged(
            workspace,
            relative_file,
            expected_current=expected_edited,
            original_content=inspected.output,
            original_mode=inspected.mode,
            max_file_bytes=rollback_limit,
        )
        if rollback.status != "PASS":
            return TaskReport(
                "FAIL",
                "rollback",
                "Post-edit inspection was blocked; guarded rollback could not be confirmed: "
                + rollback.detail,
                relative_file,
                summary=edit["summary"],
                changed=True,
            )
        restored = inspect_file(workspace, relative_file, max_bytes=rollback_limit)
        if restored.status != "PASS" or restored.output != inspected.output:
            detail = restored.detail if restored.status != "PASS" else "Restored content differs from the original."
            return TaskReport(
                "FAIL",
                "rollback",
                "Post-edit inspection was blocked; rollback verification failed: " + detail,
                relative_file,
                summary=edit["summary"],
                changed=True,
            )
        return TaskReport(
            "BLOCKED",
            "edit_verify",
            "Post-edit inspection was blocked; original content was restored and verified: "
            + verified.detail,
            relative_file,
            summary=edit["summary"],
            changed=False,
        )
    if verified.output == inspected.output:
        return TaskReport(
            "FAIL",
            "edit_verify",
            "Replacement reported success, but file content did not change.",
            relative_file,
            summary=edit["summary"],
            changed=False,
        )

    if expected_text not in verified.output:
        rollback = restore_if_unchanged(
            workspace,
            relative_file,
            expected_current=verified.output,
            original_content=inspected.output,
            original_mode=inspected.mode,
            max_file_bytes=20000,
        )
        if rollback.status != "PASS":
            return TaskReport(
                "FAIL",
                "rollback",
                "Acceptance failed; rollback could not be confirmed: " + rollback.detail,
                relative_file,
                summary=edit["summary"],
                changed=True,
            )
        restored = inspect_file(workspace, relative_file, max_bytes=20000)
        if restored.status != "PASS" or restored.output != inspected.output:
            detail = restored.detail if restored.status != "PASS" else "Restored content differs from the original."
            return TaskReport(
                "FAIL",
                "rollback",
                "Acceptance failed; rollback verification failed: " + detail,
                relative_file,
                summary=edit["summary"],
                changed=True,
            )
        return TaskReport(
            "FAIL",
            "acceptance",
            "Edited file did not satisfy acceptance; original content was restored and verified.",
            relative_file,
            summary=edit["summary"],
            changed=False,
        )

    checks = run_checks(workspace, timeout_seconds=check_timeout_seconds)
    if checks.status != "PASS":
        rollback = restore_if_unchanged(
            workspace, relative_file,
            expected_current=verified.output,
            original_content=inspected.output,
            original_mode=inspected.mode,
            max_file_bytes=20000,
        )
        if rollback.status != "PASS":
            return TaskReport(
                "FAIL", "rollback",
                "Checks failed; rollback could not be confirmed: " + rollback.detail,
                relative_file, summary=edit["summary"],
                checks=checks.output, changed=True,
            )
        restored = inspect_file(workspace, relative_file, max_bytes=20000)
        if restored.status != "PASS" or restored.output != inspected.output:
            detail = restored.detail if restored.status != "PASS" else "Restored content differs from the original."
            return TaskReport(
                "FAIL", "rollback",
                "Checks failed; rollback verification failed: " + detail,
                relative_file, summary=edit["summary"],
                checks=checks.output, changed=True,
            )
        return TaskReport(
            "FAIL", "checks",
            "Checks failed; original content was restored and verified.",
            relative_file, summary=edit["summary"],
            checks=checks.output, changed=False,
        )
    return TaskReport(
        checks.status, "verify", checks.detail, relative_file,
        summary=edit["summary"], checks=checks.output, changed=True,
    )



def run_task(
    workspace: str | Path,
    relative_file: str,
    task_description: str,
    *,
    expected_text: str,
    model_timeout_seconds: int = 10800,
    check_timeout_seconds: int = 120,
    lock_timeout_seconds: float = 5.0,
) -> TaskReport:
    """Run a task while holding the workspace-wide cooperative lock."""
    lock = workspace_lock(workspace, timeout_seconds=lock_timeout_seconds)
    try:
        lock.__enter__()
    except (OSError, ValueError) as exc:
        return TaskReport(
            "BLOCKED",
            "lock",
            f"Could not acquire workspace lock: {exc}",
            relative_file,
        )

    try:
        return _run_task_locked(
            workspace,
            relative_file,
            task_description,
            expected_text=expected_text,
            model_timeout_seconds=model_timeout_seconds,
            check_timeout_seconds=check_timeout_seconds,
        )
    finally:
        lock.__exit__(None, None, None)

