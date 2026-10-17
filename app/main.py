import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.health import router as health_router
from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = create_async_engine(
            config.sqlalchemy_url(),
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            pool_timeout=10,
            connect_args={"timeout": 3, "command_timeout": 3},
        )
        app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(
        title="台股研究室 API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/v1/docs" if config.app_env != "production" else None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json" if config.app_env != "production" else None,
    )

    @app.middleware("http")
    async def request_metadata(request: Request, call_next):
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        fields = [
            {"location": list(error["loc"]), "message": error["msg"]}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={"error": {
                "code": "VALIDATION_ERROR", "message": "Invalid request.",
                "field_errors": fields, "request_id": request.state.request_id,
            }},
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        # Do not print exception text: database errors may include connection details.
        logger.error("Unhandled %s request_id=%s", type(exc).__name__, request.state.request_id)
        return JSONResponse(
            status_code=500,
            headers={"X-Request-ID": request.state.request_id, "Cache-Control": "no-store"},
            content={"error": {
                "code": "INTERNAL_ERROR", "message": "An unexpected error occurred.",
                "field_errors": [], "request_id": request.state.request_id,
            }},
        )

    app.include_router(health_router, prefix="/api/v1")
    return app


# Public ASGI entry point: run from backend/ with uvicorn app.main:app --reload.
app = create_app()
