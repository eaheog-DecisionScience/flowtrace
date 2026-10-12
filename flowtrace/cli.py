"""Zero-config CLI: `flowtrace run main.py` - no source modification required."""

import argparse
import json
import runpy
import sys
from pathlib import Path

from .tracer import Tracer
from .indexer import build_index
from .discovery import discover_entrypoint, find_project_root
from .callgraph import build_call_graph
from .navigation import build_navigation_index, get_source_snippet, find_symbol
from .graph import build_graph
from .dataflow import build_dataflow
from .impact import build_impact
from .execution_diff import build_execution_diff
from .causality import build_why_report
from .gitintel import (is_git_repo, get_commit_log, build_commit_impact,
                        find_last_commit_for_symbol, correlate_why_with_git,
                        get_changed_line_ranges)
from .llm import LLMClient, LLMConfigError
from .ask import build_answer
from .compress import compress_trace
from .spec import extract_function_spec
from .mockgen import generate_mock_test
from .perf import analyze_performance
from .verifier import verify_patch


def build_parser():
    parser = argparse.ArgumentParser(prog="flowtrace")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run a script or auto-discover a project's entry point, with tracing enabled")
    run_p.add_argument("script", help="Path to a .py script, OR a project directory to auto-discover an entry point in")
    run_p.add_argument("script_args", nargs=argparse.REMAINDER)
    run_p.add_argument("-o", "--output", default="trace.json", help="Output trace JSON path")
    run_p.add_argument("--entry", default=None,
                        help="When `script` is a directory, force this file as the entry point (path relative to that directory), skipping auto-discovery")
    run_p.add_argument("--include-stdlib", action="store_true",
                        help="Include stdlib / vendor call frames (off by default)")
    run_p.add_argument("--max-depth", type=int, default=None,
                        help="Maximum call-stack depth to record")
    run_p.add_argument("--exclude", action="append", default=[],
                        help="Module prefix (script tracing) or directory name (auto-discovery) to exclude (repeatable)")
    run_p.add_argument("--no-redact", action="store_true",
                        help="Disable automatic secret/PII redaction (passwords, tokens, API keys) - on by default")

    index_p = sub.add_parser("index", help="Statically index a project directory (no execution)")
    index_p.add_argument("path", nargs="?", default=".", help="Project root to index (default: current directory)")
    index_p.add_argument("-o", "--output", default="flowtrace-index.json", help="Output index JSON path")
    index_p.add_argument("--exclude", action="append", default=[],
                          help="Directory name to additionally ignore (repeatable)")

    nav_p = sub.add_parser("nav", help="Code navigation: source, callers, callees, and related tests for a symbol")
    nav_p.add_argument("path", nargs="?", default=".", help="Project directory (default: current directory)")
    nav_p.add_argument("query", help="Qualified name or suffix to look up, e.g. 'ClassName.method' or 'login'")
    nav_p.add_argument("-o", "--output", default=None, help="Also write the full navigation record as JSON")
    nav_p.add_argument("--exclude", action="append", default=[],
                        help="Directory name to additionally ignore (repeatable)")

    graph_p = sub.add_parser("graph", help="Build the unified project graph (folders/files/classes/functions/imports/calls, optionally merged with a runtime trace)")
    graph_p.add_argument("path", nargs="?", default=".", help="Project directory (default: current directory)")
    graph_p.add_argument("-o", "--output", default="flowtrace-graph.json", help="Output graph JSON path")
    graph_p.add_argument("--trace", default=None, help="A trace.json from `flowtrace run` to merge in as the runtime layer (adds CALLS_RUNTIME + REALIZES + FLOWS_TO edges)")
    graph_p.add_argument("--run-id", default="run", help="Label for the merged trace's runtime nodes, in case you merge multiple traces later (default: 'run')")
    graph_p.add_argument("--no-source", action="store_true", help="Don't embed source snippets in function/class/method nodes (smaller output)")
    graph_p.add_argument("--exclude", action="append", default=[],
                          help="Directory name to additionally ignore (repeatable)")

    dataflow_p = sub.add_parser("dataflow", help="Reconstruct value flow between calls from a runtime trace")
    dataflow_p.add_argument("trace", help="A trace.json from `flowtrace run`")
    dataflow_p.add_argument("-o", "--output", default=None, help="Write the full edge list as JSON to this path")

    impact_p = sub.add_parser("impact", help="Impact analysis: what depends on this symbol, and what it reaches (files, tests, API routes, DB/network calls, observed runtime paths)")
    impact_p.add_argument("path", nargs="?", default=".", help="Project directory (default: current directory)")
    impact_p.add_argument("query", help="Qualified name or suffix to analyze, e.g. 'LoginHandler.login'")
    impact_p.add_argument("--trace", default=None, help="A trace.json to check for observed runtime paths through this symbol")
    impact_p.add_argument("--max-depth", type=int, default=None, help="Limit transitive caller/callee search depth (default: unlimited)")
    impact_p.add_argument("-o", "--output", default=None, help="Write the full impact record as JSON")
    impact_p.add_argument("--exclude", action="append", default=[],
                           help="Directory name to additionally ignore (repeatable)")
    impact_p.add_argument("--show-git", action="store_true", help="Also show the last commit that touched this symbol (requires `path` to be a git repo)")

    diff_p = sub.add_parser("diff", help="Compare two execution traces: path changes, new/resolved exceptions, latency, argument/return differences")
    diff_p.add_argument("trace_a", help="First trace.json (baseline / 'before')")
    diff_p.add_argument("trace_b", help="Second trace.json ('after')")
    diff_p.add_argument("-o", "--output", default=None, help="Write the full diff (including per-call tree diff) as JSON")

    why_p = sub.add_parser("why", help="Causal exception analysis: the full call chain leading to an exception observed in a trace")
    why_p.add_argument("trace", help="A trace.json from `flowtrace run`")
    why_p.add_argument("exception_type", nargs="?", default=None, help="Filter to this exception type (e.g. 'ValueError'). Omit to report on every exception found.")
    why_p.add_argument("--call-id", type=int, default=None, help="Report on this exact call_id's occurrence instead of grouping by (type, message)")
    why_p.add_argument("--git", default=None, help="Path to the project's git repo/root - correlates each frame in the causal chain with the last commit that touched it")
    why_p.add_argument("-o", "--output", default=None, help="Write the full report as JSON")

    git_log_p = sub.add_parser("git-log", help="List commits, optionally filtered to ones that actually touched a specific function's line range")
    git_log_p.add_argument("path", nargs="?", default=".", help="Project directory (default: current directory)")
    git_log_p.add_argument("--function", default=None, help="Only show commits whose diff overlaps this function's line range (qualified name or suffix)")
    git_log_p.add_argument("--limit", type=int, default=20, help="Max commits to show (default: 20)")
    git_log_p.add_argument("--exclude", action="append", default=[], help="Directory name to additionally ignore when indexing (repeatable)")
    git_log_p.add_argument("-o", "--output", default=None, help="Write full results as JSON")

    git_impact_p = sub.add_parser("git-impact", help="What did this commit change, and what does that transitively affect?")
    git_impact_p.add_argument("path", nargs="?", default=".", help="Project directory (default: current directory)")
    git_impact_p.add_argument("commit", help="Commit hash or revision (e.g. HEAD, HEAD~1)")
    git_impact_p.add_argument("--max-depth", type=int, default=None, help="Limit transitive search depth")
    git_impact_p.add_argument("--exclude", action="append", default=[], help="Directory name to additionally ignore when indexing (repeatable)")
    git_impact_p.add_argument("-o", "--output", default=None, help="Write full results as JSON")

    ask_p = sub.add_parser("ask", help="Ask a natural-language question about the project (evidence-first: an LLM narrates deterministic nav/impact data, never invents it)")
    ask_p.add_argument("path", help="Project directory")
    ask_p.add_argument("question", help="Natural-language question, e.g. 'where does login begin?'")
    ask_p.add_argument("--base-url", default=None, help="OpenAI-compatible API base URL (or set FLOWTRACE_LLM_BASE_URL). For Gemini's free tier: https://generativelanguage.googleapis.com/v1beta/openai")
    ask_p.add_argument("--api-key", default=None, help="API key (or set FLOWTRACE_LLM_API_KEY)")
    ask_p.add_argument("--model", default=None, help="Model name (or set FLOWTRACE_LLM_MODEL), e.g. gpt-4o-mini, gemini-2.0-flash")
    ask_p.add_argument("--exclude", action="append", default=[], help="Directory name to additionally ignore (repeatable)")
    ask_p.add_argument("-o", "--output", default=None, help="Write the full answer + evidence as JSON")

    compress_p = sub.add_parser("compress", help="Collapse repeated loop calls in a trace for smaller/more token-efficient output (never collapses a run containing an exception)")
    compress_p.add_argument("trace", help="A trace.json from `flowtrace run`")
    compress_p.add_argument("--threshold", type=int, default=5, help="Minimum consecutive same-function sibling calls to collapse (default: 5)")
    compress_p.add_argument("-o", "--output", default=None, help="Output path (default: overwrite input)")

    spec_p = sub.add_parser("spec", help="Extract function contract, signature, docstring, and observed runtime schema")
    spec_p.add_argument("path", nargs="?", default=".", help="Project directory (default: .)")
    spec_p.add_argument("symbol", help="Qualified function or method name (e.g. 'enterprise_pipeline.apply_coupon')")
    spec_p.add_argument("--trace", default=None, help="Optional trace.json to merge observed runtime argument and return schemas")
    spec_p.add_argument("-o", "--output", default=None, help="Output JSON path")

    mock_p = sub.add_parser("mock", help="Synthesize standalone pytest test code with mocks from a recorded trace")
    mock_p.add_argument("trace", help="A trace.json from flowtrace run")
    mock_p.add_argument("--target", default=None, help="Target function to test (default: entry point)")
    mock_p.add_argument("-o", "--output", default=None, help="Output test script path (e.g. test_mock.py)")

    perf_p = sub.add_parser("perf", help="Analyze trace for N+1 queries, repetitive network loops, and hotspots")
    perf_p.add_argument("trace", help="A trace.json from flowtrace run")
    perf_p.add_argument("--threshold", type=int, default=4, help="N+1 threshold for repetitive calls (default: 4)")
    perf_p.add_argument("-o", "--output", default=None, help="Output JSON path")

    verify_p = sub.add_parser("verify-patch", help="Autonomously verify if a code patch fixed a bug without regressions")
    verify_p.add_argument("trace_before", help="Failing baseline trace.json")
    verify_p.add_argument("trace_after", help="Patched run trace.json")
    verify_p.add_argument("-o", "--output", default=None, help="Output JSON path")

    return parser


