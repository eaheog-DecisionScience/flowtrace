"""Causal exception analysis ("why did this happen?"): for an exception
observed in a runtime trace, reconstructs the full call chain from the
entry point down to where it occurred, each frame annotated with its
arguments - not just a stack trace, but what data led to the failure.

Subtlety this module has to handle: Python's trace machinery fires an
'exception' event in *every* frame the exception propagates through, not
just where it was raised. A ValueError raised three calls deep and
uncaught produces three exception events (same type/message, different
call_id/depth) - one per frame it passed through. Naively reporting on
"every exception event matching this type" would print the same failure
three times. Instead, matching events are grouped by (type, message) and
only the *deepest* occurrence - the frame closest to where it actually
originated - is used to build the causal chain.
"""


def find_exceptions(trace_doc, exception_type=None, call_id=None):
    return [
        ev for ev in trace_doc.get("events", [])
        if ev["type"] == "exception"
        and (exception_type is None or ev.get("exception_type") == exception_type)
        and (call_id is None or ev.get("call_id") == call_id)
    ]


def build_causal_chain(trace_doc, exception_event):
    calls_by_id = {ev["call_id"]: ev for ev in trace_doc.get("events", []) if ev["type"] == "call"}
    returns_by_id = {ev["call_id"]: ev for ev in trace_doc.get("events", []) if ev["type"] == "return"}

    origin_id = exception_event.get("call_id")
    chain = []
    cur_id = origin_id
    while cur_id is not None and cur_id in calls_by_id:
        call_ev = calls_by_id[cur_id]
        ret_ev = returns_by_id.get(cur_id)
        chain.append({
            "call_id": cur_id, "function": call_ev["function"], "module": call_ev.get("module"),
            "file": call_ev.get("file"), "line": call_ev.get("line"), "args": call_ev.get("args", {}),
            "category": call_ev.get("category"),
            "return_value": ret_ev.get("return_value") if ret_ev else None,
            "is_origin": cur_id == origin_id,
        })
        cur_id = call_ev.get("parent_id")
    chain.reverse()  # entry point first, origin last - the actual causal order

    return {
        "exception_type": exception_event.get("exception_type"),
        "exception_message": exception_event.get("exception_message"),
        "origin_call_id": origin_id,
        "origin_function": chain[-1]["function"] if chain else exception_event.get("function"),
        "depth": len(chain),
        "chain": chain,
    }


def build_why_report(trace_doc, exception_type=None, call_id=None):
    matches = find_exceptions(trace_doc, exception_type=exception_type, call_id=call_id)
    if not matches:
        return {"error": "no matching exception found in trace"}

    if call_id is not None:
        chosen = matches  # explicit call_id: report exactly that occurrence, no grouping
    else:
        # dedupe propagation: same exception seen in multiple frames as it
        # unwinds -> keep only the deepest (truest-to-origin) occurrence
        # per (type, message).
        deepest = {}
        for ev in matches:
            key = (ev.get("exception_type"), ev.get("exception_message"))
            if key not in deepest or ev.get("depth", 0) > deepest[key].get("depth", 0):
                deepest[key] = ev
        chosen = list(deepest.values())

    reports = [build_causal_chain(trace_doc, ev) for ev in chosen]
    return {"schema_version": "0.1.0", "match_count": len(reports), "reports": reports}
