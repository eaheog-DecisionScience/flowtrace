"""Static Analyzer (Phase 2 / "Layer 1-3" of the project-understanding
roadmap): walks a project directory and, without executing any code,
extracts its folder structure, import graph, class hierarchy, and function
inventory via Python's `ast` module.

This is deliberately independent of the runtime Tracer Core - it produces
a persistent index that a trace's `call_id`/`module`/`file`/`line` fields
can later be joined against (e.g. "show me the docstring and callers for
whatever function call_id 42 in this trace actually is").
"""

import ast
import json
import os
import time
from pathlib import Path

from .classifier import classify_module

DEFAULT_IGNORE_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "env", ".env", "node_modules",
    "site-packages", "dist-packages", "dist", "build", ".tox", ".eggs",
    ".pytest_cache", ".mypy_cache", ".idea", ".vscode", ".ruff_cache",
    ".flowtrace",
}


def _should_ignore_dir(name):
    return name in DEFAULT_IGNORE_DIRS or name.endswith(".egg-info")


def discover_python_files(root_dir, extra_ignore=()):
    """Walk root_dir, skipping venvs/vendor/VCS dirs, returning sorted .py paths."""
    root = Path(root_dir).resolve()
    ignore = set(extra_ignore)
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _should_ignore_dir(d) and d not in ignore]
        for fname in filenames:
            if fname.endswith(".py"):
                found.append(Path(dirpath) / fname)
    return sorted(found)


def _module_name_for(path, root):
    parts = list(path.relative_to(root).parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]
    return ".".join(parts) if parts else path.stem


def _resolve_import_targets(imp, root, module_parts, is_package):
    """Best-effort resolution of an import statement to project-local file(s).

    `is_package` distinguishes a package's __init__.py (whose module_name
    already *is* the package - level=1 means "this same package") from a
    regular module file (whose module_name includes the file itself, so
    level=1 means "the package containing this file", i.e. one level up).
    """
    level = imp["level"]
    mod = imp["module"]

    if level > 0:
        base_parts = module_parts if is_package else module_parts[:-1]
        if level > 1:
            base_parts = base_parts[: max(0, len(base_parts) - (level - 1))]
    else:
        base_parts = []

    def try_path(parts):
        if not parts:
            return None
        for candidate_root in (root, root / "src"):
            candidate = candidate_root.joinpath(*parts)
            py = candidate.with_suffix(".py")
            if py.is_file():
                return py
            init = candidate / "__init__.py"
            if init.is_file():
                return init
        return None

    targets = []
    if mod:
        base_target_parts = (base_parts + mod.split(".")) if level > 0 else mod.split(".")
        p = try_path(base_target_parts)
        if p:
            targets.append(p)
        else:
            # `from pkg import submodule` - submodule may be a file inside pkg
            for n in imp["names"]:
                p2 = try_path(base_target_parts + [n])
                if p2:
                    targets.append(p2)
    else:
        # bare `from . import name[, name2...]`
        for n in imp["names"]:
            p = try_path(base_parts + [n])
            if p:
                targets.append(p)
    return targets


class _FileVisitor(ast.NodeVisitor):
    """Single-module AST walk. Tracks a scope stack so nested functions/
    classes get correctly qualified names, and methods are distinguished
    from free functions."""

    def __init__(self, module_name):
        self.module_name = module_name
        self.imports = []
        self.classes = []
        self.functions = []
        self._scope_stack = []  # [(kind, name), ...] kind in {"class","function"}

    def _qualify(self, name):
        prefix = ".".join(s[1] for s in self._scope_stack)
        return f"{self.module_name}.{prefix}.{name}" if prefix else f"{self.module_name}.{name}"

    @staticmethod
    def _unparse(node):
        try:
            return ast.unparse(node)
        except Exception:
            return getattr(node, "id", type(node).__name__)

    def visit_Import(self, node):
        for alias in node.names:
            self.imports.append({
                "module": alias.name, "names": [], "kind": "import",
                "alias": alias.asname, "line": node.lineno, "level": 0,
            })
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        self.imports.append({
            "module": node.module or "", "names": [a.name for a in node.names],
            "kind": "from", "alias": None, "line": node.lineno, "level": node.level or 0,
        })
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        record = {
            "qualified_name": self._qualify(node.name), "name": node.name,
            "line": node.lineno, "end_line": getattr(node, "end_lineno", node.lineno),
            "bases": [self._unparse(b) for b in node.bases],
            "decorators": [self._unparse(d) for d in node.decorator_list],
            "docstring": ast.get_docstring(node), "methods": [],
        }
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                record["methods"].append(child.name)
        self.classes.append(record)
        self._scope_stack.append(("class", node.name))
        self.generic_visit(node)
        self._scope_stack.pop()

    def visit_FunctionDef(self, node):
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node):
        self._visit_function(node)

    def _visit_function(self, node):
        in_class = bool(self._scope_stack) and self._scope_stack[-1][0] == "class"
        in_function = bool(self._scope_stack) and self._scope_stack[-1][0] == "function"
        class_qualified = f"{self.module_name}." + ".".join(s[1] for s in self._scope_stack) if in_class else None
        self.functions.append({
            "qualified_name": self._qualify(node.name), "name": node.name,
            "line": node.lineno, "end_line": getattr(node, "end_lineno", node.lineno),
            "args": [a.arg for a in node.args.args],
            "decorators": [self._unparse(d) for d in node.decorator_list],
            "is_method": in_class, "is_nested": in_function,
            "class_qualified_name": class_qualified, "docstring": ast.get_docstring(node),
        })
        self._scope_stack.append(("function", node.name))
        self.generic_visit(node)
        self._scope_stack.pop()


