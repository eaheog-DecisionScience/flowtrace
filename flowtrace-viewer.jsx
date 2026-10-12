import React, { useState, useMemo, useRef, useEffect, useCallback } from "react";
import * as d3 from "d3";
import {
  Play, Pause, SkipBack, SkipForward, ChevronRight, ChevronDown,
  Search, Download, Network, Database, LayoutGrid, Cpu, Package,
  AlertTriangle, ZoomIn, ZoomOut, Maximize2, GitBranch, List, Upload,
} from "lucide-react";

/* ─────────────────────────────────────────────────────────────────────────
   DESIGN TOKENS
   Subject: an execution tracer / logic-analyzer for code. The signature
   idea is literal: calls are "signal traces" on a phosphor scope, amber
   on near-black, with a faint scanline field behind the canvas.
   ───────────────────────────────────────────────────────────────────── */
const T = {
  bgVoid: "#0B0E14",
  bgPanel: "#141924",
  bgPanelRaised: "#1B2230",
  hairline: "#262E3D",
  ink: "#E8E6DE",
  inkDim: "#8B93A7",
  accent: "#FFB84D", // phosphor amber - the signature color
  danger: "#FF5C5C",
  mono: "'IBM Plex Mono', 'SFMono-Regular', Menlo, monospace",
  sans: "'IBM Plex Sans', -apple-system, sans-serif",
};

const CATEGORY_META = {
  Network: { color: "#5EEAD4", icon: Network },
  Database: { color: "#B794F6", icon: Database },
  UI: { color: "#FF7FA6", icon: LayoutGrid },
  Logic: { color: "#9CCFA0", icon: Cpu },
  Stdlib: { color: "#6B7280", icon: Package },
};
const CATEGORIES = Object.keys(CATEGORY_META);

/* ─────────────────────────────────────────────────────────────────────────
   SAMPLE DATA — a hand-curated trace (schema v0.1.0) that exercises every
   category + a mutated argument + a caught exception, so the UI has
   something illustrative to show on load.
   ───────────────────────────────────────────────────────────────────── */
const DEMO_TRACE = {
  schema_version: "0.1.0",
  generated_at: 1786011825.7,
  root_dir: "/app",
  events: [
    { call_id: 1, parent_id: null, type: "call", function: "handle_signup_request", module: "__main__", category: "Logic", file: "app.py", line: 12, depth: 1, args: { user_id: 42 }, timestamp: 0.000 },
    { call_id: 2, parent_id: 1, type: "call", function: "get", module: "requests.api", category: "Network", file: "requests/api.py", line: 73, depth: 2, args: { url: "https://api.example.com/users/42" }, timestamp: 0.004 },
    { call_id: 2, type: "return", function: "get", return_value: { status_code: 200, __type__: "Response" }, arg_diff: {}, depth: 2, timestamp: 0.041 },
    { call_id: 3, parent_id: 1, type: "call", function: "enrich_user", module: "__main__", category: "Logic", file: "app.py", line: 24, depth: 2, args: { user: { id: 42, name: "Ada", roles: ["admin"] } }, timestamp: 0.042 },
    { call_id: 3, type: "return", function: "enrich_user", return_value: { id: 42, name: "Ada", roles: ["admin", "beta-tester"], last_seen: "2026-08-06" }, arg_diff: { user: { type: "dict_diff", added: { last_seen: "2026-08-06" }, removed: {}, modified: { roles: { before: ["admin"], after: ["admin", "beta-tester"] } } } }, depth: 2, timestamp: 0.043 },
    { call_id: 4, parent_id: 1, type: "call", function: "execute", module: "sqlalchemy.orm.session", category: "Database", file: "sqlalchemy/orm/session.py", line: 1845, depth: 2, args: { statement: "INSERT INTO users ..." }, timestamp: 0.044 },
    { call_id: 4, type: "return", function: "execute", return_value: { rowcount: 1 }, arg_diff: {}, depth: 2, timestamp: 0.061 },
    { call_id: 5, parent_id: 1, type: "call", function: "validate_email", module: "__main__", category: "Logic", file: "app.py", line: 31, depth: 2, args: { email: "ada@example.com" }, timestamp: 0.062 },
    { call_id: 5, type: "return", function: "validate_email", return_value: true, arg_diff: {}, depth: 2, timestamp: 0.062 },
    { call_id: 6, parent_id: 1, type: "call", function: "render_confirmation", module: "flask.templating", category: "UI", file: "flask/templating.py", line: 146, depth: 2, args: { template: "confirm.html", user_id: 42 }, timestamp: 0.063 },
    { call_id: 6, type: "return", function: "render_confirmation", return_value: "<html>...</html>", arg_diff: {}, depth: 2, timestamp: 0.071 },
    { call_id: 7, parent_id: 1, type: "call", function: "risky_divide", module: "__main__", category: "Logic", file: "app.py", line: 40, depth: 2, args: { a: 10, b: 0 }, timestamp: 0.072 },
    { call_id: 7, type: "exception", function: "risky_divide", exception_type: "ZeroDivisionError", exception_message: "division by zero", depth: 2, timestamp: 0.072 },
    { call_id: 7, type: "return", function: "risky_divide", return_value: null, arg_diff: {}, depth: 2, timestamp: 0.073 },
    { call_id: 8, parent_id: 1, type: "call", function: "urlopen", module: "urllib.request", category: "Stdlib", file: "urllib/request.py", line: 214, depth: 2, args: { url: "https://cdn.example.com/avatar.png" }, timestamp: 0.074 },
    { call_id: 8, type: "return", function: "urlopen", return_value: { __type__: "HTTPResponse" }, arg_diff: {}, depth: 2, timestamp: 0.098 },
    { call_id: 1, type: "return", function: "handle_signup_request", return_value: { status: "ok", id: 42 }, arg_diff: {}, depth: 1, timestamp: 0.099 },
  ],
};
DEMO_TRACE.event_count = DEMO_TRACE.events.length;

