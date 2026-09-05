import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST")
os.environ.setdefault("FSOLAR_USERNAME", "test")
os.environ.setdefault("FSOLAR_PASSWORD", "test")
os.environ.setdefault("FSOLAR_DEVICE_SN", "test")
os.environ.setdefault("ALLOWED_CHAT_ID", "123")

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import bot as bot_module  # noqa: E402
from fsolar import parse_snapshot  # noqa: E402
from storage import Storage  # noqa: E402
from telegram.error import Forbidden  # noqa: E402


class FakeTelegramBot:
    def __init__(self, failures: int = 0):
        self.failures = failures
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("temporary Telegram failure")
        self.sent.append((chat_id, text))


class ForbiddenTelegramBot:
    async def send_message(self, chat_id: int, text: str) -> None:
        raise Forbidden("bot was blocked")


class FakeFSolarClient:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    async def get_snapshot(self):
        return self.snapshot


def _context(storage: Storage, telegram_bot):
    return SimpleNamespace(
        application=SimpleNamespace(bot_data={"storage": storage}),
        bot=telegram_bot,
    )


def _queue_notification(storage: Storage) -> None:
    storage.add_subscriber(123)
    storage.record_transition(
        False,
        "gridLost",
        40,
        datetime(2026, 2, 3, tzinfo=timezone.utc),
        "power lost",
    )


def test_delivery_removes_sent_notification(tmp_path):
    storage = Storage(str(tmp_path / "bot.db"))
    _queue_notification(storage)
    telegram_bot = FakeTelegramBot()

    asyncio.run(bot_module.delivery_job(_context(storage, telegram_bot)))

    assert telegram_bot.sent == [(123, "power lost")]
    assert storage.pending_notifications() == []
    storage.close()


def test_failed_delivery_is_retried(tmp_path):
    storage = Storage(str(tmp_path / "bot.db"))
    _queue_notification(storage)
    telegram_bot = FakeTelegramBot(failures=1)
    context = _context(storage, telegram_bot)

    asyncio.run(bot_module.delivery_job(context))
    pending = storage.pending_notifications()
    assert pending[0][3] == 1

    asyncio.run(bot_module.delivery_job(context))
    assert telegram_bot.sent == [(123, "power lost")]
    assert storage.pending_notifications() == []
    storage.close()


def test_forbidden_chat_is_unsubscribed(tmp_path):
    storage = Storage(str(tmp_path / "bot.db"))
    _queue_notification(storage)

    asyncio.run(bot_module.delivery_job(_context(storage, ForbiddenTelegramBot())))

    assert storage.subscribers() == []
    assert storage.pending_notifications() == []
    storage.close()


def test_consumption_statistics_format():
    snapshot = parse_snapshot({
        "totalConsumPower": "1513",
        "eLoadToday": "6.74",
        "eLoadMonth": "38.52",
        "eLoadYear": "147.05",
        "eLoadTotal": "2147.05",
    })

    message = bot_module._fmt_stats(snapshot)

    assert "Зараз: 1.51 кВт" in message
    assert "Сьогодні: 6.74 кВт·год" in message
    assert "Цього місяця: 38.52 кВт·год" in message
    assert "Цього року: 147.05 кВт·год" in message
    assert "За весь час: 2147.05 кВт·год" in message


def test_battery_format_shows_charging_power():
    snapshot = parse_snapshot({"emsSoc": "80", "emsPowerTotal": "-1530"})

    message = bot_module._fmt_battery(snapshot)

    assert "Заряджання: 1.53 кВт" in message


def test_battery_threshold_messages_include_soc_and_power():
    snapshot = parse_snapshot({"emsSoc": "49", "emsPowerTotal": "850"})

    message = bot_module._fmt_battery_threshold("discharging", 50, snapshot)

    assert "нижче 50%: зараз 49%" in message
    assert "Потужність розряджання: 850 Вт" in message


