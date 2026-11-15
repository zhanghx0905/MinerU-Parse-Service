"""Durable HTTP contracts and live-request capacity under one coordinator."""

import asyncio
import io
import threading
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from app.config import QueueSettings, Settings
from app.main import create_app
from app.models import ParseOutput
from app.worker import Worker
from tests.helpers import make_pdf


@pytest.fixture
def configuration(tmp_path):
    return Settings(preload_models=False, request_timeout=2), QueueSettings(
        database=str(tmp_path / "queue.db"),
        object_dir=str(tmp_path / "objects"),
        worker_token="worker",
        poll_seconds=0.005,
    )


@pytest.mark.parametrize(
    "name,payload,status",
    [
        ("x.txt", b"hi", 400),
        ("x.pdf", b"not a pdf", 400),
        ("empty.pdf", b"", 400),
        ("broken.pdf", b"%PDF-1.7\nbroken", 400),
        ("large.pdf", b"a" * 2001, 413),
        ("pages.pdf", make_pdf(2), 413),
    ],
)
def test_rejects_invalid_files(configuration, name, payload, status):
    settings, config = configuration
    app = create_app(replace(settings, max_upload_bytes=2000, max_pages=1), queue_settings=config)
    with TestClient(app) as client:
        response = client.post("/jobs", files={"file": (name, payload)})
        assert response.status_code == status
        assert client.get("/health").json()["pending"] == 0


def test_encrypted_pdf(configuration):
    writer = PdfWriter()
    writer.add_blank_page(600, 800)
    writer.encrypt("secret")
    payload = io.BytesIO()
    writer.write(payload)
    settings, config = configuration
    with TestClient(create_app(settings, queue_settings=config)) as client:
        response = client.post("/jobs", files={"file": ("locked.pdf", payload.getvalue())})
        assert response.status_code == 400 and response.json()["statusText"] == "pdfEncrypted"


def test_cache_options_bypass_and_health_through_worker(configuration):
    settings, config = configuration

    def parse(_payload, encoded, tier, _options):
        return ParseOutput(tier + (" ![](data:image/png;base64,YQ==)" if encoded else ""), 1)

    with TestClient(create_app(settings, queue_settings=config)) as client:
        upload = {"file": ("test.PDF", make_pdf())}
        first = client.post("/jobs", files=upload).json()
        client.headers["Authorization"] = "Bearer worker"
        worker = Worker(settings, config, parse=parse, client=client)
        worker.execute(worker.claim())
        assert "data:image" in client.get(first["result_url"]).json()["markdown"]
        assert client.post("/jobs", files=upload).json()["id"] == first["id"]
        plain = client.post("/jobs?encode_img=false&tier=flash", files=upload).json()
        worker.execute(worker.claim())
        assert client.get(plain["result_url"]).json()["markdown"] == "flash"
        bypassed = [
            client.post("/jobs?disable_cache=true", files=upload).json()["id"] for _ in range(2)
        ]
        assert len(set([first["id"], plain["id"], *bypassed])) == 4
        assert client.post("/jobs?tier=unknown", files=upload).status_code == 422
        health = client.get("/health").json()
        assert health["status"] == "ok" and health["completed"] == 2


def test_second_api_is_rejected_and_shutdown_releases_ownership(configuration):
    settings, config = configuration
    with TestClient(create_app(settings, queue_settings=config)):
        with pytest.raises(RuntimeError, match="Only one API process"):
            with TestClient(create_app(settings, queue_settings=config)):
                pytest.fail("Second coordinator acquired the database")
    with TestClient(create_app(settings, queue_settings=config)) as client:
        assert client.get("/health").status_code == 200


def test_upload_capacity_rejects_before_reading_and_releases_slots(configuration):
    settings, config = configuration
    app = create_app(settings, queue_settings=replace(config, max_uploads=1))

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        request = httpx.Request(
            "POST", "http://test/jobs", files={"file": ("test.pdf", make_pdf())}
        )
        body = request.read()

        async def slow_body():
            entered.set()
            await release.wait()
            yield body

        async def unread_body():
            raise AssertionError("Rejected request body was consumed")
            yield b""

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                pending = asyncio.create_task(
                    client.post("/jobs", content=slow_body(), headers=request.headers)
                )
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    response = await client.post("/jobs", content=unread_body())
                    assert response.status_code == 503
                finally:
                    release.set()
                assert (await pending).status_code == 202
                assert (
                    await client.post("/jobs", files={"file": ("test.pdf", make_pdf())})
                ).status_code == 202

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_sync_waiter_survives_upload_release_and_frees_on_exit(configuration, cancel):
    settings, config = configuration
    app = create_app(settings, queue_settings=replace(config, max_uploads=1, max_sync_waiters=1))

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def wait(_job_id, *, timeout):
            entered.set()
            await release.wait()
            return ParseOutput("parsed", 1)

        async with app.router.lifespan_context(app):
            app.state.service.wait = wait
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                upload = {"file": ("test.pdf", make_pdf())}
                pending = asyncio.create_task(client.post("/v2/parse/file", files=upload))
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    assert (await client.post("/v2/parse/file", files=upload)).status_code == 503
                    assert (await client.post("/jobs", files=upload)).status_code == 202
                    if cancel:
                        pending.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await pending
                    else:
                        release.set()
                        assert (await pending).status_code == 200
                finally:
                    release.set()
                    if not pending.done():
                        await pending
                assert (await client.post("/v2/parse/file", files=upload)).status_code == 200

    asyncio.run(scenario())


