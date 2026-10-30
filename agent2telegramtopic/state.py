"""SQLite persistence for topic lanes."""

from dataclasses import dataclass
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Literal

from agent2telegramtopic.routing import LaneKey


LaneStatus = Literal["live", "parked", "waiting"]


@dataclass
class Lane:
    chat_id: int
    thread_id: int
    name: str
    agent: str
    session_id: str
    status: LaneStatus
    busy: bool
    last_activity: float
    last_compact: float | None
    ctx_tokens: int


class LaneStore:
    def __init__(self, db_path: str | Path):
        self.db_path = ":memory:" if str(db_path) == ":memory:" else str(Path(db_path).expanduser())
        if self.db_path != ":memory:":
            Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        # One connection is shared deliberately: this also keeps :memory: stores
        # visible to every caller. The lock serializes its use across poller,
        # slot-manager and lane-worker threads.
        self._lock = RLock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self._db.execute("PRAGMA busy_timeout = 30000")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return self._db

    def _initialize(self) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS lanes (
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('live', 'parked', 'waiting')),
                    busy INTEGER NOT NULL,
                    last_activity REAL NOT NULL,
                    last_compact REAL,
                    ctx_tokens INTEGER NOT NULL,
                    PRIMARY KEY (chat_id, thread_id)
                )"""
            )
            schema = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='lanes'"
            ).fetchone()[0]
            if "waiting" not in schema:
                # SQLite cannot alter a CHECK constraint. Rebuild the table,
                # preserving every lane and its primary key.
                db.execute("ALTER TABLE lanes RENAME TO lanes_old")
                db.execute(
                    """CREATE TABLE lanes (
                        chat_id INTEGER NOT NULL,
                        thread_id INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        agent TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (status IN ('live', 'parked', 'waiting')),
                        busy INTEGER NOT NULL,
                        last_activity REAL NOT NULL,
                        last_compact REAL,
                        ctx_tokens INTEGER NOT NULL,
                        PRIMARY KEY (chat_id, thread_id)
                    )"""
                )
                db.execute(
                    "INSERT INTO lanes SELECT chat_id, thread_id, name, agent, session_id, "
                    "status, busy, last_activity, last_compact, ctx_tokens FROM lanes_old"
                )
                db.execute("DROP TABLE lanes_old")
            # Telegram reaction updates carry no message_thread_id: remember which topic each
            # inbound and outbound message belongs to.
            db.execute(
                """CREATE TABLE IF NOT EXISTS message_threads (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL,
                    PRIMARY KEY (chat_id, message_id)
                )"""
            )
            # Topics whose creation Telegram announced and whose first message has not yet
            # been delivered to the agent with the naming hint.
            db.execute(
                """CREATE TABLE IF NOT EXISTS new_topics (
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL,
                    PRIMARY KEY (chat_id, thread_id)
                )"""
            )

    @staticmethod
    def _lane(row: tuple) -> Lane:
        return Lane(*row[:6], bool(row[6]), *row[7:])

    def get(self, key: LaneKey) -> Lane | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT chat_id, thread_id, name, agent, session_id, status, busy, "
                "last_activity, last_compact, ctx_tokens FROM lanes "
                "WHERE chat_id = ? AND thread_id = ?",
                (key.chat_id, key.thread_id),
            ).fetchone()
        return self._lane(row) if row is not None else None

    def upsert(self, lane: Lane) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO lanes
                (chat_id, thread_id, name, agent, session_id, status, busy,
                 last_activity, last_compact, ctx_tokens)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, thread_id) DO UPDATE SET
                    name=excluded.name, agent=excluded.agent,
                    session_id=excluded.session_id, status=excluded.status,
                    busy=excluded.busy, last_activity=excluded.last_activity,
                    last_compact=excluded.last_compact, ctx_tokens=excluded.ctx_tokens""",
                (lane.chat_id, lane.thread_id, lane.name, lane.agent, lane.session_id,
                 lane.status, int(lane.busy), lane.last_activity, lane.last_compact,
                 lane.ctx_tokens),
            )

    def list_live(self) -> list[Lane]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT chat_id, thread_id, name, agent, session_id, status, busy, "
                "last_activity, last_compact, ctx_tokens FROM lanes WHERE status = 'live'"
            ).fetchall()
        return [self._lane(row) for row in rows]

    def list_waiting(self) -> list[Lane]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT chat_id, thread_id, name, agent, session_id, status, busy, "
                "last_activity, last_compact, ctx_tokens FROM lanes WHERE status = 'waiting' "
                "ORDER BY last_activity"
            ).fetchall()
        return [self._lane(row) for row in rows]

    def list_all(self) -> list[Lane]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT chat_id, thread_id, name, agent, session_id, status, busy, "
                "last_activity, last_compact, ctx_tokens FROM lanes ORDER BY chat_id, thread_id"
            ).fetchall()
        return [self._lane(row) for row in rows]

    def reconcile(self, live_keys: set[LaneKey]) -> None:
        """Clear stale busy flags and align persisted status with tmux reality."""
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT chat_id, thread_id FROM lanes").fetchall()
            for chat_id, thread_id in rows:
                key = LaneKey(chat_id, thread_id)
                db.execute("UPDATE lanes SET status = ?, busy = 0 WHERE chat_id = ? AND thread_id = ?",
                           ("live" if key in live_keys else "parked", chat_id, thread_id))

    def set_status(self, key: LaneKey, status: LaneStatus) -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE lanes SET status = ? WHERE chat_id = ? AND thread_id = ?",
                       (status, key.chat_id, key.thread_id))

    def touch(self, key: LaneKey, ts: float) -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE lanes SET last_activity = ? WHERE chat_id = ? AND thread_id = ?",
                       (ts, key.chat_id, key.thread_id))

    def set_ctx_tokens(self, key: LaneKey, ctx_tokens: int) -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE lanes SET ctx_tokens = ? WHERE chat_id = ? AND thread_id = ?",
                       (ctx_tokens, key.chat_id, key.thread_id))

    def mark_compacted(self, key: LaneKey, ts: float, ctx_tokens: int) -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE lanes SET last_compact = ?, ctx_tokens = ? "
                       "WHERE chat_id = ? AND thread_id = ?",
                       (ts, ctx_tokens, key.chat_id, key.thread_id))

    def set_busy(self, key: LaneKey, busy: bool) -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE lanes SET busy = ? WHERE chat_id = ? AND thread_id = ?",
                       (int(busy), key.chat_id, key.thread_id))

    def record_message(self, chat_id: int, message_id: int, thread_id: int) -> None:
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO message_threads (chat_id, message_id, thread_id) "
                       "VALUES (?, ?, ?)", (chat_id, message_id, thread_id))

    def thread_for_message(self, chat_id: int, message_id: int) -> int | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT thread_id FROM message_threads "
                             "WHERE chat_id = ? AND message_id = ?", (chat_id, message_id)).fetchone()
        return row[0] if row is not None else None

    def mark_new_topic(self, key: LaneKey) -> None:
        with self._lock, self._connect() as db:
            db.execute("INSERT OR IGNORE INTO new_topics (chat_id, thread_id) VALUES (?, ?)",
                       (key.chat_id, key.thread_id))

    def is_new_topic(self, key: LaneKey) -> bool:
        with self._lock, self._connect() as db:
            return db.execute("SELECT 1 FROM new_topics WHERE chat_id = ? AND thread_id = ?",
                              (key.chat_id, key.thread_id)).fetchone() is not None

    def clear_new_topic(self, key: LaneKey) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM new_topics WHERE chat_id = ? AND thread_id = ?",
                       (key.chat_id, key.thread_id))
