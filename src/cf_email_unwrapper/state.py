"""Local state: the email context of every mail handed to the agent, the replies made (with their
Message-IDs) and the outbox of replies not yet confirmed by the gateway.

The agent only receives the text. Everything needed to reply lives here, keyed by the task id,
so replies survive restarts and do not depend on what the agent keeps.
"""

import sqlite3
import uuid
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
    closed_at      TEXT,
    context_id     TEXT
);
CREATE TABLE IF NOT EXISTS replies (
    task_id TEXT NOT NULL,
    state   INTEGER NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (task_id, state)
);
CREATE TABLE IF NOT EXISTS reply_mails (
    message_id TEXT PRIMARY KEY,
    task_id    TEXT,
    context_id TEXT,
    sent_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reply_mails_context ON reply_mails (context_id, sent_at);
CREATE TABLE IF NOT EXISTS outbox (
    id              TEXT PRIMARY KEY,
    recipient       TEXT NOT NULL,
    raw             BLOB NOT NULL,
    created_at      REAL NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL
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
    context_id: str | None = None  # A2A context the mail was sent in


@dataclass(frozen=True)
class Parent:
    """The mail an inbound mail answers, when it is one this unwrapper knows."""

    task_id: str | None  # the task it belongs to (None for a direct agent reply)
    latest_reply: bool  # our most recent reply in its thread


@dataclass(frozen=True)
class OutboxReply:
    """A finished reply waiting to be sent; times are Unix timestamps."""

    id: str
    recipient: str
    raw: bytes
    created_at: float
    attempts: int
    next_attempt_at: float


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
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(mails)")}
        if "context_id" not in columns:  # state files from before the column existed
            self._db.execute("ALTER TABLE mails ADD COLUMN context_id TEXT")

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
            "INSERT INTO mails (message_id, author, subject, refs, reply_cert_pem, context_id,"
            " received_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (message_id) DO UPDATE SET author = excluded.author,"
            " subject = excluded.subject, refs = excluded.refs,"
            " reply_cert_pem = excluded.reply_cert_pem, context_id = excluded.context_id",
            (mail.message_id, mail.author, mail.subject, mail.references, mail.reply_cert_pem,
             mail.context_id, _now()),
        )

    def attach_task(self, message_id: str, task_id: str) -> None:
        self._db.execute("UPDATE mails SET task_id = ? WHERE message_id = ?", (task_id, message_id))

    def mail_for_task(self, task_id: str) -> Mail | None:
        row = self._db.execute(
            "SELECT message_id, author, subject, refs, reply_cert_pem, context_id FROM mails"
            " WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return Mail(*row) if row else None

    def parent(self, message_id: str) -> Parent | None:
        """What ``message_id`` is to us: one of our replies, a mail we delivered, or unknown."""
        row = self._db.execute(
            "SELECT r.task_id, r.context_id IS NOT NULL AND r.message_id = ("
            "  SELECT message_id FROM reply_mails WHERE context_id = r.context_id"
            "  ORDER BY sent_at DESC, rowid DESC LIMIT 1)"
            " FROM reply_mails r WHERE r.message_id = ?",
            (message_id,),
        ).fetchone()
        if row:
            return Parent(task_id=row[0], latest_reply=bool(row[1]))
        row = self._db.execute(
            "SELECT task_id FROM mails WHERE message_id = ? AND task_id IS NOT NULL",
            (message_id,),
        ).fetchone()
        return Parent(task_id=row[0], latest_reply=False) if row else None

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

    def record_reply(
        self, task_id: str, state: int, *, final: bool, mail: Mail, message_id: str, raw: bytes
    ) -> None:
        """Record the (task, state) answer and queue its reply in one transaction."""
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
            self._enqueue(task_id, mail, message_id, raw)

    def record_direct_reply(self, *, mail: Mail, message_id: str, raw: bytes) -> None:
        """Close a mail the agent answered directly and queue the reply in one transaction."""
        with self._db:
            self._db.execute("BEGIN")
            self._db.execute(
                "UPDATE mails SET closed_at = ? WHERE message_id = ? AND closed_at IS NULL",
                (_now(), mail.message_id),
            )
            self._enqueue(None, mail, message_id, raw)

    def _enqueue(self, task_id: str | None, mail: Mail, message_id: str, raw: bytes) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO reply_mails (message_id, task_id, context_id, sent_at)"
            " VALUES (?, ?, ?, ?)",
            (message_id, task_id, mail.context_id, _now()),
        )
        recipient = mail.author
        now = datetime.now(UTC).timestamp()
        self._db.execute(
            "INSERT INTO outbox (id, recipient, raw, created_at, next_attempt_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), recipient, raw, now, now),
        )

    def outbox(self) -> list[OutboxReply]:
        """Queued replies, the next due first."""
        rows = self._db.execute(
            "SELECT id, recipient, raw, created_at, attempts, next_attempt_at FROM outbox"
            " ORDER BY next_attempt_at, created_at"
        )
        return [OutboxReply(*row) for row in rows]

    def next_reply_due(self) -> float | None:
        return self._db.execute("SELECT min(next_attempt_at) FROM outbox").fetchone()[0]

    def postpone_reply(self, reply_id: str, next_attempt_at: float) -> None:
        self._db.execute(
            "UPDATE outbox SET attempts = attempts + 1, next_attempt_at = ? WHERE id = ?",
            (next_attempt_at, reply_id),
        )

    def remove_reply(self, reply_id: str) -> None:
        self._db.execute("DELETE FROM outbox WHERE id = ?", (reply_id,))
