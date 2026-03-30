"""
app/routers/dashboard.py
------------------------
Dashboard analytics endpoints.

Routes (prefix /api/v1/dashboard set in main.py):
  GET /stats  — aggregate complaint statistics (JWT)
  GET /sla    — per-department SLA metrics (JWT)

Requirements: 13.1, 13.2, 13.3
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.models import Complaint, Department
from app.schemas.schemas import DashboardStats, SLAMetrics, SLAResponse

logger = logging.getLogger("vlcr.routers.dashboard")

router = APIRouter(tags=["dashboard"])


# ── GET /stats ────────────────────────────────────────────────────────────────

@router.get("/stats", response_model=DashboardStats)
async def get_dashboard_stats(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Return aggregate complaint statistics for the dashboard.

    All counts are computed in a single pass where possible.
    Requirements: 13.1, 13.3
    """
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())  # Monday

    # ── Scalar aggregates ─────────────────────────────────────────────────────

    scalar_result = await db.execute(
        select(
            # total_today
            func.count(
                case((Complaint.created_at >= today_start, Complaint.id))
            ).label("total_today"),
            # routed_today: complaints that have a routed_at timestamp today
            func.count(
                case((Complaint.routed_at >= today_start, Complaint.id))
            ).label("routed_today"),
            # review_queue
            func.count(
                case((Complaint.status == "review_required", Complaint.id))
            ).label("review_queue"),
            # avg_route_seconds: average of (routed_at - created_at) in seconds
            func.avg(
                case(
                    (
                        Complaint.routed_at.isnot(None),
                        func.extract(
                            "epoch", Complaint.routed_at - Complaint.created_at
                        ),
                    )
                )
            ).label("avg_route_seconds"),
            # total_this_week
            func.count(
                case((Complaint.created_at >= week_start, Complaint.id))
            ).label("total_this_week"),
            # critical_open: critical severity, not resolved/review_required
            func.count(
                case(
                    (
                        (Complaint.severity == "critical")
                        & (Complaint.status.notin_(["resolved"])),
                        Complaint.id,
                    )
                )
            ).label("critical_open"),
            # resolution_rate_pct numerator and total for post-processing
            func.count(
                case((Complaint.status == "resolved", Complaint.id))
            ).label("resolved_count"),
            func.count(Complaint.id).label("total_all"),
        )
    )
    row = scalar_result.one()

    total_all: int = row.total_all or 0
    resolved_count: int = row.resolved_count or 0
    resolution_rate_pct = (
        round(resolved_count / total_all * 100, 2) if total_all > 0 else 0.0
    )
    avg_route_seconds = float(row.avg_route_seconds or 0.0)

    # ── Top categories ────────────────────────────────────────────────────────

    cat_result = await db.execute(
        select(Complaint.category, func.count(Complaint.id).label("cnt"))
        .where(Complaint.category.isnot(None))
        .group_by(Complaint.category)
        .order_by(func.count(Complaint.id).desc())
        .limit(10)
    )
    top_categories = [
        {"category": r.category, "count": r.cnt} for r in cat_result.all()
    ]

    # ── By language ───────────────────────────────────────────────────────────

    lang_result = await db.execute(
        select(Complaint.citizen_lang, func.count(Complaint.id).label("cnt"))
        .group_by(Complaint.citizen_lang)
        .order_by(func.count(Complaint.id).desc())
    )
    by_language = [
        {"language": r.citizen_lang, "count": r.cnt} for r in lang_result.all()
    ]

    # ── By state ──────────────────────────────────────────────────────────────

    state_result = await db.execute(
        select(Complaint.location_state, func.count(Complaint.id).label("cnt"))
        .where(Complaint.location_state.isnot(None))
        .group_by(Complaint.location_state)
        .order_by(func.count(Complaint.id).desc())
    )
    by_state = [
        {"state": r.location_state, "count": r.cnt} for r in state_result.all()
    ]

    # ── Queue depths (count per status) ──────────────────────────────────────

    depth_result = await db.execute(
        select(Complaint.status, func.count(Complaint.id).label("cnt"))
        .group_by(Complaint.status)
    )
    queue_depths = {r.status: r.cnt for r in depth_result.all()}

    return DashboardStats(
        total_today=row.total_today or 0,
        routed_today=row.routed_today or 0,
        review_queue=row.review_queue or 0,
        avg_route_seconds=avg_route_seconds,
        total_this_week=row.total_this_week or 0,
        critical_open=row.critical_open or 0,
        resolution_rate_pct=resolution_rate_pct,
        top_categories=top_categories,
        by_language=by_language,
        by_state=by_state,
        queue_depths=queue_depths,
    )


# ── GET /sla ──────────────────────────────────────────────────────────────────

@router.get("/sla", response_model=SLAResponse)
async def get_sla_metrics(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Return per-department SLA metrics.

    For each active department, computes:
      - total complaints assigned
      - resolved count
      - average resolution time in hours (dispatched_at → resolved via status)
      - resolution rate percentage
      - SLA breach count (avg resolution > dept.sla_hours)

    Requirements: 13.2, 13.3
    """
    # Per-department aggregates joined with Department for sla_hours / name
    dept_result = await db.execute(
        select(
            Department.code.label("dept_code"),
            Department.name.label("dept_name"),
            Department.sla_hours.label("sla_hours"),
            func.count(Complaint.id).label("total_complaints"),
            func.count(
                case((Complaint.status == "resolved", Complaint.id))
            ).label("resolved_count"),
            # avg resolution hours: from created_at to dispatched_at for resolved complaints
            func.avg(
                case(
                    (
                        (Complaint.status == "resolved")
                        & Complaint.dispatched_at.isnot(None),
                        func.extract(
                            "epoch",
                            Complaint.dispatched_at - Complaint.created_at,
                        )
                        / 3600.0,
                    )
                )
            ).label("avg_resolution_hours"),
            # breached: resolved complaints where resolution time exceeded sla_hours
            func.count(
                case(
                    (
                        (Complaint.status == "resolved")
                        & Complaint.dispatched_at.isnot(None)
                        & (
                            func.extract(
                                "epoch",
                                Complaint.dispatched_at - Complaint.created_at,
                            )
                            / 3600.0
                            > Department.sla_hours
                        ),
                        Complaint.id,
                    )
                )
            ).label("breached_count"),
        )
        .outerjoin(Complaint, Complaint.dept_code == Department.code)
        .where(Department.is_active.is_(True))
        .group_by(Department.code, Department.name, Department.sla_hours)
        .order_by(Department.name)
    )

    departments: list[SLAMetrics] = []
    for row in dept_result.all():
        total = row.total_complaints or 0
        resolved = row.resolved_count or 0
        rate = round(resolved / total * 100, 2) if total > 0 else 0.0
        avg_hours = float(row.avg_resolution_hours or 0.0)

        departments.append(
            SLAMetrics(
                dept_code=row.dept_code,
                dept_name=row.dept_name,
                total_complaints=total,
                resolved_count=resolved,
                avg_resolution_hours=avg_hours,
                resolution_rate_pct=rate,
                sla_hours=row.sla_hours or 72,
                breached_count=row.breached_count or 0,
            )
        )

    return SLAResponse(
        departments=departments,
        generated_at=datetime.now(timezone.utc),
    )
