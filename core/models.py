from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase

def uid(): return str(uuid4())
def utcnow(): return datetime.now(timezone.utc)
class Base(DeclarativeBase): pass

class Account(Base):
    __tablename__ = 'accounts'
    id = Column(String(36), primary_key=True, default=uid)
    name = Column(String(128), nullable=False, unique=True)
    mode = Column(String(16), nullable=False, default='paper')
    currency = Column(String(3), nullable=False, default='USD')
    platform = Column(String(32), nullable=False, default='synthetic')
    prop_firm = Column(String(64), nullable=True)
    challenge_type = Column(String(64), nullable=True)
    enabled = Column(Boolean, nullable=False, default=False)
    initial_balance = Column(Numeric(24,8), nullable=False)
    risk_policy = Column(JSON, nullable=False)
    prop_rules = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (CheckConstraint("mode = 'paper'", name='account_paper_only'),
                      CheckConstraint('initial_balance > 0', name='positive_initial_balance'))

class AccountSnapshot(Base):
    __tablename__ = 'account_snapshots'
    id = Column(String(36), primary_key=True, default=uid)
    account_id = Column(ForeignKey('accounts.id'), nullable=False, index=True)
    observed_at = Column(DateTime(timezone=True), nullable=False, index=True)
    risk_day = Column(Date, nullable=False)
    # Validated AccountState JSON includes balance, equity and all counters.
    state = Column(JSON, nullable=False)
    source = Column(String(32), nullable=False, default='synthetic')

class StrategyVersion(Base):
    __tablename__ = 'strategy_versions'
    id = Column(String(36), primary_key=True, default=uid)
    name = Column(String(128), nullable=False)
    version = Column(Integer, nullable=False)
    status = Column(String(32), nullable=False, default='research')
    specification = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (UniqueConstraint('name','version'),
                      CheckConstraint("status IN ('research','backtest','validation','walk_forward','paper','approved')", name='strategy_status'))

class SignalRecord(Base):
    __tablename__ = 'signals'
    id = Column(String(36), primary_key=True, default=uid)
    account_id = Column(ForeignKey('accounts.id'), nullable=False, index=True)
    strategy_id = Column(ForeignKey('strategy_versions.id'), nullable=True)
    request_key = Column(String(128), nullable=False)
    payload = Column(JSON, nullable=False)
    market_context = Column(JSON, nullable=False)
    snapshot_id = Column(ForeignKey('account_snapshots.id'), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (UniqueConstraint('account_id','request_key'),)

class RiskDecisionRecord(Base):
    __tablename__ = 'risk_decisions'
    id = Column(String(36), primary_key=True, default=uid)
    signal_id = Column(ForeignKey('signals.id'), nullable=False, unique=True)
    result = Column(JSON, nullable=False)
    policy_snapshot = Column(JSON, nullable=False)
    rules_snapshot = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

class KillSwitch(Base):
    __tablename__ = 'kill_switch'
    id = Column(Integer, primary_key=True)
    active = Column(Boolean, nullable=False, default=True)
    reason = Column(Text, nullable=False)
    changed_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (CheckConstraint('id = 1', name='global_singleton'),)

class AuditEvent(Base):
    __tablename__ = 'audit_events'
    id = Column(String(36), primary_key=True, default=uid)
    actor = Column(String(64), nullable=False)
    action = Column(String(128), nullable=False)
    payload = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)

class JournalEntry(Base):
    __tablename__ = 'journal_entries'
    id = Column(String(36), primary_key=True, default=uid)
    account_id = Column(ForeignKey('accounts.id'), nullable=False, index=True)
    signal_id = Column(ForeignKey('signals.id'), nullable=False)
    # Reserved for phase 5/9; no orders are inserted by v0.2.
    trade_context = Column(JSON, nullable=False)
    pnl = Column(Numeric(24,8), nullable=True)
    result_r = Column(Numeric(24,8), nullable=True)
    rule_compliant = Column(Boolean, nullable=True)
    outcome = Column(String(16), nullable=True)
    error_type = Column(String(32), nullable=True)
    entry_reason = Column(Text, nullable=True)
    exit_reason = Column(Text, nullable=True)
    opened_at = Column(DateTime(timezone=True), nullable=False)
    closed_at = Column(DateTime(timezone=True), nullable=True)

class EconomicEvent(Base):
    __tablename__ = 'economic_events'
    id = Column(String(36), primary_key=True, default=uid)
    provider_key = Column(String(256), nullable=False, unique=True)
    name = Column(String(128), nullable=False)
    currency = Column(String(3), nullable=False)
    impact = Column(String(16), nullable=False)
    occurs_at = Column(DateTime(timezone=True), nullable=False, index=True)
    retrieved_at = Column(DateTime(timezone=True), nullable=False)
    source = Column(Text, nullable=False)

class KnowledgeSource(Base):
    __tablename__ = 'knowledge_sources'
    id = Column(String(36), primary_key=True, default=uid)
    url = Column(Text, nullable=False, unique=True)
    transcript = Column(Text, nullable=True)
    content_hash = Column(String(64), nullable=True)
    extracted_rules = Column(JSON, nullable=True)
    contradictions = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

class BacktestRun(Base):
    __tablename__ = 'backtest_runs'
    id = Column(String(36), primary_key=True, default=uid)
    strategy_id = Column(ForeignKey('strategy_versions.id'), nullable=False)
    dataset_hash = Column(String(64), nullable=False)
    split = Column(String(16), nullable=False)
    start_at = Column(DateTime(timezone=True), nullable=False)
    end_at = Column(DateTime(timezone=True), nullable=False)
    assumptions = Column(JSON, nullable=False)
    metrics = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (CheckConstraint("split IN ('train','validation','test','walk_forward')", name='dataset_split'),
                      CheckConstraint('end_at > start_at', name='valid_test_window'))

class ImprovementProposal(Base):
    __tablename__ = 'improvement_proposals'
    id = Column(String(36), primary_key=True, default=uid)
    strategy_id = Column(ForeignKey('strategy_versions.id'), nullable=False)
    proposal = Column(JSON, nullable=False)
    status = Column(String(32), nullable=False, default='proposed')
    approved_by = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
