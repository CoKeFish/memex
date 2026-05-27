from fastapi import FastAPI

from memex.api.middleware import RequestContextMiddleware
from memex.api.routers import bridge, health, inbox, ingest, sources
from memex.logging import setup_logging

setup_logging()

app = FastAPI(
    title="memex",
    version="0.1.0",
    description="Personal life-data consolidation — store + endpoints (v0)",
)

app.add_middleware(RequestContextMiddleware)

app.include_router(health.router)
app.include_router(ingest.router)
app.include_router(inbox.router)
app.include_router(sources.router)
app.include_router(bridge.router)
