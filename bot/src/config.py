"""Конфігурація бота зі змінних оточення (.env). Жодних секретів у коді."""
import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    bot_token: str
    fsolar_username: str
    fsolar_password: str
    device_sn: str
    device_type: str
    allowed_chat_ids: frozenset[int]
    poll_interval_minutes: int
    db_path: str
    timezone: str
    stale_after_minutes: int = 15
    telemetry_retention_days: int = 90


def _parse_allowed_chat_ids(raw: str) -> frozenset[int]:
    """Accept one ID or comma-separated IDs; malformed lists fail closed."""
    if not raw.strip():
        return frozenset()
    try:
        parts = [part.strip() for part in raw.split(",")]
        if any(re.fullmatch(r"[+-]?[0-9]+", part) is None for part in parts):
            raise ValueError("invalid chat ID list")
        chat_ids = frozenset(int(part) for part in parts)
        # Negative group IDs are valid; IDs must fit SQLite's signed INTEGER.
        if any(chat_id == 0 or not -(2**63) <= chat_id < 2**63 for chat_id in chat_ids):
            raise ValueError("chat ID outside the supported range")
    except ValueError as exc:
        raise SystemExit(
            "ALLOWED_CHAT_ID має містити один ненульовий цілий chat ID або список "
            "64-бітних chat ID через кому, без порожніх елементів"
        ) from exc
    return chat_ids


def load_config() -> Config:
    def positive_int(key: str, default: int) -> int:
        try:
            value = int(os.environ.get(key, str(default)))
        except ValueError as exc:
            raise SystemExit(f"{key} має бути цілим числом") from exc
        if value <= 0:
            raise SystemExit(f"{key} має бути більшим за нуль")
        return value

    def required(key: str) -> str:
        value = os.environ.get(key, "").strip()
        if not value:
            raise SystemExit(f"Відсутня обовʼязкова змінна оточення: {key}")
        return value

    allowed_chat_ids = _parse_allowed_chat_ids(os.environ.get("ALLOWED_CHAT_ID", ""))

    interval_raw = os.environ.get("POLL_INTERVAL_MINUTES", "3").strip()
    try:
        poll_interval_minutes = int(interval_raw)
    except ValueError as exc:
        raise SystemExit("POLL_INTERVAL_MINUTES має бути цілим числом") from exc
    if poll_interval_minutes <= 0:
        raise SystemExit("POLL_INTERVAL_MINUTES має бути більшим за нуль")

    return Config(
        bot_token=required("TELEGRAM_BOT_TOKEN"),
        fsolar_username=required("FSOLAR_USERNAME"),
        fsolar_password=required("FSOLAR_PASSWORD"),
        device_sn=required("FSOLAR_DEVICE_SN"),
        device_type=os.environ.get("FSOLAR_DEVICE_TYPE", "OG").strip() or "OG",
        allowed_chat_ids=allowed_chat_ids,
        poll_interval_minutes=poll_interval_minutes,
        db_path=os.environ.get("DB_PATH", "bot.db").strip() or "bot.db",
        timezone=os.environ.get("TIMEZONE", "Europe/Kyiv").strip() or "Europe/Kyiv",
        stale_after_minutes=positive_int("STALE_AFTER_MINUTES", 15),
        telemetry_retention_days=positive_int("TELEMETRY_RETENTION_DAYS", 90),
    )
