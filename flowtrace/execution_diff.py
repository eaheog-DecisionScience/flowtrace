"""Execution comparison ("did the execution path change?"): diffs two
runtime traces - typically the same entry point traced before and after a
code change - at the call-tree level, plus aggregate summaries
(exceptions, per-function latency, argument/return-value differences,
architectural category mix).

Two traces never share call_ids, so nodes are matched structurally: each
level's children are aligned by function name via sequence alignment
(difflib.SequenceMatcher), not by id. A call in one run's tree is "the
same call" as whichever call at the corresponding aligned position in the
other run's tree has the same function name - insertions, removals, and
reordering all fall out of the alignment rather than needing a bespoke
tree-edit-distance implementation.
"""

import difflib
from collections import Counter

from .serializer import diff_states


def _build_tree(trace_doc):
    calls = {}
    roots = []
    for ev in trace_doc.get("events", []):
        if ev["type"] == "call":
            node = {
                "call_id": ev["call_id"], "function": ev["function"], "category": ev.get("category"),
                "args": ev.get("args", {}), "call_ts": ev.get("timestamp"), "return_ts": None,
                "return_value": None, "exceptions": [], "children": [],
            }
            calls[ev["call_id"]] = node
            parent = calls.get(ev.get("parent_id"))
            (parent["children"] if parent else roots).append(node)
        elif ev["type"] == "return":
            node = calls.get(ev["call_id"])
            if node:
                node["return_ts"] = ev.get("timestamp")
                node["return_value"] = ev.get("return_value")
        elif ev["type"] == "exception":
            node = calls.get(ev["call_id"])
            if node:
                node["exceptions"].append({"type": ev.get("exception_type"), "message": ev.get("exception_message")})

    if not roots:
        return None
    if len(roots) == 1:
        return roots[0]
    return {"function": "(program)", "category": None, "args": {}, "call_ts": None, "return_ts": None,
            "return_value": None, "exceptions": [], "children": roots}


def _duration_ms(node):
    if node.get("call_ts") is None or node.get("return_ts") is None:
        return None
    return (node["return_ts"] - node["call_ts"]) * 1000


def _align_children(children_a, children_b):
    """Aligns two children lists by function name, returning
    {status: 'matched'|'added'|'removed', a, b} in traversal order."""
    names_a = [c["function"] for c in children_a]
    names_b = [c["function"] for c in children_b]
    sm = difflib.SequenceMatcher(None, names_a, names_b, autojunk=False)
    result = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for a, b in zip(children_a[i1:i2], children_b[j1:j2]):
                result.append({"status": "matched", "a": a, "b": b})
        else:
            for a in children_a[i1:i2]:
                result.append({"status": "removed", "a": a, "b": None})
            for b in children_b[j1:j2]:
                result.append({"status": "added", "a": None, "b": b})
    return result


def _diff_pair(a, b):
    entry = {"function": a["function"], "status": "matched"}
    dur_a, dur_b = _duration_ms(a), _duration_ms(b)
    changed = False
    if dur_a is not None and dur_b is not None:
        delta = round(dur_b - dur_a, 3)
        entry["duration_ms"] = {"a": round(dur_a, 3), "b": round(dur_b, 3), "delta": delta}
        changed = changed or abs(delta) > 0.01

    arg_diff = {}
    for k in set(a["args"]) | set(b["args"]):
        d = diff_states(a["args"].get(k), b["args"].get(k))
        if d:
            arg_diff[k] = d
    if arg_diff:
        entry["arg_diff"] = arg_diff
        changed = True

    ret_diff = diff_states(a["return_value"], b["return_value"])
    if ret_diff:
        entry["return_diff"] = ret_diff
        changed = True

    exc_a = {(e["type"], e["message"]) for e in a["exceptions"]}
    exc_b = {(e["type"], e["message"]) for e in b["exceptions"]}
    if exc_a != exc_b:
        entry["exception_diff"] = {
            "only_in_a": sorted(exc_a - exc_b), "only_in_b": sorted(exc_b - exc_a),
        }
        changed = True

    children_result = [_diff_entry(p) for p in _align_children(a["children"], b["children"])]
    entry["children"] = children_result
    changed = changed or any(c["status"] != "matched" or c.get("changed") for c in children_result)
    entry["changed"] = changed
    return entry


