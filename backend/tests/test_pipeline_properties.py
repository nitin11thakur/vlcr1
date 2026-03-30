# Feature: vlcr-fullstack-rebuild, Property 1: PII scrubbing removes all sensitive patterns
# Feature: vlcr-fullstack-rebuild, Property 2: Classification confidence gate
"""
Property-based tests for the VLCR pipeline service.

Tests use:
- Hypothesis for property generation
- SQLite in-memory (aiosqlite) for DB isolation
- fakeredis for Redis isolation
- unittest.mock for mocking external services
"""

import asyncio
import re
import sys
import types
from unittest.mock import AsyncMock, patch

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

# ── Patch database engine before importing app modules ───────────────────────
# app/core/database.py creates a PostgreSQL engine at module level.
# We intercept it before import so tests use SQLite in-memory instead.

from sqlalchemy import JSON
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

SQLITE_URL = "sqlite+aiosqlite:///:memory:"

# Create a lightweight Base for test models
class _TestBase(DeclarativeBase):
    pass


# Patch the database module so app models use our test Base
_fake_db_module = types.ModuleType("app.core.database")
_fake_db_module.Base = _TestBase
sys.modules["app.core.database"] = _fake_db_module

# JSONB is PostgreSQL-only; swap it for JSON so SQLite can create the schema
_fake_pg_module = types.ModuleType("sqlalchemy.dialects.postgresql")
_fake_pg_module.UUID = __import__("sqlalchemy").types.Uuid
_fake_pg_module.JSONB = JSON
sys.modules["sqlalchemy.dialects.postgresql"] = _fake_pg_module

# Now import app modules (they'll use the patched Base)
from app.models.models import (  # noqa: E402
    AuditLog,
    Complaint,
    Department,
    GovUser,
    RoutingRule,
    StatusEvent,
)
from app.services.pipeline import process_text_complaint  # noqa: E402


# ── Session factory ───────────────────────────────────────────────────────────

