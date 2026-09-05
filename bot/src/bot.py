"""Telegram-бот моніторингу електромережі через FSolar (однокористувацький режим)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.error import Forbidden
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Config, load_config
from charts import PERIODS, render_chart
from detector import battery_direction
from fsolar import FSolarClient, Snapshot
from monitoring import format_duration, is_fresh, process_observation
from storage import Outage, Storage

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("gridbot")
logging.getLogger("httpx").setLevel(logging.WARNING)  # Telegram URLs contain the bot token.

cfg: Config = load_config()

try:
    TZ = ZoneInfo(cfg.timezone)
except Exception:  # noqa: BLE001 — невідома назва поясу → UTC
    log.warning("Невідомий TIMEZONE=%s, використовую UTC", cfg.timezone)
    TZ = ZoneInfo("UTC")

client = FSolarClient(
    cfg.fsolar_username,
    cfg.fsolar_password,
    cfg.device_sn,
    cfg.device_type,
    device_timezone=TZ,
)


def _local_time(dt: datetime | None, fmt: str = "%H:%M:%S") -> str:
    return dt.astimezone(TZ).strftime(fmt) if dt is not None else "невідомо"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _freshness_warning(s: Snapshot) -> str:
    if is_fresh(s.timestamp, _utc_now(), cfg.stale_after_minutes):
        return ""
    return (
        "\n⚠️ Немає актуальної телеметрії. Це останні отримані показники; "
        "стан зараз невідомий.\n"
        f"Час даних: {_local_time(s.timestamp, '%d.%m.%Y %H:%M')}"
    )


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
BTN_OUTAGES = "⚡ Відключення"
BTN_CHARTS = "📈 Графіки"
BTN_SUBSCRIBE = "🔔 Підписатись"
BTN_UNSUBSCRIBE = "🔕 Відписатись"


def _menu_kb(subscribed: bool) -> ReplyKeyboardMarkup:
    toggle = BTN_UNSUBSCRIBE if subscribed else BTN_SUBSCRIBE
    return ReplyKeyboardMarkup(
        [[BTN_STATUS, BTN_BATTERY], [BTN_STATS, BTN_HISTORY], [BTN_OUTAGES, BTN_CHARTS], [toggle]],
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
    if not s.grid_known:
        grid = "⚠️ Стан мережі невідомий"
    elif not is_fresh(s.timestamp, _utc_now(), cfg.stale_after_minutes):
        grid = "⚠️ Останній відомий стан: мережа " + ("була" if s.is_present else "була відсутня")
    volt = f"{s.voltage:.0f} В" if s.voltage is not None else "—"
    freq = f"{s.frequency:.2f} Гц" if s.frequency is not None else "—"
    soc = f"{s.soc}%" if s.soc is not None else "—"
    return (
        f"{grid}\n"
        f"Напруга: {volt} · Частота: {freq}\n"
        f"🔋 Батарея: {soc}\n"
        f"Оновлено: {_local_time(s.timestamp)}{_freshness_warning(s)}"
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
    return "\n".join(line for line in lines if line) + _freshness_warning(s)


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
        f"Оновлено: {_local_time(s.timestamp)}{_freshness_warning(s)}"
    )


def _fmt_transition(event_type: str, s: Snapshot, outage: Outage | None = None) -> str:
    soc = f" · Батарея {s.soc}%" if s.soc is not None else ""
    when = _local_time(s.timestamp, "%H:%M")
    if event_type == "gridLost":
        return f"⚡️ Зникла мережа о {when}{soc}"
    message = f"✅ Зʼявилась мережа о {when}{soc}"
    if outage is None or s.timestamp is None:
        return message + "\nЧас початку відключення невідомий."
    duration = format_duration(int(s.timestamp.timestamp()) - outage.started_at)
    if outage.has_gaps:
        message += f"\nІнтервал між показниками: ≈ {duration}"
        message += "\nБули прогалини у зв’язку — точна тривалість відключення невідома."
    elif not outage.start_known:
        message += f"\nВідключення спостерігалось щонайменше {duration}; початок невідомий."
    else:
        message += f"\nВідключення тривало ≈ {duration}."
    if outage.start_soc is not None and s.soc is not None:
        message += f"\n🔋 Заряд батареї: {outage.start_soc}% → {s.soc}%."
    return message


def _fmt_outages(storage: Storage) -> str:
    now = _utc_now()
    local_now = now.astimezone(TZ)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)
    lines = ["⚡ Статистика відключень"]
    for title, start in (("Сьогодні", day_start), ("Цього місяця", month_start)):
        count, seconds, incomplete = storage.outage_summary(int(start.timestamp()), int(now.timestamp()))
        duration = format_duration(seconds) if count else "0 хв"
        lines.append(f"{title}: {count} · ≈ {duration}" + (" *" if incomplete else ""))
    outage = storage.open_outage()
    if outage:
        lines.append(f"\nОстанній відомий стан: мережі немає з {datetime.fromtimestamp(outage.started_at, TZ):%d.%m %H:%M}.")
        if not outage.start_known:
            lines.append("Початок відключення не зафіксовано.")
    if storage.get_value("connection_offline") == "1":
        lines.append("📡 Немає свіжих даних; поточний стан невідомий.")
    started = storage.get_value("monitor_started_at")
    if started:
        lines.append(f"\nОблік від {datetime.fromtimestamp(int(started), TZ):%d.%m.%Y %H:%M}.")
    else:
        lines.append("\nОблік почнеться після першого опитування.")
    lines.append("Тривалість приблизна, за телеметрією. * Є невідомий початок або прогалини.")
    return "\n".join(lines)


def _chart_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(label, callback_data=f"charts:{period}")
        for period, (_, label) in PERIODS.items()
    ]])


async def _send_charts(message, storage: Storage, period: str = "day") -> None:
    if period not in PERIODS:
        await message.reply_text("Використайте /charts day, /charts week або /charts month.")
        return
    end = _utc_now()
    start = end - timedelta(days=PERIODS[period][0])
    rows = storage.telemetry_between(int(start.timestamp()), int(end.timestamp()))
    if not rows:
        await message.reply_text(
            "📈 За цей період ще немає записаних показників. Історія накопичується "
            "під час опитування інвертора; старі дані з FSolar не завантажуються.",
            reply_markup=_chart_keyboard(),
        )
        return
    try:
        chart = await asyncio.to_thread(
            render_chart, rows, start, end, TZ, period,
            gap_seconds=cfg.stale_after_minutes * 60,
        )
        try:
            await message.reply_photo(
                photo=chart,
                caption=f"📈 {PERIODS[period][1]} · записів: {len(rows)}. "
                "Графік містить лише накопичені показники.",
                reply_markup=_chart_keyboard(),
            )
        finally:
            chart.close()
    except Exception as exc:
        log.warning("chart failed: %s", type(exc).__name__)
        await message.reply_text("⚠️ Не вдалося побудувати або надіслати графік. Спробуйте ще раз.")


def _fmt_battery_threshold(direction: str, threshold: int, s: Snapshot) -> str:
    soc = f"{s.soc}%" if s.soc is not None else "—"
    power = _fmt_power(abs(s.batt_power)) if s.batt_power is not None else "—"
    when = _local_time(s.timestamp, "%H:%M")
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
        "✅ Підписано на сповіщення про мережу, батарею та втрату/відновлення зв’язку.",
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


async def cmd_outages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _allowed(update):
        await update.message.reply_text(_fmt_outages(_storage(context)))


async def cmd_charts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _allowed(update):
        period = context.args[0].lower() if context.args else "day"
        await _send_charts(update.message, _storage(context), period)


async def on_chart_period(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if _allowed(update) and query.message:
        await _send_charts(query.message, _storage(context), query.data.split(":", 1)[1])


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
    elif text == BTN_OUTAGES:
        await update.message.reply_text(_fmt_outages(storage))
    elif text == BTN_CHARTS:
        await _send_charts(update.message, storage)
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
        log.warning("poll failed: %s", type(exc).__name__)
        snapshot = None
    _process_observation(storage, snapshot)


def _process_observation(storage: Storage, snapshot: Snapshot | None) -> None:
    process_observation(
        storage, snapshot, _utc_now(), stale_minutes=cfg.stale_after_minutes,
        retention_days=cfg.telemetry_retention_days, zone=TZ,
        transition_message=_fmt_transition, battery_message=_fmt_battery_threshold,
    )


async def health_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    _process_observation(_storage(context), None)


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
    app.add_handler(CommandHandler("outages", cmd_outages))
    app.add_handler(CommandHandler("charts", cmd_charts))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(on_chart_period, pattern=r"^charts:(day|week|month)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.job_queue.run_repeating(
        poll_job,
        interval=timedelta(minutes=cfg.poll_interval_minutes),
        first=timedelta(seconds=15),
        name="fsolar-poll",
    )
    app.job_queue.run_repeating(
        health_job, interval=60, first=60, name="telemetry-health",
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