/* ─────────────────────────────────────────────────────────────────────────
   SYNTHETIC 500-NODE GENERATOR — proves the graph can handle the spec's
   scale target (500 nodes, collapsible sub-graphs) without a real trace.
   ───────────────────────────────────────────────────────────────────── */
function generateSyntheticTrace(targetNodes = 480) {
  const funcNames = {
    Network: ["fetch", "post_json", "connect", "download", "stream_chunk"],
    Database: ["query", "insert_row", "commit", "run_migration", "acquire_conn"],
    UI: ["render", "paint_frame", "diff_dom", "handle_click", "layout"],
    Logic: ["process", "transform", "validate", "compute_hash", "reduce", "normalize"],
    Stdlib: ["loads", "dumps", "compile", "walk", "glob"],
  };
  const weights = [["Logic", 0.5], ["Network", 0.15], ["Database", 0.15], ["UI", 0.1], ["Stdlib", 0.1]];
  const pickCategory = () => {
    let r = Math.random();
    for (const [cat, w] of weights) { if (r < w) return cat; r -= w; }
    return "Logic";
  };
  const events = [];
  let nextId = 1;
  let t = 0;
  const budget = { n: targetNodes - 1 };

  function genNode(parentId, depth) {
    const id = nextId++;
    const category = pickCategory();
    const names = funcNames[category];
    const fn = names[Math.floor(Math.random() * names.length)] + "_" + id;
    t += Math.random() * 2;
    events.push({
      call_id: id, parent_id: parentId, type: "call", function: fn,
      module: category.toLowerCase() + ".mod" + (id % 9), category,
      file: `src/${category.toLowerCase()}/mod_${id % 9}.py`, line: 10 + (id % 200),
      depth, args: { n: id, batch_size: 32 }, timestamp: t / 1000,
    });

    const fanoutRoll = Math.random();
    const maxFanout = depth < 4 ? 6 : depth < 8 ? 3 : 1;
    let numChildren = depth < 14 ? Math.floor(fanoutRoll * (maxFanout + 1)) : 0;
    numChildren = Math.min(numChildren, budget.n);
    for (let i = 0; i < numChildren && budget.n > 0; i++) {
      budget.n--;
      genNode(id, depth + 1);
    }

    if (Math.random() < 0.02) {
      t += 0.1;
      events.push({ call_id: id, type: "exception", function: fn, exception_type: "RuntimeError", exception_message: "synthetic failure at node " + id, depth, timestamp: t / 1000 });
    }
    t += Math.random() * 1.5;
    events.push({ call_id: id, type: "return", function: fn, return_value: id % 2 === 0 ? { ok: true } : id, arg_diff: {}, depth, timestamp: t / 1000 });
  }

  genNode(null, 1);
  return { schema_version: "0.1.0", generated_at: 0, root_dir: "/synthetic", event_count: events.length, events };
}

/* ─────────────────────────────────────────────────────────────────────────
   TREE BUILDING
   ───────────────────────────────────────────────────────────────────── */
function buildTree(events) {
  const nodesById = new Map();
  const roots = [];
  for (const ev of events) {
    if (ev.type === "call") {
      const node = {
        id: ev.call_id, function: ev.function, module: ev.module,
        category: ev.category || "Logic", file: ev.file, line: ev.line,
        depth: ev.depth, args: ev.args || {}, callTimestamp: ev.timestamp,
        children: [], exceptions: [], returnValue: undefined, argDiff: {},
        returnTimestamp: undefined,
      };
      nodesById.set(ev.call_id, node);
      const parent = ev.parent_id != null ? nodesById.get(ev.parent_id) : null;
      if (parent) parent.children.push(node); else roots.push(node);
    } else if (ev.type === "return") {
      const node = nodesById.get(ev.call_id);
      if (node) { node.returnValue = ev.return_value; node.argDiff = ev.arg_diff || {}; node.returnTimestamp = ev.timestamp; }
    } else if (ev.type === "exception") {
      const node = nodesById.get(ev.call_id);
      if (node) node.exceptions.push({ type: ev.exception_type, message: ev.exception_message });
    }
  }
  if (roots.length === 0) return null;
  const root = roots.length === 1
    ? roots[0]
    : { id: "__root__", function: "(program)", category: "Logic", depth: 0, args: {}, children: roots, exceptions: [], argDiff: {} };

  (function annotate(n) {
    let count = 0;
    for (const c of n.children) count += 1 + annotate(c);
    n.descendantCount = count;
    return count;
  })(root);

  return { root, nodesById };
}

/* ─────────────────────────────────────────────────────────────────────────
   MILESTONE 2 — build the same tree/events shape from a unified
   flowtrace-graph.json instead of a raw trace.json, so GraphView/
   SequenceView/TransportBar all work unchanged. The payoff: each runtime
   node also carries `.static` (source, docstring, callers/callees,
   execution count - via REALIZES) and `.flowsIn`/`.flowsOut` (via
   FLOWS_TO) - source, static structure, and runtime shown together.
   ───────────────────────────────────────────────────────────────────── */
