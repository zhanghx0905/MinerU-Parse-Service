"""Real spawn workers: independent PIDs, reserved capacity, crash recovery and shutdown."""

import json
import multiprocessing
import os
import signal
import socket
import threading
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import uvicorn

from app.config import QueueSettings, Settings
from app.main import create_app
from app.models import ParseOutput
from app.worker import WorkerPool, run_worker_process
from tests.helpers import make_pdf as pdf


class BlockingEngine:
    def parse(self, _payload, encode_img, _tier, _options):
        root = Path(os.environ["MINERU_TEST_GATE_DIR"])
        if encode_img:
            (root / f"bulk-{os.getpid()}").touch()
            deadline = time.monotonic() + 40
            while not (root / "release").exists():
                if time.monotonic() > deadline:
                    raise RuntimeError("Test release deadline exceeded")
                time.sleep(0.01)
        return ParseOutput("parsed", 1)

    def close(self):
        pass


def stub_worker(settings, queue_settings):
    root = Path(os.environ["MINERU_TEST_GATE_DIR"])
    (root / f"ready-{os.getpid()}").write_text(
        json.dumps(
            {
                "workers": settings.parse_workers,
                "priority": settings.priority_workers,
            }
        )
    )
    run_worker_process(settings, queue_settings, parse=BlockingEngine().parse)


def stub_pool(settings, queue_settings):
    WorkerPool(settings, queue_settings, worker_target=stub_worker).run()


def wait_until(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("Condition did not become true before the deadline")


def test_spawn_pool_reserves_vip_replaces_only_crashed_child_and_drains(tmp_path, monkeypatch):
    gate = tmp_path / "gate"
    gate.mkdir()
    monkeypatch.setenv("MINERU_TEST_GATE_DIR", str(gate))
    settings = Settings(preload_models=False, parse_workers=4, priority_workers=1)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    config = QueueSettings(
        database=str(tmp_path / "queue.db"),
        object_dir=str(tmp_path / "objects"),
        worker_token="worker",
        worker_api_url=f"http://127.0.0.1:{port}",
        poll_seconds=0.02,
        lease_seconds=5,
        heartbeat_seconds=0.5,
    )
    api_settings = replace(settings, parse_workers=1, priority_workers=0)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(api_settings, queue_settings=config),
            log_level="error",
            access_log=False,
        )
    )
    server_thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    server_thread.start()
    pool = multiprocessing.get_context("spawn").Process(target=stub_pool, args=(settings, config))
    try:
        wait_until(lambda: server.started)
        with httpx.Client(base_url=config.worker_api_url, timeout=5, trust_env=False) as client:
            upload = {"file": ("test.pdf", pdf())}
            for _ in range(4):
                assert client.post("/jobs?disable_cache=true", files=upload).status_code == 202
            pool.start()
            wait_until(lambda: len(list(gate.glob("ready-*"))) == 4)
            wait_until(lambda: len(list(gate.glob("bulk-*"))) == 3)
            roles = {
                int(path.name.removeprefix("ready-")): json.loads(path.read_text())
                for path in gate.glob("ready-*")
            }
            assert all(role["workers"] == 1 for role in roles.values())
            assert sum(role["priority"] for role in roles.values()) == 1
            vip_pid = next(pid for pid, role in roles.items() if role["priority"])
            response = client.post("/jobs?priority=1&encode_img=false", files=upload)
            assert response.status_code == 202
            job = response.json()
            wait_until(lambda: client.get(job["status_url"]).json()["state"] == "succeeded")
            assert not (gate / f"bulk-{vip_pid}").exists()
            assert client.get("/health").json()["running"] == 3

            crashed_pid = int(next(gate.glob("bulk-*")).name.removeprefix("bulk-"))
            os.kill(crashed_pid, signal.SIGKILL)
            wait_until(lambda: len(list(gate.glob("ready-*"))) == 5)
            replacement = next(
                p for p in gate.glob("ready-*") if int(p.name.removeprefix("ready-")) not in roles
            )
            assert json.loads(replacement.read_text()) == {"workers": 1, "priority": 0}
            replacement_pid = int(replacement.name.removeprefix("ready-"))
            wait_until(lambda: (gate / f"bulk-{replacement_pid}").exists())
            for pid in roles:
                if pid != crashed_pid:
                    os.kill(pid, 0)  # Other workers survive the crash.

            os.kill(pool.pid, signal.SIGTERM)
            time.sleep(0.2)
            assert pool.is_alive(), "The supervisor must wait for current parses to finish"
            (gate / "release").touch()
            pool.join(timeout=15)
            assert pool.exitcode == 0
            for path in gate.glob("ready-*"):
                with pytest.raises(ProcessLookupError):
                    os.kill(int(path.name.removeprefix("ready-")), 0)
    finally:
        (gate / "release").touch()
        if pool.pid is not None:
            if pool.is_alive():
                pool.terminate()
                pool.join(timeout=10)
            if pool.is_alive():
                pool.kill()
                pool.join(timeout=5)
            pool.close()
        for path in gate.glob("ready-*"):
            try:
                os.kill(int(path.name.removeprefix("ready-")), signal.SIGKILL)
            except ProcessLookupError:
                pass
        server.should_exit = True
        server_thread.join(timeout=5)
        listener.close()
