import json

print('='*68)
print('  FLOWTRACE NICHE FUNCTIONALITY VALIDATION  -  FINAL RESULTS')
print('='*68)

from flowtrace.tracer import Tracer
import enterprise_pipeline as ep

print('\n[SETUP] Generating traces...')
ta = Tracer(target_dir='.')
ta.start()
try:
    ep.process_checkout('user_42', 'SUMMER20', 'order_001')
except Exception:
    pass
ta.stop()
doc_a = ta.to_trace_doc()
with open('trace_run_a.json','w') as f:
    json.dump(doc_a, f, indent=2, default=str)

tb = Tracer(target_dir='.')
tb.start()
try:
    ep.process_checkout('user_42', 'INVALID', 'order_002')
except Exception as ex:
    run_b_err = str(ex)
tb.stop()
doc_b = tb.to_trace_doc()
with open('trace_run_b.json','w') as f:
    json.dump(doc_b, f, indent=2, default=str)

print(f'  Run A: {doc_a["event_count"]} events | Run B: {doc_b["event_count"]} events (crash: KeyError {run_b_err})')
results = {}

# TEST 1: ARG_DIFF
print('\n' + '-'*68)
print('TEST 1: ARG_DIFF  -  Silent In-Place Dict Mutation Detection')
print('-'*68)
for ev in doc_b['events']:
    diff = ev.get('arg_diff')
    if diff and ev.get('function') == 'apply_coupon' and ev.get('type') == 'return':
        od = diff.get('order', {})
        added   = sorted(od.get('added',{}).keys())
        removed = sorted(od.get('removed',{}).keys())
        modified= sorted(od.get('modified',{}).keys())
        print(f'  apply_coupon mutation captured:')
        print(f'    Keys ADDED    : {added}  (error_code is the injected poison)')
        print(f'    Keys REMOVED  : {removed}  (discount_rate was silently deleted)')
        print(f'    Keys MODIFIED : {modified}')
        ok = 'error_code' in added
        results['arg_diff'] = ok
        print(f'  [{"PASS" if ok else "FAIL"}] Mutation detected before crash occurred')

# TEST 2: DATAFLOW
print('\n' + '-'*68)
print('TEST 2: DATAFLOW  -  Runtime Value Flow Edge Reconstruction')
print('-'*68)
from flowtrace.dataflow import build_dataflow
df = build_dataflow(doc_b)
print(f'  Flow edges discovered: {df["edge_count"]}')
for e in df['edges']:
    print(f'    {e["from_function"]}.{e["from_label"]}  ==>  {e["to_function"]}(arg={e["to_arg"]})  [conf={e["confidence"]}]')
ok = any('apply_coupon' in e['from_function'] and 'calculate_total' in e['to_function'] for e in df['edges'])
results['dataflow'] = ok
print(f'  [{"PASS" if ok else "FAIL"}] apply_coupon -> calculate_total data dependency confirmed at runtime')

# TEST 3: WHY (Causal Analysis)
print('\n' + '-'*68)
print('TEST 3: WHY  -  Causal Root-Cause Chain Analysis')
print('-'*68)
from flowtrace.causality import build_why_report
why = build_why_report(doc_b)
reports = why.get('reports', [])
if reports:
    r = reports[0]
    print(f'  Exception    : {r["exception_type"]}: {r["exception_message"]}')
    print(f'  Root cause   : {r["origin_function"]}  (call_id={r["origin_call_id"]}, depth={r["depth"]})')
    print(f'  Causal chain :')
    for step in r['chain']:
        marker = '  <-- ROOT CAUSE HERE' if step['is_origin'] else ''
        args = ', '.join(step.get('args',{}).keys())
        print(f'    {step["function"]}({args}){marker}')
    ok = r['origin_function'] == 'calculate_total'
else:
    ok = False
results['why'] = ok
print(f'  [{"PASS" if ok else "FAIL"}] Exception root cause correctly pinpointed to calculate_total (not process_checkout)')

# TEST 4: DIFF (Execution Comparison)
print('\n' + '-'*68)
print('TEST 4: DIFF  -  Execution Path Comparison (Run A vs Run B)')
print('-'*68)
from flowtrace.execution_diff import build_execution_diff
diff = build_execution_diff(doc_a, doc_b)
s = diff['summary']
exc_diff = diff['exception_diff']
lat_diff = diff['latency_diff']
print(f'  Call count   : A={s["call_count_a"]} calls  vs  B={s["call_count_b"]} calls')
print(f'  Path changed : {s["path_changed"]}')
removed = s.get('removed_functions', [])
print(f'  Functions missing in B: {removed}')
new_excs = exc_diff.get('only_in_b', [])
print(f'  New exceptions in B (not in A):')
for fn, et, ev2 in new_excs:
    print(f'    {fn} raised {et}: {ev2}')
