"""Browser JSON mutation boundary; runs before FastAPI parses sensitive bodies."""

from fastapi import Request
from starlette._utils import get_route_path
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.exception_handlers import error_response
from app.core.config import Settings
from app.services.errors import ServiceError

AUTH_MUTATIONS = frozenset(
    (
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
        "/api/v1/me/sessions/revoke-others",
    )
)
MAX_AUTH_BODY_BYTES = 16_384


class AuthSecurityMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = get_route_path(scope) if scope["type"] == "http" else ""
        if scope["type"] != "http" or scope["method"] != "POST" or path not in AUTH_MUTATIONS:
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        origins = request.headers.getlist("origin")
        csrf_headers = request.headers.getlist("x-csrf-protection")
        if (
            len(origins) != 1
            or origins[0] not in self.settings.auth_allowed_origins
            or csrf_headers != ["1"]
        ):
            await error_response(request, 403, "CSRF_REJECTED", "請求來源或 CSRF header 不正確。")(
                scope, receive, send
            )
            return
        content_types = request.headers.getlist("content-type")
        if (
            len(content_types) != 1
            or content_types[0].split(";", 1)[0].strip().lower() != "application/json"
        ):
            await error_response(request, 415, "JSON_REQUIRED", "請使用 application/json。")(
                scope, receive, send
            )
            return

        if path == "/api/v1/auth/login":
            # Trust only the ASGI peer, already normalized by a configured trusted proxy.
            peer = request.client.host if request.client else "unknown"
            try:
                request.app.state.login_limiter.check_ip(peer)
            except ServiceError as error:
                response = error_response(request, error.status_code, error.code, error.message)
                if error.status_code == 429:
                    response.headers["Retry-After"] = "60"
                await response(scope, receive, send)
                return

        # Bound actual bytes, including chunked bodies and deceptive Content-Length.
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > MAX_AUTH_BODY_BYTES:
                await error_response(request, 413, "BODY_TOO_LARGE", "請求內容超過大小限制。")(
                    scope, receive, send
                )
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        if path != "/api/v1/auth/login" and body:
            await error_response(request, 422, "BODY_NOT_ALLOWED", "此操作不接受 request body。")(
                scope, receive, send
            )
            return
        body_sent = False

        async def replay() -> Message:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
