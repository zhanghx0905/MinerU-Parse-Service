"""GPU workers use authenticated HTTP; the SQLite file stays on the coordinator."""

import argparse
import json
import logging
import os
import threading
import time
from importlib.metadata import version
from uuid import uuid4

import httpx

from app.config import QueueSettings, Settings
from app.domain.parsing import ParseOptions
from app.infrastructure.mineru.engine import MinerUEngine
from app.interfaces import WORKER_PROTOCOL_VERSION

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings, queue_settings, engine=None, client=None, fatal_exit=os._exit):
        if not queue_settings.worker_token:
            raise ValueError("WORKER_API_TOKEN is required")
        os.environ.setdefault("MINERU_PROFILE_STAGES", "true")
        self.settings = settings
        self.config = queue_settings
        self.engine = engine or MinerUEngine(settings.tier)
        self.client = client or httpx.Client(
            base_url=queue_settings.worker_api_url.rstrip("/"),
            headers={"Authorization": "Bearer " + queue_settings.worker_token},
            timeout=min(5, queue_settings.heartbeat_seconds / 2),
            trust_env=False,
            limits=httpx.Limits(max_connections=settings.parse_workers * 4 + 2),
        )
        self.id = uuid4().hex
        self.stop = threading.Event()
        self.fatal_exit = fatal_exit
        self.max_run_seconds = float(os.getenv("WORKER_MAX_RUN_SECONDS", "6000"))

    def claim(self, min_priority=0):
        response = self.client.post(
            "/internal/worker/claim",
            json={
                "worker_id": self.id,
                "min_priority": min_priority,
                "protocol_version": WORKER_PROTOCOL_VERSION,
                "mineru_version": version("mineru"),
                "model_revision": self.config.model_revision,
            },
        )
        response.raise_for_status()
        if response.status_code == 204:
            return None
        job = response.json()
        if job.get("protocol_version") != WORKER_PROTOCOL_VERSION:
            raise RuntimeError("Coordinator worker protocol does not match; upgrade coordinator")
        return job

    def execute(self, job):
        job_id, token = job["id"], job["lease_token"]
        headers = {"X-Task-Lease": token}
        done = threading.Event()
        lost = threading.Event()
        started = time.monotonic()
        monitor = threading.Thread(
            target=self._monitor_lease,
            args=(job, headers, done, lost, started),
            daemon=True,
        )
        monitor.start()
        try:
            download_started = time.perf_counter()
            response = self._transfer(
                "GET", f"/internal/jobs/{job_id}/input", lost, headers=headers, timeout=60
            )
            payload = response.content
            download_seconds = time.perf_counter() - download_started
            parse_started = time.perf_counter()
            result = self.engine.parse(
                payload, job["encode_img"], job["tier"], ParseOptions(**job["parse_options"])
            )
            parse_seconds = time.perf_counter() - parse_started
            if lost.is_set():
                return
            content = result.markdown.encode("utf-8")
            if len(content) > job["max_result_bytes"]:
                raise ValueError("Result exceeds coordinator limit")
            timings = {
                **result.timings,
                "worker.download": download_seconds,
                "worker.parse": parse_seconds,
            }
            # The same lease can retry a commit whose HTTP acknowledgement was lost.
            self._transfer(
                "POST",
                f"/internal/jobs/{job_id}/complete",
                lost,
                content=content,
                params={"pages": result.pages},
                headers={
                    **headers,
                    "Content-Type": "text/markdown; charset=utf-8",
                    "X-Parse-Timings": json.dumps(timings),
                },
                timeout=60,
            )
        except Exception as exc:
            logger.exception("Task attempt failed: job=%s type=%s", job_id, type(exc).__name__)
            if not lost.is_set():
                try:
                    response = self.client.post(f"/internal/jobs/{job_id}/fail", headers=headers)
                    response.raise_for_status()
                except Exception:
                    pass  # The coordinator recovers the task after its lease expires.
        finally:
            done.set()
            monitor.join(timeout=self.config.heartbeat_seconds + 6)

    def _monitor_lease(self, job, headers, done, lost, started):
        """Keep ownership alive independently of blocking native model execution."""
        deadline = started + job["lease_seconds"] * 0.8
        while not done.wait(self.config.heartbeat_seconds):
            try:
                response = self.client.post(
                    f"/internal/jobs/{job['id']}/heartbeat", headers=headers
                )
                response.raise_for_status()
                deadline = time.monotonic() + response.json()["lease_seconds"] * 0.8
            except Exception as exc:
                rejected = (
                    isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 409
                )
                if rejected or time.monotonic() >= deadline:
                    lost.set()
            if lost.is_set() or time.monotonic() - started > self.max_run_seconds:
                # Python cannot safely cancel a native GPU call in a thread.
                logger.error("Worker lease or execution deadline expired")
                self.fatal_exit(70)
                return

    def _transfer(self, method, path, lost, **kwargs):
        """Absorb brief transfer backpressure before spending another parse attempt."""
        for attempt in range(3):
            if lost.is_set():
                raise RuntimeError("Task lease lost during transfer")
            try:
                response = self.client.request(method, path, **kwargs)
                if response.status_code not in {429, 502, 503, 504} or attempt == 2:
                    response.raise_for_status()
                    return response
                delay = min(5, max(0, float(response.headers.get("Retry-After", attempt + 1))))
            except httpx.TransportError:
                if attempt == 2:
                    raise
                delay = attempt + 1
            lost.wait(delay)

    def _slot(self, min_priority=0):
        while not self.stop.is_set():
            try:
                job = self.claim(min_priority)
                if job:
                    self.execute(job)
                    continue
            except Exception as exc:
                logger.warning("Worker coordinator request failed: %s", type(exc).__name__)
            self.stop.wait(self.config.poll_seconds)

    def run(self):
        if self.settings.parse_workers != 1:
            raise ValueError(
                "A worker process handles one document; use WorkerPool for concurrency"
            )
        try:
            if self.settings.preload_models:
                self.engine.warmup()
            self._slot(int(self.settings.priority_workers > 0))
        finally:
            self.client.close()
            self.engine.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    from app.interfaces.worker_pool import WorkerPool

    WorkerPool(Settings.from_env(), QueueSettings.from_env()).run()


if __name__ == "__main__":
    main()
