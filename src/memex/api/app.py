from fastapi import FastAPI

from memex.api.routers import health, inbox, ingest, sources
from memex.logging import setup_logging

setup_logging()

app = FastAPI(
    title="memex",
    version="0.1.0",
    description="Personal life-data consolidation — store + endpoints (v0)",
)

app.include_router(health.router)
app.include_router(ingest.router)
app.include_router(inbox.router)
app.include_router(sources.router)
