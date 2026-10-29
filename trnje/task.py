"""One-file, one-edit task loop. Deliberately no retries or arbitrary commands."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from trnje.local_model import run_local_model
from trnje.workspace import inspect_file, replace_exact, run_checks


@dataclass(frozen=True)
class TaskReport:
    status: str
    phase: str
    detail: str
    file: str
    summary: str = ""
    checks: str = ""
    changed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "phase": self.phase,
            "detail": self.detail,
            "file": self.file,
            "summary": self.summary,
            "checks": self.checks,
            "changed": self.changed,
        }


def _extract_edit(output: str) -> dict[str, str] | None:
    """Accept a JSON object only; tolerate a short model preamble, not code fences."""
    start, end = output.find("{"), output.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(output[start : end + 1])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict) or set(value) != {"old", "new", "summary"}:
        return None
    if not all(isinstance(value[key], str) for key in ("old", "new", "summary")):
        return None
    if not value["old"] or not value["summary"].strip():
        return None
    return value


def run_task(
    workspace: str | Path,
    relative_file: str,
    task_description: str,
    *,
    model_timeout_seconds: int = 120,
    check_timeout_seconds: int = 120,
) -> TaskReport:
    """Inspect one user-selected file, request one exact edit, and verify once."""
    if not task_description.strip():
        return TaskReport("FAIL", "input", "Task description must not be empty.", relative_file)
    inspected = inspect_file(workspace, relative_file, max_bytes=12000)
    if inspected.status != "PASS":
        return TaskReport(inspected.status, "inspect", inspected.detail, relative_file)

    prompt = (
        "You are proposing one minimal source-code edit. Return exactly one JSON object "
        'with string keys "old", "new", and "summary". "old" must be a non-empty exact '
        "substring of the supplied file, occurring once. Do not include a file path, "
        "commands, markdown fences, or any other keys. Do not claim tests were run. "
        "Make only the smallest change that addresses the task.\n\n"
        f"TASK:\n{task_description[:2000]}\n\n"
        f"FILE: {relative_file}\nFILE CONTENT:\n{inspected.output}"
    )
    proposed = run_local_model(prompt, timeout_seconds=model_timeout_seconds, max_tokens=512, context_size=4096)
    if proposed.status != "PASS":
        return TaskReport(proposed.status, "model", proposed.error, relative_file)
    edit = _extract_edit(proposed.output)
    if edit is None:
        return TaskReport("BLOCKED", "proposal", "Model did not return the required edit JSON.", relative_file)
    applied = replace_exact(workspace, relative_file, edit["old"], edit["new"], max_file_bytes=20000)
    if applied.status != "PASS":
        return TaskReport(applied.status, "edit", applied.detail, relative_file, edit["summary"])

    checks = run_checks(workspace, timeout_seconds=check_timeout_seconds)
    return TaskReport(
        checks.status,
        "verify" if checks.status == "PASS" else "checks",
        checks.detail,
        relative_file,
        summary=edit["summary"],
        checks=checks.output,
        changed=True,
    )
