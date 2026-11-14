"""Async facade for the SQLite coordinator and object store."""

import asyncio
import hashlib
import logging
from time import perf_counter
from uuid import uuid4

from starlette.concurrency import run_in_threadpool

from app.application.fingerprints import content_fingerprint
from app.domain.parsing import ParseOptions, ParseOutput

logger = logging.getLogger(__name__)


class JobFailed(Exception):
    pass


class QueueService:
    def __init__(self, settings, queue_settings, *, store, storage):
        if not queue_settings.worker_token:
            raise ValueError("WORKER_API_TOKEN is required in queue mode")
        self.settings = settings
        self.config = queue_settings
        self.store = store
        self.storage = storage
        self.closed = False
        self.ready = True
        self.last_error = None

    async def submit(
        self,
        payload,
        pages,
        encode_img,
        disable_cache,
        tier=None,
        upload_token=None,
        idempotency_key=None,
        timings=None,
        options: ParseOptions | None = None,
        priority=0,
    ):
        owned_upload = upload_token is None
        if owned_upload:
            upload_token = await run_in_threadpool(self.store.reserve_upload)
        tier = tier or self.settings.tier
        options = options or ParseOptions()
        fingerprint = content_fingerprint(
            payload, tier, encode_img, options, self.config.model_revision
        )
        if idempotency_key:
            idempotency_key = hashlib.sha256(idempotency_key.encode()).hexdigest()
        input_key = f"inputs/{upload_token or uuid4().hex}.pdf"
        try:
            started = perf_counter()
            await run_in_threadpool(self.storage.put, input_key, payload)
            measurements = dict(timings or {})
            measurements["storage.input"] = perf_counter() - started
            job, reused = await run_in_threadpool(
                self.store.submit,
                fingerprint=fingerprint,
                input_key=input_key,
                pages=pages,
                tier=tier,
                encode_img=encode_img,
                disable_cache=disable_cache,
                idempotency_key=idempotency_key,
                upload_token=upload_token,
                options=options,
                priority=priority,
            )
            try:
                await run_in_threadpool(self.store.observe, tier, measurements)
            except Exception as exc:
                logger.warning("Upload metric update failed: %s", type(exc).__name__)
            return job, reused
        finally:
            if owned_upload:
                await run_in_threadpool(self.store.release_upload, upload_token)

    async def result(self, job):
        markdown = await run_in_threadpool(self.storage.get, job["output_key"])
        return ParseOutput(markdown.decode("utf-8"), job["pages"])

    async def wait(self, job_id, *, timeout=None):
        async def poll():
            while True:
                job = await run_in_threadpool(self.store.get, job_id)
                if not job or job["state"] == "failed":
                    raise JobFailed("PDF parsing failed")
                if job["state"] == "succeeded":
                    return await self.result(job)
                await asyncio.sleep(self.config.poll_seconds)

        return await asyncio.wait_for(
            poll(), timeout=self.settings.request_timeout if timeout is None else timeout
        )

    async def maintenance(self):
        while not self.closed:
            try:
                await run_in_threadpool(self.store.maintenance)
                for key in await run_in_threadpool(self.store.garbage):
                    await run_in_threadpool(self.storage.delete, key)
                    await run_in_threadpool(self.store.forget_garbage, key)
                self.last_error = None
            except Exception as exc:
                self.last_error = type(exc).__name__
                logger.warning("Coordinator maintenance failed: %s", type(exc).__name__)
            await asyncio.sleep(min(5, self.config.heartbeat_seconds))

    async def close(self):
        self.closed = True
