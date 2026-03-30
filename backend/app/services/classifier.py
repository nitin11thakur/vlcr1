"""
app/services/classifier.py
--------------------------
AI Classification Service — uses Claude API to classify complaints into the
Indian government taxonomy.  PII is scrubbed before sending to external APIs
(Requirements 8.4, 8.7, 18.1).
"""

import json
import logging
import re
from typing import Optional

from anthropic import AsyncAnthropic

from app.core.config import settings
from app.core.exceptions import ClassificationFailed

logger = logging.getLogger("vlcr.classifier")

# ── Taxonomy ───────────────────────────────────────────────────────────────────

CATEGORIES = [
    "Water", "Roads & Infrastructure", "Electricity", "Sanitation",
    "Health", "Revenue & Land", "Law & Order", "Education", "Agriculture",
]

DEPT_TAXONOMY = {
    "Water": {
        "subcats": ["Hand pump broken", "No water supply", "Water contamination", "Pipeline leak", "Tap connection pending"],
        "dept_prefix": "JJM",
    },
    "Roads & Infrastructure": {
        "subcats": ["Pothole", "Road not built", "Bridge damaged", "Street light out", "Drainage blocked"],
        "dept_prefix": "PWD",
    },
    "Electricity": {
        "subcats": ["Power outage", "Fallen wire / Safety hazard", "Transformer fault", "New connection pending", "High billing"],
        "dept_prefix": "DISCOM",
    },
    "Sanitation": {
        "subcats": ["Garbage not collected", "Open defecation", "Sewage overflow", "Drain clogged", "No toilet facility"],
        "dept_prefix": "MUNI",
    },
    "Health": {
        "subcats": ["Hospital closed", "No doctor", "Medicine shortage", "Ambulance not responding", "ASHA worker absent"],
        "dept_prefix": "HEALTH",
    },
    "Revenue & Land": {
        "subcats": ["Land record dispute", "Encroachment", "Property tax issue", "Ration card problem", "Birth/death certificate"],
        "dept_prefix": "REV",
    },
    "Law & Order": {
        "subcats": ["FIR not registered", "Police inaction", "Harassment", "Cybercrime", "Missing person"],
        "dept_prefix": "POLICE",
    },
    "Education": {
        "subcats": ["School closed", "Teacher absent", "Mid-day meal issue", "Scholarship delayed", "Textbook shortage"],
        "dept_prefix": "EDU",
    },
    "Agriculture": {
        "subcats": ["Crop damage compensation", "Fertilizer shortage", "Irrigation failure", "Kisan credit issue", "Mandi problem"],
        "dept_prefix": "AGRI",
    },
}

SEVERITY_GUIDE = {
    "critical": "Immediate threat to life, safety, or fundamental rights (fallen electric wire, no water for 7+ days, hospital closed in emergency)",
    "high":     "Significant disruption affecting many people or urgent personal need (power outage >24h, major road blocked)",
    "medium":   "Ongoing inconvenience with no immediate danger (intermittent water supply, pothole on secondary road)",
    "low":      "Administrative or paperwork issues (certificate delay, minor billing dispute)",
}

CLASSIFICATION_PROMPT = """You are an AI classifier for VLCR — India's Vernacular Language Complaint Router.
Classify the following citizen complaint (already translated to English) into the structured schema below.

CATEGORIES: {categories}

DEPARTMENT TAXONOMY (category → subcategories):
{taxonomy}

SEVERITY GUIDE:
{severity}

COMPLAINT TEXT:
"{complaint_text}"

LOCATION CONTEXT (if available): {location}

Respond ONLY with a JSON object in this exact format:
{{
  "category": "<one of the categories above>",
  "subcategory": "<most specific matching subcategory>",
  "severity": "<critical|high|medium|low>",
  "confidence": <0.0-1.0 float>,
  "location_state": "<Indian state name or null>",
  "location_district": "<district name or null>",
  "location_block": "<block/tehsil name or null>",
  "location_village": "<village/ward name or null>",
  "dept_prefix": "<prefix from taxonomy>",
  "reasoning": "<one sentence explanation>"
}}

Be strict with confidence: score < 0.70 means the complaint is ambiguous or unclear.
"""

# ── PII scrubbing ──────────────────────────────────────────────────────────────

