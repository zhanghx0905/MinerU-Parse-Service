"""Submit, wait for and retain content-addressed parsing jobs."""

import asyncio
import hashlib
import json
import logging
from importlib.metadata import version
from time import perf_counter

from starlette.concurrency import run_in_threadpool

from app.models import ParseOptions, ParseOutput, Tier

logger = logging.getLogger(__name__)

# Change when output policy changes, even if the model/parser versions stay the same.
OUTPUT_REVISION = 2


def content_fingerprint(
    payload: bytes, tier: Tier, encode_img: bool, options: ParseOptions, model_revision: str
) -> str:
    """Identify reusable work by all output options and implementation/model revisions."""
    digest = hashlib.sha256(payload)
    digest.update(
        json.dumps(
            [
                OUTPUT_REVISION,
                tier,
                encode_img,
                options.to_dict(),
                version("mineru"),
                model_revision,
            ],
            sort_keys=True,
        ).encode()
    )
    return digest.hexdigest()


class JobFailed(Exception):
    pass


class QueueService:
    def __init__(self, settings, queue_settings, *, store, storage):
        if not queue_settings.worker_token:
            raise ValueError("WORKER_API_TOKEN is required")
        self.settings = settings
        self.config = queue_settings
        self.store = store
        self.storage = storage
        self.last_error = None

    async def submit(
        self,
        payload,
        pages,
        encode_img,
        disable_cache,
        tier,
        *,
        upload_token,
        timings,
        options: ParseOptions,
        priority=0,
    ):
        fingerprint = content_fingerprint(
            payload, tier, encode_img, options, self.config.model_revision
        )
        input_key = f"inputs/{upload_token}.pdf"
        started = perf_counter()
        await run_in_threadpool(self.storage.put, input_key, payload)
        measurements = dict(timings)
        measurements["storage.input"] = perf_counter() - started
        job, reused = await run_in_threadpool(
            self.store.submit,
            fingerprint=fingerprint,
            input_key=input_key,
            pages=pages,
            tier=tier,
            encode_img=encode_img,
            disable_cache=disable_cache,
            upload_token=upload_token,
            options=options,
            priority=priority,
        )
        try:
            await run_in_threadpool(self.store.observe, tier, measurements)
        except Exception as exc:
            logger.warning("Upload metric update failed: %s", type(exc).__name__)
        return job, reused

    async def result(self, job):
        markdown = await run_in_threadpool(self.storage.get, job["output_key"])
        return ParseOutput(markdown.decode("utf-8"), job["pages"])

    async def wait(self, job_id, *, timeout):
        async def poll():
            while True:
                job = await run_in_threadpool(self.store.get, job_id)
                if not job or job["state"] == "failed":
                    raise JobFailed("PDF parsing failed")
                if job["state"] == "succeeded":
                    return await self.result(job)
                await asyncio.sleep(self.config.poll_seconds)

        return await asyncio.wait_for(poll(), timeout)

    async def maintenance(self):
        while True:
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
