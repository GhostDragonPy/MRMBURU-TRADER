"""Demo-orders intent tables. Paper schema unchanged."""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('demo_control',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('rollout', sa.String(16), nullable=False, server_default='disabled'),
        sa.Column('blocked', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('protection_failed', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('canary_day', sa.String(16), nullable=True),
        sa.Column('reason', sa.Text(), nullable=False, server_default='initial'),
        sa.Column('changed_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('id = 1', name='demo_control_singleton'),
        sa.CheckConstraint("rollout IN ('disabled','shadow','canary','enabled')", name='demo_rollout_states'))
    op.create_table('demo_order_intents',
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
                           name='demo_intent_status'))
    op.create_index('ix_demo_order_intents_created_at', 'demo_order_intents', ['created_at'])
    op.create_table('demo_owned_positions',
        sa.Column('position_id', sa.String(64), primary_key=True),
        sa.Column('signal_id', sa.String(64), sa.ForeignKey('demo_order_intents.signal_id'),
                  nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))

def downgrade():
    op.drop_table('demo_owned_positions')
    op.drop_table('demo_order_intents')
    op.drop_table('demo_control')
