"""Persistent monitoring decisions; no network calls or Telegram dependencies."""
from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from typing import Callable

from detector import battery_direction, detect_battery_thresholds, detect_transition
from fsolar import Snapshot
from storage import Outage, Storage


def is_fresh(timestamp: datetime | None, now: datetime, stale_minutes: int) -> bool:
    if timestamp is None or timestamp.tzinfo is None:
        return False
    age = (now - timestamp).total_seconds()
    return -60 <= age <= stale_minutes * 60


def format_duration(seconds: int | float) -> str:
    minutes = max(0, int(seconds) // 60)
    if not minutes:
        return "менше 1 хв"
    days, remainder = divmod(minutes, 1440)
    hours, minutes = divmod(remainder, 60)
    return " ".join(
        text for value, text in (
            (days, f"{days} дн"), (hours, f"{hours} год"), (minutes, f"{minutes} хв")
        ) if value
    )


def process_observation(
    storage: Storage,
    snapshot: Snapshot | None,
    now: datetime,
    *,
    stale_minutes: int,
    retention_days: int,
    zone: tzinfo,
    transition_message: Callable[[str, Snapshot, Outage | None], str],
    battery_message: Callable[[str, int, Snapshot], str],
) -> None:
    """Commit one observation (or failed request/watchdog tick) atomically.

    Duplicate/older telemetry must never replay power or battery transitions.
    Connectivity is based on measurement time, not on HTTP success.
    """
    now_ts = int(now.timestamp())
    timestamp = snapshot.timestamp if snapshot and snapshot.grid_known else None
    plausible = timestamp is not None and timestamp.tzinfo is not None and (
        timestamp - now
    ).total_seconds() <= 60
    sample_ts = int(timestamp.timestamp()) if plausible else None

    with storage.transaction():
        started = storage.get_value("monitor_started_at")
        if started is None:
            storage.set_value("monitor_started_at", now_ts)
        last_valid = storage.get_value("last_valid_at")
        if sample_ts is not None and (last_valid is None or sample_ts > int(last_valid)):
            storage.set_value("last_valid_at", sample_ts)
            last_valid = str(sample_ts)
        reference = int(last_valid if last_valid is not None else started or now_ts)
        was_offline = storage.get_value("connection_offline") == "1"
        previous_ts_raw = storage.get_value("last_processed_at")
        previous_ts = int(previous_ts_raw) if previous_ts_raw is not None else None
        fresh = is_fresh(timestamp, now, stale_minutes) and (
            previous_ts is None or sample_ts >= previous_ts
        )

        if not fresh and now_ts - reference > stale_minutes * 60:
            if not was_offline:
                last_text = (
                    datetime.fromtimestamp(int(last_valid), zone).strftime("%d.%m %H:%M")
                    if last_valid is not None else "ще не отримано"
                )
                storage.queue_messages([
                    "📡 Немає свіжих даних від інвертора\n"
                    f"Минуло понад {stale_minutes} хв без актуальної телеметрії.\n"
                    f"Останні дані: {last_text}\n"
                    "Стан мережі й батареї зараз невідомий. Причиною може бути "
                    "зв’язок інвертора, інтернет або недоступність FSolar."
                ])
                storage.set_value("connection_offline", 1)
                storage.set_value("connection_offline_since", reference)
                storage.mark_outage_gap()
        elif fresh and was_offline:
            since = int(storage.get_value("connection_offline_since") or reference)
            storage.queue_messages([
                "📡 Зв’язок з інвертором відновлено\n"
                f"Без свіжих даних: ≈ {format_duration(now_ts - since)}\n"
                f"Оновлено: {timestamp.astimezone(zone):%d.%m %H:%M}"
            ])
            storage.set_value("connection_offline", 0)

        if not fresh or (previous_ts is not None and sample_ts <= previous_ts):
            return

        gap = previous_ts is not None and sample_ts - previous_ts > stale_minutes * 60
        if gap:
            storage.mark_outage_gap()
        previous = storage.get_last_present()
        transition = detect_transition(previous, snapshot.is_present)
        outage = storage.open_outage()

        if not snapshot.is_present and outage is None:
            storage.start_outage(
                sample_ts, snapshot.soc, known=previous is True and not gap and not was_offline,
            )
        elif snapshot.is_present and outage is not None:
            storage.end_outage(sample_ts, snapshot.soc)

        if transition is None:
            storage.set_last_present(snapshot.is_present)
        else:
            storage.record_transition(
                snapshot.is_present, transition, snapshot.soc,
                timestamp.astimezone(timezone.utc),
                transition_message(transition, snapshot, outage),
            )

        if snapshot.soc is not None:
            previous_soc = storage.get_last_battery_soc()
            direction = battery_direction(snapshot.batt_power)
            alerts = detect_battery_thresholds(previous_soc, snapshot.soc, direction)
            if previous_soc is None or direction in {"charging", "discharging"}:
                storage.record_battery_state(
                    snapshot.soc,
                    [battery_message(flow, threshold, snapshot) for flow, threshold in alerts],
                )

        storage.add_telemetry(sample_ts, snapshot.soc, snapshot.load_power, snapshot.is_present)
        storage.set_value("last_processed_at", sample_ts)
        storage.prune_telemetry(now_ts - retention_days * 86400)
