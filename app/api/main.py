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

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes_admin import router as admin_router
from app.api.routes_auth import router as auth_router
from app.api.routes_chat import router as chat_router
from app.api.routes_health import router as health_router
from app.api.routes_proposals import router as proposals_router
from app.api.routes_query import router as query_router
from app.auth.dependencies import require_principal, validate_oidc_settings
from app.catalog.runtime import get_structured_catalog
from app.config import get_settings
from app.llm_provider import LLMProviderConfigError
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

    # Authentication is fail-closed: when it is enabled, an incomplete or
    # invalid configuration must stop the service from serving protected
    # endpoints at all, rather than letting them fall back to anonymous
    # access.
    if settings.authentik_enabled:
        problems = validate_oidc_settings(settings)
        if problems:
            message = " ".join(problems)
            logger.error("OIDC authentication is enabled but misconfigured: %s", message)
            raise RuntimeError(f"OIDC authentication is enabled but misconfigured: {message}")
        logger.info(
            "Authentik OIDC authentication is enabled; protected endpoints require a valid "
            "application session."
        )
    else:
        logger.warning(
            "AUTHENTICATION IS DISABLED -- every API endpoint is reachable without an "
            "authenticated identity. This is a DEVELOPMENT-ONLY mode and is not a security "
            "control. Set AUTHENTIK_ENABLED=true before exposing this service."
        )

    # Warm the structured metric catalog cache. Never fatal: it returns None
    # (and logs a warning) if catalog.json is missing/invalid, and the
    # pipeline then runs exactly as it did before the catalog existed.
    get_structured_catalog(settings)
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


@app.exception_handler(LLMProviderConfigError)
async def llm_provider_config_error_handler(request: Request, exc: LLMProviderConfigError):
    """A misconfigured LLM provider is an operational fault, not a bug in the
    request. Report it precisely (the message names the setting to fix and
    contains no secret material) instead of a generic 500."""
    logger.error("LLM provider misconfiguration on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={"detail": f"LLM provider is not configured correctly: {exc}"},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Every unhandled error returns the same JSON shape -- the frontend
    never has to special-case an HTML traceback page."""
    logger.exception("Unhandled exception on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


# Health and readiness probes stay public so that infrastructure can watch
# the service without a credential. The authentication endpoints are public
# by design: the browser needs them to start a login.
app.include_router(health_router)
app.include_router(auth_router)

# Everything else is user-facing and requires an authenticated identity.
app.include_router(chat_router, dependencies=[Depends(require_principal)])
app.include_router(query_router, dependencies=[Depends(require_principal)])
app.include_router(proposals_router, dependencies=[Depends(require_principal)])
app.include_router(admin_router, dependencies=[Depends(require_principal)])
