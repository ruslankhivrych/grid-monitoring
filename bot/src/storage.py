"""SQLite-стан бота: підписники, останній стан мережі, історія подій."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone


class Storage:
    def __init__(self, path: str):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
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
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

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
        self._db.commit()

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
        with self._db:
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
        with self._db:
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
        self._db.commit()

    def remove_subscriber(self, chat_id: int) -> None:
        with self._db:
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
        self._db.commit()

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
        self._db.commit()

    def mark_notification_failed(self, notification_id: int) -> None:
        self._db.execute(
            "UPDATE pending_notifications SET attempts=attempts+1 WHERE id=?",
            (notification_id,),
        )
        self._db.commit()
