import datetime
import redis
from sqlalchemy.orm import Session
from models import SpendPolicy
from config import REDIS_URL

redis_client = redis.from_url(REDIS_URL, decode_responses=True)

# Lua check-and-reserve script (executes atomically in Redis single-threaded execution context)
LUA_SPEND_CHECK = """
local agent_val = tonumber(redis.call('GET', KEYS[1]) or 0)
local fleet_val = tonumber(redis.call('GET', KEYS[2]) or 0)
local amount = tonumber(ARGV[1])
local agent_limit = tonumber(ARGV[2])
local fleet_limit = tonumber(ARGV[3])

if (agent_val + amount > agent_limit) or (fleet_val + amount > fleet_limit) then
    return 0 -- Denied
else
    redis.call('INCRBY', KEYS[1], amount)
    redis.call('INCRBY', KEYS[2], amount)
    return 1 -- Allowed
end
"""

spend_check_script = redis_client.register_script(LUA_SPEND_CHECK)

def get_limits(agent_id: str, db: Session) -> tuple[int, int]:
    """Helper to retrieve limits for agent and fleet."""
    agent_policy = db.query(SpendPolicy).filter(
        SpendPolicy.scope == f"agent:{agent_id}",
        SpendPolicy.period == "daily"
    ).first()
    
    fleet_policy = db.query(SpendPolicy).filter(
        SpendPolicy.scope == "fleet",
        SpendPolicy.period == "daily"
    ).first()
    
    DEFAULT_LIMIT = 999999999
    agent_limit = agent_policy.limit_amount if agent_policy else DEFAULT_LIMIT
    fleet_limit = fleet_policy.limit_amount if fleet_policy else DEFAULT_LIMIT
    return agent_limit, fleet_limit

def check_and_reserve_spend(agent_id: str, amount_cents: int, db: Session) -> tuple[bool, int]:
    """
    Checks and reserves the requested spend amount.
    Returns:
        (allowed: bool, remaining_agent_budget: int)
    """
    today_str = datetime.date.today().isoformat()
    agent_limit, fleet_limit = get_limits(agent_id, db)
    
    agent_key = f"spend:agent:{agent_id}:daily:{today_str}"
    fleet_key = f"spend:fleet:daily:{today_str}"
    
    result = spend_check_script(
        keys=[agent_key, fleet_key],
        args=[amount_cents, agent_limit, fleet_limit]
    )
    
    current_agent_spend = int(redis_client.get(agent_key) or 0)
    remaining_budget = max(0, agent_limit - current_agent_spend)
    
    return result == 1, remaining_budget

def is_spend_limit_exceeded(agent_id: str, amount_cents: int, db: Session) -> bool:
    """
    Dry-run check to see if the transaction amount exceeds limits.
    Does not write or reserve anything.
    """
    today_str = datetime.date.today().isoformat()
    agent_limit, fleet_limit = get_limits(agent_id, db)
    
    agent_key = f"spend:agent:{agent_id}:daily:{today_str}"
    fleet_key = f"spend:fleet:daily:{today_str}"
    
    current_agent_spend = int(redis_client.get(agent_key) or 0)
    current_fleet_spend = int(redis_client.get(fleet_key) or 0)
    
    return (current_agent_spend + amount_cents > agent_limit) or (current_fleet_spend + amount_cents > fleet_limit)