function buildTreeFromGraph(graphDoc) {
  const nodesById = new Map(graphDoc.nodes.map((n) => [n.id, n]));
  const runtimeNodes = graphDoc.nodes.filter((n) => n.type === "runtime_call");
  if (runtimeNodes.length === 0) return null;

  const realizesTarget = new Map();
  const callsStaticOut = new Map(), callsStaticIn = new Map();
  const flowsOutByRun = new Map(), flowsInByRun = new Map();
  const childrenOf = new Map();
  const pushTo = (map, key, value) => {
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(value);
  };

  for (const e of graphDoc.edges) {
    if (e.type === "REALIZES") realizesTarget.set(e.source, e.target);
    if (e.type === "CALLS_STATIC") {
      pushTo(callsStaticOut, e.source, e);
      pushTo(callsStaticIn, e.target, e);
    }
    if (e.type === "FLOWS_TO") {
      pushTo(flowsOutByRun, e.source, e);
      pushTo(flowsInByRun, e.target, e);
    }
    if (e.type === "CALLS_RUNTIME") {
      pushTo(childrenOf, e.source, e.target);
    }
  }

  const treeNodesById = new Map();
  for (const rn of runtimeNodes) {
    const staticId = realizesTarget.get(rn.id);
    const staticNode = staticId ? nodesById.get(staticId) : null;
    const staticInfo = staticNode ? {
      file: staticNode.file, line: staticNode.line, endLine: staticNode.end_line,
      docstring: staticNode.docstring, source: staticNode.source,
      executionCount: staticNode.execution_count || 0,
      callers: (callsStaticIn.get(staticId) || []).map((e) => ({ name: nodesById.get(e.source)?.name || e.source, resolved: true })),
      callees: (callsStaticOut.get(staticId) || []).map((e) => ({ name: nodesById.get(e.target)?.name || e.target, resolved: e.resolved, category: nodesById.get(e.target)?.category })),
    } : null;

    treeNodesById.set(rn.id, {
      id: rn.id, function: rn.name, module: staticNode ? staticNode.file : (rn.file || ""),
      category: rn.category || "Logic", file: rn.file, line: rn.line, depth: rn.depth || 0,
      args: rn.args || {}, callTimestamp: rn.timestamp, returnTimestamp: rn.return_timestamp,
      returnValue: rn.return_value, argDiff: rn.arg_diff || {}, exceptions: rn.exceptions || [],
      children: [], static: staticInfo,
      flowsOut: (flowsOutByRun.get(rn.id) || []).map((e) => ({ toName: nodesById.get(e.target)?.name, label: e.from_label, arg: e.to_arg, match: e.match })),
      flowsIn: (flowsInByRun.get(rn.id) || []).map((e) => ({ fromName: nodesById.get(e.source)?.name, label: e.from_label, arg: e.to_arg, match: e.match })),
    });
  }

  const hasParent = new Set();
  for (const [parentId, childIds] of childrenOf) {
    const parent = treeNodesById.get(parentId);
    if (!parent) continue;
    for (const childId of childIds) {
      const child = treeNodesById.get(childId);
      if (child) { parent.children.push(child); hasParent.add(childId); }
    }
  }

  const roots = [...treeNodesById.values()].filter((n) => !hasParent.has(n.id));
  const root = roots.length === 1
    ? roots[0]
    : { id: "__root__", function: "(program)", category: "Logic", args: {}, children: roots, exceptions: [], argDiff: {} };

  (function annotate(n) {
    let count = 0;
    for (const c of n.children) count += 1 + annotate(c);
    n.descendantCount = count;
    return count;
  })(root);

  return { root, nodesById: treeNodesById };
}

function eventsFromGraph(graphDoc) {
  const runtimeNodes = graphDoc.nodes.filter((n) => n.type === "runtime_call");
  const events = [];
  for (const rn of runtimeNodes) {
    const callTs = rn.timestamp ?? 0;
    const returnTs = rn.return_timestamp ?? callTs;
    events.push({ call_id: rn.call_id, type: "call", function: rn.name, args: rn.args || {}, depth: rn.depth, timestamp: callTs, _order: 0 });
    for (const exc of rn.exceptions || []) {
      events.push({ call_id: rn.call_id, type: "exception", function: rn.name, exception_type: exc.type, exception_message: exc.message, depth: rn.depth, timestamp: returnTs, _order: 1 });
    }
    events.push({ call_id: rn.call_id, type: "return", function: rn.name, return_value: rn.return_value, arg_diff: rn.arg_diff || {}, depth: rn.depth, timestamp: returnTs, _order: 2 });
  }
  // sort by actual timestamp (not call_id-grouped) so a parent's children
  // correctly interleave between its own call and return events, matching
  // real chronological/nesting order rather than "all of node X's events,
  // then all of node Y's".
  events.sort((a, b) => (a.timestamp - b.timestamp) || (a._order - b._order) || (a.call_id - b.call_id));
  return events;
}

/* ─────────────────────────────────────────────────────────────────────────
   SMALL PRESENTATIONAL HELPERS
   ───────────────────────────────────────────────────────────────────── */
function CategoryDot({ category, size = 8 }) {
  const color = CATEGORY_META[category]?.color || T.inkDim;
  return <span style={{ width: size, height: size, borderRadius: "50%", background: color, display: "inline-block", flexShrink: 0 }} />;
}

