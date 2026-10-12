"""FlowTrace MCP server - exposes everything already built (index, nav,
impact, dataflow, graph, diff, why, git) as tools an AI agent can call
directly to understand a codebase, instead of grepping through files or
asking a human to run the CLI and paste output back in.

This is the "architecture for AI agents navigating codebases on behalf of
humans" piece: every tool here wraps an already-tested backend function -
no new analysis logic lives in this file, only the MCP plumbing (schema
declaration, JSON serialization, error->tool-error translation).

Run with:
    python -m flowtrace.mcp_server

Then point any MCP client (Claude Desktop, Claude Code, etc.) at it via
stdio. Config example for Claude Desktop's claude_desktop_config.json:
    {"mcpServers": {"flowtrace": {"command": "python",
                                   "args": ["-m", "flowtrace.mcp_server"]}}}
"""

import json
from pathlib import Path

from mcp.server import MCPServer

from .indexer import build_index, get_cached_call_graph, save_cached_call_graph
from .callgraph import build_call_graph
from .navigation import build_navigation_index, find_symbol, get_source_snippet
from .impact import build_impact
from .dataflow import build_dataflow
from .graph import build_graph
from .execution_diff import build_execution_diff
from .causality import build_why_report
from .gitintel import is_git_repo, get_commit_log, build_commit_impact, find_last_commit_for_symbol, correlate_why_with_git
from .discovery import discover_entrypoint, find_project_root
from .compress import compress_trace

mcp = MCPServer(
    name="flowtrace",
    description="Static + runtime code understanding for Python projects: "
                 "project structure, call graphs, impact analysis, data flow, "
                 "causal exception analysis, and git correlation.",
)

_session_cache = {}


def _index_and_callgraph(path, force_refresh=False):
    project_path = Path(path).resolve()
    if not project_path.is_dir():
        raise ValueError(f"not a directory: {project_path}")

    path_key = str(project_path)
    if not force_refresh and path_key in _session_cache:
        return project_path, _session_cache[path_key]["index_doc"], _session_cache[path_key]["call_graph"]

    index_doc = build_index(project_path, use_cache=not force_refresh)
    call_graph = None if force_refresh else get_cached_call_graph(project_path)
    if call_graph is None:
        call_graph = build_call_graph(index_doc, project_path)
        save_cached_call_graph(project_path, call_graph)

    _session_cache[path_key] = {
        "index_doc": index_doc,
        "call_graph": call_graph,
    }
    return project_path, index_doc, call_graph


def _resolve_trace(trace, trace_file):
    """Accepts either an inline trace dict or a path to a trace.json - so
    tools work equally well chained directly off flowtrace_run's output
    (no disk round-trip) or pointed at a file a human already produced."""
    if trace is not None:
        return trace
    if trace_file:
        path = Path(trace_file)
        if not path.is_file():
            raise ValueError(f"trace file not found: {path}")
        return json.loads(path.read_text())
    raise ValueError("provide either `trace` (inline) or `trace_file` (a path to a trace.json)")


@mcp.tool()
def flowtrace_overview(path: str) -> dict:
    """Get a high-level structural overview of a Python project: file/class/
    function counts, the top-level folder layout, and external dependency
    categories. Call this FIRST when exploring an unfamiliar codebase."""
    project_path, index_doc, call_graph = _index_and_callgraph(path)
    return {
        "root_dir": str(project_path),
        "file_count": index_doc["file_count"],
        "class_count": index_doc["class_count"],
        "function_count": index_doc["function_count"],
        "external_dependencies_by_category": index_doc["external_dependencies_by_category"],
        "folder_tree": index_doc["folder_tree"],
        "static_call_edges": len(call_graph),
    }


@mcp.tool()
def flowtrace_search_symbols(path: str, query: str) -> dict:
    """Find functions/methods/classes by name (exact, dotted suffix like
    'ClassName.method', or substring). Use this to resolve a name you've
    seen (in an error message, a question, or a file) to its exact
    qualified name before calling flowtrace_navigate or flowtrace_impact."""
    project_path, index_doc, call_graph = _index_and_callgraph(path)
    nav_index = build_navigation_index(index_doc, call_graph)
    matches = find_symbol(nav_index, query)
    return {"query": query, "match_count": len(matches), "matches": matches[:30]}