def _diff_entry(pair):
    if pair["status"] == "matched":
        return _diff_pair(pair["a"], pair["b"])
    if pair["status"] == "removed":
        return {"function": pair["a"]["function"], "status": "removed", "changed": True, "children": []}
    return {"function": pair["b"]["function"], "status": "added", "changed": True, "children": []}


def _walk_collect(node, on_node):
    on_node(node)
    for c in node.get("children", []):
        _walk_collect(c, on_node)


def build_execution_diff(trace_a, trace_b):
    tree_a, tree_b = _build_tree(trace_a), _build_tree(trace_b)
    if tree_a is None or tree_b is None:
        return {"error": "one or both traces had no call events"}

    tree_diff = _diff_entry(_align_children([tree_a], [tree_b])[0])

    counts_a, counts_b = Counter(), Counter()
    _walk_collect(tree_a, lambda n: counts_a.update([n["function"]]))
    _walk_collect(tree_b, lambda n: counts_b.update([n["function"]]))
    added_functions = sorted(set(counts_b) - set(counts_a))
    removed_functions = sorted(set(counts_a) - set(counts_b))
    call_count_changed = sorted(
        ({"function": fn, "count_a": counts_a.get(fn, 0), "count_b": counts_b.get(fn, 0)}
         for fn in set(counts_a) | set(counts_b) if counts_a.get(fn, 0) != counts_b.get(fn, 0)),
        key=lambda e: e["function"],
    )

    exc_a, exc_b = [], []
    _walk_collect(tree_a, lambda n: exc_a.extend((n["function"], e["type"], e["message"]) for e in n["exceptions"]))
    _walk_collect(tree_b, lambda n: exc_b.extend((n["function"], e["type"], e["message"]) for e in n["exceptions"]))
    set_a, set_b = set(exc_a), set(exc_b)
    exception_diff = {"only_in_a": sorted(set_a - set_b), "only_in_b": sorted(set_b - set_a), "in_both": sorted(set_a & set_b)}

    dur_a, dur_b = {}, {}
    _walk_collect(tree_a, lambda n: _duration_ms(n) is not None and dur_a.setdefault(n["function"], []).append(_duration_ms(n)))
    _walk_collect(tree_b, lambda n: _duration_ms(n) is not None and dur_b.setdefault(n["function"], []).append(_duration_ms(n)))
    latency_diff = []
    for fn in sorted(set(dur_a) | set(dur_b)):
        a_vals, b_vals = dur_a.get(fn, []), dur_b.get(fn, [])
        if a_vals and b_vals:
            avg_a, avg_b = sum(a_vals) / len(a_vals), sum(b_vals) / len(b_vals)
            latency_diff.append({"function": fn, "avg_a_ms": round(avg_a, 3), "avg_b_ms": round(avg_b, 3),
                                  "delta_ms": round(avg_b - avg_a, 3), "count_a": len(a_vals), "count_b": len(b_vals)})
    latency_diff.sort(key=lambda e: -abs(e["delta_ms"]))

    cat_a, cat_b = Counter(), Counter()
    _walk_collect(tree_a, lambda n: n.get("category") and cat_a.update([n["category"]]))
    _walk_collect(tree_b, lambda n: n.get("category") and cat_b.update([n["category"]]))
    category_diff = {cat: {"a": cat_a.get(cat, 0), "b": cat_b.get(cat, 0)} for cat in sorted(set(cat_a) | set(cat_b))}

    return {
        "schema_version": "0.1.0",
        "summary": {
            "call_count_a": sum(counts_a.values()), "call_count_b": sum(counts_b.values()),
            "path_changed": bool(tree_diff.get("changed")),
            "added_functions": added_functions, "removed_functions": removed_functions,
            "call_count_changed": call_count_changed,
        },
        "exception_diff": exception_diff,
        "latency_diff": latency_diff[:20],
        "category_diff": category_diff,
        "tree_diff": tree_diff,
    }