def _resolve_entrypoint(target, entry_override, extra_ignore):
    """Returns (script_path, project_root) for `flowtrace run <target>`.

    `target` may be a specific .py file (traced directly) or a directory
    (entry point auto-discovered, unless --entry overrides it).
    """
    if target.is_file():
        return target, find_project_root(target)

    if not target.is_dir():
        print(f"error: not a file or directory: {target}", file=sys.stderr)
        sys.exit(1)

    project_root = find_project_root(target)

    if entry_override:
        entry_path = (target / entry_override).resolve()
        if not entry_path.is_file():
            print(f"error: --entry {entry_override} not found under {target}", file=sys.stderr)
            sys.exit(1)
        return entry_path, project_root

    best, project_root, ranked = discover_entrypoint(target, extra_ignore=extra_ignore)
    if best is None:
        print(f"error: no Python files found under {target}", file=sys.stderr)
        sys.exit(1)

    print(f"Auto-discovered entry point: {best.relative_to(project_root)}")
    runner_ups = [p for p, _score in ranked[1:4]]
    if runner_ups:
        shown = ", ".join(str(p.relative_to(project_root)) for p in runner_ups)
        print(f"  (also considered: {shown} — pass --entry to override)")

    return best, project_root


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        target = Path(args.script).resolve()
        if not target.exists():
            print(f"error: not found: {target}", file=sys.stderr)
            sys.exit(1)

        script_path, project_root = _resolve_entrypoint(target, args.entry, args.exclude)

        tracer = Tracer(
            include_stdlib=args.include_stdlib,
            max_depth=args.max_depth,
            target_dir=project_root,
            exclude_prefixes=tuple(args.exclude),
            redact_secrets=not args.no_redact,
        )

        old_argv = sys.argv
        sys.argv = [str(script_path)] + args.script_args
        sys.path.insert(0, str(project_root))
        sys.path.insert(0, str(script_path.parent))

        tracer.start()
        try:
            runpy.run_path(str(script_path), run_name="__main__")
        except SystemExit:
            pass
        except Exception as e:
            print(f"warning: traced script raised {type(e).__name__}: {e}", file=sys.stderr)
        finally:
            tracer.stop()
            sys.argv = old_argv

        doc = tracer.to_trace_doc()
        Path(args.output).write_text(json.dumps(doc, indent=2, default=str))
        print(f"Trace written to {args.output} ({doc['event_count']} events)")

    elif args.command == "index":
        project_path = Path(args.path).resolve()
        if not project_path.is_dir():
            print(f"error: not a directory: {project_path}", file=sys.stderr)
            sys.exit(1)

        doc = build_index(project_path, extra_ignore=args.exclude)
        Path(args.output).write_text(json.dumps(doc, indent=2, default=str))

        errors = [f["path"] for f in doc["files"] if f.get("error")]
        print(
            f"Index written to {args.output}: {doc['file_count']} files, "
            f"{doc['class_count']} classes, {doc['function_count']} functions, "
            f"{len(doc['import_graph'])} intra-project import edges"
        )
        if errors:
            print(f"warning: {len(errors)} file(s) had syntax errors and were skipped: {', '.join(errors[:5])}"
                  + (" ..." if len(errors) > 5 else ""), file=sys.stderr)

    elif args.command == "nav":
        project_path = Path(args.path).resolve()
        if not project_path.is_dir():
            print(f"error: not a directory: {project_path}", file=sys.stderr)
            sys.exit(1)

        index_doc = build_index(project_path, extra_ignore=args.exclude)
        call_graph = build_call_graph(index_doc, project_path)
        nav_index = build_navigation_index(index_doc, call_graph)

        matches = find_symbol(nav_index, args.query)
        if not matches:
            print(f"error: no symbol matching '{args.query}' found", file=sys.stderr)
            sys.exit(1)
        if len(matches) > 1:
            print(f"{len(matches)} symbols match '{args.query}':")
            for m in matches[:20]:
                print(f"  {m}")
            if len(matches) > 20:
                print(f"  ... and {len(matches) - 20} more")
            print("\nPass a more specific / fully-qualified name.")
            sys.exit(1)

        rec = nav_index[matches[0]]
        _print_nav_record(rec, project_path)

        if args.output:
            Path(args.output).write_text(json.dumps(rec, indent=2, default=str))
            print(f"\nFull navigation record written to {args.output}")

    elif args.command == "graph":
        project_path = Path(args.path).resolve()
        if not project_path.is_dir():
            print(f"error: not a directory: {project_path}", file=sys.stderr)
            sys.exit(1)

        trace_doc = None
        if args.trace:
            trace_path = Path(args.trace)
            if not trace_path.is_file():
                print(f"error: trace file not found: {trace_path}", file=sys.stderr)
                sys.exit(1)
            trace_doc = json.loads(trace_path.read_text())

        graph_doc = build_graph(project_path, extra_ignore=args.exclude, trace_doc=trace_doc, run_id=args.run_id, include_source=not args.no_source)
        Path(args.output).write_text(json.dumps(graph_doc, indent=2, default=str))

        type_counts, edge_type_counts = {}, {}
        for n in graph_doc["nodes"]:
            type_counts[n["type"]] = type_counts.get(n["type"], 0) + 1
        for e in graph_doc["edges"]:
            edge_type_counts[e["type"]] = edge_type_counts.get(e["type"], 0) + 1

        print(f"Graph written to {args.output}: {graph_doc['node_count']} nodes, {graph_doc['edge_count']} edges")
        print("  nodes: " + ", ".join(f"{k}={v}" for k, v in sorted(type_counts.items())))
        print("  edges: " + ", ".join(f"{k}={v}" for k, v in sorted(edge_type_counts.items())))
        if trace_doc:
            realized = edge_type_counts.get("REALIZES", 0)
            total_calls = sum(1 for e in trace_doc["events"] if e["type"] == "call")
            print(f"  runtime identity: {realized}/{total_calls} call events matched to a static symbol")

    elif args.command == "dataflow":
        trace_path = Path(args.trace)
        if not trace_path.is_file():
            print(f"error: trace file not found: {trace_path}", file=sys.stderr)
            sys.exit(1)
        trace_doc = json.loads(trace_path.read_text())
        flow = build_dataflow(trace_doc)
        print(f"{flow['edge_count']} data-flow edge(s) found:\n")
        for e in flow["edges"]:
            arrow = "==" if e["match"] == "exact" else "~~"
            print(f"  {e['from_function']}.{e['from_label']:<10} {arrow}> {e['to_function']}(arg={e['to_arg']})  [{e['match']}, {e['confidence']}]")
        if args.output:
            Path(args.output).write_text(json.dumps(flow, indent=2, default=str))
            print(f"\nFull edge list written to {args.output}")

    elif args.command == "impact":
        project_path = Path(args.path).resolve()
        if not project_path.is_dir():
            print(f"error: not a directory: {project_path}", file=sys.stderr)
            sys.exit(1)

        index_doc = build_index(project_path, extra_ignore=args.exclude)
        call_graph = build_call_graph(index_doc, project_path)
        nav_index = build_navigation_index(index_doc, call_graph)
        function_qnames = {fn["qualified_name"] for fn in index_doc["functions"]}

        matches = [m for m in find_symbol(nav_index, args.query) if m in function_qnames]
        if not matches:
            print(f"error: no function/method matching '{args.query}' found (impact analysis applies to functions/methods, not classes)", file=sys.stderr)
            sys.exit(1)
        if len(matches) > 1:
            print(f"{len(matches)} symbols match '{args.query}':")
            for m in matches[:20]:
                print(f"  {m}")
            print("\nPass a more specific / fully-qualified name.")
            sys.exit(1)

        trace_doc = None
        if args.trace:
            trace_path = Path(args.trace)
            if not trace_path.is_file():
                print(f"error: trace file not found: {trace_path}", file=sys.stderr)
                sys.exit(1)
            trace_doc = json.loads(trace_path.read_text())

        impact = build_impact(index_doc, call_graph, matches[0], trace_doc=trace_doc, max_depth=args.max_depth)

        if args.show_git:
            if is_git_repo(project_path):
                impact["last_commit"] = find_last_commit_for_symbol(project_path, index_doc, matches[0])
            else:
                print(f"warning: {project_path} is not a git repo, skipping --show-git", file=sys.stderr)
                impact["last_commit"] = None

        _print_impact(impact)

        if args.output:
            Path(args.output).write_text(json.dumps(impact, indent=2, default=str))
            print(f"\nFull impact record written to {args.output}")

    elif args.command == "diff":
        path_a, path_b = Path(args.trace_a), Path(args.trace_b)
        if not path_a.is_file():
            print(f"error: trace file not found: {path_a}", file=sys.stderr)
            sys.exit(1)
        if not path_b.is_file():
            print(f"error: trace file not found: {path_b}", file=sys.stderr)
            sys.exit(1)
        diff = build_execution_diff(json.loads(path_a.read_text()), json.loads(path_b.read_text()))
        if "error" in diff:
            print(f"error: {diff['error']}", file=sys.stderr)
            sys.exit(1)
        _print_diff(diff)
        if args.output:
            Path(args.output).write_text(json.dumps(diff, indent=2, default=str))
            print(f"\nFull diff (including per-call tree) written to {args.output}")

    elif args.command == "why":
        trace_path = Path(args.trace)
        if not trace_path.is_file():
            print(f"error: trace file not found: {trace_path}", file=sys.stderr)
            sys.exit(1)
        trace_doc = json.loads(trace_path.read_text())
        report = build_why_report(trace_doc, exception_type=args.exception_type, call_id=args.call_id)
        if "error" in report:
            suffix = f" of type '{args.exception_type}'" if args.exception_type else ""
            print(f"No exceptions{suffix} found in this trace.")
        else:
            if args.git:
                git_root = Path(args.git).resolve()
                if not is_git_repo(git_root):
                    print(f"warning: {git_root} is not a git repo, skipping --git correlation", file=sys.stderr)
                else:
                    git_index = build_index(git_root)
                    report = correlate_why_with_git(git_root, git_index, report)
            _print_why(report)
        if args.output:
            Path(args.output).write_text(json.dumps(report, indent=2, default=str))
            print(f"\nFull report written to {args.output}")

    elif args.command == "git-log":
        project_path = Path(args.path).resolve()
        if not project_path.is_dir():
            print(f"error: not a directory: {project_path}", file=sys.stderr)
            sys.exit(1)
        if not is_git_repo(project_path):
            print(f"error: {project_path} is not a git repository", file=sys.stderr)
            sys.exit(1)

        path_filter = None
        if args.function:
            index_doc = build_index(project_path, extra_ignore=args.exclude)
            call_graph = build_call_graph(index_doc, project_path)
            nav_index = build_navigation_index(index_doc, call_graph)
            matches = find_symbol(nav_index, args.function)
            if not matches:
                print(f"error: no symbol matching '{args.function}' found", file=sys.stderr)
                sys.exit(1)
            if len(matches) > 1:
                print(f"{len(matches)} symbols match '{args.function}': {', '.join(matches[:10])}", file=sys.stderr)
                sys.exit(1)
            entity = next((f for f in index_doc["functions"] if f["qualified_name"] == matches[0]), None) \
                or next((c for c in index_doc["classes"] if c["qualified_name"] == matches[0]), None)
            path_filter = entity["file"]
            print(f"Commits touching {matches[0]} ({path_filter}):\n")
            candidates = get_commit_log(project_path, max_commits=max(args.limit * 5, 100), path_filter=path_filter)
            commits = [
                c for c in candidates
                if any(entity["line"] <= end and start <= entity["end_line"]
                       for start, end in get_changed_line_ranges(project_path, c["hash"], path_filter))
            ][:args.limit]
        else:
            commits = get_commit_log(project_path, max_commits=args.limit)

        if not commits:
            print("No matching commits found.")
        for c in commits:
            print(f"  {c['short_hash']}  {c['date']}  {c['author']:<20}  {c['message']}")

        if args.output:
            Path(args.output).write_text(json.dumps(commits, indent=2, default=str))
            print(f"\nFull results written to {args.output}")

    elif args.command == "git-impact":
        project_path = Path(args.path).resolve()
        if not project_path.is_dir():
            print(f"error: not a directory: {project_path}", file=sys.stderr)
            sys.exit(1)
        if not is_git_repo(project_path):
            print(f"error: {project_path} is not a git repository", file=sys.stderr)
            sys.exit(1)

        index_doc = build_index(project_path, extra_ignore=args.exclude)
        call_graph = build_call_graph(index_doc, project_path)
        try:
            commit_impact = build_commit_impact(project_path, index_doc, call_graph, args.commit, max_depth=args.max_depth)
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)

        print(f"GIT IMPACT  {args.commit}\n")
        print(f"Changed files ({len(commit_impact['changed_files'])}):")
        for f in commit_impact["changed_files"]:
            print(f"  {f}")
        print(f"\nChanged symbols ({len(commit_impact['changed_symbols'])}):")
        for s in commit_impact["changed_symbols"]:
            print(f"  {s}")
        print(f"\nTransitively affected: {len(commit_impact['transitively_affected_symbols'])} symbol(s) across {len(commit_impact['transitively_affected_files'])} file(s)")
        for f in commit_impact["transitively_affected_files"][:15]:
            print(f"  {f}")

        if args.output:
            Path(args.output).write_text(json.dumps(commit_impact, indent=2, default=str))
            print(f"\nFull results written to {args.output}")

    elif args.command == "ask":
        project_path = Path(args.path).resolve()
        if not project_path.is_dir():
            print(f"error: not a directory: {project_path}", file=sys.stderr)
            sys.exit(1)
        try:
            client = LLMClient(base_url=args.base_url, api_key=args.api_key, model=args.model)
        except LLMConfigError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
        try:
            result = build_answer(project_path, args.question, client, extra_ignore=args.exclude)
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)

        print(f"Q: {result['question']}\n")
        print(result["answer"])
        matched = result["evidence"]["matched_symbols"]
        if matched:
            print(f"\n(evidence drawn from {len(matched)} symbol(s): {', '.join(matched)})")

        if args.output:
            Path(args.output).write_text(json.dumps(result, indent=2, default=str))
            print(f"\nFull answer + evidence written to {args.output}")

    elif args.command == "compress":
        trace_path = Path(args.trace)
        if not trace_path.is_file():
            print(f"error: trace file not found: {trace_path}", file=sys.stderr)
            sys.exit(1)
        trace_doc = json.loads(trace_path.read_text())
        compressed = compress_trace(trace_doc, loop_threshold=args.threshold)
        c = compressed["compression"]
        print(f"{c['original_event_count']} -> {c['compressed_event_count']} events "
              f"({c['runs_collapsed']} loop run(s) collapsed, threshold={c['loop_threshold']})")
        out_path = Path(args.output) if args.output else trace_path
        out_path.write_text(json.dumps(compressed, indent=2, default=str))
        print(f"Written to {out_path}")

    elif args.command == "spec":
        trace_data = None
        if args.trace:
            tp = Path(args.trace).resolve()
            if tp.is_file():
                trace_data = json.loads(tp.read_text(encoding="utf-8"))
        spec = extract_function_spec(args.path, args.symbol, trace_data=trace_data)
        print(f"FUNCTION CONTRACT: {spec['symbol']}")
        if spec.get("file"):
            print(f"  Defined in : {spec['file']}")
        if spec.get("docstring"):
            print(f"  Docstring  : {spec['docstring']}")
        print(f"  Parameters :")
        for p in spec.get("parameters", []):
            ann = f": {p['annotation']}" if p.get("annotation") else ""
            default = f" = {p['default']}" if p.get("default") is not None else ""
            print(f"    - {p['name']}{ann}{default}")
        if spec.get("return_annotation"):
            print(f"  Return type: {spec['return_annotation']}")
        rt = spec.get("observed_runtime_types", {})
        if rt.get("arguments"):
            print(f"\n  Observed Runtime Types:")
            for arg_name, arg_info in rt["arguments"].items():
                print(f"    - {arg_name}: {', '.join(arg_info['types'])}")
                if arg_info.get("observed_dict_keys"):
                    print(f"      keys: {', '.join(arg_info['observed_dict_keys'])}")
        print(f"\n  Usage Recipe:\n    {spec['usage_recipe']}")
        if args.output:
            Path(args.output).write_text(json.dumps(spec, indent=2, default=str))
            print(f"\nFull spec written to {args.output}")

    elif args.command == "mock":
        tp = Path(args.trace).resolve()
        if not tp.is_file():
            print(f"error: trace file not found: {tp}", file=sys.stderr)
            sys.exit(1)
        trace_data = json.loads(tp.read_text(encoding="utf-8"))
        test_code = generate_mock_test(trace_data, target_function=args.target)
        out_path = Path(args.output) if args.output else tp.parent / f"test_{args.target or 'mock'}.py"
        out_path.write_text(test_code, encoding="utf-8")
        print(f"Standalone mock test generated -> {out_path}")

    elif args.command == "perf":
        tp = Path(args.trace).resolve()
        if not tp.is_file():
            print(f"error: trace file not found: {tp}", file=sys.stderr)
            sys.exit(1)
        trace_data = json.loads(tp.read_text(encoding="utf-8"))
        report = analyze_performance(trace_data, n_plus_one_threshold=args.threshold)
        print(f"PERFORMANCE ANALYSIS  [{report['status']}]")
        print(f"  N+1 Patterns detected : {report['warning_count']}")
        for w in report.get("warnings", []):
            print(f"\n  [{w['severity']}] {w['parent_function']} -> {w['child_function']}")
            print(f"    Count: {w['call_count']} times | Total: {w['total_duration_ms']}ms (avg: {w['avg_duration_ms']}ms)")
            print(f"    Advice: {w['advice']}")
        print("\n  Top Latency Hotspots:")
        for hs in report.get("top_hotspots", []):
            print(f"    - {hs['function']}: {hs['total_duration_ms']}ms ({hs['call_count']} calls)")
        if args.output:
            Path(args.output).write_text(json.dumps(report, indent=2, default=str))
            print(f"\nFull report written to {args.output}")

    elif args.command == "verify-patch":
        pa = Path(args.trace_before).resolve()
        pb = Path(args.trace_after).resolve()
        if not pa.is_file() or not pb.is_file():
            print(f"error: trace file(s) not found: {pa}, {pb}", file=sys.stderr)
            sys.exit(1)
        doc_a = json.loads(pa.read_text(encoding="utf-8"))
        doc_b = json.loads(pb.read_text(encoding="utf-8"))
        res = verify_patch(doc_a, doc_b)
        print(f"AUTONOMOUS PATCH VERIFICATION")
        print(f"  Verdict : {res['verdict']}")
        print(f"  Summary : {res['summary']}")
        if res.get("fixed_exceptions"):
            print(f"  Fixed   : {res['fixed_exceptions']}")
        if res.get("new_exceptions"):
            print(f"  New err : {res['new_exceptions']}")
        if res.get("unblocked_functions"):
            print(f"  Restored: {res['unblocked_functions']}")
        if args.output:
            Path(args.output).write_text(json.dumps(res, indent=2, default=str))
            print(f"\nFull verification record written to {args.output}")



