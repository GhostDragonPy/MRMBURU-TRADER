"""Prop-sim control, preflight and order intents. Paper schema unchanged."""
from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'prop_sim_control',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('rollout', sa.String(16), nullable=False, server_default='disabled'),
        sa.Column('blocked', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('protection_failed', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('canary_day', sa.String(16), nullable=True),
        sa.Column('canary_consumed', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('armed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reason', sa.Text(), nullable=False, server_default='initial'),
        sa.Column('changed_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('id = 1', name='prop_sim_control_singleton'),
        sa.CheckConstraint("rollout IN ('disabled','shadow','canary','enabled')",
                           name='prop_sim_rollout_states'),
    )
    op.create_table(
        'prop_sim_preflight',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('account_id', sa.String(64), nullable=False),
        sa.Column('trader_login', sa.String(64), nullable=False),
        sa.Column('broker', sa.String(32), nullable=False),
        sa.Column('is_live', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('host', sa.String(128), nullable=False),
        sa.Column('symbol', sa.String(16), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.Column('checked_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('detail', sa.JSON(), nullable=False),
        sa.CheckConstraint('id = 1', name='prop_sim_preflight_singleton'),
        sa.CheckConstraint("status IN ('passed','failed')", name='prop_sim_preflight_status'),
        sa.CheckConstraint('is_live = true', name='prop_sim_preflight_live_infra'),
        sa.CheckConstraint("host = 'live.ctraderapi.com'", name='prop_sim_preflight_live_host'),
        sa.CheckConstraint("broker = 'FTMO'", name='prop_sim_preflight_broker'),
    )
    op.create_table(
        'prop_sim_order_intents',
        sa.Column('signal_id', sa.String(64), primary_key=True),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('request', sa.JSON(), nullable=False),
        sa.Column('response', sa.JSON(), nullable=False),
        sa.Column('broker_order_id', sa.String(64), nullable=True),
        sa.Column('position_id', sa.String(64), nullable=True),
        sa.Column('fill_price', sa.String(32), nullable=True),
        sa.Column('stop_loss', sa.String(32), nullable=True),
        sa.Column('take_profit', sa.String(32), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('reserved','shadow','sent','filled','uncertain','failed')",
                           name='prop_sim_intent_status'),
    )
    op.create_index('ix_prop_sim_order_intents_created_at', 'prop_sim_order_intents', ['created_at'])
    op.create_table(
        'prop_sim_owned_positions',
        sa.Column('position_id', sa.String(64), primary_key=True),
        sa.Column('signal_id', sa.String(64), sa.ForeignKey('prop_sim_order_intents.signal_id'),
                  nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table('prop_sim_owned_positions')
    op.drop_index('ix_prop_sim_order_intents_created_at', table_name='prop_sim_order_intents')
    op.drop_table('prop_sim_order_intents')
    op.drop_table('prop_sim_preflight')
    op.drop_table('prop_sim_control')
