"""SQLite-стан бота: підписники, останній стан мережі, історія подій."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class Outage:
    id: int
    started_at: int
    ended_at: int | None
    start_soc: int | None
    end_soc: int | None
    start_known: bool
    has_gaps: bool


class Storage:
    def __init__(self, path: str):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._transaction_depth = 0
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS subscribers (chat_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT, ts TEXT, soc INTEGER
            );
            CREATE TABLE IF NOT EXISTS pending_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_pending_notifications_id
                ON pending_notifications(id);
            CREATE TABLE IF NOT EXISTS telemetry (
                ts INTEGER PRIMARY KEY,
                soc INTEGER,
                load_power REAL,
                grid_present INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at INTEGER NOT NULL,
                ended_at INTEGER,
                start_soc INTEGER,
                end_soc INTEGER,
                start_known INTEGER NOT NULL DEFAULT 1,
                has_gaps INTEGER NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_outage
                ON outages((1)) WHERE ended_at IS NULL;
            CREATE INDEX IF NOT EXISTS idx_outages_started ON outages(started_at);
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def transaction(self):
        """Allow one poll to commit history, state and messages together."""
        if self._transaction_depth:
            yield
            return
        self._transaction_depth += 1
        try:
            with self._db:
                yield
        finally:
            self._transaction_depth -= 1

    def _commit(self) -> None:
        if not self._transaction_depth:
            self._db.commit()

    def get_value(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_value(self, key: str, value) -> None:
        self._db.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)),
        )
        self._commit()

    def queue_messages(self, messages: list[str]) -> None:
        with self.transaction():
            self._db.executemany(
                "INSERT INTO pending_notifications(chat_id,text) VALUES(?,?)",
                ((chat_id, message) for message in messages for chat_id in self.subscribers()),
            )

    def add_telemetry(self, ts: int, soc: int | None, load_power: float | None, present: bool) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO telemetry(ts,soc,load_power,grid_present) VALUES(?,?,?,?)",
            (ts, soc, load_power, int(present)),
        )
        self._commit()

    def prune_telemetry(self, before: int) -> None:
        self._db.execute("DELETE FROM telemetry WHERE ts < ?", (before,))
        self._commit()

    def telemetry_between(self, start: int, end: int) -> list[tuple]:
        return self._db.execute(
            "SELECT ts,soc,load_power,grid_present FROM telemetry "
            "WHERE ts >= ? AND ts <= ? ORDER BY ts", (start, end),
        ).fetchall()

    def open_outage(self) -> Outage | None:
        row = self._db.execute(
            "SELECT id,started_at,ended_at,start_soc,end_soc,start_known,has_gaps "
            "FROM outages WHERE ended_at IS NULL",
        ).fetchone()
        return Outage(*row) if row else None

    def start_outage(self, ts: int, soc: int | None, known: bool) -> None:
        self._db.execute(
            "INSERT INTO outages(started_at,start_soc,start_known) VALUES(?,?,?)",
            (ts, soc, int(known)),
        )
        self._commit()

    def mark_outage_gap(self) -> None:
        self._db.execute("UPDATE outages SET has_gaps=1 WHERE ended_at IS NULL")
        self._commit()

    def end_outage(self, ts: int, soc: int | None) -> None:
        self._db.execute(
            "UPDATE outages SET ended_at=?,end_soc=? WHERE ended_at IS NULL", (ts, soc),
        )
        self._commit()

    def outage_summary(self, start: int, end: int) -> tuple[int, int, bool]:
        """Count overlapping episodes, clip durations to the requested interval.

        An open outage is counted only up to the last processed sample, since
        the grid state after the last observation is not known.
        """
        last_sample = int(self.get_value("last_processed_at") or end)
        rows = self._db.execute(
            "SELECT started_at,ended_at,start_known,has_gaps FROM outages "
            "WHERE started_at < ? AND (ended_at IS NULL OR ended_at > ?)", (end, start),
        ).fetchall()
        count, seconds, incomplete = 0, 0, False
        for began, finished, known, gaps in rows:
            observed_end = finished if finished is not None else min(end, last_sample)
            if observed_end < start:
                continue
            count += 1
            seconds += max(0, min(end, observed_end) - max(start, began))
            incomplete |= not known or bool(gaps)
        return count, seconds, incomplete

    # --- стан мережі ---

    def get_last_present(self) -> bool | None:
        row = self._db.execute("SELECT value FROM kv WHERE key='last_present'").fetchone()
        return None if row is None else row[0] == "1"

    def set_last_present(self, present: bool) -> None:
        self._db.execute(
            "INSERT INTO kv(key, value) VALUES('last_present', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("1" if present else "0",),
        )
        self._commit()

    def get_last_battery_soc(self) -> int | None:
        row = self._db.execute("SELECT value FROM kv WHERE key='last_battery_soc'").fetchone()
        if row is None:
            return None
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return None

    def record_battery_state(self, soc: int, messages: list[str]) -> None:
        """Атомарно зберігає SOC і ставить порогові сповіщення в чергу."""
        with self.transaction():
            self._db.execute(
                "INSERT INTO kv(key, value) VALUES('last_battery_soc', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(soc),),
            )
            subscribers = self.subscribers()
            self._db.executemany(
                "INSERT INTO pending_notifications(chat_id, text) VALUES(?, ?)",
                (
                    (chat_id, message)
                    for message in messages
                    for chat_id in subscribers
                ),
            )

    def record_transition(
        self,
        present: bool,
        event_type: str,
        soc: int | None,
        when: datetime,
        message: str,
    ) -> None:
        """Атомарно зберігає стан, подію та чергу повідомлень для підписників."""
        ts = when.isoformat(timespec="seconds")
        subscribers = self.subscribers()
        with self.transaction():
            self._db.execute(
                "INSERT INTO kv(key, value) VALUES('last_present', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("1" if present else "0",),
            )
            self._db.execute(
                "INSERT INTO events(type, ts, soc) VALUES(?, ?, ?)",
                (event_type, ts, soc),
            )
            self._db.executemany(
                "INSERT INTO pending_notifications(chat_id, text) VALUES(?, ?)",
                ((chat_id, message) for chat_id in subscribers),
            )

    # --- підписники ---

    def add_subscriber(self, chat_id: int) -> None:
        self._db.execute("INSERT OR IGNORE INTO subscribers(chat_id) VALUES(?)", (chat_id,))
        self._commit()

    def remove_subscriber(self, chat_id: int) -> None:
        with self.transaction():
            self._db.execute("DELETE FROM subscribers WHERE chat_id=?", (chat_id,))
            self._db.execute("DELETE FROM pending_notifications WHERE chat_id=?", (chat_id,))

    def subscribers(self) -> list[int]:
        return [r[0] for r in self._db.execute("SELECT chat_id FROM subscribers").fetchall()]

    # --- історія ---

    def add_event(self, event_type: str, soc: int | None, when: datetime | None = None) -> None:
        ts = (when or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        self._db.execute(
            "INSERT INTO events(type, ts, soc) VALUES(?, ?, ?)",
            (event_type, ts, soc),
        )
        self._commit()

    def recent_events(self, limit: int = 10) -> list[tuple[str, str, int | None]]:
        return self._db.execute(
            "SELECT type, ts, soc FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    # --- надійна доставка ---

    def pending_notifications(self, limit: int = 100) -> list[tuple[int, int, str, int]]:
        return self._db.execute(
            "SELECT id, chat_id, text, attempts FROM pending_notifications "
            "ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()

    def mark_notification_sent(self, notification_id: int) -> None:
        self._db.execute("DELETE FROM pending_notifications WHERE id=?", (notification_id,))
        self._commit()

    def mark_notification_failed(self, notification_id: int) -> None:
        self._db.execute(
            "UPDATE pending_notifications SET attempts=attempts+1 WHERE id=?",
            (notification_id,),
        )
        self._commit()
