"""Local state: the email context of every mail handed to the agent, and the replies sent.

The agent only receives the text. Everything needed to reply lives here, keyed by the task id,
so replies survive restarts and do not depend on what the agent keeps.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mails (
    message_id     TEXT PRIMARY KEY,
    task_id        TEXT UNIQUE,
    author         TEXT NOT NULL,
    subject        TEXT NOT NULL,
    refs           TEXT NOT NULL,
    reply_cert_pem TEXT,
    received_at    TEXT NOT NULL,
    closed_at      TEXT
);
CREATE TABLE IF NOT EXISTS replies (
    task_id TEXT NOT NULL,
    state   INTEGER NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (task_id, state)
);
"""


@dataclass(frozen=True)
class Mail:
    """What a reply needs to know about the mail it answers."""

    message_id: str
    author: str
    subject: str
    references: str
    reply_cert_pem: str | None


def _now() -> str:
    return datetime.now(UTC).isoformat()


class State:
    """SQLite file owned by one unwrapper process. Calls are short local writes."""

    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def delivered(self, message_id: str) -> bool:
        """True once the agent accepted this mail (or it was answered directly)."""
        row = self._db.execute(
            "SELECT task_id IS NOT NULL OR closed_at IS NOT NULL FROM mails WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        return bool(row and row[0])

    def record_mail(self, mail: Mail) -> None:
        """Store the context before the agent sees the mail; a retry overwrites it."""
        self._db.execute(
            "INSERT INTO mails (message_id, author, subject, refs, reply_cert_pem, received_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (message_id) DO UPDATE SET author = excluded.author,"
            " subject = excluded.subject, refs = excluded.refs,"
            " reply_cert_pem = excluded.reply_cert_pem",
            (mail.message_id, mail.author, mail.subject, mail.references, mail.reply_cert_pem,
             _now()),
        )

    def attach_task(self, message_id: str, task_id: str) -> None:
        self._db.execute("UPDATE mails SET task_id = ? WHERE message_id = ?", (task_id, message_id))

    def close_mail(self, message_id: str) -> None:
        self._db.execute(
            "UPDATE mails SET closed_at = ? WHERE message_id = ? AND closed_at IS NULL",
            (_now(), message_id),
        )

    def mail_for_task(self, task_id: str) -> Mail | None:
        row = self._db.execute(
            "SELECT message_id, author, subject, refs, reply_cert_pem FROM mails WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return Mail(*row) if row else None

    def open_tasks(self) -> list[str]:
        """Tasks handed to the agent that have not received a final reply."""
        rows = self._db.execute(
            "SELECT task_id FROM mails WHERE task_id IS NOT NULL AND closed_at IS NULL"
            " ORDER BY received_at"
        )
        return [task_id for (task_id,) in rows]

    def replied(self, task_id: str, state: int) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM replies WHERE task_id = ? AND state = ?", (task_id, state)
        ).fetchone()
        return row is not None

    def record_reply(self, task_id: str, state: int, *, final: bool) -> None:
        with self._db:
            self._db.execute("BEGIN")
            self._db.execute(
                "INSERT OR IGNORE INTO replies (task_id, state, sent_at) VALUES (?, ?, ?)",
                (task_id, state, _now()),
            )
            if final:
                self._db.execute(
                    "UPDATE mails SET closed_at = ? WHERE task_id = ? AND closed_at IS NULL",
                    (_now(), task_id),
                )
