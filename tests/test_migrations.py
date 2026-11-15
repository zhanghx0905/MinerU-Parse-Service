"""Upgrade independent historical databases without losing jobs or published results."""

import json
import sqlite3
from uuid import uuid4

import pytest

from app.config import QueueSettings, Settings
from app.models import LEGACY_OPTIONS, ParseOptions
from app.queue import database
from app.queue.store import QueueStore
from app.storage import LocalStorage

# Frozen schema shipped by 1.3.1; independent of the current DDL.
SCHEMA_V3 = """

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


def legacy_database(tmp_path, version):
    config = QueueSettings(
        database=str(tmp_path / "queue.db"), object_dir=str(tmp_path / "objects")
    )
    storage = LocalStorage(config.object_dir)
    with sqlite3.connect(config.database) as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA_V3)
        connection.execute("INSERT INTO schema_version VALUES (?)", (version,))
        for state in ("queued", "running", "succeeded", "failed"):
            job_id, lease = uuid4().hex, uuid4().hex
            input_key, output_key = f"inputs/{uuid4().hex}.pdf", f"results/{lease}.md"
            storage.put(input_key, b"source")
            if state == "succeeded":
                storage.put(output_key, b"published result")
            connection.execute(
                "INSERT INTO jobs (id,fingerprint,dedup_key,input_key,output_key,tier,encode_img,"
                "pages,priority,state,attempts,lease_token,completed_token,lease_until,worker_id,"
                "created,queued,available,started,finished,cache_until,error,result_bytes,timings,"
                "parse_options) VALUES (?,?,?,?,?,'standard',1,2,0,?,1,?,?,5000,'worker',"
                "10,10,10,20,?,?,NULL,16,?,?)",
                (
                    job_id,
                    state,
                    state if state != "failed" else None,
                    input_key,
                    output_key if state == "succeeded" else None,
                    state,
                    lease if state == "running" else None,
                    lease if state == "succeeded" else None,
                    30 if state in {"succeeded", "failed"} else None,
                    3600,
                    json.dumps({"worker.parse": 6}),
                    ParseOptions(keep_header_footer=True).to_json(),
                ),
            )
            connection.execute("INSERT INTO idempotency VALUES (?,?,?)", (state, state, job_id))
        connection.execute("INSERT INTO uploads VALUES ('pending-upload',1000)")
        connection.execute("INSERT INTO garbage VALUES ('existing-garbage')")
        connection.execute("INSERT INTO workers VALUES ('worker',90)")
        connection.execute(
            "INSERT INTO metrics VALUES ('sdk.total','standard',1,6,?)",
            (json.dumps([0] * 7 + [1, 1]),),
        )
        connection.execute("INSERT INTO counters VALUES ('succeeded',1)")
        connection.execute("INSERT INTO counters VALUES ('idempotency_hit',2)")
        connection.execute("INSERT INTO processing_totals VALUES ('standard',1,2,1,2,6)")
        if version in {1, 2}:
            connection.execute("ALTER TABLE jobs DROP COLUMN priority")
            connection.execute("ALTER TABLE jobs ADD COLUMN cost INTEGER NOT NULL DEFAULT 3")
            connection.execute("ALTER TABLE jobs ADD COLUMN lane TEXT NOT NULL DEFAULT 'short'")
            connection.execute("CREATE TABLE limits (max_pending INTEGER)")
            connection.execute("INSERT INTO limits VALUES (8)")
        if version == 1:
            connection.execute("ALTER TABLE jobs DROP COLUMN parse_options")
        before = [dict(row) for row in connection.execute("SELECT * FROM jobs")]
    return config, storage, before


@pytest.mark.parametrize("version", [1, 2, 3])
def test_upgrade_preserves_tasks_leases_results_and_measurements(tmp_path, version):
    config, storage, before = legacy_database(tmp_path, version)
    store = QueueStore(Settings(), config, clock=lambda: 100)
    retired = {
        "fingerprint",
        "idempotency_key",
        "cache_until",
        "result_bytes",
        "timings",
        "started",
        "cost",
        "lane",
    }
    for old in before:
        current = store.get(old["id"])
        for key in old.keys() - retired - {"dedup_key", "parse_options"}:
            assert current[key] == old[key]
        assert not current.keys() & retired
        assert current["priority"] == 0
        assert current["dedup_key"] == (None if version == 1 else old["dedup_key"])
        expected = LEGACY_OPTIONS if version == 1 else ParseOptions(keep_header_footer=True)
        assert ParseOptions.from_json(current["parse_options"]) == expected
        assert storage.get(current["input_key"]) == b"source"
        if current["state"] == "succeeded":
            assert storage.get(current["output_key"]) == b"published result"
    with store.connect() as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 4
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert not tables & {"idempotency", "waiters", "transfers", "limits", "jobs_new"}
        assert connection.execute("SELECT count(*) FROM uploads").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM garbage").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM workers").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM counters WHERE event='idempotency_hit'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT completed_pages FROM processing_totals WHERE tier='standard'"
            ).fetchone()[0]
            == 2
        )
        assert connection.execute("SELECT total FROM metrics").fetchone()[0] == 6
        assert "count" not in {row[1] for row in connection.execute("PRAGMA table_info(metrics)")}
    reopened = QueueStore(Settings(), config, clock=lambda: 100)
    running = next(row for row in before if row["state"] == "running")
    assert reopened.complete(running["id"], running["lease_token"], 2, {})
    assert reopened.complete(running["id"], running["lease_token"], 2, {})


@pytest.mark.parametrize("version", [1, 2, 3])
def test_migration_failure_rolls_back_schema_and_all_data(tmp_path, version, monkeypatch):
    config, _, before = legacy_database(tmp_path, version)
    migrate = database._migrate_jobs

    def fail_after_migration(connection, source_version):
        migrate(connection, source_version)
        raise sqlite3.OperationalError("simulated failure before version publication")

    monkeypatch.setattr(database, "_migrate_jobs", fail_after_migration)
    with pytest.raises(sqlite3.OperationalError, match="simulated failure"):
        QueueStore(Settings(), config)
    with sqlite3.connect(config.database) as connection:
        connection.row_factory = sqlite3.Row
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == version
        assert [dict(row) for row in connection.execute("SELECT * FROM jobs")] == before
        assert connection.execute("SELECT count(*) FROM idempotency").fetchone()[0] == 4
        assert "count" in {row[1] for row in connection.execute("PRAGMA table_info(metrics)")}
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='jobs_new'"
        ).fetchone()


def test_unknown_schema_is_rejected_without_modification(tmp_path):
    config, _, _ = legacy_database(tmp_path, 3)
    with sqlite3.connect(config.database) as connection:
        connection.execute("UPDATE schema_version SET version=99")
    with pytest.raises(ValueError, match="Unsupported queue schema"):
        QueueStore(Settings(), config)
    with sqlite3.connect(config.database) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 99
        assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 4
