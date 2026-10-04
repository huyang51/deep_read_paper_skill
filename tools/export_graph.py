"""Export the vault relation graph as a standalone interactive HTML page.

One self-contained file — no CDN, no server — that renders the papers and
their declared ``relations`` as a force-directed SVG graph:

  * edge color = relation type (method_similar / problem_related /
    complementary / evolutionary), with a clickable legend that hides a type;
  * arrows = direction: a ``predecessor``/``successor`` declaration draws one
    arrowhead pointing later → earlier; a ``peer`` declaration draws a dashed
    edge with arrowheads on both ends (parallel work, no influence order);
  * mutual declarations (A lists B, B lists A) collapse into one edge;
  * clicking a node opens a side panel with the paper's relations in the
    shared Chinese vocabulary (RELATION_TYPE_CN / DIRECTION_CN).

Usage:
    python tools/export_graph.py [--vault PATH] [-o out/graph.html]

Exits 0 on success. Papers whose ``relations`` point at missing ids are
reported to stderr and skipped on the edges (the nodes still render).
"""
import argparse
import json
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows (GBK console mangles CJK + arrows): the
# dead-target / duplicate-id warnings here carry paper file names.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))

from mcp_server.markdown_parser import coerce_id, get_all_papers  # noqa: E402
from mcp_server.relations import DIRECTION_CN, RELATION_TYPE_CN, relations_of  # noqa: E402

# Colors are fixed (not theme tokens) so edge color stays a reliable legend key
# in both light and dark mode.
TYPE_COLORS = {
    "method_similar": "#4c8fd6",
    "problem_related": "#d68f3a",
    "complementary": "#4da56b",
    "evolutionary": "#9a6fd0",
}
FALLBACK_COLOR = "#8a93a0"


def _label(paper: dict) -> str:
    """Graph label: file stem (the wikilink-resolvable form) or short_name."""
    f = paper.get("file", "")
    if f:
        return Path(f).stem
    return paper.get("short_name", "")


def build_graph_data(papers: list) -> tuple:
    """(nodes, edges, dead_targets, duplicate_ids) — JSON-ready structures.

    duplicate_ids lists ``(pid, dropped_file, kept_file)``: two papers sharing
    an id overwrite each other in the index, and the dropped file's relations
    would vanish from the page without a word — the same detection
    get_paper_by_id already warns about, so main() prints it.
    """
    index = {}
    dupes = []
    for p in papers:
        pid = coerce_id(p.get("id"))
        if pid is not None:
            if pid in index:
                dupes.append((pid, index[pid].get("file", "?"), p.get("file", "?")))
            index[pid] = p

    nodes = []
    for pid, p in index.items():
        nodes.append({
            "id": pid,
            "label": _label(p) or f"#{pid}",
            "title": p.get("title", ""),
            "year": p.get("year", ""),
            "venue": p.get("venue", ""),
        })

    edges = {}
    dead = set()
    for pid, p in index.items():
        for entry in relations_of(p):
            tid = coerce_id(entry.get("target"))
            if tid is None or tid == pid:
                continue
            if tid not in index:
                dead.add(tid)
                continue
            rtype = entry.get("type", "")
            direction = entry.get("direction") or ""
            # normalize_relations always emits a direction key ("" when the
            # frontmatter omitted it), so an earlier `get(..., "peer")` default
            # never fired: missing and typo'd directions silently drew a
            # one-way arrow. Only the two directed words may create one —
            # anything else renders as an undirected peer line
            # (validate_relations is what reports the bad value).
            directed = direction in ("predecessor", "successor")
            # Arrow source→target runs later → earlier: "B is my predecessor"
            # means the arrow leaves me (the later paper) and lands on B.
            if direction == "successor":
                src, dst = tid, pid
            else:
                src, dst = pid, tid
            key = (min(src, dst), max(src, dst))
            edge = edges.setdefault(key, {
                "source": src, "target": dst, "types": [],
                "peer": not directed, "notes": [],
            })
            if directed:
                # If the peer half of the declaration created this edge first,
                # flipping only the flag would keep the stale src/dst — the
                # arrow would render the wrong way round. The directed
                # declaration carries the direction, so it owns both.
                edge["peer"] = False
                edge["source"], edge["target"] = src, dst
            if rtype and rtype not in edge["types"]:
                edge["types"].append(rtype)
            note = entry.get("note", "")
            if note and note not in edge["notes"]:
                edge["notes"].append(note)
    edge_list = sorted(edges.values(), key=lambda e: (e["source"], e["target"]))
    return nodes, edge_list, dead, dupes


