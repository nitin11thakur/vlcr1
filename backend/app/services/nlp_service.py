"""
app/services/nlp_service.py
---------------------------
Language processing service: language detection (Bhashini LangID),
translation (Bhashini pipeline), and ASR (Bhashini).

Tasks 8.1 / 8.2 / 8.3 each add a section to this file.
"""

import logging
from typing import Optional, Tuple

import httpx

from app.core.config import settings

logger = logging.getLogger("vlcr.nlp")

# ── Constants ─────────────────────────────────────────────────────────────────

DTMF_LANG_MAP: dict[int, Optional[str]] = {
    1: "hi",   # Hindi
    2: "ta",   # Tamil
    3: "te",   # Telugu
    4: "bn",   # Bengali
    5: "mr",   # Marathi
    6: "kn",   # Kannada
    7: "ml",   # Malayalam
    8: "gu",   # Gujarati
    9: None,   # Sub-menu (bho / ur / or / pa)
}

LANG_NAMES: dict[str, str] = {
    "hi":  "हिंदी (Hindi)",
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

# Bhashini inference pipeline endpoint
_BHASHINI_PIPELINE_URL = "https://dhruva-api.bhashini.gov.in/services/inference/pipeline"


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_lang_name(bcp47: str) -> str:
    """Return the display name for a BCP-47 language code."""
    return LANG_NAMES.get(bcp47, bcp47)


def dtmf_to_lang(key: int) -> Optional[str]:
    """Map a DTMF digit (1–9) to a BCP-47 language code, or None for sub-menu."""
    return DTMF_LANG_MAP.get(key)


# ── Unicode-range heuristic ───────────────────────────────────────────────────

def _detect_language_heuristic(text: str) -> Tuple[str, float]:
    """
    Detect language from Unicode script ranges.

    Returns (bcp47_code, confidence=0.6) — lower confidence than API.
    Defaults to "hi" when no script is recognised.

    Validates: Requirements 6.3
    """
    # Count characters per script block
    counts: dict[str, int] = {
        "hi": 0,   # Devanagari — also covers mr / bho
        "ta": 0,
        "te": 0,
        "bn": 0,
        "kn": 0,
        "ml": 0,
        "gu": 0,
        "pa": 0,   # Gurmukhi
        "or": 0,
        "ur": 0,   # Arabic script
    }

    for ch in text:
        cp = ord(ch)
        if 0x0900 <= cp <= 0x097F:   # Devanagari
            counts["hi"] += 1
        elif 0x0B80 <= cp <= 0x0BFF: # Tamil
            counts["ta"] += 1
        elif 0x0C00 <= cp <= 0x0C7F: # Telugu
            counts["te"] += 1
        elif 0x0980 <= cp <= 0x09FF: # Bengali
            counts["bn"] += 1
        elif 0x0C80 <= cp <= 0x0CFF: # Kannada
            counts["kn"] += 1
        elif 0x0D00 <= cp <= 0x0D7F: # Malayalam
            counts["ml"] += 1
        elif 0x0A80 <= cp <= 0x0AFF: # Gujarati
            counts["gu"] += 1
        elif 0x0A00 <= cp <= 0x0A7F: # Gurmukhi (Punjabi)
            counts["pa"] += 1
        elif 0x0B00 <= cp <= 0x0B7F: # Odia
            counts["or"] += 1
        elif 0x0600 <= cp <= 0x06FF: # Arabic (Urdu)
            counts["ur"] += 1

    best_lang = max(counts, key=lambda k: counts[k])
    if counts[best_lang] == 0:
        return "hi", 0.5   # default — no script detected

    return best_lang, 0.6


# ── Language Detection ────────────────────────────────────────────────────────

async def detect_language(text: str) -> Tuple[str, float]:
    """
    Detect the language of *text* using the Bhashini LangID API.

    Returns (bcp47_code, confidence) where confidence is in [0.0, 1.0].

    Falls back to a Unicode-range heuristic when the API is unavailable,
    logging a WARNING as required by Requirement 6.3.

    Validates: Requirements 6.1, 6.2, 6.3
    """
    if settings.BHASHINI_API_KEY and settings.BHASHINI_USER_ID:
        try:
            payload = {
                "pipelineTasks": [
                    {"taskType": "langid", "config": {}}
                ],
                "inputData": {
                    "input": [{"source": text}]
                },
            }
            headers = {
                "userID": settings.BHASHINI_USER_ID,
                "ulcaApiKey": settings.BHASHINI_API_KEY,
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    _BHASHINI_PIPELINE_URL,
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()

            prediction = (
                data["pipelineResponse"][0]["output"][0]["langPrediction"][0]
            )
            lang_code: str = prediction["langCode"]
            confidence: float = float(prediction["langScore"])
            logger.info("Bhashini LangID: %s (conf=%.3f)", lang_code, confidence)
            return lang_code, confidence

        except Exception as exc:
            logger.warning(
                "Bhashini LangID unavailable (%s) — falling back to Unicode heuristic",
                exc,
            )
    else:
        logger.warning(
            "Bhashini credentials not configured — falling back to Unicode heuristic"
        )

    lang_code, confidence = _detect_language_heuristic(text)
    logger.info("Heuristic LangID: %s (conf=%.3f)", lang_code, confidence)
    return lang_code, confidence


# ── Translation ───────────────────────────────────────────────────────────────

import hashlib

from app.core.redis_client import cache_get, cache_set


def _translation_cache_key(text: str, source_lang: str, target_lang: str) -> str:
    """Build a Redis cache key for a translation request."""
    digest = hashlib.sha256(f"{text}{source_lang}{target_lang}".encode()).hexdigest()
    return f"translate:{digest}"


async def _call_bhashini_translate(text: str, source_lang: str, target_lang: str) -> str:
    """
    Call the Bhashini translation pipeline and return the translated text.
    Raises on any HTTP or parsing error.
    """
    payload = {
        "pipelineTasks": [
            {
                "taskType": "translation",
                "config": {
                    "language": {
                        "sourceLanguage": source_lang,
                        "targetLanguage": target_lang,
                    }
                },
            }
        ],
        "inputData": {"input": [{"source": text}]},
    }
    headers = {
        "userID": settings.BHASHINI_USER_ID,
        "ulcaApiKey": settings.BHASHINI_API_KEY,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=25.0) as client:
        resp = await client.post(_BHASHINI_PIPELINE_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    return data["pipelineResponse"][0]["output"][0]["target"]


async def translate_to_english(text: str, source_lang: str) -> Tuple[str, float]:
    """
    Translate *text* from *source_lang* to English.

    Returns (translated_text, confidence).

    Short-circuits when source is already ``en`` (returns original, conf=1.0).
    Results are cached in Redis for 3600 s.
    On API error, returns (original_text, 0.0) and logs the error.

    Validates: Requirements 7.1, 7.3, 7.5, 7.6
    """
    if source_lang == "en":
        return text, 1.0

    cache_key = _translation_cache_key(text, source_lang, "en")
    cached = await cache_get(cache_key)
    if cached is not None:
        # Stored as [translated_text, confidence]
        if isinstance(cached, list) and len(cached) == 2:
            return cached[0], cached[1]
        if isinstance(cached, str):
            return cached, 1.0

    try:
        translated = await _call_bhashini_translate(text, source_lang, "en")
        await cache_set(cache_key, [translated, 1.0], ttl=3600)
        logger.info("Translated %s→en (len=%d)", source_lang, len(translated))
        return translated, 1.0
    except Exception as exc:
        logger.error("Bhashini translate %s→en failed: %s", source_lang, exc)
        return text, 0.0


async def translate_to_hindi(text: str, source_lang: str) -> str:
    """
    Translate *text* from *source_lang* to Hindi.

    Short-circuits when source is already ``hi`` (returns original unchanged).
    Results are cached in Redis for 3600 s.
    On API error, returns the original text and logs the error.

    Validates: Requirements 7.2, 7.4, 7.5, 7.6
    """
    if source_lang == "hi":
        return text

    cache_key = _translation_cache_key(text, source_lang, "hi")
    cached = await cache_get(cache_key)
    if cached is not None:
        if isinstance(cached, list) and len(cached) == 2:
            return cached[0]
        if isinstance(cached, str):
            return cached

    try:
        translated = await _call_bhashini_translate(text, source_lang, "hi")
        await cache_set(cache_key, [translated, 1.0], ttl=3600)
        logger.info("Translated %s→hi (len=%d)", source_lang, len(translated))
        return translated
    except Exception as exc:
        logger.error("Bhashini translate %s→hi failed: %s", source_lang, exc)
        return text


# ── ASR (Automatic Speech Recognition) ───────────────────────────────────────

import base64

import boto3


async def transcribe_audio(
    audio_s3_key: str,
    hint_lang: Optional[str] = None,
) -> Tuple[str, float]:
    """
    Transcribe audio stored in S3 using the Bhashini ASR pipeline.

    Downloads the audio file from S3, base64-encodes it, and POSTs it to the
    Bhashini Dhruva inference pipeline with task type ``asr``.

    Returns (transcript, confidence).

    - On timeout (>25 s) or any error: returns ("", 0.0) and logs the error.
    - When ``BHASHINI_API_KEY`` is not configured: returns a mock transcript
      with confidence 0.82 (development / demo mode).

    Validates: Requirements 19.1, 19.2, 19.3, 19.4, 19.5
    """
    # ── Mock mode ─────────────────────────────────────────────────────────────
    if not settings.BHASHINI_API_KEY:
        logger.info(
            "BHASHINI_API_KEY not set — returning mock ASR transcript for key=%s",
            audio_s3_key,
        )
        return "मेरे क्षेत्र में पानी की समस्या है।", 0.82

    # ── Download audio from S3 ────────────────────────────────────────────────
    try:
        s3_client = boto3.client(
            "s3",
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_REGION,
        )
        s3_response = s3_client.get_object(
            Bucket=settings.S3_BUCKET_AUDIO,
            Key=audio_s3_key,
        )
        audio_bytes: bytes = s3_response["Body"].read()
    except Exception as exc:
        logger.error("Failed to download audio from S3 (key=%s): %s", audio_s3_key, exc)
        return "", 0.0

    # ── Base64-encode ─────────────────────────────────────────────────────────
    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

    # ── Build Bhashini ASR payload ────────────────────────────────────────────
    source_language = hint_lang or "hi"
    payload = {
        "pipelineTasks": [
            {
                "taskType": "asr",
                "config": {
                    "language": {"sourceLanguage": source_language}
                },
            }
        ],
        "inputData": {
            "audio": [{"audioContent": audio_b64}]
        },
    }
    headers = {
        "userID": settings.BHASHINI_USER_ID,
        "ulcaApiKey": settings.BHASHINI_API_KEY,
        "Content-Type": "application/json",
    }

    # ── POST to Bhashini with 25-second timeout ───────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(
                _BHASHINI_PIPELINE_URL,
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

        transcript: str = data["pipelineResponse"][0]["output"][0]["source"]
        # Bhashini may return a confidence score; default to 1.0 if absent
        try:
            confidence: float = float(
                data["pipelineResponse"][0]["output"][0].get("confidence", 1.0)
            )
        except (KeyError, TypeError, ValueError):
            confidence = 1.0

        logger.info(
            "Bhashini ASR: lang=%s, transcript_len=%d, conf=%.3f",
            source_language,
            len(transcript),
            confidence,
        )
        return transcript, confidence

    except httpx.TimeoutException as exc:
        logger.error(
            "Bhashini ASR timed out after 25 s (key=%s, lang=%s): %s",
            audio_s3_key,
            source_language,
            exc,
        )
        return "", 0.0
    except Exception as exc:
        logger.error(
            "Bhashini ASR failed (key=%s, lang=%s): %s",
            audio_s3_key,
            source_language,
            exc,
        )
        return "", 0.0