def _print_diff(diff):
    s = diff["summary"]
    print(f"EXECUTION COMPARISON   A: {s['call_count_a']} calls   B: {s['call_count_b']} calls")
    print(f"Execution path changed: {'YES' if s['path_changed'] else 'no'}\n")

    if s["added_functions"]:
        print(f"--- functions only in B ({len(s['added_functions'])}) ---")
        for f in s["added_functions"][:15]:
            print(f"  + {f}")
    if s["removed_functions"]:
        print(f"\n--- functions only in A ({len(s['removed_functions'])}) ---")
        for f in s["removed_functions"][:15]:
            print(f"  - {f}")
    if s["call_count_changed"]:
        print(f"\n--- call count changed ({len(s['call_count_changed'])}) ---")
        for c in s["call_count_changed"][:15]:
            print(f"  {c['function']}: {c['count_a']} -> {c['count_b']}")

    ed = diff["exception_diff"]
    if ed["only_in_a"] or ed["only_in_b"]:
        print("\n--- exceptions ---")
        for fn, t, m in ed["only_in_b"]:
            print(f"  NEW in B: {fn} raised {t}: {m}")
        for fn, t, m in ed["only_in_a"]:
            print(f"  RESOLVED (was in A only): {fn} raised {t}: {m}")

    if diff["latency_diff"]:
        top = diff["latency_diff"][:10]
        print(f"\n--- latency changes (top {len(top)}) ---")
        for l in top:
            sign = "+" if l["delta_ms"] >= 0 else ""
            print(f"  {l['function']}: {l['avg_a_ms']}ms -> {l['avg_b_ms']}ms ({sign}{l['delta_ms']}ms, n={l['count_a']}/{l['count_b']})")

    changed_cats = {k: v for k, v in diff["category_diff"].items() if v["a"] != v["b"]}
    if changed_cats:
        print("\n--- category mix changed ---")
        for cat, counts in changed_cats.items():
            print(f"  {cat}: {counts['a']} -> {counts['b']}")