@mcp.tool()
def flowtrace_navigate(path: str, symbol: str) -> dict:
    """For a specific function/method/class (use its exact qualified name -
    call flowtrace_search_symbols first if unsure), returns its source
    code, docstring, static callers, static callees (including categorized
    external calls like database/network), and any tests that cover it."""
    project_path, index_doc, call_graph = _index_and_callgraph(path)
    nav_index = build_navigation_index(index_doc, call_graph)
    matches = find_symbol(nav_index, symbol)
    if not matches:
        return {"error": f"no symbol matching '{symbol}' found"}
    if len(matches) > 1:
        return {"ambiguous": True, "candidates": matches[:20]}
    rec = dict(nav_index[matches[0]])
    if rec.get("kind") != "class":
        rec["source"] = get_source_snippet(project_path, rec["file"], rec["line"], rec["end_line"])
    return rec


@mcp.tool()
def flowtrace_impact(path: str, symbol: str, max_depth: int = None) -> dict:
    """'What happens if I change this?' - transitive callers (what might
    break, what to re-test), transitive callees, affected files/tests,
    API routes reachable upstream, and database/network operations
    reachable downstream, for a given function/method."""
    project_path, index_doc, call_graph = _index_and_callgraph(path)
    nav_index = build_navigation_index(index_doc, call_graph)
    function_qnames = {fn["qualified_name"] for fn in index_doc["functions"]}
    matches = [m for m in find_symbol(nav_index, symbol) if m in function_qnames]
    if not matches:
        return {"error": f"no function/method matching '{symbol}' found (impact analysis needs a function or method, not a class)"}
    if len(matches) > 1:
        return {"ambiguous": True, "candidates": matches[:20]}
    impact = build_impact(index_doc, call_graph, matches[0], max_depth=max_depth)
    if is_git_repo(project_path):
        impact["last_commit"] = find_last_commit_for_symbol(project_path, index_doc, matches[0])
    return impact


@mcp.tool()
def flowtrace_run(script_or_dir: str, args: list = None, output_path: str = None,
                   compress: bool = False, loop_threshold: int = 5) -> dict:
    """Executes a Python script (or auto-discovers the entry point in a
    directory) under a runtime tracer and returns the resulting execution
    trace summary. THIS RUNS THE TARGET CODE - only call it on code the
    user trusts and wants executed, same as you would a shell command.

    Set compress=True to collapse long runs of repeated loop calls before
    returning (recommended for anything that loops a lot - keeps the
    response within a reasonable context budget; never collapses a run
    that contains an exception). Set output_path to also write the
    (uncompressed) trace to disk for later reference with flowtrace_why/
    flowtrace_diff's trace_file parameter."""
    from .tracer import Tracer
    import runpy
    import sys as _sys
    import io
    import contextlib

    target = Path(script_or_dir).resolve()
    if not target.exists():
        return {"error": f"not found: {target}"}

    if target.is_dir():
        entry_path, project_root, ranked = discover_entrypoint(target)
        if entry_path is None:
            return {"error": f"no Python files found under {target}"}
    else:
        entry_path, project_root = target, find_project_root(target)

    tracer = Tracer(target_dir=project_root)
    old_argv = _sys.argv
    _sys.argv = [str(entry_path)] + (args or [])
    _sys.path.insert(0, str(project_root))
    _sys.path.insert(0, str(entry_path.parent))

    # CRITICAL: MCP over stdio uses stdout exclusively for the JSON-RPC
    # protocol - if the traced script prints anything, it corrupts the
    # protocol stream and breaks the client session. Capture it instead.
    captured_stdout, captured_stderr = io.StringIO(), io.StringIO()
    tracer.start()
    error = None
    try:
        with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(captured_stderr):
            runpy.run_path(str(entry_path), run_name="__main__")
    except SystemExit:
        pass
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    finally:
        tracer.stop()
        _sys.argv = old_argv

    doc = tracer.to_trace_doc()

    if output_path:
        Path(output_path).write_text(json.dumps(doc, indent=2, default=str))

    result_trace = compress_trace(doc, loop_threshold=loop_threshold) if compress else doc

    return {
        "entry_point": str(entry_path.relative_to(project_root)),
        "event_count": result_trace["event_count"],
        "runtime_error": error,
        "stdout": captured_stdout.getvalue(),
        "stderr": captured_stderr.getvalue(),
        "written_to": output_path,
        "trace": result_trace,  # pass straight into flowtrace_why/flowtrace_dataflow/flowtrace_diff
    }


