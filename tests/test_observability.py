"""Backlog, weighted parsing speed and throughput share durable completion semantics."""

from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from app.config import QueueSettings, Settings
from app.infrastructure.metrics import render_metrics
from app.infrastructure.sqlite.jobs import QueueStore
from app.infrastructure.sqlite.observability import read_observability
from app.main import create_app
from tests.test_api import make_pdf


def submit(store, pages, fingerprint=None):
    return store.submit(
        fingerprint=fingerprint or uuid4().hex,
        input_key=f"inputs/{uuid4().hex}.pdf",
        pages=pages,
        tier="standard",
        encode_img=True,
    )[0]


@pytest.fixture
def store(tmp_path):
    now = [1000.0]
    settings = Settings(service_mode="queue", preload_models=False)
    config = QueueSettings(database=str(tmp_path / "queue.db"))
    return QueueStore(settings, config, clock=lambda: now[0]), now


def test_backlog_distinguishes_queued_and_running_without_inventing_speed(store):
    coordinator, now = store
    assert read_observability(coordinator)["parsing"]["average_seconds_per_page"] is None
    assert (
        'mineru_processed_pages_total{tier="standard"} 0.0' in render_metrics(coordinator).decode()
    )
    submit(coordinator, 2)
    submit(coordinator, 9)
    coordinator.claim(uuid4().hex)
    now[0] += 7
    snapshot = read_observability(coordinator)
    assert snapshot["queue"]["queued"] == 1
    assert snapshot["queue"]["running"] == 1
    assert snapshot["queue"]["pending"] == 2
    assert snapshot["queue"]["pending_pages"] == 11
    assert snapshot["queue"]["oldest_wait_seconds"] == 7
    assert snapshot["parsing"]["average_pages_per_second"] is None


def test_only_successful_publications_count_once_and_totals_survive_retention(store):
    coordinator, now = store
    first = submit(coordinator, 2, "same-document")
    attempt = coordinator.claim(uuid4().hex)
    assert coordinator.complete(first["id"], attempt["lease_token"], 2, {"worker.parse": 6})
    assert coordinator.complete(first["id"], attempt["lease_token"], 2, {"worker.parse": 999})
    # A cache hit serves more requests without representing additional GPU work.
    assert submit(coordinator, 2, "same-document")["id"] == first["id"]

    second = submit(coordinator, 8)
    attempt = coordinator.claim(uuid4().hex)
    assert coordinator.complete(second["id"], attempt["lease_token"], 8, {"worker.parse": 4})

    third = submit(coordinator, 1)
    lost = coordinator.claim(uuid4().hex)
    coordinator.fail(third["id"], lost["lease_token"])
    now[0] += 3
    retry = coordinator.claim(uuid4().hex)
    assert not coordinator.complete(third["id"], lost["lease_token"], 1, {"worker.parse": 500})
    assert coordinator.complete(third["id"], retry["lease_token"], 1, {})

    parsing = read_observability(coordinator)["parsing"]
    assert parsing["completed_jobs"] == 3 and parsing["completed_pages"] == 11
    assert parsing["measured_jobs"] == 2 and parsing["measured_pages"] == 10
    assert parsing["parse_seconds"] == 10
    assert parsing["average_seconds_per_page"] == 1
    assert parsing["average_pages_per_second"] == 1
    assert parsing["recent_pages_per_minute"] == pytest.approx(11 / 5)

    reopened = QueueStore(coordinator.settings, coordinator.config, clock=lambda: now[0])
    assert read_observability(reopened)["parsing"] == parsing
    now[0] += coordinator.config.retention_seconds + 1
    reopened.maintenance()
    after_cleanup = read_observability(reopened)["parsing"]
    assert after_cleanup["completed_pages"] == 11
    assert after_cleanup["average_seconds_per_page"] == 1
    assert after_cleanup["recent_completed_pages"] == 0


def test_recent_throughput_uses_retained_wall_clock_window(store):
    coordinator, now = store
    coordinator.config = replace(coordinator.config, retention_seconds=120)
    job = submit(coordinator, 6)
    attempt = coordinator.claim(uuid4().hex)
    coordinator.complete(job["id"], attempt["lease_token"], 6, {"worker.parse": 12})
    parsing = read_observability(coordinator)["parsing"]
    assert parsing["recent_window_seconds"] == 120
    assert parsing["recent_pages_per_minute"] == 3
    assert parsing["average_seconds_per_page"] == 2
    now[0] += 121
    assert read_observability(coordinator)["parsing"]["recent_pages_per_minute"] == 0


def test_stats_and_prometheus_expose_the_same_persisted_observations(tmp_path):
    settings = Settings(service_mode="queue", preload_models=False, api_key="observer-key")
    config = QueueSettings(
        database=str(tmp_path / "queue.db"),
        object_dir=str(tmp_path / "objects"),
        worker_token="worker-key",
    )
    with TestClient(create_app(settings, queue_settings=config)) as client:
        assert client.get("/stats").status_code == 401
        client.headers["Authorization"] = "Bearer observer-key"
        job = client.post("/jobs", files={"file": ("file.pdf", make_pdf(2))}).json()
        coordinator = client.app.state.service.store
        attempt = coordinator.claim(uuid4().hex)
        coordinator.complete(job["id"], attempt["lease_token"], 2, {"worker.parse": 4})
        stats = client.get("/stats").json()
        assert stats["queue"]["pending"] == 0
        assert stats["parsing"]["average_seconds_per_page"] == 2
        assert stats["parsing"]["average_pages_per_second"] == 0.5
        metrics = client.get("/metrics")
        samples = {
            sample.name: sample.value
            for family in text_string_to_metric_families(metrics.text)
            for sample in family.samples
            if sample.labels.get("tier") == "standard" and "stage" not in sample.labels
        }
        assert samples["mineru_processed_pages_total"] == 2
        assert samples["mineru_measured_pages_total"] == 2
        assert samples["mineru_processing_seconds_total"] == 4
        assert samples["mineru_parse_seconds_per_page"] == 2
