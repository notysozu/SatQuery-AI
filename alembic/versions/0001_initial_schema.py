"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-08

"""
from __future__ import annotations

from typing import Sequence, Union

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Enable PostGIS extension (idempotent).
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    # ── Enum types (idempotent creation) ──────────────────────────────────────
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE sessionstate AS ENUM ('active', 'idle', 'closed');
        EXCEPTION WHEN duplicate_object THEN null;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE imagemodality AS ENUM ('optical', 'sar', 'multispectral', 'hyperspectral', 'unknown');
        EXCEPTION WHEN duplicate_object THEN null;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE workflowtype AS ENUM ('vqa', 'captioning', 'grounding', 'change_detection', 'sar_fusion');
        EXCEPTION WHEN duplicate_object THEN null;
        END $$;
    """)

    # ── users ─────────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("hashed_password", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    # ── sessions ──────────────────────────────────────────────────────────────
    op.create_table(
        "sessions",
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("state", postgresql.ENUM("active", "idle", "closed", name="sessionstate", create_type=False), nullable=False),
        sa.Column("conversation_history", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"], unique=False)

    # ── image_assets ──────────────────────────────────────────────────────────
    op.create_table(
        "image_assets",
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("uri", sa.String(), nullable=False),
        sa.Column(
            "modality",
            postgresql.ENUM("optical", "sar", "multispectral", "hyperspectral", "unknown", name="imagemodality", create_type=False),
            nullable=False,
        ),
        sa.Column("crs", sa.String(), nullable=True),
        sa.Column(
            "bbox",
            geoalchemy2.types.Geometry(geometry_type="POLYGON", srid=4326),
            nullable=True,
        ),
        sa.Column("acquisition_time", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("band_count", sa.Integer(), nullable=True),
        sa.Column("file_size_bytes", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("asset_id"),
    )

    # ── queries ───────────────────────────────────────────────────────────────
    op.create_table(
        "queries",
        sa.Column("query_id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("referenced_assets", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("trace", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.session_id"]),
        sa.PrimaryKeyConstraint("query_id"),
    )
    op.create_index("ix_queries_session_id", "queries", ["session_id"], unique=False)

    # ── analysis_runs ─────────────────────────────────────────────────────────
    op.create_table(
        "analysis_runs",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("query_id", sa.String(), nullable=False),
        sa.Column(
            "workflow",
            postgresql.ENUM("vqa", "captioning", "grounding", "change_detection", "sar_fusion", name="workflowtype", create_type=False),
            nullable=True,
        ),
        sa.Column("plan", sa.String(), nullable=True),
        sa.Column("tools_used", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("trace", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["query_id"], ["queries.query_id"]),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("ix_analysis_runs_query_id", "analysis_runs", ["query_id"], unique=False)

    # ── findings ──────────────────────────────────────────────────────────────
    op.create_table(
        "findings",
        sa.Column("finding_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column(
            "geometry",
            geoalchemy2.types.Geometry(geometry_type="GEOMETRY", srid=4326),
            nullable=True,
        ),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("answer", sa.String(), nullable=True),
        sa.Column("properties", postgresql.JSONB(astext_type=sa.Text()), nullable=True, server_default="{}"),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["analysis_runs.run_id"]),
        sa.PrimaryKeyConstraint("finding_id"),
    )
    op.create_index("ix_findings_run_id", "findings", ["run_id"], unique=False)

    # ── model_runs ────────────────────────────────────────────────────────────
    op.create_table(
        "model_runs",
        sa.Column("model_run_id", sa.String(), nullable=False),
        sa.Column("model_id", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("input_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("output_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("model_run_id"),
    )
    op.create_index("ix_model_runs_model_id", "model_runs", ["model_id"], unique=False)

    # ── reports ───────────────────────────────────────────────────────────────
    op.create_table(
        "reports",
        sa.Column("report_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("summary", sa.String(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("export_uri", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["analysis_runs.run_id"]),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.session_id"]),
        sa.PrimaryKeyConstraint("report_id"),
    )
    op.create_index("ix_reports_run_id", "reports", ["run_id"], unique=False)
    op.create_index("ix_reports_session_id", "reports", ["session_id"], unique=False)


def downgrade() -> None:
    op.drop_table("reports")
    op.drop_table("model_runs")
    op.drop_table("findings")
    op.drop_table("analysis_runs")
    op.drop_table("queries")
    op.drop_table("image_assets")
    op.drop_table("sessions")
    op.drop_table("users")

    # Drop enum types
    op.execute("DROP TYPE IF EXISTS workflowtype")
    op.execute("DROP TYPE IF EXISTS imagemodality")
    op.execute("DROP TYPE IF EXISTS sessionstate")
