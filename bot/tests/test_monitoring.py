import math
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fsolar import parse_snapshot
from monitoring import is_fresh, process_observation
from storage import Storage

BASE = datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc)
KYIV = ZoneInfo("Europe/Kyiv")


def sample(minute=0, present=True, soc=100, power=-500):
    return parse_snapshot({
        "dataTime": int((BASE + timedelta(minutes=minute)).timestamp() * 1000),
        "acRInVolt": "230" if present else "0", "emsSoc": soc,
        "totalConsumPower": "500", "emsPowerTotal": power,
    })


def observe(storage, snapshot, minute=0, retention=90):
    process_observation(
        storage, snapshot, BASE + timedelta(minutes=minute), stale_minutes=15,
        retention_days=retention, zone=KYIV,
        transition_message=lambda event, snap, outage: event,
        battery_message=lambda flow, level, snap: f"{flow}:{level}",
    )


@pytest.fixture
def storage(tmp_path):
    db = Storage(str(tmp_path / "bot.db"))
    db.add_subscriber(123)
    yield db
    db.close()


def test_freshness_boundaries_and_invalid_time():
    assert is_fresh(BASE - timedelta(minutes=15), BASE, 15)
    assert not is_fresh(BASE - timedelta(minutes=15, seconds=1), BASE, 15)
    assert not is_fresh(BASE + timedelta(minutes=5), BASE, 15)
    assert not is_fresh(None, BASE, 15)
    assert not is_fresh(BASE.replace(tzinfo=None), BASE, 15)


def test_stale_http_success_warns_once_without_changing_power_state(storage):
    observe(storage, sample())
    observe(storage, sample(present=False, soc=20), minute=16)
    observe(storage, sample(present=False, soc=20), minute=20)
    notifications = storage.pending_notifications()
    assert len(notifications) == 1
    assert "Немає свіжих даних" in notifications[0][2]
    assert storage.get_last_present() is True
    assert storage.get_last_battery_soc() == 100
    assert storage.recent_events() == []
    assert len(storage.telemetry_between(0, int(BASE.timestamp()) + 99999)) == 1


def test_api_failure_and_recovery_survive_restart(tmp_path):
    path = str(tmp_path / "bot.db")
    db = Storage(path)
    db.add_subscriber(123)
    observe(db, sample())
    observe(db, None, minute=16)
    db.close()
    db = Storage(path)
    observe(db, None, minute=30)
    assert len(db.pending_notifications()) == 1
    observe(db, sample(31), minute=31)
    assert db.get_value("connection_offline") == "0"
    assert len(db.pending_notifications()) == 2
    assert "відновлено" in db.pending_notifications()[1][2]
    observe(db, sample(31), minute=32)
    assert len(db.pending_notifications()) == 2
    db.close()


def test_startup_failures_have_grace_period(storage):
    observe(storage, None)
    observe(storage, None, minute=15)
    assert storage.pending_notifications() == []
    observe(storage, None, minute=16)
    assert len(storage.pending_notifications()) == 1


def test_missing_or_future_timestamps_never_reset_watchdog(storage):
    observe(storage, sample())
    observe(storage, sample(1000), minute=16)
    observe(storage, parse_snapshot({"acRInVolt": "230"}), minute=20)
    assert storage.get_value("last_valid_at") == str(int(BASE.timestamp()))
    assert len(storage.pending_notifications()) == 1
    assert storage.get_value("connection_offline") == "1"


def test_duplicate_and_out_of_order_readings_do_not_replay_alerts(storage):
    observe(storage, sample())
    observe(storage, sample(5, present=False, soc=89), minute=5)
    pending = storage.pending_notifications()
    observe(storage, sample(5, present=True, soc=100), minute=6)
    observe(storage, sample(2, present=True, soc=100), minute=7)
    assert storage.pending_notifications() == pending
    assert storage.get_last_present() is False
    assert storage.get_last_battery_soc() == 89
    assert len(storage.recent_events()) == 1
    assert len(storage.telemetry_between(0, int(BASE.timestamp()) + 99999)) == 2


@pytest.mark.parametrize('start_soc,end_soc,power,expected', [
    (90, 89, -1043, [('discharging', 90)]),
    (50, 49, -742, [('discharging', 50)]),
    (25, 24, -742, [('discharging', 25)]),
    (90, 91, 1530, [('charging', 90)]),
    (99, 100, 1530, [('charging', 99)]),
    (91, 20, -742, [('discharging', 90), ('discharging', 50), ('discharging', 25)]),
    (89, 100, 1530, [('charging', 90), ('charging', 99)]),
])
def test_signed_power_crossings_survive_restart_without_repeating(
    tmp_path, start_soc, end_soc, power, expected,
):
    path = str(tmp_path / 'thresholds.db')
    db = Storage(path)
    db.add_subscriber(123)
    observe(db, sample(0, present=False, soc=start_soc, power=power))
    assert db.pending_notifications() == []
    db.close()

    db = Storage(path)
    observe(db, sample(5, present=False, soc=end_soc, power=power), minute=5)
    pending = db.pending_notifications()
    assert [row[2] for row in pending] == [f'{flow}:{level}' for flow, level in expected]
    db.close()

    db = Storage(path)
    observe(db, sample(5, present=False, soc=end_soc, power=power), minute=6)
    observe(db, sample(10, present=False, soc=end_soc, power=power), minute=10)
    assert db.pending_notifications() == pending
    db.close()


