"""Sample application traced by flowtrace. Run with:
    flowtrace run examples/demo.py
"""


def fetch_user(user_id):
    # pretend network call
    return {"id": user_id, "name": "Ada", "roles": ["admin"]}


def enrich_user(user):
    # mutate the incoming dict in place -> exercises arg state diffing
    user["last_seen"] = "2026-08-06"
    user["roles"].append("beta-tester")
    return user


def save_user(user):
    # pretend database write
    return {"status": "ok", "id": user["id"]}


def process_signup(user_id):
    user = fetch_user(user_id)
    user = enrich_user(user)
    result = save_user(user)
    return result


def risky_divide(a, b):
    try:
        return a / b
    except ZeroDivisionError:
        return None


def main():
    result = process_signup(42)
    print("signup result:", result)

    for divisor in (2, 0, 5):
        print("divide:", risky_divide(10, divisor))


if __name__ == "__main__":
    main()
