"""Discord sandbox, idempotency and automatic paper control."""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('automatic_paper_control',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('paused', sa.Boolean(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('changed_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('id = 1', name='automatic_paper_control_singleton'))
    op.create_table('discord_interactions',
        sa.Column('interaction_id', sa.String(32), primary_key=True),
        sa.Column('user_id', sa.String(32), nullable=False),
        sa.Column('action', sa.String(64), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('response', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_discord_interactions_created_at','discord_interactions',['created_at'])
    op.create_table('discord_paper_positions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('account_id', sa.String(36), sa.ForeignKey('accounts.id'), nullable=False),
        sa.Column('signal_id', sa.String(36), sa.ForeignKey('signals.id'), nullable=False, unique=True),
        sa.Column('side', sa.String(4), nullable=False),
        sa.Column('symbol', sa.String(16), nullable=False),
        sa.Column('units', sa.Numeric(24,8), nullable=False),
        sa.Column('entry', sa.Numeric(24,8), nullable=False),
        sa.Column('stop_loss', sa.Numeric(24,8), nullable=False),
        sa.Column('take_profit', sa.Numeric(24,8), nullable=False),
        sa.Column('risk_amount', sa.Numeric(24,8), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('exit_price', sa.Numeric(24,8), nullable=True),
        sa.Column('pnl', sa.Numeric(24,8), nullable=True),
        sa.Column('close_reason', sa.Text(), nullable=True),
        sa.CheckConstraint("side IN ('buy','sell')", name='discord_position_side'),
        sa.CheckConstraint("symbol = 'EURUSD'", name='discord_position_eurusd'),
        sa.CheckConstraint('units > 0', name='discord_position_positive_units'))
    op.create_index('ix_discord_paper_positions_account_id','discord_paper_positions',['account_id'])
    op.create_index('ix_discord_paper_positions_closed_at','discord_paper_positions',['closed_at'])
def downgrade():
    op.drop_table('discord_paper_positions')
    op.drop_table('discord_interactions')
    op.drop_table('automatic_paper_control')
