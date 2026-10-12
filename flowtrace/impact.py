"""Impact Analysis (Priority 4): "what happens if I change this?" -
combines transitive static callers/callees, the files/tests they live in,
and - when a runtime trace is supplied - which of those relationships
were actually observed executing, into one answer for a given symbol.

Impact here has two directions, and both matter for a change:
    upstream (transitive callers)  - what might break; what to re-test
    downstream (transitive callees) - what this reaches out into: other
                                       files, and external systems
                                       (database/network calls) whose
                                       behavior this function's output
                                       could indirectly affect
"""

from .navigation import _is_test_function

API_DECORATOR_PATTERNS = (
    ".route(", ".get(", ".post(", ".put(", ".delete(", ".patch(", ".websocket(",
)


def _looks_like_api_endpoint(fn):
    return any(p in d for d in fn.get("decorators", []) for p in API_DECORATOR_PATTERNS)


def _build_adjacency(call_graph):
    callers_of, callees_of = {}, {}
    for e in call_graph:
        if not e["callee"]:
            continue
        callers_of.setdefault(e["callee"], set()).add(e["caller"])
        callees_of.setdefault(e["caller"], set()).add(e["callee"])
    return callers_of, callees_of


def _transitive_closure(start, adjacency, max_depth=None):
    """BFS closure over `adjacency` starting at `start`. Returns
    {qualified_name: depth}, excluding `start` itself."""
    seen = {start: 0}
    frontier = [start]
    depth = 0
    while frontier and (max_depth is None or depth < max_depth):
        depth += 1
        next_frontier = []
        for node in frontier:
            for neighbor in adjacency.get(node, ()):
                if neighbor not in seen:
                    seen[neighbor] = depth
                    next_frontier.append(neighbor)
        frontier = next_frontier
    seen.pop(start, None)
    return seen


def _downstream_external_calls(downstream_scope, call_graph, category):
    scope = set(downstream_scope)
    return [
        {"from": e["caller"], "expr": e["callee_expr"], "category": e["category"], "file": e["file"], "line": e["line"]}
        for e in call_graph
        if e["caller"] in scope and not e["callee"] and e["category"] == category
    ]


def _observed_runtime_paths(trace_doc, symbol, max_samples=5):
    """Best-effort: finds calls in the trace matching this symbol (by full
    module.function guess, falling back to bare function name if that
    finds nothing) and reconstructs the caller chain each was observed
    under, via parent_id."""
    events_by_id = {ev["call_id"]: ev for ev in trace_doc.get("events", []) if ev["type"] == "call"}
    bare_name = symbol.split(".")[-1]

    exact = [ev for ev in events_by_id.values() if f"{ev.get('module', '')}.{ev['function']}" == symbol]
    matched = exact or [ev for ev in events_by_id.values() if ev["function"] == bare_name]

    paths = []
    for ev in matched:
        chain = [ev["function"]]
        cur = ev
        while cur.get("parent_id") is not None and cur["parent_id"] in events_by_id:
            cur = events_by_id[cur["parent_id"]]
            chain.append(cur["function"])
        chain.reverse()
        paths.append({"call_id": ev["call_id"], "path": chain})

    return {"match_count": len(matched), "match_type": "exact" if exact else "by_name", "sample_paths": paths[:max_samples]}


def _find_possible_callers(call_graph, bare_method_name):
    """Unresolved call sites ANYWHERE in the project whose call expression
    ends in `.bare_method_name` - these are call sites that COULD be
    calling `symbol` (the resolver just couldn't prove the receiver's
    type - see inference.py). This exists because real-world validation
    (see README) found the single most dangerous failure mode: an
    unresolved call site is not evidence that no call exists, but
    `direct_callers`/`transitive_callers` alone can't distinguish "proven
    zero callers" from "resolution just failed here." This field is that
    distinction, made explicit instead of silently absent."""
    seen, results = set(), []
    for e in call_graph:
        if e["callee"]:
            continue
        expr_tail = e["callee_expr"].rsplit(".", 1)[-1].split("(")[0]
        if expr_tail != bare_method_name:
            continue
        key = (e["caller"], e["line"])
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "caller": e["caller"], "file": e["file"], "line": e["line"],
            "expr": e["callee_expr"], "possible_callees": e.get("possible_callees") or [],
        })
    return results


def _confidence_label(proven_count, possible_count):
    if proven_count > 0 and possible_count == 0:
        return "high"
    if possible_count > 0:
        return "medium" if proven_count > 0 else "low"
    return "unverified"  # 0 proven AND 0 possible - likely a true entry point, but say so, don't imply proof


def build_impact(index_doc, call_graph, symbol, trace_doc=None, max_depth=None):
    functions_by_qname = {fn["qualified_name"]: fn for fn in index_doc["functions"]}
    callers_of, callees_of = _build_adjacency(call_graph)

    upstream = _transitive_closure(symbol, callers_of, max_depth)
    downstream = _transitive_closure(symbol, callees_of, max_depth)

    affected_files = sorted({functions_by_qname[q]["file"] for q in upstream if q in functions_by_qname})
    affected_tests = sorted(q for q in upstream if q in functions_by_qname and _is_test_function(functions_by_qname[q]))
    api_endpoints = sorted(q for q in upstream if q in functions_by_qname and _looks_like_api_endpoint(functions_by_qname[q]))

    db_ops = _downstream_external_calls(set(downstream) | {symbol}, call_graph, "Database")
    network_ops = _downstream_external_calls(set(downstream) | {symbol}, call_graph, "Network")

    bare_name = symbol.rsplit(".", 1)[-1]
    possible_callers = _find_possible_callers(call_graph, bare_name)
    direct_callers = sorted(callers_of.get(symbol, []))
    confidence = _confidence_label(len(direct_callers), len(possible_callers))

    observed = _observed_runtime_paths(trace_doc, symbol) if trace_doc else None

    return {
        "symbol": symbol,
        "direct_callers": direct_callers,
        "direct_callees": sorted(callees_of.get(symbol, [])),
        "transitive_callers": sorted(upstream.keys()),
        "transitive_callees": sorted(downstream.keys()),
        # NOTE (scope boundary): affected_files/affected_tests/api_endpoints
        # are computed from the PROVEN transitive-caller closure only -
        # they do NOT expand through possible_callers. Expanding "possibly
        # calls X" transitively would compound uncertainty combinatorially
        # (a possible caller of a possible caller...) without a principled
        # way to bound it. possible_callers is reported separately instead
        # of silently folded in either direction.
        "affected_files": affected_files,
        "affected_tests": affected_tests,
        "api_endpoints": api_endpoints,
        "database_operations": db_ops,
        "network_operations": network_ops,
        "possible_callers": possible_callers,
        "impact_confidence": confidence,
        "observed_runtime_paths": observed,
        "summary": {
            "functions": len(upstream), "files": len(affected_files), "tests": len(affected_tests),
            "api_routes": len(api_endpoints), "database_operations": len(db_ops),
            "possible_callers": len(possible_callers), "impact_confidence": confidence,
        },
    }