def index_file(path, root):
    module_name = _module_name_for(path, root)
    rel_path = str(path.relative_to(root))
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        return {
            "path": rel_path, "module_name": module_name, "line_count": 0,
            "docstring": None, "imports": [], "classes": [], "functions": [],
            "error": f"SyntaxError: {e.msg} (line {e.lineno})",
        }

    visitor = _FileVisitor(module_name)
    visitor.visit(tree)

    module_parts = module_name.split(".")
    is_package = path.name == "__init__.py"
    for imp in visitor.imports:
        targets = _resolve_import_targets(imp, root, module_parts, is_package)
        imp["resolved_paths"] = [str(t.relative_to(root)) for t in targets]
        imp["category"] = classify_module(imp["module"]) if not imp["resolved_paths"] and imp["module"] else None

    return {
        "path": rel_path, "module_name": module_name,
        "line_count": len(source.splitlines()), "docstring": ast.get_docstring(tree),
        "imports": visitor.imports, "classes": visitor.classes, "functions": visitor.functions,
        "error": None,
    }


def build_folder_tree(rel_paths):
    """rel_paths: relative path strings (POSIX-ish). Returns a nested tree:
    {"type": "dir"|"file", "name": ..., "children": [...] (dirs only)}."""
    root = {"type": "dir", "name": ".", "children": {}}
    for rp in rel_paths:
        parts = Path(rp).parts
        node = root
        for i, part in enumerate(parts):
            is_last = i == len(parts) - 1
            node["children"].setdefault(part, {
                "type": "file" if is_last else "dir", "name": part,
                "children": None if is_last else {},
            })
            node = node["children"][part]

    def finalize(n):
        if n["children"] is None:
            return {"type": n["type"], "name": n["name"]}
        return {"type": n["type"], "name": n["name"], "children": [finalize(c) for c in n["children"].values()]}

    return finalize(root)


def _get_cache_dir(root_dir):
    return Path(root_dir) / ".flowtrace"


def _load_cache(cache_path):
    try:
        if cache_path.is_file():
            return json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _save_cache(cache_path, data):
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp_file = cache_path.with_suffix(".tmp")
        temp_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp_file.replace(cache_path)
    except Exception:
        pass


def build_index(root_dir, extra_ignore=(), use_cache=True):
    root = Path(root_dir).resolve()
    cache_path = _get_cache_dir(root) / "index_cache.json"
    cached = _load_cache(cache_path) if use_cache else None

    files = discover_python_files(root, extra_ignore=extra_ignore)
    cached_meta = (cached.get("files_meta") or {}) if (cached and isinstance(cached, dict)) else {}

    current_files_set = set()
    file_records = []
    has_changes = False

    for path in files:
        rel_path = str(path.relative_to(root))
        current_files_set.add(rel_path)
        try:
            stat = path.stat()
            mtime, size = stat.st_mtime, stat.st_size
        except OSError:
            mtime, size = 0, 0

        cached_entry = cached_meta.get(rel_path)
        if (cached_entry and cached_entry.get("mtime") == mtime 
                and cached_entry.get("size") == size and "record" in cached_entry):
            file_records.append(cached_entry["record"])
        else:
            has_changes = True
            rec = index_file(path, root)
            file_records.append(rec)
            cached_meta[rel_path] = {
                "mtime": mtime,
                "size": size,
                "record": rec,
            }

    deleted_files = set(cached_meta.keys()) - current_files_set
    if deleted_files:
        has_changes = True
        for df in deleted_files:
            cached_meta.pop(df, None)

    if not has_changes and cached and "index_doc" in cached:
        return cached["index_doc"]

    all_classes, all_functions, import_graph = [], [], []
    external_categories = {}
    unresolved_imports = []

    for rec in file_records:
        for cls in rec["classes"]:
            all_classes.append({**cls, "file": rec["path"]})
        for fn in rec["functions"]:
            all_functions.append({**fn, "file": rec["path"]})
        for imp in rec["imports"]:
            if imp.get("resolved_paths"):
                for target in imp["resolved_paths"]:
                    import_graph.append({"from": rec["path"], "to": target, "via_module": imp.get("module") or ".".join(imp.get("names", []))})
            elif imp.get("module"):
                cat = imp.get("category")
                if cat:
                    external_categories[cat] = external_categories.get(cat, 0) + 1
                unresolved_imports.append({"file": rec["path"], "module": imp.get("module"), "category": cat})

    rel_paths = [r["path"] for r in file_records]

    index_doc = {
        "schema_version": "0.1.0",
        "generated_at": time.time(),
        "root_dir": str(root),
        "file_count": len(file_records),
        "class_count": len(all_classes),
        "function_count": len(all_functions),
        "files": file_records,
        "classes": all_classes,
        "functions": all_functions,
        "import_graph": import_graph,
        "external_dependencies_by_category": external_categories,
        "unresolved_imports": unresolved_imports,
        "folder_tree": build_folder_tree(rel_paths),
    }

    if use_cache:
        _save_cache(cache_path, {
            "version": 1,
            "files_meta": cached_meta,
            "index_doc": index_doc,
            "call_graph": None,
        })

    return index_doc


def get_cached_call_graph(root_dir):
    root = Path(root_dir).resolve()
    cache_path = _get_cache_dir(root) / "index_cache.json"
    cached = _load_cache(cache_path)
    if cached and isinstance(cached, dict):
        return cached.get("call_graph")
    return None


def save_cached_call_graph(root_dir, call_graph):
    root = Path(root_dir).resolve()
    cache_path = _get_cache_dir(root) / "index_cache.json"
    cached = _load_cache(cache_path)
    if cached and isinstance(cached, dict):
        cached["call_graph"] = call_graph
        _save_cache(cache_path, cached)

