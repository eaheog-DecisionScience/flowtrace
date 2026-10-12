# Flowtrace

[![PyPI version](https://img.shields.io/pypi/v/flowtrace.svg)](https://pypi.org/project/flowtrace/)
[![Python versions](https://img.shields.io/pypi/pyversions/flowtrace.svg)](https://pypi.org/project/flowtrace/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![MCP Compatible](https://img.shields.io/badge/MCP-Protocol%202.0-blue.svg)](https://modelcontextprotocol.io/)

**Deterministic runtime execution tracing, multi-pass static call graphs, and local MCP server for Python developers and AI pair programmers (Cursor, Claude Desktop, Claude Code).**

---

## Why Flowtrace?

When AI coding assistants (like Cursor, Claude Code, or Copilot) try to diagnose bugs in large codebases, they are forced to guess: they search files with keyword `grep`, read thousands of tokens of unrelated code, and frequently hallucinate call paths or variable values.

**Flowtrace gives AI agents and developers deterministic runtime ground truth:**
- ?? **Silent Mutation Catching (`arg_diff`)**: Detects in-place dictionary/object mutations before an exception is raised.
- ?? **Causal Root-Cause Pinpointing (`why`)**: Distinguishes the true origin frame from propagation frames across deep call stacks.
- ?? **Transitive Blast-Radius Impact (`impact`)**: Knows every caller, API route, and test file affected by touching a function.
- ?? **Function Contract & Usage Extractor (`spec`)**: Extracts docstrings, parameter signatures, and real-world observed runtime schemas.
- ?? **Time-Travel Mock Generator (`mock`)**: Converts recorded executions into standalone `pytest` unit test mocks with zero manual boilerplate.
- ? **N+1 Query Sentry (`perf`)**: Detects unbatched database queries and repetitive network loops in call trees.
- ??? **Autonomous Patch Verifier (`verify-patch`)**: Diffs before-and-after traces to verify that an AI's proposed bug fix resolved errors without regressions.
- ?? **Enterprise-Grade Redaction**: Cryptographic salted SHA-256 fingerprinting guarantees zero leaks of credentials, PII, or tokens.

---

## ? 1-Click MCP Setup for AI Assistants

Install Flowtrace:

```bash
pip install flowtrace
```

### A. Cursor IDE
1. Go to **Cursor Settings** (`Ctrl + ,` or `Cmd + ,`) ? **Features** ? **MCP Servers**.
2. Click **+ Add New MCP Server**.
3. Fill in:
   - **Name**: `flowtrace`
   - **Type**: `command`
   - **Command**: `flowtrace-mcp`

---

### B. Claude Desktop
Add Flowtrace to your `claude_desktop_config.json`:
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "flowtrace": {
      "command": "flowtrace-mcp"
    }
  }
}
```

---

### C. Claude Code (Terminal CLI)
Add Flowtrace with a single command:
```bash
claude mcp add flowtrace -- flowtrace-mcp
```

---

## ?? CLI Capabilities & Showcase

Flowtrace requires **zero source code modifications**. Just run it:

### 1. Runtime Tracing & Argument State Diffing
Run any script or auto-discover a project's entrypoint:
```bash
flowtrace run my_script.py -o trace.json
flowtrace run my_project/               # Auto-discovers entrypoint
```
Flowtrace automatically captures frame execution, exceptions, and **in-place argument mutations (`arg_diff`)**:
```
apply_coupon mutation captured at return:
  Keys ADDED    : ['error_code', 'subtotal', 'items']
  Keys REMOVED  : ['discount_rate']  <-- Caught before crash!
```

---

### 2. Causal Exception Origin Analysis (`why`)
Triage crashes instantly without stepping through 20 frames in `pdb`:
```bash
flowtrace why trace.json
```
```
1 distinct exception(s) found
WHY: KeyError: 'discount_rate'
  origin: calculate_total  (call_id=21, depth=2)
  causal chain:
    process_checkout(user_id, coupon_code, order_id)
      calculate_total(order)  <-- ROOT CAUSE HERE
```

---

### 3. Transitive Blast-Radius Impact Analysis (`impact`)
Know what breaks before modifying a function across 1,000+ files:
```bash
flowtrace impact . "enterprise_pipeline.apply_coupon"
flowtrace impact /path/to/django "db.models.query.QuerySet._filter_or_exclude"
```
```
IMPACT ANALYSIS: db.models.query.QuerySet._filter_or_exclude
Confidence: HIGH
Direct callers (3):
  - db.models.query.QuerySet.complex_filter
  - db.models.query.QuerySet.exclude
  - db.models.query.QuerySet.filter
Transitive callers (6): complex_filter, contains, exclude, filter, get ...
```

---

### 4. Function Contract & Observed Usage Extractor (`spec`)
Extract signatures, type annotations, and real-world observed dictionary schemas:
```bash
flowtrace spec . "enterprise_pipeline.apply_coupon" --trace trace.json
```
```
FUNCTION CONTRACT: enterprise_pipeline.apply_coupon
  Docstring  : Apply coupon to order and calculate discount rate.
  Parameters : order, coupon_code
  Observed Runtime Types:
    - order: dict (keys: coupon, user_id)
    - coupon_code: str
  Usage Recipe:
    result = apply_coupon(order={'user_id': 'user_42', 'coupon': 'SUMMER20'}, coupon_code='SUMMER20')
```

---

### 5. Time-Travel Mock & Fixture Synthesis (`mock`)
Convert any real trace into standalone, ready-to-run `pytest` mocks:
```bash
flowtrace mock trace.json --target process_checkout -o test_mock.py
pytest test_mock.py
```
Automatically patches external databases, network calls, and ORMs with the exact values observed at runtime.

---

### 6. N+1 Query & Loop Performance Sentry (`perf`)
Catch unbatched database queries and repetitive network loops:
```bash
flowtrace perf trace.json
```
```
PERFORMANCE ANALYSIS [CRITICAL]
  [CRITICAL] load_user_dashboard -> db.execute_sql_query
  Count: 15 times | Total: 75.0ms
  Advice: N+1 Database Query pattern detected. Use bulk queries or prefetch/join.
```

---

### 7. Autonomous AI Patch Verification (`verify-patch`)
Verify whether a bug fix resolved the issue without side-effects or regressions:
```bash
flowtrace verify-patch trace_before.json trace_after.json
```
```
AUTONOMOUS PATCH VERIFICATION
  Verdict : SAFE_TO_MERGE
  Summary : All original exceptions eliminated with zero new errors.
            Successfully unblocked 3 downstream function(s): ['Database.save_order', 'validate_auth'].
  Fixed   : ['KeyError']
```

---

## ?? Enterprise Security & Anti-Exfiltration Guarantee

Flowtrace includes an un-bypassable data security layer in [`flowtrace/redaction.py`](flowtrace/redaction.py):

* **Pattern & Entropy Matching**: Automatically detects and masks DB connection URIs (`postgres://user:pass@...`), PEM private keys, AWS tokens, Stripe keys, OpenAI keys, JWTs, credit card numbers, SSNs, and Bearer tokens.
* **Cryptographic Salted Fingerprinting**: Secrets are masked with a deterministic salted SHA-256 fingerprint (`<redacted>:key=token:sha256=40ce58f7`):
  - AI agents and dataflow tools can prove that `fetch_auth()` produced the same token passed into `api_call()`.
  - The raw secret is **mathematically non-reconstructible**.
* **Anti-Prompt-Injection Enforcement**: With `FLOWTRACE_STRICT_PRIVACY=1` (enabled by default), redaction cannot be disabled even if a caller forces `redact_secrets=False`.
* **Audit Trail**: Every redaction is logged to `redaction_audit.log` for SOC-2/HIPAA compliance.

---

## ??? Performance & Incremental Disk Caching

Flowtrace uses an incremental disk cache (`.flowtrace/index_cache.json`) that tracks `(mtime, size)` metadata across every `.py` file:

| Operation | Cold Run | Cached Run | Speedup |
|---|---|---|---|
| Symbol Lookup | 2.10s | **0.0087s** | **250x** |
| Call Graph Navigation | 2.35s | **0.0056s** | **400x** |
| Blast-Radius Impact | 2.60s | **0.0877s** | **30x** |
| Django (906 files) | 1m 41s | **0.0015s** | **1,000x+** |

---

## ?? Contributing & License

Contributions are warmly welcome! Please open an issue or pull request on [GitHub](https://github.com/flowtrace/flowtrace).

Flowtrace is licensed under the [MIT License](LICENSE).
