"""
app/routers/auth.py
-------------------
Authentication endpoints.

Routes (prefix /api/v1/auth set in main.py):
  POST /token          — OAuth2 password form (for Swagger UI / OAuth2 clients)
  POST /login          — JSON body login
  GET  /me             — current user info (JWT required)
  POST /seed-demo-dept — create demo departments, routing rules, and three demo GovUsers

Security (Requirements 3.1, 3.2, 3.6, 18.6):
  - Passwords verified with bcrypt (cost ≥ 12)
  - JWT signed with HS256, 8-hour expiry
  - hashed_password is NEVER returned in any response
"""

import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.core.database import get_db
from app.core.exceptions import Unauthorized
from app.models.models import Department, GovUser, RoutingRule
from app.schemas.schemas import LoginRequest, TokenResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_token_response(user: GovUser) -> TokenResponse:
    """Create a TokenResponse for *user*. Never includes hashed_password."""
    token = create_access_token(
        {"sub": user.username, "role": user.role, "name": user.full_name or user.username}
    )
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=8 * 3600,
        user_role=user.role,
        user_name=user.full_name or user.username,
    )


async def _authenticate(username: str, password: str, db: AsyncSession) -> GovUser:
    """Look up user by username and verify password. Raises Unauthorized on failure."""
    result = await db.execute(select(GovUser).where(GovUser.username == username))
    user: GovUser | None = result.scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(password, user.hashed_password):
        raise Unauthorized(detail="Invalid username or password.")
    return user


# ---------------------------------------------------------------------------
# POST /token  — OAuth2 password form (Requirement 3.1)
# ---------------------------------------------------------------------------

@router.post("/token", response_model=TokenResponse, summary="OAuth2 password-form login")
async def token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """
    Standard OAuth2 password-form endpoint used by Swagger UI's Authorize button.
    Returns access_token and token_type: bearer.
    """
    user = await _authenticate(form_data.username, form_data.password, db)
    logger.info("OAuth2 token issued for user=%s role=%s", user.username, user.role)
    return _build_token_response(user)


# ---------------------------------------------------------------------------
# POST /login  — JSON body (Requirement 3.2)
# ---------------------------------------------------------------------------

