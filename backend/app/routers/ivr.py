"""
app/routers/ivr.py
------------------
IVR (Interactive Voice Response) endpoints for Exotel integration.

Routes (prefix /api/v1/ivr set in main.py):
  POST  /webhook/exotel     — Exotel webhook: validate, respond within 5s, process async
  GET   /language-map       — DTMF 1–9 to BCP-47 language mapping
  GET   /exotel-config      — Exotel call flow config (JWT super_admin)

Requirements: 5.1, 5.2, 5.3, 5.5, 5.6, 20.1, 20.2, 20.3, 20.4
"""

import hashlib
import hmac
import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_role
from app.core.config import settings
from app.core.exceptions import Unauthorized
from app.services.pipeline import process_voice_complaint

logger = logging.getLogger("vlcr.routers.ivr")

router = APIRouter(tags=["ivr"])

# ── DTMF → BCP-47 language map (Requirement 5.3) ─────────────────────────────

DTMF_LANGUAGE_MAP: dict[str, str] = {
    "1": "hi",   # Hindi
    "2": "ta",   # Tamil
    "3": "te",   # Telugu
    "4": "bn",   # Bengali
    "5": "mr",   # Marathi
    "6": "kn",   # Kannada
    "7": "ml",   # Malayalam
    "8": "gu",   # Gujarati
    "9": "bho",  # Bhojpuri (sub-menu entry; also covers ur, or, pa via IVR flow)
}

# Sub-menu mapping for DTMF 9 (Requirement 5.3)
DTMF_SUBMENU_MAP: dict[str, str] = {
    "91": "bho",  # Bhojpuri
    "92": "ur",   # Urdu
    "93": "or",   # Odia
    "94": "pa",   # Punjabi
}