def test_poll_queues_battery_threshold_once(tmp_path, monkeypatch):
    storage = Storage(str(tmp_path / "bot.db"))
    storage.add_subscriber(123)
    storage.record_battery_state(91, [])
    snapshot = parse_snapshot({
        "acRInVolt": "0",
        "workModeStr": "Battery Mode",
        "emsSoc": "89",
        "emsPowerTotal": "850",
        "dataTime": int(datetime.now(timezone.utc).timestamp() * 1000),
    })
    monkeypatch.setattr(bot_module, "client", FakeFSolarClient(snapshot))
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"storage": storage}))

    asyncio.run(bot_module.poll_job(context))
    first_pending = storage.pending_notifications()
    asyncio.run(bot_module.poll_job(context))

    assert len(first_pending) == 1
    assert "нижче 90%: зараз 89%" in first_pending[0][2]
    assert storage.pending_notifications() == first_pending
    storage.close()


def test_telemetry_time_is_consistent_in_messages_and_saved_history(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_module, "TZ", ZoneInfo("Europe/Kyiv"))
    monkeypatch.setattr(bot_module, "_utc_now", lambda: datetime(2026, 9, 4, 15, 34, tzinfo=timezone.utc))
    storage = Storage(str(tmp_path / "bot.db"))
    storage.add_subscriber(123)
    storage.set_last_present(True)
    snapshot = parse_snapshot({
        "acRInVolt": "0",
        "emsSoc": "49",
        "emsPowerTotal": "850",
        "dataTime": int(datetime(2026, 9, 4, 10, 30, tzinfo=timezone.utc).timestamp() * 1000),
        "dataTimeStr": "2026-09-04 18:30:00",
        "timeZone": "UTC+03:00",
    })
    monkeypatch.setattr(bot_module, "client", FakeFSolarClient(snapshot))

    for formatter in (bot_module._fmt_status, bot_module._fmt_battery, bot_module._fmt_stats):
        assert "Оновлено: 18:30:00" in formatter(snapshot)
    assert "Оновлено: 18:30" in bot_module._fmt_battery_threshold("discharging", 50, snapshot)

    asyncio.run(bot_module.poll_job(_context(storage, FakeTelegramBot())))

    assert storage.recent_events() == [("gridLost", "2026-09-04T15:30:00+00:00", 49)]
    assert "о 18:30" in storage.pending_notifications()[0][2]
    assert "2026-09-04 18:30" in bot_module._fmt_history(storage.recent_events())
    storage.close()


def test_missing_telemetry_time_is_not_replaced_with_current_time(tmp_path, monkeypatch):
    storage = Storage(str(tmp_path / "bot.db"))
    storage.add_subscriber(123)
    storage.set_last_present(True)
    storage.record_battery_state(91, [])
    snapshot = parse_snapshot({"acRInVolt": "0", "emsSoc": "20", "emsPowerTotal": "850"})
    monkeypatch.setattr(bot_module, "client", FakeFSolarClient(snapshot))

    assert "Оновлено: невідомо" in bot_module._fmt_status(snapshot)
    asyncio.run(bot_module.poll_job(_context(storage, FakeTelegramBot())))

    assert storage.get_last_present() is True
    assert storage.get_last_battery_soc() == 91
    assert storage.recent_events() == []
    assert storage.pending_notifications() == []
    storage.close()


def test_restoration_message_includes_duration_soc_and_uncertainty():
    from storage import Outage
    when = datetime(2026, 9, 5, 12, 18, tzinfo=timezone.utc)
    snapshot = parse_snapshot({"dataTime": int(when.timestamp() * 1000), "emsSoc": 64})
    outage = Outage(1, int(when.timestamp()) - 8280, None, 100, None, True, False)
    message = bot_module._fmt_transition("gridRestored", snapshot, outage)
    assert "2 год 18 хв" in message
    assert "100% → 64%" in message
    uncertain = Outage(1, outage.started_at, None, 100, None, True, True)
    assert "точна тривалість" in bot_module._fmt_transition("gridRestored", snapshot, uncertain)


