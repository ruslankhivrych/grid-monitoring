import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from config import load_config


@pytest.fixture(autouse=True)
def required_environment(monkeypatch):
    for key in ('TELEGRAM_BOT_TOKEN', 'FSOLAR_USERNAME', 'FSOLAR_PASSWORD', 'FSOLAR_DEVICE_SN'):
        monkeypatch.setenv(key, 'test')
    monkeypatch.setenv('POLL_INTERVAL_MINUTES', '3')
    monkeypatch.setenv('STALE_AFTER_MINUTES', '15')
    monkeypatch.setenv('TELEMETRY_RETENTION_DAYS', '90')


@pytest.mark.parametrize('value,expected', [
    ('123', {123}),
    ('123,456', {123, 456}),
    (' 123, 456 , -1001234567890 ', {123, 456, -1001234567890}),
    ('123,123,456', {123, 456}),
    ('+123,00456', {123, 456}),
    ('', set()),
    ('   ', set()),
    (None, set()),
])
def test_allowed_chat_id_accepts_single_and_multiple_ids(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv('ALLOWED_CHAT_ID', raising=False)
    else:
        monkeypatch.setenv('ALLOWED_CHAT_ID', value)
    config = load_config()
    assert config.allowed_chat_ids == frozenset(expected)
    assert isinstance(config.allowed_chat_ids, frozenset)


@pytest.mark.parametrize('value', [
    ',', '123,', ',123', '123,,456', '123, ,456', '123,abc', 'abc', '123;456',
    '123 456', '12.3', '1_234', '0', '123,0', '-0', '++123',
    str(2**63), str(-(2**63) - 1), '123,' + '9' * 5000,
])
def test_invalid_chat_id_list_stops_startup(monkeypatch, value):
    monkeypatch.setenv('ALLOWED_CHAT_ID', value)
    with pytest.raises(SystemExit, match='ALLOWED_CHAT_ID'):
        load_config()


def test_single_chat_and_one_minute_polling_remain_compatible(monkeypatch):
    monkeypatch.setenv('ALLOWED_CHAT_ID', '123')
    monkeypatch.setenv('POLL_INTERVAL_MINUTES', '1')
    config = load_config()
    assert config.allowed_chat_ids == frozenset({123})
    assert config.poll_interval_minutes == 1
