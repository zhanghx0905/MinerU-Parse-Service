"""SQLite connection policy and transactional schema migrations; no stored configuration."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.domain.parsing import LEGACY_OPTIONS, TIERS
from app.infrastructure.sqlite.runtime import ensure_safe_wal

SCHEMA_VERSION = 3
_SCHEMA = """

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
    dedup_key TEXT UNIQUE, idempotency_key TEXT UNIQUE,
    input_key TEXT NOT NULL, output_key TEXT,
    tier TEXT NOT NULL, encode_img INTEGER NOT NULL,
    pages INTEGER NOT NULL, priority INTEGER NOT NULL DEFAULT 0 CHECK(priority IN (0,1)),
    state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT, completed_token TEXT, lease_until REAL, worker_id TEXT,
    created REAL NOT NULL, queued REAL NOT NULL, available REAL NOT NULL,
    started REAL, finished REAL, cache_until REAL,
    error TEXT, result_bytes INTEGER NOT NULL DEFAULT 0,
    timings TEXT NOT NULL DEFAULT '{}',
    parse_options TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, job_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idempotency_job ON idempotency(job_id);
CREATE INDEX IF NOT EXISTS jobs_retention ON jobs(state, finished);
CREATE INDEX IF NOT EXISTS jobs_cache_expiry ON jobs(cache_until)
    WHERE dedup_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS uploads (id TEXT PRIMARY KEY, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS waiters (id TEXT PRIMARY KEY, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS transfers (
    id TEXT PRIMARY KEY, expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS garbage (key TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS workers (id TEXT PRIMARY KEY, seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS metrics (
    stage TEXT NOT NULL, tier TEXT NOT NULL, count INTEGER NOT NULL,
    total REAL NOT NULL, buckets TEXT NOT NULL, PRIMARY KEY(stage, tier)
);
CREATE TABLE IF NOT EXISTS counters (
    event TEXT PRIMARY KEY, value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS processing_totals (
    tier TEXT PRIMARY KEY,
    completed_jobs INTEGER NOT NULL, completed_pages INTEGER NOT NULL,
    measured_jobs INTEGER NOT NULL, measured_pages INTEGER NOT NULL,
    parse_seconds REAL NOT NULL
);

"""


class QueueDatabase:
    """Own connection/transaction mechanics so scheduling methods only express policy."""

    def __init__(self, database):
        ensure_safe_wal()
        self.database = database
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise ValueError("Queue requires a persistent local SQLite WAL database")
        with self.transaction() as connection:
            # executescript() implicitly commits, so execute each DDL statement
            # inside the same transaction as the migration and version update.
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(statement)
            row = connection.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                connection.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
            elif row[0] in {1, 2}:
                if row[0] == 1:
                    # Keep the original recognition policy for old tasks.
                    legacy = LEGACY_OPTIONS.to_json().replace("'", "''")
                    connection.execute(
                        "ALTER TABLE jobs ADD COLUMN parse_options TEXT NOT NULL DEFAULT '"
                        + legacy
                        + "'"
                    )
                    connection.execute("UPDATE jobs SET dedup_key=NULL")
                connection.execute("DROP INDEX IF EXISTS jobs_schedule")
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(priority IN (0,1))"
                )
                connection.execute("ALTER TABLE jobs DROP COLUMN cost")
                connection.execute("ALTER TABLE jobs DROP COLUMN lane")
                connection.execute("DROP TABLE IF EXISTS limits")
                connection.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))
            elif row[0] != SCHEMA_VERSION:
                raise ValueError("Unsupported queue schema version")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_schedule ON jobs(state,priority DESC,queued,id)"
            )
            # Export zero counters before the first completion so rate() has a baseline.
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
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
