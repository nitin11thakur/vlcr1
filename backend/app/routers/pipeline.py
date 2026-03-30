"""
app/routers/pipeline.py
-----------------------
Pipeline health and complaint management endpoints.

Routes (prefix /api/v1/pipeline set in main.py):
  GET  /status      — service health check with latency metrics (JWT)
  GET  /complaints  — paginated complaints with filters (JWT)

Requirements: 15.1, 15.3, 15.4, 15.5
"""

import logging
import time
from datetime import date, datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.core.redis_client import get_redis
from app.models.models import Complaint
from app.schemas.schemas import (
    ComplaintListItem,
    ComplaintListResponse,
    PipelineStatus,
    ServiceHealth,
)

logger = logging.getLogger("vlcr.routers.pipeline")

router = APIRouter(tags=["pipeline"])

# ── Health check thresholds ───────────────────────────────────────────────────

_LATENCY_DEGRADED_MS = 1000.0  # > 1000 ms → degraded


def _ms(start: float) -> float:
    """Return elapsed milliseconds since *start* (from time.monotonic())."""
    return (time.monotonic() - start) * 1000.0


# ── Per-service health checks ─────────────────────────────────────────────────

async def _check_postgres(db: AsyncSession) -> ServiceHealth:
    start = time.monotonic()
    try:
        await db.execute(text("SELECT 1"))
        latency = _ms(start)
        status = "healthy" if latency < _LATENCY_DEGRADED_MS else "degraded"
        return ServiceHealth(
            service="postgresql",
            status=status,
            uptime_pct=100.0,
            note=f"latency={latency:.1f}ms",
        )
    except Exception as exc:
        logger.warning("PostgreSQL health check failed: %s", exc)
        return ServiceHealth(service="postgresql", status="down", uptime_pct=0.0, note=str(exc))


async def _check_redis() -> ServiceHealth:
    r = get_redis()
    if r is None:
        return ServiceHealth(service="redis", status="down", uptime_pct=0.0, note="client not initialised")
    start = time.monotonic()
    try:
        await r.ping()
        latency = _ms(start)
        status = "healthy" if latency < _LATENCY_DEGRADED_MS else "degraded"
        return ServiceHealth(
            service="redis",
            status=status,
            uptime_pct=100.0,
            note=f"latency={latency:.1f}ms",
        )
    except Exception as exc:
        logger.warning("Redis health check failed: %s", exc)
        return ServiceHealth(service="redis", status="down", uptime_pct=0.0, note=str(exc))


async def _check_claude() -> ServiceHealth:
    if not settings.ANTHROPIC_API_KEY:
        return ServiceHealth(
            service="claude",
            status="down",
            uptime_pct=0.0,
            note="ANTHROPIC_API_KEY not configured",
        )
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": settings.CLAUDE_MODEL,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )
        latency = _ms(start)
        # 200 or 400-level auth errors still mean the API is reachable
        if resp.status_code in (200, 400, 401, 403, 422, 529):
            status = "healthy" if latency < _LATENCY_DEGRADED_MS else "degraded"
            return ServiceHealth(
                service="claude",
                status=status,
                uptime_pct=100.0,
                note=f"latency={latency:.1f}ms http={resp.status_code}",
            )
        return ServiceHealth(
            service="claude",
            status="degraded",
            uptime_pct=50.0,
            note=f"unexpected http={resp.status_code}",
        )
    except Exception as exc:
        logger.warning("Claude health check failed: %s", exc)
        return ServiceHealth(service="claude", status="down", uptime_pct=0.0, note=str(exc))


async def _check_bhashini() -> ServiceHealth:
    if not settings.BHASHINI_API_KEY:
        return ServiceHealth(
            service="bhashini",
            status="down",
            uptime_pct=0.0,
            note="BHASHINI_API_KEY not configured",
        )
    bhashini_base = "https://meity-auth.ulcacontrib.org"
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.head(bhashini_base)
        latency = _ms(start)
        if resp.status_code < 500:
            status = "healthy" if latency < _LATENCY_DEGRADED_MS else "degraded"
            return ServiceHealth(
                service="bhashini",
                status=status,
                uptime_pct=100.0,
                note=f"latency={latency:.1f}ms http={resp.status_code}",
            )
        return ServiceHealth(
            service="bhashini",
            status="degraded",
            uptime_pct=50.0,
            note=f"http={resp.status_code}",
        )
    except Exception as exc:
        logger.warning("Bhashini health check failed: %s", exc)
        return ServiceHealth(service="bhashini", status="down", uptime_pct=0.0, note=str(exc))