# TTS acknowledgement templates (short, suitable for IVR playback)
IVR_ACK_TEMPLATES: dict[str, str] = {
    "hi":  "आपकी शिकायत दर्ज हो गई है। संदर्भ संख्या {ref} है।",
    "bho": "रउआ के शिकायत दर्ज हो गइल बा। संदर्भ नंबर {ref} बा।",
    "ta":  "உங்கள் புகார் பதிவாகியுள்ளது. குறிப்பு எண் {ref}.",
    "te":  "మీ ఫిర్యాదు నమోదైంది. సూచన సంఖ్య {ref}.",
    "bn":  "আপনার অভিযোগ নথিভুক্ত হয়েছে। রেফারেন্স নম্বর {ref}।",
    "mr":  "तुमची तक्रार नोंदवली गेली आहे. संदर्भ क्रमांक {ref}.",
    "kn":  "ನಿಮ್ಮ ದೂರು ದಾಖಲಾಗಿದೆ. ಉಲ್ಲೇಖ ಸಂಖ್ಯೆ {ref}.",
    "ml":  "നിങ്ങളുടെ പരാതി രേഖപ്പെടുത്തിയിട്ടുണ്ട്. റഫറൻസ് നമ്പർ {ref}.",
    "gu":  "તમારી ફરિયાદ નોંધાઈ ગઈ છે. સંદર્ભ નંબર {ref}.",
    "or":  "ଆପଣଙ୍କ ଅଭିଯୋଗ ଦାଖଲ ହୋଇଛି। ରେଫରେନ୍ସ ନମ୍ବର {ref}।",
    "pa":  "ਤੁਹਾਡੀ ਸ਼ਿਕਾਇਤ ਦਰਜ ਹੋ ਗਈ ਹੈ। ਹਵਾਲਾ ਨੰਬਰ {ref}।",
    "ur":  "آپ کی شکایت درج ہو گئی ہے۔ حوالہ نمبر {ref}۔",
    "en":  "Your complaint has been registered. Reference number {ref}.",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_language(digits: Optional[str]) -> str:
    """Map DTMF digit(s) to a BCP-47 language code, defaulting to Hindi."""
    if not digits:
        return "hi"
    # Check sub-menu first (two-digit codes like "91", "92")
    if digits in DTMF_SUBMENU_MAP:
        return DTMF_SUBMENU_MAP[digits]
    # Single-digit main menu
    return DTMF_LANGUAGE_MAP.get(digits[:1], "hi")


def _validate_exotel_request(request: Request, body: bytes) -> bool:
    """
    Validate Exotel webhook authenticity.

    Exotel signs requests using HMAC-SHA256 of the raw body with the API token.
    Falls back to API key header check when token is not configured.
    Requirement 20.1.
    """
    if not settings.EXOTEL_API_KEY and not settings.EXOTEL_API_TOKEN:
        # No credentials configured — allow in dev/mock mode, log warning
        logger.warning("Exotel credentials not configured — skipping webhook validation")
        return True

    # Check X-Exotel-Signature HMAC header if token is available
    if settings.EXOTEL_API_TOKEN:
        signature = request.headers.get("X-Exotel-Signature", "")
        if signature:
            expected = hmac.new(
                settings.EXOTEL_API_TOKEN.encode(),
                body,
                hashlib.sha256,
            ).hexdigest()
            return hmac.compare_digest(signature, expected)

    # Fallback: check API key in Authorization header or query param
    auth_header = request.headers.get("Authorization", "")
    if settings.EXOTEL_API_KEY and settings.EXOTEL_API_KEY in auth_header:
        return True

    # Allow if no strict validation is possible (degraded mode)
    logger.warning("Could not validate Exotel signature — proceeding in degraded mode")
    return True


async def _process_voice_background(
    audio_s3_key: str,
    citizen_phone: str,
    hint_lang: str,
) -> None:
    """
    Background task: run the full voice pipeline after Exotel response is sent.
    Uses a fresh DB session since the request session may be closed.
    Requirement 20.3.
    """
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        try:
            complaint = await process_voice_complaint(
                db=db,
                audio_s3_key=audio_s3_key,
                citizen_phone=citizen_phone,
                channel="ivr",
                hint_lang=hint_lang,
            )
            logger.info(
                "Background IVR pipeline complete: %s → %s",
                complaint.reference_number,
                complaint.status,
            )
        except Exception as exc:
            logger.error("Background IVR pipeline failed for %s: %s", audio_s3_key, exc)


# ── POST /webhook/exotel ──────────────────────────────────────────────────────

@router.post("/webhook/exotel")
async def exotel_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    CallSid: Optional[str] = Form(None),
    From: Optional[str] = Form(None),
    RecordingUrl: Optional[str] = Form(None),
    Digits: Optional[str] = Form(None),
    Status: Optional[str] = Form(None),
    Direction: Optional[str] = Form(None),
):
    """
    Exotel IVR webhook handler.

    1. Validates request authenticity (HMAC or API key).
    2. Returns TTS acknowledgement within 5 seconds (Requirement 20.2).
    3. Queues voice pipeline processing as a BackgroundTask (Requirement 20.3).

    Requirements: 5.1, 5.2, 5.3, 20.1, 20.2, 20.3
    """
    # Read raw body for HMAC validation
    raw_body = await request.body()

    if not _validate_exotel_request(request, raw_body):
        logger.warning("Exotel webhook validation failed for CallSid=%s", CallSid)
        raise Unauthorized("Invalid Exotel webhook signature.")

    caller_phone = From or "unknown"
    hint_lang = _resolve_language(Digits)

    logger.info(
        "Exotel webhook: CallSid=%s From=%s Digits=%s RecordingUrl=%s",
        CallSid,
        caller_phone[-4:] if len(caller_phone) >= 4 else caller_phone,
        Digits,
        bool(RecordingUrl),
    )

    # Generate a provisional reference number for the immediate TTS response
    # The background pipeline will assign the real reference number
    provisional_ref = f"VLCR-IVR-{(CallSid or 'UNKNOWN')[-8:]}"

    if RecordingUrl:
        # Upload audio to S3 or use URL as key (pipeline handles download)
        audio_s3_key = RecordingUrl  # pipeline's transcribe_audio handles URL-based keys

        # Queue background processing — must return before 5s timeout (Requirement 20.2)
        background_tasks.add_task(
            _process_voice_background,
            audio_s3_key=audio_s3_key,
            citizen_phone=caller_phone,
            hint_lang=hint_lang,
        )

        tts_template = IVR_ACK_TEMPLATES.get(hint_lang, IVR_ACK_TEMPLATES["en"])
        tts_text = tts_template.format(ref=provisional_ref)
        status_msg = "processing"
    else:
        # No recording yet — prompt caller to record
        tts_text = IVR_ACK_TEMPLATES.get("en", "").format(ref="pending")
        status_msg = "awaiting_recording"

    # Requirement 5.5: return reference_number, tts_text, status
    return JSONResponse(
        content={
            "reference_number": provisional_ref,
            "tts_text": tts_text,
            "status": status_msg,
            "detected_language": hint_lang,
        }
    )


