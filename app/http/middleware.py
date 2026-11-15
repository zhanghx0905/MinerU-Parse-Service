"""Authenticate and bound live requests before reading their bodies."""

import asyncio
import secrets
import time
from contextlib import suppress

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from app.http.errors import error_response
from app.models import ServiceBusy


class Capacity:
    """Event-loop-owned slots; process exit releases all live HTTP requests."""

    def __init__(self, limit):
        self.limit = limit
        self.active = 0

    def acquire(self):
        if self.active >= self.limit:
            raise ServiceBusy("Request capacity is full")
        self.active += 1

    def release(self):
        self.active -= 1


class ApiAuth:
    def __init__(self, app, worker_token, public_token=""):
        self.app = app
        self.worker_token = worker_token
        self.public_token = public_token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope["path"]
            internal = path.startswith("/internal/")
            public = path in {"/v2/parse/file", "/jobs", "/stats"} or path.startswith("/jobs/")
            token = self.worker_token if internal else self.public_token
            if internal or (public and token):
                supplied = dict(scope["headers"]).get(b"authorization", b"")
                if not token or not secrets.compare_digest(supplied, ("Bearer " + token).encode()):
                    return await error_response(401, "Authentication required")(
                        scope, receive, send
                    )
        await self.app(scope, receive, send)


async def release_upload(state):
    if not state.get("upload_released", True):
        state["upload_released"] = True
        try:
            await run_in_threadpool(state["upload_store"].release_upload, state["upload_token"])
        finally:
            state["upload_capacity"].release()


class UploadGuard:
    def __init__(self, app, settings, queue_settings):
        self.app = app
        self.settings = settings
        self.config = queue_settings
        self.uploads = Capacity(queue_settings.max_uploads)
        self.waiters = Capacity(queue_settings.max_sync_waiters)

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or scope["path"]
            not in {
                "/v2/parse/file",
                "/jobs",
            }
        ):
            return await self.app(scope, receive, send)
        state = scope.setdefault("state", {})
        state["started"] = time.perf_counter()
        limit = self.settings.max_upload_bytes + 1024 * 1024
        try:
            length = int(dict(scope["headers"]).get(b"content-length", b"0"))
            if length < 0 or length > limit:
                return await error_response(413, "Request body is too large", "invalidParseFile")(
                    scope, receive, send
                )
        except ValueError:
            return await error_response(400, "Invalid Content-Length", "invalidParseFile")(
                scope, receive, send
            )
        synchronous = scope["path"] == "/v2/parse/file"
        try:
            self.uploads.acquire()
        except ServiceBusy:
            return await error_response(
                503, "Upload capacity is full", headers={"Retry-After": "5"}
            )(scope, receive, send)
        try:
            if synchronous:
                self.waiters.acquire()
        except ServiceBusy:
            self.uploads.release()
            return await error_response(
                503, "Synchronous wait capacity is full", headers={"Retry-After": "5"}
            )(scope, receive, send)
        store = scope["app"].state.service.store
        monitor = None
        state["upload_released"] = False
        state["upload_capacity"] = self.uploads
        state["upload_store"] = store
        try:
            try:
                state["upload_token"] = await run_in_threadpool(store.reserve_upload)
            except Exception:
                return await error_response(503, "Coordinator unavailable")(scope, receive, send)
            started = time.perf_counter()
            size = 0
            finished = False

            async def guarded_receive():
                nonlocal size, finished
                if finished:
                    return await receive()
                remaining = self.config.upload_seconds - (time.perf_counter() - started)
                if synchronous:
                    remaining = min(
                        remaining,
                        self.settings.request_timeout - (time.perf_counter() - state["started"]),
                    )
                if remaining <= 0:
                    raise HTTPException(408, "Upload deadline exceeded")
                try:
                    message = await asyncio.wait_for(receive(), remaining)
                except asyncio.TimeoutError as exc:
                    raise HTTPException(408, "Upload deadline exceeded") from exc
                if message["type"] == "http.request":
                    size += len(message.get("body", b""))
                    if size > limit:
                        raise HTTPException(413, "Request body is too large")
                    if not message.get("more_body", False):
                        finished = True
                        state["upload_seconds"] = time.perf_counter() - started
                return message

            async def renew():
                while not state["upload_released"]:
                    await asyncio.sleep(min(10, self.config.upload_seconds / 3))
                    if not state["upload_released"]:
                        try:
                            if not await run_in_threadpool(
                                store.renew_upload, state["upload_token"]
                            ):
                                return
                        except Exception:
                            return

            monitor = asyncio.create_task(renew())
            await self.app(scope, guarded_receive, send)
        finally:
            if monitor:
                monitor.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor
            try:
                if "upload_token" in state:
                    await release_upload(state)
                else:
                    self.uploads.release()
            finally:
                if synchronous:
                    self.waiters.release()


class TransferGuard:
    def __init__(self, app, queue_settings):
        self.app = app
        self.config = queue_settings
        self.capacity = Capacity(queue_settings.max_transfers)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        applies = scope["type"] == "http" and (
            (scope["method"] == "GET" and path.startswith("/jobs/") and path.endswith("/result"))
            or (path.startswith("/internal/jobs/") and path.endswith(("/input", "/complete")))
        )
        if not applies:
            return await self.app(scope, receive, send)
        try:
            self.capacity.acquire()
        except ServiceBusy:
            return await error_response(
                503, "Transfer capacity is full", headers={"Retry-After": "1"}
            )(scope, receive, send)
        started = False

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await asyncio.wait_for(
                self.app(scope, receive, tracked_send), self.config.transfer_seconds
            )
        except asyncio.TimeoutError:
            if started:
                raise ConnectionError("Transfer deadline exceeded") from None
            await error_response(504, "Transfer deadline exceeded")(scope, receive, send)
        finally:
            self.capacity.release()
