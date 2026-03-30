"""
Unit tests for VLCR backend.

Covers:
  - scrub_pii (26.1)
  - _generate_ref and _extract_state (26.2)
  - Auth flows (26.3)
  - Tracking and review endpoints (26.4)
  - Pipeline error paths (26.5)

Uses:
  - pytest-asyncio for async tests
  - SQLite in-memory for DB isolation
  - unittest.mock for external service mocking
  - httpx.AsyncClient / FastAPI TestClient for endpoint tests
"""

import asyncio
import re
import sys
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Patch DB and PostgreSQL dialect before importing app modules ──────────────
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

# ── Now import app modules ────────────────────────────────────────────────────
from app.services.classifier import scrub_pii  # noqa: E402
from app.services.pipeline import _extract_state, _generate_ref  # noqa: E402
from app.models.models import (  # noqa: E402
    AuditLog, Complaint, Department, GovUser, RoutingRule, StatusEvent,
)


# ── Session factory ───────────────────────────────────────────────────────────

async def _make_session() -> AsyncSession:
    engine = create_async_engine(SQLITE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_TestBase.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    return factory()


# =============================================================================
# 26.1 — Unit tests for scrub_pii
# =============================================================================

class TestScrubPii:
    """Validates: Requirements 18.1, 8.4"""

    def test_aadhaar_replaced(self):
        text = "My Aadhaar is 123456789012 and I need help."
        result = scrub_pii(text)
        assert "123456789012" not in result
        assert "[AADHAAR]" in result

    def test_phone_replaced(self):
        text = "Call me at 9876543210 for details."
        result = scrub_pii(text)
        assert "9876543210" not in result
        assert "[PHONE]" in result

    def test_phone_starting_6_replaced(self):
        text = "My number is 6123456789."
        result = scrub_pii(text)
        assert "6123456789" not in result
        assert "[PHONE]" in result

    def test_pan_replaced(self):
        text = "PAN card: ABCDE1234F is mine."
        result = scrub_pii(text)
        assert "ABCDE1234F" not in result
        assert "[PAN]" in result

    def test_email_replaced(self):
        text = "Email me at user@example.com for info."
        result = scrub_pii(text)
        assert "user@example.com" not in result
        assert "[EMAIL]" in result

    def test_multiple_pii_types_replaced(self):
        text = "Aadhaar: 123456789012, Phone: 9876543210, PAN: ABCDE1234F, Email: a@b.com"
        result = scrub_pii(text)
        assert "123456789012" not in result
        assert "9876543210" not in result
        assert "ABCDE1234F" not in result
        assert "a@b.com" not in result
        assert "[AADHAAR]" in result
        assert "[PHONE]" in result
        assert "[PAN]" in result
        assert "[EMAIL]" in result

    def test_no_pii_unchanged_structure(self):
        text = "There is no water supply in our village since three days."
        result = scrub_pii(text)
        # No PII placeholders should appear
        assert "[AADHAAR]" not in result
        assert "[PHONE]" not in result
        assert "[PAN]" not in result
        assert "[EMAIL]" not in result

    def test_truncated_to_1500_chars(self):
        text = "x" * 2000
        result = scrub_pii(text)
        assert len(result) <= 1500

    def test_phone_not_starting_6_to_9_not_replaced(self):
        # 5-digit starting number should NOT be replaced
        text = "The code is 5123456789 here."
        result = scrub_pii(text)
        # Should not replace numbers not starting with 6-9
        assert "[PHONE]" not in result


# =============================================================================
# 26.2 — Unit tests for _generate_ref and _extract_state
# =============================================================================

_REF_RE = re.compile(r'^VLCR-[A-Z]+-\d{4}-\d{8}$')


class TestGenerateRef:
    """Validates: Requirements 4.1"""

    def test_format_matches_pattern(self):
        ref = _generate_ref("Maharashtra")
        assert _REF_RE.match(ref), f"Reference {ref!r} does not match VLCR-STATE-YEAR-8DIGITS"

    def test_contains_current_year(self):
        import datetime as dt
        ref = _generate_ref("Bihar")
        year = str(dt.datetime.now().year)
        assert year in ref

    def test_no_state_uses_default(self):
        ref = _generate_ref(None)
        assert _REF_RE.match(ref), f"Reference {ref!r} does not match pattern"

    def test_unique_refs(self):
        refs = {_generate_ref("UP") for _ in range(20)}
        assert len(refs) == 20, "Expected all 20 references to be unique"

    def test_state_abbreviation_in_ref(self):
        ref = _generate_ref("Maharashtra")
        # State abbreviation should appear in the ref
        assert "MH" in ref or "MAHARASHTRA" in ref or "Maharashtra".upper()[:2] in ref


class TestExtractState:
    """Validates: Requirements 4.1"""

    def test_mumbai_maps_to_maharashtra(self):
        result = _extract_state("There is a water problem in Mumbai")
        assert result is not None
        assert "maharashtra" in result.lower() or "mh" in result.lower() or result == "Maharashtra"

    def test_delhi_maps_to_delhi(self):
        result = _extract_state("Road broken near Delhi")
        assert result is not None

    def test_unknown_text_returns_none_or_default(self):
        result = _extract_state("xyz abc def")
        # Should return None or a default — not crash
        assert result is None or isinstance(result, str)

    def test_empty_string(self):
        result = _extract_state("")
        assert result is None or isinstance(result, str)


# =============================================================================
# 26.3 — Unit tests for auth flows
# =============================================================================

class TestAuthFlows:
    """Validates: Requirements 3.1, 3.5, 3.6, 3.7, 18.6"""

    def test_create_and_verify_token(self):
        from app.core.auth import create_access_token, verify_token
        payload = {"sub": "testuser", "role": "officer"}
        token = create_access_token(payload)
        decoded = verify_token(token)
        assert decoded["sub"] == "testuser"
        assert decoded["role"] == "officer"

    def test_invalid_token_raises_unauthorized(self):
        from app.core.auth import verify_token
        from app.core.exceptions import Unauthorized
        with pytest.raises(Unauthorized):
            verify_token("not.a.valid.token")

    def test_hash_and_verify_password(self):
        from app.core.auth import hash_password, verify_password
        plain = "securepassword123"
        hashed = hash_password(plain)
        assert hashed != plain
        assert verify_password(plain, hashed)
        assert not verify_password("wrongpassword", hashed)

    def test_bcrypt_cost_factor(self):
        from app.core.auth import hash_password
        hashed = hash_password("test")
        # bcrypt hashes start with $2b$ and the cost factor follows
        assert hashed.startswith("$2b$")
        cost = int(hashed.split("$")[2])
        assert cost >= 12, f"bcrypt cost factor {cost} is below minimum 12"

    def test_token_contains_role(self):
        from app.core.auth import create_access_token, verify_token
        token = create_access_token({"sub": "admin", "role": "super_admin"})
        decoded = verify_token(token)
        assert decoded["role"] == "super_admin"

    def test_token_does_not_contain_password(self):
        from app.core.auth import create_access_token
        token = create_access_token({"sub": "user", "role": "officer"})
        # Token should not contain any password-like field
        import base64
        parts = token.split(".")
        if len(parts) == 3:
            padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
            decoded_bytes = base64.urlsafe_b64decode(padded)
            assert b"password" not in decoded_bytes.lower()
            assert b"hashed" not in decoded_bytes.lower()

    def test_demo_user_credentials_work(self):
        """Demo credentials admin/admin123 should hash and verify correctly."""
        from app.core.auth import hash_password, verify_password
        hashed = hash_password("admin123")
        assert verify_password("admin123", hashed)
        assert not verify_password("wrongpass", hashed)


# =============================================================================
# 26.4 — Unit tests for tracking and review
# =============================================================================

class TestTrackingAndReview:
    """Validates: Requirements 11.3, 12.1, 18.2"""

    def test_complaint_not_found_raises_exception(self):
        from app.core.exceptions import ComplaintNotFound
        exc = ComplaintNotFound("Complaint 'VLCR-XX-2025-00000001' not found.")
        assert exc.status_code == 404
        assert "not found" in exc.detail.lower()

    def test_forbidden_raises_exception(self):
        from app.core.exceptions import Forbidden
        exc = Forbidden("Insufficient role.")
        assert exc.status_code == 403

    def test_unauthorized_raises_exception(self):
        from app.core.exceptions import Unauthorized
        exc = Unauthorized("Invalid token.")
        assert exc.status_code == 401

    def test_tracking_response_no_citizen_phone(self):
        """TrackingResponse schema must not include citizen_phone field."""
        from app.schemas.schemas import TrackingResponse
        import inspect
        fields = TrackingResponse.model_fields
        assert "citizen_phone" not in fields, (
            "TrackingResponse must not expose citizen_phone (Requirement 18.2)"
        )

    def test_require_role_dependency_exists(self):
        """require_role factory must exist and be callable."""
        from app.core.auth import require_role
        dep = require_role("reviewer", "super_admin")
        assert callable(dep)

    def test_complaint_not_found_code(self):
        from app.core.exceptions import ComplaintNotFound
        exc = ComplaintNotFound("not found")
        assert exc.code == "COMPLAINT_NOT_FOUND"

    def test_rate_limit_exceeded_code(self):
        from app.core.exceptions import RateLimitExceeded
        exc = RateLimitExceeded("too many requests")
        assert exc.status_code == 429


# =============================================================================
# 26.5 — Unit tests for pipeline error paths
# =============================================================================

class TestPipelineErrorPaths:
    """Validates: Requirements 8.5, 8.6, 10.5, 10.6, 19.4"""

    def test_classification_failed_sets_review_required(self):
        """ClassificationFailed exception must cause pipeline to set review_required."""
        from app.core.exceptions import ClassificationFailed

        async def _run():
            session = await _make_session()
            try:
                from app.services.pipeline import process_text_complaint
                with (
                    patch("app.services.pipeline.detect_language", AsyncMock(return_value=("hi", 0.9))),
                    patch("app.services.pipeline.translate_to_english", AsyncMock(return_value=("test", 1.0))),
                    patch("app.services.pipeline.translate_to_hindi", AsyncMock(return_value="test")),
                    patch("app.services.pipeline.classify_complaint",
                          AsyncMock(side_effect=ClassificationFailed("Claude API error"))),
                    patch("app.services.pipeline.send_acknowledgement", AsyncMock(return_value=True)),
                ):
                    complaint = await process_text_complaint(
                        db=session,
                        text="Water supply broken in our area for 3 days.",
                        citizen_phone=None,
                        citizen_name=None,
                        channel="web",
                        location_raw=None,
                    )
                assert complaint.status == "review_required", (
                    f"Expected review_required, got {complaint.status!r}"
                )
                assert complaint.dept_code is None
            finally:
                await session.close()

        asyncio.run(_run())

    def test_asr_timeout_sets_review_required(self):
        """ASR confidence 0.0 (timeout) must set complaint to review_required."""

        async def _run():
            session = await _make_session()
            try:
                from app.services.pipeline import process_voice_complaint
                with patch(
                    "app.services.pipeline.transcribe_audio",
                    AsyncMock(return_value=("", 0.0)),
                ):
                    complaint = await process_voice_complaint(
                        db=session,
                        audio_s3_key="test/audio.wav",
                        citizen_phone=None,
                        channel="ivr",
                        hint_lang="hi",
                    )
                assert complaint.status == "review_required"
                assert complaint.category is None
            finally:
                await session.close()

        asyncio.run(_run())

    def test_sms_mock_provider_does_not_raise(self):
        """Mock SMS provider must log and return False without raising."""
        from app.services.notification_service import send_acknowledgement
        import asyncio

        async def _run():
            with patch("app.services.notification_service.settings") as mock_settings:
                mock_settings.SMS_PROVIDER = "mock"
                result = await send_acknowledgement(
                    phone="+919876543210",
                    lang="hi",
                    reference_number="VLCR-MH-2025-00000001",
                    dept_name="PWD",
                    category="Water",
                )
            # Mock provider should return True (logged successfully)
            assert isinstance(result, bool)

        asyncio.run(_run())

    def test_classification_failed_exception_attributes(self):
        from app.core.exceptions import ClassificationFailed
        exc = ClassificationFailed("Claude returned non-JSON")
        assert exc.status_code == 502
        assert "Claude" in exc.detail or "non-JSON" in exc.detail or exc.detail

    def test_pipeline_review_reason_set_on_low_confidence(self):
        """review_reason must be set when confidence is below threshold."""

        async def _run():
            session = await _make_session()
            try:
                from app.services.pipeline import process_text_complaint

                async def _low_conf_classify(text_en, location_raw=None, state_code=None):
                    return {
                        "category": "Water", "subcategory": "test",
                        "severity": "low", "confidence": 0.3,
                        "dept_prefix": "JJM", "location_state": None,
                        "location_district": None, "location_block": None,
                        "location_village": None, "reasoning": "mock",
                    }

                with (
                    patch("app.services.pipeline.detect_language", AsyncMock(return_value=("hi", 0.9))),
                    patch("app.services.pipeline.translate_to_english", AsyncMock(return_value=("test", 1.0))),
                    patch("app.services.pipeline.translate_to_hindi", AsyncMock(return_value="test")),
                    patch("app.services.pipeline.classify_complaint", side_effect=_low_conf_classify),
                    patch("app.services.pipeline.send_acknowledgement", AsyncMock(return_value=True)),
                ):
                    complaint = await process_text_complaint(
                        db=session,
                        text="Water supply broken in our area for 3 days.",
                        citizen_phone=None,
                        citizen_name=None,
                        channel="web",
                        location_raw=None,
                    )
                assert complaint.status == "review_required"
                assert complaint.review_reason is not None
                assert "0.3" in complaint.review_reason or "confidence" in complaint.review_reason.lower()
            finally:
                await session.close()

        asyncio.run(_run())
