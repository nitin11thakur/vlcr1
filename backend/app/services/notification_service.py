"""
app/services/notification_service.py
-------------------------------------
SMS notification service.

Routes to Gupshup, Twilio, or mock based on settings.SMS_PROVIDER.
All functions degrade gracefully — a failed SMS never blocks the pipeline.

Requirements: 10.1, 10.2, 10.3, 10.4, 10.5
"""

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger("vlcr.notification")

# ── Message templates ──────────────────────────────────────────────────────────

_ACK_TEMPLATE = (
    "Your complaint has been registered. Reference: {ref}. "
    "Department: {dept}. Category: {cat}. "
    "Expected response within {sla} hours. "
    "Track at: https://vlcr.gov.in/track/{ref}"
)

_STATUS_TEMPLATE = (
    "Update on complaint {ref}: Status changed to {status}. "
    "{note}"
    "Track at: https://vlcr.gov.in/track/{ref}"
)


# ── Public API ─────────────────────────────────────────────────────────────────

async def send_acknowledgement(
    phone: str,
    reference_number: str,
    dept_name: str,
    category: str,
    sla_hours: int = 72,
) -> bool:
    """
    Send an SMS acknowledgement to the citizen.

    Returns True on success, False on failure.
    Never raises — SMS failure must not block the pipeline.
    """
    if not phone:
        logger.debug("send_acknowledgement: no phone number — skipping")
        return False

    message = _ACK_TEMPLATE.format(
        ref=reference_number,
        dept=dept_name,
        cat=category,
        sla=sla_hours,
    )
    return await _dispatch(phone, message)


async def send_status_update(
    phone: str,
    reference_number: str,
    new_status: str,
    note: str = "",
) -> bool:
    """
    Send an SMS status update to the citizen.

    Returns True on success, False on failure.
    """
    if not phone:
        logger.debug("send_status_update: no phone number — skipping")
        return False

    note_text = f"Note: {note}. " if note else ""
    message = _STATUS_TEMPLATE.format(
        ref=reference_number,
        status=new_status,
        note=note_text,
    )
    return await _dispatch(phone, message)


# ── Internal dispatch ──────────────────────────────────────────────────────────

async def _dispatch(phone: str, message: str) -> bool:
    """
    Route the message to the configured SMS provider.

    FIX: Changed if/if/if to if/elif/elif/else to prevent
    fall-through evaluation of all branches.
    """
    provider = settings.SMS_PROVIDER

    if provider == "mock":
        # _send_mock is synchronous — returns bool directly, no await needed.
        return _send_mock(phone, message)
    elif provider == "gupshup":
        return await _send_gupshup(phone, message)
    elif provider == "twilio":
        return await _send_twilio(phone, message)
    else:
        logger.warning(
            "Unknown SMS_PROVIDER '%s' — message not sent to ...%s",
            provider,
            phone[-4:],
        )
        return False


def _send_mock(phone: str, message: str) -> bool:
    """Log the message at INFO level; no external call (Requirement 10.5)."""
    logger.info("[MOCK SMS] → ...%s: %s", phone[-4:], message[:120])
    return True


async def _send_gupshup(phone: str, message: str) -> bool:
    """Send via Gupshup REST API (Requirement 10.2)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.gupshup.io/sm/api/v1/msg",
                data={
                    "channel": "sms",
                    "source": settings.GUPSHUP_APP_ID or "",
                    "destination": phone,
                    "message": message,
                    "src.name": "VLCR",
                },
                headers={"apikey": settings.GUPSHUP_API_KEY or ""},
            )
            if resp.status_code == 202:
                logger.info("Gupshup SMS queued for ...%s", phone[-4:])
                return True
            logger.warning("Gupshup returned %s for ...%s: %s", resp.status_code, phone[-4:], resp.text[:200])
            return False
    except Exception as exc:
        logger.warning("Gupshup SMS failed for ...%s: %s", phone[-4:], exc)
        return False


async def _send_twilio(phone: str, message: str) -> bool:
    """Send via Twilio REST API (Requirement 10.3)."""
    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        logger.warning("Twilio credentials missing — SMS not sent to ...%s", phone[-4:])
        return False

    url = f"https://api.twilio.com/2010-04-01/Accounts/{settings.TWILIO_ACCOUNT_SID}/Messages.json"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                data={
                    "From": settings.TWILIO_FROM_NUMBER or "",
                    "To": phone,
                    "Body": message,
                },
                auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
            )
            if resp.status_code in (200, 201):
                logger.info("Twilio SMS queued for ...%s", phone[-4:])
                return True
            logger.warning("Twilio returned %s for ...%s: %s", resp.status_code, phone[-4:], resp.text[:200])
            return False
    except Exception as exc:
        logger.warning("Twilio SMS failed for ...%s: %s", phone[-4:], exc)
        return False