_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>论文关系图谱</title>
<style>
:root{--fg:#24262b;--fg-dim:#5d6570;--bg:#f7f6f2;--panel:#ffffff;--rule:#e3e0d8;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--fg:#d9dce2;--fg-dim:#98a1ad;--bg:#171a20;--panel:#1e222a;--rule:#333945;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
  background:var(--bg);color:var(--fg);overflow:hidden}
#stage{position:fixed;inset:0}
svg{width:100%;height:100%;display:block;cursor:grab}
svg.panning{cursor:grabbing}
.edge{fill:none;stroke-width:1.6;opacity:.75}
.edge.peer{stroke-dasharray:5 4;opacity:.6}
.edge.dim{opacity:.08}
text.lbl{fill:var(--fg);font-size:11px;text-anchor:middle;pointer-events:none;
  paint-order:stroke;stroke:var(--bg);stroke-width:3px}
g.node{cursor:pointer}
g.node circle{stroke:var(--bg);stroke-width:2px}
g.node:hover circle{stroke:var(--accent,#33517d)}
g.node.dim{opacity:.12}
g.node.selected circle{stroke:var(--accent,#33517d);stroke-width:3px}
#legend{position:fixed;left:16px;top:16px;background:var(--panel);
  border:1px solid var(--rule);border-radius:10px;padding:10px 14px;
  box-shadow:0 2px 10px rgba(0,0,0,.08)}
#legend h2{margin:0 0 6px;font-size:12px;letter-spacing:.1em;color:var(--fg-dim)}
#legend .item{display:flex;align-items:center;gap:7px;cursor:pointer;
  user-select:none;padding:1px 0}
#legend .item.off{opacity:.35}
#legend .sw{width:18px;height:3px;border-radius:2px}
#legend .cnt{color:var(--fg-dim);font-size:12px}
#panel{position:fixed;right:16px;top:16px;width:320px;max-height:calc(100vh - 32px);
  overflow:auto;background:var(--panel);border:1px solid var(--rule);
  border-radius:10px;padding:14px 16px;box-shadow:0 2px 10px rgba(0,0,0,.08)}
#panel h2{margin:0 0 2px;font-size:16px;line-height:1.4}
#panel .meta{color:var(--fg-dim);font-size:12px;margin-bottom:8px}
#panel .rel{border-left:3px solid var(--rule);padding:2px 0 2px 9px;margin:6px 0;font-size:13px}
#panel .rel .note{color:var(--fg-dim)}
#panel .close{float:right;border:none;background:none;color:var(--fg-dim);
  font-size:16px;cursor:pointer;padding:0 2px}
#hint{position:fixed;left:16px;bottom:14px;color:var(--fg-dim);font-size:12px}
@media (max-width:640px){#panel{left:16px;width:auto;top:auto;bottom:16px;max-height:45vh}}
</style>
</head>
<body>
<div id="stage"></div>
<div id="legend"><h2>关系类型</h2></div>
<div id="panel" hidden></div>
<div id="hint">拖拽节点调整 · 滚轮缩放 · 拖空白处平移 · 点节点看关系</div>
<script id="graph-data" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('graph-data').textContent);
const N = DATA.nodes.length;
const byId = {};
DATA.nodes.forEach(n => byId[n.id] = n);

// ---- palette / legend state -------------------------------------------
const TYPES = [...new Set(DATA.edges.flatMap(e => e.types))];
const typeColor = t => __TYPE_COLORS__[t] || '__FALLBACK__';
const typeName = t => __TYPE_NAMES__[t] || t;
const off = new Set();