async def _make_session() -> AsyncSession:
    """Create a fresh in-memory SQLite DB and return a session."""
    engine = create_async_engine(SQLITE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_TestBase.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    return factory()


# ── Mock helpers ──────────────────────────────────────────────────────────────

def _make_classifier_mock(confidence: float):
    """Return an async callable for classify_complaint that returns the given confidence."""
    async def _mock_classify(text_en, location_raw=None, state_code=None):
        return {
            "category": "Water",
            "subcategory": "Hand pump broken",
            "severity": "medium",
            "confidence": confidence,
            "dept_prefix": "JJM",
            "location_state": None,
            "location_district": None,
            "location_block": None,
            "location_village": None,
            "reasoning": "mock",
        }
    return _mock_classify


# ── Property 1: PII scrubbing removes all sensitive patterns ─────────────────

from app.services.classifier import scrub_pii  # noqa: E402

_AADHAAR_RE = re.compile(r'\b\d{12}\b')
_PHONE_RE   = re.compile(r'\b[6-9]\d{9}\b')
_PAN_RE     = re.compile(r'\b[A-Z]{5}\d{4}[A-Z]\b')
_EMAIL_RE   = re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b')


# Feature: vlcr-fullstack-rebuild, Property 1: PII scrubbing removes all sensitive patterns
@given(st.text())
@settings(max_examples=100)
def test_scrub_pii_removes_all_sensitive_patterns(text: str):
    """
    For any input text, scrub_pii must remove all Aadhaar, phone, PAN, and
    email patterns so that none remain in the returned string.

    Validates: Requirements 18.1, 8.4
    """
    result = scrub_pii(text)

    assert _AADHAAR_RE.search(result) is None, (
        f"Aadhaar pattern still present after scrubbing. Result snippet: {result[:200]!r}"
    )
    assert _PHONE_RE.search(result) is None, (
        f"Phone pattern still present after scrubbing. Result snippet: {result[:200]!r}"
    )
    assert _PAN_RE.search(result) is None, (
        f"PAN pattern still present after scrubbing. Result snippet: {result[:200]!r}"
    )
    assert _EMAIL_RE.search(result) is None, (
        f"Email pattern still present after scrubbing. Result snippet: {result[:200]!r}"
    )


# ── Property 2: Classification confidence gate ────────────────────────────────

# Feature: vlcr-fullstack-rebuild, Property 2: Classification confidence gate
@given(st.floats(min_value=0.0, max_value=0.699, allow_nan=False, allow_infinity=False))
@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_low_confidence_sets_review_required(confidence: float):
    """
    For any classifier confidence below MIN_CLASSIFIER_CONFIDENCE (0.70),
    the pipeline must set complaint.status == "review_required" and
    complaint.dept_code must be None.

    Validates: Requirements 8.5
    """

    async def _run():
        session = await _make_session()
        try:
            with (
                patch(
                    "app.services.pipeline.detect_language",
                    AsyncMock(return_value=("hi", 0.9)),
                ),
                patch(
                    "app.services.pipeline.translate_to_english",
                    AsyncMock(return_value=("test complaint text", 1.0)),
                ),
                patch(
                    "app.services.pipeline.translate_to_hindi",
                    AsyncMock(return_value="test complaint text"),
                ),
                patch(
                    "app.services.pipeline.send_acknowledgement",
                    AsyncMock(return_value=True),
                ),
                patch(
                    "app.services.pipeline.classify_complaint",
                    side_effect=_make_classifier_mock(confidence),
                ),
            ):
                complaint = await process_text_complaint(
                    db=session,
                    text="There is a problem with the water supply in our village.",
                    citizen_phone=None,
                    citizen_name=None,
                    channel="web",
                    location_raw=None,
                )

            assert complaint.status == "review_required", (
                f"Expected status='review_required' for confidence={confidence:.4f}, "
                f"got status='{complaint.status}'"
            )
            assert complaint.dept_code is None, (
                f"Expected dept_code=None for confidence={confidence:.4f}, "
                f"got dept_code='{complaint.dept_code}'"
            )
        finally:
            await session.close()

    asyncio.run(_run())


# -- Property 3: ASR confidence gate --

# Feature: vlcr-fullstack-rebuild, Property 3: ASR confidence gate
from app.services.pipeline import process_voice_complaint  # noqa: E402


@given(st.floats(min_value=0.0, max_value=0.749, allow_nan=False, allow_infinity=False))
@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_low_asr_confidence_sets_review_required(asr_conf: float):
    """
    For any ASR confidence below MIN_ASR_CONFIDENCE (0.75), the pipeline must
    set complaint.status == 'review_required' and must not proceed to
    classification (complaint.category remains None).

    Validates: Requirements 5.4, 19.4
    """

    async def _run():
        session = await _make_session()
        try:
            with patch(
                "app.services.pipeline.transcribe_audio",
                AsyncMock(return_value=("some transcribed text", asr_conf)),
            ):
                complaint = await process_voice_complaint(
                    db=session,
                    audio_s3_key="test/audio.wav",
                    citizen_phone=None,
                    channel="ivr",
                    hint_lang="hi",
                )

            assert complaint.status == 'review_required', (
                f"Expected status='review_required' for asr_conf={asr_conf:.4f}, "
                f"got status='{complaint.status}'"
            )
            assert complaint.category is None, (
                f"Expected category=None (classification skipped) for asr_conf={asr_conf:.4f}, "
                f"got category='{complaint.category}'"
            )
        finally:
            await session.close()

    asyncio.run(_run())


# ── Property 8: Status event completeness ─────────────────────────────────────

# Feature: vlcr-fullstack-rebuild, Property 8: Status event completeness
from sqlalchemy import select as _select
from app.models.models import StatusEvent as _StatusEvent

_valid_complaint_text = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd", "Zs")),
    min_size=20,
    max_size=200,
)