# Order matters: Aadhaar (12 digits) before phone (10 digits) to avoid partial matches
_PII_PATTERNS = [
    (re.compile(r'\b\d{12}\b'), '[AADHAAR]'),
    (re.compile(r'\b[6-9]\d{9}\b'), '[PHONE]'),
    (re.compile(r'\b[A-Z]{5}\d{4}[A-Z]\b'), '[PAN]'),
    (re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'), '[EMAIL]'),
]

_MAX_CHARS = 1500


def scrub_pii(text: str) -> str:
    """
    Replace PII tokens with placeholders, then truncate to 1500 characters.

    Replacements (Requirements 8.4, 18.1):
      - 12-digit Aadhaar  → [AADHAAR]
      - 10-digit Indian mobile (starting 6–9) → [PHONE]
      - PAN card format (AAAAA9999A) → [PAN]
      - Email addresses → [EMAIL]

    Truncation happens AFTER scrubbing so no PII leaks at the boundary
    (Requirement 8.7).
    """
    for pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text[:_MAX_CHARS]


# ── Classification ─────────────────────────────────────────────────────────────

async def classify_complaint(
    complaint_text_en: str,
    location_raw: Optional[str] = None,
    state_code: Optional[str] = None,
) -> dict:
    """
    Call Claude API to classify a complaint.

    Returns a dict with keys:
        category, subcategory, severity, confidence,
        location_state, location_district, dept_prefix, reasoning

    Raises ClassificationFailed on non-JSON response or API exception
    (Requirements 8.1, 8.2, 8.3, 8.6).
    """
    if not settings.ANTHROPIC_API_KEY:
        logger.warning("No Anthropic API key — using mock classifier")
        return _mock_classify(complaint_text_en)

    clean_text = scrub_pii(complaint_text_en)
    location_ctx = location_raw or state_code or "Not specified"

    taxonomy_str = "\n".join(
        f"  {cat}: {', '.join(v['subcats'])}"
        for cat, v in DEPT_TAXONOMY.items()
    )
    severity_str = "\n".join(f"  {k}: {v}" for k, v in SEVERITY_GUIDE.items())

    prompt = CLASSIFICATION_PROMPT.format(
        categories=", ".join(CATEGORIES),
        taxonomy=taxonomy_str,
        severity=severity_str,
        complaint_text=clean_text,
        location=location_ctx,
    )

    raw = ""
    try:
        client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        message = await client.messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        # Strip markdown fences if present
        raw = re.sub(r"```json|```", "", raw).strip()
        result = json.loads(raw)
        logger.info(
            "Classified as %s/%s severity=%s conf=%s",
            result.get("category"),
            result.get("subcategory"),
            result.get("severity"),
            result.get("confidence"),
        )
        return result
    except json.JSONDecodeError as exc:
        logger.error("LLM returned non-JSON: %s", raw[:200])
        raise ClassificationFailed(f"JSON parse error: {exc}") from exc
    except Exception as exc:
        logger.error("Claude API error: %s", exc)
        raise ClassificationFailed(str(exc)) from exc


# ── Helpers ────────────────────────────────────────────────────────────────────

def resolve_dept_code(state_code: str, dept_prefix: str) -> str:
    """Compose department code from state + prefix, e.g. MH_JJM."""
    state = (state_code or "IN").upper().replace("-", "_")
    # Strip ISO country prefix if present (IN-MH → MH)
    if "_" in state:
        state = state.split("_")[-1]
    return f"{state}_{dept_prefix}"


def resolve_dept_name(dept_prefix: str) -> str:
    """Return human-readable department name for a given prefix."""
    names = {
        "JJM":    "Jal Jeevan Mission",
        "PWD":    "Public Works Department",
        "DISCOM": "State DISCOM (Electricity Board)",
        "MUNI":   "Municipal Corporation",
        "HEALTH": "District Health Department",
        "REV":    "Revenue & Land Records Dept",
        "POLICE": "State Police",
        "EDU":    "State Education Department",
        "AGRI":   "Agriculture Department",
    }
    return names.get(dept_prefix, dept_prefix)


def _mock_classify(text: str) -> dict:
    """Deterministic mock used when no API key is configured."""
    text_lower = text.lower()
    if any(w in text_lower for w in ["water", "pump", "पानी", "हैंडपंप", "jal"]):
        return {
            "category": "Water", "subcategory": "Hand pump broken",
            "severity": "high", "confidence": 0.91, "dept_prefix": "JJM",
            "location_state": "Bihar", "location_district": None,
            "location_block": None, "location_village": None,
            "reasoning": "Mock: water-related keywords detected",
        }
    if any(w in text_lower for w in ["road", "pothole", "सड़क", "गड्ढा"]):
        return {
            "category": "Roads & Infrastructure", "subcategory": "Pothole",
            "severity": "medium", "confidence": 0.88, "dept_prefix": "PWD",
            "location_state": None, "location_district": None,
            "location_block": None, "location_village": None,
            "reasoning": "Mock: roads-related keywords detected",
        }
    if any(w in text_lower for w in ["light", "power", "electricity", "bijli", "बिजली"]):
        return {
            "category": "Electricity", "subcategory": "Power outage",
            "severity": "high", "confidence": 0.85, "dept_prefix": "DISCOM",
            "location_state": None, "location_district": None,
            "location_block": None, "location_village": None,
            "reasoning": "Mock: electricity keywords detected",
        }
    # Default fallback
    return {
        "category": "Sanitation", "subcategory": "Garbage not collected",
        "severity": "low", "confidence": 0.55, "dept_prefix": "MUNI",
        "location_state": None, "location_district": None,
        "location_block": None, "location_village": None,
        "reasoning": "Mock: fallback classification",
    }
