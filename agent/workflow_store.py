"""SQLite-backed review/fix checkpoints for durable human approval."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, Optional

from agent import settings


class WorkflowStore:
    """Persist review snapshots and fix plans without coupling the mainline to LangGraph."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path or settings.WORKFLOW_DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflows (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    parent_id TEXT,
                    code TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_workflows_parent
                    ON workflows(parent_id, kind, created_at);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def save(
        self,
        kind: str,
        code: str,
        payload: Dict[str, Any],
        status: str,
        parent_id: str = "",
        workflow_id: str = "",
    ) -> str:
        identifier = workflow_id or f"{kind}_{uuid.uuid4().hex}"
        now = time.time()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO workflows "
                "(id, kind, status, parent_id, code, payload_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, "
                "COALESCE((SELECT created_at FROM workflows WHERE id=?), ?), ?)",
                (
                    identifier, kind, status, parent_id or None, code,
                    json.dumps(payload, ensure_ascii=False), identifier, now, now,
                ),
            )
        return identifier

    def get(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT id, kind, status, parent_id, code, payload_json, created_at, updated_at "
                "FROM workflows WHERE id=?",
                (workflow_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[5])
        except json.JSONDecodeError:
            payload = {}
        return {
            "id": row[0], "kind": row[1], "status": row[2], "parent_id": row[3] or "",
            "code": row[4], "payload": payload, "created_at": row[6], "updated_at": row[7],
        }

    def update_status(self, workflow_id: str, status: str, payload: Optional[Dict[str, Any]] = None) -> bool:
        with closing(self._connect()) as connection, connection:
            if payload is None:
                cursor = connection.execute(
                    "UPDATE workflows SET status=?, updated_at=? WHERE id=?",
                    (status, time.time(), workflow_id),
                )
            else:
                cursor = connection.execute(
                    "UPDATE workflows SET status=?, payload_json=?, updated_at=? WHERE id=?",
                    (status, json.dumps(payload, ensure_ascii=False), time.time(), workflow_id),
                )
        return cursor.rowcount > 0


__all__ = ["WorkflowStore"]
