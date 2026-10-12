"""Object serialization and payload state-diffing.

Converts arbitrary Python objects captured at trace time into JSON-safe
structures (bounding size/depth so tracing stays low-overhead), and computes
structural diffs between two serialized snapshots of the same object -
used to show "added / modified / removed" attributes for mutable arguments
and return values.
"""

from dataclasses import is_dataclass, asdict

from .redaction import redact, STRICT_PRIVACY_ENV

MAX_STR_LEN = 500
MAX_COLLECTION_ITEMS = 50
MAX_DEPTH = 4


def safe_serialize(obj, _seen=None, depth=0, max_depth=MAX_DEPTH, key=None, redact_secrets=True):
    """Recursively convert obj into a JSON-safe structure.

    Guards against: unbounded size (strings/collections truncated),
    unbounded depth, circular references, and non-serializable types
    (falls back to repr()).
    """
    if STRICT_PRIVACY_ENV:
        redact_secrets = True

    if _seen is None:
        _seen = frozenset()

    if obj is None or isinstance(obj, bool) or isinstance(obj, (int, float)):
        return obj

    obj_id = id(obj)
    if obj_id in _seen:
        return "<circular reference>"
    if depth >= max_depth:
        return f"<max depth reached: {type(obj).__name__}>"
    _seen = _seen | {obj_id}

    if redact_secrets:
        masked = redact(obj, key=key, _seen=_seen)
        if masked is not obj:
            return masked

    if isinstance(obj, str):
        if len(obj) <= MAX_STR_LEN:
            return obj
        return obj[:MAX_STR_LEN] + f"...<{len(obj)} chars total>"
    if isinstance(obj, (bytes, bytearray)):
        return f"<bytes len={len(obj)}>"


    if isinstance(obj, (list, tuple, set, frozenset)):
        items = list(obj)[:MAX_COLLECTION_ITEMS]
        result = [safe_serialize(i, _seen, depth + 1, max_depth, redact_secrets=redact_secrets) for i in items]
        if len(obj) > MAX_COLLECTION_ITEMS:
            result.append(f"...<{len(obj) - MAX_COLLECTION_ITEMS} more items>")
        return result

    if isinstance(obj, dict):
        result = {}
        for i, (k, v) in enumerate(obj.items()):
            if i >= MAX_COLLECTION_ITEMS:
                result["__truncated__"] = f"<{len(obj) - MAX_COLLECTION_ITEMS} more items>"
                break
            result[str(k)] = safe_serialize(v, _seen, depth + 1, max_depth, key=k, redact_secrets=redact_secrets)
        return result

    if is_dataclass(obj) and not isinstance(obj, type):
        try:
            fields = safe_serialize(asdict(obj), _seen, depth + 1, max_depth, redact_secrets=redact_secrets)
            fields["__type__"] = type(obj).__name__
            return fields
        except Exception:
            pass

    if hasattr(obj, "__dict__"):
        try:
            attrs = {
                k: safe_serialize(v, _seen, depth + 1, max_depth, key=k, redact_secrets=redact_secrets)
                for k, v in vars(obj).items()
                if not k.startswith("_")
            }
            attrs["__type__"] = type(obj).__name__
            attrs["__repr__"] = _safe_repr(obj, redact_secrets=redact_secrets)
            return attrs
        except Exception:
            return _safe_repr(obj, redact_secrets=redact_secrets)

    return _safe_repr(obj, redact_secrets=redact_secrets)


def _safe_repr(obj, redact_secrets=True):
    try:
        r = repr(obj)
    except Exception:
        r = f"<unrepresentable {type(obj).__name__}>"
    if redact_secrets:
        masked = redact(r)
        if masked is not r:
            return masked
    return r if len(r) <= MAX_STR_LEN else r[:MAX_STR_LEN] + "..."


def diff_states(before, after):
    """Compute a structural diff between two already-serialized values.

    Returns None if there is no meaningful difference, otherwise a dict
    describing what changed: {"type": "dict_diff"|"list_diff"|"changed", ...}
    """
    if before == after:
        return None

    if isinstance(before, dict) and isinstance(after, dict):
        before_keys, after_keys = set(before.keys()), set(after.keys())
        added = {k: after[k] for k in after_keys - before_keys}
        removed = {k: before[k] for k in before_keys - after_keys}
        modified = {}
        for k in before_keys & after_keys:
            if before[k] != after[k]:
                if isinstance(before[k], dict) and isinstance(after[k], dict):
                    nested = diff_states(before[k], after[k])
                    if nested:
                        modified[k] = nested
                else:
                    modified[k] = {"before": before[k], "after": after[k]}
        if not added and not removed and not modified:
            return None
        return {"type": "dict_diff", "added": added, "removed": removed, "modified": modified}

    if isinstance(before, list) and isinstance(after, list):
        return {"type": "list_diff", "before": before, "after": after}

    return {"type": "changed", "before": before, "after": after}
