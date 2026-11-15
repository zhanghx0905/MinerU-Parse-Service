"""Composition root: assemble domain use cases, adapters and HTTP entrypoints."""

import asyncio
from contextlib import asynccontextmanager, suppress
from importlib.metadata import version

from fastapi import FastAPI

from app.config import QueueSettings, Settings
from app.http.errors import install_error_handlers
from app.http.jobs import router as job_router
from app.http.middleware import ApiAuth, TransferGuard, UploadGuard
from app.http.parsing import router as parse_router
from app.queue.database import coordinator_lock
from app.queue.service import QueueService
from app.queue.store import QueueStore
from app.storage import LocalStorage


def create_app(settings: Settings | None = None, *, queue_settings: QueueSettings | None = None):
    settings = settings or Settings.from_env()
    queue_settings = queue_settings or QueueSettings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        with coordinator_lock(queue_settings.database):
            app.state.service = QueueService(
                settings,
                queue_settings,
                store=QueueStore(settings, queue_settings),
                storage=LocalStorage(queue_settings.object_dir),
            )
            maintenance = asyncio.create_task(app.state.service.maintenance())
            try:
                yield
            finally:
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

    return app


app = create_app()
