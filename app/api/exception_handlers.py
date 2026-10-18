import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from app.schemas.common import ErrorDetail, ErrorResponse, FieldError
from app.services.errors import ServiceError

logger = logging.getLogger(__name__)


def error_response(
    request: Request, status: int, code: str, message: str, fields: list[FieldError] | None = None
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message,
            field_errors=fields or [],
            request_id=request.state.request_id,
        )
    )
    return JSONResponse(
        status_code=status,
        content=body.model_dump(mode="json"),
        headers={"X-Request-ID": request.state.request_id, "Cache-Control": "no-store"},
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        fields = [
            FieldError(location=list(error["loc"]), message=error["msg"]) for error in exc.errors()
        ]
        return error_response(request, 422, "VALIDATION_ERROR", "Invalid request.", fields)

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        return error_response(request, exc.status_code, exc.code, exc.message)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        code = "RESOURCE_NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR"
        response = error_response(request, exc.status_code, code, str(exc.detail))
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        logger.error("Database unavailable request_id=%s", request.state.request_id)
        return error_response(request, 503, "DEPENDENCY_UNAVAILABLE", "Database is not ready.")

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        # Never log exception text: upstream and DB errors may contain credentials.
        logger.error("Unhandled %s request_id=%s", type(exc).__name__, request.state.request_id)
        return error_response(request, 500, "INTERNAL_ERROR", "An unexpected error occurred.")
