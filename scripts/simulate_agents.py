import time
import uuid
import hmac
import hashlib
import json
import os
import httpx
import threading

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:8000")
AGENT_ID = "trading-agent"
SECRET = "agent-secret-key-abc"

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

def send_request(payload: dict, description: str):
    print(f"\n[Simulator] Running: {description}...", flush=True)
    try:
        url = f"{GATEWAY_URL}/evaluate"
        resp = httpx.post(url, json=payload, timeout=5)
        if resp.status_code == 200:
            print(f"[Gateway Response] Status: 200 | Decision: {resp.json().get('decision')} | Reason: {resp.json().get('reason')}", flush=True)
        else:
            print(f"[Gateway Response] Status: {resp.status_code} | Error: {resp.json().get('detail')}", flush=True)
        return resp
    except Exception as e:
        print(f"[Error] Failed to connect to gateway: {e}", flush=True)
        return None

def poll_escalation(request_id: str):
    print(f"[Poller] Started status check thread for request {request_id}", flush=True)
    for _ in range(30):  # Poll every 1s for 30s max
        time.sleep(1)
        try:
            url = f"{GATEWAY_URL}/evaluate/{request_id}"
            resp = httpx.get(url, timeout=2)
            if resp.status_code == 200:
                data = resp.json()
                status_val = data.get("status")
                print(f"[Poller] ID {request_id} current status: {status_val}", flush=True)
                if status_val in ["allowed", "denied"]:
                    print(f"[Poller] ID {request_id} final result: {status_val.upper()}! Reason: {data.get('reason')}", flush=True)
                    return
            else:
                print(f"[Poller] Failed to poll ID {request_id}: {resp.status_code}", flush=True)
        except Exception as e:
            print(f"[Poller] Exception while polling: {e}", flush=True)
    print(f"[Poller] ID {request_id} polling expired (timeout)", flush=True)

def run_simulation():
    # Wait for gateway container to start and report healthy status
    print(f"Waiting for gateway to be active at {GATEWAY_URL}...", flush=True)
    for i in range(15):
        try:
            r = httpx.get(f"{GATEWAY_URL}/health", timeout=2)
            if r.status_code == 200:
                print("Gateway is healthy and online!", flush=True)
                break
        except Exception:
            pass
        print(f"Retrying connection in 2s ({i+1}/15)...", flush=True)
        time.sleep(2)
    else:
        print("Gateway could not be contacted. Exiting simulation.", flush=True)
        return

    while True:
        # Scenario 1: Valid trade (₹300) -> ALLOW
        req_id_1 = str(uuid.uuid4())
        p1 = {
            "agent_id": AGENT_ID,
            "action_type": "trade",
            "resource": "stock:MSFT",
            "amount_cents": 30000,
            "timestamp": int(time.time()),
            "request_id": req_id_1
        }
        send_request(sign_payload(p1, SECRET), "Scenario 1: Valid Trade (₹300)")
        time.sleep(4)

        # Scenario 2: Forbidden resource (stock:RISK) -> DENY
        req_id_2 = str(uuid.uuid4())
        p2 = {
            "agent_id": AGENT_ID,
            "action_type": "trade",
            "resource": "stock:RISK",
            "amount_cents": 10000,
            "timestamp": int(time.time()),
            "request_id": req_id_2
        }
        send_request(sign_payload(p2, SECRET), "Scenario 2: Denied Resource (stock:RISK)")
        time.sleep(4)

        # Scenario 3: Escalated trade (₹1200) -> ESCALATE
        req_id_3 = str(uuid.uuid4())
        p3 = {
            "agent_id": AGENT_ID,
            "action_type": "trade",
            "resource": "stock:AAPL",
            "amount_cents": 120000,
            "timestamp": int(time.time()),
            "request_id": req_id_3
        }
        resp3 = send_request(sign_payload(p3, SECRET), "Scenario 3: High-Value Trade (₹1200 - Triggers Escalation)")
        if resp3 and resp3.status_code == 200 and resp3.json().get("decision") == "ESCALATE":
            # Start background poller thread
            threading.Thread(target=poll_escalation, args=(req_id_3,), daemon=True).start()
        time.sleep(8)

        # Scenario 4: Replay attack -> DENY
        req_id_4 = str(uuid.uuid4())
        p4 = {
            "agent_id": AGENT_ID,
            "action_type": "trade",
            "resource": "stock:GOOG",
            "amount_cents": 15000,
            "timestamp": int(time.time()),
            "request_id": req_id_4
        }
        signed_p4 = sign_payload(p4, SECRET)
        send_request(signed_p4, "Scenario 4a: Send Request (Original)")
        send_request(signed_p4, "Scenario 4b: Re-Send Request (Replay)")
        time.sleep(4)

        # Scenario 5: Invalid signature -> Blocked with HTTP 401
        req_id_5 = str(uuid.uuid4())
        p5 = {
            "agent_id": AGENT_ID,
            "action_type": "trade",
            "resource": "stock:AMZN",
            "amount_cents": 20000,
            "timestamp": int(time.time()),
            "request_id": req_id_5
        }
        send_request(sign_payload(p5, "bad-secret-key"), "Scenario 5: Invalid Signature (Signed with incorrect key)")
        time.sleep(4)

        # Scenario 6: Exceed daily cap (₹6000) -> DENY
        req_id_6 = str(uuid.uuid4())
        p6 = {
            "agent_id": AGENT_ID,
            "action_type": "trade",
            "resource": "stock:NFLX",
            "amount_cents": 600000,
            "timestamp": int(time.time()),
            "request_id": req_id_6
        }
        send_request(sign_payload(p6, SECRET), "Scenario 6: Out of Budget Trade (₹6000 - Exceeds ₹5000 cap)")
        time.sleep(6)

        print("\n--- Completed Scenario Cycle. Restarting in 5s... ---\n", flush=True)
        time.sleep(5)

if __name__ == "__main__":
    run_simulation()
