"""Schema upgrades preserve tasks, leases, results and idempotency, without stored config."""

from dataclasses import replace
from uuid import uuid4

import pytest

from app.config import QueueSettings, Settings
from app.domain.parsing import LEGACY_OPTIONS, ParseOptions
from app.infrastructure.sqlite.database import SCHEMA_VERSION
from app.infrastructure.sqlite.jobs import QueueStore


def legacy_schema(store, version):
    """Restore the relevant v1/v2 schema and its old persisted limits."""
    with store.transaction() as connection:
        connection.execute("DROP INDEX jobs_schedule")
        connection.execute("ALTER TABLE jobs DROP COLUMN priority")
        connection.execute("ALTER TABLE jobs ADD COLUMN cost INTEGER NOT NULL DEFAULT 3")
        connection.execute("ALTER TABLE jobs ADD COLUMN lane TEXT NOT NULL DEFAULT 'short'")
        connection.execute("CREATE INDEX jobs_schedule ON jobs(state,available,queued)")
        connection.execute(
            "CREATE TABLE limits (id INTEGER PRIMARY KEY, max_pending INTEGER, "
            "max_work INTEGER, max_uploads INTEGER)"
        )
        connection.execute("INSERT INTO limits VALUES (1,8,4096,8)")
        if version == 1:
            connection.execute("ALTER TABLE jobs DROP COLUMN parse_options")
        connection.execute("UPDATE schema_version SET version=?", (version,))


@pytest.mark.parametrize("old_version", [1, 2])
def test_upgrade_preserves_jobs_and_uses_current_configuration(tmp_path, old_version):
    settings = Settings(service_mode="queue", preload_models=False)
    config = QueueSettings(database=str(tmp_path / "queue.db"))
    store = QueueStore(settings, config)
    jobs = []
    for state in ("succeeded", "running", "queued"):
        job, _ = store.submit(
            fingerprint=state,
            input_key=f"inputs/{uuid4().hex}.pdf",
            pages=1,
            tier="standard",
            encode_img=True,
            idempotency_key=state,
        )
        if state != "queued":
            attempt = store.claim(uuid4().hex)
            if state == "succeeded":
                assert store.complete(job["id"], attempt["lease_token"], 1, {})
        jobs.append(store.get(job["id"]))
    token = store.reserve_upload()
    legacy_schema(store, old_version)

    migrated = QueueStore(replace(settings, max_pending=1), replace(config, max_uploads=128))
    for before in jobs:
        after = migrated.get(before["id"])
        for key in ("state", "lease_token", "completed_token", "input_key", "output_key"):
            assert after[key] == before[key]
        assert after["priority"] == 0
        expected = LEGACY_OPTIONS if old_version == 1 else ParseOptions()
        assert ParseOptions.from_json(after["parse_options"]) == expected
        assert after["dedup_key"] == (None if old_version == 1 else before["dedup_key"])
    with migrated.connect() as connection:
        assert (
            connection.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
        )
        assert connection.execute("SELECT count(*) FROM idempotency").fetchone()[0] == 3
        assert connection.execute("SELECT id FROM uploads").fetchone()[0] == token
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='limits'").fetchone()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        assert not columns & {"cost", "lane"}
    reopened = QueueStore(settings, config)
    running = jobs[1]
    assert reopened.complete(running["id"], running["lease_token"], 1, {})


def test_failed_migration_rolls_back_all_schema_changes(tmp_path):
    settings = Settings(service_mode="queue", preload_models=False)
    config = QueueSettings(database=str(tmp_path / "queue.db"))
    store = QueueStore(settings, config)
    legacy_schema(store, 1)
    with store.transaction() as connection:
        # Prevent removing lane to fail after the earlier migration steps.
        connection.execute("CREATE INDEX custom_lane ON jobs(lane)")
    import sqlite3

    with pytest.raises(sqlite3.OperationalError):
        QueueStore(settings, config)
    with store.connect() as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 1
        columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        assert {"cost", "lane"} <= columns
        assert not columns & {"parse_options", "priority"}
        assert connection.execute("SELECT max_uploads FROM limits").fetchone()[0] == 8
