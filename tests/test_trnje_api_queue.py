import concurrent.futures
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hashlib
import json
import os

import pytest

from trnje_api_queue import IdempotencyConflict, QueueError, QueueFull, TaskQueue


def test_queue_persists_task_and_idempotent_retry(tmp_path):
    database = tmp_path / "state" / "mcp-tasks.sqlite3"
    queue = TaskQueue(database)
    first, duplicate = queue.enqueue(
        "diagnostics", {}, "request-001"
    )
    reopened = TaskQueue(database)
    second, duplicate_again = reopened.enqueue(
        "diagnostics", {}, "request-001"
    )

    assert duplicate is False
    assert duplicate_again is True
    assert first["task_id"] == second["task_id"]
    assert second["status"] == "QUEUED"
    assert reopened.pending_count() == 1


def test_same_idempotency_key_cannot_change_request(tmp_path):
    queue = TaskQueue(tmp_path / "queue.sqlite3")
    queue.enqueue("diagnostics", {}, "same-key")

    with pytest.raises(IdempotencyConflict):
        queue.enqueue("git_status", {}, "same-key")


def test_claim_and_completion_are_durable_and_ordered(tmp_path):
    queue = TaskQueue(tmp_path / "queue.sqlite3")
    first, _ = queue.enqueue("diagnostics", {}, "one")
    second, _ = queue.enqueue("git_status", {}, "two")

    claimed = queue.claim_next()
    assert claimed["task_id"] == first["task_id"]
    assert claimed["status"] == "RUNNING"
    assert claimed["payload"] == {}
    assert queue.pending_count() == 2

    digest = hashlib.sha256(b"evidence").hexdigest()
    done = queue.complete(
        first["task_id"],
        {"status": "PASS", "detail": "ok", "exit": 0},
        evidence_path="api-task.md",
        evidence_sha256=digest,
    )
    assert done["status"] == "PASS"
    assert done["evidence_sha256"] == digest
    assert queue.get(first["task_id"])["result"]["detail"] == "ok"

    next_job = queue.claim_next()
    assert next_job["task_id"] == second["task_id"]


def test_restart_marks_running_unknown_and_never_requeues(tmp_path):
    queue = TaskQueue(tmp_path / "queue.sqlite3")
    record, _ = queue.enqueue("run_task", {"file": "a.py"}, "crash")
    queue.claim_next()

    assert queue.recover_interrupted() == 1
    recovered = queue.get(record["task_id"])
    assert recovered["status"] == "UNKNOWN"
    assert "not retried" in recovered["result"]["detail"].lower()
    assert queue.claim_next() is None
    assert queue.recover_interrupted() == 0


def test_completion_refuses_non_running_or_invalid_status(tmp_path):
    queue = TaskQueue(tmp_path / "queue.sqlite3")
    record, _ = queue.enqueue("diagnostics", {}, "not-running")

    with pytest.raises(QueueError):
        queue.complete(
            record["task_id"], {"status": "PASS", "exit": 0},
            evidence_path="x", evidence_sha256="0" * 64,
        )
    queue.claim_next()
    with pytest.raises(ValueError):
        queue.complete(
            record["task_id"], {"status": "QUEUED"},
            evidence_path="x", evidence_sha256="0" * 64,
        )


def test_idempotency_key_is_bounded_and_ascii(tmp_path):
    queue = TaskQueue(tmp_path / "queue.sqlite3")
    for invalid in ("", "contains space", "č", "x" * 129, "slash/key"):
        with pytest.raises(ValueError):
            queue.enqueue("diagnostics", {}, invalid)


def test_queue_applies_pending_and_total_capacity_without_deleting_evidence(tmp_path):
    queue = TaskQueue(tmp_path / "queue.sqlite3")
    queue.MAX_PENDING_TASKS = 1
    first, _ = queue.enqueue("diagnostics", {}, "pending-1")
    with pytest.raises(QueueFull):
        queue.enqueue("diagnostics", {}, "pending-2")
    queue.claim_next()
    queue.complete(
        first["task_id"], {"status": "PASS", "exit": 0},
        evidence_path="evidence.md", evidence_sha256="b" * 64,
    )
    queue.MAX_TOTAL_TASKS = 1
    with pytest.raises(QueueFull):
        queue.enqueue("diagnostics", {}, "total-2")
    assert queue.get(first["task_id"])["evidence_sha256"] == "b" * 64


def test_database_permissions_are_private(tmp_path):
    queue = TaskQueue(tmp_path / "private" / "queue.sqlite3")
    assert queue.database.stat().st_mode & 0o777 == 0o600
    assert queue.pending_count() == 0


def test_concurrent_retries_create_one_task(tmp_path):
    queue = TaskQueue(tmp_path / "queue.sqlite3")

    def submit():
        return queue.enqueue("diagnostics", {}, "concurrent-key")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: submit(), range(8)))

    ids = {record["task_id"] for record, _ in results}
    assert len(ids) == 1
    assert sum(not duplicate for _, duplicate in results) == 1
    assert queue.pending_count() == 1
