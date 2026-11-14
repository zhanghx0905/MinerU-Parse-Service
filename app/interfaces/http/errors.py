"""One public error envelope for FastGPT, routes and admission middleware."""

import logging
import time

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from app.domain.parsing import DocumentLimitExceeded, InvalidDocument

logger = logging.getLogger(__name__)


class ParseError(HTTPException):
    """A safe diagnostic code and message; never carries SDK exception details."""

    def __init__(self, status_code, message, status_text="pdfParseFailed", headers=None):
        super().__init__(status_code, message, headers)
        self.status_text = status_text


def error_response(status, message, status_text="pdfParseFailed", *, headers=None, **extra):
    return JSONResponse(
        {
            "success": False,
            "message": message,
            "error": message,
            "statusText": status_text,
            "detail": message,
            "markdown": "",
            "pages": 0,
            "duration": 0,
            **extra,
        },
        status_code=status,
        headers=headers,
    )


def install_error_handlers(app):
    """Normalize route failures while retaining HTTP status and retry headers."""

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc):
        default = "invalidParseFile" if exc.status_code in {400, 413, 422} else "pdfParseFailed"
        started = getattr(request.state, "started", None)
        return error_response(
            exc.status_code,
            str(exc.detail),
            getattr(exc, "status_text", default),
            headers=exc.headers,
            duration=time.perf_counter() - started if started is not None else 0,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc):
        return error_response(422, "Invalid request parameters", "invalidParseFile")

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, exc):
        logger.error("Request failed: %s", type(exc).__name__)
        return error_response(500, "PDF parsing failed; see server logs")

    @app.exception_handler(InvalidDocument)
    async def invalid_document(_request: Request, exc):
        status = 413 if isinstance(exc, DocumentLimitExceeded) else 400
        return error_response(status, str(exc), exc.status_text)
