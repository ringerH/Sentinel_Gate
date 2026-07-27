from contextlib import asynccontextmanager
import datetime
from fastapi import FastAPI, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
import redis

from database import Base, engine, get_db, SessionLocal
from models import Agent, PermissionRule, SpendPolicy, AuditLogEntry, PendingApproval
from auth import authenticate_request
from policy import evaluate_policy
from spend import check_and_reserve_spend, is_spend_limit_exceeded, get_limits
from config import REDIS_URL

redis_client = redis.from_url(REDIS_URL, decode_responses=True)

def log_decision(db: Session, request_id: str, agent_id: str, action_type: str, decision: str, reason: str):
    """Utility to synchronously save or update decision logs in SQLite on request_id PK."""
    entry = db.query(AuditLogEntry).filter(AuditLogEntry.request_id == request_id).first()
    if entry:
        entry.decision = decision
        entry.reason = reason
        entry.timestamp = datetime.datetime.utcnow()
    else:
        entry = AuditLogEntry(
            request_id=request_id,
            agent_id=agent_id,
            action_type=action_type,
            decision=decision,
            reason=reason,
            timestamp=datetime.datetime.utcnow()
        )
        db.add(entry)
    db.commit()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure tables are built
    Base.metadata.create_all(bind=engine)
    
    # Seed default agents, policies, and spend limits
    db = SessionLocal()
    try:
        # 1. Seed Agents
        default_agents = [
            Agent(
                agent_id="trading-agent",
                role="trader",
                status="active",
                owner="FinOps-Alpha",
                secret_hash="agent-secret-key-abc"
            ),
            Agent(
                agent_id="suspended-agent",
                role="trader",
                status="suspended",
                owner="FinOps-Alpha",
                secret_hash="suspended-secret-key"
            )
        ]
        for agent in default_agents:
            if not db.query(Agent).filter(Agent.agent_id == agent.agent_id).first():
                db.add(agent)
                
        # 2. Seed Policy Rules for "trader" role
        default_rules = [
            PermissionRule(
                rule_id="rule-trader-stocks",
                role="trader",
                action_type="trade",
                resource_scope="stock:",
                constraints={
                    "max_amount": 1000000,          # Max limit per trade is $10k (1,000,000 cents)
                    "escalation_threshold": 100000   # Escalate to human if trade > $1k (100,000 cents)
                },
                effect="allow"
            ),
            PermissionRule(
                rule_id="rule-trader-deny-risky",
                role="trader",
                action_type="trade",
                resource_scope="stock:RISK",        # Explicitly block trading ticker stock:RISK
                constraints={},
                effect="deny"
            )
        ]
        for rule in default_rules:
            if not db.query(PermissionRule).filter(PermissionRule.rule_id == rule.rule_id).first():
                db.add(rule)
                
        # 3. Seed Spend Policies
        default_spend_policies = [
            # Agent daily limit = $5k (500,000 cents)
            SpendPolicy(
                scope="agent:trading-agent",
                period="daily",
                limit_amount=500000,
                reset_behavior="automatic"
            ),
            # Fleet daily limit = $20k (2,000,000 cents)
            SpendPolicy(
                scope="fleet",
                period="daily",
                limit_amount=2000000,
                reset_behavior="automatic"
            )
        ]
        for policy in default_spend_policies:
            if not db.query(SpendPolicy).filter(
                SpendPolicy.scope == policy.scope,
                SpendPolicy.period == policy.period
            ).first():
                db.add(policy)
                
        db.commit()
    finally:
        db.close()
    yield

app = FastAPI(
    title="Governance Layer for Financial Agents - Demo API",
    lifespan=lifespan
)

# CORS Middleware to allow operator dashboard interaction
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health_check():
    return {"status": "healthy", "service": "governance-gateway"}

