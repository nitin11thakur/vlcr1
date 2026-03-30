"""
app/services/pipeline.py
------------------------
VLCR Complaint Processing Pipeline — 8-step orchestrator.

Steps: ingest → lang-detect → translate (EN + HI) → classify → route →
       dispatch → notify → track

Requirements: 4.2, 5.2, 5.4, 8.5, 9.1, 9.2, 9.3, 9.5, 9.6, 19.4, 21.1
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ClassificationFailed
from app.core.redis_client import cache_get, cache_set
from app.models.models import (
    AuditLog,
    Complaint,
    Department,
    RoutingRule,
    StatusEvent,
)
from app.services.classifier import (
    classify_complaint,
    resolve_dept_code,
    resolve_dept_name,
)
from app.services.nlp_service import (
    detect_language,
    transcribe_audio,
    translate_to_english,
    translate_to_hindi,
)
from app.services.notification_service import send_acknowledgement

logger = logging.getLogger("vlcr.pipeline")

# ── State keyword mapping ─────────────────────────────────────────────────────

STATE_KEYWORDS: dict[str, list[str]] = {
    "Bihar": ["bihar", "patna", "gaya", "bhojpur", "muzaffarpur"],
    "Maharashtra": ["maharashtra", "mumbai", "pune", "nagpur", "nashik"],
    "Tamil Nadu": ["tamil", "chennai", "coimbatore", "madurai"],
    "Uttar Pradesh": ["uttar pradesh", "lucknow", "kanpur", "varanasi", "allahabad"],
    "West Bengal": ["west bengal", "kolkata", "howrah"],
    "Karnataka": ["karnataka", "bengaluru", "bangalore", "mysuru"],
    "Gujarat": ["gujarat", "ahmedabad", "surat", "vadodara"],
    "Rajasthan": ["rajasthan", "jaipur", "jodhpur", "udaipur"],
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _generate_ref(state: Optional[str] = None) -> str:
    """
    Generate a reference number like VLCR-MH-2026-a3f8c201.

    FIX: Uses uuid.uuid4().hex[:8] instead of str(uuid.uuid4().int)[:8].
    The .int representation is a 39-digit decimal whose leading digits are
    heavily biased toward low values, producing non-uniform identifiers.
    Hex is uniform across the full UUID entropy space.
    """
    state_code = (state or "IN").upper()[:2]
    year = datetime.now().year
    uid = uuid.uuid4().hex[:8]
    return f"VLCR-{state_code}-{year}-{uid}"


def _extract_state(text: str) -> Optional[str]:
    """Return the Indian state name detected in *text*, or None."""
    t = text.lower()
    for state, keywords in STATE_KEYWORDS.items():
        if any(kw in t for kw in keywords):
            return state
    return None


async def _lookup_dept_name(db: AsyncSession, dept_code: str, prefix: str) -> str:
    """Return the department name from DB, falling back to the prefix lookup."""
    result = await db.execute(
        select(Department).where(Department.code == dept_code)
    )
    dept = result.scalar_one_or_none()
    if dept:
        return dept.name
    return resolve_dept_name(prefix)


async def _find_routing_rule(
    db: AsyncSession,
    state: Optional[str],
    category: Optional[str],
) -> Optional[RoutingRule]:
    """
    Return the highest-priority active RoutingRule for (state, category).

    Results are cached in Redis under ``routing:{state}:{category}`` (TTL 300s)
    to reduce DB load (Requirement 9.4).  The cache stores the rule's primary
    key; on a cache hit we re-fetch the ORM object so callers always receive a
    proper SQLAlchemy instance.
    """
    if not state or not category:
        return None

    cache_key = f"routing:{state}:{category}"
    cached = await cache_get(cache_key)
    if cached is not None:
        # cached value is the rule id (str) or the sentinel "NONE"
        if cached == "NONE":
            return None
        result = await db.execute(
            select(RoutingRule).where(RoutingRule.id == cached)
        )
        rule = result.scalar_one_or_none()
        if rule is not None:
            return rule
        # Cache stale — fall through to DB query

    result = await db.execute(
        select(RoutingRule)
        .where(
            RoutingRule.state_code == state,
            RoutingRule.category == category,
            RoutingRule.is_active == True,  # noqa: E712
        )
        .order_by(RoutingRule.priority)
        .limit(1)
    )
    rule = result.scalar_one_or_none()
    await cache_set(cache_key, str(rule.id) if rule else "NONE", ttl=300)
    return rule


async def _add_status_event(
    db: AsyncSession,
    complaint_id,
    from_s: str,
    to_s: str,
    note: str = "",
) -> None:
    """Append a StatusEvent record for a status transition (Requirement 21.1)."""
    db.add(
        StatusEvent(
            complaint_id=complaint_id,
            from_status=from_s,
            to_status=to_s,
            note=note,
            actor="system",
        )
    )


async def _add_audit_log(
    db: AsyncSession,
    complaint_id,
    action: str,
    new_value: Optional[dict] = None,
    old_value: Optional[dict] = None,
) -> None:
    """Append an AuditLog record (Requirement 21.1)."""
    db.add(
        AuditLog(
            complaint_id=complaint_id,
            actor="system",
            action=action,
            old_value=old_value,
            new_value=new_value,
        )
    )


# ── Main pipeline entry points ────────────────────────────────────────────────

async def process_text_complaint(
    db: AsyncSession,
    *,
    text: str,
    channel: str,
    citizen_phone: Optional[str] = None,
    citizen_name: Optional[str] = None,
    location_raw: Optional[str] = None,
) -> Complaint:
    """
    Run the full 8-step pipeline for a text-based complaint.

    Steps:
      1. Ingest — create DB record in 'received' state
      2. Language detect
      3. Translate → English (for classifier)
      4. Translate → Hindi (for officer display)
      5. Classify via Claude (or mock)
      6. Route — find matching RoutingRule
      7. Dispatch — send to dept webhook/email/cpgrams
      8. Notify — send SMS acknowledgement
    """
    # ── Step 1: Ingest ────────────────────────────────────────────────────────
    state_hint = _extract_state(text + " " + (location_raw or ""))
    reference_number = _generate_ref(state_hint)

    complaint = Complaint(
        reference_number=reference_number,
        citizen_phone=citizen_phone,
        citizen_name=citizen_name,
        input_channel=channel,
        input_type="text",
        raw_text_original=text,
        status="received",
    )
    db.add(complaint)
    await db.flush()  # get complaint.id without committing

    await _add_status_event(db, complaint.id, "", "received", "Complaint received via text")
    await _add_audit_log(db, complaint.id, "complaint_received", {"channel": channel})

    # ── Step 2: Language detect ───────────────────────────────────────────────
    complaint.status = "processing"
    await _add_status_event(db, complaint.id, "received", "processing")

    try:
        lang_code, lang_conf = await detect_language(text)
    except Exception as exc:
        logger.warning("Language detection failed (%s) — defaulting to 'hi'", exc)
        lang_code, lang_conf = "hi", 0.0

    complaint.citizen_lang = lang_code
    complaint.lang_detect_code = lang_code
    complaint.nlp_confidence = lang_conf

    # ── Step 3: Translate → English ───────────────────────────────────────────
    try:
        text_en, _ = await translate_to_english(text, src_lang=lang_code)
    except Exception as exc:
        logger.warning("Translation to English failed (%s) — using raw text", exc)
        text_en = text

    complaint.translation_en = text_en

    # ── Step 4: Translate → Hindi (for officer display) ───────────────────────
    if lang_code != "hi":
        try:
            text_hi, _ = await translate_to_hindi(text, src_lang=lang_code)
            complaint.translation_hi = text_hi
        except Exception as exc:
            logger.warning("Translation to Hindi failed (%s) — skipping", exc)

    # ── Step 5: Classify ──────────────────────────────────────────────────────
    try:
        clf = await classify_complaint(
            complaint_text_en=text_en,
            location_raw=location_raw,
            state_code=state_hint,
        )
    except ClassificationFailed as exc:
        logger.error("Classification failed: %s — marking for review", exc)
        complaint.status = "review_required"
        complaint.review_reason = f"Classification error: {exc}"
        await _add_status_event(db, complaint.id, "processing", "review_required", str(exc))
        await db.flush()
        return complaint

    complaint.status = "classified"
    complaint.category = clf.get("category")
    complaint.subcategory = clf.get("subcategory")
    complaint.severity = clf.get("severity")
    complaint.classifier_conf = clf.get("confidence")
    complaint.location_state = clf.get("location_state") or state_hint
    complaint.location_district = clf.get("location_district")
    complaint.location_block = clf.get("location_block")
    complaint.location_village = clf.get("location_village")
    complaint.location_raw = location_raw
    complaint.classifier_raw = clf

    await _add_status_event(db, complaint.id, "processing", "classified")
    await _add_audit_log(
        db, complaint.id, "complaint_classified",
        {"category": complaint.category, "severity": complaint.severity, "confidence": complaint.classifier_conf},
    )

    # Flag low-confidence for human review (Requirement 8.5)
    if complaint.classifier_conf is not None and complaint.classifier_conf < settings.MIN_CLASSIFIER_CONFIDENCE:
        complaint.status = "review_required"
        complaint.review_reason = f"Low classifier confidence: {complaint.classifier_conf:.2f}"
        await _add_status_event(db, complaint.id, "classified", "review_required", complaint.review_reason)
        await db.flush()
        return complaint

    # ── Step 6: Route ─────────────────────────────────────────────────────────
    dept_prefix = clf.get("dept_prefix", "MUNI")
    dept_code = resolve_dept_code(
        state_code=complaint.location_state or "IN",
        dept_prefix=dept_prefix,
    )
    dept_name = await _lookup_dept_name(db, dept_code, dept_prefix)

    complaint.dept_code = dept_code
    complaint.dept_name = dept_name
    complaint.status = "routed"
    complaint.routed_at = datetime.now(timezone.utc)

    # Try to find a matching routing rule for richer routing metadata
    rule = await _find_routing_rule(db, complaint.location_state, complaint.category)
    if rule:
        complaint.routing_rule_id = rule.id

    await _add_status_event(db, complaint.id, "classified", "routed", f"Routed to {dept_name}")
    await _add_audit_log(db, complaint.id, "complaint_routed", {"dept_code": dept_code, "dept_name": dept_name})

    # ── Step 7: Dispatch ──────────────────────────────────────────────────────
    # Determine dispatch method from department record (default: mock/log)
    dept_result = await db.execute(select(Department).where(Department.code == dept_code))
    dept_obj = dept_result.scalar_one_or_none()

    dispatch_method = dept_obj.dispatch_type if dept_obj else "mock"
    complaint.dispatch_method = dispatch_method
    complaint.dispatched_at = datetime.now(timezone.utc)
    complaint.status = "dispatched"

    # Actual dispatch calls (webhook/email/cpgrams) would be implemented here.
    # For now, log the dispatch event.
    logger.info(
        "Dispatched complaint %s to %s via %s",
        reference_number, dept_name, dispatch_method,
    )

    dispatch_ref = f"DISPATCH-{uuid.uuid4().hex[:8].upper()}"
    complaint.dispatch_ref = dispatch_ref

    await _add_status_event(db, complaint.id, "routed", "dispatched", f"Dispatched via {dispatch_method}")
    await _add_audit_log(
        db, complaint.id, "complaint_dispatched",
        {"method": dispatch_method, "dispatch_ref": dispatch_ref},
    )

    # ── Step 8: Notify ────────────────────────────────────────────────────────
    if citizen_phone:
        sla_hours = dept_obj.sla_hours if dept_obj else 72
        try:
            await send_acknowledgement(
                phone=citizen_phone,
                reference_number=reference_number,
                dept_name=dept_name,
                category=complaint.category or "General",
                sla_hours=sla_hours,
            )
        except Exception as exc:
            # SMS failure must never block the pipeline
            logger.warning("Acknowledgement SMS failed for %s: %s", reference_number, exc)

    await db.flush()
    return complaint


async def process_voice_complaint(
    db: AsyncSession,
    *,
    audio_s3_key: str,
    channel: str,
    citizen_phone: Optional[str] = None,
    hint_lang: Optional[str] = None,
) -> Complaint:
    """
    Run the full pipeline for a voice-based complaint.

    Adds an ASR transcription step before the standard text pipeline.
    """
    # ── Step 1: Ingest ────────────────────────────────────────────────────────
    reference_number = _generate_ref()
    complaint = Complaint(
        reference_number=reference_number,
        citizen_phone=citizen_phone,
        input_channel=channel,
        input_type="voice",
        raw_audio_s3_key=audio_s3_key,
        status="received",
    )
    db.add(complaint)
    await db.flush()

    await _add_status_event(db, complaint.id, "", "received", "Complaint received via voice")

    # ── ASR: Transcribe audio ─────────────────────────────────────────────────
    complaint.status = "processing"
    await _add_status_event(db, complaint.id, "received", "processing")

    try:
        transcript, asr_conf, detected_lang = await transcribe_audio(
            audio_s3_key=audio_s3_key,
            hint_lang=hint_lang,
        )
    except Exception as exc:
        logger.error("ASR transcription failed for %s: %s", reference_number, exc)
        complaint.status = "review_required"
        complaint.review_reason = f"ASR transcription failed: {exc}"
        await _add_status_event(db, complaint.id, "processing", "review_required", str(exc))
        await db.flush()
        return complaint

    complaint.transcript_raw = transcript
    complaint.transcript_norm = transcript
    complaint.asr_confidence = asr_conf
    complaint.citizen_lang = detected_lang or hint_lang or "hi"
    complaint.lang_detect_code = complaint.citizen_lang

    # Flag low-ASR confidence for review (Requirement 5.4)
    if asr_conf < settings.MIN_ASR_CONFIDENCE:
        complaint.status = "review_required"
        complaint.review_reason = f"Low ASR confidence: {asr_conf:.2f}"
        await _add_status_event(db, complaint.id, "processing", "review_required", complaint.review_reason)
        await db.flush()
        return complaint

    # Continue with text pipeline from the transcript
    return await process_text_complaint(
        db,
        text=transcript,
        channel=channel,
        citizen_phone=citizen_phone,
        location_raw=None,
    )
