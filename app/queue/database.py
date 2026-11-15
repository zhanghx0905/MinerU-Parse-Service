"""Local SQLite WAL, transactional upgrades and online backup."""

import argparse
import fcntl
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from app.models import LEGACY_OPTIONS, TIERS

SCHEMA_VERSION = 4
_JOBS = """
CREATE TABLE jobs (
    id TEXT PRIMARY KEY, dedup_key TEXT UNIQUE,
    input_key TEXT NOT NULL, output_key TEXT,
    tier TEXT NOT NULL, encode_img INTEGER NOT NULL,
    pages INTEGER NOT NULL, priority INTEGER NOT NULL DEFAULT 0 CHECK(priority IN (0,1)),
    state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT, completed_token TEXT, lease_until REAL, worker_id TEXT,
    created REAL NOT NULL, queued REAL NOT NULL, available REAL NOT NULL,
    finished REAL, error TEXT, parse_options TEXT NOT NULL
)
"""
_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_schedule ON jobs(state,priority DESC,queued,id);
CREATE INDEX IF NOT EXISTS jobs_retention ON jobs(state,finished);
CREATE TABLE IF NOT EXISTS uploads (id TEXT PRIMARY KEY, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS garbage (key TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS workers (id TEXT PRIMARY KEY, seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS metrics (
    stage TEXT NOT NULL, tier TEXT NOT NULL,
    total REAL NOT NULL, buckets TEXT NOT NULL, PRIMARY KEY(stage,tier)
);
CREATE TABLE IF NOT EXISTS counters (event TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS processing_totals (
    tier TEXT PRIMARY KEY,
    completed_jobs INTEGER NOT NULL, completed_pages INTEGER NOT NULL,
    measured_jobs INTEGER NOT NULL, measured_pages INTEGER NOT NULL,
    parse_seconds REAL NOT NULL
);
"""


def ensure_safe_wal():
    version = sqlite3.sqlite_version_info
    fixed_backport = (3, 44, 6) <= version < (3, 45, 0) or (3, 50, 7) <= version < (3, 51, 0)
    if version < (3, 51, 3) and not fixed_backport:
        raise RuntimeError(
            "Concurrent WAL requires SQLite 3.51.3+ or a fixed backport. "
            "Use the API image or build scripts/build_sqlite.py and set LD_PRELOAD."
        )


@contextmanager
def coordinator_lock(database):
    """Request capacities are local: refuse a second API using the same database."""
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".api.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Only one API process may own a queue database") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _migrate_jobs(connection, version):
    """Copy retained facts; schema 1 needs its original output policy."""
    connection.execute(_JOBS.replace("CREATE TABLE jobs", "CREATE TABLE jobs_new"))
    old = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
    columns = [row[1] for row in connection.execute("PRAGMA table_info(jobs_new)")]
    defaults = {"priority": "0", "parse_options": "?"}
    expressions = []
    parameters = []
    for name in columns:
        if name == "dedup_key" and version == 1:
            expressions.append("NULL")
        elif name in old:
            expressions.append(name)
        else:
            expressions.append(defaults[name])
            if name == "parse_options":
                parameters.append(LEGACY_OPTIONS.to_json())
    connection.execute(
        f"INSERT INTO jobs_new ({','.join(columns)}) SELECT {','.join(expressions)} FROM jobs",
        parameters,
    )
    connection.execute("DROP TABLE jobs")
    connection.execute("ALTER TABLE jobs_new RENAME TO jobs")
    for table in ("idempotency", "waiters", "transfers", "limits"):
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    metric_columns = {row[1] for row in connection.execute("PRAGMA table_info(metrics)")}
    if "count" in metric_columns:
        connection.execute("ALTER TABLE metrics DROP COLUMN count")


class QueueDatabase:
    def __init__(self, database):
        ensure_safe_wal()
        self.database = database
        self._writer_lock = Lock()
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise ValueError("Queue requires a persistent local SQLite WAL database")
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
            if existing:
                row = connection.execute("SELECT version FROM schema_version").fetchone()
                if row is None or row[0] not in {1, 2, 3, SCHEMA_VERSION}:
                    raise ValueError("Unsupported queue schema version")
                if row[0] != SCHEMA_VERSION:
                    _migrate_jobs(connection, row[0])
            else:
                connection.execute(_JOBS)
            # executescript implicitly commits; keep all DDL in this transaction.
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute("DELETE FROM schema_version")
            connection.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
            connection.execute("DELETE FROM counters WHERE event='idempotency_hit'")
            connection.executemany(
                "INSERT OR IGNORE INTO processing_totals VALUES (?,0,0,0,0,0)",
                [(tier,) for tier in TIERS],
            )

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        with self._writer_lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise


def main():
    parser = argparse.ArgumentParser(description="Online backup of the queue database")
    parser.add_argument("--database", default=os.getenv("QUEUE_DATABASE", "data/queue.sqlite3"))
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    ensure_safe_wal()
    if not Path(args.database).is_file():
        parser.error("Queue database does not exist")
    if args.destination.resolve() == Path(args.database).resolve():
        parser.error("Backup destination must differ from the active database")
    with sqlite3.connect(Path(args.database).resolve().as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT version FROM schema_version").fetchone()
        if row is None or row[0] != SCHEMA_VERSION:
            parser.error("Unsupported queue database schema")
        args.destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(args.destination) as destination:
            db.backup(destination)
    print("SQLite backup completed")


if __name__ == "__main__":
    main()