async def _check_sms() -> ServiceHealth:
    provider = settings.SMS_PROVIDER
    if provider == "mock" or not provider:
        return ServiceHealth(
            service="sms",
            status="healthy",
            uptime_pct=100.0,
            note="mock provider — no external call",
        )
    # For real providers, verify the key is present
    if provider == "gupshup":
        if not settings.GUPSHUP_API_KEY:
            return ServiceHealth(
                service="sms",
                status="down",
                uptime_pct=0.0,
                note="GUPSHUP_API_KEY not configured",
            )
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.head("https://api.gupshup.io")
            latency = _ms(start)
            status = "healthy" if latency < _LATENCY_DEGRADED_MS else "degraded"
            return ServiceHealth(
                service="sms",
                status=status,
                uptime_pct=100.0,
                note=f"gupshup latency={latency:.1f}ms",
            )
        except Exception as exc:
            logger.warning("Gupshup health check failed: %s", exc)
            return ServiceHealth(service="sms", status="down", uptime_pct=0.0, note=str(exc))

    if provider == "twilio":
        if not settings.TWILIO_ACCOUNT_SID:
            return ServiceHealth(
                service="sms",
                status="down",
                uptime_pct=0.0,
                note="TWILIO_ACCOUNT_SID not configured",
            )
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.head("https://api.twilio.com")
            latency = _ms(start)
            status = "healthy" if latency < _LATENCY_DEGRADED_MS else "degraded"
            return ServiceHealth(
                service="sms",
                status=status,
                uptime_pct=100.0,
                note=f"twilio latency={latency:.1f}ms",
            )
        except Exception as exc:
            logger.warning("Twilio health check failed: %s", exc)
            return ServiceHealth(service="sms", status="down", uptime_pct=0.0, note=str(exc))

    # Unknown provider — report as degraded
    return ServiceHealth(
        service="sms",
        status="degraded",
        uptime_pct=50.0,
        note=f"unknown provider: {provider}",
    )


# ── GET /status ───────────────────────────────────────────────────────────────

@router.get("/status", response_model=PipelineStatus)
async def pipeline_status(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Return health status and latency metrics for all pipeline services.

    Checks PostgreSQL, Redis, Claude, Bhashini, and SMS in parallel.
    Each service is rated healthy / degraded / down based on latency and
    reachability (< 1000 ms → healthy, ≥ 1000 ms → degraded, exception → down).

    Requirements: 15.1, 15.4
    """
    import asyncio

    postgres_health, redis_health, claude_health, bhashini_health, sms_health = (
        await asyncio.gather(
            _check_postgres(db),
            _check_redis(),
            _check_claude(),
            _check_bhashini(),
            _check_sms(),
        )
    )

    services = [postgres_health, redis_health, claude_health, bhashini_health, sms_health]

    # Count complaints processed today
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    count_result = await db.execute(
        select(func.count()).where(Complaint.created_at >= today_start)
    )
    total_processed_today: int = count_result.scalar_one() or 0

    # Queue depths per status
    queue_result = await db.execute(
        select(Complaint.status, func.count().label("cnt"))
        .group_by(Complaint.status)
    )
    queue_depths = {row.status: row.cnt for row in queue_result}

    return PipelineStatus(
        services=services,
        queue_depths=queue_depths,
        # P95 latency metrics are not yet instrumented — return 0.0 as placeholder
        asr_latency_p95_ms=0.0,
        classifier_latency_p95_ms=0.0,
        total_processed_today=total_processed_today,
    )


# ── GET /complaints ───────────────────────────────────────────────────────────

@router.get("/complaints", response_model=ComplaintListResponse)
async def list_pipeline_complaints(
    status: Optional[str] = None,
    severity: Optional[str] = None,
    category: Optional[str] = None,
    state_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Paginated list of complaints with optional filters.

    Query params:
      status      — filter by complaint status
      severity    — filter by severity (critical|high|medium|low)
      category    — filter by category string
      state_code  — filter by location_state
      date_from   — ISO date (inclusive lower bound on created_at)
      date_to     — ISO date (inclusive upper bound on created_at)
      page        — page number (default 1)
      page_size   — items per page (default 20)

    Requirements: 15.3, 15.5
    """
    query = select(Complaint)

    if status:
        query = query.where(Complaint.status == status)
    if severity:
        query = query.where(Complaint.severity == severity)
    if category:
        query = query.where(Complaint.category == category)
    if state_code:
        query = query.where(Complaint.location_state == state_code)
    if date_from:
        query = query.where(
            Complaint.created_at >= datetime(date_from.year, date_from.month, date_from.day, tzinfo=timezone.utc)
        )
    if date_to:
        # Include the full day_to day
        query = query.where(
            Complaint.created_at < datetime(date_to.year, date_to.month, date_to.day + 1, tzinfo=timezone.utc)
        )

    # Total count
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar_one()

    # Paginated results
    offset = (page - 1) * page_size
    query = query.order_by(Complaint.created_at.desc()).offset(offset).limit(page_size)
    result = await db.execute(query)
    complaints = result.scalars().all()

    return ComplaintListResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[ComplaintListItem.model_validate(c) for c in complaints],
    )