def test_transfer_capacity_releases_after_download(configuration):
    settings, config = configuration
    app = create_app(settings, queue_settings=replace(config, max_transfers=1))
    entered, release = threading.Event(), threading.Event()

    async def scenario():
        async with app.router.lifespan_context(app):
            service = app.state.service
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                job = (await client.post("/jobs", files={"file": ("test.pdf", make_pdf())})).json()
                attempt = service.store.claim("worker")
                service.storage.put(f"results/{attempt['lease_token']}.md", b"parsed")
                assert service.store.complete(job["id"], attempt["lease_token"], 1, {})
                get = service.storage.get

                def slow_get(key):
                    entered.set()
                    assert release.wait(3)
                    return get(key)

                service.storage.get = slow_get
                pending = asyncio.create_task(client.get(job["result_url"]))
                try:
                    assert await asyncio.to_thread(entered.wait, 2)
                    assert (await client.get(job["result_url"])).status_code == 503
                finally:
                    release.set()
                assert (await pending).status_code == 200
                assert (await client.get(job["result_url"])).status_code == 200

    asyncio.run(scenario())


@pytest.mark.parametrize("operation,status", [("reserve_upload", 503), ("submit", 500)])
def test_coordinator_error_releases_upload_and_sync_slots(
    configuration, monkeypatch, operation, status
):
    import sqlite3

    settings, config = configuration
    app = create_app(settings, queue_settings=replace(config, max_uploads=1, max_sync_waiters=1))
    with TestClient(app, raise_server_exceptions=False) as client:
        store = app.state.service.store
        original = getattr(store, operation)

        def failed(*_args, **_kwargs):
            raise sqlite3.OperationalError("private storage detail")

        monkeypatch.setattr(store, operation, failed)
        upload = {"file": ("test.pdf", make_pdf())}
        response = client.post("/v2/parse/file", files=upload)
        assert response.status_code == status
        assert "private storage detail" not in response.text
        monkeypatch.setattr(store, operation, original)

        async def completed(_job_id, *, timeout):
            return ParseOutput("parsed", 1)

        app.state.service.wait = completed
        assert client.post("/v2/parse/file", files=upload).status_code == 200


def test_upload_deadline_releases_capacity(configuration):
    settings, config = configuration
    app = create_app(settings, queue_settings=replace(config, upload_seconds=0.03, max_uploads=1))

    async def scenario():
        async def delayed():
            await asyncio.sleep(1)
            yield b"unused"

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/jobs",
                    content=delayed(),
                    headers={"Content-Type": "multipart/form-data; boundary=test"},
                )
                assert response.status_code == 408
                assert (
                    await client.post("/jobs", files={"file": ("test.pdf", make_pdf())})
                ).status_code == 202

    asyncio.run(scenario())


def test_sync_timeout_releases_waiter_without_cancelling_job(configuration):
    settings, config = configuration
    app = create_app(
        replace(settings, request_timeout=0.05),
        queue_settings=replace(config, max_uploads=1, max_sync_waiters=1),
    )
    with TestClient(app) as client:
        upload = {"file": ("test.pdf", make_pdf())}
        first = client.post("/v2/parse/file", files=upload)
        assert first.status_code == 504
        assert client.post("/v2/parse/file", files=upload).status_code == 504
        assert client.get(first.json()["status_url"]).json()["state"] == "queued"


def test_transfer_deadline_after_response_start_releases_capacity(configuration):
    from app.http.middleware import TransferGuard

    _, config = configuration

    async def scenario():
        delay = True

        async def streaming(_scope, _receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            if delay:
                await asyncio.sleep(1)
            await send({"type": "http.response.body", "body": b"parsed"})

        guard = TransferGuard(streaming, replace(config, max_transfers=1, transfer_seconds=0.02))
        messages = []

        async def send(message):
            messages.append(message)

        async def receive():
            return {"type": "http.request", "body": b""}

        scope = {"type": "http", "method": "GET", "path": "/jobs/test/result"}
        with pytest.raises(ConnectionError, match="Transfer deadline exceeded"):
            await guard(scope, receive, send)
        delay = False
        await guard(scope, receive, send)
        assert messages[-1]["body"] == b"parsed"

    asyncio.run(scenario())


def test_single_coordinator_accepts_concurrent_upload_burst(configuration):
    settings, config = configuration
    app = create_app(settings, queue_settings=config)

    async def scenario():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                payload = make_pdf()

                async def submit_batch():
                    return [
                        (
                            await client.post(
                                "/jobs?disable_cache=true", files={"file": ("sample.pdf", payload)}
                            )
                        ).status_code
                        for _ in range(4)
                    ]

                responses = await asyncio.gather(
                    *(submit_batch() for _ in range(64)), return_exceptions=True
                )
                assert all(isinstance(batch, list) and batch == [202] * 4 for batch in responses)
                assert app.state.service.store.stats()["queued"] == 256

    asyncio.run(scenario())


def test_default_page_limit_accepts_2000_and_rejects_2001(configuration):
    settings, config = configuration
    assert settings.max_upload_bytes == 209715200 and settings.max_pages == 2000
    with TestClient(create_app(settings, queue_settings=config)) as client:
        accepted = client.post("/jobs", files={"file": ("allowed.pdf", make_pdf(2000))})
        assert accepted.status_code == 202 and accepted.json()["pages"] == 2000
        rejected = client.post("/jobs", files={"file": ("too-many.pdf", make_pdf(2001))})
        assert rejected.status_code == 413
        assert client.get("/health").json()["pending"] == 1
