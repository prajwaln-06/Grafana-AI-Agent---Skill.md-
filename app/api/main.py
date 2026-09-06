"""
api/main.py

Standalone FastAPI service exposing the observability-query-builder
pipeline over HTTP. Run with:

    uvicorn app.api.main:app --host 0.0.0.0 --port 8000

or via the provided Dockerfile.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes_admin import router as admin_router
from app.api.routes_chat import router as chat_router
from app.api.routes_health import router as health_router
from app.api.routes_proposals import router as proposals_router
from app.api.routes_query import router as query_router
from app.config import get_settings
from app.logging_config import configure_logging
from app.skill_index import SkillIndex, SkillIndexError

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    try:
        app.state.skill_index = SkillIndex.load(settings.skills_root)
        logger.info("Loaded skill %r version %s (%d routing rows).",
                    app.state.skill_index.metadata.name,
                    app.state.skill_index.metadata.version,
                    len(app.state.skill_index.routing_rows))
    except SkillIndexError as e:
        logger.error("Failed to load skill package: %s", e)
        raise
    yield


app = FastAPI(
    title="Observability Query Builder API",
    description="Natural-language to PromQL/OpenSearch query construction and execution.",
    version="1.0.0",
    lifespan=lifespan,
)

_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def api_key_guard(request: Request, call_next):
    """No-op when settings.api_key is unset (local dev / current setup).
    Once set, every request must carry a matching X-API-Key header --
    health/readiness endpoints stay open regardless, so infra can probe
    liveness without a key."""
    settings = get_settings()
    if settings.api_key and request.url.path not in ("/healthz", "/readyz", "/docs", "/openapi.json"):
        if request.headers.get("X-API-Key") != settings.api_key:
            return JSONResponse(status_code=401, content={"detail": "Missing or invalid X-API-Key header."})
    return await call_next(request)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Every unhandled error returns the same JSON shape -- the frontend
    never has to special-case an HTML traceback page."""
    logger.exception("Unhandled exception on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


app.include_router(health_router)
app.include_router(chat_router)
app.include_router(query_router)
app.include_router(proposals_router)
app.include_router(admin_router)

