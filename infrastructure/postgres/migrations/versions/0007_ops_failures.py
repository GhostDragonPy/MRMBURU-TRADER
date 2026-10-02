"""Ops failure log for recurring infra faults. Paper schema unchanged."""
from alembic import op
import sqlalchemy as sa

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'ops_failures',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('kind', sa.String(64), nullable=False),
        sa.Column('code', sa.String(64), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('detail', sa.JSON(), nullable=False),
        sa.Column('occurrences', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_ops_failures_kind', 'ops_failures', ['kind'])
    op.create_index('ix_ops_failures_code', 'ops_failures', ['code'])
    op.create_index('ix_ops_failures_first_seen_at', 'ops_failures', ['first_seen_at'])
    op.create_index('ix_ops_failures_last_seen_at', 'ops_failures', ['last_seen_at'])
    op.create_index('ix_ops_failures_resolved_at', 'ops_failures', ['resolved_at'])
    op.create_index('ix_ops_failures_kind_code_last', 'ops_failures',
                    ['kind', 'code', 'last_seen_at'])


def downgrade():
    op.drop_index('ix_ops_failures_kind_code_last', table_name='ops_failures')
    op.drop_index('ix_ops_failures_resolved_at', table_name='ops_failures')
    op.drop_index('ix_ops_failures_last_seen_at', table_name='ops_failures')
    op.drop_index('ix_ops_failures_first_seen_at', table_name='ops_failures')
    op.drop_index('ix_ops_failures_code', table_name='ops_failures')
    op.drop_index('ix_ops_failures_kind', table_name='ops_failures')
    op.drop_table('ops_failures')
