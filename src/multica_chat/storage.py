"""Durable persistence with request idempotency.

SQLite keeps request records and a separate append-only audit trail so state
and audit survive a service restart. The unique index on ``idempotency_key``
makes client resends converge on one request record.
"""

import json
import sqlite3
import threading
from typing import Dict, List, Optional

from .models import MultiTriggerRequest


class RequestStore:
    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    workspace_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_log (
                    request_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    PRIMARY KEY (request_id, seq)
                )
                """
            )
            self.conn.commit()

    def save(self, request: MultiTriggerRequest) -> None:
        raw = json.dumps(request.to_dict(), ensure_ascii=False)
        with self._lock:
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
            # Rewrite this request's audit rows so re-saving never duplicates them.
            self.conn.execute("DELETE FROM audit_log WHERE request_id = ?", (request.request_id,))
            for seq, event in enumerate(request.audit):
                self.conn.execute(
                    """
                    INSERT INTO audit_log (request_id, seq, timestamp, actor, action, detail)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request.request_id,
                        seq,
                        event.timestamp,
                        event.actor,
                        event.action,
                        json.dumps(event.detail, ensure_ascii=False),
                    ),
                )
            self.conn.commit()

    def get_by_id(self, request_id: str) -> Optional[MultiTriggerRequest]:
        with self._lock:
            row = self.conn.execute(
                "SELECT data FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        return MultiTriggerRequest.from_dict(json.loads(row["data"])) if row else None

    def get_by_idempotency_key(self, key: str) -> Optional[MultiTriggerRequest]:
        with self._lock:
            row = self.conn.execute(
                "SELECT data FROM requests WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return MultiTriggerRequest.from_dict(json.loads(row["data"])) if row else None

    def get_audit(self, request_id: str) -> List[Dict[str, object]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT timestamp, actor, action, detail FROM audit_log
                WHERE request_id = ? ORDER BY seq
                """,
                (request_id,),
            ).fetchall()
        return [
            {
                "timestamp": row["timestamp"],
                "actor": row["actor"],
                "action": row["action"],
                "detail": json.loads(row["detail"]),
            }
            for row in rows
        ]

    def close(self) -> None:
        with self._lock:
            self.conn.close()
