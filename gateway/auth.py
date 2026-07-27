import hmac
import hashlib
import json
import time
import redis
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from models import Agent
from config import REDIS_URL

# Connect to Redis
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

def verify_signature(payload: dict, secret: str) -> bool:
    """
    Verifies the HMAC-SHA256 signature of the payload.
    The payload must contain a 'signature' key, which is the hexadecimal HMAC
    of the rest of the payload fields ordered alphabetically.
    """
    if "signature" not in payload:
        return False
    
    sig_to_verify = payload["signature"]
    
    # Sort the dictionary key-value pairs excluding the signature field
    data = {k: v for k, v in payload.items() if k != "signature"}
    canonical_body = json.dumps(data, sort_keys=True, separators=(',', ':'))
    
    computed_sig = hmac.new(
        secret.encode('utf-8'),
        canonical_body.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(computed_sig, sig_to_verify)

def check_freshness_and_replay(request_id: str, timestamp: int) -> bool:
    """
    Checks that the timestamp is within 5 seconds of the current time
    and that the request_id is not already registered in Redis (5 min TTL).
    """
    current_time = int(time.time())
    
    # 5-second freshness window
    if abs(current_time - timestamp) > 5:
        return False
        
    # Replay protection set NX in Redis
    redis_key = f"replay:{request_id}"
    added = redis_client.set(redis_key, "1", ex=300, nx=True)  # TTL 300 seconds
    if not added:
        return False
        
    return True

def authenticate_request(payload: dict, db: Session) -> Agent:
    """
    Orchestrates the security validation steps:
    1. Check presence of credentials fields.
    2. Enforce freshness and replay checks.
    3. Retrieve the agent profile and ensure status is active.
    4. Validate HMAC-SHA256 signature using the agent's secret key.
    """
    required = ["agent_id", "timestamp", "request_id", "signature"]
    for field in required:
        if field not in payload:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Authentication payload missing: {field}"
            )
            
    agent_id = payload["agent_id"]
    timestamp = payload["timestamp"]
    request_id = payload["request_id"]
    
    # 1. Freshness & Replay Check
    if not check_freshness_and_replay(request_id, timestamp):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Request timestamp expired or replay detected"
        )
        
    # 2. Load Agent from Database
    agent = db.query(Agent).filter(Agent.agent_id == agent_id).first()
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Agent not registered in gateway"
        )
        
    # 3. Check status
    if agent.status in ["suspended", "revoked"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied: Agent is {agent.status}"
        )
        
    # 4. HMAC Signature Check
    if not verify_signature(payload, agent.secret_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signature"
        )
        
    return agent
