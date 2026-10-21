from ipaddress import ip_address

from fastapi import APIRouter, Request, Response

from app.api.auth_deps import (
    AuthenticationServiceDependency,
    CurrentSessionDependency,
    SessionCookieDependency,
)
from app.schemas.authentication import (
    LoginRequest,
    LoginResponse,
    SessionResponse,
    SessionsResponse,
    UserResponse,
)
from app.schemas.common import ErrorResponse

router = APIRouter(
    tags=["authentication"],
    responses={
        status: {"model": ErrorResponse} for status in (401, 403, 413, 415, 422, 429, 500, 503)
    },
)

BROWSER_WRITE_SCHEMA = {
    "parameters": [
        {
            "name": "X-CSRF-Protection",
            "in": "header",
            "required": True,
            "schema": {"type": "string", "enum": ["1"]},
            "description": "Browser must also send an allowed Origin and application/json.",
        }
    ]
}


@router.post(
    "/auth/login",
    response_model=LoginResponse,
    summary="Email／密碼登入，有效期固定 7 天",
    openapi_extra=BROWSER_WRITE_SCHEMA,
)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    service: AuthenticationServiceDependency,
) -> LoginResponse:
    request.app.state.login_limiter.check_email(body.email)
    try:
        peer = ip_address(request.client.host) if request.client else None
    except ValueError:
        peer = None
    result = await service.login(
        body.email,
        body.password.get_secret_value(),
        user_agent=request.headers.get("user-agent", "")[:512] or None,
        created_ip=peer,
    )
    settings = request.app.state.settings
    response.set_cookie(
        key=settings.session_cookie_name,
        value=result.credential.value,
        max_age=7 * 24 * 60 * 60,
        expires=result.credential.expires_at,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return LoginResponse(
        user=UserResponse.model_validate(result.user), expires_at=result.credential.expires_at
    )


@router.get("/me", response_model=UserResponse, summary="取得目前登入的使用者")
async def me(current: CurrentSessionDependency) -> UserResponse:
    return UserResponse.model_validate(current.user)


@router.post(
    "/auth/logout",
    status_code=204,
    summary="登出目前裝置並清除 Cookie",
    description="Send Content-Type: application/json with an empty request body.",
    openapi_extra=BROWSER_WRITE_SCHEMA,
)
async def logout(
    value: SessionCookieDependency, service: AuthenticationServiceDependency, request: Request
) -> Response:
    await service.logout(value)
    settings = request.app.state.settings
    response = Response(status_code=204)
    response.delete_cookie(
        settings.session_cookie_name,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/me/sessions", response_model=SessionsResponse, summary="列出自己的有效登入裝置")
async def sessions(
    current: CurrentSessionDependency, service: AuthenticationServiceDependency
) -> SessionsResponse:
    records = await service.list_sessions(current)
    return SessionsResponse(sessions=[SessionResponse.model_validate(record) for record in records])


@router.post(
    "/me/sessions/revoke-others",
    status_code=204,
    summary="登出自己的其他裝置",
    description="Send Content-Type: application/json with an empty request body.",
    openapi_extra=BROWSER_WRITE_SCHEMA,
)
async def revoke_other_sessions(
    current: CurrentSessionDependency, service: AuthenticationServiceDependency
) -> Response:
    await service.revoke_others(current)
    return Response(status_code=204)
