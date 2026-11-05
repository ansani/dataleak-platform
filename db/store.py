"""Database SQLite per tracciare le email già processate (idempotenza)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    internet_message_id TEXT UNIQUE NOT NULL,
    conversation_id TEXT,
    subject TEXT,
    intent TEXT,
    status TEXT,
    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_internet_message_id ON processed_emails(internet_message_id);
"""


class ProcessedDB:
    """Gestisce l'inserimento e la verifica di email già processate."""

    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(DEFAULT_SCHEMA)
            conn.commit()

    def is_processed(self, internet_message_id: str) -> bool:
        """Restituisce True se l'email è già stata processata."""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT 1 FROM processed_emails WHERE internet_message_id = ?",
                (internet_message_id,),
            )
            return cur.fetchone() is not None

    def mark_processed(
        self,
        internet_message_id: str,
        conversation_id: str | None,
        subject: str,
        intent: str,
        status: str,
    ) -> None:
        """Registra un'email come processata."""
        with self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO processed_emails
                   (internet_message_id, conversation_id, subject, intent, status)
                   VALUES (?, ?, ?, ?, ?)""",
                (internet_message_id, conversation_id, subject, intent, status),
            )
            conn.commit()