import hmac
import hashlib
import json
import time
import uuid
import datetime
import pytest
import redis
from fastapi.testclient import TestClient

from main import app
from config import REDIS_URL
from database import SessionLocal
from models import AuditLogEntry, PendingApproval

client = TestClient(app)
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

def sign_payload(payload: dict, secret: str) -> dict:
    data = {k: v for k, v in payload.items() if k != "signature"}
    canonical_body = json.dumps(data, sort_keys=True, separators=(',', ':'))
    sig = hmac.new(
        secret.encode('utf-8'),
        canonical_body.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    payload["signature"] = sig
    return payload

@pytest.fixture(autouse=True)
def clean_redis_and_db():
    # Clean up Redis keys
    today_str = time.strftime("%Y-%m-%d")
    redis_client.delete(f"spend:agent:trading-agent:daily:{today_str}")
    redis_client.delete(f"spend:fleet:daily:{today_str}")
    redis_client.delete("killswitch:fleet")
    
    # Clean up SQLite tables
    db = SessionLocal()
    try:
        db.query(AuditLogEntry).delete()
        db.query(PendingApproval).delete()
        db.commit()
    finally:
        db.close()
        
    yield
    
    # Teardown
    redis_client.delete(f"spend:agent:trading-agent:daily:{today_str}")
    redis_client.delete(f"spend:fleet:daily:{today_str}")
    redis_client.delete("killswitch:fleet")

def test_escalation_flow_and_approval():
    req_id = str(uuid.uuid4())
    payload = {
        "agent_id": "trading-agent",
        "action_type": "trade",
        "resource": "stock:AAPL",
        "amount_cents": 150000,  # $1500 (triggers escalation)
        "timestamp": int(time.time()),
        "request_id": req_id
    }
    signed = sign_payload(payload, "agent-secret-key-abc")
    
    # 1. Dispatch request
    response = client.post("/evaluate", json=signed)
    assert response.status_code == 200
    assert response.json()["decision"] == "ESCALATE"
    
    # 2. Check pending approvals in DB
    db = SessionLocal()
    try:
        pending = db.query(PendingApproval).filter(PendingApproval.request_id == req_id).first()
        assert pending is not None
        assert pending.status == "pending"
        
        log = db.query(AuditLogEntry).filter(AuditLogEntry.request_id == req_id).first()
        assert log is not None
        assert log.decision == "ESCALATE"
    finally:
        db.close()
        
    # 3. Retrieve via GET /approvals
    appr_resp = client.get("/approvals")
    assert len(appr_resp.json()) == 1
    assert appr_resp.json()[0]["request_id"] == req_id
    
    # 4. Poll status (should be pending)
    poll_resp1 = client.get(f"/evaluate/{req_id}")
    assert poll_resp1.json()["status"] == "pending"
    
    # 5. Resolve as APPROVED
    resolve_resp = client.post(f"/approvals/{req_id}/resolve", json={"approved": True})
    assert resolve_resp.status_code == 200
    assert resolve_resp.json()["status"] == "approved"
    
    # 6. Check that spend was reserved in Redis
    today_str = time.strftime("%Y-%m-%d")
    spend_reserved = int(redis_client.get(f"spend:agent:trading-agent:daily:{today_str}") or 0)
    assert spend_reserved == 150000
    
    # 7. Check final logs
    db = SessionLocal()
    try:
        final_log = db.query(AuditLogEntry).filter(
            AuditLogEntry.request_id == req_id,
            AuditLogEntry.decision == "ALLOW"
        ).first()
        assert final_log is not None
        assert "Operator Approved" in final_log.reason
    finally:
        db.close()
        
    # 8. Poll status again (should be allowed)
    poll_resp2 = client.get(f"/evaluate/{req_id}")
    assert poll_resp2.json()["status"] == "allowed"

def test_escalation_flow_and_denial():
    req_id = str(uuid.uuid4())
    payload = {
        "agent_id": "trading-agent",
        "action_type": "trade",
        "resource": "stock:AAPL",
        "amount_cents": 150000,
        "timestamp": int(time.time()),
        "request_id": req_id
    }
    signed = sign_payload(payload, "agent-secret-key-abc")
    
    client.post("/evaluate", json=signed)
    
    # Resolve as DENIED
    resolve_resp = client.post(f"/approvals/{req_id}/resolve", json={"approved": False})
    assert resolve_resp.status_code == 200
    assert resolve_resp.json()["status"] == "denied"
    
    # Verify spend was NOT reserved in Redis
    today_str = time.strftime("%Y-%m-%d")
    spend_reserved = int(redis_client.get(f"spend:agent:trading-agent:daily:{today_str}") or 0)
    assert spend_reserved == 0
    
    # Poll status (should be denied)
    poll_resp = client.get(f"/evaluate/{req_id}")
    assert poll_resp.json()["status"] == "denied"
    assert "Operator Denied" in poll_resp.json()["reason"]

def test_escalation_timeout_autodeny():
    req_id = str(uuid.uuid4())
    payload = {
        "agent_id": "trading-agent",
        "action_type": "trade",
        "resource": "stock:AAPL",
        "amount_cents": 150000,
        "timestamp": int(time.time()),
        "request_id": req_id
    }
    signed = sign_payload(payload, "agent-secret-key-abc")
    client.post("/evaluate", json=signed)
    
    # Force backdate expiration time in database to simulate timeout
    db = SessionLocal()
    try:
        pending = db.query(PendingApproval).filter(PendingApproval.request_id == req_id).first()
        pending.expires_at = datetime.datetime.utcnow() - datetime.timedelta(seconds=1)
        db.commit()
    finally:
        db.close()
        
    # Poll status (triggers auto-denial)
    poll_resp = client.get(f"/evaluate/{req_id}")
    assert poll_resp.json()["status"] == "denied"
    assert "timed out" in poll_resp.json()["reason"]
