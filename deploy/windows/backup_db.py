"""Consistent SQLite backup; keep the newest 14 automatic copies by default."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3


def backup_database(database: Path, directory: Path, keep: int = 14) -> Path:
    database, directory = database.resolve(), directory.resolve()
    if keep < 1 or not database.is_file() or database.parent == directory:
        raise ValueError("Use an existing database, a separate backup directory, and keep >= 1")
    directory.mkdir(parents=True, exist_ok=True)
    name = "bot-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".db"
    destination = directory / name
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(destination)) as target:
                source.backup(target)
                if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("Backup integrity check failed")
    except Exception:
        if destination.is_file():
            destination.unlink()
        raise
    # Only our timestamped automatic copies are rotated; manual/migration copies remain.
    copies = sorted(directory.glob("bot-????????T????????????Z.db"), reverse=True)
    for old in copies[keep:]:
        if old.resolve().parent == directory and old.is_file() and not old.is_symlink():
            old.unlink()
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--keep", type=int, default=14)
    args = parser.parse_args()
    print(backup_database(args.database, args.directory, args.keep))