function Pretty({ value }) {
  return (
    <pre style={{ margin: 0, fontFamily: T.mono, fontSize: 12, color: T.ink, whiteSpace: "pre-wrap", wordBreak: "break-word", lineHeight: 1.5 }}>
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

function DiffRow({ sign, k, children }) {
  const colors = { "+": "#7FE0A0", "-": "#FF8080", "~": T.accent };
  const bg = { "+": "rgba(127,224,160,0.08)", "-": "rgba(255,128,128,0.08)", "~": "rgba(255,184,77,0.08)" };
  return (
    <div style={{ display: "flex", gap: 8, padding: "3px 8px", background: bg[sign], borderRadius: 4, fontFamily: T.mono, fontSize: 12 }}>
      <span style={{ color: colors[sign], fontWeight: 700, width: 12 }}>{sign}</span>
      <span style={{ color: T.inkDim, flexShrink: 0 }}>{k}</span>
      <span style={{ color: T.ink, wordBreak: "break-word" }}>{children}</span>
    </div>
  );
}

function DiffView({ diff }) {
  if (!diff) return null;
  if (diff.type === "dict_diff") {
    const rows = [];
    for (const k of Object.keys(diff.added || {})) rows.push(<DiffRow key={"a" + k} sign="+" k={k}>{JSON.stringify(diff.added[k])}</DiffRow>);
    for (const k of Object.keys(diff.removed || {})) rows.push(<DiffRow key={"r" + k} sign="-" k={k}>{JSON.stringify(diff.removed[k])}</DiffRow>);
    for (const k of Object.keys(diff.modified || {})) {
      const m = diff.modified[k];
      const inner = m && m.type ? <DiffView diff={m} /> : `${JSON.stringify(m?.before)} → ${JSON.stringify(m?.after)}`;
      rows.push(<div key={"m" + k}><DiffRow sign="~" k={k}>{typeof inner === "string" ? inner : null}</DiffRow>{typeof inner !== "string" && <div style={{ marginLeft: 24 }}>{inner}</div>}</div>);
    }
    return <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>{rows}</div>;
  }
  if (diff.type === "list_diff") {
    return <DiffRow sign="~" k="[]">{JSON.stringify(diff.before)} → {JSON.stringify(diff.after)}</DiffRow>;
  }
  return <DiffRow sign="~" k="">{JSON.stringify(diff.before)} → {JSON.stringify(diff.after)}</DiffRow>;
}

/* ─────────────────────────────────────────────────────────────────────────
   GRAPH (DAG) VIEW
   ───────────────────────────────────────────────────────────────────── */
const NODE_W = 168, NODE_H = 56, DX = 196, DY = 128;

function GraphView({ root, collapsed, toggleCollapse, selectedId, setSelectedId, dim, activePathIds }) {
  const svgRef = useRef(null);
  const containerRef = useRef(null);
  const [viewBox, setViewBox] = useState(null);
  const dragState = useRef(null);

  const layout = useMemo(() => {
    if (!root) return null;
    const hierarchy = d3.hierarchy(root, (d) => (collapsed.has(d.id) ? null : d.children));
    d3.tree().nodeSize([DX, DY])(hierarchy);
    return hierarchy;
  }, [root, collapsed]);

  const fit = useCallback(() => {
    if (!layout) return;
    const nodes = layout.descendants();
    const xs = nodes.map((n) => n.x), ys = nodes.map((n) => n.y);
    const minX = Math.min(...xs) - NODE_W, maxX = Math.max(...xs) + NODE_W;
    const minY = Math.min(...ys) - NODE_H, maxY = Math.max(...ys) + NODE_H * 2;
    setViewBox({ x: minX, y: minY - 20, w: maxX - minX, h: maxY - minY + 40 });
  }, [layout]);

  useEffect(() => { fit(); }, [root]); // eslint-disable-line react-hooks/exhaustive-deps

  const onWheel = (e) => {
    e.preventDefault();
    if (!viewBox) return;
    const rect = svgRef.current.getBoundingClientRect();
    const px = (e.clientX - rect.left) / rect.width;
    const py = (e.clientY - rect.top) / rect.height;
    const factor = e.deltaY > 0 ? 1.12 : 1 / 1.12;
    const newW = viewBox.w * factor, newH = viewBox.h * factor;
    setViewBox({
      x: viewBox.x + (viewBox.w - newW) * px,
      y: viewBox.y + (viewBox.h - newH) * py,
      w: newW, h: newH,
    });
  };

  const onMouseDown = (e) => { dragState.current = { startX: e.clientX, startY: e.clientY, vb: viewBox }; };
  const onMouseMove = (e) => {
    if (!dragState.current) return;
    const rect = svgRef.current.getBoundingClientRect();
    const { startX, startY, vb } = dragState.current;
    const dx = ((e.clientX - startX) / rect.width) * vb.w;
    const dy = ((e.clientY - startY) / rect.height) * vb.h;
    setViewBox({ ...vb, x: vb.x - dx, y: vb.y - dy });
  };
  const onMouseUp = () => { dragState.current = null; };
  const zoomBtn = (factor) => () => setViewBox((vb) => vb && ({ x: vb.x + vb.w * (1 - factor) / 2, y: vb.y + vb.h * (1 - factor) / 2, w: vb.w * factor, h: vb.h * factor }));

  if (!layout || !viewBox) return null;
  const nodes = layout.descendants();
  const links = layout.links();
  const linkGen = d3.linkVertical().x((d) => d.x).y((d) => d.y);

  return (
    <div ref={containerRef} style={{ position: "relative", flex: 1, minWidth: 0, background: T.bgVoid, overflow: "hidden" }}>
      {/* scanline texture — signature ambient detail */}
      <div style={{
        position: "absolute", inset: 0, pointerEvents: "none", opacity: 0.05,
        backgroundImage: "repeating-linear-gradient(180deg, #fff 0px, #fff 1px, transparent 1px, transparent 3px)",
      }} />
      <svg
        ref={svgRef}
        width="100%" height="100%"
        viewBox={`${viewBox.x} ${viewBox.y} ${viewBox.w} ${viewBox.h}`}
        onWheel={onWheel} onMouseDown={onMouseDown} onMouseMove={onMouseMove} onMouseUp={onMouseUp} onMouseLeave={onMouseUp}
        style={{ cursor: dragState.current ? "grabbing" : "grab", display: "block" }}
      >
        <defs>
          <filter id="glow" x="-60%" y="-60%" width="220%" height="220%">
            <feGaussianBlur stdDeviation="4" result="blur" />
            <feMerge>
              <feMergeNode in="blur" /><feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>

        {links.map((l, i) => {
          const active = activePathIds.has(l.source.data.id) && activePathIds.has(l.target.data.id);
          return (
            <path key={i} d={linkGen(l)} fill="none"
              stroke={active ? T.accent : T.hairline}
              strokeWidth={active ? 2.4 : 1.4}
              filter={active ? "url(#glow)" : undefined}
              opacity={dim ? 0.25 : 1}
            />
          );
        })}

        {nodes.map((n) => {
          const d = n.data;
          const meta = CATEGORY_META[d.category] || CATEGORY_META.Logic;
          const Icon = meta.icon;
          const isSelected = selectedId === d.id;
          const isCollapsedHere = collapsed.has(d.id);
          const hiddenCount = isCollapsedHere ? d.descendantCount : 0;
          const hasError = d.exceptions && d.exceptions.length > 0;
          const faded = dim && !d.function?.toLowerCase?.().includes(dim);
          return (
            <g key={d.id} transform={`translate(${n.x - NODE_W / 2}, ${n.y - NODE_H / 2})`}
              style={{ cursor: "pointer" }} opacity={faded ? 0.25 : 1}
              onClick={() => setSelectedId(d.id)}
              onDoubleClick={(e) => { e.stopPropagation(); if (d.children && d.children.length) toggleCollapse(d.id); }}
            >
              <rect width={NODE_W} height={NODE_H} rx={10}
                fill={T.bgPanelRaised}
                stroke={isSelected ? T.accent : hasError ? T.danger : T.hairline}
                strokeWidth={isSelected ? 2 : 1}
                filter={isSelected ? "url(#glow)" : undefined}
              />
              <rect x={0} y={0} width={4} height={NODE_H} rx={2} fill={meta.color} />
              <text x={14} y={22} fontFamily={T.mono} fontSize={12.5} fontWeight={600} fill={T.ink}>
                {d.function.length > 17 ? d.function.slice(0, 16) + "…" : d.function}
              </text>
              <text x={14} y={38} fontFamily={T.sans} fontSize={10} fill={T.inkDim}>
                {d.category}{d.module ? " · " + d.module.split(".")[0] : ""}
              </text>
              {hasError && <circle cx={NODE_W - 14} cy={14} r={4} fill={T.danger} />}
              {hiddenCount > 0 && (
                <g transform={`translate(${NODE_W - 30}, ${NODE_H - 18})`}>
                  <rect width={26} height={14} rx={7} fill={T.accent} />
                  <text x={13} y={10.5} textAnchor="middle" fontFamily={T.mono} fontSize={9} fontWeight={700} fill={T.bgVoid}>+{hiddenCount}</text>
                </g>
              )}
            </g>
          );
        })}
      </svg>

      <div style={{ position: "absolute", bottom: 14, right: 14, display: "flex", gap: 6 }}>
        <IconBtn icon={ZoomOut} onClick={zoomBtn(1.25)} />
        <IconBtn icon={ZoomIn} onClick={zoomBtn(0.8)} />
        <IconBtn icon={Maximize2} onClick={fit} />
      </div>
    </div>
  );
}

function IconBtn({ icon: Icon, onClick, active, title }) {
  return (
    <button title={title} onClick={onClick} style={{
      width: 30, height: 30, borderRadius: 7, border: `1px solid ${active ? T.accent : T.hairline}`,
      background: active ? "rgba(255,184,77,0.12)" : T.bgPanelRaised, color: active ? T.accent : T.inkDim,
      display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer",
    }}>
      <Icon size={15} />
    </button>
  );
}

/* ─────────────────────────────────────────────────────────────────────────
   SEQUENCE (LIFELINE-STYLE) VIEW
   ───────────────────────────────────────────────────────────────────── */
function SequenceView({ events, nodesById, selectedId, setSelectedId, currentEventIndex }) {
  return (
    <div style={{ flex: 1, overflow: "auto", background: T.bgVoid, padding: "8px 0" }}>
      {events.map((ev, i) => {
        const node = nodesById.get(ev.call_id);
        const meta = CATEGORY_META[node?.category] || CATEGORY_META.Logic;
        const isCurrent = i === currentEventIndex;
        const depth = ev.depth || 0;
        return (
          <div key={i} onClick={() => setSelectedId(ev.call_id)}
            style={{
              display: "flex", alignItems: "center", gap: 8, padding: "5px 16px",
              background: isCurrent ? "rgba(255,184,77,0.10)" : "transparent",
              borderLeft: isCurrent ? `2px solid ${T.accent}` : "2px solid transparent",
              cursor: "pointer",
            }}>
            <span style={{ fontFamily: T.mono, fontSize: 10, color: T.inkDim, width: 34, flexShrink: 0 }}>#{i}</span>
            <div style={{ display: "flex", gap: 2, width: depth * 12, flexShrink: 0, justifyContent: "flex-end" }}>
              {Array.from({ length: depth }).map((_, k) => (
                <div key={k} style={{ width: 2, background: T.hairline }} />
              ))}
            </div>
            <CategoryDot category={node?.category} />
            <span style={{
              fontFamily: T.mono, fontSize: 12,
              color: ev.type === "exception" ? T.danger : ev.type === "return" ? T.inkDim : T.ink,
              fontWeight: ev.type === "call" ? 600 : 400,
            }}>
              {ev.type === "call" ? "→ " : ev.type === "return" ? "← " : "⚠ "}{ev.function}
            </span>
            {ev.type === "call" && <span style={{ fontFamily: T.mono, fontSize: 11, color: T.inkDim }}>({Object.keys(ev.args || {}).join(", ")})</span>}
            {ev.type === "exception" && <span style={{ fontFamily: T.mono, fontSize: 11, color: T.danger }}>{ev.exception_type}: {ev.exception_message}</span>}
          </div>
        );
      })}
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────────────────
   INSPECTOR PANEL
   ───────────────────────────────────────────────────────────────────── */
function SourceBlock({ source }) {
  if (!source) return null;
  return (
    <pre style={{
      margin: 0, fontFamily: T.mono, fontSize: 11.5, color: T.ink, whiteSpace: "pre", overflow: "auto",
      background: T.bgVoid, border: `1px solid ${T.hairline}`, borderRadius: 6, padding: 10, lineHeight: 1.5, maxHeight: 220,
    }}>
      {source}
    </pre>
  );
}

function Inspector({ node }) {
  if (!node) {
    return <div style={{ padding: 20, color: T.inkDim, fontFamily: T.sans, fontSize: 13 }}>Select a node to inspect its arguments, return value, and state diffs.</div>;
  }
  const durationMs = node.returnTimestamp != null && node.callTimestamp != null
    ? ((node.returnTimestamp - node.callTimestamp) * 1000).toFixed(2) : null;
  const diffKeys = Object.keys(node.argDiff || {});
  const s = node.static;

  return (
    <div style={{ padding: 16, overflow: "auto", flex: 1 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
        <CategoryDot category={node.category} size={10} />
        <span style={{ fontFamily: T.mono, fontSize: 15, fontWeight: 700, color: T.ink }}>{node.function}</span>
      </div>
      <div style={{ fontFamily: T.mono, fontSize: 11, color: T.inkDim, marginBottom: 16 }}>
        {node.module} · {node.file}:{node.line}{durationMs != null && ` · ${durationMs}ms`}
        {s?.executionCount > 1 && ` · executed ${s.executionCount}×`}
      </div>

      {s?.docstring && (
        <div style={{ fontFamily: T.mono, fontSize: 11.5, color: T.inkDim, fontStyle: "italic", marginBottom: 14, borderLeft: `2px solid ${T.hairline}`, paddingLeft: 8 }}>
          "{s.docstring}"
        </div>
      )}

      {s?.source && (
        <Section title="Source"><SourceBlock source={s.source} /></Section>
      )}

      {s && (s.callers.length > 0 || s.callees.length > 0) && (
        <Section title="Static relationships">
          {s.callers.length > 0 && (
            <div style={{ marginBottom: 8 }}>
              <div style={{ fontFamily: T.sans, fontSize: 10.5, color: T.inkDim, marginBottom: 4 }}>Called by ({s.callers.length})</div>
              {s.callers.map((c, i) => (
                <div key={i} style={{ fontFamily: T.mono, fontSize: 11.5, color: T.ink }}>{c.name}</div>
              ))}
            </div>
          )}
          {s.callees.length > 0 && (
            <div>
              <div style={{ fontFamily: T.sans, fontSize: 10.5, color: T.inkDim, marginBottom: 4 }}>Calls ({s.callees.length})</div>
              {s.callees.map((c, i) => (
                <div key={i} style={{ fontFamily: T.mono, fontSize: 11.5, color: c.resolved ? T.ink : T.inkDim }}>
                  {c.name}{!c.resolved && c.category ? ` [${c.category}]` : ""}
                </div>
              ))}
            </div>
          )}
        </Section>
      )}

      {(node.flowsIn?.length > 0 || node.flowsOut?.length > 0) && (
        <Section title="Data flow" color={T.accent}>
          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            {node.flowsIn?.map((f, i) => (
              <div key={"in" + i} style={{ fontFamily: T.mono, fontSize: 11.5, color: T.ink }}>
                ← <span style={{ color: T.accent }}>{f.fromName}</span>.{f.label} → this.{f.arg}
                {f.match === "overlap" && <span style={{ color: T.inkDim }}> (partial match)</span>}
              </div>
            ))}
            {node.flowsOut?.map((f, i) => (
              <div key={"out" + i} style={{ fontFamily: T.mono, fontSize: 11.5, color: T.ink }}>
                → this.{f.label} → <span style={{ color: T.accent }}>{f.toName}</span>.{f.arg}
                {f.match === "overlap" && <span style={{ color: T.inkDim }}> (partial match)</span>}
              </div>
            ))}
          </div>
        </Section>
      )}

      {node.exceptions && node.exceptions.length > 0 && (
        <Section title="Exceptions" icon={AlertTriangle} color={T.danger}>
          {node.exceptions.map((e, i) => (
            <div key={i} style={{ fontFamily: T.mono, fontSize: 12, color: T.danger, marginBottom: 4 }}>{e.type}: {e.message}</div>
          ))}
        </Section>
      )}

      <Section title="Arguments"><Pretty value={node.args} /></Section>

      {diffKeys.length > 0 && (
        <Section title="Mutated arguments" color={T.accent}>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {diffKeys.map((k) => (
              <div key={k}>
                <div style={{ fontFamily: T.mono, fontSize: 11, color: T.inkDim, marginBottom: 3 }}>{k}</div>
                <DiffView diff={node.argDiff[k]} />
              </div>
            ))}
          </div>
        </Section>
      )}

      <Section title="Return value"><Pretty value={node.returnValue} /></Section>
    </div>
  );
}

function Section({ title, icon: Icon, color, children }) {
  return (
    <div style={{ marginBottom: 18 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 6 }}>
        {Icon && <Icon size={12} color={color || T.inkDim} />}
        <span style={{ fontFamily: T.sans, fontSize: 10.5, letterSpacing: 0.6, textTransform: "uppercase", color: color || T.inkDim, fontWeight: 600 }}>{title}</span>
      </div>
      {children}
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────────────────
   FILTER RAIL
   ───────────────────────────────────────────────────────────────────── */
function FilterRail({ counts, activeCategories, toggleCategory, search, setSearch, dataset, setDataset, onUpload }) {
  const fileRef = useRef(null);
  return (
    <div style={{ width: 190, borderRight: `1px solid ${T.hairline}`, padding: 14, display: "flex", flexDirection: "column", gap: 18, overflow: "auto" }}>
      <div>
        <Label>Dataset</Label>
        <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
          <DatasetBtn active={dataset === "demo"} onClick={() => setDataset("demo")}>Demo trace</DatasetBtn>
          <DatasetBtn active={dataset === "synthetic"} onClick={() => setDataset("synthetic")}>Synthetic · 500 nodes</DatasetBtn>
          <DatasetBtn active={dataset === "trace-upload" || dataset === "graph-upload"} onClick={() => fileRef.current?.click()}>
            <Upload size={11} style={{ marginRight: 5, verticalAlign: -2 }} />Upload trace/graph.json
          </DatasetBtn>
          <input ref={fileRef} type="file" accept="application/json" style={{ display: "none" }}
            onChange={(e) => { const f = e.target.files?.[0]; if (f) onUpload(f); e.target.value = ""; }} />
        </div>
      </div>

      <div>
        <Label>Search</Label>
        <div style={{ position: "relative" }}>
          <Search size={12} style={{ position: "absolute", left: 8, top: 8, color: T.inkDim }} />
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="function name…"
            style={{
              width: "100%", background: T.bgPanelRaised, border: `1px solid ${T.hairline}`, borderRadius: 6,
              padding: "6px 8px 6px 26px", color: T.ink, fontFamily: T.sans, fontSize: 12, boxSizing: "border-box",
            }} />
        </div>
      </div>

      <div>
        <Label>Category</Label>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {CATEGORIES.map((cat) => {
            const on = activeCategories.has(cat);
            return (
              <label key={cat} style={{ display: "flex", alignItems: "center", gap: 7, cursor: "pointer", opacity: on ? 1 : 0.4 }}
                onClick={() => toggleCategory(cat)}>
                <CategoryDot category={cat} />
                <span style={{ fontFamily: T.sans, fontSize: 12, color: T.ink, flex: 1 }}>{cat}</span>
                <span style={{ fontFamily: T.mono, fontSize: 10, color: T.inkDim }}>{counts[cat] || 0}</span>
              </label>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function Label({ children }) {
  return <div style={{ fontFamily: T.sans, fontSize: 10.5, letterSpacing: 0.6, textTransform: "uppercase", color: T.inkDim, fontWeight: 600, marginBottom: 7 }}>{children}</div>;
}
function DatasetBtn({ active, onClick, children }) {
  return (
    <button onClick={onClick} style={{
      textAlign: "left", padding: "6px 9px", borderRadius: 6, fontFamily: T.sans, fontSize: 12,
      border: `1px solid ${active ? T.accent : T.hairline}`, background: active ? "rgba(255,184,77,0.10)" : "transparent",
      color: active ? T.accent : T.ink, cursor: "pointer",
    }}>{children}</button>
  );
}

/* ─────────────────────────────────────────────────────────────────────────
   TRANSPORT / SCRUB BAR
   ───────────────────────────────────────────────────────────────────── */
function TransportBar({ events, currentEventIndex, setCurrentEventIndex, isPlaying, setIsPlaying }) {
  const ev = events[currentEventIndex];
  return (
    <div style={{ borderTop: `1px solid ${T.hairline}`, padding: "10px 16px", display: "flex", alignItems: "center", gap: 12, background: T.bgPanel }}>
      <IconBtn icon={SkipBack} onClick={() => setCurrentEventIndex(0)} />
      <IconBtn icon={isPlaying ? Pause : Play} active={isPlaying} onClick={() => setIsPlaying((p) => !p)} />
      <IconBtn icon={SkipForward} onClick={() => setCurrentEventIndex(events.length - 1)} />
      <input type="range" min={0} max={Math.max(0, events.length - 1)} value={currentEventIndex}
        onChange={(e) => { setIsPlaying(false); setCurrentEventIndex(Number(e.target.value)); }}
        style={{ flex: 1, accentColor: T.accent }} />
      <div style={{ fontFamily: T.mono, fontSize: 11, color: T.inkDim, minWidth: 190, textAlign: "right" }}>
        {ev && <span style={{ color: T.ink }}>{ev.type} {ev.function}</span>} · {currentEventIndex + 1}/{events.length}
      </div>
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────────────────
   APP
   ───────────────────────────────────────────────────────────────────── */
export default function App() {
  const [dataset, setDataset] = useState("demo");
  const [uploadedTrace, setUploadedTrace] = useState(null);
  const [uploadedGraph, setUploadedGraph] = useState(null);
  const [view, setView] = useState("graph");
  const [collapsed, setCollapsed] = useState(() => new Set());
  const [selectedId, setSelectedId] = useState(null);
  const [activeCategories, setActiveCategories] = useState(() => new Set(CATEGORIES));
  const [search, setSearch] = useState("");
  const [currentEventIndex, setCurrentEventIndex] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);

  // Unified pipeline: a graph.json (Milestone 2 - carries source/static/
  // dataflow alongside runtime) and a plain trace.json both resolve to the
  // same {root, nodesById, events, rawDoc} shape, so GraphView/SequenceView/
  // TransportBar/Inspector don't need to know which one is loaded.
  const bundle = useMemo(() => {
    if (dataset === "graph-upload" && uploadedGraph) {
      const built = buildTreeFromGraph(uploadedGraph) || { root: null, nodesById: new Map() };
      return { ...built, events: eventsFromGraph(uploadedGraph), rawDoc: uploadedGraph, rawFilename: "flowtrace-graph.json" };
    }
    const trace = dataset === "trace-upload" && uploadedTrace ? uploadedTrace
      : dataset === "synthetic" ? generateSyntheticTrace(480)
      : DEMO_TRACE;
    const built = buildTree(trace.events) || { root: null, nodesById: new Map() };
    return { ...built, events: trace.events, rawDoc: trace, rawFilename: "trace.json" };
  }, [dataset, uploadedTrace, uploadedGraph]);

  const { root, nodesById, events, rawDoc, rawFilename } = bundle;

  // reset transient view state whenever the dataset changes
  useEffect(() => {
    setCollapsed(new Set());
    setSelectedId(root ? root.id : null);
    setCurrentEventIndex(0);
    setIsPlaying(false);
  }, [bundle]); // eslint-disable-line react-hooks/exhaustive-deps

  // playback
  useEffect(() => {
    if (!isPlaying) return;
    if (currentEventIndex >= events.length - 1) { setIsPlaying(false); return; }
    const t = setTimeout(() => setCurrentEventIndex((i) => Math.min(i + 1, events.length - 1)), 220);
    return () => clearTimeout(t);
  }, [isPlaying, currentEventIndex, events.length]);

  // scrubbing selects whichever call is "current" at that point in the trace
  useEffect(() => {
    const ev = events[currentEventIndex];
    if (ev) setSelectedId(ev.call_id);
  }, [currentEventIndex]); // eslint-disable-line react-hooks/exhaustive-deps

  const toggleCollapse = useCallback((id) => {
    setCollapsed((prev) => { const next = new Set(prev); next.has(id) ? next.delete(id) : next.add(id); return next; });
  }, []);
  const toggleCategory = useCallback((cat) => {
    setActiveCategories((prev) => { const next = new Set(prev); next.has(cat) ? next.delete(cat) : next.add(cat); return next; });
  }, []);

  const counts = useMemo(() => {
    const c = {};
    for (const n of nodesById.values()) c[n.category] = (c[n.category] || 0) + 1;
    return c;
  }, [nodesById]);

  const selectedNode = selectedId != null ? nodesById.get(selectedId) : null;

  const activePathIds = useMemo(() => {
    const s = new Set();
    let n = selectedNode ? { id: selectedNode.id } : null;
    // walk up via nodesById requires parent refs; recompute quickly from tree
    function findPath(node, targetId, path) {
      if (!node) return null;
      const p = [...path, node.id];
      if (node.id === targetId) return p;
      for (const c of node.children || []) { const r = findPath(c, targetId, p); if (r) return r; }
      return null;
    }
    if (root && selectedId != null) {
      const path = findPath(root, selectedId, []);
      if (path) path.forEach((id) => s.add(id));
    }
    return s;
  }, [root, selectedId]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleUpload = (file) => {
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const parsed = JSON.parse(reader.result);
        if (Array.isArray(parsed.nodes) && Array.isArray(parsed.edges)) {
          setUploadedGraph(parsed);
          setDataset("graph-upload");
        } else if (Array.isArray(parsed.events)) {
          setUploadedTrace(parsed);
          setDataset("trace-upload");
        } else {
          alert("That doesn't look like a flowtrace trace.json (has .events) or flowtrace-graph.json (has .nodes/.edges).");
        }
      } catch (e) {
        alert("Couldn't parse that file as JSON: " + e.message);
      }
    };
    reader.readAsText(file);
  };

  const exportJson = () => {
    const blob = new Blob([JSON.stringify(rawDoc, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = rawFilename;
    a.click();
  };

  const datasetLabel = { demo: "demo dataset", synthetic: "synthetic dataset", "trace-upload": "uploaded trace", "graph-upload": "uploaded graph (source+static+runtime)" }[dataset];

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100vh", background: T.bgVoid, fontFamily: T.sans }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap');
        * { box-sizing: border-box; }
        input[type=range] { -webkit-appearance: none; height: 3px; background: ${T.hairline}; border-radius: 2px; }
        input[type=range]::-webkit-slider-thumb { -webkit-appearance: none; width: 12px; height: 12px; border-radius: 50%; background: ${T.accent}; cursor: pointer; }
        @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
      `}</style>

      {/* header */}
      <div style={{ display: "flex", alignItems: "center", gap: 14, padding: "10px 16px", borderBottom: `1px solid ${T.hairline}`, background: T.bgPanel }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ width: 8, height: 8, borderRadius: "50%", background: T.accent, animation: "pulse 2s infinite" }} />
          <span style={{ fontFamily: T.mono, fontWeight: 700, fontSize: 14, color: T.ink, letterSpacing: 0.5 }}>FLOWTRACE</span>
        </div>
        <span style={{ fontFamily: T.mono, fontSize: 11, color: T.inkDim }}>{datasetLabel} · {events.length} events</span>

        <div style={{ flex: 1 }} />

        <div style={{ display: "flex", border: `1px solid ${T.hairline}`, borderRadius: 7, overflow: "hidden" }}>
          <ViewToggleBtn active={view === "graph"} icon={GitBranch} label="Graph" onClick={() => setView("graph")} />
          <ViewToggleBtn active={view === "sequence"} icon={List} label="Sequence" onClick={() => setView("sequence")} />
        </div>
        <IconBtn icon={Download} title="Export JSON" onClick={exportJson} />
      </div>

      {/* body */}
      <div style={{ display: "flex", flex: 1, minHeight: 0 }}>
        <FilterRail counts={counts} activeCategories={activeCategories} toggleCategory={toggleCategory}
          search={search} setSearch={setSearch} dataset={dataset} setDataset={setDataset} onUpload={handleUpload} />

        {view === "graph" ? (
          <GraphView root={root} collapsed={collapsed} toggleCollapse={toggleCollapse}
            selectedId={selectedId} setSelectedId={setSelectedId}
            dim={search.trim().toLowerCase() || null} activePathIds={activePathIds} />
        ) : (
          <SequenceView events={events} nodesById={nodesById} selectedId={selectedId} setSelectedId={setSelectedId} currentEventIndex={currentEventIndex} />
        )}

        <div style={{ width: 300, borderLeft: `1px solid ${T.hairline}`, background: T.bgPanel, display: "flex", flexDirection: "column" }}>
          <Inspector node={selectedNode} />
        </div>
      </div>

      <TransportBar events={events} currentEventIndex={currentEventIndex} setCurrentEventIndex={setCurrentEventIndex} isPlaying={isPlaying} setIsPlaying={setIsPlaying} />
    </div>
  );
}

function ViewToggleBtn({ active, icon: Icon, label, onClick }) {
  return (
    <button onClick={onClick} style={{
      display: "flex", alignItems: "center", gap: 6, padding: "6px 12px", border: "none",
      background: active ? "rgba(255,184,77,0.12)" : "transparent", color: active ? T.accent : T.inkDim,
      fontFamily: T.sans, fontSize: 12, fontWeight: 500, cursor: "pointer",
    }}>
      <Icon size={13} />{label}
    </button>
  );
}
