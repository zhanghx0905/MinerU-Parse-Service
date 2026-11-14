"""Business priority, durable backlog and configuration changes across restarts."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from importlib.metadata import version
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import QueueSettings, Settings
from app.domain.jobs import ServiceBusy
from app.infrastructure.sqlite.jobs import QueueStore
from app.infrastructure.sqlite.observability import read_observability
from app.main import create_app
from tests.test_queue import pdf, submit


@pytest.fixture
def store(tmp_path):
    return QueueStore(
        Settings(service_mode="queue", preload_models=False),
        QueueSettings(database=str(tmp_path / "queue.db")),
    )


def test_default_upload_capacity_accepts_64_concurrent_reservations(store):
    with ThreadPoolExecutor(max_workers=16) as pool:
        tokens = list(pool.map(lambda _: store.reserve_upload(), range(64)))
    assert len(set(tokens)) == 64
    with pytest.raises(ServiceBusy):
        store.reserve_upload()
    for token in tokens:
        store.release_upload(token)
    token = store.reserve_upload()
    store.release_upload(token)


def test_env_is_the_only_source_of_capacity_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_PENDING", "2")
    monkeypatch.setenv("QUEUE_MAX_UPLOADS", "2")
    monkeypatch.setenv("QUEUE_DATABASE", str(tmp_path / "queue.db"))
    original = QueueStore(Settings.from_env(), QueueSettings.from_env())
    jobs = [submit(original)[0] for _ in range(2)]
    tokens = [original.reserve_upload() for _ in range(2)]

    monkeypatch.setenv("MAX_PENDING", "1")
    monkeypatch.setenv("QUEUE_MAX_UPLOADS", "1")
    lowered = QueueStore(Settings.from_env(), QueueSettings.from_env())
    assert read_observability(lowered)["queue"]["max_pending"] == 1
    assert all(lowered.get(job["id"])["state"] == "queued" for job in jobs)
    with pytest.raises(ServiceBusy):
        submit(lowered)
    with pytest.raises(ServiceBusy):
        lowered.reserve_upload()
    for token in tokens:
        lowered.release_upload(token)
    lowered.release_upload(lowered.reserve_upload())

    monkeypatch.setenv("MAX_PENDING", "3")
    raised = QueueStore(Settings.from_env(), QueueSettings.from_env())
    submit(raised)
    assert raised.stats()["queued"] == 3


def test_backlog_survives_a_day_without_a_worker(store):
    now = [1000.0]
    store.clock = lambda: now[0]
    job, _ = submit(store, pages=1000)
    now[0] += 86401
    store.maintenance()
    reopened = QueueStore(store.settings, store.config, clock=lambda: now[0])
    assert reopened.get(job["id"])["state"] == "queued"
    assert reopened.claim(uuid4().hex)["id"] == job["id"]


def test_priority_then_fifo_without_page_or_age_heuristics(store):
    now = [1000.0]
    store.clock = lambda: now[0]
    first, _ = submit(store, pages=1000)
    now[0] += 1
    second, _ = submit(store, pages=1)
    assert store.claim(uuid4().hex, min_priority=1) is None
    now[0] += 120
    urgent, _ = submit(store, pages=1000, priority=1)
    assert store.claim(uuid4().hex)["id"] == urgent["id"]
    assert store.claim(uuid4().hex)["id"] == first["id"]
    assert store.claim(uuid4().hex)["id"] == second["id"]


def test_six_general_and_two_reserved_slots_leave_capacity_for_urgent_jobs(store):
    for _ in range(8):
        submit(store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        attempts = list(
            pool.map(lambda minimum: store.claim(uuid4().hex, minimum), [0] * 6 + [1] * 2)
        )
    assert sum(job is not None for job in attempts) == 6
    urgent = [submit(store, priority=1)[0] for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        vip = list(pool.map(lambda _: store.claim(uuid4().hex, 1), range(2)))
    assert {job["id"] for job in vip} == {job["id"] for job in urgent}
    assert store.stats()["running"] == 8
    assert store.stats()["queued"] == 2


@pytest.mark.parametrize("explicit_key", [False, True])
def test_urgent_reuse_promotes_existing_task_even_when_queue_is_full(store, explicit_key):
    store.settings = replace(store.settings, max_pending=1)
    kwargs = {"idempotency_key": "same-request", "disable_cache": True} if explicit_key else {}
    first, _ = submit(store, "identical", **kwargs)
    urgent, reused = submit(store, "identical", priority=1, **kwargs)
    assert reused and urgent["id"] == first["id"] and urgent["priority"] == 1
    normal, _ = submit(store, "identical", **kwargs)
    assert normal["priority"] == 1
    attempt = store.claim(uuid4().hex, 1)
    store.fail(attempt["id"], attempt["lease_token"])
    reopened = QueueStore(store.settings, store.config)
    assert reopened.get(first["id"])["priority"] == 1
    assert reopened.stats()["queued"] == 1


def test_http_priority_and_reserved_claim_work_through_the_public_contract(tmp_path):
    settings = Settings(service_mode="queue", preload_models=False)
    config = QueueSettings(
        database=str(tmp_path / "queue.db"),
        object_dir=str(tmp_path / "objects"),
        worker_token="worker",
    )
    with TestClient(create_app(settings, queue_settings=config)) as client:
        upload = {"file": ("test.pdf", pdf())}
        regular = client.post("/jobs", files=upload).json()
        assert regular["priority"] == 0
        request = {
            "worker_id": uuid4().hex,
            "model_revision": config.model_revision,
            "mineru_version": version("mineru"),
            "protocol_version": 3,
            "min_priority": 1,
        }
        headers = {"Authorization": "Bearer worker"}
        assert (
            client.post("/internal/worker/claim", headers=headers, json=request).status_code == 204
        )
        response = client.post("/jobs?priority=1", files=upload)
        assert response.status_code == 202, response.text
        urgent = response.json()
        assert urgent["reused"] and urgent["priority"] == 1
        attempt = client.post("/internal/worker/claim", headers=headers, json=request).json()
        assert attempt["id"] == regular["id"]
        for route in ("/jobs", "/v2/parse/file"):
            assert client.post(route + "?priority=2", files=upload).status_code == 422


def test_priority_worker_count_must_fit_inside_execution_pool():
    with pytest.raises(ValueError, match="PRIORITY_WORKERS"):
        Settings(parse_workers=2, priority_workers=3)
    with pytest.raises(ValueError, match="PRIORITY_WORKERS"):
        Settings(priority_workers=-1)
    assert Settings(parse_workers=8, priority_workers=2).priority_workers == 2
