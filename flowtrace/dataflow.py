"""Data-flow layer (Priority 3): reconstructs "where did this value go"
from a runtime trace.

This is deliberately NOT static taint analysis and doesn't track true
object identity - traced values are JSON snapshots (see serializer.py), so
there's no pointer to follow. Instead: if a call's return value (or an
argument it mutated) is structurally identical - or, for a mutated dict, a
strong superset match - to an argument a *later* call in the same branch
of the call tree consumes, that's a flow edge. Cheap to compute from data
we already capture, strong signal in practice, but a heuristic, not a
proof - two unrelated calls that happen to pass around an identical value
would show up as a (spurious) edge. Scalars (numbers, short strings,
None/bool) are excluded as flow candidates for exactly that reason - they
collide constantly and aren't distinctive enough to trust structurally.

Known scope limit: only sibling-scope flow (calls sharing the same
parent_id) is tracked. A value a parent passes down into a child call's
argument - as opposed to a value produced by one child and consumed by a
later sibling - isn't (that's parent-to-child flow, a different shape of
edge this module doesn't build yet).
"""

import json


def _fingerprint(value):
    """Canonical string form for structural equality, or None if the value
    is too small/collision-prone to trust as a flow signal."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None
    if isinstance(value, str):
        return value if len(value) >= 8 else None
    if isinstance(value, (list, dict)):
        try:
            return json.dumps(value, sort_keys=True)
        except TypeError:
            return None
    return None


def _dict_overlap_score(produced, consumed):
    """Fraction of `produced`'s keys whose value also appears unchanged in
    `consumed` - catches "the same dict, but it since gained/changed a key"
    (e.g. a mutated argument) even though exact fingerprints now differ."""
    if not isinstance(produced, dict) or not isinstance(consumed, dict) or not produced:
        return 0.0
    matches = sum(1 for k, v in produced.items() if k in consumed and consumed[k] == v)
    return matches / len(produced)


def _apply_diff(before, diff):
    """Reconstructs an approximate after-state for a mutated argument, since
    the trace only stores the diff, not the full after-snapshot."""
    if diff is None:
        return before
    if diff.get("type") == "dict_diff" and isinstance(before, dict):
        result = dict(before)
        for k in diff.get("removed", {}):
            result.pop(k, None)
        for k, v in diff.get("added", {}).items():
            result[k] = v
        for k, nested in diff.get("modified", {}).items():
            if isinstance(nested, dict) and nested.get("type"):
                result[k] = _apply_diff(result.get(k), nested)
            elif isinstance(nested, dict) and "after" in nested:
                result[k] = nested["after"]
        return result
    if diff.get("type") in ("list_diff", "changed"):
        return diff.get("after")
    return before


def build_dataflow(trace_doc, min_overlap=0.6):
    """Returns {"edges": [...]}, each edge: from_call, from_label
    ("return" or "arg:<name>"), to_call, to_arg, match ("exact"|"overlap"),
    confidence, plus from_function/to_function for readability."""
    calls = {}
    children_by_parent = {}
    order_counter = 0

    for ev in trace_doc.get("events", []):
        if ev["type"] == "call":
            calls[ev["call_id"]] = {
                "call_id": ev["call_id"], "parent_id": ev.get("parent_id"),
                "function": ev["function"], "args": ev.get("args", {}),
                "return_value": None, "arg_diff": {}, "order": order_counter,
            }
            order_counter += 1
            children_by_parent.setdefault(ev.get("parent_id"), []).append(ev["call_id"])
        elif ev["type"] == "return":
            c = calls.get(ev["call_id"])
            if c:
                c["return_value"] = ev.get("return_value")
                c["arg_diff"] = ev.get("arg_diff", {})

    def producers_before(order_cutoff, parent_id, exclude_call_id):
        result = []
        for sibling_id in children_by_parent.get(parent_id, []):
            if sibling_id == exclude_call_id:
                continue
            sc = calls[sibling_id]
            if sc["order"] >= order_cutoff:
                continue
            if sc["return_value"] is not None:
                result.append((sibling_id, "return", sc["return_value"]))
            for arg_name, diff in sc["arg_diff"].items():
                after = _apply_diff(sc["args"].get(arg_name), diff)
                if after is not None:
                    result.append((sibling_id, f"arg:{arg_name}", after))
        return result

    edges = []
    for call_id, c in calls.items():
        for arg_name, arg_value in c["args"].items():
            fp = _fingerprint(arg_value)
            best_by_producer = {}  # dedupe: keep the strongest match per producer point
            for prod_id, label, prod_value in producers_before(c["order"], c["parent_id"], call_id):
                if fp is not None and _fingerprint(prod_value) == fp:
                    best_by_producer[(prod_id, label)] = ("exact", 1.0)
                elif isinstance(arg_value, dict) and isinstance(prod_value, dict):
                    score = _dict_overlap_score(prod_value, arg_value)
                    if score >= min_overlap:
                        prev = best_by_producer.get((prod_id, label))
                        if not prev or prev[1] < score:
                            best_by_producer[(prod_id, label)] = ("overlap", round(score, 2))
            for (prod_id, label), (match_type, confidence) in best_by_producer.items():
                edges.append({
                    "from_call": prod_id, "from_label": label, "from_function": calls[prod_id]["function"],
                    "to_call": call_id, "to_arg": arg_name, "to_function": c["function"],
                    "match": match_type, "confidence": confidence,
                })

    return {"schema_version": "0.1.0", "edge_count": len(edges), "edges": edges}