@mcp.tool()
def flowtrace_run_test(project_dir: str, test_target: str = None, args: list = None,
                       output_path: str = None) -> dict:
    """Executes a test target using pytest in an isolated subprocess and returns execution results and failures.

    project_dir: Root directory of the Python project.
    test_target: Path or node ID for the test (e.g. 'test_call_resolution.py::TestPhase1TargetPatterns::test_01_direct_assignment').
    args: Extra CLI flags passed to pytest, e.g. ['-v', '-k', 'constructor'].
    output_path: Optional path to save the execution output JSON.
    """
    import subprocess
    import sys as _sys

    project_root = Path(project_dir).resolve()
    if not project_root.exists():
        return {"error": f"project directory not found: {project_root}"}

    cmd = [_sys.executable, "-m", "pytest"]
    if test_target:
        cmd.append(test_target)
    if args:
        cmd.extend(args)

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=120,
        )
        stdout, stderr = proc.stdout, proc.stderr
        exit_code = proc.returncode
        error = None
    except subprocess.TimeoutExpired:
        stdout, stderr = "", "Test execution timed out after 120 seconds"
        exit_code = -1
        error = "Timeout"
    except Exception as e:
        stdout, stderr = "", str(e)
        exit_code = -1
        error = f"{type(e).__name__}: {e}"

    res = {
        "project_root": str(project_root),
        "test_target": test_target,
        "pytest_exit_code": exit_code,
        "passed": exit_code == 0,
        "error": error,
        "stdout": stdout,
        "stderr": stderr,
    }
    if output_path:
        Path(output_path).write_text(json.dumps(res, indent=2), encoding="utf-8")
    return res


@mcp.tool()
def flowtrace_dataflow(trace: dict = None, trace_file: str = None) -> dict:
    """Reconstructs value flow between calls in a trace: "this call's
    return value became that call's argument." Heuristic structural
    matching, not object identity. Accepts either an inline trace (e.g.
    straight from flowtrace_run's output) or trace_file, a path to a
    trace.json on disk."""
    return build_dataflow(_resolve_trace(trace, trace_file))


@mcp.tool()
def flowtrace_why(trace: dict = None, trace_file: str = None, exception_type: str = None, git_path: str = None) -> dict:
    """Given a trace that contains an exception (inline `trace`, e.g.
    straight from flowtrace_run, or `trace_file` - a path to a trace.json
    on disk), reconstructs the full causal call chain from the entry
    point to where it actually originated - correctly collapsing an
    exception that propagated through multiple frames into one report at
    the true origin. Pass git_path (a git repo directory) to also see the
    last commit that touched each frame in the chain."""
    report = build_why_report(_resolve_trace(trace, trace_file), exception_type=exception_type)
    if "error" in report:
        return report
    if git_path:
        git_root = Path(git_path).resolve()
        if is_git_repo(git_root):
            git_index = build_index(git_root)
            report = correlate_why_with_git(git_root, git_index, report)
    return report


@mcp.tool()
def flowtrace_diff(trace_a: dict = None, trace_b: dict = None, trace_a_file: str = None, trace_b_file: str = None) -> dict:
    """Compares two runtime traces taken before/after a code change: did
    the execution path change, which functions were added/removed, did
    exceptions change, how did latency shift. Each trace can be given
    inline (trace_a/trace_b) or as a file path (trace_a_file/trace_b_file).
    Traces are matched structurally (by function name at each call depth),
    not by id, since separate runs never share call ids."""
    return build_execution_diff(_resolve_trace(trace_a, trace_a_file), _resolve_trace(trace_b, trace_b_file))


