"""
app/models/models.py
--------------------
VLCR ORM Models — all six tables.
Schema from VLCR-TRD-v1.0 §3 / Requirements 2.1, 2.4, 2.5.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column, String, Float, Text, DateTime, ForeignKey,
    Boolean, Integer, Index,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship

from app.core.database import Base


def utcnow():
    return datetime.now(timezone.utc)


# ── Complaints ────────────────────────────────────────────────────────────────

class Complaint(Base):
    __tablename__ = "complaints"

    id                  = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reference_number    = Column(String(50), unique=True, nullable=False, index=True)
    created_at          = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at          = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    # Citizen
    citizen_phone       = Column(String(20), index=True)
    citizen_name        = Column(String(200))
    citizen_lang        = Column(String(10), nullable=False, default="hi")  # BCP-47

    # Input
    input_channel       = Column(String(20), nullable=False)   # ivr | whatsapp | web | sms
    input_type          = Column(String(10), nullable=False)   # voice | text
    raw_audio_s3_key    = Column(String(500))
    raw_text_original   = Column(Text)

    # Language Processing
    transcript_raw      = Column(Text)
    transcript_norm     = Column(Text)
    translation_hi      = Column(Text)
    translation_en      = Column(Text)
    lang_detect_code    = Column(String(10))
    asr_confidence      = Column(Float)
    nlp_confidence      = Column(Float)

    # Classification
    category            = Column(String(100), index=True)
    subcategory         = Column(String(200))
    severity            = Column(String(20), index=True)  # critical | high | medium | low
    classifier_conf     = Column(Float)
    location_state      = Column(String(100), index=True)
    location_district   = Column(String(100))
    location_block      = Column(String(100))
    location_village    = Column(String(200))
    location_raw        = Column(Text)
    location_lat        = Column(Float)
    location_lon        = Column(Float)

    # Routing
    dept_code           = Column(String(50), ForeignKey("departments.code"), index=True)
    dept_name           = Column(String(200))
    routing_rule_id     = Column(UUID(as_uuid=True), ForeignKey("routing_rules.id"))
    routed_at           = Column(DateTime(timezone=True))
    dispatch_method     = Column(String(20))   # cpgrams | webhook | email
    dispatch_ref        = Column(String(200))
    dispatched_at       = Column(DateTime(timezone=True))

    # Lifecycle
    status              = Column(String(30), nullable=False, default="received", index=True)
    # received | processing | classified | routed | dispatched
    # acknowledged | in_progress | resolved | review_required

    duplicate_of        = Column(UUID(as_uuid=True), ForeignKey("complaints.id"))
    review_reason       = Column(Text)
    reviewed_by         = Column(String(200))
    reviewed_at         = Column(DateTime(timezone=True))
    classifier_raw      = Column(JSONB)   # full LLM response for audit

    # Relationships
    department          = relationship("Department", back_populates="complaints", foreign_keys=[dept_code])
    routing_rule        = relationship("RoutingRule", foreign_keys=[routing_rule_id])
    audit_logs          = relationship("AuditLog", back_populates="complaint", cascade="all, delete-orphan")
    status_events       = relationship("StatusEvent", back_populates="complaint", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_complaints_created_at", "created_at"),
        Index("ix_complaints_status_severity", "status", "severity"),
        Index("ix_complaints_dept_state", "dept_code", "location_state"),
    )


# ── Departments ───────────────────────────────────────────────────────────────

class Department(Base):
    __tablename__ = "departments"

    code                = Column(String(50), primary_key=True)  # e.g. MH_PWD
    name                = Column(String(200), nullable=False)
    state_code          = Column(String(10), nullable=False, index=True)  # ISO 3166-2:IN
    dispatch_type       = Column(String(20), nullable=False)   # webhook | email | cpgrams
    dispatch_endpoint   = Column(String(500))
    dispatch_api_key    = Column(String(500))                  # encrypted in prod
    contact_email       = Column(String(200))
    sla_hours           = Column(Integer, default=72)
    escalation_hours    = Column(Integer, default=24)
    is_active           = Column(Boolean, default=True)
    created_at          = Column(DateTime(timezone=True), default=utcnow)
    updated_at          = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    complaints          = relationship("Complaint", back_populates="department", foreign_keys="Complaint.dept_code")
    routing_rules       = relationship("RoutingRule", back_populates="department", cascade="all, delete-orphan")


# ── Routing Rules ─────────────────────────────────────────────────────────────

class RoutingRule(Base):
    __tablename__ = "routing_rules"

    id                  = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dept_code           = Column(String(50), ForeignKey("departments.code"), nullable=False)
    state_code          = Column(String(10), nullable=False, index=True)
    category            = Column(String(100), nullable=False)
    subcategory         = Column(String(200))
    priority            = Column(Integer, default=100)   # lower = higher priority
    is_active           = Column(Boolean, default=True)
    created_at          = Column(DateTime(timezone=True), default=utcnow)
    updated_at          = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    department          = relationship("Department", back_populates="routing_rules")


# ── Government Users ──────────────────────────────────────────────────────────

class GovUser(Base):
    """
    hashed_password stores a bcrypt hash (cost ≥ 12) — never plaintext.
    See app/core/auth.py for hashing helpers.
    """
    __tablename__ = "gov_users"

    id                  = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username            = Column(String(100), unique=True, nullable=False)
    email               = Column(String(200), unique=True, nullable=False)
    hashed_password     = Column(String(200), nullable=False)   # bcrypt, cost ≥ 12
    full_name           = Column(String(200))
    role                = Column(String(30), nullable=False, default="officer")
    # officer | reviewer | analyst | super_admin
    state_code          = Column(String(10))
    dept_code           = Column(String(50), ForeignKey("departments.code"))
    mfa_enabled         = Column(Boolean, default=False)
    is_active           = Column(Boolean, default=True)
    last_login          = Column(DateTime(timezone=True))
    created_at          = Column(DateTime(timezone=True), default=utcnow)


# ── Audit Log ─────────────────────────────────────────────────────────────────

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id                  = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    complaint_id        = Column(UUID(as_uuid=True), ForeignKey("complaints.id", ondelete="CASCADE"), index=True)
    actor               = Column(String(200))              # username or "system"
    action              = Column(String(100), nullable=False)
    old_value           = Column(JSONB)
    new_value           = Column(JSONB)
    ip_address          = Column(String(50))
    created_at          = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    complaint           = relationship("Complaint", back_populates="audit_logs")

    __table_args__ = (Index("ix_audit_created_at", "created_at"),)


# ── Status Events (Lifecycle Timeline) ───────────────────────────────────────

class StatusEvent(Base):
    __tablename__ = "status_events"

    id                  = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    complaint_id        = Column(UUID(as_uuid=True), ForeignKey("complaints.id", ondelete="CASCADE"), index=True)
    from_status         = Column(String(30))
    to_status           = Column(String(30), nullable=False)
    note                = Column(Text)
    actor               = Column(String(200), default="system")
    created_at          = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    complaint           = relationship("Complaint", back_populates="status_events")
