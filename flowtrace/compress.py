"""Trace compression for AI/agent context budgets: collapses long runs of
repeated same-function sibling calls (a tight loop) into one
representative call plus a summary, so an agent doesn't burn its context
window on "compute() called 500 times" as 500 near-identical call/return
pairs.

This is a POST-PROCESSING pass over an already-captured trace - it does
not touch tracer.py or change what gets recorded during execution, only
how a trace is presented afterward. An exception occurring anywhere
inside a run is never hidden: any run containing one is left uncollapsed
entirely, so compression can never be the reason the one signal that
actually mattered got dropped. Intentionally conservative over clever.
"""


def compress_trace(trace_doc, loop_threshold=5, sample_events=1):
    """Returns a new trace doc (input is not mutated) where qualifying
    runs of >= loop_threshold consecutive same-function sibling calls are
    collapsed to `sample_events` full occurrences plus a summary."""
    events = trace_doc.get("events", [])
    exception_call_ids = {ev["call_id"] for ev in events if ev["type"] == "exception"}
    call_events_in_order = [ev for ev in events if ev["type"] == "call"]

    runs = []
    current = None
    for ev in call_events_in_order:
        key = (ev.get("parent_id"), ev["function"])
        if current and current["key"] == key:
            current["call_ids"].append(ev["call_id"])
        else:
            current = {"key": key, "call_ids": [ev["call_id"]]}
            runs.append(current)

    collapse_targets = {}  # call_id -> summary dict (attach to kept call) | "skip" (drop this event)
    for run in runs:
        ids = run["call_ids"]
        if len(ids) < loop_threshold or any(cid in exception_call_ids for cid in ids):
            continue
        kept, collapsed = set(ids[:sample_events]), ids[sample_events:]
        if not collapsed:
            continue
        collapse_targets[ids[0]] = {
            "additional_calls_collapsed": len(collapsed),
            "collapsed_call_ids": collapsed,
            "note": f"{len(collapsed)} more consecutive call(s) to {run['key'][1]} under the same "
                    f"parent were collapsed (loop_threshold={loop_threshold})",
        }
        for cid in collapsed:
            collapse_targets[cid] = "skip"

    new_events = []
    for ev in events:
        target = collapse_targets.get(ev.get("call_id"))
        if target == "skip":
            continue
        if isinstance(target, dict) and ev["type"] == "call":
            ev = {**ev, "_loop_summary": target}
        new_events.append(ev)

    return {
        **trace_doc,
        "events": new_events,
        "event_count": len(new_events),
        "compression": {
            "applied": True, "loop_threshold": loop_threshold, "sample_events": sample_events,
            "original_event_count": len(events), "compressed_event_count": len(new_events),
            "runs_collapsed": sum(1 for v in collapse_targets.values() if isinstance(v, dict)),
        },
    }