@given(_valid_complaint_text)
@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_full_pipeline_creates_status_events(text: str):
    """
    For any complaint that runs through the full pipeline (all mocked services
    returning success), StatusEvent records must exist for every status
    transition executed, and the sequence of to_status values must include
    at minimum 'processing' and the final complaint status.

    Validates: Requirements 21.1
    """

    async def _run():
        session = await _make_session()
        try:
            with (
                patch("app.services.pipeline.detect_language", AsyncMock(return_value=("hi", 0.95))),
                patch("app.services.pipeline.translate_to_english", AsyncMock(return_value=("water supply broken in village", 1.0))),
                patch("app.services.pipeline.translate_to_hindi", AsyncMock(return_value="test")),
                patch("app.services.pipeline.classify_complaint", AsyncMock(return_value={
                    "category": "Water", "subcategory": "Supply disruption",
                    "severity": "high", "confidence": 0.92, "dept_prefix": "JJM",
                    "location_state": "Bihar", "location_district": None,
                    "location_block": None, "location_village": None, "reasoning": "mock",
                })),
                patch("app.services.pipeline.send_acknowledgement", AsyncMock(return_value=True)),
            ):
                complaint = await process_text_complaint(
                    db=session, text=text, citizen_phone=None,
                    citizen_name=None, channel="web", location_raw=None,
                )

            result = await session.execute(
                _select(_StatusEvent)
                .where(_StatusEvent.complaint_id == complaint.id)
                .order_by(_StatusEvent.created_at)
            )
            events = result.scalars().all()
            to_statuses = [e.to_status for e in events]

            assert len(events) >= 2, f"Expected >=2 StatusEvents, got {len(events)}. to_statuses={to_statuses}"
            assert "processing" in to_statuses, f"'processing' missing. to_statuses={to_statuses}"
            assert complaint.status in ("dispatched", "review_required"), f"Unexpected final status: {complaint.status!r}"
            assert events[-1].to_status == complaint.status, (
                f"Last event to_status={events[-1].to_status!r} != complaint.status={complaint.status!r}"
            )
        finally:
            await session.close()

    asyncio.run(_run())


# ── Property 9: Routing rule lookup correctness ───────────────────────────────

# Feature: vlcr-fullstack-rebuild, Property 9: Routing rule lookup correctness
import uuid as _uuid
from app.services.pipeline import _find_routing_rule

STATES = ["Bihar", "Maharashtra", "Tamil Nadu", "Uttar Pradesh", "West Bengal", "Karnataka", "Gujarat", "Rajasthan"]
CATEGORIES = ["Water", "Roads & Infrastructure", "Electricity", "Sanitation", "Health", "Revenue & Land", "Law & Order", "Education", "Agriculture"]


@given(st.sampled_from(STATES), st.sampled_from(CATEGORIES))
@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_routing_db_and_cache_agree(state: str, category: str):
    """
    For any (state, category) pair, calling _find_routing_rule twice must
    return the same result whether served from DB or Redis cache.

    Validates: Requirements 9.1, 9.4, 14.5
    """
    import fakeredis.aioredis as _fakeredis

    async def _run():
        session = await _make_session()
        try:
            dept = Department(
                code=f"{state[:2].upper()}_{category[:3].upper()}",
                name=f"{state} {category} Dept",
                state_code=state[:2].upper(),
                dispatch_type="webhook",
                sla_hours=72,
            )
            session.add(dept)
            await session.flush()

            rule = RoutingRule(
                id=_uuid.uuid4(),
                dept_code=dept.code,
                state_code=state,
                category=category,
                priority=10,
                is_active=True,
            )
            session.add(rule)
            await session.flush()

            fake_redis = _fakeredis.FakeRedis(decode_responses=True)

            async def _fake_cache_get(key: str):
                import json
                val = await fake_redis.get(key)
                if val is None:
                    return None
                try:
                    return json.loads(val)
                except Exception:
                    return val

            async def _fake_cache_set(key: str, value, ttl: int):
                import json
                serialised = json.dumps(value) if not isinstance(value, str) else value
                await fake_redis.setex(key, ttl, serialised)

            with (
                patch("app.services.pipeline.cache_get", side_effect=_fake_cache_get),
                patch("app.services.pipeline.cache_set", side_effect=_fake_cache_set),
            ):
                result_first = await _find_routing_rule(session, state, category)
                result_second = await _find_routing_rule(session, state, category)

            assert result_first is not None, f"Expected RoutingRule for state={state!r}, category={category!r}"
            assert result_second is not None, "Second call (cache hit) returned None"
            assert result_first.id == result_second.id, f"id mismatch: DB={result_first.id} != cache={result_second.id}"
            assert result_first.dept_code == result_second.dept_code, (
                f"dept_code mismatch: DB={result_first.dept_code!r}, cache={result_second.dept_code!r}"
            )
        finally:
            await session.close()

    asyncio.run(_run())
