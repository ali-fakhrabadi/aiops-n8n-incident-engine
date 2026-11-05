"""notification kind page

Revision ID: 9b4e1d7c2a60
Revises: 3f9c2a71d5b8
Create Date: 2026-11-06 06:00:00.000000+00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "9b4e1d7c2a60"
down_revision: str | Sequence[str] | None = "3f9c2a71d5b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_notifications_kind"), "notifications", type_="check")
    op.create_check_constraint(
        op.f("ck_notifications_kind"), "notifications", "kind IN ('page', 'firing', 'resolved')"
    )


def downgrade() -> None:
    # Pages cannot be expressed in the old schema; they go with it.
    op.execute("DELETE FROM notifications WHERE kind = 'page'")
    op.drop_constraint(op.f("ck_notifications_kind"), "notifications", type_="check")
    op.create_check_constraint(
        op.f("ck_notifications_kind"), "notifications", "kind IN ('firing', 'resolved')"
    )