@app.post("/evaluate")
async def evaluate_action(request: Request, db: Session = Depends(get_db)):
    """
    Evaluates agent action. If decision is ALLOW/DENY, returns it immediately.
    If ESCALATE, registers a PendingApproval request in SQLite.
    All decisions are synchronously logged to SQLite.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload"
        )
        
    # 1. Authentication (HMAC, freshness, and replay)
    agent = authenticate_request(payload, db)
    
    request_id = payload.get("request_id")
    action_type = payload.get("action_type")
    resource = payload.get("resource")
    amount_cents = int(payload.get("amount_cents", 0))
    timestamp = int(payload.get("timestamp", 0))
    
    # 2. Kill Switch Check (Fleet-wide block)
    fleet_kill = redis_client.get("killswitch:fleet")
    if fleet_kill == "1":
        log_decision(db, request_id, agent.agent_id, action_type, "DENY", "Fleet-wide Emergency Stop is active")
        return {
            "decision": "DENY",
            "request_id": request_id,
            "reason": "Fleet-wide Emergency Stop is active",
            "remaining_budget_cents": 0
        }
        
    # 3. Policy Rule Evaluation (RBAC + ABAC)
    policy_decision, policy_reason = evaluate_policy(
        agent, action_type, resource, amount_cents, timestamp, db
    )
    
    # Early budget cap check for ALLOW or ESCALATE decisions
    if policy_decision in ["ALLOW", "ESCALATE"]:
        if is_spend_limit_exceeded(agent.agent_id, amount_cents, db):
            today_str = datetime.date.today().isoformat()
            agent_key = f"spend:agent:{agent.agent_id}:daily:{today_str}"
            current_agent_spend = int(redis_client.get(agent_key) or 0)
            agent_limit, _ = get_limits(agent.agent_id, db)
            remaining_budget = max(0, agent_limit - current_agent_spend)
            
            log_decision(db, request_id, agent.agent_id, action_type, "DENY", "Transaction exceeds daily allowed spend limits")
            return {
                "decision": "DENY",
                "request_id": request_id,
                "reason": "Transaction exceeds daily allowed spend limits",
                "remaining_budget_cents": remaining_budget
            }
            
    if policy_decision == "ESCALATE":
        # Create a pending approval record (expires in 30 seconds)
        expires_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=30)
        pending = PendingApproval(
            request_id=request_id,
            agent_id=agent.agent_id,
            action_type=action_type,
            amount=amount_cents,
            created_at=datetime.datetime.utcnow(),
            expires_at=expires_at,
            status="pending"
        )
        db.add(pending)
        db.commit()
        
        log_decision(db, request_id, agent.agent_id, action_type, "ESCALATE", policy_reason)
        return {
            "decision": "ESCALATE",
            "request_id": request_id,
            "reason": policy_reason,
            "remaining_budget_cents": 0
        }
        
    if policy_decision == "DENY":
        log_decision(db, request_id, agent.agent_id, action_type, "DENY", policy_reason)
        return {
            "decision": "DENY",
            "request_id": request_id,
            "reason": policy_reason,
            "remaining_budget_cents": 0
        }
        
    # 4. Atomic Spend Cap Reservation (Only if allowed by policy)
    spend_allowed, remaining_budget = check_and_reserve_spend(
        agent.agent_id, amount_cents, db
    )
    
    if not spend_allowed:
        log_decision(db, request_id, agent.agent_id, action_type, "DENY", "Transaction exceeds daily allowed spend limits")
        return {
            "decision": "DENY",
            "request_id": request_id,
            "reason": "Transaction exceeds daily allowed spend limits",
            "remaining_budget_cents": remaining_budget
        }
        
    # Decision logged successfully
    log_decision(db, request_id, agent.agent_id, action_type, "ALLOW", policy_reason)
    return {
        "decision": "ALLOW",
        "request_id": request_id,
        "reason": policy_reason,
        "remaining_budget_cents": remaining_budget
    }

# ----------------- OPERATOR / DASHBOARD API ROUTES -----------------

@app.get("/logs")
def get_audit_logs(db: Session = Depends(get_db)):
    """Fetch the latest 50 decision log entries."""
    return db.query(AuditLogEntry).order_by(AuditLogEntry.timestamp.desc()).limit(50).all()

@app.get("/approvals")
def get_pending_approvals(db: Session = Depends(get_db)):
    """Retrieve active pending approvals, auto-denying any expired items."""
    now = datetime.datetime.utcnow()
    # Find active expired items
    expired = db.query(PendingApproval).filter(
        PendingApproval.status == "pending",
        PendingApproval.expires_at < now
    ).all()
    
    for appr in expired:
        appr.status = "denied"
        log_decision(
            db, appr.request_id, appr.agent_id, appr.action_type, 
            "DENY", "Escalation pending approval timed out (auto-denied)"
        )
    if expired:
        db.commit()
        
    return db.query(PendingApproval).filter(PendingApproval.status == "pending").all()

@app.post("/approvals/{request_id}/resolve")
def resolve_approval(request_id: str, payload: dict, db: Session = Depends(get_db)):
    """Resolve an escalation by either approving (ALLOW) or denying it."""
    appr = db.query(PendingApproval).filter(PendingApproval.request_id == request_id).first()
    if not appr:
        raise HTTPException(status_code=404, detail="Approval request not found")
        
    if appr.status != "pending":
        raise HTTPException(status_code=400, detail="Approval is already resolved")
        
    if appr.expires_at < datetime.datetime.utcnow():
        appr.status = "denied"
        db.commit()
        log_decision(
            db, appr.request_id, appr.agent_id, appr.action_type,
            "DENY", "Operator attempted approval after expiration timeout"
        )
        raise HTTPException(status_code=400, detail="Approval has expired")
        
    approved = payload.get("approved", False)
    
    if approved:
        # Atomic spend reservation
        allowed, remaining = check_and_reserve_spend(appr.agent_id, appr.amount, db)
        if not allowed:
            appr.status = "denied"
            db.commit()
            log_decision(
                db, appr.request_id, appr.agent_id, appr.action_type,
                "DENY", "Operator approved but transaction exceeds spend limits"
            )
            return {"status": "denied", "reason": "Approved but exceeds spend limits"}
            
        appr.status = "approved"
        log_decision(db, appr.request_id, appr.agent_id, appr.action_type, "ALLOW", "Operator Approved Escalation")
    else:
        appr.status = "denied"
        log_decision(db, appr.request_id, appr.agent_id, appr.action_type, "DENY", "Operator Denied Escalation")
        
    db.commit()
    return {"status": appr.status}

@app.post("/killswitch")
def set_killswitch(payload: dict):
    """Set the fleet-wide emergency stop state in Redis."""
    active = "1" if payload.get("active", False) else "0"
    redis_client.set("killswitch:fleet", active)
    return {"killswitch_fleet_active": active == "1"}

@app.get("/metrics")
def get_dashboard_metrics(db: Session = Depends(get_db)):
    """Fetch live counters and budget states."""
    today_str = datetime.date.today().isoformat()
    agent_key = f"spend:agent:trading-agent:daily:{today_str}"
    fleet_key = f"spend:fleet:daily:{today_str}"
    
    current_agent_spend = int(redis_client.get(agent_key) or 0)
    current_fleet_spend = int(redis_client.get(fleet_key) or 0)
    
    allowed = db.query(AuditLogEntry).filter(AuditLogEntry.decision == "ALLOW").count()
    denied = db.query(AuditLogEntry).filter(AuditLogEntry.decision == "DENY").count()
    escalated = db.query(AuditLogEntry).filter(AuditLogEntry.decision == "ESCALATE").count()
    
    agent_limit, fleet_limit = get_limits("trading-agent", db)
    
    return {
        "agent_spend_cents": current_agent_spend,
        "agent_limit_cents": agent_limit,
        "fleet_spend_cents": current_fleet_spend,
        "fleet_limit_cents": fleet_limit,
        "allowed_count": allowed,
        "denied_count": denied,
        "escalated_count": escalated,
        "killswitch_fleet_active": redis_client.get("killswitch:fleet") == "1"
    }

# ----------------- AGENT STATUS POLLING ROUTE -----------------

@app.get("/evaluate/{request_id}")
def get_action_status(request_id: str, db: Session = Depends(get_db)):
    """Allows calling agents to poll the outcome of an escalated action request."""
    # 1. Check if logged as resolved final decision
    log_entry = db.query(AuditLogEntry).filter(
        AuditLogEntry.request_id == request_id,
        AuditLogEntry.decision.in_(["ALLOW", "DENY"])
    ).first()
    
    if log_entry:
        return {
            "request_id": request_id,
            "status": "allowed" if log_entry.decision == "ALLOW" else "denied",
            "reason": log_entry.reason
        }
        
    # 2. Check active pending approval status
    approval = db.query(PendingApproval).filter(PendingApproval.request_id == request_id).first()
    if approval:
        if approval.expires_at < datetime.datetime.utcnow():
            approval.status = "denied"
            db.commit()
            log_decision(db, request_id, approval.agent_id, approval.action_type, "DENY", "Escalation timed out (auto-denied)")
            return {
                "request_id": request_id,
                "status": "denied",
                "reason": "Escalation timed out (auto-denied)"
            }
        return {
            "request_id": request_id,
            "status": "pending",
            "reason": "Awaiting human operator approval"
        }
        
    raise HTTPException(status_code=404, detail="Request transaction ID not found")
