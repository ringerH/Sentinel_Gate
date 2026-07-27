import hmac
import hashlib
import json
import time
import uuid
import pytest
import redis
from fastapi.testclient import TestClient

from main import app
from config import REDIS_URL
from database import SessionLocal
from models import SpendPolicy

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
def clean_redis():
    # Clean up spend tracking and killswitch before each test
    today_str = time.strftime("%Y-%m-%d")
    redis_client.delete(f"spend:agent:trading-agent:daily:{today_str}")
    redis_client.delete(f"spend:fleet:daily:{today_str}")
    redis_client.delete("killswitch:fleet")
    yield
    # Teardown clean up to prevent state pollution
    redis_client.delete(f"spend:agent:trading-agent:daily:{today_str}")
    redis_client.delete(f"spend:fleet:daily:{today_str}")
    redis_client.delete("killswitch:fleet")

def test_allowed_trade():
    payload = {
        "agent_id": "trading-agent",
        "action_type": "trade",
        "resource": "stock:MSFT",
        "amount_cents": 50000,  # $500
        "timestamp": int(time.time()),
        "request_id": str(uuid.uuid4())
    }
    signed = sign_payload(payload, "agent-secret-key-abc")
    response = client.post("/evaluate", json=signed)
    assert response.status_code == 200
    res_data = response.json()
    assert res_data["decision"] == "ALLOW"
    assert res_data["remaining_budget_cents"] == 450000  # $5k limit - $500 = $4.5k remaining

def test_explicitly_denied_resource():
    payload = {
        "agent_id": "trading-agent",
        "action_type": "trade",
        "resource": "stock:RISK",  # Explicitly blocked in seeded rules
        "amount_cents": 5000,
        "timestamp": int(time.time()),
        "request_id": str(uuid.uuid4())
    }
    signed = sign_payload(payload, "agent-secret-key-abc")
    response = client.post("/evaluate", json=signed)
    assert response.status_code == 200
    res_data = response.json()
    assert res_data["decision"] == "DENY"
    assert "blocked by deny rule" in res_data["reason"]

def test_escalation_threshold():
    payload = {
        "agent_id": "trading-agent",
        "action_type": "trade",
        "resource": "stock:AAPL",
        "amount_cents": 150000,  # $1500 (exceeds $1000 escalation threshold)
        "timestamp": int(time.time()),
        "request_id": str(uuid.uuid4())
    }
    signed = sign_payload(payload, "agent-secret-key-abc")
    response = client.post("/evaluate", json=signed)
    assert response.status_code == 200
    res_data = response.json()
    assert res_data["decision"] == "ESCALATE"
    assert "exceeds escalation threshold" in res_data["reason"]

def test_over_budget_denial():
    payload = {
        "agent_id": "trading-agent",
        "action_type": "trade",
        "resource": "stock:AAPL",
        "amount_cents": 600000,  # $6000 (exceeds $5000 daily limit)
        "timestamp": int(time.time()),
        "request_id": str(uuid.uuid4())
    }
    signed = sign_payload(payload, "agent-secret-key-abc")
    response = client.post("/evaluate", json=signed)
    assert response.status_code == 200
    res_data = response.json()
    assert res_data["decision"] == "DENY"
    assert "exceeds daily allowed spend limits" in res_data["reason"]

def test_budget_accumulation():
    # 1. Lower the daily cap to $1000 (100,000 cents) for trading-agent to test accumulation
    # using small $400 (40,000 cents) transactions that remain below the $1000 escalation threshold.
    db = SessionLocal()
    try:
        policy = db.query(SpendPolicy).filter(
            SpendPolicy.scope == "agent:trading-agent",
            SpendPolicy.period == "daily"
        ).first()
        assert policy is not None
        policy.limit_amount = 100000  # $1k limit
        db.commit()
    finally:
        db.close()

    try:
        # Trade 1: Spend $400 (Allowed, remaining budget = $600)
        p1 = {
            "agent_id": "trading-agent",
            "action_type": "trade",
            "resource": "stock:AAPL",
            "amount_cents": 40000,
            "timestamp": int(time.time()),
            "request_id": str(uuid.uuid4())
        }
        r1 = client.post("/evaluate", json=sign_payload(p1, "agent-secret-key-abc"))
        assert r1.json()["decision"] == "ALLOW"
        assert r1.json()["remaining_budget_cents"] == 60000

        # Trade 2: Spend $400 (Allowed, remaining budget = $200)
        p2 = {
            "agent_id": "trading-agent",
            "action_type": "trade",
            "resource": "stock:AAPL",
            "amount_cents": 40000,
            "timestamp": int(time.time()),
            "request_id": str(uuid.uuid4())
        }
        r2 = client.post("/evaluate", json=sign_payload(p2, "agent-secret-key-abc"))
        assert r2.json()["decision"] == "ALLOW"
        assert r2.json()["remaining_budget_cents"] == 20000

        # Trade 3: Spend $400 (Denied - exceeds total $1000 cap by $200)
        p3 = {
            "agent_id": "trading-agent",
            "action_type": "trade",
            "resource": "stock:AAPL",
            "amount_cents": 40000,
            "timestamp": int(time.time()),
            "request_id": str(uuid.uuid4())
        }
        r3 = client.post("/evaluate", json=sign_payload(p3, "agent-secret-key-abc"))
        assert r3.json()["decision"] == "DENY"
        assert "exceeds daily allowed spend limits" in r3.json()["reason"]
    finally:
        # 2. Restore daily cap back to $5000 (500,000 cents)
        db = SessionLocal()
        try:
            policy = db.query(SpendPolicy).filter(
                SpendPolicy.scope == "agent:trading-agent",
                SpendPolicy.period == "daily"
            ).first()
            if policy:
                policy.limit_amount = 500000
                db.commit()
        finally:
            db.close()

def test_fleet_killswitch():
    # Activate fleet-wide kill switch in Redis
    redis_client.set("killswitch:fleet", "1")

    payload = {
        "agent_id": "trading-agent",
        "action_type": "trade",
        "resource": "stock:AAPL",
        "amount_cents": 50000,
        "timestamp": int(time.time()),
        "request_id": str(uuid.uuid4())
    }
    signed = sign_payload(payload, "agent-secret-key-abc")
    response = client.post("/evaluate", json=signed)
    assert response.status_code == 200
    res_data = response.json()
    assert res_data["decision"] == "DENY"
    assert "Emergency Stop" in res_data["reason"]
