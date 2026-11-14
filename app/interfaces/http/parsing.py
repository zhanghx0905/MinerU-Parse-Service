"""FastGPT synchronous parsing and asynchronous submission share one upload contract."""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from app.application.queue import QueueService
from app.domain.jobs import IdempotencyConflict, ServiceBusy
from app.domain.parsing import ParseOptions, Tier
from app.infrastructure.pdf import validate_pdf
from app.interfaces.http.admission import release_upload
from app.interfaces.http.errors import ParseError, error_response
from app.interfaces.http.jobs import public_job

logger = logging.getLogger(__name__)
router = APIRouter()


class ParseResponse(BaseModel):
    success: bool = True
    message: str = ""
    markdown: str
    pages: int
    duration: float


@dataclass
class Submission:
    payload: bytes
    pages: int
    options: ParseOptions
    encode_img: bool
    disable_cache: bool
    tier: Tier
    idempotency_key: str | None
    timings: dict[str, float]
    priority: int = 0


async def prepare_submission(
    request: Request,
    file: Annotated[UploadFile, File()],
    keep_header_footer: Annotated[bool, Form()] = False,
    keep_appendix: Annotated[bool, Form()] = True,
    image_analysis: Annotated[bool, Form()] = False,
    chart_analysis: Annotated[bool, Form()] = False,
    disable_cache: bool = False,
    encode_img: bool = True,
    tier: Tier | None = None,
    priority: Annotated[int, Query(ge=0, le=1)] = 0,
):
    """Read FastGPT form flags; existing cache/image/tier controls remain query parameters."""
    settings = request.app.state.service.settings
    try:
        if not file.filename or not file.filename.lower().endswith(".pdf"):
            raise ParseError(400, "Unsupported file type; upload a PDF", "unsupportedParseFileType")
        payload = await file.read(settings.max_upload_bytes + 1)
        if len(payload) > settings.max_upload_bytes:
            raise ParseError(413, "Uploaded PDF is too large", "invalidParseFile")
        started = time.perf_counter()
        pages = await run_in_threadpool(validate_pdf, payload, settings.max_pages)
        key = request.headers.get("Idempotency-Key")
        if key is not None and (not key.strip() or len(key) > 128):
            raise ParseError(
                400, "Idempotency-Key must contain 1 to 128 characters", "invalidParseFile"
            )
        return Submission(
            payload=payload,
            pages=pages,
            options=ParseOptions(keep_header_footer, keep_appendix, image_analysis, chart_analysis),
            encode_img=encode_img,
            disable_cache=disable_cache,
            tier=tier or settings.tier,
            priority=priority,
            idempotency_key=key,
            timings={
                "api.validate": time.perf_counter() - started,
                "api.upload": getattr(request.state, "upload_seconds", 0),
            },
        )
    finally:
        await file.close()


def remaining_wait(request):
    """The synchronous budget includes upload/validation/submission, not just polling."""
    service = request.app.state.service
    remaining = service.settings.request_timeout - (time.perf_counter() - request.state.started)
    if remaining <= 0:
        raise asyncio.TimeoutError
    return remaining


async def submit_durable(request, submission):
    """Publish a validated input with identical option/idempotency handling on both routes."""
    service = request.app.state.service
    if not isinstance(service, QueueService):
        raise ParseError(503, "Durable queue mode is not enabled")
    try:
        return await service.submit(
            submission.payload,
            submission.pages,
            submission.encode_img,
            submission.disable_cache,
            submission.tier,
            upload_token=request.state.upload_token,
            idempotency_key=submission.idempotency_key,
            timings=submission.timings,
            options=submission.options,
            priority=submission.priority,
        )
    except ServiceBusy as exc:
        raise ParseError(503, str(exc), headers={"Retry-After": "5"}) from exc
    except IdempotencyConflict as exc:
        raise ParseError(409, str(exc), "invalidParseFile") from exc


@router.post("/v2/parse/file", response_model=ParseResponse)
async def parse(request: Request, submission: Annotated[Submission, Depends(prepare_submission)]):
    """Wait for a result; timeout leaves durable work available through its task URL."""
    service = request.app.state.service
    job = None
    try:
        remaining_wait(request)
        if isinstance(service, QueueService):
            job, _ = await submit_durable(request, submission)
            submission.payload = b""
            await release_upload(request)
            result = await service.wait(job["id"], timeout=remaining_wait(request))
        else:
            if submission.priority:
                raise ParseError(503, "Priority scheduling requires durable queue mode")
            if submission.idempotency_key:
                raise ParseError(503, "Idempotency-Key requires durable queue mode")
            await release_upload(request)
            result = await service.parse(
                submission.payload,
                submission.encode_img,
                submission.disable_cache,
                submission.tier,
                submission.options,
                timeout=remaining_wait(request),
            )
    except HTTPException:
        raise
    except ServiceBusy as exc:
        raise ParseError(503, str(exc), headers={"Retry-After": "5"}) from exc
    except asyncio.TimeoutError:
        extra = {}
        headers = {"Retry-After": "1"}
        if job:
            extra = {key: public_job(job)[key] for key in ("id", "status_url", "result_url")}
            headers.update({"Location": extra["status_url"], "X-Task-ID": job["id"]})
        return error_response(
            504,
            "Timed out waiting for PDF parsing",
            headers=headers,
            duration=time.perf_counter() - request.state.started,
            **extra,
        )
    except Exception as exc:
        logger.warning("PDF parsing failed: %s", type(exc).__name__)
        return error_response(500, "PDF parsing failed; see server logs")
    finally:
        await release_upload(request)

    content = ParseResponse(
        markdown=result.markdown,
        pages=result.pages,
        duration=time.perf_counter() - request.state.started,
    )
    headers = {"X-Task-ID": job["id"], "Location": f"/jobs/{job['id']}"} if job else None
    return JSONResponse(content.model_dump(), headers=headers)


@router.post("/jobs", status_code=202)
async def submit_job(
    request: Request, submission: Annotated[Submission, Depends(prepare_submission)]
):
    try:
        job, reused = await submit_durable(request, submission)
        return JSONResponse(
            public_job(job) | {"reused": reused},
            202,
            headers={"Location": f"/jobs/{job['id']}"},
        )
    finally:
        await release_upload(request)
