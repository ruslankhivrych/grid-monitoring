"""Headless Windows entry point with explicit paths and bounded, redacted logs."""
from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import time

from dotenv import dotenv_values


class SecretFormatter(logging.Formatter):
    def __init__(self, secrets=()):
        super().__init__("%(asctime)sZ %(levelname)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S")
        self.converter = time.gmtime
        self.secrets = tuple(sorted((s for s in secrets if s), key=len, reverse=True))

    def format(self, record):
        message = super().format(record)
        for secret in self.secrets:
            message = message.replace(secret, "[REDACTED]")
        return re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{20,}", "[REDACTED]", message)


def prepare_environment(project: Path, data: Path) -> tuple[str, ...]:
    env_path = project / "bot" / ".env"
    if not env_path.is_file():
        raise FileNotFoundError("Create bot/.env before starting the Windows task")
    # Keep literal credentials, including ${...}, independent of the service's environment.
    for key, value in dotenv_values(env_path, encoding="utf-8-sig", interpolate=False).items():
        if value is not None:
            os.environ[key] = value
    data.mkdir(parents=True, exist_ok=True)
    os.environ["DB_PATH"] = str(data / "bot.db")
    os.environ["MPLCONFIGDIR"] = str(data / "matplotlib")
    return tuple(os.environ.get(key, "") for key in (
        "TELEGRAM_BOT_TOKEN", "FSOLAR_PASSWORD", "FSOLAR_USERNAME",
    ))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--logs", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="Validate configuration without polling Telegram")
    args = parser.parse_args()
    project, data, logs = (path.resolve() for path in (args.project, args.data, args.logs))
    logs.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(logs / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    # Install redaction before importing the bot or making network requests.
    try:
        secrets = prepare_environment(project, data)
    except Exception:
        handler.close()
        print("Windows bot setup failed; check bot/.env and directory permissions", file=sys.stderr)
        return 1
    handler.setFormatter(SecretFormatter(secrets))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    sys.path.insert(0, str(project / "bot" / "src"))
    log = logging.getLogger("windows-launcher")
    try:
        from config import load_config
        cfg = load_config()
        if not cfg.allowed_chat_ids:
            raise ValueError("Set ALLOWED_CHAT_ID before starting the Windows task")
        if args.check:
            log.info("Configuration validated for %s allowed chats", len(cfg.allowed_chat_ids))
            print("Windows configuration validated")
            return 0
        import bot
        log.info("Starting grid-monitoring as a background Windows task")
        bot.main()
        return 0
    except (Exception, SystemExit):
        log.exception("Bot stopped; Windows Task Scheduler will retry")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