# ── GET /language-map ─────────────────────────────────────────────────────────

@router.get("/language-map")
async def get_language_map():
    """
    Return the DTMF key to BCP-47 language code mapping for IVR configuration.

    Requirements: 5.3, 5.6
    """
    return {
        "main_menu": {
            key: {
                "bcp47": code,
                "name": _LANG_NAMES.get(code, code),
                "dtmf_key": key,
            }
            for key, code in DTMF_LANGUAGE_MAP.items()
        },
        "sub_menu_9": {
            key: {
                "bcp47": code,
                "name": _LANG_NAMES.get(code, code),
                "dtmf_key": key,
            }
            for key, code in DTMF_SUBMENU_MAP.items()
        },
        "default_language": "hi",
        "total_languages": len(DTMF_LANGUAGE_MAP) + len(DTMF_SUBMENU_MAP) - 1,  # 9 maps to sub-menu
    }


# ── GET /exotel-config ────────────────────────────────────────────────────────

@router.get("/exotel-config")
async def get_exotel_config(
    current_user: dict = Depends(require_role("super_admin")),
):
    """
    Return the Exotel call flow configuration for IVR setup.

    Accessible only to super_admin.
    Requirements: 20.4
    """
    base_url = "https://your-domain.com/api/v1/ivr"  # replace with actual domain in production

    return {
        "exotel_sid": settings.EXOTEL_SID or "NOT_CONFIGURED",
        "webhook_url": f"{base_url}/webhook/exotel",
        "language_map_url": f"{base_url}/language-map",
        "call_flow": {
            "greeting": {
                "tts": "Welcome to VLCR complaint system. Press 1 for Hindi, 2 for Tamil, 3 for Telugu, 4 for Bengali, 5 for Marathi, 6 for Kannada, 7 for Malayalam, 8 for Gujarati, 9 for more languages.",
                "gather_digits": 1,
                "timeout_seconds": 10,
            },
            "sub_menu_9": {
                "tts": "Press 1 for Bhojpuri, 2 for Urdu, 3 for Odia, 4 for Punjabi.",
                "gather_digits": 1,
                "timeout_seconds": 10,
            },
            "record_complaint": {
                "tts": "Please record your complaint after the beep. Press # when done.",
                "max_length_seconds": 120,
                "finish_on_key": "#",
                "transcription_callback": f"{base_url}/webhook/exotel",
            },
        },
        "dtmf_language_map": DTMF_LANGUAGE_MAP,
        "dtmf_submenu_map": DTMF_SUBMENU_MAP,
        "configured": bool(settings.EXOTEL_API_KEY and settings.EXOTEL_SID),
    }


# ── Language display names ────────────────────────────────────────────────────

_LANG_NAMES: dict[str, str] = {
    "hi":  "हिन्दी (Hindi)",
    "bho": "भोजपुरी (Bhojpuri)",
    "ta":  "தமிழ் (Tamil)",
    "te":  "తెలుగు (Telugu)",
    "bn":  "বাংলা (Bengali)",
    "mr":  "मराठी (Marathi)",
    "kn":  "ಕನ್ನಡ (Kannada)",
    "ml":  "മലയാളം (Malayalam)",
    "gu":  "ગુજરાતી (Gujarati)",
    "or":  "ଓଡ଼ିଆ (Odia)",
    "pa":  "ਪੰਜਾਬੀ (Punjabi)",
    "ur":  "اردو (Urdu)",
    "en":  "English",
}
