"""flowtrace - zero-config execution tracer (Tracer Core / Language Agent module).

Hooks into Python's runtime via sys.settrace() to capture function call/return
events, argument and return-value payloads, and mutable-argument state diffs,
then emits a normalised JSON execution graph consumable by the Data Engine
and Frontend UI layers described in the FlowTrace spec.
"""

from .tracer import Tracer
from .classifier import classify_module
from .serializer import safe_serialize, diff_states

__all__ = ["Tracer", "classify_module", "safe_serialize", "diff_states"]
__version__ = "0.1.0"