// ---- legend ------------------------------------------------------------
const legend = document.getElementById('legend');
TYPES.forEach(t => {
  const c = DATA.edges.filter(e => e.types.includes(t)).length;
  const item = document.createElement('div');
  item.className = 'item';
  item.innerHTML = '<span class="sw" style="background:' + typeColor(t) +
    '"></span>' + typeName(t) + ' <span class="cnt">(' + c + ')</span>';
  item.onclick = () => { off.has(t) ? off.delete(t) : off.add(t);
    item.classList.toggle('off'); applyFilter(); };
  legend.appendChild(item);
});

// ---- SVG scaffolding ---------------------------------------------------
const NS = 'http://www.w3.org/2000/svg';
const svg = document.createElementNS(NS, 'svg');
const root = document.createElementNS(NS, 'g');   // pan/zoom transform target
svg.appendChild(root);
document.getElementById('stage').appendChild(svg);

TYPES.forEach(t => {  // one arrow marker per type color
  const m = document.createElementNS(NS, 'marker');
  m.setAttribute('id', 'ar-' + t);
  m.setAttribute('viewBox', '0 0 10 10');
  m.setAttribute('refX', '9'); m.setAttribute('refY', '5');
  m.setAttribute('markerWidth', '7'); m.setAttribute('markerHeight', '7');
  m.setAttribute('orient', 'auto-start-reverse');
  const p = document.createElementNS(NS, 'path');
  p.setAttribute('d', 'M0,0 L10,5 L0,10 z');
  p.setAttribute('fill', typeColor(t));
  m.appendChild(p); svg.appendChild(m);
});

const edgeG = document.createElementNS(NS, 'g');
const nodeG = document.createElementNS(NS, 'g');
root.appendChild(edgeG); root.appendChild(nodeG);

// ---- edges -------------------------------------------------------------
DATA.edges.forEach(e => {
  e.el = document.createElementNS(NS, 'line');
  e.el.setAttribute('class', 'edge' + (e.peer ? ' peer' : ''));
  const t = e.types[0];
  e.el.setAttribute('stroke', typeColor(t));
  if (e.peer) {           // parallel work: arrows on both ends, no order
    e.el.setAttribute('marker-start', 'url(#ar-' + t + ')');
    e.el.setAttribute('marker-end', 'url(#ar-' + t + ')');
  } else {                // later → earlier, one arrowhead
    e.el.setAttribute('marker-end', 'url(#ar-' + t + ')');
  }
  edgeG.appendChild(e.el);
});

// ---- nodes -------------------------------------------------------------
DATA.edges.forEach(e => {
  (byId[e.source].adj = byId[e.source].adj || []).push(e);
  (byId[e.target].adj = byId[e.target].adj || []).push(e);
});
const radius = n => 7 + Math.min((n.adj || []).length, 6) * 1.5;

DATA.nodes.forEach(n => {
  n.g = document.createElementNS(NS, 'g');
  n.g.setAttribute('class', 'node');
  n.g.appendChild(Object.assign(document.createElementNS(NS, 'circle'),
    {r: radius(n), fill: '#33517d'}));
  n.lbl = document.createElementNS(NS, 'text');
  n.lbl.setAttribute('class', 'lbl');
  n.lbl.setAttribute('y', radius(n) + 13);
  n.lbl.textContent = n.label;
  n.g.appendChild(n.lbl);
  n.g.addEventListener('click', ev => { ev.stopPropagation(); select(n); });
  nodeG.appendChild(n.g);
});

