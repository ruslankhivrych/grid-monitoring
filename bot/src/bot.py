"""Telegram-бот моніторингу електромережі через FSolar (однокористувацький режим)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import ReplyKeyboardMarkup, Update
from telegram.error import Forbidden
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Config, load_config
from detector import battery_direction, detect_battery_thresholds, detect_transition
from fsolar import FSolarClient, Snapshot

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("gridbot")

cfg: Config = load_config()
client = FSolarClient(cfg.fsolar_username, cfg.fsolar_password, cfg.device_sn, cfg.device_type)

try:
    TZ = ZoneInfo(cfg.timezone)
except Exception:  # noqa: BLE001 — невідома назва поясу → UTC
    log.warning("Невідомий TIMEZONE=%s, використовую UTC", cfg.timezone)
    TZ = ZoneInfo("UTC")


def _local_time(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%H:%M:%S")


# --- доступ / стан ---

def _allowed(update: Update) -> bool:
    if cfg.allowed_chat_id is None:
        return False
    return update.effective_chat is not None and update.effective_chat.id == cfg.allowed_chat_id


def _storage(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["storage"]


# Підписи кнопок постійної (reply) клавіатури — вони ж надходять як текст від користувача.
BTN_STATUS = "🔌 Стан"
BTN_BATTERY = "🔋 Батарея"
BTN_STATS = "📊 Споживання"
BTN_HISTORY = "🕓 Історія"
BTN_SUBSCRIBE = "🔔 Підписатись"
BTN_UNSUBSCRIBE = "🔕 Відписатись"


def _menu_kb(subscribed: bool) -> ReplyKeyboardMarkup:
    toggle = BTN_UNSUBSCRIBE if subscribed else BTN_SUBSCRIBE
    return ReplyKeyboardMarkup(
        [[BTN_STATUS, BTN_BATTERY], [BTN_STATS, BTN_HISTORY], [toggle]],
        resize_keyboard=True,
        is_persistent=True,
    )


# --- форматування ---

def _soc_bar(soc: int | None) -> str:
    if soc is None:
        return ""
    filled = round(soc / 10)
    return "▰" * filled + "▱" * (10 - filled)


def _fmt_status(s: Snapshot) -> str:
    grid = "🟢 Мережа Є" if s.is_present else "🔴 Мережі НЕМАЄ"
    volt = f"{s.voltage:.0f} В" if s.voltage is not None else "—"
    freq = f"{s.frequency:.2f} Гц" if s.frequency is not None else "—"
    soc = f"{s.soc}%" if s.soc is not None else "—"
    return (
        f"{grid}\n"
        f"Напруга: {volt} · Частота: {freq}\n"
        f"🔋 Батарея: {soc}\n"
        f"Оновлено: {_local_time(s.timestamp)}"
    )


def _fmt_battery(s: Snapshot) -> str:
    soc = f"{s.soc}%" if s.soc is not None else "—"
    volt = f"{s.batt_voltage:.1f} В" if s.batt_voltage is not None else "—"
    curr = f"{s.batt_current:.1f} А" if s.batt_current is not None else "—"
    load = f"{s.load_percent}%" if s.load_percent is not None else "—"
    direction = battery_direction(s.batt_power)
    if direction == "charging":
        flow = f"⚡ Заряджання: {_fmt_power(abs(s.batt_power))}"
    elif direction == "discharging":
        flow = f"🔻 Розряджання: {_fmt_power(s.batt_power)}"
    elif direction == "idle":
        flow = "⏸ Акумулятор не заряджається і не розряджається"
    else:
        flow = "Потужність заряджання/розряджання: —"
    lines = [
        f"🔋 Заряд: {soc}",
        _soc_bar(s.soc),
        f"Напруга: {volt} · Струм: {curr}",
        flow,
        f"Навантаження: {load}",
        f"Оновлено: {_local_time(s.timestamp)}",
    ]
    return "\n".join(line for line in lines if line)


def _fmt_power(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value) >= 1000:
        return f"{value / 1000:.2f} кВт"
    return f"{value:.0f} Вт"


def _fmt_energy(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f} кВт·год"


def _fmt_stats(s: Snapshot) -> str:
    return (
        "📊 Споживання електроенергії\n"
        f"Зараз: {_fmt_power(s.load_power)}\n"
        f"Сьогодні: {_fmt_energy(s.load_energy_today)}\n"
        f"Цього місяця: {_fmt_energy(s.load_energy_month)}\n"
        f"Цього року: {_fmt_energy(s.load_energy_year)}\n"
        f"За весь час: {_fmt_energy(s.load_energy_total)}\n"
        f"Оновлено: {_local_time(s.timestamp)}"
    )


def _fmt_transition(event_type: str, s: Snapshot) -> str:
    soc = f" · Батарея {s.soc}%" if s.soc is not None else ""
    when = s.timestamp.astimezone(TZ).strftime("%H:%M")
    if event_type == "gridLost":
        return f"⚡️ Зникла мережа о {when}{soc}"
    return f"✅ Зʼявилась мережа о {when}{soc}"


def _fmt_battery_threshold(direction: str, threshold: int, s: Snapshot) -> str:
    soc = f"{s.soc}%" if s.soc is not None else "—"
    power = _fmt_power(abs(s.batt_power)) if s.batt_power is not None else "—"
    when = s.timestamp.astimezone(TZ).strftime("%H:%M")
    if direction == "discharging":
        return (
            "🔋 Акумулятор розряджається\n"
            f"Заряд опустився нижче {threshold}%: зараз {soc}\n"
            f"Потужність розряджання: {power}\n"
            f"Оновлено: {when}"
        )
    return (
        "⚡ Акумулятор заряджається\n"
        f"Заряд перевищив {threshold}%: зараз {soc}\n"
        f"Потужність заряджання: {power}\n"
        f"Оновлено: {when}"
    )


def _fmt_history(events) -> str:
    if not events:
        return "Поки немає зафіксованих подій."
    lines = []
    for event_type, ts, soc in events:
        icon = "⚡️" if event_type == "gridLost" else "✅"
        label = "зникла" if event_type == "gridLost" else "зʼявилась"
        soc_str = f" ({soc}%)" if soc is not None else ""
        when = _local_datetime(ts)
        lines.append(f"{icon} {when} — мережа {label}{soc_str}")
    return "Останні події:\n" + "\n".join(lines)


def _local_datetime(iso_ts: str) -> str:
    """ISO-час події (UTC) → рядок у локальному поясі. Старі naive-записи лишаємо як є."""
    try:
        dt = datetime.fromisoformat(iso_ts)
    except ValueError:
        return iso_ts.replace("T", " ")
    if dt.tzinfo is not None:
        dt = dt.astimezone(TZ)
    return dt.strftime("%Y-%m-%d %H:%M")


# --- спільні дії (команда + кнопка) ---

async def _send_status(send) -> None:
    try:
        snapshot = await client.get_snapshot()
    except Exception as exc:  # noqa: BLE001
        log.warning("status failed: %s", exc)
        await send(f"⚠️ Не вдалося отримати дані: {exc}")
        return
    await send(_fmt_status(snapshot))


async def _send_battery(send) -> None:
    try:
        snapshot = await client.get_snapshot()
    except Exception as exc:  # noqa: BLE001
        log.warning("battery failed: %s", exc)
        await send(f"⚠️ Не вдалося отримати дані: {exc}")
        return
    await send(_fmt_battery(snapshot))


async def _send_stats(send) -> None:
    try:
        snapshot = await client.get_snapshot()
    except Exception as exc:  # noqa: BLE001
        log.warning("stats failed: %s", exc)
        await send(f"⚠️ Не вдалося отримати статистику: {exc}")
        return
    await send(_fmt_stats(snapshot))


# --- команди ---

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if cfg.allowed_chat_id is None:
        await update.message.reply_text(
            "🔒 Доступ ще не налаштовано. "
            f"Додайте ALLOWED_CHAT_ID={chat_id} до .env і перезапустіть бота."
        )
        return
    if not _allowed(update):
        await update.message.reply_text(f"⛔ Доступ обмежено. Ваш chat_id: {chat_id}")
        return
    storage = _storage(context)
    storage.add_subscriber(chat_id)
    await update.message.reply_text(
        "✅ Підписано на сповіщення про зникнення/появу мережі.",
        reply_markup=_menu_kb(subscribed=True),
    )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    subscribed = update.effective_chat.id in _storage(context).subscribers()
    await update.message.reply_text("Меню:", reply_markup=_menu_kb(subscribed))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _allowed(update):
        await _send_status(update.message.reply_text)


async def cmd_battery(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _allowed(update):
        await _send_battery(update.message.reply_text)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _allowed(update):
        await _send_stats(update.message.reply_text)


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _allowed(update):
        await update.message.reply_text(_fmt_history(_storage(context).recent_events()))


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    _storage(context).remove_subscriber(update.effective_chat.id)
    await update.message.reply_text(
        "🔕 Відписано. /start — щоб підписатися знову.",
        reply_markup=_menu_kb(False),
    )


# --- натискання кнопок постійного меню (надходять як текст) ---

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    storage = _storage(context)
    chat_id = update.effective_chat.id
    text = update.message.text

    if text == BTN_STATUS:
        await _send_status(update.message.reply_text)
    elif text == BTN_BATTERY:
        await _send_battery(update.message.reply_text)
    elif text == BTN_STATS:
        await _send_stats(update.message.reply_text)
    elif text == BTN_HISTORY:
        await update.message.reply_text(_fmt_history(storage.recent_events()))
    elif text == BTN_SUBSCRIBE:
        storage.add_subscriber(chat_id)
        await update.message.reply_text("🔔 Підписано.", reply_markup=_menu_kb(True))
    elif text == BTN_UNSUBSCRIBE:
        storage.remove_subscriber(chat_id)
        await update.message.reply_text("🔕 Відписано.", reply_markup=_menu_kb(False))


# --- фоновий поллер ---

async def poll_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    storage = context.application.bot_data["storage"]
    try:
        snapshot = await client.get_snapshot()
    except Exception as exc:  # noqa: BLE001
        log.warning("poll failed: %s", exc)
        return

    previous = storage.get_last_present()
    transition = detect_transition(previous, snapshot.is_present)

    if snapshot.soc is not None:
        previous_soc = storage.get_last_battery_soc()
        direction = battery_direction(snapshot.batt_power)
        battery_alerts = detect_battery_thresholds(previous_soc, snapshot.soc, direction)
        battery_messages = [
            _fmt_battery_threshold(alert_direction, threshold, snapshot)
            for alert_direction, threshold in battery_alerts
        ]
        if previous_soc is None or direction in {"charging", "discharging"}:
            storage.record_battery_state(snapshot.soc, battery_messages)

    if transition is None:
        storage.set_last_present(snapshot.is_present)
        return

    message = _fmt_transition(transition, snapshot)
    storage.record_transition(
        snapshot.is_present,
        transition,
        snapshot.soc,
        snapshot.timestamp,
        message,
    )


async def delivery_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Доставляє персистентну чергу; невдалі повідомлення лишаються для повтору."""
    storage = context.application.bot_data["storage"]
    for notification_id, chat_id, message, attempts in storage.pending_notifications():
        try:
            await context.bot.send_message(chat_id, message)
        except Forbidden as exc:
            storage.remove_subscriber(chat_id)
            log.warning("chat %s rejected the bot; subscription removed: %s", chat_id, exc)
        except Exception as exc:  # noqa: BLE001
            storage.mark_notification_failed(notification_id)
            log.warning("send to %s failed (attempt %s): %s", chat_id, attempts + 1, exc)
        else:
            storage.mark_notification_sent(notification_id)


async def _on_shutdown(app: Application) -> None:
    storage = app.bot_data.get("storage")
    if storage is not None:
        storage.close()
    await client.aclose()


def main() -> None:
    from storage import Storage

    # Python 3.12+/3.14: run_polling() викликає asyncio.get_event_loop(), який більше не
    # створює цикл автоматично і кидає RuntimeError — створюємо його явно.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = ApplicationBuilder().token(cfg.bot_token).post_shutdown(_on_shutdown).build()
    app.bot_data["storage"] = Storage(cfg.db_path)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("battery", cmd_battery))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.job_queue.run_repeating(
        poll_job,
        interval=timedelta(minutes=cfg.poll_interval_minutes),
        first=timedelta(seconds=15),
        name="fsolar-poll",
    )
    app.job_queue.run_repeating(
        delivery_job,
        interval=timedelta(seconds=30),
        first=timedelta(seconds=5),
        name="telegram-delivery",
    )

    log.info("Бот стартував. Опитування кожні %s хв.", cfg.poll_interval_minutes)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
