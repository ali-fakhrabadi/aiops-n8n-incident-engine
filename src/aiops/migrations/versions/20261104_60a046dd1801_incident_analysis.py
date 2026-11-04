"""incident analysis

Revision ID: 60a046dd1801
Revises: bd357d905933
Create Date: 2026-11-04 21:27:22.680498+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "60a046dd1801"
down_revision: str | Sequence[str] | None = "bd357d905933"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The default exists only to fill existing rows; new rows always set the column.
    op.add_column(
        "incidents",
        sa.Column(
            "analysis_status", sa.String(length=16), nullable=False, server_default="pending"
        ),
    )
    op.add_column(
        "incidents",
        sa.Column("analysis", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "incidents",
        sa.Column("analysis_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Incidents whose log context is still pending will be analysed once it arrives; the
    # others were handled before analysis existed and will never get the event for it.
    op.execute("UPDATE incidents SET analysis_status = 'skipped' WHERE context_status <> 'pending'")
    op.alter_column("incidents", "analysis_status", server_default=None)
    op.create_check_constraint(
        op.f("ck_incidents_analysis_status"),
        "incidents",
        "analysis_status IN ('pending', 'completed', 'failed', 'skipped')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_incidents_analysis_status"), "incidents", type_="check")
    op.drop_column("incidents", "analysis_updated_at")
    op.drop_column("incidents", "analysis")
    op.drop_column("incidents", "analysis_status")
