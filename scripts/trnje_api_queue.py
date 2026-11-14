"""Durable, idempotent inbox for direct MCP tasks.

The SQLite file is separate from the legacy GitHub worker state.json. It is
owned by the same single worker process; HTTP threads only enqueue/read.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class QueueError(RuntimeError):
    """Base queue failure."""


class IdempotencyConflict(QueueError):
    """An idempotency key was reused for a different request."""


class QueueFull(QueueError):
    """The bounded durable queue reached its configured capacity."""


class TaskQueue:
    SCHEMA_VERSION = 1
    MAX_TOTAL_TASKS = 1000
    MAX_PENDING_TASKS = 100
    TERMINAL = frozenset({"PASS", "FAIL", "BLOCKED", "UNKNOWN"})

    def __init__(self, database: str | os.PathLike[str]) -> None:
        self.database = Path(database).expanduser()
        self.database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.database.is_symlink():
            raise QueueError("Queue database must not be a symlink.")
        if self.database.exists() and not self.database.is_file():
            raise QueueError("Queue database path is not a regular file.")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.database), timeout=5.0, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > self.SCHEMA_VERSION:
                raise QueueError("Queue schema is newer than this worker supports.")
            if version == 0:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS tasks (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT NOT NULL UNIQUE,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        request_sha256 TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN ('QUEUED','RUNNING','PASS','FAIL','BLOCKED','UNKNOWN')
                        ),
                        created_at INTEGER NOT NULL,
                        started_at INTEGER,
                        completed_at INTEGER,
                        result_json TEXT,
                        exit_code INTEGER,
                        evidence_path TEXT,
                        evidence_sha256 TEXT
                    );
                    CREATE INDEX IF NOT EXISTS tasks_status_sequence
                        ON tasks(status, sequence);
                    PRAGMA user_version = 1;
                    COMMIT;
                    """
                )
        try:
            os.chmod(self.database, 0o600)
        except OSError as exc:
            raise QueueError("Could not restrict queue database permissions.") from exc

    @staticmethod
    def _canonical(value: Any) -> str:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    @staticmethod
    def validate_idempotency_key(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9._:-]{1,128}", value
        ):
            raise ValueError(
                "Idempotency-Key must contain 1-128 ASCII letters, digits, '.', '_', ':' or '-'."
            )
        return value

    def enqueue(
        self, kind: str, payload: dict[str, Any], idempotency_key: str
    ) -> tuple[dict[str, Any], bool]:
        key = self.validate_idempotency_key(idempotency_key)
        if not isinstance(kind, str) or not kind:
            raise ValueError("Task kind must be a non-empty string.")
        if not isinstance(payload, dict):
            raise ValueError("Task payload must be an object.")
        payload_json = self._canonical(payload)
        request_hash = hashlib.sha256(
            (kind + "\n" + payload_json).encode("utf-8")
        ).hexdigest()
        task_id = str(uuid.uuid4())
        now = int(time.time())

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM tasks WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != request_hash:
                    connection.execute("ROLLBACK")
                    raise IdempotencyConflict(
                        "Idempotency-Key was already used for a different request."
                    )
                connection.execute("COMMIT")
                return self._row_to_dict(existing), True

            pending = int(connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE status IN ('QUEUED','RUNNING')"
            ).fetchone()[0])
            total = int(connection.execute(
                "SELECT COUNT(*) FROM tasks"
            ).fetchone()[0])
            if pending >= self.MAX_PENDING_TASKS or total >= self.MAX_TOTAL_TASKS:
                connection.execute("ROLLBACK")
                raise QueueFull(
                    "TRNJE MCP queue is full; inspect existing tasks before submitting more."
                )

            connection.execute(
                """
                INSERT INTO tasks (
                    task_id, idempotency_key, request_sha256, kind, payload_json,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'QUEUED', ?)
                """,
                (task_id, key, request_hash, kind, payload_json, now),
            )
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            connection.execute("COMMIT")
            return self._row_to_dict(row), False

    def claim_next(self) -> dict[str, Any] | None:
        now = int(time.time())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM tasks WHERE status = 'QUEUED'
                ORDER BY sequence LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            changed = connection.execute(
                """
                UPDATE tasks SET status = 'RUNNING', started_at = ?
                WHERE task_id = ? AND status = 'QUEUED'
                """,
                (now, row["task_id"]),
            ).rowcount
            if changed != 1:
                connection.execute("ROLLBACK")
                return None
            claimed = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (row["task_id"],)
            ).fetchone()
            connection.execute("COMMIT")
            return self._row_to_dict(claimed, include_payload=True)

    def complete(
        self,
        task_id: str,
        result: dict[str, Any],
        *,
        evidence_path: str,
        evidence_sha256: str,
    ) -> dict[str, Any]:
        if not isinstance(result, dict) or result.get("status") not in self.TERMINAL:
            raise ValueError("Task result must have a recognized terminal status.")
        result_json = self._canonical(result)
        exit_code = result.get("exit")
        if exit_code is not None and type(exit_code) is not int:
            exit_code = None
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE tasks
                SET status = ?, completed_at = ?, result_json = ?, exit_code = ?,
                    evidence_path = ?, evidence_sha256 = ?
                WHERE task_id = ? AND status = 'RUNNING'
                """,
                (
                    result["status"], int(time.time()), result_json, exit_code,
                    evidence_path, evidence_sha256, task_id,
                ),
            ).rowcount
            if changed != 1:
                connection.execute("ROLLBACK")
                raise QueueError("Task was not in RUNNING state; result was not accepted.")
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            connection.execute("COMMIT")
            return self._row_to_dict(row)

    def recover_interrupted(self) -> int:
        """Mark in-flight jobs UNKNOWN. Never automatically replay after restart."""
        now = int(time.time())
        result = self._canonical({
            "status": "UNKNOWN",
            "detail": "Worker restarted while this task was RUNNING; execution was not retried. Inspect workspace before resubmission.",
            "exit": None,
        })
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute(
                """
                UPDATE tasks
                SET status = 'UNKNOWN', completed_at = ?, result_json = ?, exit_code = NULL
                WHERE status = 'RUNNING'
                """,
                (now, result),
            ).rowcount
            connection.execute("COMMIT")
            return int(count)

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            return self._row_to_dict(row) if row is not None else None

    def list_recent(self, limit: int = 10) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("Task list limit must be between 1 and 20.")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks ORDER BY sequence DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._row_to_dict(row) for row in rows]

    def pending_count(self) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM tasks WHERE status IN ('QUEUED','RUNNING')"
            ).fetchone()
            return int(row["count"])

    @classmethod
    def _row_to_dict(
        cls, row: sqlite3.Row, *, include_payload: bool = False
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "task_id": row["task_id"],
            "kind": row["kind"],
            "status": row["status"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "exit_code": row["exit_code"],
            "evidence_path": row["evidence_path"],
            "evidence_sha256": row["evidence_sha256"],
        }
        if row["result_json"] is not None:
            result["result"] = json.loads(row["result_json"])
        if include_payload:
            result["payload"] = json.loads(row["payload_json"])
        return result
