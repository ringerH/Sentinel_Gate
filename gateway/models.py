import datetime
from sqlalchemy import Column, String, Integer, DateTime, JSON
from database import Base

class Agent(Base):
    __tablename__ = "agents"
    agent_id = Column(String, primary_key=True, index=True)
    role = Column(String, nullable=False)
    status = Column(String, default="active")  # active, suspended, revoked
    owner = Column(String, nullable=False)
    secret_hash = Column(String, nullable=False)  # secret string to verify HMAC signatures
    permission_overrides = Column(JSON, nullable=True)

class PermissionRule(Base):
    __tablename__ = "permission_rules"
    rule_id = Column(String, primary_key=True, index=True)
    role = Column(String, index=True, nullable=False)
    action_type = Column(String, nullable=False)
    resource_scope = Column(String, nullable=False)
    constraints = Column(JSON, nullable=True)  # max_amount, allowed_hours, allowed_counterparties, escalation_threshold
    effect = Column(String, default="allow")  # allow or deny

class SpendPolicy(Base):
    __tablename__ = "spend_policies"
    scope = Column(String, primary_key=True)  # "agent:<agent_id>" or "fleet"
    period = Column(String, primary_key=True)  # "daily", "monthly"
    limit_amount = Column(Integer, nullable=False)  # stored in minor units (cents)
    reset_behavior = Column(String, default="automatic")

class AuditLogEntry(Base):
    __tablename__ = "audit_log_entries"
    request_id = Column(String, primary_key=True, index=True)
    agent_id = Column(String, nullable=False)
    action_type = Column(String, nullable=False)
    decision = Column(String, nullable=False)  # ALLOW, DENY, ESCALATE
    reason = Column(String, nullable=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)

class PendingApproval(Base):
    __tablename__ = "pending_approvals"
    request_id = Column(String, primary_key=True, index=True)
    agent_id = Column(String, nullable=False)
    action_type = Column(String, nullable=False)
    amount = Column(Integer, nullable=False)  # cents
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    status = Column(String, default="pending")  # pending, approved, denied
