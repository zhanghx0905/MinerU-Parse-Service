"""Service liveness and recent worker activity; not an inference readiness probe."""

import os
from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.application.queue import QueueService
from app.infrastructure.sqlite.observability import read_observability

router = APIRouter()


@router.get("/stats")
async def stats(request: Request):
    """Global queue backlog and parsing rates from the coordinator's durable read model."""
    service = request.app.state.service
    if not isinstance(service, QueueService):
        raise HTTPException(503, "Queue statistics require durable queue mode")
    return await run_in_threadpool(read_observability, service.store)


@router.get("/health")
async def health(request: Request):
    service = request.app.state.service
    settings = service.settings
    try:
        mineru_version = version("mineru")
    except PackageNotFoundError:
        mineru_version = "not installed"
    if isinstance(service, QueueService):
        stats = await run_in_threadpool(service.store.stats)
        return {
            "status": "ok",
            "mode": "queue",
            "service_version": version("mineru-pdf-service"),
            "mineru_version": mineru_version,
            "tier": settings.tier,
            "model_ready": stats["workers"] > 0,
            "last_error": service.last_error,
            "pending": stats["queued"] + stats["running"],
            "completed": stats["succeeded"],
            **stats,
        }
    return {
        "status": "stopping" if service.closed else "ok",
        "mineru_version": mineru_version,
        "tier": settings.tier,
        "pending": len(service.tasks),
        "completed": service.completed,
        "last_error": service.last_error,
        "model_ready": service.ready,
        "parse_workers": settings.parse_workers,
        "small_backend": os.getenv("MINERU_MODEL_SMALL_BACKEND", "auto"),
    }
