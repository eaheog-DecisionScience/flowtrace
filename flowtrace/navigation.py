"""Code Navigation (Phase 4): given a symbol, answers "where does this
live, who calls it, and what does it call" by joining the static project
index with the static call graph - no execution required.
"""

from pathlib import Path

TEST_DIR_NAMES = {"test", "tests", "testing"}


def _is_test_function(fn):
    if fn["name"].startswith("test_") or fn["name"].endswith("_test"):
        return True
    return any(part.lower() in TEST_DIR_NAMES for part in Path(fn["file"]).parts[:-1])


def build_navigation_index(index_doc, call_graph):
    """qualified_name -> navigation record, for every function/method/class
    in the project."""
    functions_by_qname = {fn["qualified_name"]: fn for fn in index_doc["functions"]}
    classes_by_qname = {c["qualified_name"]: c for c in index_doc["classes"]}

    callers_of, callees_of = {}, {}
    for edge in call_graph:
        callees_of.setdefault(edge["caller"], []).append(edge)
        if edge["callee"]:
            callers_of.setdefault(edge["callee"], []).append(edge)

    records = {}

    for qname, fn in functions_by_qname.items():
        callers = callers_of.get(qname, [])
        callees = callees_of.get(qname, [])
        related_tests = sorted({
            e["caller"] for e in callers
            if e["caller"] in functions_by_qname and _is_test_function(functions_by_qname[e["caller"]])
        })
        records[qname] = {
            "qualified_name": qname, "kind": "method" if fn["is_method"] else "function",
            "name": fn["name"], "file": fn["file"], "line": fn["line"], "end_line": fn["end_line"],
            "args": fn["args"], "decorators": fn["decorators"], "docstring": fn["docstring"],
            "class_qualified_name": fn["class_qualified_name"],
            "callers": [{"qualified_name": e["caller"], "file": e["file"], "line": e["line"]} for e in callers],
            "callees": [
                {"qualified_name": e["callee"], "expr": e["callee_expr"], "file": e["file"],
                 "line": e["line"], "resolved": e["resolved"], "category": e["category"]}
                for e in callees
            ],
            "related_tests": related_tests,
        }

    for qname, cls in classes_by_qname.items():
        records[qname] = {
            "qualified_name": qname, "kind": "class", "name": cls["name"],
            "file": cls["file"], "line": cls["line"], "end_line": cls["end_line"],
            "bases": cls["bases"], "methods": cls["methods"], "docstring": cls["docstring"],
            "decorators": cls["decorators"],
        }

    return records


def get_source_snippet(root_dir, file_path, line, end_line, context=0):
    path = Path(root_dir) / file_path
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    start = max(1, line - context)
    end = min(len(lines), end_line + context)
    return "\n".join(lines[start - 1:end])


def find_symbol(nav_index, query):
    """Resolve a user query - an exact qualified name, a dotted suffix
    like 'ClassName.method', or a bare name - to matching qualified names."""
    if query in nav_index:
        return [query]
    suffix_matches = sorted(q for q in nav_index if q.endswith("." + query))
    if suffix_matches:
        return suffix_matches
    return sorted(q for q in nav_index if query in q)
