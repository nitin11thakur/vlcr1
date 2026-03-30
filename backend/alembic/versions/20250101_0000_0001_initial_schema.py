"""Initial schema — all six VLCR tables.

Revision ID: 0001
Revises:
Create Date: 2025-01-01 00:00:00.000000

Validates: Requirements 2.2
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── departments ────────────────────────────────────────────────────────────
    op.create_table(
        "departments",
        sa.Column("code", sa.String(50), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("state_code", sa.String(10), nullable=False),
        sa.Column("dispatch_type", sa.String(20), nullable=False),
        sa.Column("dispatch_endpoint", sa.String(500), nullable=True),
        sa.Column("dispatch_api_key", sa.String(500), nullable=True),
        sa.Column("contact_email", sa.String(200), nullable=True),
        sa.Column("sla_hours", sa.Integer(), nullable=True, server_default="72"),
        sa.Column("escalation_hours", sa.Integer(), nullable=True, server_default="24"),
        sa.Column("is_active", sa.Boolean(), nullable=True, server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_departments_state_code", "departments", ["state_code"])

    # ── routing_rules ──────────────────────────────────────────────────────────
    op.create_table(
        "routing_rules",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("dept_code", sa.String(50), nullable=False),
        sa.Column("state_code", sa.String(10), nullable=False),
        sa.Column("category", sa.String(100), nullable=False),
        sa.Column("subcategory", sa.String(200), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=True, server_default="100"),
        sa.Column("is_active", sa.Boolean(), nullable=True, server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["dept_code"], ["departments.code"], name="fk_routing_rules_dept_code"),
    )
    op.create_index("ix_routing_rules_state_code", "routing_rules", ["state_code"])

    # ── complaints ─────────────────────────────────────────────────────────────
    op.create_table(
        "complaints",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("reference_number", sa.String(50), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Citizen
        sa.Column("citizen_phone", sa.String(20), nullable=True),
        sa.Column("citizen_name", sa.String(200), nullable=True),
        sa.Column("citizen_lang", sa.String(10), nullable=False, server_default="hi"),
        # Input
        sa.Column("input_channel", sa.String(20), nullable=False),
        sa.Column("input_type", sa.String(10), nullable=False),
        sa.Column("raw_audio_s3_key", sa.String(500), nullable=True),
        sa.Column("raw_text_original", sa.Text(), nullable=True),
        # Language processing
        sa.Column("transcript_raw", sa.Text(), nullable=True),
        sa.Column("transcript_norm", sa.Text(), nullable=True),
        sa.Column("translation_hi", sa.Text(), nullable=True),
        sa.Column("translation_en", sa.Text(), nullable=True),
        sa.Column("lang_detect_code", sa.String(10), nullable=True),
        sa.Column("asr_confidence", sa.Float(), nullable=True),
        sa.Column("nlp_confidence", sa.Float(), nullable=True),
        # Classification
        sa.Column("category", sa.String(100), nullable=True),
        sa.Column("subcategory", sa.String(200), nullable=True),
        sa.Column("severity", sa.String(20), nullable=True),
        sa.Column("classifier_conf", sa.Float(), nullable=True),
        sa.Column("location_state", sa.String(100), nullable=True),
        sa.Column("location_district", sa.String(100), nullable=True),
        sa.Column("location_block", sa.String(100), nullable=True),
        sa.Column("location_village", sa.String(200), nullable=True),
        sa.Column("location_raw", sa.Text(), nullable=True),
        sa.Column("location_lat", sa.Float(), nullable=True),
        sa.Column("location_lon", sa.Float(), nullable=True),
        # Routing
        sa.Column("dept_code", sa.String(50), nullable=True),
        sa.Column("dept_name", sa.String(200), nullable=True),
        sa.Column("routing_rule_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("routed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatch_method", sa.String(20), nullable=True),
        sa.Column("dispatch_ref", sa.String(200), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        # Lifecycle
        sa.Column("status", sa.String(30), nullable=False, server_default="received"),
        sa.Column("duplicate_of", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column("reviewed_by", sa.String(200), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("classifier_raw", postgresql.JSONB(), nullable=True),
        # Foreign keys
        sa.ForeignKeyConstraint(["dept_code"], ["departments.code"], name="fk_complaints_dept_code"),
        sa.ForeignKeyConstraint(["routing_rule_id"], ["routing_rules.id"], name="fk_complaints_routing_rule_id"),
        sa.ForeignKeyConstraint(["duplicate_of"], ["complaints.id"], name="fk_complaints_duplicate_of"),
    )
    op.create_index("ix_complaints_reference_number", "complaints", ["reference_number"])
    op.create_index("ix_complaints_citizen_phone", "complaints", ["citizen_phone"])
    op.create_index("ix_complaints_category", "complaints", ["category"])
    op.create_index("ix_complaints_severity", "complaints", ["severity"])
    op.create_index("ix_complaints_location_state", "complaints", ["location_state"])
    op.create_index("ix_complaints_dept_code", "complaints", ["dept_code"])
    op.create_index("ix_complaints_status", "complaints", ["status"])
    op.create_index("ix_complaints_created_at", "complaints", ["created_at"])
    op.create_index("ix_complaints_status_severity", "complaints", ["status", "severity"])
    op.create_index("ix_complaints_dept_state", "complaints", ["dept_code", "location_state"])

    # ── gov_users ──────────────────────────────────────────────────────────────
    op.create_table(
        "gov_users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("username", sa.String(100), nullable=False, unique=True),
        sa.Column("email", sa.String(200), nullable=False, unique=True),
        sa.Column("hashed_password", sa.String(200), nullable=False),
        sa.Column("full_name", sa.String(200), nullable=True),
        sa.Column("role", sa.String(30), nullable=False, server_default="officer"),
        sa.Column("state_code", sa.String(10), nullable=True),
        sa.Column("dept_code", sa.String(50), nullable=True),
        sa.Column("mfa_enabled", sa.Boolean(), nullable=True, server_default="false"),
        sa.Column("is_active", sa.Boolean(), nullable=True, server_default="true"),
        sa.Column("last_login", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["dept_code"], ["departments.code"], name="fk_gov_users_dept_code"),
    )

    # ── audit_logs ─────────────────────────────────────────────────────────────
    op.create_table(
        "audit_logs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("complaint_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor", sa.String(200), nullable=True),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("old_value", postgresql.JSONB(), nullable=True),
        sa.Column("new_value", postgresql.JSONB(), nullable=True),
        sa.Column("ip_address", sa.String(50), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["complaint_id"],
            ["complaints.id"],
            name="fk_audit_logs_complaint_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_audit_logs_complaint_id", "audit_logs", ["complaint_id"])
    op.create_index("ix_audit_created_at", "audit_logs", ["created_at"])

    # ── status_events ──────────────────────────────────────────────────────────
    op.create_table(
        "status_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("complaint_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("from_status", sa.String(30), nullable=True),
        sa.Column("to_status", sa.String(30), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(200), nullable=True, server_default="system"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["complaint_id"],
            ["complaints.id"],
            name="fk_status_events_complaint_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_status_events_complaint_id", "status_events", ["complaint_id"])


def downgrade() -> None:
    op.drop_table("status_events")
    op.drop_table("audit_logs")
    op.drop_table("gov_users")
    op.drop_table("complaints")
    op.drop_table("routing_rules")
    op.drop_table("departments")
