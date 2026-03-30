"""
app/routers/routing.py
----------------------
Routing configuration endpoints.

Routes (prefix /api/v1/routing set in main.py):
  GET    /departments          — list departments, filterable by state_code (JWT)
  GET    /rules                — list active rules, cached per state in Redis (JWT)
  POST   /rules                — create routing rule, invalidate Redis cache (JWT super_admin)
  DELETE /rules/{id}           — soft-delete rule, invalidate Redis cache (JWT super_admin)

Requirements: 14.1, 14.2, 14.3, 14.4, 14.5
"""

import logging
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_role
from app.core.database import get_db
from app.core.redis_client import cache_get, cache_set, invalidate
from app.models.models import Department, RoutingRule
from app.schemas.schemas import DepartmentSchema, RoutingRuleCreate, RoutingRuleSchema

logger = logging.getLogger("vlcr.routers.routing")

router = APIRouter(tags=["routing"])

_RULES_CACHE_TTL = 300  # seconds


# ── GET /departments ──────────────────────────────────────────────────────────

@router.get("/departments", response_model=List[DepartmentSchema])
async def list_departments(
    state_code: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Return all active departments, optionally filtered by state_code.

    Requirements: 14.1
    """
    query = select(Department).where(Department.is_active == True)
    if state_code:
        query = query.where(Department.state_code == state_code)
    query = query.order_by(Department.state_code, Department.name)

    result = await db.execute(query)
    departments = result.scalars().all()
    return [DepartmentSchema.model_validate(d) for d in departments]


# ── GET /rules ────────────────────────────────────────────────────────────────

@router.get("/rules", response_model=List[RoutingRuleSchema])
async def list_rules(
    state_code: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Return active routing rules.

    When state_code is provided, results are cached in Redis under
    key ``routing:{state_code}`` with TTL 300s.
    Without state_code, all active rules are returned without caching.

    Requirements: 14.2, 14.5
    """
    if state_code:
        cache_key = f"routing:{state_code}"
        cached = await cache_get(cache_key)
        if cached is not None:
            logger.debug("Cache hit for %s", cache_key)
            return cached

    query = select(RoutingRule).where(RoutingRule.is_active == True)
    if state_code:
        query = query.where(RoutingRule.state_code == state_code)
    query = query.order_by(RoutingRule.priority, RoutingRule.state_code)

    result = await db.execute(query)
    rules = result.scalars().all()
    serialised = [RoutingRuleSchema.model_validate(r).model_dump(mode="json") for r in rules]

    if state_code:
        await cache_set(cache_key, serialised, ttl=_RULES_CACHE_TTL)

    return serialised


# ── POST /rules ───────────────────────────────────────────────────────────────

@router.post("/rules", response_model=RoutingRuleSchema, status_code=201)
async def create_rule(
    body: RoutingRuleCreate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_role("super_admin")),
):
    """
    Create a new routing rule and invalidate the Redis cache for its state.

    Requirements: 14.3
    """
    # Verify the department exists
    dept_result = await db.execute(
        select(Department).where(Department.code == body.dept_code)
    )
    if dept_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail=f"Department '{body.dept_code}' not found.")

    rule = RoutingRule(
        dept_code=body.dept_code,
        state_code=body.state_code,
        category=body.category,
        subcategory=body.subcategory,
        priority=body.priority,
        is_active=True,
    )
    db.add(rule)
    await db.flush()

    await invalidate(f"routing:{body.state_code}")
    logger.info(
        "Routing rule created: dept=%s state=%s category=%s by %s",
        body.dept_code, body.state_code, body.category, current_user.get("sub"),
    )
    return RoutingRuleSchema.model_validate(rule)


# ── DELETE /rules/{id} ────────────────────────────────────────────────────────

@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_role("super_admin")),
):
    """
    Soft-delete a routing rule (set is_active = False) and invalidate
    the Redis cache for its state.

    Requirements: 14.4
    """
    result = await db.execute(
        select(RoutingRule).where(RoutingRule.id == rule_id)
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=404, detail=f"Routing rule '{rule_id}' not found.")

    state_code = rule.state_code
    rule.is_active = False
    await db.flush()

    await invalidate(f"routing:{state_code}")
    logger.info(
        "Routing rule %s soft-deleted by %s (state=%s)",
        rule_id, current_user.get("sub"), state_code,
    )
