"""Authenticate before reading uploads; public and worker credentials are independent."""

import secrets

from app.interfaces.http.errors import error_response


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
