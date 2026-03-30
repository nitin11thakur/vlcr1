"""
app/routers/review.py
---------------------
Review queue endpoints for human reviewers.

Routes (prefix /api/v1/review set in main.py):
  GET  /queue                  — paginated review_required complaints (JWT reviewer+)
  POST /{ref}/reclassify       — update classification, re-route, dispatch (JWT reviewer+)
  POST /{ref}/mark-duplicate   — mark complaint as duplicate (JWT reviewer+)

Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_role
from app.core.database import get_db
from app.core.exceptions import ComplaintNotFound
from app.models.models import AuditLog, Complaint, Department, StatusEvent
from app.schemas.schemas import (
    ComplaintDetail,
    ReclassifyRequest,
    ReviewQueueItem,
    ReviewQueueResponse,
)
from pydantic import BaseModel

logger = logging.getLogger("vlcr.routers.review")

router = APIRouter(tags=["review"])


# ── Request schema ────────────────────────────────────────────────────────────

class MarkDuplicateRequest(BaseModel):
    original_reference_number: str



# ── GET /queue ────────────────────────────────────────────────────────────────

@router.get("/queue", response_model=ReviewQueueResponse)
async def get_review_queue(
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_role("reviewer", "super_admin")),
):
    """
    Return paginated complaints with status == 'review_required'.

    Requirements: 12.1
    """
    base_query = select(Complaint).where(Complaint.status == "review_required")

    count_result = await db.execute(
        select(func.count()).select_from(base_query.subquery())
    )
    total = count_result.scalar_one()

    offset = (page - 1) * page_size
    result = await db.execute(
        base_query.order_by(Complaint.created_at.asc()).offset(offset).limit(page_size)
    )
    complaints = result.scalars().all()

    items = [
        ReviewQueueItem(
            reference_number=c.reference_number,
            created_at=c.created_at,
            review_reason=c.review_reason,
            citizen_lang=c.citizen_lang,
            raw_text_original=c.raw_text_original,
            transcript_norm=c.transcript_norm,
            translation_en=c.translation_en,
            ai_category=c.category,
            ai_subcategory=c.subcategory,
            ai_severity=c.severity,
            ai_confidence=c.classifier_conf,
            location_state=c.location_state,
        )
        for c in complaints
    ]

    return ReviewQueueResponse(total=total, page=page, page_size=page_size, items=items)


# ── POST /{ref}/reclassify ────────────────────────────────────────────────────

@router.post("/{ref}/reclassify", response_model=ComplaintDetail)
async def reclassify_complaint(
    ref: str,
    body: ReclassifyRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_role("reviewer", "super_admin")),
):
    """
    Reclassify a complaint: update category/subcategory/severity/dept_code,
    re-run routing and dispatch, write AuditLog + StatusEvent.

    Requirements: 12.2, 12.3, 12.4, 12.5
    """
    result = await db.execute(
        select(Complaint).where(Complaint.reference_number == ref)
    )
    complaint = result.scalar_one_or_none()
    if not complaint:
        raise ComplaintNotFound(f"Complaint {ref!r} not found.")

    reviewer = current_user.get("sub", "unknown")
    now = datetime.now(timezone.utc)

    # Capture old values for audit
    old_values = {
        "category": complaint.category,
        "subcategory": complaint.subcategory,
        "severity": complaint.severity,
        "dept_code": complaint.dept_code,
        "status": complaint.status,
    }

    # Look up dept_name from Department table
    dept_result = await db.execute(
        select(Department).where(Department.code == body.dept_code)
    )
    dept = dept_result.scalar_one_or_none()
    dept_name = dept.name if dept else body.dept_code

    # Update classification fields
    complaint.category = body.category
    complaint.subcategory = body.subcategory
    complaint.severity = body.severity
    complaint.dept_code = body.dept_code
    complaint.dept_name = dept_name
    complaint.reviewed_by = reviewer
    complaint.reviewed_at = now
    complaint.updated_at = now

    # Re-run routing + dispatch
    complaint.routed_at = now
    complaint.dispatched_at = now
    complaint.dispatch_method = "webhook"
    complaint.dispatch_ref = f"RECLASSIFY-{complaint.reference_number}"

    old_status = old_values["status"]
    complaint.status = "dispatched"

    new_values = {
        "category": complaint.category,
        "subcategory": complaint.subcategory,
        "severity": complaint.severity,
        "dept_code": complaint.dept_code,
        "status": "dispatched",
        "reviewer_note": body.reviewer_note,
    }

    # AuditLog (Requirement 12.5)
    db.add(
        AuditLog(
            complaint_id=complaint.id,
            actor=reviewer,
            action="reclassify",
            old_value=old_values,
            new_value=new_values,
        )
    )

    # StatusEvent (Requirement 12.4)
    db.add(
        StatusEvent(
            complaint_id=complaint.id,
            from_status=old_status,
            to_status="dispatched",
            note=body.reviewer_note or f"Reclassified by {reviewer}",
            actor=reviewer,
        )
    )

    await db.flush()
    logger.info("Complaint %s reclassified by %s → %s/%s", ref, reviewer, body.category, body.dept_code)
    return ComplaintDetail.model_validate(complaint)


# ── POST /{ref}/mark-duplicate ────────────────────────────────────────────────

@router.post("/{ref}/mark-duplicate", response_model=ComplaintDetail)
async def mark_duplicate(
    ref: str,
    body: MarkDuplicateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_role("reviewer", "super_admin")),
):
    """
    Mark a complaint as a duplicate of another.

    Sets complaint.duplicate_of = original.id, status = 'duplicate',
    writes AuditLog + StatusEvent.

    Requirements: 12.6
    """
    # Fetch the complaint to mark
    result = await db.execute(
        select(Complaint).where(Complaint.reference_number == ref)
    )
    complaint = result.scalar_one_or_none()
    if not complaint:
        raise ComplaintNotFound(f"Complaint {ref!r} not found.")

    # Fetch the original complaint
    orig_result = await db.execute(
        select(Complaint).where(
            Complaint.reference_number == body.original_reference_number
        )
    )
    original = orig_result.scalar_one_or_none()
    if not original:
        raise ComplaintNotFound(
            f"Original complaint {body.original_reference_number!r} not found."
        )

    reviewer = current_user.get("sub", "unknown")
    now = datetime.now(timezone.utc)
    old_status = complaint.status

    complaint.duplicate_of = original.id
    complaint.status = "duplicate"
    complaint.updated_at = now

    # AuditLog (Requirement 12.6)
    db.add(
        AuditLog(
            complaint_id=complaint.id,
            actor=reviewer,
            action="mark_duplicate",
            old_value={"status": old_status, "duplicate_of": None},
            new_value={
                "status": "duplicate",
                "duplicate_of": str(original.id),
                "original_reference_number": body.original_reference_number,
            },
        )
    )

    # StatusEvent
    db.add(
        StatusEvent(
            complaint_id=complaint.id,
            from_status=old_status,
            to_status="duplicate",
            note=f"Marked as duplicate of {body.original_reference_number} by {reviewer}",
            actor=reviewer,
        )
    )

    await db.flush()
    logger.info("Complaint %s marked as duplicate of %s by %s", ref, body.original_reference_number, reviewer)
    return ComplaintDetail.model_validate(complaint)
