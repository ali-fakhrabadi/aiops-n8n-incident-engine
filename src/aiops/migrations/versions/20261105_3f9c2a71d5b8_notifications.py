"""notifications

Revision ID: 3f9c2a71d5b8
Revises: 60a046dd1801
Create Date: 2026-11-05 07:10:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3f9c2a71d5b8"
down_revision: str | Sequence[str] | None = "60a046dd1801"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reported_status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("handed_off_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handoff_attempts", sa.Integer(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("route", sa.String(length=32), nullable=True),
        sa.Column("channel", sa.String(length=32), nullable=True),
        sa.Column("channel_message_id", sa.String(length=64), nullable=True),
        sa.Column("execution_id", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("handoff_attempts >= 0", name=op.f("ck_notifications_handoff_attempts")),
        sa.CheckConstraint("kind IN ('firing', 'resolved')", name=op.f("ck_notifications_kind")),
        sa.CheckConstraint(
            "reported_status IN ('firing', 'resolved')",
            name=op.f("ck_notifications_reported_status"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'delivered', 'suppressed', 'failed')",
            name=op.f("ck_notifications_status"),
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name=op.f("fk_notifications_incident_id_incidents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        sa.UniqueConstraint("incident_id", "kind", name="uq_notifications_incident_id_kind"),
    )


def downgrade() -> None:
    op.drop_table("notifications")
