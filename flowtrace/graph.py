"""Milestone 1 - Unified Project Graph.

Merges the static index, the static call graph, and (optionally) a
runtime trace into a single graph of typed nodes and edges. This is the
identity layer: the index, call graph, and tracer all separately know
about the same functions and classes but don't share a stable id for
them. This module gives them one.

Node id scheme (stable across static/runtime so they can share one id
space):
    dir:<relative/path>              a folder
    file:<relative/path.py>          a file
    py:<qualified_name>              a function, method, or class
    ext:<module>                     an unresolved external import/call target
    run:<run_id>:<call_id>           one runtime call instance

Edge types:
    CONTAINS       dir->dir, dir->file, file->class/function, class->method
    IMPORTS        file->file, file->ext
    INHERITS       class->class (best-effort - same resolution style as calls)
    CALLS_STATIC   function->function/class/ext, from callgraph.py
    CALLS_RUNTIME  parent call_id -> child call_id, within one trace run
    REALIZES       a runtime call instance -> the static `py:` node it's an
                    execution of, when resolvable. This is the specific edge
                    that turns "call_id 18" into "a known entity in the
                    project" - the join the roadmap calls the biggest gap.
    FLOWS_TO       runtime call instance -> runtime call instance: a value
                    produced by one call (its return, or a mutated arg)
                    structurally matches an argument a later sibling call
                    consumed. From dataflow.py - heuristic, not exact.
"""

import ast
from pathlib import Path

from .indexer import build_index
from .callgraph import build_call_graph, _build_symbol_tables, _local_bindings_for_file
from .dataflow import build_dataflow
from .navigation import get_source_snippet


def _node(node_id, type_, name, **extra):
    n = {"id": node_id, "type": type_, "name": name}
    n.update(extra)
    return n


def _edge(source, target, type_, **extra):
    e = {"source": source, "target": target, "type": type_}
    e.update(extra)
    return e


