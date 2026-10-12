from collections import defaultdict
from typing import Any, Dict, List


def analyze_performance(trace_data: Dict[str, Any], n_plus_one_threshold: int = 4) -> Dict[str, Any]:
    """Analyzes trace events for N+1 queries, repetitive network loops, and execution bottlenecks."""
    events = trace_data.get("events", [])
    calls = {e.get("call_id"): e for e in events if e.get("type") == "call"}
    returns = {e.get("call_id"): e for e in events if e.get("type") == "return"}

    # Group calls by (parent_id, child_func)
    parent_child_groups = defaultdict(list)
    func_durations = defaultdict(float)
    func_counts = defaultdict(int)

    for cid, call in calls.items():
        fn = call.get("function", "")
        mod = call.get("module", "")
        full_fn = f"{mod}.{fn}" if mod else fn
        pid = call.get("parent_id")
        ret = returns.get(cid)

        duration_ms = 0.0
        if ret and call.get("timestamp") and ret.get("timestamp"):
            duration_ms = max(0.0, (ret["timestamp"] - call["timestamp"]) * 1000.0)

        func_durations[full_fn] += duration_ms
        func_counts[full_fn] += 1

        if pid is not None:
            parent_child_groups[(pid, full_fn)].append({
                "call_id": cid,
                "category": call.get("category", "Logic"),
                "args": call.get("args", {}),
                "duration_ms": duration_ms,
                "file": call.get("file"),
                "line": call.get("line")
            })

    # Detect N+1 patterns
    n_plus_one_warnings = []
    for (pid, child_fn), call_instances in parent_child_groups.items():
        count = len(call_instances)
        if count >= n_plus_one_threshold:
            parent_call = calls.get(pid, {})
            parent_fn = parent_call.get("function", "<unknown_caller>")
            parent_mod = parent_call.get("module", "")
            full_parent = f"{parent_mod}.{parent_fn}" if parent_mod else parent_fn

            total_dur = sum(c["duration_ms"] for c in call_instances)
            categories = {c["category"] for c in call_instances}
            
            is_db = "Database" in categories or "db" in child_fn.lower() or "sql" in child_fn.lower() or "query" in child_fn.lower()
            is_net = "Network" in categories or "request" in child_fn.lower() or "http" in child_fn.lower() or "fetch" in child_fn.lower()

            severity = "LOW"
            advice = "Consider vectorizing or batching calls."
            if is_db:
                severity = "CRITICAL" if count >= 10 else "WARNING"
                advice = f"N+1 Database Query pattern: '{full_parent}' called '{child_fn}' {count} times in a loop. Use bulk queries or prefetch/join."
            elif is_net:
                severity = "CRITICAL" if count >= 10 else "WARNING"
                advice = f"Repetitive Network Calls: '{full_parent}' fired {count} network requests. Batch with bulk API or concurrent gathering."
            elif count >= 20:
                severity = "WARNING"
                advice = f"High iteration loop: {count} calls to '{child_fn}' inside '{full_parent}'."

            if severity in ("WARNING", "CRITICAL"):
                n_plus_one_warnings.append({
                    "parent_function": full_parent,
                    "child_function": child_fn,
                    "call_count": count,
                    "total_duration_ms": round(total_dur, 2),
                    "avg_duration_ms": round(total_dur / count, 2),
                    "severity": severity,
                    "file": call_instances[0].get("file"),
                    "line": call_instances[0].get("line"),
                    "advice": advice
                })

    # Top Hotspots
    sorted_hotspots = sorted(
        func_durations.items(),
        key=lambda x: x[1],
        reverse=True
    )[:5]

    hotspots = [
        {
            "function": fn,
            "total_duration_ms": round(dur, 2),
            "call_count": func_counts[fn]
        }
        for fn, dur in sorted_hotspots
    ]

    overall_severity = "HEALTHY"
    if any(w["severity"] == "CRITICAL" for w in n_plus_one_warnings):
        overall_severity = "CRITICAL"
    elif n_plus_one_warnings:
        overall_severity = "WARNING"

    return {
        "status": overall_severity,
        "n_plus_one_detected": len(n_plus_one_warnings) > 0,
        "warning_count": len(n_plus_one_warnings),
        "warnings": n_plus_one_warnings,
        "top_hotspots": hotspots,
        "summary": f"{len(n_plus_one_warnings)} N+1 anti-pattern(s) detected. Overall status: {overall_severity}."
    }
