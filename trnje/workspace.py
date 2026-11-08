"""Small, bounded workspace operations for the Trnje prototype.

No shell is used for file operations. Edits require one exact, unique match and
must stay inside the configured workspace. This module does not commit changes.
"""
from __future__ import annotations

import os
import stat
import subprocess
import fcntl
import hashlib
import tempfile
import time
from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OperationResult:
    status: str
    detail: str = ""
    output: str = ""
    returncode: int | None = None
    mode: int | None = None


@contextmanager
def workspace_lock(
    workspace: str | os.PathLike[str],
    *,
    timeout_seconds: float = 5.0,
) -> Iterator[None]:
    """Serialize cooperating task runners with a bounded lock wait."""
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0 < timeout_seconds <= 60
    ):
        raise ValueError("Lock timeout must be greater than 0 and at most 60 seconds.")
    root = Path(workspace).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Workspace must be a directory.")
    lock_dir = Path(tempfile.gettempdir()) / "trnje-locks"
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256(os.fsencode(str(root))).hexdigest()
    lock_path = lock_dir / f"{digest}.lock"
    deadline = time.monotonic() + timeout_seconds
    with lock_path.open("a+b") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Workspace lock acquisition timed out after {timeout_seconds:g} seconds."
                    )
                time.sleep(min(0.05, remaining))
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
        target_stat = target.stat()
        if target_stat.st_size > max_bytes:
            return OperationResult("BLOCKED", f"File exceeds the {max_bytes}-byte read limit.")
        return OperationResult(
            "PASS",
            output=target.read_text(encoding="utf-8"),
            mode=target_stat.st_mode & 0o777,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        return OperationResult("BLOCKED", f"Inspection refused ({type(exc).__name__}).")


def _replace_exact_unlocked(
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
    if old == new:
        return OperationResult("FAIL", "Replacement would not change file content.")
    try:
        root, target = _workspace_file(workspace, relative_path)
        if not target.is_file():
            return OperationResult("BLOCKED", "Target is not an existing regular file.")
        original_stat = target.stat()
        if original_stat.st_size > max_file_bytes:
            return OperationResult("BLOCKED", f"File exceeds the {max_file_bytes}-byte edit limit.")
        original_mode = original_stat.st_mode & 0o777
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
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(updated)
                handle.flush()
                os.fchmod(handle.fileno(), original_mode)
                os.fsync(handle.fileno())
            os.replace(temp, target)
        finally:
            if temp.exists():
                temp.unlink()
        return OperationResult("PASS", "Applied one unique exact replacement.")
    except (OSError, UnicodeError, ValueError) as exc:
        return OperationResult("BLOCKED", f"Edit refused ({type(exc).__name__}).")



def _restore_if_unchanged_unlocked(
    workspace: str | os.PathLike[str],
    relative_path: str,
    expected_current: str,
    original_content: str,
    *,
    original_mode: int | None = None,
    max_file_bytes: int = 20000,
) -> OperationResult:
    """Restore original content only if the file still matches the inspected edit."""
    try:
        root, target = _workspace_file(workspace, relative_path)
        if not target.is_file():
            return OperationResult("BLOCKED", "Rollback target is not a regular file.")
        current_stat = target.stat()
        if current_stat.st_size > max_file_bytes:
            return OperationResult("BLOCKED", "Rollback target exceeds the size limit.")
        restore_mode = current_stat.st_mode & 0o777 if original_mode is None else original_mode
        if not isinstance(restore_mode, int) or not 0 <= restore_mode <= 0o777:
            return OperationResult("FAIL", "Original file mode must contain only permission bits.")
        current = target.read_text(encoding="utf-8")
        if current != expected_current:
            return OperationResult(
                "BLOCKED",
                "Rollback refused because the file changed after verification.",
            )
        if len(original_content.encode("utf-8")) > max_file_bytes:
            return OperationResult("BLOCKED", "Original content exceeds the size limit.")
        if target.is_symlink() or not target.resolve(strict=False).is_relative_to(root):
            return OperationResult("BLOCKED", "Rollback target escaped the workspace.")
        temp = target.with_name(f".{target.name}.trnje-tmp")
        if temp.exists() or temp.is_symlink():
            return OperationResult("BLOCKED", "Rollback temporary path already exists.")
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(original_content)
                handle.flush()
                os.fchmod(handle.fileno(), restore_mode)
                os.fsync(handle.fileno())
            os.replace(temp, target)
        finally:
            if temp.exists():
                temp.unlink()
        return OperationResult("PASS", "Original content restored.")
    except (OSError, UnicodeError, ValueError) as exc:
        return OperationResult("BLOCKED", f"Rollback refused ({type(exc).__name__}).")


def replace_exact(workspace: str | os.PathLike[str], relative_path: str, old: str, new: str, *, max_file_bytes: int=20000) -> OperationResult:
    """Run the operation while holding the workspace lock."""
    try:
        with workspace_lock(workspace):
            return _replace_exact_unlocked(workspace=workspace, relative_path=relative_path, old=old, new=new, max_file_bytes=max_file_bytes)
    except (OSError, ValueError) as exc:
        return OperationResult("BLOCKED", f"Edit refused ({type(exc).__name__}).")


def restore_if_unchanged(workspace: str | os.PathLike[str], relative_path: str, expected_current: str, original_content: str, *, original_mode: int | None = None, max_file_bytes: int=20000) -> OperationResult:
    """Run the operation while holding the workspace lock."""
    try:
        with workspace_lock(workspace):
            return _restore_if_unchanged_unlocked(workspace=workspace, relative_path=relative_path, expected_current=expected_current, original_content=original_content, original_mode=original_mode, max_file_bytes=max_file_bytes)
    except (OSError, ValueError) as exc:
        return OperationResult("BLOCKED", f"Rollback refused ({type(exc).__name__}).")

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