// ---- force simulation --------------------------------------------------
const W = () => svg.clientWidth || 800, H = () => svg.clientHeight || 600;
DATA.nodes.forEach((n, i) => {   // deterministic spiral start — no jitter luck
  const a = 2.4 * i, r = 30 + 14 * Math.sqrt(i);
  n.x = W() / 2 + r * Math.cos(a); n.y = H() / 2 + r * Math.sin(a);
  n.vx = n.vy = 0;
});
let alpha = 1, pinned = null;
function tick() {
  for (let i = 0; i < N; i++) {           // pairwise repulsion (small n)
    for (let j = i + 1; j < N; j++) {
      const a = DATA.nodes[i], b = DATA.nodes[j];
      let dx = a.x - b.x, dy = a.y - b.y;
      let d2 = dx * dx + dy * dy || 1;
      const f = 1800 * alpha / d2, d = Math.sqrt(d2);
      dx /= d; dy /= d;
      a.vx += dx * f; a.vy += dy * f;
      b.vx -= dx * f; b.vy -= dy * f;
    }
  }
  DATA.edges.forEach(e => {               // springs toward rest length
    const a = byId[e.source], b = byId[e.target];
    const dx = b.x - a.x, dy = b.y - a.y;
    const d = Math.sqrt(dx * dx + dy * dy) || 1;
    // Linear Hooke's law. The old extra `* d * 0.01` made F ∝ (d-150)·d:
    // with ~150 nodes a stray wide pair fed back quadratically and every
    // coordinate went Infinity→NaN by frame ~20 (blank canvas). tests/
    // test_export_graph.py runs THIS tick under node to keep it provably
    // stable at vault sizes the 2-node real vault never exercised.
    const f = (d - 150) * 0.02 * alpha;
    a.vx += dx / d * f; a.vy += dy / d * f;
    b.vx -= dx / d * f; b.vy -= dy / d * f;
  });
  DATA.nodes.forEach(n => {
    n.vx += (W() / 2 - n.x) * 0.003 * alpha;   // gentle gravity
    n.vy += (H() / 2 - n.y) * 0.003 * alpha;
    if (n !== pinned) { n.x += n.vx *= .82; n.y += n.vy *= .82; }
    else { n.x = pinned.x; n.y = pinned.y; }
  });
  alpha *= .992;
}
function frame() {
  for (let k = 0; k < 3 && alpha > .01; k++) tick();
  DATA.edges.forEach(e => {
    const a = byId[e.source], b = byId[e.target];
    e.el.setAttribute('x1', a.x); e.el.setAttribute('y1', a.y);
    e.el.setAttribute('x2', b.x); e.el.setAttribute('y2', b.y);
  });
  DATA.nodes.forEach(n => n.g.setAttribute('transform',
    'translate(' + n.x + ',' + n.y + ')'));
  requestAnimationFrame(frame);
}
frame();

// ---- drag / pan / zoom -------------------------------------------------
let drag = null, pan = null, view = {x: 0, y: 0, k: 1};
function applyView() {
  root.setAttribute('transform',
    'translate(' + view.x + ',' + view.y + ') scale(' + view.k + ')');
}
svg.addEventListener('pointerdown', ev => {
  const g = ev.target.closest('g.node');
  if (g) { drag = DATA.nodes.find(n => n.g === g); pinned = drag; alpha = Math.max(alpha, .3); }
  else { pan = {x: ev.clientX - view.x, y: ev.clientY - view.y}; svg.classList.add('panning'); }
});
window.addEventListener('pointermove', ev => {
  if (drag) {
    drag.x = (ev.clientX - view.x) / view.k;
    drag.y = (ev.clientY - view.y) / view.k;
  } else if (pan) { view.x = ev.clientX - pan.x; view.y = ev.clientY - pan.y; applyView(); }
});
window.addEventListener('pointerup', () => { drag = null; pan = null; svg.classList.remove('panning'); });
svg.addEventListener('wheel', ev => {
  ev.preventDefault();
  const f = ev.deltaY < 0 ? 1.12 : 1 / 1.12;
  const k2 = Math.max(.3, Math.min(4, view.k * f));
  view.x = ev.clientX - (ev.clientX - view.x) * k2 / view.k;
  view.y = ev.clientY - (ev.clientY - view.y) * k2 / view.k;
  view.k = k2; applyView();
}, {passive: false});
svg.addEventListener('click', () => select(null));

