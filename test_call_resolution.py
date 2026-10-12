"""Call-resolution benchmark suite (per the phased inference roadmap):
each fixture under fixtures/ is a tiny, hand-written program with a known
expected resolution outcome. Every future change to callgraph.py or
inference.py must keep these passing - this is what "freeze the
benchmark" means in practice.

Fixtures 1-5 are exactly Phase 1's target patterns and must PROVE their
target. Fixtures 6-10 document current, deliberate gaps/limitations
(function-return inference, parameters, flow-order sensitivity) - they
assert the CURRENT conservative behavior, not a wishlist, so a future
phase extending coverage should update these deliberately rather than
accidentally regress something else while doing so.
"""

from pathlib import Path

import pytest

from flowtrace.callgraph import build_call_graph
from flowtrace.indexer import build_index

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _resolve(fixture_dir):
    index_doc = build_index(fixture_dir)
    return build_call_graph(index_doc, fixture_dir)


def _find_edge(edges, callee_expr, caller_suffix=None):
    matches = [e for e in edges if e["callee_expr"] == callee_expr
               and (caller_suffix is None or e["caller"].endswith(caller_suffix))]
    assert matches, f"no call edge found for expr={callee_expr!r} caller_suffix={caller_suffix!r} among {[(e['caller'], e['callee_expr']) for e in edges]}"
    return matches[0]


class TestPhase1TargetPatterns:
    """Fixtures 1-5: must PROVE their target (confidence=1.0)."""

    def test_01_direct_assignment(self):
        edges = _resolve(FIXTURES_DIR / "01_direct_assignment")
        e = _find_edge(edges, "session.get")
        assert e["callee"] == "fixtures.01_direct_assignment.Session.get".replace("fixtures.", "") or e["callee"].endswith("Session.get")
        assert e["confidence"] == 1.0

    def test_02_imported_constructor(self):
        edges = _resolve(FIXTURES_DIR / "02_imported_constructor")
        e = _find_edge(edges, "session.get")
        assert e["callee"].endswith("Session.get")
        assert e["confidence"] == 1.0

    def test_03_module_constructor(self):
        edges = _resolve(FIXTURES_DIR / "03_module_constructor")
        e = _find_edge(edges, "session.get")
        assert e["callee"].endswith("Session.get")
        assert e["confidence"] == 1.0

    def test_04_alias_assignment(self):
        edges = _resolve(FIXTURES_DIR / "04_alias_assignment")
        e = _find_edge(edges, "client.get")
        assert e["callee"].endswith("Session.get")
        assert e["confidence"] == 1.0

    def test_05_self_attribute(self):
        edges = _resolve(FIXTURES_DIR / "05_self_attribute")
        e = _find_edge(edges, "self.session.get")
        assert e["callee"].endswith("Session.get")
        assert e["confidence"] == 1.0

    def test_11_with_statement(self):
        """The exact real-world pattern found in psf/requests' api.py
        (`with sessions.Session() as session: session.request(...)`) that
        connects requests.get() to Session.request() - missed entirely
        before this fixture was added, since with-bindings aren't Assign
        nodes."""
        edges = _resolve(FIXTURES_DIR / "11_with_statement")
        e = _find_edge(edges, "session.request")
        assert e["callee"].endswith("Session.request")
        assert e["confidence"] == 1.0

    def test_12_reexport(self):
        """The exact real-world pattern found in requests/__init__.py:
        `from .api import get` re-exports the function so consumers see
        `requests.get(...)`. Both the direct call (pkg.get) and the
        constructor-then-method chain (pkg.Session().get()) must resolve
        through the re-export, not just the module's own directly-defined
        symbols."""
        edges = _resolve(FIXTURES_DIR / "12_reexport")
        e1 = _find_edge(edges, "pkg.get")
        assert e1["callee"].endswith("pkg.impl.get")
        assert e1["confidence"] == 1.0

        e2 = _find_edge(edges, "session.get")
        assert e2["callee"].endswith("pkg.impl.Session.get")
        assert e2["confidence"] == 1.0

    def test_13_wildcard_reexport(self):
        """The exact real-world pattern found in encode/httpx's
        __init__.py: `from ._client import *` re-exports everything
        public in _client.py. httpx uses this for its ENTIRE public API,
        once per submodule - explains why httpx's resolution rate was
        much lower than click/rich's until this fixture drove the fix."""
        edges = _resolve(FIXTURES_DIR / "13_wildcard_reexport")
        e = _find_edge(edges, "client.get")
        assert e["callee"].endswith("pkg._client.Client.get")
        assert e["confidence"] == 1.0


