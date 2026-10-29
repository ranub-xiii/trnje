from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import patch

from trnje.local_model import run_local_model


def test_missing_model_is_blocked(tmp_path):
    result = run_local_model("hello", model_path=tmp_path / "missing.gguf")
    assert result.status == "BLOCKED"


def test_empty_prompt_is_rejected(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"fixture")
    result = run_local_model(" ", model_path=model)
    assert result.status == "FAIL"


def test_success_is_bounded_and_uses_no_shell(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"fixture")
    completed = CompletedProcess(args=[], returncode=0, stdout="TRNJE_LOCAL_MODEL_OK\n", stderr="")
    with patch("trnje.local_model.shutil.which", return_value="/usr/bin/llama-cli"), patch(
        "trnje.local_model.subprocess.run", return_value=completed
    ) as run:
        result = run_local_model("say OK", model_path=model)
    assert result.status == "PASS"
    assert result.output == "TRNJE_LOCAL_MODEL_OK"
    assert run.call_args.kwargs["shell"] is False
    assert run.call_args.kwargs["timeout"] == 120


def test_nonzero_exit_is_failure(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"fixture")
    completed = CompletedProcess(args=[], returncode=2, stdout="", stderr="error")
    with patch("trnje.local_model.shutil.which", return_value="/usr/bin/llama-cli"), patch(
        "trnje.local_model.subprocess.run", return_value=completed
    ):
        result = run_local_model("hello", model_path=model)
    assert result.status == "FAIL"
    assert result.returncode == 2


def test_timeout_is_blocked(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"fixture")
    with patch("trnje.local_model.shutil.which", return_value="/usr/bin/llama-cli"), patch(
        "trnje.local_model.subprocess.run", side_effect=TimeoutExpired(cmd="llama-cli", timeout=120)
    ):
        result = run_local_model("hello", model_path=model)
    assert result.status == "BLOCKED"
