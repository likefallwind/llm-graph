"""导出 d3 力导向可视化（单文件 HTML，数据内嵌）。"""
import json
import os

from . import db

TEMPLATE = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>AI 知识图谱</title>
<script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
<style>
  body { margin: 0; font: 13px/1.5 system-ui, sans-serif; background: #111; color: #ddd; }
  #panel { position: fixed; top: 0; left: 0; padding: 10px 14px; background: rgba(20,20,20,.92);
           max-width: 320px; border-radius: 0 0 8px 0; }
  #panel h3 { margin: 4px 0; }
  #detail { position: fixed; top: 0; right: 0; padding: 12px 16px; background: rgba(20,20,20,.95);
            max-width: 340px; min-width: 220px; border-radius: 0 0 0 8px; display: none; }
  #detail h3 { margin: 2px 0 8px; }
  .legend span { display: inline-block; margin-right: 10px; }
  .legend i { display: inline-block; width: 18px; height: 3px; vertical-align: middle; margin-right: 4px; }
  label { margin-right: 10px; cursor: pointer; user-select: none; }
  svg { width: 100vw; height: 100vh; }
  .muted { color: #888; }
  #search { width: 95%; margin-top: 6px; background:#222; color:#ddd; border:1px solid #444;
            border-radius:4px; padding:4px 6px; }
</style>
</head>
<body>
<div id="panel">
  <h3>AI 知识图谱 <span class="muted" id="stats"></span></h3>
  <div class="legend" id="legend"></div>
  <div id="filters"></div>
  <input id="search" placeholder="搜索节点并高亮…">
  <div class="muted">拖拽移动 · 滚轮缩放 · 点击看详情<br>虚线 = proposed（待审核）</div>
</div>
<div id="detail"></div>
<svg></svg>
<script>
const DATA = __DATA__;
const EDGE_COLORS = { is_a: "#4e9bff", part_of: "#2fbf71", prerequisite_of: "#ff8c42", related_to: "#888" };
const EDGE_LABELS = { is_a: "is_a 分类", part_of: "part_of 组成", prerequisite_of: "先修", related_to: "相关" };

const legend = d3.select("#legend");
for (const [t, c] of Object.entries(EDGE_COLORS))
  legend.append("span").html(`<i style="background:${c}"></i>${EDGE_LABELS[t]}`);

const active = new Set(Object.keys(EDGE_COLORS));
const filters = d3.select("#filters");
for (const t of Object.keys(EDGE_COLORS)) {
  const lb = filters.append("label");
  lb.append("input").attr("type", "checkbox").property("checked", true)
    .on("change", function () { this.checked ? active.add(t) : active.delete(t); refresh(); });
  lb.append("span").text(" " + EDGE_LABELS[t]);
}

const svg = d3.select("svg"), W = innerWidth, H = innerHeight;
const g = svg.append("g");
svg.call(d3.zoom().scaleExtent([0.15, 6]).on("zoom", e => g.attr("transform", e.transform)));

const nodeById = new Map(DATA.nodes.map(n => [n.id, n]));
DATA.links.forEach(l => { l.source = l.src; l.target = l.dst; });

const deg = new Map();
DATA.links.forEach(l => {
  deg.set(l.src, (deg.get(l.src) || 0) + 1);
  deg.set(l.dst, (deg.get(l.dst) || 0) + 1);
});

const sim = d3.forceSimulation(DATA.nodes)
  .force("link", d3.forceLink(DATA.links).id(d => d.id).distance(70).strength(0.35))
  .force("charge", d3.forceManyBody().strength(-220))
  .force("center", d3.forceCenter(W / 2, H / 2))
  .force("collide", d3.forceCollide().radius(d => 8 + Math.sqrt(deg.get(d.id) || 1) * 3));