@mcp.tool()
def flowtrace_git_log(path: str, function: str = None, limit: int = 20) -> dict:
    """Lists recent git commits, optionally filtered to only commits whose
    diff actually overlaps a specific function/method's line range (not
    just "touched the file" - a real per-symbol history)."""
    project_path = Path(path).resolve()
    if not is_git_repo(project_path):
        return {"error": f"{project_path} is not a git repository"}
    if function:
        project_path, index_doc, call_graph = _index_and_callgraph(path)
        nav_index = build_navigation_index(index_doc, call_graph)
        matches = find_symbol(nav_index, function)
        if not matches:
            return {"error": f"no symbol matching '{function}' found"}
        if len(matches) > 1:
            return {"ambiguous": True, "candidates": matches[:20]}
        from .gitintel import get_changed_line_ranges
        entity = next((f for f in index_doc["functions"] if f["qualified_name"] == matches[0]), None) \
            or next((c for c in index_doc["classes"] if c["qualified_name"] == matches[0]), None)
        candidates = get_commit_log(project_path, max_commits=max(limit * 5, 100), path_filter=entity["file"])
        commits = [
            c for c in candidates
            if any(entity["line"] <= end and start <= entity["end_line"]
                   for start, end in get_changed_line_ranges(project_path, c["hash"], entity["file"]))
        ][:limit]
        return {"symbol": matches[0], "commits": commits}
    return {"commits": get_commit_log(project_path, max_commits=limit)}


@mcp.tool()
def flowtrace_git_impact(path: str, commit: str, max_depth: int = None) -> dict:
    """What did a specific git commit change, and what does that
    transitively affect (files, functions) according to the static call
    graph? Answers "what could this commit have broken." """
    project_path, index_doc, call_graph = _index_and_callgraph(path)
    if not is_git_repo(project_path):
        return {"error": f"{project_path} is not a git repository"}
    try:
        return build_commit_impact(project_path, index_doc, call_graph, commit, max_depth=max_depth)
    except RuntimeError as e:
        return {"error": str(e)}


@mcp.tool()
def flowtrace_spec(project_dir: str, symbol: str, trace_file: str = None) -> dict:
    """Extracts function contract, parameter types, default values, docstrings,
    observed runtime argument/return schemas, and synthesized usage code examples."""
    from .spec import extract_function_spec
    trace_doc = None
    if trace_file:
        p = Path(trace_file).resolve()
        if p.is_file():
            trace_doc = json.loads(p.read_text(encoding="utf-8"))
    return extract_function_spec(project_dir, symbol, trace_data=trace_doc)


@mcp.tool()
def flowtrace_mock(trace_file: str, target_function: str = None) -> dict:
    """Generates standalone pytest unit test code with mocks synthesized from recorded
    runtime trace events (database, network, or external calls)."""
    from .mockgen import generate_mock_test
    p = Path(trace_file).resolve()
    if not p.is_file():
        return {"error": f"trace file not found: {trace_file}"}
    trace_doc = json.loads(p.read_text(encoding="utf-8"))
    test_code = generate_mock_test(trace_doc, target_function=target_function)
    return {"target_function": target_function, "generated_test_code": test_code}


@mcp.tool()
def flowtrace_perf(trace_file: str, threshold: int = 4) -> dict:
    """Analyzes a runtime trace for N+1 database queries, repetitive network loops,
    and latency bottlenecks, returning actionable refactoring advice."""
    from .perf import analyze_performance
    p = Path(trace_file).resolve()
    if not p.is_file():
        return {"error": f"trace file not found: {trace_file}"}
    trace_doc = json.loads(p.read_text(encoding="utf-8"))
    return analyze_performance(trace_doc, n_plus_one_threshold=threshold)


@mcp.tool()
def flowtrace_verify_patch(trace_before_file: str, trace_after_file: str) -> dict:
    """Autonomously verifies whether a code patch resolved a bug without regressions
    by comparing before-and-after execution traces."""
    from .verifier import verify_patch
    p_a = Path(trace_before_file).resolve()
    p_b = Path(trace_after_file).resolve()
    if not p_a.is_file() or not p_b.is_file():
        return {"error": f"one or both trace files not found: {trace_before_file}, {trace_after_file}"}
    doc_a = json.loads(p_a.read_text(encoding="utf-8"))
    doc_b = json.loads(p_b.read_text(encoding="utf-8"))
    return verify_patch(doc_a, doc_b)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

