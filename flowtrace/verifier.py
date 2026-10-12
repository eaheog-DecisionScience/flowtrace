from typing import Any, Dict, List
from .execution_diff import build_execution_diff
from .causality import find_exceptions


def verify_patch(trace_before: Dict[str, Any], trace_after: Dict[str, Any],
                 latency_regression_threshold_pct: float = 50.0) -> Dict[str, Any]:
    """Autonomously verifies whether a code patch resolved a bug without regressions."""
    diff = build_execution_diff(trace_before, trace_after)
    before_excs = find_exceptions(trace_before)
    after_excs = find_exceptions(trace_after)

    summary = diff.get("summary", {})
    exc_diff = diff.get("exception_diff", {})
    lat_diff = diff.get("latency_diff", [])

    # 1. Exception analysis
    before_exc_types = {e.get("exception_type") for e in before_excs}
    after_exc_types = {e.get("exception_type") for e in after_excs}
    
    fixed_exceptions = list(before_exc_types - after_exc_types)
    remaining_exceptions = list(before_exc_types & after_exc_types)
    new_exceptions = list(after_exc_types - before_exc_types)

    # 2. Path analysis
    restored_functions = summary.get("removed_functions", [])  # functions in B that weren't in A or vice versa
    # Note: in build_execution_diff:
    # removed_functions = functions only in A (meaning missing in B)
    # added_functions = functions only in B (meaning newly executed in B)
    newly_executed_functions = summary.get("added_functions", [])
    dropped_functions = summary.get("removed_functions", [])

    # 3. Latency regressions
    # Ignore functions that aborted early with exceptions in the baseline
    crashed_before_funcs = {e.get("function") for e in before_excs}
    regressed_functions = []
    for lc in lat_diff:
        fn_name = lc.get("function", "")
        if fn_name in crashed_before_funcs or any(fn_name.endswith("." + cf) for cf in crashed_before_funcs if cf):
            continue
        avg_a = lc.get("avg_a_ms", 0.0)
        avg_b = lc.get("avg_b_ms", 0.0)
        delta = avg_b - avg_a
        # Require both a meaningful percentage spike AND a real absolute duration increase (> 10ms)
        if avg_a > 2.0 and delta > 10.0:
            pct_change = (delta / avg_a) * 100.0
            if pct_change >= latency_regression_threshold_pct:
                regressed_functions.append({
                    "function": fn_name,
                    "before_ms": round(avg_a, 2),
                    "after_ms": round(avg_b, 2),
                    "pct_increase": round(pct_change, 1)
                })

    # 4. Final Verdict Logic
    verdict = "SAFE_TO_MERGE"
    reasons = []

    if remaining_exceptions:
        verdict = "BUG_STILL_PRESENT"
        reasons.append(f"Original exception(s) {remaining_exceptions} still occurred in the patched run.")
    elif new_exceptions:
        verdict = "REGRESSION_DETECTED"
        reasons.append(f"Patch introduced new exception(s): {new_exceptions}.")
    elif regressed_functions:
        verdict = "PERFORMANCE_REGRESSION"
        reasons.append(f"{len(regressed_functions)} function(s) suffered >{latency_regression_threshold_pct}% latency spikes.")
    else:
        reasons.append("All original exceptions eliminated with zero new errors.")
        if newly_executed_functions:
            reasons.append(f"Successfully unblocked {len(newly_executed_functions)} downstream function(s): {newly_executed_functions[:3]}.")

    return {
        "verdict": verdict,
        "is_safe": verdict == "SAFE_TO_MERGE",
        "fixed_exceptions": fixed_exceptions,
        "remaining_exceptions": remaining_exceptions,
        "new_exceptions": new_exceptions,
        "unblocked_functions": newly_executed_functions,
        "dropped_functions": dropped_functions,
        "latency_regressions": regressed_functions,
        "reasons": reasons,
        "summary": f"Verdict: {verdict}. {' '.join(reasons)}"
    }
