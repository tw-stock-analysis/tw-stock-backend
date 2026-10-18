from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.exception_handlers import register_exception_handlers
from app.api.v1.router import router as v1_router
from app.core.config import Settings, get_settings
from app.services.fugle import FugleService


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = create_async_engine(
            config.sqlalchemy_url(),
            isolation_level="REPEATABLE READ",
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            pool_timeout=10,
            connect_args={"timeout": 3, "command_timeout": 3},
        )
        app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with httpx.AsyncClient(follow_redirects=False) as source_client:
                app.state.fugle_service = FugleService(source_client, config)
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

    register_exception_handlers(app)
    app.include_router(v1_router)
    return app


# Public ASGI entry point: run from backend/ with uvicorn app.main:app --reload.
app = create_app()
