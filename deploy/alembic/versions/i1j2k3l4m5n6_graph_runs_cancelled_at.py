"""Add cancelled_at field to graph_runs for Stage 3 cancellation API.

Revision ID: i1j2k3l4m5n6
Revises: h9i0j1k2l3m4
Create Date: 2026-09-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TIMESTAMP

# revision identifiers, used by Alembic.
revision: str = "i1j2k3l4m5n6"
down_revision: str | None = "h9i0j1k2l3m4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add cancelled_at column for cancellation API."""
    op.add_column(
        "graph_runs",
        sa.Column("cancelled_at", TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Remove cancelled_at column."""
    op.drop_column("graph_runs", "cancelled_at")