def _print_why(report):
    print(f"{report['match_count']} distinct exception(s) found\n")
    for r in report["reports"]:
        print(f"WHY: {r['exception_type']}: {r['exception_message']}")
        print(f"  origin: {r['origin_function']}  (call_id={r['origin_call_id']}, depth={r['depth']})\n")
        print("  causal chain (entry point -> ... -> origin):")
        for i, frame in enumerate(r["chain"]):
            marker = "  <-- exception originated here" if frame["is_origin"] else ""
            print(f"    {'  ' * i}{frame['function']}({', '.join(frame['args'].keys())}){marker}")
            if "last_commit" in frame:
                c = frame["last_commit"]
                if c:
                    print(f"    {'  ' * i}    last changed: {c['short_hash']}  {c['date']}  {c['message']}")
                elif frame.get("symbol"):
                    print(f"    {'  ' * i}    last changed: no matching commit found")
        print()


def _print_impact(impact):
    s = impact["summary"]
    print(f"IMPACT ANALYSIS  {impact['symbol']}")
    print(f"Impact confidence: {s['impact_confidence'].upper()}\n")
    print(f"Potential impact: {s['functions']} function(s), {s['files']} file(s), {s['tests']} test(s), "
          f"{s['api_routes']} API route(s), {s['database_operations']} DB operation(s)\n")

    print(f"--- direct callers, PROVEN ({len(impact['direct_callers'])}) ---")
    for c in impact["direct_callers"][:10]:
        print(f"  {c}")
    if not impact["direct_callers"]:
        print("  none proven")

    if impact["possible_callers"]:
        pc = impact["possible_callers"]
        print(f"\n--- possible callers, UNPROVEN ({len(pc)}) ---")
        print("  Call sites elsewhere in the project that call a same-named method on some")
        print("  object whose type wasn't resolved - could be calling this, could be calling")
        print("  something else entirely named the same. NOT evidence of a real caller, but")
        print("  the absence of PROVEN callers above is *also* not evidence there isn't one.")
        for c in pc[:10]:
            print(f"    {c['caller']}  ({c['file']}:{c['line']})  {c['expr']}")
        if len(pc) > 10:
            print(f"    ... and {len(pc) - 10} more")
    elif not impact["direct_callers"]:
        print("\n  (no possible callers found elsewhere either - likely a genuine entry point,")
        print("   though dynamic dispatch/getattr calls are never visible to static analysis)")

    print(f"\n--- direct callees ({len(impact['direct_callees'])}) ---")
    for c in impact["direct_callees"][:10]:
        print(f"  {c}")

    print(f"\n--- transitively affected files, from PROVEN callers only ({len(impact['affected_files'])}) ---")
    for f in impact["affected_files"][:15]:
        print(f"  {f}")

    print(f"\n--- tests to re-run, from PROVEN callers only ({len(impact['affected_tests'])}) ---")
    if impact["affected_tests"]:
        for t in impact["affected_tests"]:
            print(f"  {t}")
    else:
        print("  none proven upstream" + (" - but see possible callers above before assuming none exist" if impact["possible_callers"] else ""))

    if impact["api_endpoints"]:
        print(f"\n--- API routes reachable upstream ({len(impact['api_endpoints'])}) ---")
        for a in impact["api_endpoints"]:
            print(f"  {a}")

    if impact["database_operations"]:
        print(f"\n--- database operations downstream ({len(impact['database_operations'])}) ---")
        for op in impact["database_operations"][:10]:
            print(f"  {op['from']}: {op['expr']}  ({op['file']}:{op['line']})")

    if impact["network_operations"]:
        print(f"\n--- network operations downstream ({len(impact['network_operations'])}) ---")
        for op in impact["network_operations"][:10]:
            print(f"  {op['from']}: {op['expr']}  ({op['file']}:{op['line']})")

    if impact["observed_runtime_paths"]:
        rp = impact["observed_runtime_paths"]
        print(f"\n--- observed runtime paths ({rp['match_count']} call(s) matched in trace, {rp['match_type']}) ---")
        for p in rp["sample_paths"]:
            print("  " + " -> ".join(p["path"]))

    if impact.get("last_commit"):
        c = impact["last_commit"]
        print(f"\n--- last commit touching this symbol ---")
        print(f"  {c['short_hash']}  {c['author']}  {c['date']}")
        print(f"  {c['message']}")
    elif "last_commit" in impact:
        print("\n--- last commit touching this symbol ---\n  none found in git history")


