"""
app/routers/complaints.py
--------------------------
Complaint submission and management endpoints.

Routes (prefix /api/v1/complaints set in main.py):
  POST   /text                  — submit text complaint (public, rate-limited)
  POST   /voice                 — submit voice complaint (public, multipart)
  GET    /                      — paginated complaint list (JWT)
  GET    /{ref}                 — complaint detail (JWT)
  PATCH  /{ref}/status          — update status (JWT)
  GET    /{ref}/audit           — audit log (JWT reviewer+)

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 18.5, 21.2, 21.3
"""

import hashlib
import logging
import os
import tempfile
import uuid
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_role
from app.core.database import get_db
from app.core.exceptions import ComplaintNotFound, RateLimitExceeded
from app.core.redis_client import cache_set, dedup_check, rate_limit_check
from app.models.models import AuditLog, Complaint, StatusEvent
from app.schemas.schemas import (
    AuditLogEntry,
    AuditLogResponse,
    ComplaintAcknowledgement,
    ComplaintDetail,
    ComplaintListItem,
    ComplaintListResponse,
    StatusUpdateRequest,
    TextComplaintRequest,
)
from app.services.pipeline import process_text_complaint, process_voice_complaint

logger = logging.getLogger("vlcr.routers.complaints")

router = APIRouter(tags=["complaints"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_client_ip(request: Request) -> str:
    """Return the real client IP, honouring X-Forwarded-For."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _complaint_hash(text: str, phone: Optional[str]) -> str:
    """SHA-256 of text + phone for dedup keying."""
    raw = (text + (phone or "")).encode()
    return hashlib.sha256(raw).hexdigest()


# ── POST /text ────────────────────────────────────────────────────────────────

@router.post("/text", response_model=ComplaintAcknowledgement, status_code=201)
async def submit_text_complaint(
    request: Request,
    body: TextComplaintRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Submit a text complaint.

    Checks (in order):
      1. IP rate limit — 10 requests / 60 s
      2. Daily phone limit — 5 complaints / day per phone (if phone provided)
      3. Dedup — same text+phone within 72 h returns original reference_number

    Requirements: 4.1, 4.2, 4.3, 4.4, 4.5
    """
    client_ip = _get_client_ip(request)

    # 1. IP rate limit
    if not await rate_limit_check(f"rl:ip:{client_ip}", 10, 60):
        raise RateLimitExceeded("IP rate limit exceeded (10 requests/min).")

    # 2. Daily phone limit
    if body.citizen_phone:
        today = date.today().isoformat()
        if not await rate_limit_check(
            f"daily_complaints:{body.citizen_phone}:{today}", 5, 86400
        ):
            raise RateLimitExceeded("Daily complaint limit exceeded (5/day per phone).")

    # 3. Dedup check (72 h = 259200 s)
    hash_key = _complaint_hash(body.text, body.citizen_phone)
    existing_id = await dedup_check(hash_key, 259200)
    if existing_id:
        # Look up the original complaint's reference_number
        result = await db.execute(
            select(Complaint).where(Complaint.reference_number == existing_id)
        )
        original = result.scalar_one_or_none()
        if original:
            return ComplaintAcknowledgement(
                reference_number=original.reference_number,
                status=original.status,
                message="Duplicate complaint detected. Your original submission is being processed.",
                dept_name=original.dept_name,
                category=original.category,
                severity=original.severity,
            )

    # Process the complaint
    complaint = await process_text_complaint(
        db=db,
        text=body.text,
        citizen_phone=body.citizen_phone,
        citizen_name=body.citizen_name,
        channel=body.channel,
        location_raw=body.location_raw,
    )

    # Store dedup key → reference_number so future duplicates resolve correctly
    await cache_set(f"dedup:{hash_key}", complaint.reference_number, 259200)

    return ComplaintAcknowledgement(
        reference_number=complaint.reference_number,
        status=complaint.status,
        message="Your complaint has been received and is being processed.",
        dept_name=complaint.dept_name,
        category=complaint.category,
        severity=complaint.severity,
    )


# ── POST /voice ───────────────────────────────────────────────────────────────

@router.post("/voice", response_model=ComplaintAcknowledgement, status_code=201)
async def submit_voice_complaint(
    request: Request,
    audio: UploadFile = File(..., description="Audio file (WAV/MP3)"),
    citizen_phone: Optional[str] = Form(None),
    channel: str = Form("ivr"),
    hint_lang: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Submit a voice complaint via multipart upload.

    Uploads audio to S3 (or uses a mock key if S3 is not configured),
    then runs the voice pipeline.

    Requirements: 4.2, 5.2
    """
    from app.core.config import settings

    audio_uuid = str(uuid.uuid4())
    s3_key = f"voice/{audio_uuid}.wav"

    if settings.AWS_ACCESS_KEY_ID:
        # Upload to real S3
        try:
            import boto3

            contents = await audio.read()
            s3_client = boto3.client(
                "s3",
                aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
                region_name=settings.AWS_REGION,
            )
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
                tmp.write(contents)
                tmp_path = tmp.name
            try:
                s3_client.upload_file(tmp_path, settings.S3_BUCKET_AUDIO, s3_key)
            finally:
                os.unlink(tmp_path)
            logger.info("Uploaded audio to S3: %s", s3_key)
        except Exception as exc:
            logger.warning("S3 upload failed (%s) — using mock key %s", exc, s3_key)
    else:
        # No S3 configured — save locally and use mock key
        contents = await audio.read()
        local_dir = "/tmp/vlcr_audio"
        os.makedirs(local_dir, exist_ok=True)
        local_path = os.path.join(local_dir, f"{audio_uuid}.wav")
        with open(local_path, "wb") as f:
            f.write(contents)
        logger.info("S3 not configured — saved audio locally: %s (key=%s)", local_path, s3_key)

    complaint = await process_voice_complaint(
        db=db,
        audio_s3_key=s3_key,
        citizen_phone=citizen_phone,
        channel=channel,
        hint_lang=hint_lang,
    )

    return ComplaintAcknowledgement(
        reference_number=complaint.reference_number,
        status=complaint.status,
        message="Your voice complaint has been received and is being processed.",
        dept_name=complaint.dept_name,
        category=complaint.category,
        severity=complaint.severity,
    )


# ── GET / ─────────────────────────────────────────────────────────────────────

@router.get("", response_model=ComplaintListResponse)
async def list_complaints(
    status: Optional[str] = None,
    severity: Optional[str] = None,
    category: Optional[str] = None,
    state_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Paginated list of complaints with optional filters.

    Filters: status, severity, category, state_code, date_from (ISO date), date_to (ISO date)
    Requirements: 4.6
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
        query = query.where(Complaint.created_at >= datetime.fromisoformat(date_from))
    if date_to:
        query = query.where(Complaint.created_at <= datetime.fromisoformat(date_to))

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


# ── GET /{ref} ────────────────────────────────────────────────────────────────

@router.get("/{ref}", response_model=ComplaintDetail)
async def get_complaint(
    ref: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Return full complaint detail by reference number.
    Requirements: 4.6
    """
    result = await db.execute(
        select(Complaint).where(Complaint.reference_number == ref)
    )
    complaint = result.scalar_one_or_none()
    if not complaint:
        raise ComplaintNotFound(f"Complaint {ref!r} not found.")

    return ComplaintDetail.model_validate(complaint)


# ── PATCH /{ref}/status ───────────────────────────────────────────────────────

@router.patch("/{ref}/status", response_model=ComplaintDetail)
async def update_complaint_status(
    ref: str,
    body: StatusUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Manually advance a complaint's status.

    Writes both an AuditLog record and a StatusEvent record.
    Requirements: 21.2, 21.3
    """
    result = await db.execute(
        select(Complaint).where(Complaint.reference_number == ref)
    )
    complaint = result.scalar_one_or_none()
    if not complaint:
        raise ComplaintNotFound(f"Complaint {ref!r} not found.")

    old_status = complaint.status
    complaint.status = body.status
    complaint.updated_at = datetime.now(timezone.utc)

    actor = current_user.get("sub", "unknown")
    client_ip = _get_client_ip(request)

    # AuditLog entry (Requirement 21.2)
    db.add(
        AuditLog(
            complaint_id=complaint.id,
            actor=actor,
            action="status_update",
            old_value={"status": old_status},
            new_value={"status": body.status, "note": body.note},
            ip_address=client_ip,
        )
    )

    # StatusEvent entry (Requirement 21.1)
    db.add(
        StatusEvent(
            complaint_id=complaint.id,
            from_status=old_status,
            to_status=body.status,
            note=body.note or f"Status updated by {actor}",
            actor=actor,
        )
    )

    await db.flush()
    return ComplaintDetail.model_validate(complaint)


# ── GET /{ref}/audit ──────────────────────────────────────────────────────────

@router.get("/{ref}/audit", response_model=AuditLogResponse)
async def get_audit_log(
    ref: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_role("reviewer", "super_admin")),
):
    """
    Return the full audit log for a complaint.

    Requires role: reviewer or super_admin.
    Requirements: 21.3
    """
    result = await db.execute(
        select(Complaint).where(Complaint.reference_number == ref)
    )
    complaint = result.scalar_one_or_none()
    if not complaint:
        raise ComplaintNotFound(f"Complaint {ref!r} not found.")

    logs_result = await db.execute(
        select(AuditLog)
        .where(AuditLog.complaint_id == complaint.id)
        .order_by(AuditLog.created_at.asc())
    )
    logs = logs_result.scalars().all()

    return AuditLogResponse(
        reference_number=ref,
        total=len(logs),
        entries=[AuditLogEntry.model_validate(log) for log in logs],
    )
