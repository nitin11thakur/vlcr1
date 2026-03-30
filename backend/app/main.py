"""
app/main.py
-----------
FastAPI application entry point.

Lifespan:
  1. Runs Alembic `upgrade head` before accepting requests (Requirement 2.3)
     — skipped when VERCEL=1 (serverless functions are stateless; run migrations
       via `railway run alembic upgrade head` or Vercel build command instead)
  2. Checks for missing API keys and logs WARNING per missing key (Requirement 1.6)
  3. Initialises Redis connection

Middleware:
  - CORS (origins from settings.CORS_ORIGINS)
  - TrustedHostMiddleware
  - HSTS header when DEBUG=false (Requirement 18.3)

Requirements: 1.4, 1.6, 15.2, 18.3
"""

import logging
import os
from contextlib import asynccontextmanager

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings
from app.core.exceptions import VLCRException
from app.routers import auth, complaints, dashboard, ivr, pipeline, review, routing, tracking

logger = logging.getLogger("vlcr")

# ── Alembic path ─────────────────────────────────────────────────────────────
# Resolves relative to this file's location so it works regardless of CWD.
# app/main.py → backend/app/main.py  →  ../alembic.ini  →  backend/alembic.ini
_ALEMBIC_INI = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "alembic.ini")
)


# ── Migrations ────────────────────────────────────────────────────────────────

def _run_migrations() -> None:
    """Run pending Alembic migrations synchronously at startup."""
    try:
        cfg = AlembicConfig(_ALEMBIC_INI)
        alembic_command.upgrade(cfg, "head")
        logger.info("Alembic migrations applied successfully.")
    except Exception as exc:
        logger.error("Alembic migration failed: %s", exc)
        raise


# ── Redis init ────────────────────────────────────────────────────────────────

async def _init_redis() -> None:
    """Initialise the Redis connection pool."""
    try:
        from app.core.redis_client import init_redis
        await init_redis()
        logger.info("Redis connection initialised.")
    except Exception as exc:
        logger.warning("Redis initialisation failed (%s) — caching/rate-limiting degraded.", exc)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.getenv("VERCEL"):
        # Only auto-migrate in Docker / local / Railway environments.
        # On Vercel: run `alembic upgrade head` in the build/deploy step instead.
        logger.info("VLCR startup: running Alembic migrations…")
        _run_migrations()
    else:
        logger.info("VLCR startup: VERCEL=1 detected — skipping auto-migration.")

    logger.info("VLCR startup: checking API keys…")
    settings.warn_missing_keys()

    logger.info("VLCR startup: initialising Redis…")
    await _init_redis()

    logger.info("VLCR ready.")
    yield
    logger.info("VLCR shutdown.")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="VLCR — Vernacular Language Complaint Router",
    description="AI-powered multilingual complaint routing for Indian government services.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)


# ── CORS ──────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Trusted hosts ─────────────────────────────────────────────────────────────

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["*"],  # tighten in production via env
)


# ── HSTS header (production only) ─────────────────────────────────────────────

class HSTSMiddleware(BaseHTTPMiddleware):
    """Add Strict-Transport-Security header when DEBUG=false (Requirement 18.3)."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if not settings.DEBUG:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response


app.add_middleware(HSTSMiddleware)


# ── Exception handler ─────────────────────────────────────────────────────────

@app.exception_handler(VLCRException)
async def vlcr_exception_handler(request: Request, exc: VLCRException) -> JSONResponse:
    """Return structured JSON error for all VLCRException subclasses."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": exc.detail},
    )


# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(auth.router,       prefix="/api/v1/auth")
app.include_router(complaints.router, prefix="/api/v1/complaints")
app.include_router(tracking.router,   prefix="/api/v1/track")
app.include_router(dashboard.router,  prefix="/api/v1/dashboard")
app.include_router(review.router,     prefix="/api/v1/review")
app.include_router(routing.router,    prefix="/api/v1/routing")
app.include_router(pipeline.router,   prefix="/api/v1/pipeline")
app.include_router(ivr.router,        prefix="/api/v1/ivr")


# ── Liveness probe ────────────────────────────────────────────────────────────

@app.get("/api/health", tags=["health"])
async def health_check():
    """Lightweight liveness probe — no external dependency checks (Requirement 15.2)."""
    return {"status": "ok"}
