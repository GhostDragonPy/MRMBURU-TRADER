"""Independent paper ledger and append-only event history."""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('paper_ledgers',
        sa.Column('account_id', sa.String(36), sa.ForeignKey('accounts.id'), primary_key=True),
        sa.Column('state', sa.JSON(), nullable=False))
    op.create_table('paper_events',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('account_id', sa.String(36), sa.ForeignKey('accounts.id'), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_paper_events_account_id', 'paper_events', ['account_id'])
    op.create_index('ix_paper_events_created_at', 'paper_events', ['created_at'])

def downgrade():
    op.drop_table('paper_events')
    op.drop_table('paper_ledgers')
