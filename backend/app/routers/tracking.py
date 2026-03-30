"""
app/routers/tracking.py
-----------------------
Public tracking endpoints — no authentication required.

Routes (prefix /api/v1/track set in main.py):
  GET /phone/{phone}          — list complaints by phone, rate-limited 5/min per IP
  GET /{reference_number}     — single complaint tracking, cached 60s in Redis

IMPORTANT: /phone/{phone} is defined BEFORE /{reference_number} to prevent
FastAPI from matching the literal "phone" as a reference number.

Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 18.2
"""

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import ComplaintNotFound, RateLimitExceeded
from app.core.redis_client import cache_get, cache_set, rate_limit_check
from app.models.models import Complaint, StatusEvent
from app.schemas.schemas import TrackingResponse

logger = logging.getLogger("vlcr.routers.tracking")

router = APIRouter(tags=["tracking"])

# Human-readable labels for each status value
_STATUS_LABELS: Dict[str, str] = {
    "received": "Complaint Received",
    "processing": "Being Processed",
    "classified": "Classified",
    "routed": "Routed to Department",
    "dispatched": "Dispatched",
    "acknowledged": "Acknowledged by Department",
    "in_progress": "In Progress",
    "resolved": "Resolved",
    "review_required": "Under Review",
}


def _get_client_ip(request: Request) -> str:
    """Return the real client IP, honouring X-Forwarded-For."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _build_tracking_response(
    complaint: Complaint,
    db: AsyncSession,
) -> TrackingResponse:
    """Build a TrackingResponse from a Complaint ORM object."""
    # Fetch timeline events ordered by created_at
    events_result = await db.execute(
        select(StatusEvent)
        .where(StatusEvent.complaint_id == complaint.id)
        .order_by(StatusEvent.created_at.asc())
    )
    events = events_result.scalars().all()

    timeline: List[Dict[str, Any]] = [
        {
            "from_status": ev.from_status,
            "to_status": ev.to_status,
            "note": ev.note,
            "actor": ev.actor,
            "created_at": ev.created_at.isoformat() if ev.created_at else None,
        }
        for ev in events
    ]

    return TrackingResponse(
        reference_number=complaint.reference_number,
        status=complaint.status,
        status_label=_STATUS_LABELS.get(complaint.status, complaint.status),
        created_at=complaint.created_at,
        dept_name=complaint.dept_name,
        category=complaint.category,
        severity=complaint.severity,
        timeline=timeline,
    )


# ── GET /phone/{phone} ────────────────────────────────────────────────────────
# MUST be defined before /{reference_number} to avoid "phone" being matched
# as a reference number by FastAPI's path parameter routing.

@router.get("/phone/{phone}", response_model=List[TrackingResponse])
async def track_by_phone(
    phone: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Return a list of complaints associated with the given phone number.

    Rate-limited to 5 requests per minute per IP.
    citizen_phone is NOT included in the response (Requirement 18.2).

    Requirements: 11.2, 11.3, 11.5, 18.2
    """
    client_ip = _get_client_ip(request)

    # Rate limit: 5/min per IP (Requirement 11.5)
    if not await rate_limit_check(f"rl:ip:{client_ip}:track_phone", 5, 60):
        raise RateLimitExceeded("Rate limit exceeded for phone tracking (5 requests/min).")

    result = await db.execute(
        select(Complaint)
        .where(Complaint.citizen_phone == phone)
        .order_by(Complaint.created_at.desc())
    )
    complaints = result.scalars().all()

    responses = []
    for complaint in complaints:
        responses.append(await _build_tracking_response(complaint, db))

    return responses


# ── GET /{reference_number} ───────────────────────────────────────────────────

@router.get("/{reference_number}", response_model=TrackingResponse)
async def track_by_reference(
    reference_number: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Return tracking information for a complaint by reference number.

    Response is cached in Redis for 60 seconds (key: track:{reference_number}).
    citizen_phone is NOT included in the response (Requirement 18.2).

    Requirements: 11.1, 11.3, 11.4, 11.5, 18.2
    """
    cache_key = f"track:{reference_number}"

    # Check Redis cache first (Requirement 11.5)
    cached = await cache_get(cache_key)
    if cached is not None:
        logger.debug("Cache hit for %s", cache_key)
        return TrackingResponse(**cached)

    # Fetch from DB
    result = await db.execute(
        select(Complaint).where(Complaint.reference_number == reference_number)
    )
    complaint = result.scalar_one_or_none()

    if complaint is None:
        raise ComplaintNotFound(f"Complaint {reference_number!r} not found.")

    tracking = await _build_tracking_response(complaint, db)

    # Cache the response for 60 seconds (Requirement 11.5)
    await cache_set(cache_key, tracking.model_dump(mode="json"), ttl=60)

    return tracking
