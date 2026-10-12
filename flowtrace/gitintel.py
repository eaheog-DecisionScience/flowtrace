"""Git integration (Priority 8): correlates commit history with the
static index, so "what did this commit change" resolves to actual
functions/classes (via diff hunk line ranges overlapping the index's
line ranges), not just filenames - and from there into everything else
already built: transitive impact (this module reuses impact.py's
closure), and "which commit last touched the function where this
exception originated" for `flowtrace why --git`.

Pure `git` subprocess calls, no GitPython dependency - keeps this
consistent with the rest of the project (stdlib + the project's own
modules only).
"""

import re
import subprocess
from pathlib import Path

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FIELD_SEP = "\x1f"
_COMMIT_FORMAT = f"%H{_FIELD_SEP}%h{_FIELD_SEP}%an{_FIELD_SEP}%ae{_FIELD_SEP}%ad{_FIELD_SEP}%s"


def _run_git(repo_dir, args):
    try:
        result = subprocess.run(["git", "-C", str(repo_dir)] + args, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError("git is not installed / not on PATH")
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def is_git_repo(repo_dir):
    try:
        out = _run_git(repo_dir, ["rev-parse", "--is-inside-work-tree"])
        return out.strip() == "true"
    except RuntimeError:
        return False


def get_commit_log(repo_dir, max_commits=50, path_filter=None, since=None):
    args = ["log", f"--format={_COMMIT_FORMAT}", "--date=iso-strict", f"-n{max_commits}"]
    if since:
        args.append(f"--since={since}")
    if path_filter:
        args += ["--", path_filter]
    out = _run_git(repo_dir, args)

    commits = []
    for line in out.splitlines():
        if not line.strip():
            continue
        full_hash, short_hash, author, email, date, subject = line.split(_FIELD_SEP)
        commits.append({"hash": full_hash, "short_hash": short_hash, "author": author,
                         "email": email, "date": date, "message": subject})
    return commits


def get_changed_files(repo_dir, commit_hash):
    out = _run_git(repo_dir, ["diff-tree", "--no-commit-id", "--name-status", "-r", commit_hash])
    changes = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        changes.append({"status": parts[0], "path": parts[-1]})
    return changes


def get_changed_line_ranges(repo_dir, commit_hash, file_path):
    """Line ranges in the NEW version of `file_path` touched by this
    commit, read off the diff hunk headers (@@ -a,b +c,d @@). A
    pure-deletion hunk (d==0) still gets a zero-width range at the
    insertion point, so it can be attributed to whichever function
    contains that point."""
    out = _run_git(repo_dir, ["show", "--unified=0", commit_hash, "--", file_path])
    ranges = []
    for line in out.splitlines():
        m = _HUNK_RE.match(line)
        if not m:
            continue
        new_start = int(m.group(3))
        new_count = int(m.group(4)) if m.group(4) is not None else 1
        ranges.append((new_start, new_start) if new_count == 0 else (new_start, new_start + new_count - 1))
    return ranges


def _overlaps(entry_line, entry_end, ranges):
    return any(entry_line <= end and start <= entry_end for start, end in ranges)


def symbol_at_line(index_doc, file_path, line):
    for fn in index_doc["functions"]:
        if fn["file"] == file_path and fn["line"] <= line <= fn["end_line"]:
            return fn["qualified_name"]
    for cls in index_doc["classes"]:
        if cls["file"] == file_path and cls["line"] <= line <= cls["end_line"]:
            return cls["qualified_name"]
    return None


def symbols_overlapping(index_doc, file_path, line_ranges):
    """All function/class qualified_names in `file_path` whose own line
    range overlaps any of `line_ranges`."""
    matches = set()
    for fn in index_doc["functions"]:
        if fn["file"] == file_path and _overlaps(fn["line"], fn["end_line"], line_ranges):
            matches.add(fn["qualified_name"])
    for cls in index_doc["classes"]:
        if cls["file"] == file_path and _overlaps(cls["line"], cls["end_line"], line_ranges):
            matches.add(cls["qualified_name"])
    return matches


def build_commit_impact(repo_dir, index_doc, call_graph, commit_hash, max_depth=None):
    """What did this commit change, and what does that transitively
    affect (reusing impact.py's caller closure)?"""
    from .impact import _build_adjacency, _transitive_closure

    changed_files_raw = get_changed_files(repo_dir, commit_hash)
    changed_files = [cf["path"] for cf in changed_files_raw]

    changed_symbols = set()
    for cf in changed_files_raw:
        if not cf["path"].endswith(".py") or cf["status"] == "D":
            continue
        ranges = get_changed_line_ranges(repo_dir, commit_hash, cf["path"])
        changed_symbols |= symbols_overlapping(index_doc, cf["path"], ranges)

    functions_by_qname = {fn["qualified_name"]: fn for fn in index_doc["functions"]}
    callers_of, _ = _build_adjacency(call_graph)

    affected = set(changed_symbols)
    for sym in changed_symbols:
        affected |= _transitive_closure(sym, callers_of, max_depth).keys()

    affected_files = sorted({functions_by_qname[q]["file"] for q in affected if q in functions_by_qname})

    return {
        "commit": commit_hash,
        "changed_files": changed_files,
        "changed_symbols": sorted(changed_symbols),
        "transitively_affected_symbols": sorted(affected),
        "transitively_affected_files": affected_files,
    }


def find_last_commit_for_symbol(repo_dir, index_doc, qualified_name, max_commits=200):
    """Walks commit history for the symbol's file, returning the most
    recent commit whose diff hunks actually overlap the symbol's line
    range (not just "touched the file" - a real per-symbol blame)."""
    fn = next((f for f in index_doc["functions"] if f["qualified_name"] == qualified_name), None)
    entity = fn or next((c for c in index_doc["classes"] if c["qualified_name"] == qualified_name), None)
    if entity is None:
        return None

    for commit in get_commit_log(repo_dir, max_commits=max_commits, path_filter=entity["file"]):
        ranges = get_changed_line_ranges(repo_dir, commit["hash"], entity["file"])
        if _overlaps(entity["line"], entity["end_line"], ranges):
            return {**commit, "file": entity["file"]}
    return None


def correlate_why_with_git(repo_dir, index_doc, why_report):
    """Attaches `last_commit` to every frame in a `causality.build_why_report`
    result, resolving each frame's file:line back to a symbol first."""
    repo_dir = Path(repo_dir).resolve()
    for report in why_report.get("reports", []):
        for frame in report["chain"]:
            rel_file = None
            if frame.get("file"):
                try:
                    rel_file = str(Path(frame["file"]).resolve().relative_to(repo_dir))
                except ValueError:
                    rel_file = None  # frame's file isn't under this repo (e.g. stdlib) - can't correlate
            qname = symbol_at_line(index_doc, rel_file, frame["line"]) if rel_file and frame.get("line") else None
            frame["symbol"] = qname
            frame["last_commit"] = find_last_commit_for_symbol(repo_dir, index_doc, qname) if qname else None
    return why_report
