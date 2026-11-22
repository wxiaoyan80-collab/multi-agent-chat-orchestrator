"""Durable persistence with request idempotency and atomic state transitions.

SQLite is used in explicit-transaction mode so attempt creation and review
decisions are serialized at the database level (not just by an in-process lock).
"""

import json
import sqlite3
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from .errors import NotFound
from .models import Attempt, MultiTriggerRequest


class RequestStore:
    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.isolation_level = None  # explicit BEGIN IMMEDIATE / COMMIT
        self.conn.row_factory = sqlite3.Row
        self._tx_depth = 0
        self._init_schema()

    # ---- reentrant transaction helpers --------------------------------------
    def _begin(self) -> None:
        if self._tx_depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        else:
            self.conn.execute(f"SAVEPOINT sp_{self._tx_depth}")
        self._tx_depth += 1

    def _commit(self) -> None:
        self._tx_depth -= 1
        if self._tx_depth == 0:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute(f"RELEASE sp_{self._tx_depth}")

    def _rollback(self) -> None:
        self._tx_depth -= 1
        if self._tx_depth == 0:
            self.conn.execute("ROLLBACK")
        else:
            self.conn.execute(f"ROLLBACK TO sp_{self._tx_depth}")
            self.conn.execute(f"RELEASE sp_{self._tx_depth}")

    def _init_schema(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotency_index (
                    workspace_id TEXT NOT NULL,
                    initiator_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    body_hash TEXT NOT NULL,
                    PRIMARY KEY (workspace_id, initiator_id, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    downstream_key TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    task_id TEXT,
                    attempt_no INTEGER NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    request_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    PRIMARY KEY (request_id, seq)
                );
                """
            )

    # ---- low level -----------------------------------------------------------
    def _write_request(self, request: MultiTriggerRequest) -> None:
        raw = json.dumps(request.to_dict(), ensure_ascii=False)
        self.conn.execute(
            """
            INSERT INTO requests
                (request_id, idempotency_key, workspace_id, session_id,
                 data, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(request_id) DO UPDATE SET
                data=excluded.data,
                updated_at=excluded.updated_at
            """,
            (
                request.request_id,
                request.idempotency_key,
                request.workspace_id,
                request.session_id,
                raw,
                request.created_at,
                request.updated_at,
            ),
        )
        self.conn.execute(
            "DELETE FROM audit_log WHERE request_id = ?", (request.request_id,)
        )
        for event in request.audit:
            self.conn.execute(
                """
                INSERT INTO audit_log (request_id, seq, timestamp, actor, action, detail)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    event.seq,
                    event.timestamp,
                    event.actor,
                    event.action,
                    json.dumps(event.detail, ensure_ascii=False),
                ),
            )

    @staticmethod
    def _load(row: sqlite3.Row) -> MultiTriggerRequest:
        return MultiTriggerRequest.from_dict(json.loads(row["data"]))

    # ---- public API ----------------------------------------------------------
    def save(self, request: MultiTriggerRequest) -> None:
        with self._lock:
            self._begin()
            try:
                self._write_request(request)
                self._commit()
            except Exception:
                self._rollback()
                raise

    def get_by_id(self, request_id: str) -> Optional[MultiTriggerRequest]:
        with self._lock:
            row = self.conn.execute(
                "SELECT data FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        return self._load(row) if row else None

    def get_idempotency(
        self, workspace_id: str, initiator_id: str, key: str
    ) -> Optional[Tuple[str, str]]:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT request_id, body_hash FROM idempotency_index
                WHERE workspace_id = ? AND initiator_id = ? AND idempotency_key = ?
                """,
                (workspace_id, initiator_id, key),
            ).fetchone()
        return (row["request_id"], row["body_hash"]) if row else None

    def create_request_atomic(
        self, request: MultiTriggerRequest, body_hash: str
    ) -> Optional[MultiTriggerRequest]:
        """Insert request + idempotency index in one transaction.

        Returns an existing request when a concurrent creator won the race.
        """
        with self._lock:
            self._begin()
            try:
                row = self.conn.execute(
                    """
                    SELECT request_id FROM idempotency_index
                    WHERE workspace_id = ? AND initiator_id = ? AND idempotency_key = ?
                    """,
                    (request.workspace_id, request.initiator_id, request.idempotency_key),
                ).fetchone()
                if row:
                    self._rollback()
                    return self.get_by_id(row["request_id"])
                self.conn.execute(
                    """
                    INSERT INTO idempotency_index
                        (workspace_id, initiator_id, idempotency_key, request_id, body_hash)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        request.workspace_id,
                        request.initiator_id,
                        request.idempotency_key,
                        request.request_id,
                        body_hash,
                    ),
                )
                self._write_request(request)
                self._commit()
                return None
            except Exception:
                self._rollback()
                raise

    def get_or_create_attempt(self, attempt: Attempt) -> Attempt:
        """Atomic create. Replaying the same downstream key returns the original
        attempt (and its run_id), so an enqueue-ack loss never double-dispatches."""
        with self._lock:
            self._begin()
            try:
                row = self.conn.execute(
                    "SELECT data FROM attempts WHERE downstream_key = ?",
                    (attempt.downstream_key,),
                ).fetchone()
                if row:
                    self._rollback()
                    return Attempt.from_dict(json.loads(row["data"]))
                self.conn.execute(
                    """
                    INSERT INTO attempts (downstream_key, request_id, task_id, attempt_no, data)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        attempt.downstream_key,
                        attempt.request_id,
                        attempt.task_id,
                        attempt.attempt_no,
                        json.dumps(attempt.__dict__, ensure_ascii=False),
                    ),
                )
                self._commit()
                return attempt
            except Exception:
                self._rollback()
                raise

    def update_attempt(self, attempt: Attempt) -> None:
        with self._lock:
            self._begin()
            try:
                self.conn.execute(
                    "UPDATE attempts SET data = ? WHERE downstream_key = ?",
                    (
                        json.dumps(attempt.__dict__, ensure_ascii=False),
                        attempt.downstream_key,
                    ),
                )
                self._commit()
            except Exception:
                self._rollback()
                raise

    def update_atomic(
        self, request_id: str, mutator: Callable[[MultiTriggerRequest], Any]
    ) -> Any:
        """Serialize a read-modify-write on one request across threads/processes."""
        with self._lock:
            self._begin()
            try:
                row = self.conn.execute(
                    "SELECT data FROM requests WHERE request_id = ?", (request_id,)
                ).fetchone()
                if not row:
                    raise NotFound(f"request {request_id} not found")
                request = self._load(row)
                result = mutator(request)
                self._write_request(request)
                self._commit()
                return result
            except Exception:
                self._rollback()
                raise

    def get_audit(self, request_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT seq, timestamp, actor, action, detail FROM audit_log
                WHERE request_id = ? ORDER BY seq
                """,
                (request_id,),
            ).fetchall()
        return [
            {
                "seq": row["seq"],
                "ts": row["timestamp"],
                "actor": row["actor"],
                "action": row["action"],
                "data": json.loads(row["detail"]),
            }
            for row in rows
        ]

    def close(self) -> None:
        with self._lock:
            self.conn.close()
