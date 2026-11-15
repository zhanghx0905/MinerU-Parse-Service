"""Bounded-label metrics and optional SDK diagnostic timings."""

import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

from app.models import STAGES

_current = ContextVar("mineru_stage_timings", default=None)
_installed = False
_sink_lock = threading.Lock()
_pattern = re.compile(r"MinerU stage: name=([a-z_.]+), elapsed_s=([0-9.]+), pid=\d+")


def _sink(message):
    target = _current.get()
    if target is None:
        return
    match = _pattern.fullmatch(message.record["message"])
    if match and match[1] in STAGES:
        values, lock = target
        with lock:
            values[match[1]] = values.get(match[1], 0) + float(match[2])


@contextmanager
def record_sdk_stages(timings):
    global _installed
    from loguru import logger

    with _sink_lock:
        if not _installed:
            logger.add(_sink, filter=lambda record: record["name"] == "mineru.utils.timing")
            _installed = True
    token = _current.set((timings, threading.Lock()))
    try:
        yield
    finally:
        _current.reset(token)


@contextmanager
def timed(timings, stage):
    started = perf_counter()
    try:
        yield
    finally:
        timings[stage] = timings.get(stage, 0) + perf_counter() - started
