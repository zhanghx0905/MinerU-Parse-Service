"""GPU workers use authenticated HTTP; the SQLite file stays on the coordinator."""

import argparse
import json
import logging
import multiprocessing
import os
import signal
import threading
import time
from dataclasses import replace
from importlib.metadata import version
from uuid import uuid4

import httpx

from app.config import QueueSettings, Settings
from app.models import WORKER_PROTOCOL_VERSION, ParseOptions
from app.parser.engine import parse_pdf, warmup

logger = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        settings,
        queue_settings,
        *,
        parse=parse_pdf,
        warmup=warmup,
        client=None,
        fatal_exit=os._exit,
    ):
        if not queue_settings.worker_token:
            raise ValueError("WORKER_API_TOKEN is required")
        os.environ.setdefault("MINERU_PROFILE_STAGES", "true")
        self.settings = settings
        self.config = queue_settings
        self.parse = parse
        self.warmup = warmup
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
            result = self.parse(
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
                self.warmup(self.settings.tier)
            self._slot(int(self.settings.priority_workers > 0))
        finally:
            self.client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    WorkerPool(Settings.from_env(), QueueSettings.from_env()).run()


RESTART_DELAY_SECONDS = 5


def run_worker_process(settings, queue_settings, *, parse=parse_pdf):
    # Models and HTTP clients are created after spawn, never inherited from a
    # parent that may already have initialized CUDA or native library locks.

    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    worker = Worker(settings, queue_settings, parse=parse)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_args: worker.stop.set())
    worker.run()


class WorkerPool:
    def __init__(self, settings, queue_settings, *, worker_target=run_worker_process):
        self.settings = settings
        self.config = queue_settings
        self.worker_target = worker_target
        self.context = multiprocessing.get_context("spawn")
        self.stop = threading.Event()
        self.children = {}
        self.retry_at = {}

    def _start(self, slot):
        priority = int(slot < self.settings.priority_workers)
        settings = replace(self.settings, parse_workers=1, priority_workers=priority)
        child = self.context.Process(
            target=self.worker_target,
            args=(settings, self.config),
            name=f"mineru-{'vip' if priority else 'general'}-{slot}",
        )
        child.start()
        self.children[slot] = child
        logger.info("Started parse process: slot=%s pid=%s priority=%s", slot, child.pid, priority)

    def run(self):
        previous_handlers = {}
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, lambda *_args: self.stop.set())
        try:
            for slot in range(self.settings.parse_workers):
                if self.stop.is_set():
                    break
                self._start(slot)
            while not self.stop.wait(1):
                for slot in range(self.settings.parse_workers):
                    child = self.children.get(slot)
                    if child is None:
                        if time.monotonic() >= self.retry_at[slot] and not self.stop.is_set():
                            self._start(slot)
                    elif child.exitcode is not None:
                        logger.warning(
                            "Parse process exited: slot=%s code=%s", slot, child.exitcode
                        )
                        child.join()
                        child.close()
                        del self.children[slot]
                        self.retry_at[slot] = time.monotonic() + RESTART_DELAY_SECONDS
        finally:
            # Forward termination to every child before waiting for any of them.
            # Workers finish their current document; Docker bounds the total
            # shutdown grace period and the coordinator recovers expired leases.
            for child in self.children.values():
                if child.is_alive():
                    child.terminate()
            for child in self.children.values():
                child.join()
                child.close()
            self.children.clear()
            for signum, previous in previous_handlers.items():
                signal.signal(signum, previous)


if __name__ == "__main__":
    main()