svg.append("defs").selectAll("marker").data(Object.entries(EDGE_COLORS)).join("marker")
  .attr("id", d => "arrow-" + d[0]).attr("viewBox", "0 -4 8 8")
  .attr("refX", 14).attr("markerWidth", 6).attr("markerHeight", 6).attr("orient", "auto")
  .append("path").attr("d", "M0,-4L8,0L0,4").attr("fill", d => d[1]);

const link = g.append("g").selectAll("line").data(DATA.links).join("line")
  .attr("stroke", d => EDGE_COLORS[d.type]).attr("stroke-opacity", 0.6)
  .attr("stroke-width", 1.4)
  .attr("stroke-dasharray", d => d.status === "proposed" ? "4 3" : null)
  .attr("marker-end", d => `url(#arrow-${d.type})`);

const node = g.append("g").selectAll("g").data(DATA.nodes).join("g")
  .style("cursor", "pointer")
  .call(d3.drag()
    .on("start", (e, d) => { if (!e.active) sim.alphaTarget(0.25).restart(); d.fx = d.x; d.fy = d.y; })
    .on("drag", (e, d) => { d.fx = e.x; d.fy = e.y; })
    .on("end", (e, d) => { if (!e.active) sim.alphaTarget(0); d.fx = d.fy = null; }));

node.append("circle")
  .attr("r", d => 5 + Math.sqrt(deg.get(d.id) || 1) * 2.2)
  .attr("fill", d => d.status === "proposed" ? "#c9b458" : "#7fb3ff")
  .attr("stroke", "#111").attr("stroke-width", 1.2);

node.append("text").text(d => d.name)
  .attr("dx", 9).attr("dy", 4).attr("fill", "#ccc").style("font-size", "11px");

node.on("click", (e, d) => {
  const el = d3.select("#detail").style("display", "block");
  el.html(`<h3>${d.name}</h3>
    <div class="muted">${(d.aliases || []).join("、") || ""}</div>
    <p>${d.definition || "（无定义）"}</p>
    ${d.facets && d.facets.length ? "<b>facets:</b> " + d.facets.join("、") : ""}
    <div class="muted">status: ${d.status} · source: ${d.source}</div>`);
});
svg.on("click", e => { if (e.target.tagName === "svg") d3.select("#detail").style("display", "none"); });

d3.select("#search").on("input", function () {
  const q = this.value.trim().toLowerCase();
  node.select("circle").attr("stroke", d =>
    q && (d.name.toLowerCase().includes(q) ||
          (d.aliases || []).some(a => a.toLowerCase().includes(q))) ? "#ff5252" : "#111")
    .attr("stroke-width", d =>
      q && d.name.toLowerCase().includes(q) ? 3 : 1.2);
});

function refresh() {
  link.style("display", d => active.has(d.type) ? null : "none");
}

sim.on("tick", () => {
  link.attr("x1", d => d.source.x).attr("y1", d => d.source.y)
      .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
  node.attr("transform", d => `translate(${d.x},${d.y})`);
});

d3.select("#stats").text(`${DATA.nodes.length} 节点 / ${DATA.links.length} 边`);
</script>
</body>
</html>
"""


def export_html(conn, out_path: str, include_proposed=True) -> str:
    statuses = set(db.visible_statuses())
    if include_proposed:
        statuses.add("proposed")
    nodes = [{
        "id": n["id"], "name": n["name"], "aliases": n["aliases"],
        "definition": n["definition"], "facets": n["facets"],
        "status": n["status"], "source": n["source"],
    } for n in db.list_nodes(conn) if n["status"] in statuses]
    ids = {n["id"] for n in nodes}
    links = [{
        "src": e["src"], "dst": e["dst"], "type": e["type"], "status": e["status"],
    } for e in db.list_edges(conn)
        if e["status"] in statuses and e["src"] in ids and e["dst"] in ids]

    html = TEMPLATE.replace("__DATA__", json.dumps(
        {"nodes": nodes, "links": links}, ensure_ascii=False))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path
