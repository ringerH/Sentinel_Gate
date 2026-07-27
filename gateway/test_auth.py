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
    # Clean up replay cache and killswitch before each test
    redis_client.delete("killswitch:fleet")
    today_str = time.strftime("%Y-%m-%d")
    redis_client.delete(f"spend:agent:trading-agent:daily:{today_str}")
    redis_client.delete(f"spend:fleet:daily:{today_str}")
    # Delete test replays if we want, but since they use random UUIDs it's fine.
    yield
    # Cleanup after test
    redis_client.delete("killswitch:fleet")
    redis_client.delete(f"spend:agent:trading-agent:daily:{today_str}")
    redis_client.delete(f"spend:fleet:daily:{today_str}")

def test_valid_request():
    payload = {
        "agent_id": "trading-agent",
        "action_type": "trade",
        "resource": "stock:AAPL",
        "amount_cents": 5000,
        "timestamp": int(time.time()),
        "request_id": str(uuid.uuid4())
    }
    signed = sign_payload(payload, "agent-secret-key-abc")
    response = client.post("/evaluate", json=signed)
    assert response.status_code == 200
    assert response.json()["decision"] == "ALLOW"

def test_invalid_signature():
    payload = {
        "agent_id": "trading-agent",
        "action_type": "trade",
        "resource": "stock:AAPL",
        "amount_cents": 5000,
        "timestamp": int(time.time()),
        "request_id": str(uuid.uuid4())
    }
    signed = sign_payload(payload, "wrong-secret-key")
    response = client.post("/evaluate", json=signed)
    assert response.status_code == 401
    assert "Invalid signature" in response.json()["detail"]

def test_replay_attack():
    req_id = str(uuid.uuid4())
    payload = {
        "agent_id": "trading-agent",
        "action_type": "trade",
        "resource": "stock:AAPL",
        "amount_cents": 5000,
        "timestamp": int(time.time()),
        "request_id": req_id
    }
    signed = sign_payload(payload, "agent-secret-key-abc")
    
    # First request should pass
    response1 = client.post("/evaluate", json=signed)
    assert response1.status_code == 200
    
    # Replay request (same request_id) should fail
    response2 = client.post("/evaluate", json=signed)
    assert response2.status_code == 401
    assert "replay detected" in response2.json()["detail"]

def test_expired_timestamp():
    payload = {
        "agent_id": "trading-agent",
        "action_type": "trade",
        "resource": "stock:AAPL",
        "amount_cents": 5000,
        "timestamp": int(time.time()) - 10,  # 10 seconds ago
        "request_id": str(uuid.uuid4())
    }
    signed = sign_payload(payload, "agent-secret-key-abc")
    response = client.post("/evaluate", json=signed)
    assert response.status_code == 401
    assert "expired" in response.json()["detail"]

def test_suspended_agent():
    payload = {
        "agent_id": "suspended-agent",
        "action_type": "trade",
        "resource": "stock:AAPL",
        "amount_cents": 5000,
        "timestamp": int(time.time()),
        "request_id": str(uuid.uuid4())
    }
    signed = sign_payload(payload, "suspended-secret-key")
    response = client.post("/evaluate", json=signed)
    assert response.status_code == 403
    assert "Agent is suspended" in response.json()["detail"]
