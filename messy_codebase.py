"""
messy_codebase.py - Stress-testing codebase for Flowtrace.
Contains adversarial syntax, edge-case language features, poison objects,
circular references, silent state mutations, N+1 query patterns, and secrets.
"""

import sys
import functools
from dataclasses import dataclass
from typing import Optional, List, Dict, Any, Union

# 1. Toxic / Exploding Objects (Objects whose __repr__ or __str__ blows up)
class ToxicReprObject:
    """An adversarial object that raises exceptions when inspected via repr() or str()."""
    def __init__(self, name: str = "toxic_payload"):
        self.name = name

    def __repr__(self):
        raise RuntimeError("CRITICAL: ToxicReprObject.__repr__ exploded!")

    def __str__(self):
        raise RuntimeError("CRITICAL: ToxicReprObject.__str__ exploded!")


# 2. Stacked Decorators and Closures
def timing_decorator(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)
    return wrapper

def validating_decorator(tag: str = "audit"):
    def inner_dec(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper
    return inner_dec


# 3. Dataclasses and Complex Types
@dataclass
class TransactionMetadata:
    txn_id: str
    amount: float
    tags: List[str]
    payload: Dict[str, Any]


# 4. Complex Functions with Positional-Only (/), Keyword-Only (*), Walrus (:=), and Match/Case
@timing_decorator
@validating_decorator("payment_v2")
def process_dynamic_payment(
    pos_only_id: str,
    /,
    amount: float,
    *args,
    currency: str = "USD",
    metadata: Optional[Dict[str, Any]] = None,
    **kwargs
) -> Dict[str, Any]:
    """Process dynamic payment with Python 3.8+ positional-only and keyword-only syntax.
    
    Contains walrus operators, complex comprehensions, and match-case dispatch.
    """
    # Walrus operator := stress test
    if (eff_amount := amount * 1.05) > 100.0:
        tier = "enterprise"
    else:
        tier = "standard"

    # Pattern matching (Python 3.10+ match/case)
    match currency:
        case "USD" | "EUR":
            fx_rate = 1.0
        case "GBP":
            fx_rate = 1.25
        case other if len(other) == 3:
            fx_rate = 0.95
        case _:
            fx_rate = 1.0

    # Complex nested comprehension with closure
    sub_calculations = {
        f"item_{i}": (lambda x: x * fx_rate)(i)
        for i in range(3)
        if (eff_idx := i * 2) >= 0
    }

    return {
        "status": "APPROVED",
        "pos_only_id": pos_only_id,
        "effective_amount": eff_amount,
        "tier": tier,
        "currency": currency,
        "fx_rate": fx_rate,
        "calculations": sub_calculations,
    }


# 5. Deep Recursion (Call-stack depth stress test)
def recursive_tree_walk(depth: int, max_depth: int = 15, accum: Optional[List[int]] = None) -> List[int]:
    """Recursively walks a tree down to max_depth frames."""
    if accum is None:
        accum = []
    accum.append(depth)
    if depth >= max_depth:
        return accum
    return recursive_tree_walk(depth + 1, max_depth, accum)


# 6. Silent In-Place Mutation (The bug that static linters CANNOT catch)
def stage_1_receive_order(raw_order: Dict[str, Any]) -> Dict[str, Any]:
    raw_order["status"] = "RECEIVED"
    raw_order["security_clearance"] = True
    return raw_order

def stage_2_poison_mutation(order_state: Dict[str, Any]) -> Dict[str, Any]:
    """Silently deletes an essential key and mutates a nested dict in-place."""
    # Silent removal:
    if "auth_token" in order_state:
        del order_state["auth_token"]
    # Poison injection:
    order_state["injected_anomaly"] = "CORRUPT_FLAG_0x99"
    # Nested mutation:
    if "items" in order_state and isinstance(order_state["items"], list):
        order_state["items"].append({"item_id": "ghost_item_99", "price": 0.0})
    return order_state

def stage_3_verify_order(order_state: Dict[str, Any]) -> bool:
    """Expects auth_token to exist. Crashes because stage_2 silently removed it!"""
    token = order_state["auth_token"]  # Raises KeyError: 'auth_token'
    return len(token) > 0


# 7. N+1 Query Anti-Pattern (Loops calling simulated database)
def mock_db_find_user_by_id(user_id: int) -> Dict[str, Any]:
    """Simulates a database query for a single user."""
    return {"id": user_id, "username": f"user_{user_id}", "active": True}

def load_user_profiles_n_plus_one(user_ids: List[int]) -> List[Dict[str, Any]]:
    """Classic N+1 database antipattern: loop executing single queries instead of batch."""
    results = []
    for uid in user_ids:
        # N+1 repetitive query
        record = mock_db_find_user_by_id(uid)
        results.append(record)
    return results

def load_user_profiles_batched(user_ids: List[int]) -> List[Dict[str, Any]]:
    """Fixed version: batched single operation."""
    return [{"id": uid, "username": f"user_{uid}", "active": True} for uid in user_ids]


# 8. Causal Exception Propagation Chain
def frame_level_4_root_cause():
    """Origin of the failure."""
    raise ZeroDivisionError("division by zero at math engine level 4")

def frame_level_3_mediator():
    try:
        return frame_level_4_root_cause()
    except ZeroDivisionError as e:
        # Re-raise with causal chaining
        raise RuntimeError("Service mediator failed during computation") from e

def frame_level_2_coordinator():
    return frame_level_3_mediator()

def frame_level_1_entrypoint():
    try:
        return frame_level_2_coordinator()
    finally:
        # Cleanup code in finally block
        pass


# 9. Leaked Secrets and Credentials (Redaction stress test)
def authenticate_and_transfer_funds(
    aws_key: str,
    openai_key: str,
    db_uri: str,
    bearer_token: str,
    credit_card: str,
    account_dict: Dict[str, Any]
) -> Dict[str, Any]:
    """Receives sensitive credentials and processes transaction."""
    return {
        "status": "COMPLETED",
        "authenticated": True,
        "aws_fingerprint": aws_key,
        "token_echo": bearer_token,
        "account_info": account_dict,
    }


# 10. Adversarial Runner Entrypoint
def run_all_messy_operations(should_crash: bool = True):
    # Test A: Toxic and Circular references
    toxic = ToxicReprObject("chernobyl_object")
    
    circular_dict: Dict[str, Any] = {"name": "infinite_loop"}
    circular_dict["self_ref"] = circular_dict  # Self-referencing circular dict
    
    circular_list: List[Any] = [1, 2, 3]
    circular_list.append(circular_list)  # Self-referencing circular list

    # Test B: Positional/Keyword/Match/Walrus
    payment_res = process_dynamic_payment(
        "ACC_0991",
        450.0,
        currency="USD",
        metadata={"toxic": toxic, "circular": circular_dict}
    )

    # Test C: Deep recursion
    rec_res = recursive_tree_walk(0, max_depth=12)

    # Test D: Secrets and Credentials
    sec_res = authenticate_and_transfer_funds(
        aws_key="AKIAIOSFODNN7EXAMPLE",
        openai_key="sk-proj-abc123def456ghi789jkl012mno345pqr678stu901vwx234",
        db_uri="postgresql://prod_admin:UltraSecretP@ssw0rd99@db.internal:5432/core_db",
        bearer_token="Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkFsaWNlIn0.signature12345",
        credit_card="4111111111111111",
        account_dict={"api_secret": "super_secret_token_val_999", "balance": 15000}
    )

    # Test E: N+1 queries
    n_plus_one_res = load_user_profiles_n_plus_one(list(range(10)))

    # Test F: Silent Mutation
    order = {
        "order_id": "ORD-771",
        "auth_token": "valid_token_xyz_99",
        "user_id": "u123",
        "items": [{"item_id": "item_1", "price": 49.99}]
    }
    stage_1_receive_order(order)
    stage_2_poison_mutation(order)

    if should_crash:
        # Will crash with KeyError: 'auth_token'
        stage_3_verify_order(order)
    else:
        # Patch applied: re-add token if missing
        if "auth_token" not in order:
            order["auth_token"] = "restored_token"
        stage_3_verify_order(order)

    return {
        "payment": payment_res,
        "recursion": rec_res,
        "secrets": sec_res,
        "n_plus_one": n_plus_one_res,
    }


if __name__ == "__main__":
    try:
        run_all_messy_operations(should_crash=True)
    except Exception as e:
        print(f"Captured expected execution crash: {type(e).__name__}: {e}")
