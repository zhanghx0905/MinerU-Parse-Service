"""Composition root: assemble domain use cases, adapters and HTTP entrypoints."""

import asyncio
from contextlib import asynccontextmanager, suppress
from importlib.metadata import version

from fastapi import FastAPI

from app.application.parsing import ParseService
from app.application.queue import QueueService
from app.config import QueueSettings, Settings
from app.infrastructure.mineru.engine import MinerUEngine
from app.infrastructure.sqlite.jobs import QueueStore
from app.infrastructure.storage import create_storage
from app.interfaces.http.admission import TransferGuard, UploadGuard
from app.interfaces.http.auth import ApiAuth
from app.interfaces.http.errors import install_error_handlers
from app.interfaces.http.health import router as health_router
from app.interfaces.http.jobs import router as job_router
from app.interfaces.http.parsing import router as parse_router


def create_app(settings: Settings | None = None, *, engine=None, queue_settings=None):
    settings = settings or Settings.from_env()
    queue_settings = queue_settings or QueueSettings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        durable = settings.service_mode == "queue"
        if durable:
            app.state.service = QueueService(
                settings,
                queue_settings,
                store=QueueStore(settings, queue_settings),
                storage=create_storage(queue_settings),
            )
        else:
            app.state.service = ParseService(settings, engine or MinerUEngine(settings.tier))
        maintenance = None
        try:
            if durable:
                maintenance = asyncio.create_task(app.state.service.maintenance())
            elif settings.preload_models and engine is None:
                await app.state.service.warmup()
            yield
        finally:
            await app.state.service.close()
            if maintenance:
                maintenance.cancel()
                with suppress(asyncio.CancelledError):
                    await maintenance

    app = FastAPI(
        title="MinerU PDF Service", version=version("mineru-pdf-service"), lifespan=lifespan
    )

    app.add_middleware(UploadGuard, settings=settings, queue_settings=queue_settings)
    app.add_middleware(TransferGuard, queue_settings=queue_settings)
    app.add_middleware(
        ApiAuth, worker_token=queue_settings.worker_token, public_token=settings.api_key
    )
    install_error_handlers(app)
    app.include_router(parse_router)
    app.include_router(job_router)
    app.include_router(health_router)

    return app


app = create_app()