def _decompose_expr(expr_src):
    try:
        node = ast.parse(expr_src, mode="eval").body
    except SyntaxError:
        return None, []
    chain = []
    cur = node
    while isinstance(cur, ast.Attribute):
        chain.insert(0, cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        return cur.id, chain
    return None, chain


def _resolve_base(base_expr, local_bindings, module_classes, current_module):
    """Best-effort resolution of a class base expression to a project-local
    class's qualified name - reuses the same local_bindings a call would."""
    base_name, chain = _decompose_expr(base_expr)
    if base_name is None:
        return None
    if not chain:
        if base_name in local_bindings and local_bindings[base_name][0] == "class":
            return local_bindings[base_name][1]
        if base_name in module_classes.get(current_module, {}):
            return module_classes[current_module][base_name]["qualified_name"]
    elif len(chain) == 1 and base_name in local_bindings and local_bindings[base_name][0] == "module":
        target = local_bindings[base_name][1]
        if chain[0] in module_classes.get(target, {}):
            return module_classes[target][chain[0]]["qualified_name"]
    return None


def _add_tree_nodes_and_edges(tree, nodes, edges, parent_id=None, path_prefix=""):
    if tree["type"] == "dir":
        node_path = path_prefix or "."
        node_id = f"dir:{node_path}"
        nodes.setdefault(node_id, _node(node_id, "dir", tree["name"]))
        if parent_id:
            edges.append(_edge(parent_id, node_id, "CONTAINS"))
        for child in tree.get("children", []):
            child_path = child["name"] if not path_prefix else f"{path_prefix}/{child['name']}"
            _add_tree_nodes_and_edges(child, nodes, edges, node_id, child_path)
    else:
        node_id = f"file:{path_prefix}"
        nodes.setdefault(node_id, _node(node_id, "file", tree["name"], file=path_prefix))
        if parent_id:
            edges.append(_edge(parent_id, node_id, "CONTAINS"))


def _add_runtime_layer(trace_doc, run_id, nodes, edges, root, local_lookup):
    call_node_ids = {}  # call_id -> node id, so the return/exception pass can find it back

    for ev in trace_doc.get("events", []):
        if ev["type"] != "call":
            continue
        call_node_id = f"run:{run_id}:{ev['call_id']}"
        call_node_ids[ev["call_id"]] = call_node_id
        module = ev.get("module", "")
        # Primary guess: tracer.py records code.co_qualname (e.g.
        # "LoginHandler.login" on Python 3.11+, else just "login") - this
        # mirrors how qualified_name is built in indexer.py closely enough
        # to join directly, MOST of the time.
        guess = f"{module}.{ev['function']}" if module else ev["function"]
        static_id = f"py:{guess}"
        resolved_static = static_id if static_id in nodes else None

        # Fallback: the entry script always runs as module "__main__" at
        # runtime, but the static indexer names it by its file path (e.g.
        # "examples.demo") - so for the entry point (and anything else
        # where the module-name guess misses) fall back to a (file, local
        # qualname) lookup instead, which is naming-scheme independent.
        if resolved_static is None and ev.get("file"):
            try:
                rel_file = str(Path(ev["file"]).resolve().relative_to(root))
            except ValueError:
                rel_file = None
            if rel_file:
                alt_qname = local_lookup.get((rel_file, ev["function"]))
                if alt_qname and f"py:{alt_qname}" in nodes:
                    resolved_static = f"py:{alt_qname}"

        nodes[call_node_id] = _node(
            call_node_id, "runtime_call", ev["function"], file=ev.get("file"), line=ev.get("line"),
            depth=ev.get("depth"), category=ev.get("category"), timestamp=ev.get("timestamp"),
            run_id=run_id, call_id=ev["call_id"], args=ev.get("args", {}),
            return_value=None, arg_diff={}, exceptions=[],
        )
        if ev.get("parent_id") is not None:
            edges.append(_edge(f"run:{run_id}:{ev['parent_id']}", call_node_id, "CALLS_RUNTIME"))
        if resolved_static:
            edges.append(_edge(call_node_id, resolved_static, "REALIZES"))
            nodes[resolved_static]["execution_count"] = nodes[resolved_static].get("execution_count", 0) + 1

    # second pass: attach return values / diffs / exceptions to the nodes
    # the call pass already created, so a runtime node carries its full
    # picture in one place instead of needing the raw trace alongside it.
    for ev in trace_doc.get("events", []):
        node_id = call_node_ids.get(ev.get("call_id"))
        if node_id is None:
            continue
        if ev["type"] == "return":
            nodes[node_id]["return_value"] = ev.get("return_value")
            nodes[node_id]["arg_diff"] = ev.get("arg_diff", {})
            nodes[node_id]["return_timestamp"] = ev.get("timestamp")
        elif ev["type"] == "exception":
            nodes[node_id]["exceptions"].append({"type": ev.get("exception_type"), "message": ev.get("exception_message")})

    # data flow: edges between runtime call instances (not static symbols -
    # a data-flow edge is inherently about specific values at specific
    # moments in this one run, so it belongs on run: nodes)
    flow = build_dataflow(trace_doc)
    for e in flow["edges"]:
        src = call_node_ids.get(e["from_call"])
        tgt = call_node_ids.get(e["to_call"])
        if src and tgt:
            edges.append(_edge(src, tgt, "FLOWS_TO", from_label=e["from_label"], to_arg=e["to_arg"],
                                match=e["match"], confidence=e["confidence"]))


def build_graph(root_dir, extra_ignore=(), trace_doc=None, run_id="run", include_source=True):
    root = Path(root_dir).resolve()
    index_doc = build_index(root, extra_ignore=extra_ignore)
    call_graph = build_call_graph(index_doc, root)
    module_functions, module_classes, module_name_by_file = _build_symbol_tables(index_doc)

    nodes = {}
    edges = []

    _add_tree_nodes_and_edges(index_doc["folder_tree"], nodes, edges)

    for cls in index_doc["classes"]:
        node_id = f"py:{cls['qualified_name']}"
        source = get_source_snippet(root, cls["file"], cls["line"], cls["end_line"]) if include_source else None
        nodes[node_id] = _node(node_id, "class", cls["name"], file=cls["file"], line=cls["line"],
                                end_line=cls["end_line"], bases=cls["bases"], docstring=cls["docstring"],
                                source=source)
        file_node_id = f"file:{cls['file']}"
        if file_node_id in nodes:
            edges.append(_edge(file_node_id, node_id, "CONTAINS"))

    for fn in index_doc["functions"]:
        node_id = f"py:{fn['qualified_name']}"
        source = get_source_snippet(root, fn["file"], fn["line"], fn["end_line"]) if include_source else None
        nodes[node_id] = _node(node_id, "method" if fn["is_method"] else "function", fn["name"],
                                file=fn["file"], line=fn["line"], end_line=fn["end_line"],
                                args=fn["args"], docstring=fn["docstring"], is_method=fn["is_method"],
                                source=source)
        parent_id = f"py:{fn['class_qualified_name']}" if fn["is_method"] and fn["class_qualified_name"] else f"file:{fn['file']}"
        if parent_id in nodes:
            edges.append(_edge(parent_id, node_id, "CONTAINS"))

    for imp_edge in index_doc["import_graph"]:
        src, tgt = f"file:{imp_edge['from']}", f"file:{imp_edge['to']}"
        if src in nodes and tgt in nodes:
            edges.append(_edge(src, tgt, "IMPORTS", via_module=imp_edge["via_module"]))
    for unresolved in index_doc["unresolved_imports"]:
        ext_id = f"ext:{unresolved['module']}"
        nodes.setdefault(ext_id, _node(ext_id, "external", unresolved["module"], category=unresolved["category"]))
        src = f"file:{unresolved['file']}"
        if src in nodes:
            edges.append(_edge(src, ext_id, "IMPORTS"))

    for file_record in index_doc["files"]:
        if file_record.get("error") or not file_record["classes"]:
            continue
        local_bindings = _local_bindings_for_file(file_record, module_functions, module_classes, module_name_by_file)
        for fname, qname in module_functions.get(file_record["module_name"], {}).items():
            local_bindings.setdefault(fname, ("func", qname))
        for cname, info in module_classes.get(file_record["module_name"], {}).items():
            local_bindings.setdefault(cname, ("class", info["qualified_name"]))
        for cls in file_record["classes"]:
            child_id = f"py:{cls['qualified_name']}"
            for base_expr in cls["bases"]:
                base_qname = _resolve_base(base_expr, local_bindings, module_classes, file_record["module_name"])
                parent_id = f"py:{base_qname}" if base_qname else None
                if parent_id and parent_id in nodes:
                    edges.append(_edge(child_id, parent_id, "INHERITS"))

    for call_edge in call_graph:
        src_id = f"py:{call_edge['caller']}"
        if src_id not in nodes:
            continue
        if call_edge["callee"]:
            tgt_id = f"py:{call_edge['callee']}"
            if tgt_id in nodes:
                edges.append(_edge(src_id, tgt_id, "CALLS_STATIC", line=call_edge["line"], resolved=True))
        elif call_edge["category"]:
            ext_name = call_edge["callee_expr"].split(".")[0].split("(")[0]
            ext_id = f"ext:{ext_name}"
            nodes.setdefault(ext_id, _node(ext_id, "external", ext_name, category=call_edge["category"]))
            edges.append(_edge(src_id, ext_id, "CALLS_STATIC", line=call_edge["line"], resolved=False, expr=call_edge["callee_expr"]))

    if trace_doc:
        local_lookup = {}
        for fn in index_doc["functions"]:
            module_name = module_name_by_file.get(fn["file"], "")
            prefix = module_name + "."
            suffix = fn["qualified_name"][len(prefix):] if fn["qualified_name"].startswith(prefix) else fn["name"]
            local_lookup[(fn["file"], suffix)] = fn["qualified_name"]
        _add_runtime_layer(trace_doc, run_id, nodes, edges, root, local_lookup)

    return {
        "schema_version": "0.1.0",
        "root_dir": str(root),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": list(nodes.values()),
        "edges": edges,
    }
