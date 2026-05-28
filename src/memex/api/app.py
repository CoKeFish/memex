from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from memex.api.middleware import RequestContextMiddleware
from memex.api.routers import gateway, health, inbox, ingest, sources
from memex.api.streaming import build_streaming_runner
from memex.logging import get_logger, setup_logging

setup_logging()

_log = get_logger("memex.api.app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Arranca el StreamingRunner al startup, lo frena al shutdown.

    El runner escucha los chats Telegram marcados `streaming=True`. Si no hay
    ninguno configurado, `start()` es no-op (cero costo). El bootstrap es
    resiliente: una DB caída al boot no impide que el API sirva HTTP.
    """
    runner = build_streaming_runner()
    await runner.start()
    _log.info("app.lifespan.started")
    try:
        yield
    finally:
        await runner.stop()
        _log.info("app.lifespan.stopped")


app = FastAPI(
    title="memex",
    version="0.1.0",
    description="Personal life-data consolidation — store + endpoints (v0)",
    lifespan=lifespan,
)

app.add_middleware(RequestContextMiddleware)

app.include_router(health.router)
app.include_router(ingest.router)
app.include_router(inbox.router)
app.include_router(sources.router)
app.include_router(gateway.router)
