"""Tracer Core (Language Agent) - Python implementation.

Hooks into runtime execution via sys.settrace() to capture function
entrance/exit events, positional/keyword arguments, return values, and
call-stack depth, with minimal overhead achieved by:
  - only registering a per-frame local trace function for frames we
    actually want to record (namespace/depth filtered at 'call' time)
  - never subscribing to 'line' events
  - bounding serialized payload size/depth (see serializer.py)
"""

import inspect
import itertools
import os
import sys
import threading
import time
from pathlib import Path

from .classifier import classify_module
from .serializer import diff_states, safe_serialize

_STDLIB_DIR = str(Path(os.__file__).parent.resolve())
_PACKAGE_DIR = str(Path(__file__).parent.resolve())
_call_id_counter = itertools.count(1)


class Tracer:
    def __init__(self, include_stdlib=False, max_depth=None, target_dir=None,
                 exclude_prefixes=None, redact_secrets=True):
        self.include_stdlib = include_stdlib
        self.max_depth = max_depth
        self.target_dir = str(Path(target_dir).resolve()) if target_dir else str(Path.cwd().resolve())
        self.exclude_prefixes = tuple(exclude_prefixes or ())
        self.redact_secrets = redact_secrets
        self.events = []
        self._stack = []  # list of (call_id, arg_snapshot_dict)
        self._depth = 0
        self._start_time = None

    # -- filtering -----------------------------------------------------

    def _is_stdlib_or_vendor(self, filename):
        try:
            resolved = str(Path(filename).resolve())
        except Exception:
            return True
        if "site-packages" in resolved or "dist-packages" in resolved:
            return True
        if resolved.startswith(_STDLIB_DIR):
            return True
        return False

    def _should_trace(self, frame):
        filename = frame.f_code.co_filename
        if filename.startswith("<"):
            return False  # exec/eval/repl frames

        try:
            resolved = str(Path(filename).resolve())
        except Exception:
            return False

        if resolved.startswith(_PACKAGE_DIR):
            return False  # never trace the tracer's own internals

        module = frame.f_globals.get("__name__", "")
        if self.exclude_prefixes and module.startswith(self.exclude_prefixes):
            return False

        is_vendor = self._is_stdlib_or_vendor(filename)
        if is_vendor and not self.include_stdlib:
            return False

        # By default, scope to the target application directory so that
        # imported third-party packages outside it stay filtered out even
        # when they don't live under a recognisable stdlib/site-packages path.
        if not resolved.startswith(self.target_dir) and not self.include_stdlib:
            return False

        if self.max_depth is not None and self._depth >= self.max_depth:
            return False

        return True

    # -- sys.settrace callbacks -----------------------------------------

    def _global_trace(self, frame, event, arg):
        # sys.settrace only re-invokes the *local* trace function for
        # 'line'/'return'/'exception' events - the initial 'call' event is
        # only ever seen here, so we must record it before handing off.
        if event != "call":
            return None
        if not self._should_trace(frame):
            return None
        self._on_call(frame)
        return self._local_trace

    def _local_trace(self, frame, event, arg):
        if event == "return":
            self._on_return(frame, arg)
        elif event == "exception":
            self._on_exception(frame, arg)
        return self._local_trace

    def _capture_args(self, frame):
        info = inspect.getargvalues(frame)
        args = {}
        for name in info.args:
            args[name] = safe_serialize(info.locals.get(name), key=name, redact_secrets=self.redact_secrets)
        if info.varargs:
            args[f"*{info.varargs}"] = safe_serialize(info.locals.get(info.varargs), key=info.varargs, redact_secrets=self.redact_secrets)
        if info.keywords:
            args[f"**{info.keywords}"] = safe_serialize(info.locals.get(info.keywords), key=info.keywords, redact_secrets=self.redact_secrets)
        return args

    def _on_call(self, frame):
        code = frame.f_code
        module = frame.f_globals.get("__name__", "")
        args = self._capture_args(frame)

        call_id = next(_call_id_counter)
        parent_id = self._stack[-1][0] if self._stack else None
        self._depth += 1

        self.events.append({
            "call_id": call_id,
            "parent_id": parent_id,
            "type": "call",
            "function": code.co_qualname if hasattr(code, "co_qualname") else code.co_name,
            "module": module,
            "category": classify_module(module),
            "file": code.co_filename,
            "line": frame.f_lineno,
            "depth": self._depth,
            "args": args,
            "timestamp": time.time(),
        })
        self._stack.append((call_id, args))

    def _on_return(self, frame, retval):
        if not self._stack:
            return
        call_id, arg_snapshot = self._stack.pop()
        code = frame.f_code

        after_args = self._capture_args(frame)
        arg_diff = {}
        for name, before_val in arg_snapshot.items():
            after_val = after_args.get(name)
            delta = diff_states(before_val, after_val)
            if delta:
                arg_diff[name] = delta

        self.events.append({
            "call_id": call_id,
            "type": "return",
            "function": code.co_qualname if hasattr(code, "co_qualname") else code.co_name,
            "return_value": safe_serialize(retval, redact_secrets=self.redact_secrets),
            "arg_diff": arg_diff,
            "depth": self._depth,
            "timestamp": time.time(),
        })
        self._depth = max(0, self._depth - 1)

    def _on_exception(self, frame, exc_info):
        exc_type, exc_value, _tb = exc_info
        call_id = self._stack[-1][0] if self._stack else None
        self.events.append({
            "call_id": call_id,
            "type": "exception",
            "function": frame.f_code.co_name,
            "exception_type": exc_type.__name__,
            "exception_message": str(exc_value)[:500],
            "depth": self._depth,
            "timestamp": time.time(),
        })

    # -- HTTP monkey patching --------------------------------------------

    def _patch_http_libraries(self):
        self._original_urlopen = None
        self._original_urllib3_urlopen = None
        
        # Patch stdlib urllib
        try:
            import urllib.request
            self._original_urlopen = urllib.request.urlopen
            
            def patched_urlopen(url, *args, **kwargs):
                if isinstance(url, str):
                    url = urllib.request.Request(url)
                
                # Inject traceparent header
                call_id = self._stack[-1][0] if self._stack else 0
                trace_id = "00000000000000000000000000000000"
                span_id = f"{call_id:016x}"
                url.add_header("traceparent", f"00-{trace_id}-{span_id}-01")
                
                return self._original_urlopen(url, *args, **kwargs)
                
            urllib.request.urlopen = patched_urlopen
        except ImportError:
            pass

        # Patch urllib3 (used by requests)
        try:
            import urllib3.connectionpool
            self._original_urllib3_urlopen = urllib3.connectionpool.HTTPConnectionPool.urlopen
            
            def patched_urllib3_urlopen(pool_self, method, url, body=None, headers=None, *args, **kwargs):
                headers = headers or {}
                call_id = self._stack[-1][0] if self._stack else 0
                trace_id = "00000000000000000000000000000000"
                span_id = f"{call_id:016x}"
                headers["traceparent"] = f"00-{trace_id}-{span_id}-01"
                
                return self._original_urllib3_urlopen(pool_self, method, url, body=body, headers=headers, *args, **kwargs)
                
            urllib3.connectionpool.HTTPConnectionPool.urlopen = patched_urllib3_urlopen
        except ImportError:
            pass

    def _unpatch_http_libraries(self):
        if self._original_urlopen:
            import urllib.request
            urllib.request.urlopen = self._original_urlopen
        if self._original_urllib3_urlopen:
            import urllib3.connectionpool
            urllib3.connectionpool.HTTPConnectionPool.urlopen = self._original_urllib3_urlopen

    # -- lifecycle -------------------------------------------------------

    def start(self):
        self._start_time = time.time()
        self._patch_http_libraries()
        sys.settrace(self._global_trace)
        threading.settrace(self._global_trace)

    def stop(self):
        sys.settrace(None)
        threading.settrace(None)
        self._unpatch_http_libraries()

    def to_trace_doc(self):
        return {
            "schema_version": "0.1.0",
            "generated_at": self._start_time,
            "root_dir": self.target_dir,
            "event_count": len(self.events),
            "events": self.events,
        }
