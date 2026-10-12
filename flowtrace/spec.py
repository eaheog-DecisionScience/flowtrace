import ast
import inspect
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .indexer import build_index


def _extract_type_hint(val: Any) -> str:
    if val is None:
        return "None"
    if isinstance(val, bool):
        return "bool"
    if isinstance(val, int):
        return "int"
    if isinstance(val, float):
        return "float"
    if isinstance(val, str):
        return "str"
    if isinstance(val, bytes):
        return "bytes"
    if isinstance(val, list):
        if val:
            inner = _extract_type_hint(val[0])
            return f"list[{inner}]"
        return "list"
    if isinstance(val, dict):
        return "dict"
    return type(val).__name__


def _summarize_dict_schema(d: dict) -> Dict[str, str]:
    schema = {}
    for k, v in d.items():
        schema[str(k)] = _extract_type_hint(v)
    return schema


def extract_function_spec(project_dir: str, symbol: str, trace_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Extracts static contract, docstring, annotations, runtime schema, and code examples for a function."""
    root = Path(project_dir).resolve()
    
    # 1. Attempt static analysis from AST index
    func_ast = None
    file_path = None
    docstring = ""
    ast_params = []
    return_annotation = None
    
    try:
        index = build_index(root)
        target_name = symbol.split(".")[-1]
        
        for fn in index.get("functions", []):
            qname = fn.get("qualified_name", "")
            fname = fn.get("name", "")
            if symbol == qname or symbol == fname or qname.endswith("." + symbol) or symbol.endswith("." + fname):
                file_rel = fn.get("file")
                file_path = str(root / file_rel) if file_rel else None
                break
        
        if file_path and Path(file_path).exists():
            code = Path(file_path).read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(code, filename=file_path)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == target_name:
                    func_ast = node
                    docstring = ast.get_docstring(node) or ""
                    if node.returns:
                        return_annotation = ast.unparse(node.returns)
                    
                    # Parse args
                    args = node.args
                    posonly = getattr(args, "posonlyargs", [])
                    reg_args = args.args
                    all_pos = posonly + reg_args
                    num_defaults = len(args.defaults)
                    
                    for i, arg in enumerate(all_pos):
                        default_val = None
                        default_idx = i - (len(all_pos) - num_defaults)
                        if default_idx >= 0:
                            default_val = ast.unparse(args.defaults[default_idx])
                        ann = ast.unparse(arg.annotation) if arg.annotation else None
                        kind = "positional_only" if i < len(posonly) else "positional_or_keyword"
                        ast_params.append({
                            "name": arg.arg,
                            "annotation": ann,
                            "default": default_val,
                            "kind": kind
                        })
                    
                    if args.vararg:
                        ann = ast.unparse(args.vararg.annotation) if args.vararg.annotation else None
                        ast_params.append({
                            "name": args.vararg.arg,
                            "annotation": ann,
                            "default": None,
                            "kind": "var_positional"
                        })
                        
                    kwonly = getattr(args, "kwonlyargs", [])
                    kw_defaults = getattr(args, "kw_defaults", [])
                    for i, arg in enumerate(kwonly):
                        default_val = None
                        if i < len(kw_defaults) and kw_defaults[i] is not None:
                            default_val = ast.unparse(kw_defaults[i])
                        ann = ast.unparse(arg.annotation) if arg.annotation else None
                        ast_params.append({
                            "name": arg.arg,
                            "annotation": ann,
                            "default": default_val,
                            "kind": "keyword_only"
                        })
                        
                    if args.kwarg:
                        ann = ast.unparse(args.kwarg.annotation) if args.kwarg.annotation else None
                        ast_params.append({
                            "name": args.kwarg.arg,
                            "annotation": ann,
                            "default": None,
                            "kind": "var_keyword"
                        })
                    break
    except Exception:
        pass
        
    # 2. Dynamic inspect fallback if not found in AST
    if not ast_params:
        try:
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            parts = symbol.split(".")
            mod = None
            obj = None
            for i in range(len(parts), 0, -1):
                mod_name = ".".join(parts[:i])
                try:
                    mod = importlib.import_module(mod_name)
                    obj = mod
                    for attr in parts[i:]:
                        obj = getattr(obj, attr)
                    break
                except (ImportError, AttributeError):
                    continue
            
            if obj and callable(obj):
                docstring = inspect.getdoc(obj) or ""
                sig = inspect.signature(obj)
                for p in sig.parameters.values():
                    ann = None if p.annotation is inspect.Parameter.empty else str(p.annotation)
                    def_val = None if p.default is inspect.Parameter.empty else repr(p.default)
                    ast_params.append({
                        "name": p.name,
                        "annotation": ann,
                        "default": def_val,
                        "kind": str(p.kind)
                    })
                if sig.return_annotation is not inspect.Signature.empty:
                    return_annotation = str(sig.return_annotation)
        except Exception:
            pass

    # 3. Dynamic runtime trace integration (if trace provided)
    runtime_samples = []
    observed_return_types = set()
    observed_arg_shapes = {}
    
    if trace_data and "events" in trace_data:
        target_short = symbol.split(".")[-1]
        events = trace_data["events"]
        calls = {e.get("call_id"): e for e in events if e.get("type") == "call"}
        
        for ev in events:
            ev_fn = ev.get("function", "")
            if ev_fn == target_short or ev_fn == symbol or ev_fn.endswith("." + target_short):
                if ev.get("type") == "call":
                    args = ev.get("args", {})
                    for k, v in args.items():
                        if k not in observed_arg_shapes:
                            observed_arg_shapes[k] = {"types": set(), "dict_keys": set(), "samples": []}
                        t_name = _extract_type_hint(v)
                        observed_arg_shapes[k]["types"].add(t_name)
                        if isinstance(v, dict):
                            observed_arg_shapes[k]["dict_keys"].update(v.keys())
                        if len(observed_arg_shapes[k]["samples"]) < 2:
                            observed_arg_shapes[k]["samples"].append(v)
                            
                elif ev.get("type") == "return":
                    ret = ev.get("return_value")
                    observed_return_types.add(_extract_type_hint(ret))
                    if len(runtime_samples) < 2 and ret is not None:
                        runtime_samples.append(ret)

    # 4. Synthesize verified usage example
    sample_call_args = []
    for p in ast_params:
        p_name = p["name"]
        if p_name in ("self", "cls"):
            continue
        # Use runtime sample if available
        if p_name in observed_arg_shapes and observed_arg_shapes[p_name]["samples"]:
            val_str = repr(observed_arg_shapes[p_name]["samples"][0])
        elif p.get("default") is not None:
            val_str = p['default']
        elif p.get("annotation") in ("int", "float"):
            val_str = "100"
        elif p.get("annotation") == "str":
            val_str = "'example'"
        elif p.get("annotation") == "dict":
            val_str = "{'key': 'value'}"
        else:
            val_str = "..."

        if p.get("kind") == "positional_only":
            sample_call_args.append(val_str)
        elif p.get("kind") in ("var_positional", "var_keyword"):
            continue
        else:
            sample_call_args.append(f"{p_name}={val_str}")

    fn_call_name = symbol.split(".")[-1]
    usage_snippet = f"result = {fn_call_name}({', '.join(sample_call_args)})"

    # Convert sets to lists for JSON serialization
    serialized_observed = {}
    for k, v in observed_arg_shapes.items():
        serialized_observed[k] = {
            "types": sorted(list(v["types"])),
            "observed_dict_keys": sorted(list(v["dict_keys"])),
            "sample": v["samples"][0] if v["samples"] else None
        }

    return {
        "symbol": symbol,
        "file": file_path,
        "docstring": docstring.strip() if docstring else None,
        "parameters": ast_params,
        "return_annotation": return_annotation,
        "observed_runtime_types": {
            "arguments": serialized_observed,
            "return_types": sorted(list(observed_return_types)),
            "sample_returns": runtime_samples[:2]
        },
        "usage_recipe": usage_snippet
    }