def test_chart_and_outage_handlers_deny_unauthorized_chat(monkeypatch):
    monkeypatch.setattr(bot_module, "_allowed", lambda update: False)
    message = SimpleNamespace(reply_text=AsyncMock(), reply_photo=AsyncMock())
    query = SimpleNamespace(answer=AsyncMock(), message=message, data="charts:day")
    update = SimpleNamespace(message=message, callback_query=query)
    context = SimpleNamespace()
    asyncio.run(bot_module.cmd_outages(update, context))
    asyncio.run(bot_module.cmd_charts(update, context))
    asyncio.run(bot_module.on_chart_period(update, context))
    message.reply_text.assert_not_awaited()
    message.reply_photo.assert_not_awaited()
    query.answer.assert_awaited_once()


def test_chart_handler_sends_png_and_period_buttons(tmp_path, monkeypatch):
    now = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(bot_module, "_utc_now", lambda: now)
    storage = Storage(str(tmp_path / "bot.db"))
    storage.add_telemetry(int(now.timestamp()) - 300, 95, 510, True)
    received = []
    async def photo(**kwargs):
        received.append(kwargs["photo"].read(8))
        assert kwargs["reply_markup"].inline_keyboard[0][2].callback_data == "charts:month"
    message = SimpleNamespace(reply_text=AsyncMock(), reply_photo=photo)
    asyncio.run(bot_module._send_charts(message, storage))
    assert received == [b'\x89PNG\r\n\x1a\n']
    message.reply_text.assert_not_awaited()
    storage.close()


def test_chart_handler_explains_empty_history_and_invalid_period(tmp_path):
    storage = Storage(str(tmp_path / "bot.db"))
    message = SimpleNamespace(reply_text=AsyncMock(), reply_photo=AsyncMock())
    asyncio.run(bot_module._send_charts(message, storage, "week"))
    assert "ще немає" in message.reply_text.call_args.args[0]
    asyncio.run(bot_module._send_charts(message, storage, "invalid"))
    assert "/charts day" in message.reply_text.call_args.args[0]
    message.reply_photo.assert_not_awaited()
    storage.close()


def test_poll_api_failure_is_passed_to_health_monitor(tmp_path, monkeypatch):
    now = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(bot_module, "_utc_now", lambda: now)
    monkeypatch.setattr(bot_module, "client", SimpleNamespace(get_snapshot=AsyncMock(side_effect=RuntimeError('offline'))))
    storage = Storage(str(tmp_path / "bot.db"))
    storage.add_subscriber(123)
    storage.set_value("last_valid_at", int(now.timestamp()) - 1000)
    asyncio.run(bot_module.poll_job(_context(storage, FakeTelegramBot())))
    assert len(storage.pending_notifications()) == 1
    assert "Немає свіжих" in storage.pending_notifications()[0][2]
    storage.close()


def test_outage_command_uses_kyiv_calendar_boundaries(tmp_path, monkeypatch):
    now = datetime(2026, 9, 6, 0, 30, tzinfo=ZoneInfo('Europe/Kyiv'))
    monkeypatch.setattr(bot_module, "_utc_now", lambda: now.astimezone(timezone.utc))
    monkeypatch.setattr(bot_module, "TZ", ZoneInfo('Europe/Kyiv'))
    storage = Storage(str(tmp_path / "bot.db"))
    midnight = int(now.replace(hour=0, minute=0).timestamp())
    storage.start_outage(midnight - 600, 100, True)
    storage.end_outage(midnight + 900, 90)
    message = bot_module._fmt_outages(storage)
    assert "Сьогодні: 1 · ≈ 15 хв" in message
    assert "Цього місяця: 1 · ≈ 25 хв" in message
    storage.close()


def teardown_module():
    asyncio.run(bot_module.client.aclose())
