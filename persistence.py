from __future__ import annotations

import json
import os
import sqlite3
from typing import Any


class SQLitePersistence:
    def __init__(self, path: str) -> None:
        self.path = path
        self._db: sqlite3.Connection | None = None

    async def connect(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.execute("PRAGMA journal_mode=WAL;")
        self._db.execute("PRAGMA synchronous=NORMAL;")
        self._db.execute("PRAGMA temp_store=MEMORY;")
        self._db.execute("PRAGMA foreign_keys=ON;")
        self._create_schema()
        self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    async def load_state(self) -> dict[str, Any]:
        db = self._require_db()

        response_rows = db.execute(
            """
            SELECT response_id, thread_id, parent_response_id, model, status,
                   assistant_output, last_error, created_at, completed_at
            FROM responses
            ORDER BY created_at ASC
            """
        ).fetchall()

        message_rows = db.execute(
            """
            SELECT response_id, role, content, seq
            FROM messages
            ORDER BY response_id ASC, seq ASC
            """
        ).fetchall()

        response_messages: dict[str, list[dict[str, str]]] = {}
        for response_id, role, content, _ in message_rows:
            response_messages.setdefault(response_id, []).append(
                {"role": role, "content": content}
            )

        responses: dict[str, dict[str, Any]] = {}
        for row in response_rows:
            response_id = row[0]
            responses[response_id] = {
                "response_id": response_id,
                "thread_id": row[1],
                "parent_response_id": row[2],
                "model": row[3],
                "status": row[4],
                "assistant_output": row[5] or "",
                "last_error": row[6],
                "created_at": row[7],
                "completed_at": row[8],
                "messages": response_messages.get(response_id, []),
            }

        return {"responses": responses}

    async def insert_thread(self, thread_id: str, created_at: int) -> None:
        db = self._require_db()
        db.execute(
            "INSERT OR IGNORE INTO threads (thread_id, created_at) VALUES (?, ?)",
            (thread_id, created_at),
        )
        db.commit()

    async def insert_response(
        self,
        response_id: str,
        thread_id: str,
        parent_response_id: str | None,
        model: str,
        status: str,
        created_at: int,
    ) -> None:
        db = self._require_db()
        db.execute(
            """
            INSERT INTO responses (
                response_id, thread_id, parent_response_id, model, status,
                assistant_output, last_error, created_at
            ) VALUES (?, ?, ?, ?, ?, '', NULL, ?)
            """,
            (response_id, thread_id, parent_response_id, model, status, created_at),
        )
        db.commit()

    async def update_response(
        self,
        response_id: str,
        *,
        status: str,
        assistant_output: str,
        last_error: str | None,
        completed_at: int | None,
    ) -> None:
        db = self._require_db()
        db.execute(
            """
            UPDATE responses
            SET status = ?, assistant_output = ?, last_error = ?, completed_at = ?
            WHERE response_id = ?
            """,
            (status, assistant_output, last_error, completed_at, response_id),
        )
        db.commit()

    async def insert_step(
        self,
        step_id: str,
        response_id: str,
        thread_id: str,
        step_index: int,
        status: str,
        created_at: int,
    ) -> None:
        db = self._require_db()
        db.execute(
            """
            INSERT INTO steps (
                step_id, response_id, thread_id, step_index,
                status, last_error, created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, NULL)
            """,
            (step_id, response_id, thread_id, step_index, status, created_at),
        )
        db.commit()

    async def update_step(
        self,
        step_id: str,
        *,
        status: str,
        last_error: str | None,
        completed_at: int,
    ) -> None:
        db = self._require_db()
        db.execute(
            """
            UPDATE steps
            SET status = ?, last_error = ?, completed_at = ?
            WHERE step_id = ?
            """,
            (status, last_error, completed_at, step_id),
        )
        db.commit()

    async def insert_message(
        self,
        response_id: str,
        thread_id: str,
        step_id: str | None,
        seq: int,
        role: str,
        content: str,
        created_at: int,
    ) -> None:
        db = self._require_db()
        db.execute(
            """
            INSERT INTO messages (
                response_id, thread_id, step_id, seq, role, content, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (response_id, thread_id, step_id, seq, role, content, created_at),
        )
        db.commit()

    async def insert_tool_call(
        self,
        tool_call_id: str,
        response_id: str,
        thread_id: str,
        step_id: str,
        name: str,
        arguments: dict[str, Any],
        mode: str,
        status: str,
        created_at: int,
    ) -> None:
        db = self._require_db()
        db.execute(
            """
            INSERT INTO tool_calls (
                tool_call_id, response_id, thread_id, step_id, name,
                arguments_json, mode, status, last_error,
                created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL)
            """,
            (
                tool_call_id,
                response_id,
                thread_id,
                step_id,
                name,
                json.dumps(arguments, separators=(",", ":")),
                mode,
                status,
                created_at,
            ),
        )
        db.commit()

    async def update_tool_call(
        self,
        tool_call_id: str,
        *,
        status: str,
        last_error: str | None,
        completed_at: int,
    ) -> None:
        db = self._require_db()
        db.execute(
            """
            UPDATE tool_calls
            SET status = ?, last_error = ?, completed_at = ?
            WHERE tool_call_id = ?
            """,
            (status, last_error, completed_at, tool_call_id),
        )
        db.commit()

    async def insert_tool_result(
        self,
        tool_call_id: str,
        response_id: str,
        thread_id: str,
        output: Any,
        is_error: bool,
        created_at: int,
    ) -> None:
        db = self._require_db()
        db.execute(
            """
            INSERT INTO tool_results (
                tool_call_id, response_id, thread_id, output_json, is_error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                tool_call_id,
                response_id,
                thread_id,
                json.dumps(output, separators=(",", ":")),
                int(is_error),
                created_at,
            ),
        )
        db.commit()

    def _create_schema(self) -> None:
        db = self._require_db()
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS threads (
                thread_id TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS responses (
                response_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                parent_response_id TEXT,
                model TEXT NOT NULL,
                status TEXT NOT NULL,
                assistant_output TEXT NOT NULL,
                last_error TEXT,
                created_at INTEGER NOT NULL,
                completed_at INTEGER,
                FOREIGN KEY(thread_id) REFERENCES threads(thread_id)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS steps (
                step_id TEXT PRIMARY KEY,
                response_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                status TEXT NOT NULL,
                last_error TEXT,
                created_at INTEGER NOT NULL,
                completed_at INTEGER,
                FOREIGN KEY(response_id) REFERENCES responses(response_id),
                FOREIGN KEY(thread_id) REFERENCES threads(thread_id)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                response_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                step_id TEXT,
                seq INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(response_id) REFERENCES responses(response_id),
                FOREIGN KEY(thread_id) REFERENCES threads(thread_id)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS tool_calls (
                tool_call_id TEXT PRIMARY KEY,
                response_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                last_error TEXT,
                created_at INTEGER NOT NULL,
                completed_at INTEGER,
                FOREIGN KEY(response_id) REFERENCES responses(response_id),
                FOREIGN KEY(thread_id) REFERENCES threads(thread_id),
                FOREIGN KEY(step_id) REFERENCES steps(step_id)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS tool_results (
                result_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool_call_id TEXT NOT NULL,
                response_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                output_json TEXT NOT NULL,
                is_error INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(tool_call_id) REFERENCES tool_calls(tool_call_id),
                FOREIGN KEY(response_id) REFERENCES responses(response_id),
                FOREIGN KEY(thread_id) REFERENCES threads(thread_id)
            )
            """
        )

    def _require_db(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("SQLitePersistence is not connected")
        return self._db
