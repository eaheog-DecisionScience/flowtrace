"""Local variable, self-attribute, function-return, and parameter type
inference (Phase 1 + Phase 2).

Deliberately narrow, not a general Python type system. Scope directly
driven by real-world measurement (see README) against psf/requests,
pallets/flask, flasky, django, httpx, click, and rich.

Phase 1 (constructor-assignment-based):
    x = ClassName()                  direct constructor (same module or imported)
    x = module.ClassName()           module-qualified constructor
    x = y                            alias (copies y's known type, if any)
    self.x = ClassName()             instance attribute, aggregated across
                                      ALL of a class's methods
    with ClassName() as x:           with-statement binding

Phase 2 (this round - measurement-driven, attacking the next-largest
category found after Phase 1's real-world rerun):
    x = make_thing()                 function-return inference - x's type
                                      is make_thing's inferred return type,
                                      IF every non-trivial `return` in
                                      make_thing agrees on exactly one type
    def f(session): session.get()    parameter type propagation - if EVERY
                                      call site to f passes an argument of
                                      the same known type for `session`,
                                      that type is usable inside f's body
    ClassName.method()               classmethod/staticmethod called
                                      directly on the class (not an
                                      instance) - no inference needed, just
                                      a resolution path that was missing
    super().method()                 resolved via the caller's class's own
                                      first base that defines the method
                                      (one level of MRO, not full chain)

Explicitly still NOT attempted: control-flow-sensitive narrowing (an
if/else assigning two different types is "both possible," not analyzed
per-branch), container element types, getattr/dynamic dispatch, and
function-return/parameter chains deeper than one level (a function whose
return type depends on a parameter whose type depends on another
function's return type... is not chased indefinitely).

Critically, unchanged from Phase 1: a variable assigned more than one
distinct type is never silently collapsed to a single guess.
`resolve_via_type` only returns a PROVEN target when every candidate type
agrees; otherwise it returns POSSIBLE targets, which callers (callgraph.py,
impact.py) must not treat as equivalent to "no caller" - see impact.py's
possible_callers.
"""

import ast


