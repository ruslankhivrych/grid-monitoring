import asyncio
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import fsolar  # noqa: E402


@pytest.mark.parametrize(
    ("instant", "expected_date"),
    [
        ("2026-01-15T15:34:00+00:00", "2026-01-15 17:34:00"),
        ("2026-09-05T15:34:00+00:00", "2026-09-05 18:34:00"),
        ("2026-01-15T22:30:00+00:00", "2026-01-16 00:30:00"),
        ("2026-09-05T22:30:00+00:00", "2026-09-06 01:30:00"),
    ],
)
def test_snapshot_request_uses_device_time_on_utc_host(monkeypatch, instant, expected_date):
    frozen_instant = datetime.fromisoformat(instant)

    class UTCServerDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                # Simulate Docker/VPS using UTC as its local timezone.
                return frozen_instant.astimezone(timezone.utc).replace(tzinfo=None)
            return frozen_instant.astimezone(tz)

    monkeypatch.setattr(fsolar, "datetime", UTCServerDatetime)

    async def request_snapshot():
        client = fsolar.FSolarClient(
            "test-user",
            "test-password",
            "test-device",
            device_timezone=ZoneInfo("Europe/Kyiv"),
        )
        client._post = AsyncMock(
            return_value={"dataTimeStr": expected_date}
        )
        try:
            snapshot = await client.get_snapshot()
            client._post.assert_awaited_once_with(
                "/device/get_device_snapshot",
                {
                    "deviceSn": "test-device",
                    "deviceType": "OG",
                    "dateStr": expected_date,
                },
            )
            # A response without timeZone must use the client's configured zone,
            # including winter/summer offsets and dates around UTC midnight.
            assert snapshot.timestamp == frozen_instant
        finally:
            await client.aclose()

    asyncio.run(request_snapshot())


def test_source_clock_prevents_reported_five_hour_shift():
    snapshot = fsolar.parse_snapshot(
        {
            "dataTime": int(datetime(2026, 9, 4, 10, 30, tzinfo=timezone.utc).timestamp() * 1000),
            "dataTimeStr": "2026-09-04 18:30:00",
            "timeZone": "UTC+03:00",
        },
        device_timezone=ZoneInfo("Europe/Kyiv"),
    )

    assert snapshot.timestamp == datetime(2026, 9, 4, 15, 30, tzinfo=timezone.utc)
    assert snapshot.timestamp.astimezone(ZoneInfo("Europe/Kyiv")).strftime("%H:%M") == "18:30"


@pytest.mark.parametrize(
    ("source_zone", "expected_utc"),
    [
        ("UTC+03:00", "2026-01-15T15:30:00+00:00"),
        ("UTC+05:45", "2026-01-15T12:45:00+00:00"),
        ("UTC-03:30", "2026-01-15T22:00:00+00:00"),
    ],
)
def test_explicit_source_zone_overrides_device_timezone(source_zone, expected_utc):
    snapshot = fsolar.parse_snapshot(
        {"dataTimeStr": "2026-01-15 18:30:00", "timeZone": source_zone},
        device_timezone=ZoneInfo("Europe/Kyiv"),
    )

    assert snapshot.timestamp == datetime.fromisoformat(expected_utc)


def test_iso_timestamp_keeps_its_own_offset():
    snapshot = fsolar.parse_snapshot(
        {"dataTimeStr": "2026-09-04T18:30:00-07:00", "timeZone": "UTC+03:00"},
        device_timezone=ZoneInfo("Europe/Kyiv"),
    )

    assert snapshot.timestamp == datetime(2026, 9, 5, 1, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("as_text", [False, True])
def test_legacy_numeric_timestamp_stays_utc(as_text):
    expected = datetime(2026, 9, 4, 15, 30, tzinfo=timezone.utc)
    milliseconds = int(expected.timestamp() * 1000)
    snapshot = fsolar.parse_snapshot(
        {"dataTime": str(milliseconds) if as_text else milliseconds},
        device_timezone=ZoneInfo("Europe/Kyiv"),
    )

    assert snapshot.timestamp == expected


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"dataTimeStr": "2026-09-05"},
        {"dataTimeStr": "0001-01-01 00:00:00", "timeZone": "UTC+14:00"},
        {"dataTimeStr": "not-a-time", "dataTime": "invalid"},
        {"dataTime": 0},
        {"dataTime": "NaN"},
        {"dataTime": "Infinity"},
        {"dataTime": 10**30},
    ],
)
def test_unavailable_timestamp_is_not_replaced_with_current_time(data):
    assert fsolar.parse_snapshot(data).timestamp is None


def test_unknown_explicit_timezone_does_not_guess_from_device_or_numeric_time():
    snapshot = fsolar.parse_snapshot(
        {
            "dataTimeStr": "2026-09-04 18:30:00",
            "timeZone": "unsupported-zone",
            "dataTime": int(datetime(2026, 9, 4, 10, 30, tzinfo=timezone.utc).timestamp() * 1000),
        },
        device_timezone=ZoneInfo("Europe/Kyiv"),
    )

    assert snapshot.timestamp is None
