"""Static call graph (foundation for Phase 4 / Code Navigation).

This is a different graph from both the import graph (file-level, in
indexer.py) and the runtime call graph (execution-order, in tracer.py):
it's the *static* "who calls whom" relationship between functions/methods,
resolved via each file's import bindings and the project's symbol table -
without executing anything.

Resolution is intentionally best-effort: `self.method()` / `cls.method()`,
direct calls to imported or sibling functions/classes, and one level of
`module.function()` attribute access are resolved. Anything dynamic
(decorated dispatch, `getattr`, calls through `*args`, star-imports, deep
attribute chains) is left unresolved but still recorded - unresolved calls
whose base name traces back to a *known external import* are still tagged
with an architectural category, so "callees" can show e.g. `requests.get`
as [Network] even without a project-local target.
"""

import ast
from pathlib import Path
import builtins as _builtins_module

from .classifier import classify_module
from .inference import (build_function_environment, build_class_self_attribute_environment, resolve_via_type,
                         infer_function_return_type, _constructor_target)

_BUILTIN_NAMES = frozenset(dir(_builtins_module))


def _build_symbol_tables(index_doc):
    """module_name -> {simple_name: qualified_name}, for functions and classes."""
    module_functions = {}
    module_classes = {}
    module_name_by_file = {f["path"]: f["module_name"] for f in index_doc["files"]}

    for fn in index_doc["functions"]:
        if fn["is_nested"] or fn["is_method"]:
            continue
        module_name = module_name_by_file.get(fn["file"])
        if module_name is not None:
            module_functions.setdefault(module_name, {})[fn["name"]] = fn["qualified_name"]

    for cls in index_doc["classes"]:
        module_name = module_name_by_file.get(cls["file"])
        if module_name is not None:
            module_classes.setdefault(module_name, {})[cls["name"]] = {
                "qualified_name": cls["qualified_name"], "methods": set(cls["methods"]),
            }

    return module_functions, module_classes, module_name_by_file


def _local_bindings_for_file(file_record, module_functions, module_classes, module_name_by_file):
    """name -> ("func"|"class"|"module"|"external", target) for names this
    file's imports bind at module scope. Sibling top-level definitions are
    added separately by the caller."""
    bindings = {}
    for imp in file_record["imports"]:
        resolved = imp.get("resolved_paths") or []

        if imp["kind"] == "import":
            local_name = imp["alias"] or imp["module"].split(".")[0]
            if resolved:
                target_module = next(
                    (module_name_by_file.get(rp) for rp in resolved if module_name_by_file.get(rp) == imp["module"]),
                    module_name_by_file.get(resolved[0]),
                )
                bindings[local_name] = ("module", target_module)
            else:
                bindings[local_name] = ("external", imp.get("category") or classify_module(imp["module"]))

        elif imp["kind"] == "from":
            # resolved[0], when present, is always the *direct* module match if
            # one exists (indexer.py guarantees that ordering) - so it's a safe
            # best-effort candidate even for relative imports, where imp["module"]
            # is just the unqualified tail ("serializer", not "flowtrace.serializer")
            # and can't be string-compared against the fully-qualified module name.
            direct_module = module_name_by_file.get(resolved[0]) if resolved else None
            for name in imp["names"]:
                if name == "*":
                    # `from .submodule import *` - can't know which names
                    # without resolving submodule's own public symbols, so
                    # record the target module under a reserved pseudo-key
                    # ("*" can never collide with a real Python identifier)
                    # for _resolve_call/_constructor_target to check as a
                    # last resort. Found to matter a lot in practice: httpx
                    # re-exports its ENTIRE public API this way, once per
                    # submodule, in its own __init__.py.
                    if direct_module:
                        bindings.setdefault("*", []).append(direct_module)
                    continue
                resolved_as = None
                if direct_module:
                    if name in module_functions.get(direct_module, {}):
                        resolved_as = ("func", module_functions[direct_module][name])
                    elif name in module_classes.get(direct_module, {}):
                        resolved_as = ("class", module_classes[direct_module][name]["qualified_name"])
                if resolved_as is None:
                    for rp in resolved:
                        mn = module_name_by_file.get(rp)
                        if mn and mn.split(".")[-1] == name:
                            resolved_as = ("module", mn)
                            break
                if resolved_as is None:
                    category = imp.get("category") or (classify_module(imp["module"]) if imp["module"] else "Logic")
                    resolved_as = ("external", category)
                bindings[name] = resolved_as
    return bindings


