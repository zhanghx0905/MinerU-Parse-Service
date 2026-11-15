"""Consumer contract: actual FastGPT multipart fields through API, queue and worker."""

import asyncio
import json
import threading
from dataclasses import replace
from importlib.metadata import version
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import QueueSettings, Settings
from app.main import create_app
from app.models import ParseOptions, ParseOutput
from app.worker import Worker
from tests.helpers import make_pdf


class OptionsEngine:
    def __init__(self):
        self.calls = []

    def parse(self, payload, encode_img, tier, options):
        self.calls.append(options)
        return ParseOutput(options.to_json(), 1)

    def close(self):
        pass


@pytest.fixture
def queue_app(tmp_path):
    settings = Settings(preload_models=False, request_timeout=5)
    config = QueueSettings(
        database=str(tmp_path / "jobs.db"),
        object_dir=str(tmp_path / "objects"),
        worker_token="worker-secret",
        poll_seconds=0.005,
    )
    return create_app(settings, queue_settings=config), settings, config


def test_durable_options_survive_restart_and_reach_worker_and_cache(queue_app):
    app, settings, config = queue_app
    upload = {"file": ("file.pdf", make_pdf())}
    expected = ParseOptions(True, False, True, True)
    fields = {k: str(v).lower() for k, v in expected.to_dict().items()}
    with TestClient(app) as client:
        first = client.post("/jobs", files=upload, data=fields).json()
        default = client.post("/jobs", files=upload).json()
        assert first["id"] != default["id"]
        assert first["parse_options"] == expected.to_dict()
    with TestClient(create_app(settings, queue_settings=config)) as client:
        client.headers["Authorization"] = "Bearer " + config.worker_token
        engine = OptionsEngine()
        worker = Worker(settings, config, parse=engine.parse, client=client)
        worker.execute(worker.claim())
        result = client.get(first["result_url"])
        assert result.status_code == 200
        assert json.loads(result.json()["markdown"]) == expected.to_dict()
        reused = client.post("/jobs", files=upload, data=fields).json()
        assert reused["reused"] and reused["id"] == first["id"]
        assert engine.calls == [expected]


def test_sync_fastgpt_request_executes_through_real_worker_protocol(queue_app):
    app, settings, config = queue_app
    # A longer wait allows the test worker to run while the synchronous caller waits.
    app = create_app(replace(settings, request_timeout=5), queue_settings=config)
    with TestClient(app) as client:
        worker_client = TestClient(app)
        worker_client.headers["Authorization"] = "Bearer " + config.worker_token
        engine = OptionsEngine()
        worker = Worker(settings, config, parse=engine.parse, client=worker_client)
        stop = threading.Event()

        def run_worker():
            while not stop.wait(0.005):
                job = worker.claim()
                if job:
                    worker.execute(job)

        thread = threading.Thread(target=run_worker)
        thread.start()
        try:
            response = client.post(
                "/v2/parse/file",
                files={"file": ("file.pdf", make_pdf())},
                data={"image_analysis": "true", "keep_appendix": "false"},
            )
            assert response.status_code == 200
            assert response.json()["success"] is True
            assert response.json()["pages"] == 1
            assert (
                json.loads(response.json()["markdown"])
                == ParseOptions(image_analysis=True, keep_appendix=False).to_dict()
            )
            assert response.headers["X-Task-ID"]
        finally:
            stop.set()
            thread.join(timeout=5)
            worker_client.close()


def test_sync_timeout_exposes_recoverable_job_and_content_reuse(queue_app, monkeypatch):
    app, _, _ = queue_app
    with TestClient(app) as client:

        async def timed_out(_job_id, *, timeout):
            raise asyncio.TimeoutError

        monkeypatch.setattr(app.state.service, "wait", timed_out)
        upload = {"file": ("file.pdf", make_pdf())}
        response = client.post("/v2/parse/file", files=upload)
        assert response.status_code == 504
        job = response.json()
        assert job["statusText"] == "pdfParseFailed"
        assert response.headers["Location"] == job["status_url"]
        assert client.get(job["status_url"]).json()["state"] == "queued"
        assert client.post("/jobs", files=upload).json()["id"] == job["id"]
        changed = client.post("/jobs", files=upload, data={"image_analysis": "true"})
        assert changed.status_code == 202 and changed.json()["id"] != job["id"]


def test_old_worker_cannot_silently_ignore_options(queue_app):
    app, _, config = queue_app
    with TestClient(app) as client:
        client.post("/jobs", files={"file": ("file.pdf", make_pdf())})
        response = client.post(
            "/internal/worker/claim",
            headers={"Authorization": "Bearer " + config.worker_token},
            json={
                "worker_id": uuid4().hex,
                "mineru_version": version("mineru"),
                "model_revision": config.model_revision,
            },
        )
        assert response.status_code == 409
        assert client.get("/health").json()["queued"] == 1