class TestPhase2TargetPatterns:
    """Fixtures 6, 8, 14, 15: Phase 2's target patterns - function-return
    inference, parameter type propagation, classmethod/staticmethod calls,
    and super(). Must PROVE their target, same discipline as Phase 1."""

    def test_06_function_return_now_resolved(self):
        """Was in TestDocumentedGaps under Phase 1 (asserted this must
        NOT resolve yet) - now Phase 2's target. make_session()'s single
        `return Session()` gives it a provable return type, so `session =
        make_session()` can be typed the same as a direct constructor."""
        edges = _resolve(FIXTURES_DIR / "06_function_return")
        e = _find_edge(edges, "session.get")
        assert e["callee"].endswith("Session.get")
        assert e["confidence"] == 1.0

    def test_08_parameter_now_resolved(self):
        """Was in TestDocumentedGaps under Phase 1 - now Phase 2's target.
        `handle`'s only call site passes a direct `Session()` constructor
        for the `session` parameter, so that type is usable inside
        handle's own body."""
        edges = _resolve(FIXTURES_DIR / "08_parameter")
        e = _find_edge(edges, "session.get")
        assert e["callee"].endswith("Session.get")
        assert e["confidence"] == 1.0

    def test_14_classmethod_on_class(self):
        edges = _resolve(FIXTURES_DIR / "14_classmethod")
        e = _find_edge(edges, "Session.default")
        assert e["callee"].endswith("Session.default")
        assert e["confidence"] == 1.0

    def test_15_super_call(self):
        edges = _resolve(FIXTURES_DIR / "15_super_call")
        e = _find_edge(edges, "super().greet")
        assert e["callee"].endswith("Base.greet")
        assert e["confidence"] == 1.0


class TestPhase2NeverGuesses:
    """Parameter type propagation must apply the exact same discipline as
    Phase 1: disagreement across call sites means POSSIBLE, never a
    silent pick of one candidate."""

    def test_16_ambiguous_parameter_across_call_sites(self):
        edges = _resolve(FIXTURES_DIR / "16_ambiguous_parameter")
        e = _find_edge(edges, "obj.get")
        assert e["callee"] is None, "handle() is called with both Session and Cache - must not silently resolve to one"
        assert e["confidence"] == 0.5
        assert len(e["possible_callees"]) == 2
        assert any(p.endswith("Session.get") for p in e["possible_callees"])
        assert any(p.endswith("Cache.get") for p in e["possible_callees"])


class TestDocumentedGaps:
    """Fixtures 7, 9, 10: current, deliberate limitations. These assert
    the CONSERVATIVE behavior (unresolved or possible-not-proven), not a
    wishlist - they exist so a future phase changes them on purpose.
    (Fixtures 6 and 8 moved to TestPhase2TargetPatterns once function-return
    inference and parameter type propagation started resolving them.)"""

    def test_07_conditional_assignment_is_possible_not_proven(self):
        edges = _resolve(FIXTURES_DIR / "07_conditional_assignment")
        e = _find_edge(edges, "s.get")
        assert e["callee"] is None, "must never silently guess between Session.get and RedisClient.get"
        assert e["confidence"] == 0.5
        assert len(e["possible_callees"]) == 2
        assert any(p.endswith("Session.get") for p in e["possible_callees"])
        assert any(p.endswith("RedisClient.get") for p in e["possible_callees"])

    def test_09_nested_scope_is_independent(self):
        edges = _resolve(FIXTURES_DIR / "09_nested_scope")
        e = _find_edge(edges, "s.get")
        assert e["callee"] is not None, "inner scope's own assignment should resolve"
        assert e["callee"].endswith("Session.get"), "inner `s` must resolve to Session (its own scope), not Other (outer scope)"
        assert e["confidence"] == 1.0

    def test_10_shadowing_is_conservatively_ambiguous(self):
        edges = _resolve(FIXTURES_DIR / "10_shadowing")
        gets = [e for e in edges if e["callee_expr"] == "s.get"]
        assert len(gets) == 2
        for e in gets:
            assert e["callee"] is None, (
                "Phase 1 is not flow-order-sensitive: a variable reassigned to a "
                "different type mid-function is treated as ambiguous at every use, "
                "even where a flow-sensitive analysis could resolve it precisely"
            )
            assert e["confidence"] == 0.5
            assert len(e["possible_callees"]) == 2


class TestNeverSilentlyWrong:
    """The property that matters most: an inferred resolution must never
    disagree with what the code actually does, even as fixtures evolve."""

    def test_all_phase1_fixtures_produce_no_false_positives(self):
        """For every fixture, every resolved (confidence=1.0) edge's
        target must be a method that actually exists on a class the
        variable could actually hold - i.e. resolve_via_type's own
        internal consistency, exercised end-to-end through the real
        indexer + callgraph pipeline, not just unit-tested in isolation."""
        for fixture in sorted(d for d in FIXTURES_DIR.iterdir() if d.is_dir()):
            edges = _resolve(fixture)
            index_doc = build_index(fixture)
            methods_by_class = {c["qualified_name"]: set(c["methods"]) for c in index_doc["classes"]}
            for e in edges:
                if e["callee"] and "." in e["callee"]:
                    cls_qname, method = e["callee"].rsplit(".", 1)
                    if cls_qname in methods_by_class:
                        assert method in methods_by_class[cls_qname], (
                            f"{fixture}: resolved {e['callee']} but {method} isn't actually "
                            f"a method of {cls_qname} - false positive resolution!"
                        )