def _wildcard_lookup(target_module, attr, local_bindings_by_module, module_functions, module_classes):
    """Last-resort lookup through target_module's own `from .sub import *`
    re-exports. Only used after direct definition and named re-export
    both fail. If `attr` is found in more than one wildcard target,
    that's a genuine ambiguity (rare - names colliding across a package's
    own submodules) and this returns None rather than guessing."""
    targets = local_bindings_by_module.get(target_module, {}).get("*", [])
    found = []
    for wt in targets:
        if attr in module_functions.get(wt, {}):
            found.append(("func", module_functions[wt][attr]))
        elif attr in module_classes.get(wt, {}):
            found.append(("class", module_classes[wt][attr]["qualified_name"]))
    if len(found) == 1:
        return found[0]
    return None


class _CallCollector(ast.NodeVisitor):
    """Walks one module's AST, recording every Call node together with the
    qualified name of whichever function/method scope it occurs in, and
    (for inference.py) the FunctionDef/ClassDef nodes themselves keyed by
    qualified name."""

    def __init__(self, module_name):
        self.module_name = module_name
        self.calls = []
        self.function_nodes = {}  # qualified_name -> ast.FunctionDef/AsyncFunctionDef
        self.class_nodes = {}     # qualified_name -> ast.ClassDef
        self._scope_stack = []  # [(kind, name), ...]
        self._cur_func = None
        self._cur_self = None  # first-arg name, if the enclosing function is a method

    def _qualify(self, name):
        prefix = ".".join(s[1] for s in self._scope_stack)
        return f"{self.module_name}.{prefix}.{name}" if prefix else f"{self.module_name}.{name}"

    def visit_ClassDef(self, node):
        self.class_nodes[self._qualify(node.name)] = node
        self._scope_stack.append(("class", node.name))
        self.generic_visit(node)
        self._scope_stack.pop()

    def visit_FunctionDef(self, node):
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node):
        self._visit_function(node)

    def _visit_function(self, node):
        qname = self._qualify(node.name)
        self.function_nodes[qname] = node
        in_class = bool(self._scope_stack) and self._scope_stack[-1][0] == "class"
        self_arg = node.args.args[0].arg if (in_class and node.args.args) else None

        prev_func, prev_self = self._cur_func, self._cur_self
        self._cur_func, self._cur_self = qname, self_arg
        self._scope_stack.append(("function", node.name))
        self.generic_visit(node)
        self._scope_stack.pop()
        self._cur_func, self._cur_self = prev_func, prev_self

    def visit_Call(self, node):
        if self._cur_func is not None:
            base, chain = self._decompose(node.func)
            self.calls.append({
                "caller": self._cur_func, "self_arg": self._cur_self,
                "base": base, "chain": chain, "line": node.lineno,
                "raw": self._safe_unparse(node.func),
                "args": node.args, "keywords": node.keywords,  # raw AST, for parameter type propagation
            })
        self.generic_visit(node)

    @staticmethod
    def _decompose(func_node):
        chain = []
        cur = func_node
        while isinstance(cur, ast.Attribute):
            chain.insert(0, cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            return cur.id, chain
        if isinstance(cur, ast.Call) and isinstance(cur.func, ast.Name) and cur.func.id == "super" and not cur.args:
            # zero-arg super().method() - flagged with a sentinel base that
            # can never collide with a real identifier; _resolve_call
            # special-cases it to walk the caller's class's own bases.
            return "<super>", chain
        return None, chain  # base isn't a simple name (call result, subscript, ...) - unresolved

    @staticmethod
    def _safe_unparse(node):
        try:
            return ast.unparse(node)
        except Exception:
            return "<expr>"


def _decompose_expr_str(expr_str):
    """Same decomposition as _CallCollector._decompose, but starting from
    a raw expression string (used for class base expressions, which
    indexer.py stores pre-unparsed as text)."""
    try:
        node = ast.parse(expr_str, mode="eval").body
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


def _resolve_base_class(base_expr_str, local_bindings, module_classes, current_module):
    base_name, chain = _decompose_expr_str(base_expr_str)
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


def _resolve_super_call(class_of_caller, method_name, classes_by_qname, local_bindings, module_classes, current_module):
    """Resolves `super().method()` to the first base class (in MRO order
    as written) that defines `method`. Deliberately one level only - if
    that base doesn't have the method, this does NOT continue walking
    its own bases (real multi-level MRO) - a scoped boundary, not an
    oversight, since chained inheritance beyond one level is rarer and
    adds real complexity for a diminishing-returns case."""
    info = classes_by_qname.get(class_of_caller)
    if not info:
        return None
    for base_expr in info.get("bases", []):
        base_qname = _resolve_base_class(base_expr, local_bindings, module_classes, current_module)
        if base_qname and method_name in classes_by_qname.get(base_qname, {}).get("methods", ()):
            return f"{base_qname}.{method_name}"
    return None


def _resolve_call(call, current_module, local_bindings, module_functions, module_classes, class_of_caller,
                   local_bindings_by_module=None, classes_by_qname=None):
    base, chain = call["base"], call["chain"]
    if base is None:
        return None, None

    if base == "<super>" and len(chain) == 1 and class_of_caller and classes_by_qname is not None:
        target = _resolve_super_call(class_of_caller, chain[0], classes_by_qname, local_bindings, module_classes, current_module)
        if target:
            return target, None
        return None, None

    if call["self_arg"] and base == call["self_arg"] and len(chain) == 1 and class_of_caller:
        for info in module_classes.get(current_module, {}).values():
            if info["qualified_name"] == class_of_caller and chain[0] in info["methods"]:
                return f"{class_of_caller}.{chain[0]}", None

    if base in local_bindings:
        kind, target = local_bindings[base]
        if not chain and kind in ("func", "class"):
            return target, None
        if len(chain) == 1 and kind == "class" and classes_by_qname is not None:
            # ClassName.method(...) - a classmethod/staticmethod called
            # directly on the class object, not through an instance. Found
            # via real-world diagnosis (Retry.from_int(...) in requests'
            # own source) as a distinct gap from the instance-call cases
            # Phase 1 targeted - this needs no type inference at all, just
            # checking the method exists on the class already known here.
            attr = chain[0]
            if attr in classes_by_qname.get(target, {}).get("methods", ()):
                return f"{target}.{attr}", None
        if len(chain) == 1 and kind == "module":
            attr = chain[0]
            if attr in module_functions.get(target, {}):
                return module_functions[target][attr], None
            if attr in module_classes.get(target, {}):
                return module_classes[target][attr]["qualified_name"], None
            # Real-world validation (see README) found this is the single
            # highest-leverage gap of all: `attr` isn't DEFINED in `target`,
            # but `target`'s own __init__.py might RE-EXPORT it via its own
            # `from .submodule import attr` - the canonical way almost every
            # real Python package exposes its public API (e.g. requests'
            # `from .api import get` is why `requests.get(...)` works at all).
            # One level of re-export indirection only - deliberately not
            # chasing re-exports of re-exports, to avoid needing cycle
            # detection for a case that's rare in practice.
            if local_bindings_by_module is not None:
                reexport = local_bindings_by_module.get(target, {}).get(attr)
                if reexport and reexport[0] == "func":
                    return reexport[1], None
                if reexport and reexport[0] == "class":
                    return reexport[1], None
                wildcard = _wildcard_lookup(target, attr, local_bindings_by_module, module_functions, module_classes)
                if wildcard:
                    return wildcard[1], None
        if kind == "external":
            return None, target

    if not chain:
        if base in module_functions.get(current_module, {}):
            return module_functions[current_module][base], None
        if base in module_classes.get(current_module, {}):
            return module_classes[current_module][base]["qualified_name"], None
        if base in _BUILTIN_NAMES:
            # Real gap found via validation against psf/requests, pallets/flask,
            # flasky: bare calls to len/isinstance/str/getattr/etc were the
            # single largest contributor to "fully unresolved" call sites
            # (up to 16% of all calls) - purely because builtins aren't
            # imported, so they never appeared in local_bindings at all.
            # Fixed cheaply: builtins() is a fixed, enumerable set with
            # zero false-positive risk, unlike the type-inference gap below.
            return None, "Stdlib"

    return None, None


def build_call_graph(index_doc, root_dir):
    """Returns a list of edges: {caller, callee, callee_expr, file, line,
    resolved, category, confidence, evidence, possible_callees}.

    `callee` is a qualified name only when PROVEN (confidence=1.0) -
    either via the original direct/self/import resolution, Phase 1 type
    inference, or Phase 2 (function-return inference, parameter type
    propagation, classmethod calls, super()). `possible_callees` holds
    plausible-but-unproven targets (e.g. a variable with more than one
    possible type) - these must never be treated as "no caller" by
    downstream consumers (see impact.py).

    Multi-pass pipeline (why: parameter type propagation needs to know
    which calls target a given function, which needs a first resolution
    pass to already exist; function-return inference needs each
    function's own local environment already built, which itself can
    depend on OTHER functions' already-inferred return types):

      1. local_bindings_by_module - every file's own import bindings
      2. parse + collect every file (once) - AST, calls, function/class nodes
      3. SHALLOW environments (no function-return awareness yet)
      4. function_return_types, computed from the shallow environments
      5. DEEP environments, rebuilt now WITH function_return_types
      6. first call-resolution pass, using the deep environments
      7. param_types, computed from pass 6's resolved edges + deep envs
      8. final pass: upgrade previously-unresolved var.method() edges
         where `var` is exactly a parameter with a now-known type
    """
    root = Path(root_dir).resolve()
    module_functions, module_classes, module_name_by_file = _build_symbol_tables(index_doc)
    function_by_qname = {fn["qualified_name"]: fn for fn in index_doc["functions"]}
    classes_by_qname = {cls["qualified_name"]: {"methods": set(cls["methods"]), "bases": cls["bases"]} for cls in index_doc["classes"]}

    # PASS 1: local_bindings for every file, needed before any resolution
    # since re-export/wildcard resolution looks at a DIFFERENT module's
    # bindings than the one currently being resolved.
    local_bindings_by_module = {}
    for file_record in index_doc["files"]:
        if file_record.get("error"):
            continue
        module_name = file_record["module_name"]
        lb = _local_bindings_for_file(file_record, module_functions, module_classes, module_name_by_file)
        for fname, qname in module_functions.get(module_name, {}).items():
            lb.setdefault(fname, ("func", qname))
        for cname, info in module_classes.get(module_name, {}).items():
            lb.setdefault(cname, ("class", info["qualified_name"]))
        local_bindings_by_module[module_name] = lb

    # PASS 2: parse + collect every file exactly once.
    parsed = {}  # module_name -> {"file_record", "collector"}
    for file_record in index_doc["files"]:
        if file_record.get("error"):
            continue
        path = root / file_record["path"]
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        except SyntaxError:
            continue
        module_name = file_record["module_name"]
        collector = _CallCollector(module_name)
        collector.visit(tree)
        parsed[module_name] = {"file_record": file_record, "collector": collector}

    # PASS 3: shallow environments (constructor/alias/with only - no
    # function-return awareness, since we don't know any return types yet).
    shallow_envs = {}   # func_qname -> env
    shallow_self_envs = {}  # class_qname -> env
    for module_name, data in parsed.items():
        local_bindings = local_bindings_by_module[module_name]
        for func_qname, func_node in data["collector"].function_nodes.items():
            shallow_envs[func_qname] = build_function_environment(func_node, local_bindings, module_classes, module_name, local_bindings_by_module)
        for class_qname, class_node in data["collector"].class_nodes.items():
            shallow_self_envs[class_qname] = build_class_self_attribute_environment(class_node, local_bindings, module_classes, module_name, local_bindings_by_module)

    # PASS 4: function return types, using the shallow environments.
    function_return_types = {}
    for module_name, data in parsed.items():
        local_bindings = local_bindings_by_module[module_name]
        for func_qname, func_node in data["collector"].function_nodes.items():
            rt = infer_function_return_type(func_node, shallow_envs.get(func_qname, {}), local_bindings, module_classes,
                                             module_functions, module_name, local_bindings_by_module)
            if rt:
                function_return_types[func_qname] = rt

    # PASS 5: deep environments, now with function_return_types available -
    # `x = make_thing()` can resolve if make_thing's return type is known.
    deep_envs = {}
    deep_self_envs = {}
    for module_name, data in parsed.items():
        local_bindings = local_bindings_by_module[module_name]
        for func_qname, func_node in data["collector"].function_nodes.items():
            deep_envs[func_qname] = build_function_environment(func_node, local_bindings, module_classes, module_name, local_bindings_by_module,
                                                                 module_functions, function_return_types)
        for class_qname, class_node in data["collector"].class_nodes.items():
            deep_self_envs[class_qname] = build_class_self_attribute_environment(class_node, local_bindings, module_classes, module_name, local_bindings_by_module,
                                                                                  module_functions, function_return_types)

    def resolve_one(call, module_name, local_bindings, class_of_caller):
        target, category = _resolve_call(call, module_name, local_bindings, module_functions, module_classes, class_of_caller,
                                          local_bindings_by_module, classes_by_qname)
        confidence, evidence, possible = None, None, []
        if target is not None:
            confidence = 1.0
            evidence = "direct resolution (self/sibling/import/re-export/classmethod/super)"
        elif category is None:
            base, chain = call["base"], call["chain"]
            if base is not None and len(chain) == 1 and base != call["self_arg"]:
                types = deep_envs.get(call["caller"], {}).get(base)
                if types:
                    target, confidence, possible = resolve_via_type(types, chain[0], classes_by_qname)
                    if target:
                        evidence = f"'{base}' inferred as {sorted(types)}"
                    elif possible:
                        evidence = f"'{base}' has {len(types)} possible type(s): {sorted(types)}"
            elif base is not None and base == call["self_arg"] and len(chain) == 2 and class_of_caller:
                types = deep_self_envs.get(class_of_caller, {}).get(chain[0])
                if types:
                    target, confidence, possible = resolve_via_type(types, chain[1], classes_by_qname)
                    if target:
                        evidence = f"'self.{chain[0]}' inferred as {sorted(types)}"
                    elif possible:
                        evidence = f"'self.{chain[0]}' has {len(types)} possible type(s): {sorted(types)}"
        return {
            "caller": call["caller"], "callee": target, "callee_expr": call["raw"],
            "resolved": target is not None, "category": category,
            "confidence": confidence, "evidence": evidence, "possible_callees": possible,
        }

    # PASS 6: first resolution pass. Kept as a flat list of (call, edge,
    # file_record) so pass 7/8 can revisit unresolved ones without
    # re-parsing anything.
    pending = []
    for module_name, data in parsed.items():
        local_bindings = local_bindings_by_module[module_name]
        for call in data["collector"].calls:
            caller_fn = function_by_qname.get(call["caller"])
            class_of_caller = caller_fn["class_qualified_name"] if caller_fn else None
            edge = resolve_one(call, module_name, local_bindings, class_of_caller)
            edge["file"] = data["file_record"]["path"]
            edge["line"] = call["line"]
            pending.append({"call": call, "edge": edge, "module_name": module_name, "local_bindings": local_bindings})

    # PASS 7: parameter type propagation. For every function with >=1
    # PROVEN call site, and every parameter position/keyword, collect the
    # inferred type of the actual argument expression at each such call
    # site (via the CALLER's own deep env). If every call site that had a
    # determinable argument type agrees on exactly one, that's usable as
    # the parameter's type inside the function body. A function called
    # from only one place, or with disagreeing argument types, gets
    # nothing here - never guessed.
    param_type_candidates = {}  # (func_qname, param_name) -> set(qnames)
    for item in pending:
        edge, call = item["edge"], item["call"]
        callee_qname = edge["callee"]
        if not callee_qname:
            continue
        fn = function_by_qname.get(callee_qname)
        if not fn:
            continue
        param_names = fn["args"]
        offset = 1 if fn["is_method"] else 0  # skip self/cls
        caller_env = deep_envs.get(call["caller"], {})
        caller_local_bindings = item["local_bindings"]
        caller_module = item["module_name"]

        for i, arg_node in enumerate(call["args"]):
            pi = i + offset
            if pi >= len(param_names):
                continue
            _accumulate_param_type(param_type_candidates, callee_qname, param_names[pi], arg_node,
                                    caller_env, caller_local_bindings, module_classes, caller_module, local_bindings_by_module)
        for kw in call["keywords"]:
            if kw.arg and kw.arg in param_names:
                _accumulate_param_type(param_type_candidates, callee_qname, kw.arg, kw.value,
                                        caller_env, caller_local_bindings, module_classes, caller_module, local_bindings_by_module)

    param_types_by_func = {}  # func_qname -> {param_name: frozenset(qnames)} - may have >1 candidate;
    # resolve_via_type (called in pass 8) is what decides proven vs possible, same as everywhere else.
    for (func_qname, param_name), types in param_type_candidates.items():
        param_types_by_func.setdefault(func_qname, {})[param_name] = frozenset(types)

    # PASS 8: upgrade previously-unresolved var.method() edges where `var`
    # is exactly a parameter name with a now-known type. Only needs a
    # fresh environment for functions that actually got a param type - a
    # narrow, targeted re-check, not a full re-run of pass 6.
    for item in pending:
        edge, call = item["edge"], item["call"]
        if edge["callee"] or edge["possible_callees"]:
            continue  # already resolved (or already flagged ambiguous) - don't touch
        base, chain = call["base"], call["chain"]
        if base is None or len(chain) != 1 or base == call["self_arg"]:
            continue
        param_types = param_types_by_func.get(call["caller"], {})
        types = param_types.get(base)
        if not types:
            continue
        target, confidence, possible = resolve_via_type(types, chain[0], classes_by_qname)
        if target:
            edge["callee"], edge["resolved"] = target, True
            edge["confidence"], edge["evidence"] = confidence, f"parameter '{base}' inferred as {sorted(types)} from all call sites"
        elif possible:
            edge["confidence"], edge["possible_callees"] = confidence, possible
            edge["evidence"] = f"parameter '{base}' has {len(types)} possible type(s) across call sites: {sorted(types)}"

    return [item["edge"] for item in pending]


def _accumulate_param_type(candidates, func_qname, param_name, arg_node, caller_env, caller_local_bindings,
                            module_classes, caller_module, local_bindings_by_module):
    qname = None
    if isinstance(arg_node, ast.Name):
        types = caller_env.get(arg_node.id)
        if types and len(types) == 1:
            qname = next(iter(types))
    elif isinstance(arg_node, ast.Call):
        qname = _constructor_target(arg_node, caller_local_bindings, module_classes, caller_module, local_bindings_by_module)
    if qname:
        candidates.setdefault((func_qname, param_name), set()).add(qname)
