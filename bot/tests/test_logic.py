import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from detector import (  # noqa: E402
    battery_direction,
    detect_battery_thresholds,
    detect_transition,
)
from fsolar import parse_snapshot  # noqa: E402
from storage import Storage  # noqa: E402


def test_first_snapshot_is_baseline():
    assert detect_transition(None, True) is None
    assert detect_transition(None, False) is None


def test_no_change():
    assert detect_transition(True, True) is None
    assert detect_transition(False, False) is None


def test_grid_lost_and_restored():
    assert detect_transition(True, False) == "gridLost"
    assert detect_transition(False, True) == "gridRestored"


def test_battery_direction_uses_fsolar_power_sign():
    assert battery_direction(-800) == "charging"
    assert battery_direction(800) == "discharging"
    assert battery_direction(5) == "idle"
    assert battery_direction(None) == "unknown"


def test_discharge_thresholds_are_detected_only_while_discharging():
    assert detect_battery_thresholds(91, 89, "discharging") == [("discharging", 90)]
    assert detect_battery_thresholds(51, 49, "discharging") == [("discharging", 50)]
    assert detect_battery_thresholds(26, 24, "discharging") == [("discharging", 25)]
    assert detect_battery_thresholds(91, 89, "charging") == []


def test_charge_thresholds_are_detected_only_while_charging():
    assert detect_battery_thresholds(90, 91, "charging") == [("charging", 90)]
    assert detect_battery_thresholds(99, 100, "charging") == [("charging", 99)]
    assert detect_battery_thresholds(90, 91, "discharging") == []
    assert detect_battery_thresholds(None, 100, "charging") == []


def test_multiple_skipped_thresholds_are_reported():
    assert detect_battery_thresholds(91, 20, "discharging") == [
        ("discharging", 90),
        ("discharging", 50),
        ("discharging", 25),
    ]


def test_parse_grid_on():
    s = parse_snapshot({
        "acRInVolt": "218.3", "acRInFreq": "49.98", "workModeStr": "Line Mode",
        "emsSoc": "99", "emsVoltage": "54", "emsCurrent": "9", "dataTime": 1780215900000,
    })
    assert s.is_present is True
    assert s.voltage == 218.3
    assert s.soc == 99
    assert s.work_mode == "Line Mode"


def test_parse_grid_off():
    s = parse_snapshot({"acRInVolt": "0", "workModeStr": "Battery Mode", "emsSoc": "80"})
    assert s.is_present is False
    assert s.soc == 80


def test_parse_soc_fallback_to_avg():
    s = parse_snapshot({"acRInVolt": "230", "emsSocAvg": "55"})
    assert s.soc == 55


def test_parse_zero_soc_does_not_fall_back():
    s = parse_snapshot({"acRInVolt": "0", "emsSoc": 0, "emsSocAvg": "55"})
    assert s.soc == 0


def test_parse_battery_details():
    s = parse_snapshot({
        "acRInVolt": "218", "emsSoc": "99", "emsPower": "491",
        "loadPercent": "21", "totalEmsCapacity": "300",
    })
    assert s.batt_power == 491
    assert s.load_percent == 21
    assert s.capacity == 300


def test_parse_battery_total_power_and_calculated_fallback():
    total = parse_snapshot({"emsPower": "-70", "emsPowerTotal": "-75"})
    calculated = parse_snapshot({"emsVoltage": "54", "emsCurrent": "-2"})
    assert total.batt_power == -75
    assert calculated.batt_power == -108


def test_parse_consumption_statistics():
    s = parse_snapshot({
        "totalConsumPower": "513",
        "eLoadToday": "6.74",
        "eLoadMonth": "38.52",
        "eLoadYear": "2147.05",
        "eLoadTotal": "2147.05",
    })
    assert s.load_power == 513
    assert s.load_energy_today == 6.74
    assert s.load_energy_month == 38.52
    assert s.load_energy_year == 2147.05
    assert s.load_energy_total == 2147.05


def test_parse_consumption_uses_fallback_fields():
    s = parse_snapshot({
        "loadPowerConvertion": "800",
        "tLoadToday": "1.25",
        "tLoadMonth": "12.50",
        "tLoadYear": "100.75",
        "tLoadTotal": "200.25",
    })
    assert s.load_power == 800
    assert s.load_energy_today == 1.25
    assert s.load_energy_month == 12.5
    assert s.load_energy_year == 100.75
    assert s.load_energy_total == 200.25


def test_storage_persists_state_subscribers_and_events(tmp_path):
    db_path = tmp_path / "bot.db"
    when = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    storage = Storage(str(db_path))
    assert storage.get_last_present() is None
    storage.set_last_present(True)
    storage.add_subscriber(123)
    storage.add_event("gridLost", 42, when)
    storage.close()

    reopened = Storage(str(db_path))
    assert reopened.get_last_present() is True
    assert reopened.subscribers() == [123]
    assert reopened.recent_events() == [("gridLost", "2026-01-02T03:04:05+00:00", 42)]
    reopened.close()


def test_transition_and_notification_queue_are_persistent(tmp_path):
    db_path = tmp_path / "bot.db"
    when = datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc)

    storage = Storage(str(db_path))
    storage.add_subscriber(123)
    storage.record_transition(False, "gridLost", 41, when, "power lost")

    assert storage.get_last_present() is False
    assert storage.recent_events() == [("gridLost", "2026-02-03T04:05:06+00:00", 41)]
    pending = storage.pending_notifications()
    assert pending == [(pending[0][0], 123, "power lost", 0)]

    notification_id = pending[0][0]
    storage.mark_notification_failed(notification_id)
    assert storage.pending_notifications() == [(notification_id, 123, "power lost", 1)]
    storage.close()

    reopened = Storage(str(db_path))
    assert reopened.pending_notifications() == [(notification_id, 123, "power lost", 1)]
    reopened.mark_notification_sent(notification_id)
    assert reopened.pending_notifications() == []
    reopened.close()


def test_unsubscribe_discards_queued_notifications(tmp_path):
    storage = Storage(str(tmp_path / "bot.db"))
    storage.add_subscriber(123)
    storage.record_transition(
        False,
        "gridLost",
        40,
        datetime(2026, 2, 3, tzinfo=timezone.utc),
        "power lost",
    )

    storage.remove_subscriber(123)

    assert storage.subscribers() == []
    assert storage.pending_notifications() == []
    storage.close()


def test_existing_database_gets_notification_queue_migration(tmp_path):
    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE subscribers (chat_id INTEGER PRIMARY KEY);
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT, ts TEXT, soc INTEGER
        );
        """
    )
    legacy.close()

    storage = Storage(str(db_path))

    assert storage.pending_notifications() == []
    storage.close()


def test_battery_state_and_alerts_are_persistent(tmp_path):
    db_path = tmp_path / "bot.db"
    storage = Storage(str(db_path))
    storage.add_subscriber(123)
    storage.record_battery_state(89, ["battery below 90"])
    storage.close()

    reopened = Storage(str(db_path))
    assert reopened.get_last_battery_soc() == 89
    pending = reopened.pending_notifications()
    assert [(chat_id, text) for _, chat_id, text, _ in pending] == [
        (123, "battery below 90")
    ]
    reopened.close()
