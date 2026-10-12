"""
enterprise_pipeline.py - Multi-stage data pipeline for validating Flowtrace niche features.

Scenario: A checkout service that:
1. Fetches a user record
2. Applies coupon logic (mutates the order dict - ARG_DIFF target)
3. Calculates total (consumes data from apply_coupon - DATAFLOW target)
4. Validates auth (raises a deep KeyError - WHY target)
5. Saves to DB

The deliberate bug: apply_coupon removes discount_rate when coupon is INVALID,
causing calculate_total to KeyError on discount_rate.
"""


class UserProfile:
    def __init__(self, user_id, tier):
        self.user_id = user_id
        self.tier = tier
        self.session_token = "tok_abc123"

    def to_dict(self):
        return {"user_id": self.user_id, "tier": self.tier, "token": self.session_token}


class Database:
    def __init__(self):
        self._orders = {}

    def save_order(self, order_id, order):
        self._orders[order_id] = order
        return order_id

    def get_user(self, user_id):
        return UserProfile(user_id=user_id, tier="premium")


def fetch_user(user_id):
    db = Database()
    user = db.get_user(user_id)
    return user.to_dict()


def apply_coupon(order, coupon_code):
    """Apply coupon to order and calculate discount rate."""
    order["items"] = [{"name": "Widget", "price": 99.99}]
    order["subtotal"] = 99.99
    order["discount_rate"] = 0.10
    if coupon_code == "INVALID":
        del order["discount_rate"]
        order["error_code"] = "COUPON_EXPIRED"
    else:
        order["discount_rate"] = 0.20
    return order


def calculate_total(order):
    subtotal = order["subtotal"]
    discount = order["discount_rate"]
    tax_rate = 0.08
    total = subtotal * (1 - discount) * (1 + tax_rate)
    return round(total, 2)


def validate_auth(user_context, required_tier):
    tier = user_context.get("tier")
    if tier != required_tier:
        raise PermissionError(f"User tier insufficient. Required: {required_tier}")
    return True


def save_order(db, order_id, order):
    return db.save_order(order_id, order)


def process_checkout(user_id, coupon_code, order_id):
    db = Database()
    user = fetch_user(user_id)
    order = {"user_id": user_id, "coupon": coupon_code}
    order = apply_coupon(order, coupon_code)
    total = calculate_total(order)
    validate_auth(user, "premium")
    record_id = save_order(db, order_id, order)
    return {"order_id": record_id, "total": total, "user": user}


if __name__ == "__main__":
    print("=== Run A: Valid coupon ===")
    result_a = process_checkout("user_42", "SUMMER20", "order_001")
    print(f"Success: order={result_a['order_id']}, total={result_a['total']}")

    print("\n=== Run B: Invalid coupon (will fail) ===")
    result_b = process_checkout("user_42", "INVALID", "order_002")
    print(f"Success: {result_b}")
