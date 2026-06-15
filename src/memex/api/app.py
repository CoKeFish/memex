import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from memex.api.auth import DEFAULT_DEV_USER_ID
from memex.api.middleware import RequestContextMiddleware
from memex.api.routers import (
    accounts,
    auth,
    backfill,
    bienestar,
    calendar,
    feedback,
    filters,
    finance,
    gateway,
    geo,
    graph,
    hackathones,
    health,
    identidades,
    inbox,
    ingest,
    ingest_scheduler,
    llm,
    logs,
    media,
    metrics,
    modules,
    oauth,
    processing,
    quality,
    relevance,
    review,
    sources,
    stats,
    telegram,
)
from memex.api.streaming import build_streaming_runner
from memex.config import settings
from memex.db import connection
from memex.logging import get_logger, setup_logging
from memex.security import vault

setup_logging()

_log = get_logger("memex.api.app")


def _ensure_dev_vault() -> None:
    """Dev (auth off): el user seed de la migración 0001 NO tiene vault — el DEK se crea al
    registrarse (`vault.provision_user`), y dev-sin-login saltea el registro. Sin DEK, guardar un
    secreto (token de OAuth, credenciales de ingesta) tira `UserVaultMissingError`. Esto provisiona
    el vault del user dev al boot, idempotente. No-op si auth está enforced, si falta
    `MEMEX_SECRET_KEY`, o si ya está provisionado. La contraseña es aleatoria y nunca se usa (en dev
    no hay login); lo que importa es el DEK envuelto con la master key."""
    if settings.auth_enforced or not settings.secret_key.strip():
        return
    with connection() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM user_credentials WHERE user_id = :uid"),
            {"uid": DEFAULT_DEV_USER_ID},
        ).first()
        if exists is not None:
            return
        vault.provision_user(conn, DEFAULT_DEV_USER_ID, secrets.token_urlsafe(32))
    _log.info("app.dev_vault.provisioned", user_id=DEFAULT_DEV_USER_ID)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Arranca el StreamingRunner al startup, lo frena al shutdown.

    El runner escucha los chats Telegram marcados `streaming=True`. Si no hay
    ninguno configurado, `start()` es no-op (cero costo). El bootstrap es
    resiliente: una DB caída al boot no impide que el API sirva HTTP.
    """
    # Dev: garantiza el vault del user único para que el flujo de cuentas/OAuth funcione sin login.
    # Resiliente: si falla (DB caída al boot), el API igual sirve HTTP.
    try:
        _ensure_dev_vault()
    except Exception:
        _log.warning("app.dev_vault.provision_failed", exc_info=True)
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
app.include_router(auth.router)
app.include_router(accounts.router)
app.include_router(oauth.router)
app.include_router(telegram.router)
app.include_router(ingest.router)
app.include_router(ingest_scheduler.router)
app.include_router(inbox.router)
app.include_router(sources.router)
app.include_router(backfill.router)
app.include_router(gateway.router)
app.include_router(geo.router)
app.include_router(media.router)
app.include_router(filters.router)
app.include_router(feedback.router)
app.include_router(finance.router)
app.include_router(bienestar.router)
app.include_router(calendar.router)
app.include_router(hackathones.router)
app.include_router(identidades.router)
app.include_router(graph.router)
app.include_router(metrics.router)
app.include_router(llm.router)
app.include_router(logs.router)
app.include_router(stats.router)
app.include_router(modules.router)
app.include_router(processing.router)
app.include_router(quality.router)
app.include_router(relevance.router)
app.include_router(review.router)
