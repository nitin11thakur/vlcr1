"""
app/schemas/schemas.py
----------------------
VLCR — Pydantic request/response schemas for all API endpoints.

Security note (Requirement 18.2): citizen_phone is intentionally excluded
from all public-facing response schemas (TrackingResponse, ComplaintAcknowledgement,
ComplaintListItem, AuditLogEntry). It is only present in ComplaintDetail which
is served on authenticated endpoints only.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator
import re


# ── Enums / Literals ──────────────────────────────────────────────────────────

CHANNEL_TEXT = "^(web|whatsapp|sms)$"
CHANNEL_VOICE = "^(ivr|whatsapp|web)$"
SEVERITY_VALUES = {"critical", "high", "medium", "low"}


# ── Request Schemas ───────────────────────────────────────────────────────────

class TextComplaintRequest(BaseModel):
    """
    POST /api/v1/complaints/text
    Requirement 4.1: text 10-2000 chars, optional E.164 phone, optional name,
    channel enum (web|whatsapp|sms), optional location.
    """
    text: str = Field(
        ...,
        min_length=10,
        max_length=2000,
        description="Complaint text in any supported Indian language",
    )
    citizen_phone: Optional[str] = Field(
        None,
        description="Citizen phone number in E.164 format (e.g. +919876543210)",
    )
    citizen_name: Optional[str] = Field(None, max_length=200)
    channel: str = Field("web", pattern=CHANNEL_TEXT)
    location_raw: Optional[str] = Field(None, max_length=500)

    @field_validator("citizen_phone")
    @classmethod
    def validate_e164(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not re.match(r"^\+[1-9]\d{6,14}$", v):
            raise ValueError("citizen_phone must be in E.164 format (e.g. +919876543210)")
        return v

    model_config = {
        "json_schema_extra": {
            "example": {
                "text": "hamare gaon mein handpump tuta hua hai. pani nahi aa raha hai.",
                "citizen_phone": "+919876543210",
                "citizen_name": "Ramesh Kumar",
                "channel": "web",
                "location_raw": "Village Sonpur, Block Maner, District Patna, Bihar",
            }
        }
    }


class VoiceComplaintRequest(BaseModel):
    """
    POST /api/v1/complaints/voice
    Accepts an S3 key for the uploaded audio file.
    """
    audio_s3_key: str = Field(..., description="S3 key of the uploaded audio file")
    citizen_phone: Optional[str] = Field(None, description="Caller phone in E.164 format")
    channel: str = Field("ivr", pattern=CHANNEL_VOICE)
    hint_lang: Optional[str] = Field(
        None,
        description="BCP-47 language hint from IVR DTMF key press",
    )

    @field_validator("citizen_phone")
    @classmethod
    def validate_e164(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not re.match(r"^\+[1-9]\d{6,14}$", v):
            raise ValueError("citizen_phone must be in E.164 format")
        return v


class LoginRequest(BaseModel):
    """POST /api/v1/auth/login - JSON body login."""
    username: str
    password: str


class ReclassifyRequest(BaseModel):
    """
    POST /api/v1/review/{reference_number}/reclassify
    Requirement 12.2
    """
    category: str
    subcategory: str
    severity: str
    dept_code: str
    reviewer_note: Optional[str] = None

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, v: str) -> str:
        if v not in SEVERITY_VALUES:
            raise ValueError(f"severity must be one of {sorted(SEVERITY_VALUES)}")
        return v


class RoutingRuleCreate(BaseModel):
    """POST /api/v1/routing/rules - create a new routing rule (super_admin only)."""
    dept_code: str
    state_code: str
    category: str
    subcategory: Optional[str] = None
    priority: int = Field(100, ge=1, description="Lower value = higher priority")


class StatusUpdateRequest(BaseModel):
    """
    PATCH /api/v1/complaints/{reference_number}/status
    Allows an authenticated officer to manually advance a complaint status.
    """
    status: str = Field(
        ...,
        description=(
            "New status value. One of: received, processing, classified, routed, "
            "dispatched, acknowledged, in_progress, resolved, review_required"
        ),
    )
    note: Optional[str] = Field(None, max_length=1000, description="Optional note for the status event")

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        allowed = {
            "received", "processing", "classified", "routed", "dispatched",
            "acknowledged", "in_progress", "resolved", "review_required",
        }
        if v not in allowed:
            raise ValueError(f"status must be one of {sorted(allowed)}")
        return v


# ── Response Schemas ──────────────────────────────────────────────────────────

class ComplaintAcknowledgement(BaseModel):
    """
    Returned immediately after a successful complaint submission.
    Requirement 4.2, 16.3
    NOTE: citizen_phone is intentionally absent (Requirement 18.2).
    """
    reference_number: str
    status: str
    message: str
    dept_name: Optional[str] = None
    category: Optional[str] = None
    severity: Optional[str] = None
    estimated_response_hours: int = 72


class ComplaintDetail(BaseModel):
    """Full complaint detail - served only on authenticated endpoints."""
    id: UUID
    reference_number: str
    created_at: datetime
    updated_at: datetime
    citizen_phone: Optional[str] = None
    citizen_name: Optional[str] = None
    citizen_lang: str
    input_channel: str
    input_type: str
    raw_text_original: Optional[str] = None
    transcript_norm: Optional[str] = None
    translation_en: Optional[str] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    severity: Optional[str] = None
    classifier_conf: Optional[float] = None
    location_state: Optional[str] = None
    location_district: Optional[str] = None
    location_block: Optional[str] = None
    location_village: Optional[str] = None
    dept_code: Optional[str] = None
    dept_name: Optional[str] = None
    status: str
    routed_at: Optional[datetime] = None
    dispatched_at: Optional[datetime] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    review_reason: Optional[str] = None

    model_config = {"from_attributes": True}


class ComplaintListItem(BaseModel):
    """Compact row used in paginated complaint lists."""
    reference_number: str
    created_at: datetime
    category: Optional[str] = None
    subcategory: Optional[str] = None
    severity: Optional[str] = None
    status: str
    citizen_lang: str
    location_district: Optional[str] = None
    location_state: Optional[str] = None
    dept_name: Optional[str] = None
    classifier_conf: Optional[float] = None

    model_config = {"from_attributes": True}


class ComplaintListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: List[ComplaintListItem]


class TrackingResponse(BaseModel):
    """
    GET /api/v1/track/{reference_number} - public, unauthenticated endpoint.
    Requirement 11.1, 11.4, 18.2: citizen_phone MUST NOT be present.
    """
    reference_number: str
    status: str
    status_label: str
    created_at: datetime
    dept_name: Optional[str] = None
    category: Optional[str] = None
    severity: Optional[str] = None
    timeline: List[Dict[str, Any]] = Field(default_factory=list)
    # citizen_phone deliberately excluded - Requirement 18.2


class DashboardStats(BaseModel):
    """GET /api/v1/dashboard/stats - Requirement 13.1"""
    total_today: int
    routed_today: int
    review_queue: int
    avg_route_seconds: float
    total_this_week: int
    critical_open: int
    resolution_rate_pct: float
    top_categories: List[Dict[str, Any]]
    by_language: List[Dict[str, Any]]
    by_state: List[Dict[str, Any]]
    queue_depths: Dict[str, Any]


class SLAMetrics(BaseModel):
    """Per-department SLA metrics row. GET /api/v1/dashboard/sla - Requirement 13.2"""
    dept_code: str
    dept_name: str
    total_complaints: int
    resolved_count: int
    avg_resolution_hours: float
    resolution_rate_pct: float
    sla_hours: int
    breached_count: int = 0


class SLAResponse(BaseModel):
    """Wrapper for the /dashboard/sla endpoint."""
    departments: List[SLAMetrics]
    generated_at: datetime


class ServiceHealth(BaseModel):
    service: str
    status: str   # healthy | degraded | down
    uptime_pct: float
    note: str = ""


class PipelineStatus(BaseModel):
    """GET /api/v1/pipeline/status - Requirement 15.1, 15.4"""
    services: List[ServiceHealth]
    queue_depths: Dict[str, Any]
    asr_latency_p95_ms: float
    classifier_latency_p95_ms: float
    total_processed_today: int


class AuditLogEntry(BaseModel):
    """
    Single audit log record.
    GET /api/v1/complaints/{reference_number}/audit - Requirement 21.3
    NOTE: citizen_phone is not included here (Requirement 18.2).
    """
    id: UUID
    complaint_id: UUID
    actor: str
    action: str
    old_value: Optional[Dict[str, Any]] = None
    new_value: Optional[Dict[str, Any]] = None
    ip_address: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AuditLogResponse(BaseModel):
    """Paginated audit log for a complaint."""
    reference_number: str
    total: int
    entries: List[AuditLogEntry]


# ── Supporting / Shared Schemas ───────────────────────────────────────────────

class ClassificationResult(BaseModel):
    category: str
    subcategory: str
    severity: str
    confidence: float
    location_state: Optional[str] = None
    location_district: Optional[str] = None
    location_block: Optional[str] = None
    location_village: Optional[str] = None
    dept_code: str
    dept_name: str


class ReviewQueueItem(BaseModel):
    """
    FIX: Field names now match the ORM Complaint model columns exactly.
    Previously used ai_category/ai_subcategory/ai_severity/ai_confidence
    which don't exist on the model, causing all fields to resolve to None
    when from_attributes=True.
    """
    reference_number: str
    created_at: datetime
    review_reason: Optional[str] = None
    citizen_lang: str
    raw_text_original: Optional[str] = None
    transcript_norm: Optional[str] = None
    translation_en: Optional[str] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    severity: Optional[str] = None
    classifier_conf: Optional[float] = None
    location_state: Optional[str] = None

    model_config = {"from_attributes": True}


class ReviewQueueResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: List[ReviewQueueItem]


class DepartmentSchema(BaseModel):
    code: str
    name: str
    state_code: str
    dispatch_type: str
    contact_email: Optional[str] = None
    sla_hours: int
    escalation_hours: int
    is_active: bool

    model_config = {"from_attributes": True}


class RoutingRuleSchema(BaseModel):
    id: Optional[UUID] = None
    dept_code: str
    state_code: str
    category: str
    subcategory: Optional[str] = None
    priority: int
    is_active: bool

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user_role: str
    user_name: str


class IVRComplaintRequest(BaseModel):
    caller_id: str
    audio_url: str
    dtmf_language_key: Optional[int] = None
    session_id: str


class IVRResponse(BaseModel):
    reference_number: str
    detected_language: str
    category: str
    dept_name: str
    acknowledgement_text: str   # In citizen's language for TTS
