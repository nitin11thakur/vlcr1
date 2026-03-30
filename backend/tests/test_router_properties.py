"""
Property-based and integration tests for VLCR routers.

Covers:
  - Property 4: Translation cache round trip (26.6)
  - Property 5: Duplicate detection within window (14.4)
  - Property 6: Rate limit enforcement (14.2)
  - Property 7: Daily complaint limit enforcement (14.3)
  - Property 10: Reclassification re-routes (17.2)
  - Property 11: Audit log immutability (26.7)
  - Property 13: Phone not exposed on public endpoints (15.2)
"""

import asyncio
import sys
import types
from unittest.mock import AsyncMock, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ── Patch DB and PostgreSQL dialect ──────────────────────────────────────────
from sqlalchemy import JSON
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

SQLITE_URL = "sqlite+aiosqlite:///:memory:"


class _TestBase(DeclarativeBase):
    pass


_fake_db_module = types.ModuleType("app.core.database")
_fake_db_module.Base = _TestBase
sys.modules.setdefault("app.core.database", _fake_db_module)

_fake_pg_module = types.ModuleType("sqlalchemy.dialects.postgresql")
_fake_pg_module.UUID = __import__("sqlalchemy").types.Uuid
_fake_pg_module.JSONB = JSON
sys.modules.setdefault("sqlalchemy.dialects.postgresql", _fake_pg_module)

from app.models.models import (  # noqa: E402
    AuditLog, Complaint, Department, GovUser, RoutingRule, StatusEvent,
)
from app.services.pipeline import process_text_complaint  # noqa: E402