def _decompose(node):
    """(base_name, [attr, attr, ...]) for a Name/Attribute chain, or
    (None, []) if the base isn't a simple Name - same shape callgraph.py's
    _CallCollector uses, so results compose directly."""
    chain = []
    cur = node
    while isinstance(cur, ast.Attribute):
        chain.insert(0, cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        return cur.id, chain
    return None, chain


def _constructor_target(call_node, local_bindings, module_classes, current_module, local_bindings_by_module=None):
    """If `call_node` looks like a constructor call for a known class -
    ClassName() or module.ClassName() - returns its qualified_name, else
    None. Reuses the same local_bindings a normal call resolution uses."""
    if not isinstance(call_node, ast.Call):
        return None
    base, chain = _decompose(call_node.func)
    if base is None:
        return None

    if not chain:
        if base in local_bindings and local_bindings[base][0] == "class":
            return local_bindings[base][1]
        if base in module_classes.get(current_module, {}):
            return module_classes[current_module][base]["qualified_name"]
    elif len(chain) == 1 and base in local_bindings and local_bindings[base][0] == "module":
        target_module = local_bindings[base][1]
        attr = chain[0]
        if attr in module_classes.get(target_module, {}):
            return module_classes[target_module][attr]["qualified_name"]
        # same re-export gap as callgraph.py's _resolve_call: `attr` might
        # not be defined in target_module directly, but re-exported via
        # target_module's own `from .submodule import attr` (or `import *`)
        if local_bindings_by_module is not None:
            reexport = local_bindings_by_module.get(target_module, {}).get(attr)
            if reexport and reexport[0] == "class":
                return reexport[1]
            wildcard_targets = local_bindings_by_module.get(target_module, {}).get("*", [])
            found = [wt for wt in wildcard_targets if attr in module_classes.get(wt, {})]
            if len(found) == 1:
                return module_classes[found[0]][attr]["qualified_name"]
    return None


def _function_call_target(call_node, local_bindings, module_functions, current_module, local_bindings_by_module=None):
    """Same shape as _constructor_target, but resolves to a FUNCTION
    qualified name instead of a class - used to detect `x = make_thing()`
    so make_thing's inferred return type (see infer_function_return_type)
    can be attributed to `x`."""
    if not isinstance(call_node, ast.Call):
        return None
    base, chain = _decompose(call_node.func)
    if base is None:
        return None

    if not chain:
        if base in local_bindings and local_bindings[base][0] == "func":
            return local_bindings[base][1]
        if base in module_functions.get(current_module, {}):
            return module_functions[current_module][base]
    elif len(chain) == 1 and base in local_bindings and local_bindings[base][0] == "module":
        target_module = local_bindings[base][1]
        attr = chain[0]
        if attr in module_functions.get(target_module, {}):
            return module_functions[target_module][attr]
        if local_bindings_by_module is not None:
            reexport = local_bindings_by_module.get(target_module, {}).get(attr)
            if reexport and reexport[0] == "func":
                return reexport[1]
            wildcard_targets = local_bindings_by_module.get(target_module, {}).get("*", [])
            found = [wt for wt in wildcard_targets if attr in module_functions.get(wt, {})]
            if len(found) == 1:
                return module_functions[found[0]][attr]
    return None


class _ReturnCollector(ast.NodeVisitor):
    """Collects every `return <expr>` in a function's OWN top-level body
    (not nested functions) and tries to resolve each to a candidate type,
    using constructor calls, aliases already known in the function's own
    environment, and (once available) other functions' already-inferred
    return types. A bare `return` or `return None` is neutral - common
    early-exit/sentinel pattern, doesn't disqualify the rest. Any other
    kind of return expression downgrades the whole function to "unknown"
    - conservative, matching Phase 1's core discipline: no guessing."""

    def __init__(self, env, local_bindings, module_classes, module_functions, current_module,
                 local_bindings_by_module=None, function_return_types=None):
        self.env = env
        self.local_bindings = local_bindings
        self.module_classes = module_classes
        self.module_functions = module_functions
        self.current_module = current_module
        self.local_bindings_by_module = local_bindings_by_module
        self.function_return_types = function_return_types or {}
        self.candidates = set()
        self.saw_unknown = False

    def visit_Return(self, node):
        if node.value is None:
            return
        if isinstance(node.value, ast.Constant) and node.value.value is None:
            return
        if isinstance(node.value, ast.Call):
            qname = _constructor_target(node.value, self.local_bindings, self.module_classes, self.current_module, self.local_bindings_by_module)
            if qname is None:
                func_qname = _function_call_target(node.value, self.local_bindings, self.module_functions, self.current_module, self.local_bindings_by_module)
                if func_qname:
                    qname = self.function_return_types.get(func_qname)
            if qname:
                self.candidates.add(qname)
            else:
                self.saw_unknown = True
        elif isinstance(node.value, ast.Name):
            types = self.env.get(node.value.id)
            if types:
                self.candidates |= set(types)
            else:
                self.saw_unknown = True
        else:
            self.saw_unknown = True

    def visit_FunctionDef(self, node):
        pass

    def visit_AsyncFunctionDef(self, node):
        pass


def infer_function_return_type(func_node, env, local_bindings, module_classes, module_functions, current_module,
                                local_bindings_by_module=None, function_return_types=None):
    """Returns a single qualified class name if every non-trivial `return`
    in this function agrees on exactly one type, else None. One level of
    chaining through other functions' return types is supported (pass
    function_return_types computed from a prior pass over "shallower"
    environments) - deeper chains aren't attempted."""
    collector = _ReturnCollector(env, local_bindings, module_classes, module_functions, current_module,
                                  local_bindings_by_module, function_return_types)
    for stmt in func_node.body:
        collector.visit(stmt)
    if collector.saw_unknown or len(collector.candidates) != 1:
        return None
    return next(iter(collector.candidates))


class _LocalEnvironmentBuilder(ast.NodeVisitor):
    """Single-function-body pass: builds var_name -> frozenset(qualified
    class names) | None from constructor assignments and same-scope
    aliasing. Does not descend into nested function/class definitions -
    their scope is independent and built separately, when they're
    visited as their own top-level unit."""

    def __init__(self, local_bindings, module_classes, current_module, local_bindings_by_module=None,
                 module_functions=None, function_return_types=None, param_types=None):
        self.local_bindings = local_bindings
        self.module_classes = module_classes
        self.current_module = current_module
        self.local_bindings_by_module = local_bindings_by_module
        self.module_functions = module_functions or {}
        self.function_return_types = function_return_types or {}
        self.env = dict(param_types or {})  # seed with known parameter types, if any (Phase 2)

    def _record(self, name, qname_or_none):
        if qname_or_none is None:
            self.env[name] = None  # explicitly downgraded - reassigned to something we can't infer
            return
        existing = self.env.get(name)
        if name in self.env and existing is None:
            return  # stays downgraded once unknown; Phase 1 doesn't do flow-order precision
        types = set(existing) if existing else set()
        types.add(qname_or_none)
        self.env[name] = frozenset(types)

    def _resolve_call_value(self, value):
        qname = _constructor_target(value, self.local_bindings, self.module_classes, self.current_module, self.local_bindings_by_module)
        if qname is None and self.function_return_types:
            func_qname = _function_call_target(value, self.local_bindings, self.module_functions, self.current_module, self.local_bindings_by_module)
            if func_qname:
                qname = self.function_return_types.get(func_qname)
        return qname

    def visit_Assign(self, node):
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
            value = node.value
            if isinstance(value, ast.Call):
                self._record(target, self._resolve_call_value(value))
            elif isinstance(value, ast.Name):
                if value.id in self.env:
                    source = self.env[value.id]
                    if source is None:
                        self._record(target, None)
                    else:
                        for qn in source:
                            self._record(target, qn)
                # else: source not tracked at all - leave target untouched
                # rather than marking unknown, since we may just not have
                # seen its origin (e.g. a parameter) rather than it truly
                # being untypeable
            else:
                self._record(target, None)
        self.generic_visit(node)

    def _visit_with(self, node):
        # `with Expr() as name:` binds `name` exactly like `name = Expr()`
        # would - found via real-world validation to matter a lot: it's
        # the exact pattern connecting requests.get() to Session.request()
        # in psf/requests' own api.py (`with sessions.Session() as session:`),
        # which was silently invisible to Phase 1 before this handler existed.
        for item in node.items:
            if item.optional_vars is not None and isinstance(item.optional_vars, ast.Name):
                target = item.optional_vars.id
                if isinstance(item.context_expr, ast.Call):
                    self._record(target, self._resolve_call_value(item.context_expr))
                else:
                    self._record(target, None)
        self.generic_visit(node)

    def visit_With(self, node):
        self._visit_with(node)

    def visit_AsyncWith(self, node):
        self._visit_with(node)

    def visit_FunctionDef(self, node):
        pass

    def visit_AsyncFunctionDef(self, node):
        pass

    def visit_ClassDef(self, node):
        pass


def build_function_environment(func_node, local_bindings, module_classes, current_module, local_bindings_by_module=None,
                                module_functions=None, function_return_types=None, param_types=None):
    builder = _LocalEnvironmentBuilder(local_bindings, module_classes, current_module, local_bindings_by_module,
                                        module_functions, function_return_types, param_types)
    for stmt in func_node.body:
        builder.visit(stmt)
    return builder.env


class _SelfAttributeBuilder(ast.NodeVisitor):
    """Scans ALL of a class's methods (not just __init__) for
    `self.attr = expr` assignments, aggregating into attr_name ->
    frozenset(qualified class names) for the class as a whole."""

    def __init__(self, self_arg_names, local_bindings, module_classes, current_module, local_bindings_by_module=None,
                 module_functions=None, function_return_types=None):
        self.self_arg_names = self_arg_names
        self.local_bindings = local_bindings
        self.module_classes = module_classes
        self.current_module = current_module
        self.local_bindings_by_module = local_bindings_by_module
        self.module_functions = module_functions or {}
        self.function_return_types = function_return_types or {}
        self.env = {}

    def visit_Assign(self, node):
        for target in node.targets:
            if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                    and target.value.id in self.self_arg_names and isinstance(node.value, ast.Call)):
                qname = _constructor_target(node.value, self.local_bindings, self.module_classes, self.current_module, self.local_bindings_by_module)
                if qname is None and self.function_return_types:
                    func_qname = _function_call_target(node.value, self.local_bindings, self.module_functions, self.current_module, self.local_bindings_by_module)
                    if func_qname:
                        qname = self.function_return_types.get(func_qname)
                if qname:
                    existing = self.env.get(target.attr, frozenset())
                    self.env[target.attr] = frozenset(existing | {qname})
        self.generic_visit(node)


def build_class_self_attribute_environment(class_node, local_bindings, module_classes, current_module, local_bindings_by_module=None,
                                            module_functions=None, function_return_types=None):
    builder = _SelfAttributeBuilder({"self"}, local_bindings, module_classes, current_module, local_bindings_by_module,
                                     module_functions, function_return_types)
    for item in class_node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            builder.visit(item)
    return builder.env


def resolve_via_type(qualified_class_names, method_name, classes_by_qname):
    """Given a set of candidate qualified class names for some variable
    and a method name being called on it, returns
    (proven_target_or_None, confidence, possible_targets).

    Only returns a PROVEN target (confidence=1.0) when there's exactly
    one candidate type AND it has that method. Multiple candidate types,
    or a method that exists on more than one candidate, produce POSSIBLE
    targets instead - never silently collapsed to a guess.
    """
    if not qualified_class_names:
        return None, None, []
    targets = sorted({
        f"{qname}.{method_name}" for qname in qualified_class_names
        if method_name in classes_by_qname.get(qname, {}).get("methods", ())
    })
    if len(qualified_class_names) == 1 and len(targets) == 1:
        return targets[0], 1.0, []
    if targets:
        return None, 0.5, targets
    return None, None, []
