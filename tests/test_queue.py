"""Real SQLite transactions, HTTP coordination, and durable recovery."""

import asyncio
import json
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import QueueSettings, Settings
from app.main import create_app
from app.models import LeaseLost, ServiceBusy
from app.queue.store import QueueStore
from tests.helpers import make_pdf as pdf
from tests.helpers import submit


@pytest.fixture
def configuration(tmp_path):
    return Settings(preload_models=False, max_pending=8), QueueSettings(
        database=str(tmp_path / "jobs.db"),
        object_dir=str(tmp_path / "objects"),
        worker_token="test-worker-token",
        poll_seconds=0.01,
    )


def claim_in_process(settings, config):
    return QueueStore(settings, config).claim(uuid4().hex)


def test_concurrent_processes_claim_distinct_jobs_and_share_budget(configuration):
    settings, config = configuration
    store = QueueStore(settings, config)
    for _ in range(8):
        submit(store)
    with pytest.raises(ServiceBusy):
        submit(QueueStore(settings, config))
    with ProcessPoolExecutor(max_workers=4) as pool:
        claimed = list(pool.map(claim_in_process, [settings] * 12, [config] * 12))
    assert len({job["id"] for job in claimed if job}) == 8
    assert store.stats()["running"] == 8


def test_global_deduplication_and_cache_bypass(configuration):
    settings, config = configuration
    stores = [QueueStore(settings, config), QueueStore(settings, config)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = list(pool.map(lambda i: submit(stores[i % 2], "identical"), range(16)))
    assert len({job["id"] for job, _ in jobs}) == 1
    a, _ = submit(stores[0], "bypass", disable_cache=True)
    b, _ = submit(stores[0], "bypass", disable_cache=True)
    assert a["id"] != b["id"]


def test_expired_lease_is_retried_and_stale_worker_cannot_commit(configuration):
    settings, config = configuration
    now = [100.0]
    store = QueueStore(settings, replace(config, max_attempts=2), clock=lambda: now[0])
    original, _ = submit(store)
    first = store.claim(uuid4().hex)
    now[0] += config.lease_seconds + 1
    store.maintenance()
    assert store.get(original["id"])["state"] == "queued"
    assert not store.complete(first["id"], first["lease_token"], 1, {})
    with pytest.raises(LeaseLost):
        store.heartbeat(first["id"], first["lease_token"])
    now[0] += 3
    second = store.claim(uuid4().hex)
    assert second["attempts"] == 2 and second["lease_token"] != first["lease_token"]
    assert store.complete(second["id"], second["lease_token"], 1, {"sdk.total": 1.2})
    assert store.complete(second["id"], second["lease_token"], 1, {"sdk.total": 1.2})
    rows, _ = store.metric_rows()
    assert json.loads(next(row for row in rows if row["stage"] == "sdk.total")["buckets"])[-1] == 1
    assert store.get(original["id"])["state"] == "succeeded"
    reopened = QueueStore(settings, config, clock=lambda: now[0])
    assert reopened.get(original["id"])["state"] == "succeeded"


def test_queue_deadline_retry_exhaustion_and_cleanup(configuration):
    settings, config = configuration
    now = [100.0]
    store = QueueStore(
        settings, replace(config, max_attempts=1, max_queue_seconds=300), clock=lambda: now[0]
    )
    job, _ = submit(store)
    attempt = store.claim(uuid4().hex)
    store.fail(job["id"], attempt["lease_token"])
    assert store.get(job["id"])["state"] == "failed"
    waiting, _ = submit(store)
    now[0] += store.config.max_queue_seconds + 1
    store.maintenance()
    assert store.get(waiting["id"])["error"] == "queue_timeout"
    now[0] += config.retention_seconds + 1
    store.maintenance()
    assert store.get(job["id"]) is None
    assert store.stats()["pending_pages"] == 0
    assert store.garbage()


def test_large_documents_use_the_same_capacity_as_small_documents(configuration):
    settings, config = configuration
    store = QueueStore(replace(settings, max_pending=2), config)
    first, _ = submit(store, pages=1000)
    second, _ = submit(store, pages=1000)
    with pytest.raises(ServiceBusy):
        submit(store)
    assert store.stats()["pending_pages"] == 2000
    assert store.claim(uuid4().hex)["id"] == first["id"]
    assert store.claim(uuid4().hex)["id"] == second["id"]


def test_expired_upload_does_not_delete_committed_job_input(configuration):
    settings, config = configuration
    now = [100.0]
    store = QueueStore(settings, config, clock=lambda: now[0])
    token = store.reserve_upload()
    key = f"inputs/{token}.pdf"
    store.submit(fingerprint="one", input_key=key, pages=1, tier="standard", encode_img=True)
    now[0] += config.upload_seconds + 11
    store.maintenance()
    assert key not in store.garbage()


def test_http_async_lifecycle_authentication_metrics_and_reuse(configuration):
    settings, config = configuration
    app = create_app(settings, queue_settings=config)
    headers = {"Authorization": "Bearer " + config.worker_token}
    from importlib.metadata import version

    with TestClient(app) as client:
        upload = {"file": ("private-name.pdf", pdf())}
        job = client.post("/jobs", files=upload).json()
        assert job["state"] == "queued" and "input_key" not in job
        assert client.get(job["result_url"]).status_code == 202
        assert client.post("/internal/worker/claim", json={}).status_code == 401
        claimed = client.post(
            "/internal/worker/claim",
            headers=headers,
            json={
                "worker_id": uuid4().hex,
                "model_revision": config.model_revision,
                "mineru_version": version("mineru"),
                "protocol_version": 3,
            },
        ).json()
        lease = {**headers, "X-Task-Lease": claimed["lease_token"]}
        assert client.get(f"/internal/jobs/{job['id']}/input", headers=lease).content.startswith(
            b"%PDF"
        )
        assert (
            client.post(f"/internal/jobs/{job['id']}/heartbeat", headers=lease).status_code == 200
        )
        done = client.post(
            f"/internal/jobs/{job['id']}/complete?pages=1",
            content=b"# Parsed content",
            headers=lease,
        )
        assert done.status_code == 200
        result = client.get(job["result_url"]).json()
        assert result["markdown"] == "# Parsed content"
        reused = client.post("/jobs", files=upload).json()
        assert reused["id"] == job["id"] and reused["reused"]
        metrics = client.get("/metrics")
        assert metrics.status_code == 200
        assert 'event="succeeded"' in metrics.text
        assert "private-name" not in metrics.text


def test_sync_waiter_timeout_does_not_cancel_durable_job(configuration, monkeypatch):
    settings, config = configuration
    with TestClient(
        create_app(replace(settings, request_timeout=5), queue_settings=config)
    ) as client:

        async def timed_out(_job_id, *, timeout):
            raise asyncio.TimeoutError

        monkeypatch.setattr(client.app.state.service, "wait", timed_out)
        response = client.post("/v2/parse/file?priority=1", files={"file": ("test.pdf", pdf())})
        assert response.status_code == 504
        assert client.get(response.headers["Location"]).json()["priority"] == 1
        assert client.get("/health").json()["pending"] == 1
        assert client.post("/jobs", files={"file": ("test.pdf", pdf())}).json()["reused"]


def test_both_submission_routes_still_enforce_the_document_page_limit(configuration):
    settings, config = configuration
    app = create_app(replace(settings, max_pages=1), queue_settings=config)
    with TestClient(app) as client:
        for route in ("/jobs", "/v2/parse/file"):
            response = client.post(route, files={"file": ("sample.pdf", pdf(2))})
            assert response.status_code == 413
        assert client.get("/health").json()["pending"] == 0


def test_expired_upload_cannot_publish_a_job(configuration):
    settings, config = configuration
    now = [100.0]
    store = QueueStore(settings, config, clock=lambda: now[0])
    token = store.reserve_upload()
    now[0] += config.upload_seconds + 11
    with pytest.raises(ServiceBusy):
        store.submit(
            fingerprint="one",
            input_key=f"inputs/{token}.pdf",
            pages=1,
            tier="standard",
            encode_img=True,
            upload_token=token,
        )
    assert store.stats()["queued"] == 0


def test_chunked_multipart_size_limit_is_enforced(configuration):
    settings, config = configuration

    async def scenario():
        app = create_app(replace(settings, max_upload_bytes=100), queue_settings=config)

        async def body():
            yield (
                b"--boundary\r\nContent-Disposition: form-data; "
                b'name="file"; filename="x.pdf"\r\n\r\n'
            )
            for _ in range(20):
                yield b"a" * 100000
            yield b"\r\n--boundary--\r\n"

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/jobs",
                    content=body(),
                    headers={"Content-Type": "multipart/form-data; boundary=boundary"},
                )
                assert response.status_code == 413

    asyncio.run(scenario())


