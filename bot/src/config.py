"""Конфігурація бота зі змінних оточення (.env). Жодних секретів у коді."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    bot_token: str
    fsolar_username: str
    fsolar_password: str
    device_sn: str
    device_type: str
    allowed_chat_id: int | None
    poll_interval_minutes: int
    db_path: str
    timezone: str


def load_config() -> Config:
    def required(key: str) -> str:
        value = os.environ.get(key, "").strip()
        if not value:
            raise SystemExit(f"Відсутня обовʼязкова змінна оточення: {key}")
        return value

    allowed_raw = os.environ.get("ALLOWED_CHAT_ID", "").strip()
    try:
        allowed_chat_id = int(allowed_raw) if allowed_raw else None
    except ValueError as exc:
        raise SystemExit("ALLOWED_CHAT_ID має бути цілим числом") from exc

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
        allowed_chat_id=allowed_chat_id,
        poll_interval_minutes=poll_interval_minutes,
        db_path=os.environ.get("DB_PATH", "bot.db").strip() or "bot.db",
        timezone=os.environ.get("TIMEZONE", "Europe/Kyiv").strip() or "Europe/Kyiv",
    )