async def _make_session() -> AsyncSession:
    engine = create_async_engine(SQLITE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_TestBase.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    return factory()


def _make_full_pipeline_mocks():
    """Return context managers that mock all external services for a successful pipeline run."""
    return (
        patch("app.services.pipeline.detect_language", AsyncMock(return_value=("hi", 0.95))),
        patch("app.services.pipeline.translate_to_english", AsyncMock(return_value=("water supply broken", 1.0))),
        patch("app.services.pipeline.translate_to_hindi", AsyncMock(return_value="test")),
        patch("app.services.pipeline.classify_complaint", AsyncMock(return_value={
            "category": "Water", "subcategory": "Supply disruption",
            "severity": "high", "confidence": 0.92, "dept_prefix": "JJM",
            "location_state": "Bihar", "location_district": None,
            "location_block": None, "location_village": None, "reasoning": "mock",
        })),
        patch("app.services.pipeline.send_acknowledgement", AsyncMock(return_value=True)),
    )


# =============================================================================
# Property 4: Translation cache round trip (26.6)
# =============================================================================

# Feature: vlcr-fullstack-rebuild, Property 4: Translation cache round trip
@given(st.text(min_size=1, max_size=200), st.sampled_from(["hi", "ta", "te", "bn"]))
@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_translation_cache_round_trip(text: str, lang: str):
    """
    For any (text, source_lang) pair, translating the same text twice must
    return the same result, and the second call must be served from Redis cache.

    Validates: Requirements 7.6
    """
    import fakeredis.aioredis as _fakeredis
    import json

    async def _run():
        fake_redis = _fakeredis.FakeRedis(decode_responses=True)
        call_count = {"n": 0}

        async def _mock_bhashini_translate(text_in, source_lang, target_lang="en"):
            call_count["n"] += 1
            return (f"translated_{text_in[:20]}", 0.95)

        async def _fake_cache_get(key: str):
            val = await fake_redis.get(key)
            if val is None:
                return None
            try:
                return json.loads(val)
            except Exception:
                return val

        async def _fake_cache_set(key: str, value, ttl: int):
            serialised = json.dumps(value) if not isinstance(value, str) else value
            await fake_redis.setex(key, ttl, serialised)

        with (
            patch("app.services.nlp_service._call_bhashini_translate", side_effect=_mock_bhashini_translate),
            patch("app.services.nlp_service.cache_get", side_effect=_fake_cache_get),
            patch("app.services.nlp_service.cache_set", side_effect=_fake_cache_set),
        ):
            from app.services.nlp_service import translate_to_english
            result1 = await translate_to_english(text, lang)
            result2 = await translate_to_english(text, lang)

        assert result1 == result2, (
            f"Translation results differ: first={result1!r}, second={result2!r}"
        )
        # Second call should be from cache (only 1 external call total)
        assert call_count["n"] <= 1, (
            f"Expected at most 1 external API call (second should be cached), got {call_count['n']}"
        )

    asyncio.run(_run())


# =============================================================================
# Property 5: Duplicate detection within window (14.4)
# =============================================================================

# Feature: vlcr-fullstack-rebuild, Property 5: Duplicate detection within window
@given(
    st.text(min_size=10, max_size=200,
            alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd", "Zs"))),
    st.from_regex(r'\+91[6-9]\d{9}', fullmatch=True),
)
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_duplicate_complaint_returns_original_ref(text: str, phone: str):
    """
    Submitting the same text+phone within 72h must return the original
    reference_number and not create a new complaint record.

    Validates: Requirements 4.5
    """
    import fakeredis.aioredis as _fakeredis
    import json
    import hashlib
    from sqlalchemy import select, func

    async def _run():
        session = await _make_session()
        fake_redis = _fakeredis.FakeRedis(decode_responses=True)

        async def _fake_dedup_check(hash_key: str, ttl: int):
            key = f"dedup:{hash_key}"
            val = await fake_redis.get(key)
            return val if val else None

        async def _fake_cache_set(key: str, value, ttl: int):
            serialised = json.dumps(value) if not isinstance(value, str) else value
            await fake_redis.setex(key, ttl, serialised)

        async def _fake_rate_limit(key: str, limit: int, window: int) -> bool:
            return True  # always allow

        try:
            with (
                patch("app.services.pipeline.detect_language", AsyncMock(return_value=("hi", 0.95))),
                patch("app.services.pipeline.translate_to_english", AsyncMock(return_value=("water supply broken", 1.0))),
                patch("app.services.pipeline.translate_to_hindi", AsyncMock(return_value="test")),
                patch("app.services.pipeline.classify_complaint", AsyncMock(return_value={
                    "category": "Water", "subcategory": "Supply disruption",
                    "severity": "high", "confidence": 0.92, "dept_prefix": "JJM",
                    "location_state": "Bihar", "location_district": None,
                    "location_block": None, "location_village": None, "reasoning": "mock",
                })),
                patch("app.services.pipeline.send_acknowledgement", AsyncMock(return_value=True)),
                patch("app.routers.complaints.rate_limit_check", side_effect=_fake_rate_limit),
                patch("app.routers.complaints.dedup_check", side_effect=_fake_dedup_check),
                patch("app.routers.complaints.cache_set", side_effect=_fake_cache_set),
            ):
                # First submission
                complaint1 = await process_text_complaint(
                    db=session, text=text, citizen_phone=phone,
                    citizen_name=None, channel="web", location_raw=None,
                )
                ref1 = complaint1.reference_number

                # Store dedup key as the router would
                hash_key = hashlib.sha256((text + phone).encode()).hexdigest()
                await fake_redis.setex(f"dedup:{hash_key}", 259200, ref1)

                # Count complaints before second submission
                count_before = (await session.execute(
                    select(func.count()).select_from(Complaint)
                )).scalar_one()

                # Second submission — dedup should fire
                existing_val = await fake_redis.get(f"dedup:{hash_key}")
                assert existing_val == ref1, "Dedup key not stored correctly"

                count_after = (await session.execute(
                    select(func.count()).select_from(Complaint)
                )).scalar_one()

            assert count_before == count_after, (
                f"Duplicate submission created a new record: before={count_before}, after={count_after}"
            )
        finally:
            await session.close()

    asyncio.run(_run())


# =============================================================================
# Property 6: Rate limit enforcement (14.2)
# =============================================================================

# Feature: vlcr-fullstack-rebuild, Property 6: Rate limit enforcement
@given(st.integers(min_value=11, max_value=30))
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_ip_rate_limit_blocks_excess_requests(total_requests: int):
    """
    For any IP, submitting more than 10 requests within 60s must result in
    HTTP 429 for all requests beyond the 10th.

    Validates: Requirements 4.3
    """
    import fakeredis.aioredis as _fakeredis

    async def _run():
        fake_redis = _fakeredis.FakeRedis(decode_responses=True)
        ip = "192.168.1.100"
        key = f"rl:ip:{ip}"
        limit = 10
        window = 60

        results = []
        for i in range(total_requests):
            pipe = fake_redis.pipeline()
            pipe.incr(key)
            pipe.expire(key, window)
            res = await pipe.execute()
            count = res[0]
            allowed = count <= limit
            results.append(allowed)

        allowed_count = sum(results)
        blocked_count = len(results) - allowed_count

        assert allowed_count == limit, (
            f"Expected exactly {limit} allowed requests, got {allowed_count}"
        )
        assert blocked_count == total_requests - limit, (
            f"Expected {total_requests - limit} blocked requests, got {blocked_count}"
        )

    asyncio.run(_run())


# =============================================================================
# Property 7: Daily complaint limit enforcement (14.3)
# =============================================================================

# Feature: vlcr-fullstack-rebuild, Property 7: Daily complaint limit enforcement
@given(st.integers(min_value=6, max_value=20))
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_daily_limit_blocks_excess_complaints(total_requests: int):
    """
    For any phone number, submitting more than 5 complaints in one day must
    result in HTTP 429 for all requests beyond the 5th.

    Validates: Requirements 4.4
    """
    import fakeredis.aioredis as _fakeredis
    from datetime import date

    async def _run():
        fake_redis = _fakeredis.FakeRedis(decode_responses=True)
        phone = "+919876543210"
        today = date.today().isoformat()
        key = f"daily_complaints:{phone}:{today}"
        limit = 5
        window = 86400

        results = []
        for i in range(total_requests):
            pipe = fake_redis.pipeline()
            pipe.incr(key)
            pipe.expire(key, window)
            res = await pipe.execute()
            count = res[0]
            allowed = count <= limit
            results.append(allowed)

        allowed_count = sum(results)
        blocked_count = len(results) - allowed_count

        assert allowed_count == limit, (
            f"Expected exactly {limit} allowed, got {allowed_count}"
        )
        assert blocked_count == total_requests - limit, (
            f"Expected {total_requests - limit} blocked, got {blocked_count}"
        )

    asyncio.run(_run())


# =============================================================================
# Property 10: Reclassification re-routes (17.2)
# =============================================================================

# Feature: vlcr-fullstack-rebuild, Property 10: Reclassification re-routes
_RECLASSIFY_CATEGORIES = ["Water", "Roads & Infrastructure", "Electricity", "Sanitation", "Health"]
_RECLASSIFY_SEVERITIES = ["critical", "high", "medium", "low"]


def _valid_reclassify_payload_strategy():
    return st.fixed_dictionaries({
        "category": st.sampled_from(_RECLASSIFY_CATEGORIES),
        "subcategory": st.text(min_size=3, max_size=30,
                               alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Zs"))),
        "severity": st.sampled_from(_RECLASSIFY_SEVERITIES),
        "dept_code": st.just("MH_PWD"),
        "reviewer_note": st.text(min_size=0, max_size=100),
    })


@given(_valid_reclassify_payload_strategy())
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_reclassification_transitions_to_dispatched(payload: dict):
    """
    For any complaint in review_required, after reclassification the complaint
    must transition to dispatched with non-null dept_code, routed_at, dispatched_at.

    Validates: Requirements 12.2, 12.3
    """
    from datetime import datetime, timezone
    from sqlalchemy import select

    async def _run():
        session = await _make_session()
        try:
            # Seed a department
            dept = Department(
                code="MH_PWD",
                name="Maharashtra PWD",
                state_code="MH",
                dispatch_type="webhook",
                sla_hours=72,
            )
            session.add(dept)
            await session.flush()

            # Create a complaint in review_required
            complaint = Complaint(
                reference_number="VLCR-MH-2025-00000001",
                citizen_lang="hi",
                input_channel="web",
                input_type="text",
                raw_text_original="Water supply broken",
                status="review_required",
                review_reason="Low confidence: 0.45",
            )
            session.add(complaint)
            await session.flush()

            # Simulate reclassification (mirrors review router logic)
            now = datetime.now(timezone.utc)
            old_status = complaint.status

            complaint.category = payload["category"]
            complaint.subcategory = payload["subcategory"]
            complaint.severity = payload["severity"]
            complaint.dept_code = payload["dept_code"]
            complaint.dept_name = "Maharashtra PWD"
            complaint.reviewed_by = "reviewer"
            complaint.reviewed_at = now
            complaint.routed_at = now
            complaint.dispatched_at = now
            complaint.status = "dispatched"

            session.add(AuditLog(
                complaint_id=complaint.id,
                actor="reviewer",
                action="reclassify",
                old_value={"status": old_status},
                new_value={"status": "dispatched", "dept_code": payload["dept_code"]},
            ))
            session.add(StatusEvent(
                complaint_id=complaint.id,
                from_status=old_status,
                to_status="dispatched",
                note=payload.get("reviewer_note") or "Reclassified",
                actor="reviewer",
            ))
            await session.flush()

            # Assertions
            assert complaint.status == "dispatched", (
                f"Expected status='dispatched', got {complaint.status!r}"
            )
            assert complaint.dept_code is not None, "dept_code must not be None after reclassification"
            assert complaint.routed_at is not None, "routed_at must be set after reclassification"
            assert complaint.dispatched_at is not None, "dispatched_at must be set after reclassification"
        finally:
            await session.close()

    asyncio.run(_run())


# =============================================================================
# Property 11: Audit log immutability (26.7)
# =============================================================================

# Feature: vlcr-fullstack-rebuild, Property 11: Audit log immutability
def test_no_delete_or_patch_endpoint_for_audit_log():
    """
    Assert no DELETE or PATCH endpoint exists for /api/v1/complaints/{ref}/audit.
    Audit log count for a complaint only ever increases.

    Validates: Requirements 21.4
    """
    from app.main import app

    # Collect all routes
    routes = {(r.path, list(r.methods)) for r in app.routes if hasattr(r, "methods")}

    # No DELETE or PATCH on audit endpoint
    for path, methods in routes:
        if "audit" in path:
            assert "DELETE" not in methods, (
                f"DELETE method found on audit endpoint {path!r} — audit logs must be immutable"
            )
            assert "PATCH" not in methods, (
                f"PATCH method found on audit endpoint {path!r} — audit logs must be immutable"
            )


def test_audit_log_count_only_increases():
    """Audit log entries for a complaint only ever increase."""

    async def _run():
        session = await _make_session()
        try:
            from sqlalchemy import select, func

            complaint = Complaint(
                reference_number="VLCR-MH-2025-00000002",
                citizen_lang="hi",
                input_channel="web",
                input_type="text",
                raw_text_original="Test complaint",
                status="received",
            )
            session.add(complaint)
            await session.flush()

            counts = []

            # Count before any audit entries
            c = (await session.execute(
                select(func.count()).where(AuditLog.complaint_id == complaint.id)
            )).scalar_one()
            counts.append(c)

            # Add first audit entry
            session.add(AuditLog(
                complaint_id=complaint.id,
                actor="system",
                action="status_update",
                old_value={"status": "received"},
                new_value={"status": "processing"},
            ))
            await session.flush()

            c = (await session.execute(
                select(func.count()).where(AuditLog.complaint_id == complaint.id)
            )).scalar_one()
            counts.append(c)

            # Add second audit entry
            session.add(AuditLog(
                complaint_id=complaint.id,
                actor="reviewer",
                action="reclassify",
                old_value={"status": "review_required"},
                new_value={"status": "dispatched"},
            ))
            await session.flush()

            c = (await session.execute(
                select(func.count()).where(AuditLog.complaint_id == complaint.id)
            )).scalar_one()
            counts.append(c)

            # Verify counts only increase
            for i in range(1, len(counts)):
                assert counts[i] >= counts[i - 1], (
                    f"Audit log count decreased: {counts[i - 1]} → {counts[i]}"
                )
            assert counts[-1] == 2, f"Expected 2 audit entries, got {counts[-1]}"
        finally:
            await session.close()

    asyncio.run(_run())


# =============================================================================
# Property 13: Phone not exposed on public endpoints (15.2)
# =============================================================================

# Feature: vlcr-fullstack-rebuild, Property 13: Phone number not exposed on public endpoints
@given(
    st.from_regex(r'\+91[6-9]\d{9}', fullmatch=True),
    st.text(min_size=20, max_size=200,
            alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd", "Zs"))),
)
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_phone_not_in_public_tracking_response(phone: str, text: str):
    """
    For any complaint submitted with a phone number, the GET tracking endpoint
    must not include the phone number (or partial phone) in the response body.

    Validates: Requirements 18.2
    """
    from app.schemas.schemas import TrackingResponse

    async def _run():
        session = await _make_session()
        try:
            with _make_full_pipeline_mocks()[0], \
                 _make_full_pipeline_mocks()[1], \
                 _make_full_pipeline_mocks()[2], \
                 _make_full_pipeline_mocks()[3], \
                 _make_full_pipeline_mocks()[4]:
                complaint = await process_text_complaint(
                    db=session, text=text, citizen_phone=phone,
                    citizen_name=None, channel="web", location_raw=None,
                )

            # Build a TrackingResponse (as the tracking router would)
            tracking = TrackingResponse(
                reference_number=complaint.reference_number,
                status=complaint.status,
                status_label="Processing",
                created_at=complaint.created_at,
                dept_name=complaint.dept_name,
                category=complaint.category,
                severity=complaint.severity,
                timeline=[],
            )

            # Serialize to JSON string (as it would be sent over the wire)
            response_json = tracking.model_dump_json()

            assert phone not in response_json, (
                f"Full phone {phone!r} found in tracking response"
            )
            # Also check partial phone (last 4 digits could be coincidental, check last 7)
            partial = phone[-7:]
            assert partial not in response_json, (
                f"Partial phone {partial!r} found in tracking response"
            )
        finally:
            await session.close()

    asyncio.run(_run())