// ---- filtering + info panel -------------------------------------------
function applyFilter() {
  DATA.edges.forEach(e => e.el.classList.toggle('dim',
    e.types.some(t => off.has(t))));
  DATA.nodes.forEach(n => {
    const linked = (n.adj || []).some(e => !e.types.some(t => off.has(t)));
    n.g.classList.toggle('dim', N > 1 && !linked);
  });
}
const panel = document.getElementById('panel');
let selected = null;
function select(n) {
  if (selected) selected.g.classList.remove('selected');
  selected = n;
  if (!n) { panel.hidden = true; return; }
  n.g.classList.add('selected');
  const rels = [];
  (n.adj || []).forEach(e => {
    const other = byId[e.source === n.id ? e.target : e.source];
    const dir = e.peer ? '__PEER_CN__'
      : (e.source === n.id
        ? '本文建立于它之上（前作）' : '它建立于本文之上（后继）');
    e.types.forEach(t => rels.push(
      '<div class="rel" style="border-left-color:' + typeColor(t) + '">' +
      typeName(t) + ' · ' + dir + '<br><b>' + esc(other.title || other.label) +
      '</b>' + (other.year ? ' (' + esc(String(other.year)) + ')' : '') +
      (e.notes[0] ? '<div class="note">' + esc(e.notes[0]) + '</div>' : '') +
      '</div>'));
  });
  panel.innerHTML = '<button class="close" aria-label="关闭">✕</button>' +
    '<h2>' + esc(n.title || n.label) + '</h2>' +
    '<div class="meta">' +
    [n.label !== (n.title || '') ? n.label : '',
     n.year ? esc(String(n.year)) : '', n.venue ? esc(n.venue) : '']
      .filter(Boolean).join(' · ') + '</div>' +
    (rels.length ? rels.join('') : '<div class="meta">暂无已声明的关系</div>');
  panel.hidden = false;
  panel.querySelector('.close').onclick = () => select(null);
}
function esc(s) {
  return String(s).replace(/[&<>"]/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
</script>
</body>
</html>
"""


def render_html(nodes: list, edges: list) -> str:
    """Serialize the page — data inlined, palettes/names spliced as JSON."""
    data = json.dumps({"nodes": nodes, "edges": edges},
                      ensure_ascii=False, separators=(",", ":"))
    # A literal `</` (e.g. a title holding "</script>") would terminate the
    # script element during HTML parsing and truncate the payload; `<\/` is
    # the same string to a JSON parser.
    data = data.replace("</", "<\\/")
    html = _HTML.replace("__DATA__", data)
    html = html.replace("__TYPE_COLORS__", json.dumps(TYPE_COLORS))
    html = html.replace("__TYPE_NAMES__",
                        json.dumps(RELATION_TYPE_CN, ensure_ascii=False))
    html = html.replace("__PEER_CN__", DIRECTION_CN["peer"])
    html = html.replace("__FALLBACK__", FALLBACK_COLOR)
    return html


def main(argv=None):
    ap = argparse.ArgumentParser(description="导出独立交互式论文关系图谱页")
    ap.add_argument("--vault", default="knowledge-base",
                    help="vault 根目录（默认 ./knowledge-base）")
    ap.add_argument("-o", "--output", default="output/graph.html",
                    help="输出 HTML 路径（默认 output/graph.html）")
    args = ap.parse_args(argv)

    vault = Path(args.vault)
    papers = get_all_papers(vault / "papers")
    if not papers:
        print(f"vault 为空或不存在：{vault}", file=sys.stderr)
        return 1

    nodes, edges, dead, dupes = build_graph_data(papers)
    for tid in sorted(dead):
        print(f"警告：relations 指向不存在的论文 id={tid}，相应边已跳过"
              "（可运行 tools/verify_graph_arrows.py 查看）", file=sys.stderr)
    for pid, dropped, kept in dupes:
        print(f"警告：论文 id={pid} 重复（{dropped} 被 {kept} 覆盖），"
              "被覆盖文件的 relations 不会出现在图中——先修复 id 再重导出",
              file=sys.stderr)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(nodes, edges), encoding="utf-8")
    print(f"图谱已导出：{out}（{len(nodes)} 篇论文，{len(edges)} 条边，"
          f"浏览器直接打开即可）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