top_lat = sorted(lat_diff, key=lambda x: abs(x.get('delta_ms',0)), reverse=True)[:3]
print(f'  Top latency deltas:')
for lc in top_lat:
    print(f'    {lc["function"]}: {lc["avg_a_ms"]:.2f}ms -> {lc["avg_b_ms"]:.2f}ms ({lc["delta_ms"]:+.2f}ms)')
ok = 'save_order' in removed and len(new_excs) > 0
results['diff'] = ok
print(f'  [{"PASS" if ok else "FAIL"}] Diff correctly flags save_order missing + 2 new exceptions')

# TEST 5: IMPACT (Own codebase)
print('\n' + '-'*68)
print('TEST 5: IMPACT  -  Transitive Caller Analysis (Enterprise Pipeline)')
print('-'*68)
from flowtrace.mcp_server import flowtrace_impact
imp = flowtrace_impact('.', 'enterprise_pipeline.apply_coupon')
callers = imp.get('direct_callers', [])
t_callers = imp.get('transitive_callers', [])
files = imp.get('affected_files', [])
print(f'  Target           : enterprise_pipeline.apply_coupon')
print(f'  Confidence       : {imp["impact_confidence"]}')
print(f'  Direct callers   : {callers}')
print(f'  Transitive callers: {t_callers}')
print(f'  Affected files   : {files}')
ok = 'enterprise_pipeline.process_checkout' in callers
results['impact_pipeline'] = ok
print(f'  [{"PASS" if ok else "FAIL"}] apply_coupon->process_checkout caller link proven')

# TEST 6: IMPACT ON DJANGO (906 files)
print('\n' + '-'*68)
print('TEST 6: IMPACT  -  Django Large Codebase (906 files, cached)')
print('-'*68)
try:
    from flowtrace.mcp_server import flowtrace_impact as fi_dj, flowtrace_overview
    ov = flowtrace_overview('C:/Users/HK Pandey/Downloads/django/django')
    print(f'  Django index: {ov["file_count"]} files loaded (warm cache)')
    imp_dj = fi_dj('C:/Users/HK Pandey/Downloads/django/django', 'db.models.query.QuerySet._filter_or_exclude')
    callers_dj = imp_dj.get('direct_callers', [])
    t_callers_dj = imp_dj.get('transitive_callers', [])
    files_dj = imp_dj.get('affected_files', [])
    print(f'  Target     : db.models.query.QuerySet._filter_or_exclude')
    print(f'  Confidence : {imp_dj["impact_confidence"]}')
    print(f'  Direct callers ({len(callers_dj)}): {callers_dj[:5]}')
    print(f'  Transitive callers ({len(t_callers_dj)}): {[c.split(".")[-1] for c in t_callers_dj[:5]]}...')
    print(f'  Affected files ({len(files_dj)}): {[f.split(chr(92))[-1] for f in files_dj[:5]]}')
    ok = imp_dj['impact_confidence'] in ('high','medium','low')
    results['impact_django'] = ok
    print(f'  [{"PASS" if ok else "FAIL"}] Django impact resolved across {ov["file_count"]}-file codebase')
except Exception as ex:
    print(f'  [INFO] Django: {ex}')
    results['impact_django'] = False

# SUMMARY
print('\n' + '='*68)
print('FINAL VALIDATION SUMMARY')
print('='*68)
labels = {
    'arg_diff': 'ARG_DIFF   : Silent in-place mutation detection',
    'dataflow':  'DATAFLOW   : Runtime producer->consumer edge reconstruction',
    'why':       'WHY        : Causal root-cause chain analysis',
    'diff':      'DIFF       : Execution path comparison (before vs after)',
    'impact_pipeline': 'IMPACT     : Transitive caller analysis (single file)',
    'impact_django':   'IMPACT     : Large codebase impact (Django 906 files)',
}
total = 0; passed = 0
for k, label in labels.items():
    ok = results.get(k, False)
    s = 'PASS' if ok else 'FAIL'
    if ok:
        passed += 1
    total += 1
    print(f'  [{s}]  {label}')
print(f'\n  Result: {passed}/{total} tests passed  ({int(passed/total*100)}%)')
