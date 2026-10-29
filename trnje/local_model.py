"""Bounded, local-only adapter for llama.cpp's llama-cli executable."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModelResult:
    status: str
    output: str = ""
    error: str = ""
    returncode: int | None = None
    elapsed_seconds: float = 0.0


def run_local_model(
    prompt: str,
    *,
    model_path: str | os.PathLike[str] | None = None,
    executable: str | None = None,
    timeout_seconds: int = 120,
    max_tokens: int = 128,
    context_size: int = 1024,
    threads: int = 4,
) -> ModelResult:
    """Run one local inference turn with explicit bounds and no shell.

    Configuration comes from explicit arguments or TRNJE_MODEL_PATH and
    TRNJE_LLAMA_CLI. No remote provider fallback is attempted.
    """
    if not prompt.strip():
        return ModelResult(status="FAIL", error="Prompt must not be empty.")
    if timeout_seconds < 1 or max_tokens < 1 or context_size < 1 or threads < 1:
        return ModelResult(status="FAIL", error="All execution limits must be positive.")

    configured_model = model_path or os.environ.get("TRNJE_MODEL_PATH")
    if not configured_model:
        return ModelResult(
            status="BLOCKED",
            error="Set TRNJE_MODEL_PATH or pass model_path explicitly.",
        )
    model = Path(configured_model).expanduser()
    if not model.is_file():
        return ModelResult(status="BLOCKED", error="Configured model file was not found.")

    configured_executable = executable or os.environ.get("TRNJE_LLAMA_CLI", "llama-cli")
    resolved_executable = shutil.which(configured_executable)
    if resolved_executable is None and Path(configured_executable).is_file():
        resolved_executable = str(Path(configured_executable).resolve())
    if resolved_executable is None:
        return ModelResult(status="BLOCKED", error="llama-cli executable was not found.")

    command = [
        resolved_executable,
        "-m", str(model.resolve()),
        "-p", prompt,
        "-n", str(max_tokens),
        "-c", str(context_size),
        "-t", str(threads),
        "--temp", "0",
        "--single-turn",
        "--no-display-prompt",
    ]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return ModelResult(
            status="BLOCKED",
            error=f"Local inference exceeded the {timeout_seconds}s timeout.",
            elapsed_seconds=time.monotonic() - started,
        )
    except OSError as exc:
        return ModelResult(
            status="FAIL",
            error=f"Could not start local inference ({type(exc).__name__}).",
            elapsed_seconds=time.monotonic() - started,
        )

    elapsed = time.monotonic() - started
    output = completed.stdout.strip()
    if completed.returncode != 0:
        return ModelResult(
            status="FAIL",
            output=output,
            error=f"llama-cli exited with code {completed.returncode}.",
            returncode=completed.returncode,
            elapsed_seconds=elapsed,
        )
    if not output:
        return ModelResult(
            status="FAIL",
            error="llama-cli exited successfully but returned no stdout.",
            returncode=completed.returncode,
            elapsed_seconds=elapsed,
        )
    return ModelResult(
        status="PASS",
        output=output,
        returncode=completed.returncode,
        elapsed_seconds=elapsed,
    )
