from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyCookie

from app.services.authentication import AuthenticationService, CurrentSession
from app.services.errors import ServiceError


class SessionCookie(APIKeyCookie):
    def __init__(self):
        super().__init__(name="session", scheme_name="SessionCookie", auto_error=False)

    async def __call__(self, request: Request) -> str | None:
        return request.cookies.get(request.app.state.settings.session_cookie_name)


session_cookie = SessionCookie()
SessionCookieDependency = Annotated[str | None, Security(session_cookie)]


def get_authentication_service(request: Request) -> AuthenticationService:
    return request.app.state.authentication_service


AuthenticationServiceDependency = Annotated[
    AuthenticationService, Depends(get_authentication_service)
]


async def get_current_session(
    value: SessionCookieDependency, service: AuthenticationServiceDependency
) -> CurrentSession:
    if not value:
        raise ServiceError("SESSION_INVALID", "登入憑證無效或已過期。", 401)
    return await service.authenticate(value)


CurrentSessionDependency = Annotated[CurrentSession, Depends(get_current_session)]