def test_public_auth_rejects_before_body_is_read_and_uses_separate_credentials(queue_app):
    _, settings, config = queue_app
    app = create_app(replace(settings, api_key="public-secret"), queue_settings=config)

    async def scenario():
        async def unread_body():
            raise AssertionError("Unauthorized request body was read")
            yield b""  # pragma: no cover

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post("/v2/parse/file", content=unread_body())
                assert response.status_code == 401
                assert response.json()["statusText"] == "pdfParseFailed"
                assert (await client.get("/health")).status_code == 200
                upload = {"file": ("file.pdf", make_pdf())}
                created = await client.post(
                    "/jobs", files=upload, headers={"Authorization": "Bearer public-secret"}
                )
                assert created.status_code == 202
                assert (await client.get(created.json()["status_url"])).status_code == 401
                rejected = await client.get(
                    created.json()["status_url"],
                    headers={"Authorization": "Bearer " + config.worker_token},
                )
                assert rejected.status_code == 401
                internal = await client.post(
                    "/internal/worker/claim",
                    json={},
                    headers={"Authorization": "Bearer public-secret"},
                )
                assert internal.status_code == 401

    asyncio.run(scenario())


@pytest.mark.parametrize("route", ["/v2/parse/file", "/jobs"])
def test_invalid_options_are_rejected_instead_of_ignored(queue_app, route):
    app, _, _ = queue_app
    with TestClient(app) as client:
        response = client.post(
            route, files={"file": ("file.pdf", make_pdf())}, data={"chart_analysis": "perhaps"}
        )
        assert response.status_code == 422
        assert response.json()["statusText"] == "invalidParseFile"
        assert client.get("/health").json()["pending"] == 0


@pytest.mark.parametrize("route", ["/v2/parse/file", "/jobs"])
@pytest.mark.parametrize(
    "kind,expected",
    [
        ("encrypted", "pdfEncrypted"),
        ("damaged", "pdfCorrupted"),
        ("wrong-type", "unsupportedParseFileType"),
        ("empty", "invalidParseFile"),
    ],
)
def test_fastgpt_diagnostic_codes(queue_app, route, kind, expected):
    import io

    from pypdf import PdfWriter

    filename = "file.pdf"
    payload = b"not a PDF"
    if kind in {"encrypted", "empty"}:
        writer = PdfWriter()
        if kind == "encrypted":
            writer.add_blank_page(100, 100)
            writer.encrypt("secret")
        output = io.BytesIO()
        writer.write(output)
        payload = output.getvalue()
    if kind == "wrong-type":
        filename = "file.docx"
    app, _, _ = queue_app
    with TestClient(app) as client:
        response = client.post(route, files={"file": (filename, payload)})
        assert response.status_code == 400
        body = response.json()
        assert body["statusText"] == expected
        assert body["error"] and body["success"] is False
        assert client.get("/health").json()["pending"] == 0


def test_worker_retries_transfer_backpressure_without_reparsing(queue_app):
    app, settings, config = queue_app
    with TestClient(app) as client:
        job = client.post("/jobs", files={"file": ("file.pdf", make_pdf())}).json()
        client.headers["Authorization"] = "Bearer " + config.worker_token
        engine = OptionsEngine()
        worker = Worker(settings, config, parse=engine.parse, client=client)
        execute_request = client.request
        completions = []

        def request(method, path, **kwargs):
            if path.endswith("/complete"):
                completions.append(path)
                if len(completions) == 1:
                    return httpx.Response(
                        503,
                        headers={"Retry-After": "0"},
                        request=httpx.Request(method, "http://test" + path),
                    )
            return execute_request(method, path, **kwargs)

        claimed = worker.claim()
        client.request = request
        worker.execute(claimed)
        assert client.get(job["status_url"]).json()["state"] == "succeeded"
        assert client.get(job["status_url"]).json()["attempts"] == 1
        assert len(engine.calls) == 1 and len(completions) == 2


def test_filtered_empty_markdown_is_a_valid_durable_result(queue_app):
    app, settings, config = queue_app

    class EmptyEngine:
        def parse(self, *_args):
            return ParseOutput("", 1)

    with TestClient(app) as client:
        job = client.post("/jobs", files={"file": ("file.pdf", make_pdf())}).json()
        client.headers["Authorization"] = "Bearer " + config.worker_token
        worker = Worker(settings, config, parse=EmptyEngine().parse, client=client)
        worker.execute(worker.claim())
        response = client.get(job["result_url"])
        assert response.status_code == 200
        assert response.json()["markdown"] == "" and response.json()["pages"] == 1


def test_validation_time_is_deducted_from_sync_wait_budget(queue_app, monkeypatch):
    import time

    _, settings, config = queue_app
    app = create_app(replace(settings, request_timeout=0.4), queue_settings=config)
    captured = []

    def validate(_payload, _max_pages):
        time.sleep(0.06)
        return 1

    async def wait(_job_id, *, timeout):
        captured.append(timeout)
        return ParseOutput("parsed", 1)

    monkeypatch.setattr("app.http.parsing.validate_pdf", validate)
    with TestClient(app) as client:
        monkeypatch.setattr(app.state.service, "wait", wait)
        response = client.post("/v2/parse/file", files={"file": ("file.pdf", make_pdf())})
        assert response.status_code == 200
        assert 0 < captured[0] < 0.35
