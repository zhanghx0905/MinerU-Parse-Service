"""Online backup for a coordinator-owned database."""

import argparse
import os
import sqlite3
from pathlib import Path

from app.infrastructure.sqlite.database import SCHEMA_VERSION
from app.infrastructure.sqlite.runtime import ensure_safe_wal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=os.getenv("QUEUE_DATABASE", "data/queue.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("destination", type=Path)
    args = parser.parse_args()
    ensure_safe_wal()
    if not Path(args.database).is_file():
        parser.error("Queue database does not exist")
    with sqlite3.connect(args.database, timeout=5) as connection:
        version = connection.execute("SELECT version FROM schema_version").fetchone()
        if not version or version[0] != SCHEMA_VERSION:
            parser.error("Unsupported queue database schema")
        if args.destination.resolve() == Path(args.database).resolve():
            parser.error("Backup destination must differ from the active database")
        args.destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(args.destination) as destination:
            connection.backup(destination)
        print("SQLite backup completed")


if __name__ == "__main__":
    main()
