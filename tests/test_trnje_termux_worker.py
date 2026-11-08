import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

WORKER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "trnje_termux_worker.py"
SPEC = importlib.util.spec_from_file_location("trnje_termux_worker_tested", WORKER_PATH)
WORKER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(WORKER)


class TrnjeTermuxWorkerTests(unittest.TestCase):
    def test_accepts_allowlisted_diagnostics(self):
        self.assertEqual(
            WORKER.parse('TRNJE_TASK_JSON\n{"kind":"diagnostics","payload":{}}')[0],
            "diagnostics",
        )

    def test_rejects_arbitrary_shell_and_unknown_payload(self):
        for body in (
            'TRNJE_TASK_JSON\n{"kind":"shell","payload":{"cmd":"id"}}',
            'TRNJE_TASK_JSON\n{"kind":"run_tests","payload":{"cmd":"pytest"}}',
        ):
            with self.assertRaises(WORKER.Blocked):
                WORKER.parse(body)

    def test_rejects_duplicate_keys_and_non_string_kind(self):
        for body in (
            'TRNJE_TASK_JSON\n{"kind":"diagnostics","kind":"run_tests","payload":{}}',
            'TRNJE_TASK_JSON\n{"kind":[],"payload":{}}',
        ):
            with self.assertRaises(WORKER.Blocked):
                WORKER.parse(body)

    def test_rejects_path_traversal_sensitive_and_symlink_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env.local").write_text("synthetic-secret", encoding="utf-8")
            (root / "link.txt").symlink_to(root / ".env.local")
            for name in ("../outside.txt", ".env.local", "link.txt"):
                with self.assertRaises(WORKER.Blocked):
                    WORKER.file_in_root(root, name)

    def test_allows_normal_bounded_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "README.md"
            target.write_text("safe", encoding="utf-8")
            self.assertEqual(WORKER.file_in_root(root, "README.md"), target)

    def test_large_file_is_read_in_bounded_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "large.md"
            target.write_bytes(b"a" * (WORKER.MAX_READ_BYTES + 100))
            first = WORKER.execute(root, "read_file", {"path": "large.md"})
            self.assertEqual(first["status"], "PASS")
            self.assertIn("NEXT_OFFSET: 12000", first["output"])
            second = WORKER.execute(
                root, "read_file", {"path": "large.md", "offset": WORKER.MAX_READ_BYTES}
            )
            self.assertEqual(second["status"], "PASS")
            self.assertIn("COMPLETE: true", second["output"])
            self.assertTrue(second["output"].endswith("a" * 100))

    def test_missing_file_has_a_specific_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            result = WORKER.execute(Path(directory), "read_file", {"path": "missing.md"})
            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn("FILE_NOT_FOUND", result["detail"])


    def test_gh_retries_tls_handshake_timeout_then_succeeds(self):
        failed = type("Completed", (), {
            "returncode": 1, "stdout": "",
            "stderr": 'Get "https://api.github.com/...": net/http: TLS handshake timeout',
        })()
        succeeded = type("Completed", (), {
            "returncode": 0, "stdout": '[{"id":1}]', "stderr": "",
        })()
        with patch.object(WORKER.subprocess, "run", side_effect=[failed, failed, succeeded]) as run, \
             patch.object(WORKER.time, "sleep") as sleep:
            result = WORKER.gh(["repos/example"], timeout=45)
        self.assertEqual(result, '[{"id":1}]')
        self.assertEqual(run.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 5])

    def test_gh_stops_after_three_transient_failures(self):
        failed = type("Completed", (), {
            "returncode": 1, "stdout": "",
            "stderr": "net/http: TLS handshake timeout",
        })()
        with patch.object(WORKER.subprocess, "run", return_value=failed) as run, \
             patch.object(WORKER.time, "sleep") as sleep:
            with self.assertRaises(WORKER.Blocked) as raised:
                WORKER.gh(["repos/example"], timeout=45)
        self.assertIn("after 3 attempt", str(raised.exception))
        self.assertIn("retry limit reached", str(raised.exception))
        self.assertEqual(run.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 5])

    def test_gh_does_not_retry_permission_errors(self):
        forbidden = type("Completed", (), {
            "returncode": 1, "stdout": "",
            "stderr": "HTTP 403: Resource not accessible by integration",
        })()
        with patch.object(WORKER.subprocess, "run", return_value=forbidden) as run, \
             patch.object(WORKER.time, "sleep") as sleep:
            with self.assertRaises(WORKER.Blocked) as raised:
                WORKER.gh(["repos/example"])
        self.assertIn("non-retryable error", str(raised.exception))
        run.assert_called_once()
        sleep.assert_not_called()

    def test_gh_retries_http_5xx_but_not_arbitrary_server_errors(self):
        unavailable = type("Completed", (), {
            "returncode": 1, "stdout": "",
            "stderr": "HTTP 503: Service Unavailable",
        })()
        succeeded = type("Completed", (), {
            "returncode": 0, "stdout": "ok", "stderr": "",
        })()
        with patch.object(WORKER.subprocess, "run", side_effect=[unavailable, succeeded]) as run, \
             patch.object(WORKER.time, "sleep") as sleep:
            self.assertEqual(WORKER.gh(["repos/example"]), "ok")
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_gh_retries_process_timeout_with_a_bounded_attempt_count(self):
        succeeded = type("Completed", (), {
            "returncode": 0, "stdout": "ok", "stderr": "",
        })()
        with patch.object(WORKER.subprocess, "run",
                          side_effect=[WORKER.subprocess.TimeoutExpired("gh", 30), succeeded]) as run, \
             patch.object(WORKER.time, "sleep") as sleep:
            self.assertEqual(WORKER.gh(["repos/example"]), "ok")
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_empty_mailbox_poll_updates_heartbeat(self):
        state = {"version": 1, "last_seen": 0, "tasks": {}}
        with patch.object(WORKER, "comments", return_value=[]), \
             patch.object(WORKER, "deliver"), patch.object(WORKER, "save"):
            self.assertEqual(WORKER.once(Path("/tmp"), state), 0)
        self.assertEqual(state["last_poll_status"], "PASS")
        self.assertIsInstance(state["last_poll_at"], int)

    def test_subprocess_disables_shell_and_stdin(self):
        with patch.object(WORKER.subprocess, "run") as mocked:
            mocked.return_value = type(
                "Completed", (), {"returncode": 0, "stdout": "ok", "stderr": ""}
            )()
            self.assertEqual(WORKER.call(["python", "--version"])["status"], "PASS")
            self.assertFalse(mocked.call_args.kwargs["shell"])
            self.assertIs(mocked.call_args.kwargs["stdin"], WORKER.subprocess.DEVNULL)

    def test_redacts_github_token_pattern(self):
        token = "ghp_abcdefghijklmnopqrstuvwxyz123456"
        self.assertNotIn(token, WORKER.clean("token=" + token))

    def test_interrupted_task_is_not_retried(self):
        state = {"version": 1, "last_seen": 0,
                 "tasks": {"42": {"status": "RUNNING", "kind": "run_task"}}}
        WORKER.recover(state)
        record = state["tasks"]["42"]
        self.assertEqual(record["status"], "RESULT_PENDING")
        self.assertEqual(record["task_status"], "UNKNOWN")
        self.assertIn("not retried", record["body"])

    def test_existing_result_comment_prevents_duplicate_post(self):
        body = WORKER.RESULT + "\n" + json.dumps({"source_comment_id": 42})
        state = {"version": 1, "last_seen": 0, "tasks": {
            "42": {"status": "RESULT_PENDING", "task_status": "PASS", "body": body}
        }}
        with patch.object(WORKER, "save"), patch.object(WORKER, "post") as post:
            WORKER.deliver(state, [{"body": body}])
        self.assertEqual(state["tasks"]["42"]["status"], "PASS")
        self.assertNotIn("body", state["tasks"]["42"])
        post.assert_not_called()

    def test_failed_result_post_remains_pending(self):
        body = WORKER.RESULT + "\n" + json.dumps({"source_comment_id": 43})
        state = {"version": 1, "last_seen": 0, "tasks": {
            "43": {"status": "RESULT_PENDING", "task_status": "PASS", "body": body}
        }}
        with patch.object(WORKER, "save"), patch.object(WORKER, "post", return_value=False):
            WORKER.deliver(state, [])
        self.assertEqual(state["tasks"]["43"]["status"], "RESULT_PENDING")
        self.assertEqual(state["tasks"]["43"]["body"], body)

    def test_state_is_atomic_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            state_file = state / "state.json"
            with (patch.object(WORKER, "STATE", state),
                  patch.object(WORKER, "STATE_FILE", state_file),
                  patch.object(WORKER, "LOGS", state / "logs")):
                WORKER.save({"version": 1, "last_seen": 42, "tasks": {}})
            self.assertTrue(state_file.is_file())
            self.assertEqual(json.loads(state_file.read_text())["last_seen"], 42)
            self.assertEqual(state_file.stat().st_mode & 0o777, 0o600)

    def test_git_status_reports_command_failure(self):
        failed = {"status": "FAIL", "exit": 2, "output": "", "detail": "exit"}
        with patch.object(WORKER, "call", return_value=failed):
            self.assertEqual(WORKER.git_status(Path("/tmp/trnje"))["status"], "FAIL")

    def test_diagnostics_does_not_claim_pass_when_git_check_fails(self):
        outcomes = [
            {"status": "PASS", "exit": 0, "output": "main", "detail": ""},
            {"status": "PASS", "exit": 0, "output": "abc", "detail": ""},
            {"status": "FAIL", "exit": 1, "output": "bad", "detail": "exit"},
            {"status": "PASS", "exit": 0, "output": "Python 3", "detail": ""},
        ]
        with patch.object(WORKER, "call", side_effect=outcomes):
            self.assertEqual(WORKER.execute(Path("/tmp/trnje"), "diagnostics", {})["status"], "FAIL")

    def test_run_task_rejects_sensitive_target_before_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("synthetic", encoding="utf-8")
            with patch.object(WORKER, "call") as command:
                result = WORKER.execute(root, "run_task", {"file": ".env", "task": "x", "expect": "y"})
            self.assertEqual(result["status"], "BLOCKED")
            command.assert_not_called()

    def test_run_task_invocation_matches_runner_cli_contract(self):
        import runpy
        import shutil
        import sys
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / "scripts"
            scripts.mkdir()
            runner = scripts / "run_task.py"
            shutil.copy2(Path(__file__).resolve().parents[1] / "scripts" / "run_task.py", runner)
            (root / "sample.py").write_text("value = 1\\n", encoding="utf-8")

            payload = {"file": "sample.py", "task": "change value", "expect": "value = 2"}
            captured = {}
            calls = []

            def fake_run_task(workspace, relative_file, task_description, *,
                              expected_text, model_timeout_seconds, check_timeout_seconds):
                captured.update(
                    workspace=workspace,
                    relative_file=relative_file,
                    task_description=task_description,
                    expected_text=expected_text,
                    model_timeout_seconds=model_timeout_seconds,
                    check_timeout_seconds=check_timeout_seconds,
                )
                return type("Report", (), {
                    "status": "PASS",
                    "as_dict": lambda self: {"status": "PASS"},
                })()

            def fake_call(argv, cwd=None, timeout=60):
                calls.append((list(argv), timeout))
                if len(calls) == 1:
                    previous_argv = sys.argv
                    sys.argv = [argv[1], *argv[2:]]
                    try:
                        with patch("trnje.task.run_task", side_effect=fake_run_task):
                            try:
                                runpy.run_path(argv[1], run_name="__main__")
                            except SystemExit as exc:
                                code = exc.code if isinstance(exc.code, int) else 1
                            else:
                                code = 0
                    finally:
                        sys.argv = previous_argv
                    return {"status": "PASS" if code == 0 else "FAIL",
                            "exit": code, "output": "runner invoked", "detail": ""}
                return {"status": "PASS", "exit": 0, "output": "", "detail": ""}

            with patch.object(WORKER, "call", side_effect=fake_call):
                result = WORKER.execute(root, "run_task", payload)

            self.assertEqual(result["status"], "PASS")
            self.assertEqual(captured["expected_text"], payload["expect"])
            self.assertEqual(captured["model_timeout_seconds"], 300)
            self.assertEqual(captured["check_timeout_seconds"], 180)
            self.assertIn("--expect", calls[0][0])
            self.assertIn(payload["expect"], calls[0][0])
            self.assertEqual(calls[0][1], 600)

    def test_installer_sha256_pin_matches_worker_source(self):
        import hashlib
        import re

        installer = (Path(__file__).resolve().parents[1] /
                     "scripts" / "install_trnje_termux_worker.sh").read_text(encoding="utf-8")
        match = re.search(r'^expected_sha256="([0-9a-f]{64})"$', installer, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), hashlib.sha256(WORKER_PATH.read_bytes()).hexdigest())
        self.assertIn('worker_ref="${TRNJE_WORKER_REF:-main}"', installer)


if __name__ == "__main__":
    unittest.main()
