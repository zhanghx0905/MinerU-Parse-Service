"""Upgrading a v1 database must preserve tasks, leases, results and idempotency."""

from dataclasses import replace
from uuid import uuid4

from app.config import QueueSettings, Settings
from app.domain.parsing import LEGACY_OPTIONS, ParseOptions
from app.infrastructure.sqlite.database import SCHEMA_VERSION
from app.infrastructure.sqlite.jobs import QueueStore


def test_v1_database_migrates_once_and_preserves_old_task_semantics(tmp_path):
    settings = Settings(service_mode="queue", preload_models=False)
    config = QueueSettings(database=str(tmp_path / "queue.db"))
    store = QueueStore(settings, config)
    job, _ = store.submit(
        fingerprint="old-fingerprint",
        input_key=f"inputs/{uuid4().hex}.pdf",
        pages=1,
        tier="standard",
        encode_img=True,
        idempotency_key="old-key",
    )
    running = store.claim(uuid4().hex)
    with store.transaction() as connection:
        connection.execute("ALTER TABLE jobs DROP COLUMN parse_options")
        connection.execute("UPDATE schema_version SET version=1")
    migrated = QueueStore(settings, config)
    recovered = migrated.get(job["id"])
    assert recovered["state"] == "running"
    assert recovered["lease_token"] == running["lease_token"]
    assert ParseOptions.from_json(recovered["parse_options"]) == LEGACY_OPTIONS
    assert recovered["dedup_key"] is None
    assert migrated.complete(job["id"], running["lease_token"], 1, {})
    with migrated.connect() as connection:
        assert (
            connection.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
        )
        assert connection.execute("SELECT job_id FROM idempotency").fetchone()[0] == job["id"]
    reopened = QueueStore(settings, config)
    assert reopened.get(job["id"])["state"] == "succeeded"
    new, _ = reopened.submit(
        fingerprint="new-fingerprint",
        input_key=f"inputs/{uuid4().hex}.pdf",
        pages=1,
        tier="standard",
        encode_img=True,
    )
    assert ParseOptions.from_json(new["parse_options"]) == ParseOptions()


def test_incompatible_limits_do_not_partially_commit_migration(tmp_path):
    settings = Settings(service_mode="queue", preload_models=False)
    config = QueueSettings(database=str(tmp_path / "queue.db"))
    store = QueueStore(settings, config)
    with store.transaction() as connection:
        connection.execute("ALTER TABLE jobs DROP COLUMN parse_options")
        connection.execute("UPDATE schema_version SET version=1")
    import pytest

    with pytest.raises(ValueError, match="identical queue limits"):
        QueueStore(replace(settings, max_pending=1), config)
    with store.connect() as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 1
        assert "parse_options" not in [
            row[1] for row in connection.execute("PRAGMA table_info(jobs)")
        ]