def test_reported_90_percent_crossing_queues_alert(storage):
    # SOC/time sequence recorded on the VPS during the reported outage.
    readings = [(0, 91), (5, 90), (10, 89), (15, 89), (20, 88), (25, 88), (90, 83), (95, 82)]
    for minute, soc in readings:
        observe(storage, sample(minute, present=False, soc=soc, power=-742), minute=minute)
    assert [row[2] for row in storage.pending_notifications()] == ['discharging:90']


def test_outage_survives_restart_and_records_soc(tmp_path):
    path = str(tmp_path / "bot.db")
    db = Storage(path)
    observe(db, sample())
    observe(db, sample(5, present=False), minute=5)
    started = db.open_outage()
    db.close()
    db = Storage(path)
    observe(db, sample(10, present=False, soc=80), minute=10)
    assert db.open_outage().id == started.id
    observe(db, sample(15, present=True, soc=70), minute=15)
    assert db.open_outage() is None
    row = db._db.execute('SELECT started_at,ended_at,start_soc,end_soc,start_known,has_gaps FROM outages').fetchone()
    assert row == (int(BASE.timestamp()) + 300, int(BASE.timestamp()) + 900, 100, 70, 1, 0)
    assert db.outage_summary(int(BASE.timestamp()), int(BASE.timestamp()) + 1800) == (1, 600, False)
    db.close()


def test_starting_without_power_has_unknown_outage_start(storage):
    observe(storage, sample(present=False))
    assert not storage.open_outage().start_known
    assert storage.recent_events() == []
    observe(storage, sample(5, present=False), minute=5)
    assert len(storage._db.execute('SELECT id FROM outages').fetchall()) == 1
    observe(storage, sample(10), minute=10)
    assert storage.outage_summary(int(BASE.timestamp()), int(BASE.timestamp()) + 1800) == (1, 600, True)


def test_response_without_grid_measurements_does_not_fake_an_outage(storage):
    observe(storage, sample())
    partial = parse_snapshot({"dataTime": int((BASE + timedelta(minutes=5)).timestamp() * 1000)})
    observe(storage, partial, minute=5)
    assert storage.get_last_present() is True
    assert storage.recent_events() == []
    assert storage.get_value('last_valid_at') == str(int(BASE.timestamp()))


def test_telemetry_gap_marks_outage_duration_uncertain(storage):
    observe(storage, sample())
    observe(storage, sample(5, present=False), minute=5)
    observe(storage, None, minute=21)
    assert storage.open_outage().has_gaps
    observe(storage, sample(30), minute=30)
    assert storage.outage_summary(int(BASE.timestamp()), int(BASE.timestamp()) + 3600) == (1, 1500, True)


def test_open_outage_stops_accumulating_when_measurements_stop(storage):
    observe(storage, sample())
    observe(storage, sample(5, present=False), minute=5)
    observe(storage, sample(10, present=False), minute=10)
    assert storage.outage_summary(int(BASE.timestamp()), int(BASE.timestamp()) + 7200) == (1, 300, False)


def test_outage_summary_clips_cross_midnight_intervals(storage):
    midnight = datetime(2026, 9, 6, tzinfo=KYIV)
    middle = int(midnight.timestamp())
    storage.start_outage(middle - 600, 100, True)
    storage.end_outage(middle + 1200, 90)
    assert storage.outage_summary(middle - 86400, middle) == (1, 600, False)
    assert storage.outage_summary(middle, middle + 86400) == (1, 1200, False)


def test_duration_uses_instants_across_dst_clock_jump(storage):
    began = datetime(2026, 3, 29, 2, 50, tzinfo=KYIV)
    ended = datetime(2026, 3, 29, 4, 10, tzinfo=KYIV)
    storage.start_outage(int(began.timestamp()), 100, True)
    storage.end_outage(int(ended.timestamp()), 90)
    assert storage.outage_summary(int(began.timestamp()), int(ended.timestamp())) == (1, 1200, False)


def test_observation_rollback_includes_state_events_outage_and_queue(storage, monkeypatch):
    observe(storage, sample())
    def fail(*args):
        raise sqlite3.OperationalError('simulated write error')
    monkeypatch.setattr(storage, 'add_telemetry', fail)
    with pytest.raises(sqlite3.OperationalError):
        observe(storage, sample(5, present=False, soc=20), minute=5)
    assert storage.get_last_present() is True
    assert storage.get_last_battery_soc() == 100
    assert storage.get_value('last_processed_at') == str(int(BASE.timestamp()))
    assert storage.get_value('last_valid_at') == str(int(BASE.timestamp()))
    assert storage.open_outage() is None
    assert storage.pending_notifications() == []
    assert storage.recent_events() == []


def test_retention_prunes_telemetry_but_not_outages_or_events(storage):
    storage.add_telemetry(int(BASE.timestamp()) - 91 * 86400, 90, 500, True)
    storage.add_telemetry(int(BASE.timestamp()) - 89 * 86400, 90, 500, True)
    storage.add_event('gridLost', 90, BASE - timedelta(days=100))
    storage.start_outage(int(BASE.timestamp()) - 100 * 86400, 90, True)
    storage.end_outage(int(BASE.timestamp()) - 100 * 86400 + 100, 80)
    observe(storage, sample())
    assert len(storage.telemetry_between(0, int(BASE.timestamp()))) == 2
    assert len(storage.recent_events()) == 1
    assert storage._db.execute('SELECT COUNT(*) FROM outages').fetchone()[0] == 1


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-Infinity'])
def test_invalid_numeric_fields_are_not_stored_as_power(value):
    snapshot = parse_snapshot({'emsSoc': value, 'totalConsumPower': value})
    assert snapshot.soc is None
    assert snapshot.load_power is None