def _print_nav_record(rec, project_path):
    print(f"{rec['kind'].upper()}  {rec['qualified_name']}")
    print(f"  {rec['file']}:{rec['line']}-{rec.get('end_line', rec['line'])}")
    if rec.get("docstring"):
        print(f'\n  """{rec["docstring"]}"""')

    if rec["kind"] == "class":
        print(f"\n  bases: {', '.join(rec['bases']) or '(none)'}")
        print(f"  methods: {', '.join(rec['methods']) or '(none)'}")
        return

    print(f"\n  args: {', '.join(rec['args']) or '(none)'}")
    if rec.get("class_qualified_name"):
        print(f"  method of: {rec['class_qualified_name']}")

    snippet = get_source_snippet(project_path, rec["file"], rec["line"], rec["end_line"])
    if snippet:
        print("\n  --- source ---")
        for line in snippet.splitlines():
            print("  " + line)

    print(f"\n  --- callers ({len(rec['callers'])}) ---")
    if not rec["callers"]:
        print("    (none found in this project - likely an entry point, called dynamically, or called from outside the indexed tree)")
    for c in rec["callers"][:10]:
        print(f"    {c['qualified_name']}  ({c['file']}:{c['line']})")
    if len(rec["callers"]) > 10:
        print(f"    ... and {len(rec['callers']) - 10} more")

    resolved = [c for c in rec["callees"] if c["resolved"]]
    external = [c for c in rec["callees"] if not c["resolved"] and c["category"]]
    print(f"\n  --- callees ({len(resolved)} resolved, {len(external)} external) ---")
    for c in resolved[:10]:
        print(f"    {c['qualified_name']}  ({c['file']}:{c['line']})")
    for c in external[:10]:
        print(f"    {c['expr']}  [{c['category']}]  ({c['file']}:{c['line']})")

    if rec["related_tests"]:
        print("\n  --- related tests ---")
        for t in rec["related_tests"]:
            print(f"    {t}")


if __name__ == "__main__":
    main()
