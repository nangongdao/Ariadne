"""graph_runs_mvp

Revision ID: g8h9i0j1k2l3
Revises: f7a8b9c0d1e2
Create Date: 2026-09-03 10:00:00.000000

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'g8h9i0j1k2l3'
down_revision = 'f7a8b9c0d1e2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'graph_runs',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('project_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('graph_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('state', sa.String(30), nullable=False),
        sa.Column('inputs', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('outputs', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('node_states', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('errors', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('finished_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('NOW()')),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['graph_id'], ['workflow_graphs.id'], ondelete='CASCADE'),
    )

    op.create_index('ix_graph_runs_project_created', 'graph_runs', ['project_id', sa.text('created_at DESC')])
    op.create_index(
        'ix_graph_runs_state',
        'graph_runs',
        ['state'],
        postgresql_where=sa.text("state IN ('PENDING', 'RUNNING')")
    )


def downgrade() -> None:
    op.drop_index('ix_graph_runs_state', table_name='graph_runs')
    op.drop_index('ix_graph_runs_project_created', table_name='graph_runs')
    op.drop_table('graph_runs')
