"""DEMO preflight snapshot and durable canary/armed markers."""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('demo_control', sa.Column('canary_consumed', sa.Boolean(),
                                           nullable=False, server_default=sa.false()))
    op.add_column('demo_control', sa.Column('armed_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        'demo_preflight',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('account_id', sa.String(64), nullable=False),
        sa.Column('is_live', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('host', sa.String(128), nullable=False),
        sa.Column('symbol', sa.String(16), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.Column('checked_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('detail', sa.JSON(), nullable=False),
        sa.CheckConstraint('id = 1', name='demo_preflight_singleton'),
        sa.CheckConstraint("status IN ('passed','failed')", name='demo_preflight_status'),
        sa.CheckConstraint('is_live = false', name='demo_preflight_not_live'),
    )


def downgrade():
    op.drop_table('demo_preflight')
    op.drop_column('demo_control', 'armed_at')
    op.drop_column('demo_control', 'canary_consumed')