@router.post("/login", response_model=TokenResponse, summary="JSON body login")
async def login(
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """
    JSON-body login endpoint for the frontend SPA.
    Returns access_token and token_type: bearer.
    """
    user = await _authenticate(body.username, body.password, db)
    logger.info("Login successful for user=%s role=%s", user.username, user.role)
    return _build_token_response(user)


# ---------------------------------------------------------------------------
# GET /me  — current user info (Requirement 3.6)
# ---------------------------------------------------------------------------

@router.get("/me", summary="Current user info")
async def me(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Return basic profile for the authenticated user.
    hashed_password is never included (Requirement 18.6).
    """
    result = await db.execute(
        select(GovUser).where(GovUser.username == current_user["sub"])
    )
    user: GovUser | None = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise Unauthorized(detail="User account not found or inactive.")
    return {
        "username": user.username,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "state_code": user.state_code,
        "dept_code": user.dept_code,
        "mfa_enabled": user.mfa_enabled,
        "is_active": user.is_active,
    }


# ---------------------------------------------------------------------------
# POST /seed-demo-dept  — seed demo data (Requirement 3.7)
# ---------------------------------------------------------------------------

_DEMO_DEPARTMENTS = [
    {
        "code": "MH_PWD",
        "name": "Maharashtra Public Works Department",
        "state_code": "MH",
        "dispatch_type": "email",
        "dispatch_endpoint": "pwd@maharashtra.gov.in",
        "contact_email": "pwd@maharashtra.gov.in",
        "sla_hours": 72,
        "escalation_hours": 24,
    },
    {
        "code": "MH_HEALTH",
        "name": "Maharashtra Health Department",
        "state_code": "MH",
        "dispatch_type": "email",
        "dispatch_endpoint": "health@maharashtra.gov.in",
        "contact_email": "health@maharashtra.gov.in",
        "sla_hours": 48,
        "escalation_hours": 12,
    },
]

_DEMO_ROUTING_RULES = [
    # PWD handles roads, water, infrastructure
    {"dept_code": "MH_PWD", "state_code": "MH", "category": "roads_and_transport", "priority": 10},
    {"dept_code": "MH_PWD", "state_code": "MH", "category": "water_supply", "priority": 10},
    {"dept_code": "MH_PWD", "state_code": "MH", "category": "infrastructure", "priority": 20},
    # Health handles sanitation, medical
    {"dept_code": "MH_HEALTH", "state_code": "MH", "category": "health_and_sanitation", "priority": 10},
    {"dept_code": "MH_HEALTH", "state_code": "MH", "category": "medical_services", "priority": 10},
]

_DEMO_USERS = [
    {
        "username": "admin",
        "password": "admin123",
        "email": "admin@vlcr.demo",
        "full_name": "Demo Admin",
        "role": "super_admin",
        "state_code": "MH",
        "dept_code": "MH_PWD",
    },
    {
        "username": "reviewer",
        "password": "review123",
        "email": "reviewer@vlcr.demo",
        "full_name": "Demo Reviewer",
        "role": "reviewer",
        "state_code": "MH",
        "dept_code": "MH_PWD",
    },
    {
        "username": "officer",
        "password": "officer123",
        "email": "officer@vlcr.demo",
        "full_name": "Demo Officer",
        "role": "officer",
        "state_code": "MH",
        "dept_code": "MH_HEALTH",
    },
]


@router.post("/seed-demo-dept", summary="Seed demo departments, routing rules, and users")
async def seed_demo_dept(db: AsyncSession = Depends(get_db)) -> dict:
    """
    Idempotent seed endpoint — creates demo departments, routing rules, and three
    GovUsers (admin/reviewer/officer) if they do not already exist.

    Demo credentials (Requirement 3.7):
      admin    / admin123   → role: super_admin
      reviewer / review123  → role: reviewer
      officer  / officer123 → role: officer

    hashed_password is never returned (Requirement 18.6).
    """
    created: dict[str, list[str]] = {"departments": [], "routing_rules": [], "users": []}

    # ── Departments ──────────────────────────────────────────────────────────
    for dept_data in _DEMO_DEPARTMENTS:
        existing = await db.execute(
            select(Department).where(Department.code == dept_data["code"])
        )
        if existing.scalar_one_or_none() is None:
            dept = Department(**dept_data)
            db.add(dept)
            created["departments"].append(dept_data["code"])
            logger.info("Seeded department: %s", dept_data["code"])

    # Flush so routing rules can reference the new departments
    await db.flush()

    # ── Routing Rules ────────────────────────────────────────────────────────
    for rule_data in _DEMO_ROUTING_RULES:
        existing = await db.execute(
            select(RoutingRule).where(
                RoutingRule.dept_code == rule_data["dept_code"],
                RoutingRule.state_code == rule_data["state_code"],
                RoutingRule.category == rule_data["category"],
            )
        )
        if existing.scalar_one_or_none() is None:
            rule = RoutingRule(**rule_data)
            db.add(rule)
            created["routing_rules"].append(f"{rule_data['dept_code']}:{rule_data['category']}")
            logger.info("Seeded routing rule: %s → %s", rule_data["category"], rule_data["dept_code"])

    # ── GovUsers ─────────────────────────────────────────────────────────────
    for user_data in _DEMO_USERS:
        existing = await db.execute(
            select(GovUser).where(GovUser.username == user_data["username"])
        )
        if existing.scalar_one_or_none() is None:
            plain_password = user_data.pop("password")
            user = GovUser(
                **user_data,
                hashed_password=hash_password(plain_password),
            )
            db.add(user)
            created["users"].append(user_data["username"])
            logger.info("Seeded demo user: %s (role=%s)", user_data["username"], user_data["role"])

    await db.flush()

    return {
        "message": "Demo seed complete.",
        "created": created,
        "demo_credentials": [
            {"username": "admin", "password": "admin123", "role": "super_admin"},
            {"username": "reviewer", "password": "review123", "role": "reviewer"},
            {"username": "officer", "password": "officer123", "role": "officer"},
        ],
    }
