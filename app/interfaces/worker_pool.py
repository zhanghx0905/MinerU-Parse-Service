"""Supervise independent, CUDA-safe workers without an additional in-memory job queue."""

import logging
import multiprocessing
import signal
import threading
import time
from dataclasses import replace

logger = logging.getLogger(__name__)
RESTART_DELAY_SECONDS = 5


def run_worker_process(settings, queue_settings, *, engine=None):
    # Models and HTTP clients are created after spawn, never inherited from a
    # parent that may already have initialized CUDA or native library locks.
    from app.interfaces.worker import Worker

    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    worker = Worker(settings, queue_settings, engine=engine)
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
