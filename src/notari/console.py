"""The minimal Notari console.

A single self-contained page served at `/` by the FastAPI server. It renders the
memory as a graph (ReactFlow via CDN — no build step), with a time-travel slider
that filters facts by valid-time, click-an-edge provenance, and a forget button.
"""

from __future__ import annotations

CONSOLE_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<title>Notari console</title>
<link rel="stylesheet" href="https://esm.sh/reactflow@11.11.4/dist/style.css" />
<style>
  html,body,#root { height:100%; margin:0; font-family:ui-sans-serif,system-ui,sans-serif; }
  #root { display:flex; flex-direction:column; }
  .bar { display:flex; gap:10px; align-items:center; padding:10px 14px; border-bottom:1px solid #ddd; flex-wrap:wrap; }
  .bar h1 { font-size:15px; margin:0 12px 0 0; }
  .bar input[type=text]{ padding:5px 8px; border:1px solid #bbb; border-radius:6px; }
  .bar button{ padding:5px 10px; border:1px solid #bbb; border-radius:6px; background:#f6f6f6; cursor:pointer; }
  .bar button.danger{ border-color:#d66; color:#a00; }
  .chip{ padding:3px 9px; border-radius:999px; font-size:12px; font-variant-numeric:tabular-nums; }
  .chip.ok{ background:#e7f7ec; color:#166534; border:1px solid #86dfa4; }
  .chip.bad{ background:#fdeaea; color:#991b1b; border:1px solid #f3a6a6; }
  .asof{ font-variant-numeric:tabular-nums; color:#555; min-width:96px; }
  .main { flex:1; display:flex; min-height:0; }
  .flow { flex:1; }
  .panel { width:300px; border-left:1px solid #ddd; padding:14px; font-size:13px; overflow:auto; }
  .panel code{ background:#f3f3f3; padding:1px 4px; border-radius:4px; }
  .muted{ color:#888; }
</style>
</head>
<body>
<div id="root"></div>
<script type="module">
import React from "https://esm.sh/react@18.2.0";
import { createRoot } from "https://esm.sh/react-dom@18.2.0/client";
import ReactFlow, { Background, Controls } from "https://esm.sh/reactflow@11.11.4?deps=react@18.2.0,react-dom@18.2.0";

const h = React.createElement;

function App() {
  const [subject, setSubject] = React.useState("user_42");
  const [graph, setGraph] = React.useState({ nodes: [], edges: [] });
  const [bounds, setBounds] = React.useState([0, 0]);
  const [t, setT] = React.useState(Date.now());
  const [prov, setProv] = React.useState(null);
  const [audit, setAudit] = React.useState(null);

  async function load() {
    const r = await fetch("/v1/graph?subject_id=" + encodeURIComponent(subject));
    const g = await r.json();
    const times = [];
    g.edges.forEach(e => { if (e.valid_from) times.push(Date.parse(e.valid_from));
                           if (e.valid_to) times.push(Date.parse(e.valid_to)); });
    const max = Date.now();
    const min = times.length ? Math.min(...times) : max;
    setGraph(g); setBounds([min, max]); setT(max); setProv(null);
  }
  React.useEffect(() => { load(); }, []);

  const visible = e => {
    const vf = e.valid_from ? Date.parse(e.valid_from) : -Infinity;
    const vt = e.valid_to ? Date.parse(e.valid_to) : Infinity;
    return vf <= t && t < vt;
  };

  let ey = 0, vy = 0;
  const nodes = graph.nodes.map(n => {
    const ent = n.kind === "entity";
    return { id: n.id, data: { label: n.label },
      position: ent ? { x: 40, y: 30 + (ey++) * 90 } : { x: 360, y: 30 + (vy++) * 64 },
      style: { padding: 6, borderRadius: 8, fontSize: 12,
               border: "1px solid #99a", background: ent ? "#eef2ff" : "#ecfdf5" } };
  });
  const edges = graph.edges.filter(visible).map(e => ({
    id: e.fact_id, source: e.subject, target: e.object, label: e.predicate,
    animated: e.alive, style: { opacity: e.alive ? 1 : 0.4 }, labelStyle: { fontSize: 11 } }));

  async function onEdgeClick(_, edge) {
    const r = await fetch("/v1/provenance/" + edge.id);
    setProv(r.ok ? await r.json() : { error: "not found" });
  }
  async function forget() {
    if (!confirm("Forget " + subject + "? This is irreversible.")) return;
    const r = await fetch("/v1/forget/" + encodeURIComponent(subject), { method: "POST" });
    const cert = await r.json();
    alert("Deleted " + cert.facts_deleted + " facts / " + cert.episodes_deleted +
          " episodes.\\nCertificate " + cert.certificate_id +
          (cert.signature ? "\\nSigned: " + cert.algorithm : "\\n(unsigned — no NOTARI_KEK)"));
    await load();
    await verifyAudit();
  }
  async function verifyAudit() {
    // Deep: re-derives every event's digest, not just the chain links — catches
    // silent in-place content edits and flags rogue key deletions.
    const r = await fetch("/v1/audit/verify?deep=true");
    setAudit(await r.json());
  }

  const asof = new Date(t).toISOString().slice(0, 10);
  return h("div", { style: { height: "100%", display: "flex", flexDirection: "column" } },
    h("div", { className: "bar" },
      h("h1", null, "Notari"),
      h("input", { type: "text", value: subject, onChange: e => setSubject(e.target.value) }),
      h("button", { onClick: load }, "Load"),
      h("span", { className: "muted" }, "as of"),
      h("input", { type: "range", min: bounds[0], max: bounds[1], value: t,
                   onChange: e => setT(Number(e.target.value)), style: { width: 200 } }),
      h("span", { className: "asof" }, asof),
      h("button", { onClick: verifyAudit }, "Verify audit"),
      audit && h("span", { className: "chip " + (audit.ok ? "ok" : "bad") },
        audit.ok ? "chain intact \\u00b7 " + audit.entries + " entries (deep)"
                 : "TAMPERED at seq " + audit.broken_at),
      h("button", { className: "danger", onClick: forget }, "Forget subject")),
    h("div", { className: "main" },
      h("div", { className: "flow" },
        h(ReactFlow, { nodes, edges, onEdgeClick, fitView: true },
          h(Background, null), h(Controls, null))),
      h("div", { className: "panel" },
        h("b", null, "Provenance"),
        prov
          ? (prov.error
              ? h("p", { className: "muted" }, "No provenance for this fact.")
              : h("div", null,
                  h("p", null, "snippet: ", h("code", null, prov.snippet ?? "—")),
                  h("p", null, "source: ", h("code", null, prov.source_ref ?? "—")),
                  h("p", { className: "muted" }, "learned " + (prov.recorded_at || "").slice(0, 10))))
          : h("p", { className: "muted" }, "Click an edge to trace a fact to its source."))));
}

createRoot(document.getElementById("root")).render(h(App));
</script>
</body>
</html>
"""