@pytest.mark.parametrize("source_name", ["queue.sqlite3", "queue #?.sqlite3"])
def test_online_backup_preserves_jobs(configuration, tmp_path, monkeypatch, source_name):
    import sys

    from app.queue.database import main

    settings, config = configuration
    config = replace(config, database=str(tmp_path / source_name))
    store = QueueStore(settings, config)
    job, _ = submit(store)
    destination = tmp_path / "backup.sqlite3"
    monkeypatch.setattr(
        sys, "argv", ["queue_admin", "--database", config.database, str(destination)]
    )
    main()
    restored = QueueStore(settings, replace(config, database=str(destination)))
    assert restored.get(job["id"])["state"] == "queued"


def test_unfixed_sqlite_is_rejected(monkeypatch):
    import sqlite3

    from app.queue.database import ensure_safe_wal

    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 51, 2))
    with pytest.raises(RuntimeError, match="Concurrent WAL"):
        ensure_safe_wal()


def test_failed_input_is_retained_until_job_retention_expires(configuration):
    from app.storage import LocalStorage

    settings, config = configuration
    now = [100.0]
    store = QueueStore(settings, replace(config, max_attempts=1), clock=lambda: now[0])
    storage = LocalStorage(config.object_dir)
    token = store.reserve_upload()
    key = f"inputs/{token}.pdf"
    payload = pdf()
    storage.put(key, payload)
    job, _ = store.submit(
        fingerprint="recoverable",
        input_key=key,
        pages=1,
        tier="standard",
        encode_img=True,
        upload_token=token,
    )
    store.release_upload(token)
    attempt = store.claim(uuid4().hex)
    store.fail(job["id"], attempt["lease_token"])
    store.maintenance()
    assert store.get(job["id"])["state"] == "failed"
    assert key not in store.garbage()
    assert storage.get(key) == payload
    now[0] += config.retention_seconds + 1
    store.maintenance()
    assert store.get(job["id"]) is None
    assert key in store.garbage()


def test_successful_results_reuse_until_retention_then_reparse(configuration):
    settings, config = configuration
    now = [100.0]
    store = QueueStore(settings, config, clock=lambda: now[0])
    job, _ = submit(store, "same")
    attempt = store.claim(uuid4().hex)
    assert store.complete(job["id"], attempt["lease_token"], 1, {})
    now[0] += 3601
    assert submit(store, "same")[0]["id"] == job["id"]
    now[0] += config.retention_seconds
    following, reused = submit(store, "same")
    assert not reused and following["id"] != job["id"]
    assert store.get(job["id"]) is None
    assert f"results/{attempt['lease_token']}.md" in store.garbage()


def test_cache_bypass_does_not_displace_a_reusable_result(configuration):
    settings, config = configuration
    store = QueueStore(settings, config)
    original, _ = submit(store, "same")
    attempt = store.claim(uuid4().hex)
    assert store.complete(original["id"], attempt["lease_token"], 1, {})
    bypassed, reused = submit(store, "same", disable_cache=True)
    assert not reused and bypassed["id"] != original["id"]
    assert submit(store, "same")[0]["id"] == original["id"]
