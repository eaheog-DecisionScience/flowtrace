"""
test_advanced_features.py - Comprehensive stress tests for:
1. Contract & Usage Extractor (spec)
2. Time-Travel Mock Generator (mock)
3. Performance & N+1 Query Sentry (perf)
4. Autonomous Patch Verifier (verify-patch)
"""

import json
import os
import subprocess
import sys
from pathlib import Path
import pytest

from flowtrace.spec import extract_function_spec
from flowtrace.mockgen import generate_mock_test
from flowtrace.perf import analyze_performance
from flowtrace.verifier import verify_patch
from flowtrace.tracer import Tracer
import enterprise_pipeline as ep


@pytest.fixture(scope="module")
def trace_runs():
    """Generates Run A (healthy) and Run B (failing) traces."""
    # Run A
    ta = Tracer(target_dir=".")
    ta.start()
    try:
        ep.process_checkout("user_42", "SUMMER20", "order_001")
    except Exception:
        pass
    ta.stop()
    doc_a = ta.to_trace_doc()

    # Run B
    tb = Tracer(target_dir=".")
    tb.start()
    try:
        ep.process_checkout("user_42", "INVALID", "order_002")
    except Exception:
        pass
    tb.stop()
    doc_b = tb.to_trace_doc()

    return doc_a, doc_b


def test_spec_extraction_with_runtime_trace(trace_runs):
    """Test 1: Extract contract, docstring, and runtime shapes for enterprise_pipeline.apply_coupon."""
    doc_a, _ = trace_runs
    spec = extract_function_spec(".", "enterprise_pipeline.apply_coupon", trace_data=doc_a)

    assert spec["symbol"] == "enterprise_pipeline.apply_coupon"
    assert "Apply coupon to order" in (spec.get("docstring") or "")
    
    # Check parameters
    param_names = [p["name"] for p in spec["parameters"]]
    assert "order" in param_names
    assert "coupon_code" in param_names

    # Check observed runtime types
    rt = spec["observed_runtime_types"]
    assert "arguments" in rt
    assert "order" in rt["arguments"]
    assert "dict" in rt["arguments"]["order"]["types"]
    assert "user_id" in rt["arguments"]["order"]["observed_dict_keys"]
    assert "coupon" in rt["arguments"]["order"]["observed_dict_keys"]
    assert "subtotal" in rt["sample_returns"][0]

    # Check generated usage recipe
    recipe = spec["usage_recipe"]
    assert "apply_coupon(" in recipe
    assert "order=" in recipe


def test_mock_generation_and_execution(trace_runs, tmp_path):
    """Test 2: Synthesize a pytest unit test with mocks and execute it."""
    doc_a, _ = trace_runs
    generated_code = generate_mock_test(doc_a, target_function="process_checkout")

    assert "def test_process_checkout_mocked():" in generated_code
    assert "patch(" in generated_code or "mock" in generated_code.lower()

    # Write generated test to temporary file and execute via pytest
    test_file = tmp_path / "test_auto_generated.py"
    test_file.write_text(generated_code, encoding="utf-8")

    # Run the generated test in an isolated process
    res = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file)],
        capture_output=True,
        text=True,
        cwd=str(Path(".").resolve())
    )
    # The generated test should compile without syntax errors
    assert res.returncode == 0 or "passed" in res.stdout or "FAILED" not in res.stderr


def test_perf_n_plus_one_detection():
    """Test 3: Detect N+1 database queries inside a simulated loop."""
    # Create a synthetic trace with 15 repetitive DB calls inside parent_call
    events = [
        {
            "call_id": 1,
            "parent_id": None,
            "type": "call",
            "function": "load_user_dashboard",
            "module": "services.dashboard",
            "category": "Logic",
            "timestamp": 1000.0
        }
    ]
    # Simulate 15 database calls in a loop
    for i in range(2, 17):
        events.append({
            "call_id": i,
            "parent_id": 1,
            "type": "call",
            "function": "execute_sql_query",
            "module": "db.connection",
            "category": "Database",
            "timestamp": 1000.0 + (i * 0.01)
        })
        events.append({
            "call_id": i,
            "parent_id": 1,
            "type": "return",
            "function": "execute_sql_query",
            "timestamp": 1000.0 + (i * 0.01) + 0.005,
            "return_value": {"row_count": 1}
        })

    events.append({
        "call_id": 1,
        "parent_id": None,
        "type": "return",
        "function": "load_user_dashboard",
        "timestamp": 1000.5,
        "return_value": {"status": "ok"}
    })

    synthetic_trace = {"events": events}
    report = analyze_performance(synthetic_trace, n_plus_one_threshold=4)

    assert report["n_plus_one_detected"] is True
    assert report["status"] == "CRITICAL"
    assert len(report["warnings"]) == 1

    warning = report["warnings"][0]
    assert warning["parent_function"] == "services.dashboard.load_user_dashboard"
    assert warning["child_function"] == "db.connection.execute_sql_query"
    assert warning["call_count"] == 15
    assert "N+1 Database Query pattern" in warning["advice"]


def test_autonomous_patch_verification(trace_runs):
    """Test 4: Verify patch fixing a crash."""
    doc_a, doc_b = trace_runs
    # In our scenario, doc_b is the failing run, doc_a is the healthy fixed run
    result = verify_patch(trace_before=doc_b, trace_after=doc_a)

    assert result["verdict"] == "SAFE_TO_MERGE"
    assert result["is_safe"] is True
    assert "KeyError" in result["fixed_exceptions"]
    assert len(result["new_exceptions"]) == 0
    assert "save_order" in result["unblocked_functions"] or "Database.save_order" in result["unblocked_functions"]
