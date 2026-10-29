"""Small, bounded workspace operations for the Trnje prototype.

No shell is used for file operations. Edits require one exact, unique match and
must stay inside the configured workspace. This module does not commit changes.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OperationResult:
    status: str
    detail: str = ""
    output: str = ""
    returncode: int | None = None


def _workspace_file(workspace: str | os.PathLike[str], relative_path: str) -> tuple[Path, Path]:
    root = Path(workspace).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Workspace must be a directory.")
    candidate = Path(relative_path)
    if candidate.is_absolute() or not relative_path or any(part in ("", ".", "..") for part in candidate.parts):
        raise ValueError("Path must be a clean relative path inside the workspace.")
    target = root / candidate
    # Reject symlinks in every existing component, including the final file.
    current = root
    for part in candidate.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Symlink paths are not allowed.")
    resolved = target.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError("Path escapes the configured workspace.")
    return root, target


def inspect_file(workspace: str | os.PathLike[str], relative_path: str, *, max_bytes: int = 20000) -> OperationResult:
    """Read a bounded UTF-8 text file within the workspace."""
    try:
        _, target = _workspace_file(workspace, relative_path)
        if not target.is_file():
            return OperationResult("BLOCKED", "Target is not an existing regular file.")
        if target.stat().st_size > max_bytes:
            return OperationResult("BLOCKED", f"File exceeds the {max_bytes}-byte read limit.")
        return OperationResult("PASS", output=target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        return OperationResult("BLOCKED", f"Inspection refused ({type(exc).__name__}).")


def replace_exact(
    workspace: str | os.PathLike[str],
    relative_path: str,
    old: str,
    new: str,
    *,
    max_file_bytes: int = 20000,
) -> OperationResult:
    """Apply one exact replacement only when the old text occurs exactly once."""
    if not old:
        return OperationResult("FAIL", "The old text must not be empty.")
    try:
        root, target = _workspace_file(workspace, relative_path)
        if not target.is_file():
            return OperationResult("BLOCKED", "Target is not an existing regular file.")
        if target.stat().st_size > max_file_bytes:
            return OperationResult("BLOCKED", f"File exceeds the {max_file_bytes}-byte edit limit.")
        original = target.read_text(encoding="utf-8")
        occurrences = original.count(old)
        if occurrences != 1:
            return OperationResult("BLOCKED", f"Expected one exact match; found {occurrences}.")
        updated = original.replace(old, new, 1)
        encoded = updated.encode("utf-8")
        if len(encoded) > max_file_bytes:
            return OperationResult("BLOCKED", f"Edited file would exceed the {max_file_bytes}-byte limit.")
        # Re-check the resolved parent immediately before writing. Never follow a
        # symlinked target, and use a same-directory temporary file for atomic replace.
        if target.is_symlink() or not target.resolve(strict=False).is_relative_to(root):
            return OperationResult("BLOCKED", "Target changed or escaped the workspace.")
        temp = target.with_name(f".{target.name}.trnje-tmp")
        if temp.exists() or temp.is_symlink():
            return OperationResult("BLOCKED", "Temporary path already exists.")
        try:
            with temp.open("x", encoding="utf-8", newline="") as handle:
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
        finally:
            if temp.exists():
                temp.unlink()
        return OperationResult("PASS", "Applied one unique exact replacement.")
    except (OSError, UnicodeError, ValueError) as exc:
        return OperationResult("BLOCKED", f"Edit refused ({type(exc).__name__}).")


def run_checks(
    workspace: str | os.PathLike[str],
    *,
    timeout_seconds: int = 120,
) -> OperationResult:
    """Run the two fixed repository checks; callers cannot supply shell commands."""
    try:
        root = Path(workspace).resolve(strict=True)
        if not root.is_dir():
            return OperationResult("BLOCKED", "Workspace must be a directory.")
        if timeout_seconds < 1 or timeout_seconds > 600:
            return OperationResult("FAIL", "Timeout must be between 1 and 600 seconds.")
        commands = (
            ("python", "-m", "pytest", "-q"),
            ("python", "-m", "compileall", "-q", "trnje", "scripts", "tests"),
        )
        outputs: list[str] = []
        for command in commands:
            try:
                completed = subprocess.run(
                    command,
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    check=False,
                    shell=False,
                )
            except subprocess.TimeoutExpired:
                return OperationResult(
                    "BLOCKED",
                    f"Check timed out after {timeout_seconds}s: {' '.join(command)}",
                    output="\n".join(outputs),
                )
            output = (completed.stdout + completed.stderr).strip()
            outputs.append(f"$ {' '.join(command)}\nexit={completed.returncode}\n{output}")
            if completed.returncode != 0:
                return OperationResult(
                    "FAIL",
                    f"Check failed: {' '.join(command)}",
                    output="\n\n".join(outputs),
                    returncode=completed.returncode,
                )
        return OperationResult("PASS", "Both fixed checks passed.", output="\n\n".join(outputs), returncode=0)
    except OSError as exc:
        return OperationResult("BLOCKED", f"Could not run checks ({type(exc).__name__}).")
