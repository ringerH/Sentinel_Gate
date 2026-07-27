import datetime
from sqlalchemy.orm import Session
from models import PermissionRule, Agent

def evaluate_policy(
    agent: Agent,
    action_type: str,
    resource: str,
    amount_cents: int,
    timestamp: int,
    db: Session
) -> tuple[str, str]:
    """
    Evaluates policy rules for a given agent request.
    Returns (decision, reason) where decision is one of ALLOW, DENY, ESCALATE.
    Rules follow the 'deny-by-default' and 'deny overrides allow' principles.
    """
    # 1. Fetch rules matching the agent's role
    rules = db.query(PermissionRule).filter(PermissionRule.role == agent.role).all()
    
    if not rules:
        return "DENY", f"No policy rules defined for role: '{agent.role}'"
        
    # Split into DENY rules and ALLOW rules to apply precedence
    deny_rules = [r for r in rules if r.effect == "deny"]
    allow_rules = [r for r in rules if r.effect == "allow"]
    
    # 2. Evaluate Deny Rules first
    for rule in deny_rules:
        if rule.action_type == action_type:
            # Check resource scope match (e.g. '*' or prefix like 'stock:')
            if rule.resource_scope == "*" or resource.startswith(rule.resource_scope):
                return "DENY", f"Action explicitly blocked by deny rule: {rule.rule_id}"
                
    # 3. Evaluate Allow Rules
    matched_allow = False
    escalation_needed = False
    escalation_reason = ""
    allow_reasons = []
    
    try:
        request_time = datetime.datetime.utcfromtimestamp(timestamp)
        request_hour = request_time.hour
    except Exception:
        return "DENY", "Invalid timestamp formatting"
        
    for rule in allow_rules:
        if rule.action_type != action_type:
            continue
        if rule.resource_scope != "*" and not resource.startswith(rule.resource_scope):
            continue
            
        constraints = rule.constraints or {}
        
        # Check max amount constraint
        max_amount = constraints.get("max_amount")
        if max_amount is not None and amount_cents > max_amount:
            continue
            
        # Check allowed hours constraint
        allowed_hours = constraints.get("allowed_hours")
        if allowed_hours is not None and request_hour not in allowed_hours:
            continue
            
        # Check allowed counterparties constraint
        allowed_counterparties = constraints.get("allowed_counterparties")
        if allowed_counterparties is not None:
            # Counterparty should be present in the resource string
            matched_cp = any(cp in resource for cp in allowed_counterparties)
            if not matched_cp:
                continue
                
        # Rule matched successfully!
        matched_allow = True
        
        # Check escalation threshold constraint
        escalation_threshold = constraints.get("escalation_threshold")
        if escalation_threshold is not None and amount_cents > escalation_threshold:
            escalation_needed = True
            escalation_reason = (
                f"Amount {amount_cents} exceeds escalation threshold of "
                f"{escalation_threshold} on rule {rule.rule_id}"
            )
            
        allow_reasons.append(rule.rule_id)
        
    if not matched_allow:
        return "DENY", f"No matching allow rules for action '{action_type}' on resource '{resource}'"
        
    if escalation_needed:
        return "ESCALATE", escalation_reason
        
    return "ALLOW", f"Rules matched: {', '.join(allow_reasons)}"
