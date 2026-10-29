import importlib.util
import logging
import os
from pathlib import Path
import sqlite3
import sys

import pytest


def windows_module(name):
    path = Path(__file__).resolve().parents[2] / 'deploy' / 'windows' / f'{name}.py'
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_windows_env_preserves_literal_secrets_and_uses_native_database(tmp_path, monkeypatch):
    launcher = windows_module('run_bot')
    monkeypatch.setattr(os, 'environ', os.environ.copy())
    project = tmp_path / 'app'
    (project / 'bot').mkdir(parents=True)
    (project / 'bot' / '.env').write_text(
        'FSOLAR_PASSWORD="literal-${PASSWORD} # secret"\nDB_PATH=/data/bot.db\n',
        encoding='utf-8-sig',
    )
    data = tmp_path / 'persistent-data'
    secrets = launcher.prepare_environment(project, data)
    assert os.environ['FSOLAR_PASSWORD'] == 'literal-${PASSWORD} # secret'
    assert os.environ['DB_PATH'] == str(data / 'bot.db')
    assert os.environ['MPLCONFIGDIR'] == str(data / 'matplotlib')
    assert 'literal-${PASSWORD} # secret' in secrets


def test_windows_logs_redact_tokens_and_credentials_in_tracebacks():
    launcher = windows_module('run_bot')
    token = '123456:AAAlongTelegramToken0123456789'
    password = 'test-password-4938'
    formatter = launcher.SecretFormatter((token, password))
    try:
        raise ValueError('Rejected token ' + token + ' password ' + password)
    except ValueError:
        record = logging.LogRecord('test', logging.ERROR, __file__, 1, 'Request %s', (token,), sys.exc_info())
        text = formatter.format(record)
    assert token not in text and password not in text
    assert '[REDACTED]' in text and 'Traceback' in text


def test_windows_backup_captures_wal_and_only_rotates_automatic_copies(tmp_path):
    backups = windows_module('backup_db')
    database = tmp_path / 'live.db'
    directory = tmp_path / 'backups'
    directory.mkdir()
    manual = directory / 'migration-original.db'
    manual.write_text('preserve manual backups')
    writer = sqlite3.connect(database)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE samples (value INTEGER)')
        writer.execute('INSERT INTO samples VALUES (42)')
        writer.commit()
        copies = [backups.backup_database(database, directory, keep=2) for _ in range(3)]
        assert not copies[0].exists()
        assert copies[1].exists() and copies[2].exists()
        assert manual.read_text() == 'preserve manual backups'
        reader = sqlite3.connect(copies[-1])
        try:
            assert reader.execute('SELECT value FROM samples').fetchall() == [(42,)]
            assert reader.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        finally:
            reader.close()
        assert writer.execute('SELECT value FROM samples').fetchall() == [(42,)]
    finally:
        writer.close()


def test_windows_backup_rejects_live_database_directory(tmp_path):
    backups = windows_module('backup_db')
    database = tmp_path / 'bot.db'
    database.touch()
    with pytest.raises(ValueError):
        backups.backup_database(database, tmp_path)
