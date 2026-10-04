"""Tests for tools/export_graph.py — the standalone HTML graph page.

The Python side is pure data-shaping (nodes, deduped edges, dead-target
reporting); the page must carry the structural hooks the JS relies on
(embedded JSON, per-type arrow markers, peer dash class) — a regression in
either half makes the exported page lie quietly.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

import export_graph as eg  # noqa: E402


def _paper(pid, short_name="P", body="", relations=None, year=2020, related=None):
    return {"id": pid, "title": f"Paper {pid}", "short_name": short_name,
            "year": year, "venue": "V", "file": f"{short_name}.md",
            "body": body, "related_papers": related or [],
            "relations": relations or []}


class UntypedEdgeMarkerTest(unittest.TestCase):
    """A directed edge with an EMPTY relation type used to resolve
    url(#ar-undefined) — SVG draws no arrowhead, silently, so the declared
    direction vanished from the page."""

    def test_js_wires_a_real_marker_for_the_untyped_case(self):
        self.assertIn("markerKey", eg._HTML)
        self.assertIn("__untyped__", eg._HTML)


class VaultDefaultTest(unittest.TestCase):
    """--vault used to default to ./knowledge-base relative to CWD and ignored
    settings.json / PAPER_KB_VAULT_DIR entirely — on a configured machine the
    README's own command exited 1 with 「vault 为空或不存在」 while every
    sibling tool (verify_graph_arrows, migrate_relations, index_paper) worked."""

    def test_env_vault_is_used_when_flag_is_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "kb"
            (vault / "papers").mkdir(parents=True)
            (vault / "papers" / "A.md").write_text(
                "---\nid: 1\ntitle: Alpha\nshort_name: Alpha\nyear: 2020\n"
                "---\n\n正文。\n", encoding="utf-8")
            out = vault / "graph.html"
            env = dict(os.environ, PAPER_KB_VAULT_DIR=str(vault))
            r = subprocess.run([sys.executable, str(REPO / "tools" / "export_graph.py"),
                                "-o", str(out)],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace", env=env,
                               cwd=str(Path(tmp)) )  # NOT under a knowledge-base
            self.assertEqual(r.returncode, 0, msg=r.stdout + r.stderr)
            self.assertTrue(out.is_file())


class BuildGraphDataTest(unittest.TestCase):
    def test_mutual_declarations_collapse_to_one_edge(self):
        papers = [_paper(1, relations=[
                       {"target": 2, "type": "method_similar",
                        "direction": "successor", "note": "n1"}]),
                  _paper(2, relations=[
                       {"target": 1, "type": "method_similar",
                        "direction": "predecessor", "note": "n2"}])]
        nodes, edges, dead, dupes = eg.build_graph_data(papers)
        self.assertEqual([n["id"] for n in nodes], [1, 2])
        self.assertEqual(len(edges), 1)
        e = edges[0]
        self.assertEqual((e["source"], e["target"]), (1, 2))  # earlier → later
        self.assertFalse(e["peer"])
        self.assertEqual(e["types"], ["method_similar"])

    def test_peer_edge_stays_dashed_bidirectional(self):
        papers = [_paper(1, relations=[
                       {"target": 2, "type": "problem_related",
                        "direction": "peer"}]),
                  _paper(2)]
        _, edges, _, _ = eg.build_graph_data(papers)
        self.assertTrue(edges[0]["peer"])
        self.assertEqual(edges[0]["source"], 1)  # no direction: as declared

    def test_dead_target_reported_and_edge_skipped(self):
        papers = [_paper(1, relations=[
                       {"target": 99, "type": "complementary",
                        "direction": "peer"}])]
        nodes, edges, dead, dupes = eg.build_graph_data(papers)
        self.assertEqual(len(edges), 0)
        self.assertEqual(dead, {99})
        self.assertEqual(len(nodes), 1)  # the node still renders

    def test_label_uses_file_stem_not_short_name(self):
        papers = [_paper(1, short_name="FLMR v2")]
        papers[0]["file"] = "FLMR-v2.md"
        nodes, _, _, _ = eg.build_graph_data(papers)
        self.assertEqual(nodes[0]["label"], "FLMR-v2")

    def test_self_loop_is_skipped(self):
        """A relation targeting the declaring paper is noise, not an edge —
        and must not count as a dead target either."""
        papers = [_paper(1, relations=[{"target": 1, "type": "complementary",
                                        "direction": "peer"}]),
                  _paper(2)]
        nodes, edges, dead, _ = eg.build_graph_data(papers)
        self.assertEqual(edges, [])
        self.assertEqual(dead, set())

    def test_unknown_type_survives_with_fallback_color(self):
        """validate_relations reports bad types; the page must still render —
        the JS looks the type up with a `|| __FALLBACK__` and the raw value
        stays visible in the panel so the user can find the typo."""
        papers = [_paper(1, relations=[{"target": 2, "type": "fancy_new",
                                        "direction": "successor"}]),
                  _paper(2)]
        nodes, edges, _, _ = eg.build_graph_data(papers)
        self.assertEqual(edges[0]["types"], ["fancy_new"])
        html = eg.render_html(nodes, edges)
        self.assertIn(eg.FALLBACK_COLOR, html)

    def test_duplicate_ids_are_reported_not_swallowed(self):
        """Regression: the index build silently overwrote a repeated id, so the
        dropped file's relations vanished from the page without a word."""
        papers = [_paper(1, relations=[{"target": 2, "type": "problem_related",
                                        "direction": "peer"}]),
                  _paper(1)]
        papers[0]["file"] = "First.md"
        papers[1]["file"] = "Second.md"
        nodes, _, _, dupes = eg.build_graph_data(papers)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(dupes, [(1, "First.md", "Second.md")])

    def test_missing_direction_renders_peer_not_an_arrow(self):
        """Regression: normalize_relations always emits a direction key (""
        when omitted), so the old get(..., "peer") default never fired and an
        undeclared direction silently drew a one-way arrow."""
        papers = [_paper(1, relations=[{"target": 2, "type": "complementary",
                                        "direction": "", "note": ""}]),
                  _paper(2)]
        _, edges, _, _ = eg.build_graph_data(papers)
        self.assertTrue(edges[0]["peer"])

    def test_typo_direction_falls_back_to_undirected(self):
        papers = [_paper(1, relations=[{"target": 2, "type": "method_similar",
                                        "direction": "upstream"}]),
                  _paper(2)]
        _, edges, _, _ = eg.build_graph_data(papers)
        self.assertTrue(edges[0]["peer"])

    def test_directed_declaration_on_one_side_still_wins(self):
        papers = [_paper(1, relations=[{"target": 2, "type": "evolutionary",
                                        "direction": ""}]),
                  _paper(2, relations=[{"target": 1, "type": "evolutionary",
                                        "direction": "predecessor"}])]
        _, edges, _, _ = eg.build_graph_data(papers)
        self.assertEqual(len(edges), 1)
        self.assertFalse(edges[0]["peer"])
        # earlier → later: paper 2 says "1 is my predecessor", so the arrow
        # leaves 1 and lands on 2 (2026-10-04: matches Obsidian's old→new rule)
        self.assertEqual((edges[0]["source"], edges[0]["target"]), (1, 2))


class RenderHtmlTest(unittest.TestCase):
    def test_page_carries_data_markers_and_colors(self):
        papers = [_paper(1, relations=[
                       {"target": 2, "type": "method_similar",
                        "direction": "predecessor"}]),
                  _paper(2, relations=[
                       {"target": 3, "type": "problem_related",
                        "direction": "peer"}]),
                  _paper(3)]
        nodes, edges, _, _ = eg.build_graph_data(papers)
        html = eg.render_html(nodes, edges)
        self.assertNotIn("__DATA__", html)          # every placeholder spliced
        payload = json.loads(html.split(
            'type="application/json">')[1].split("</script>")[0])
        self.assertEqual(len(payload["nodes"]), 3)
        self.assertEqual(len(payload["edges"]), 2)
        for t, color in eg.TYPE_COLORS.items():     # marker per type color
            self.assertIn(color, html)
        self.assertIn("url(#ar-", html)             # JS-composed marker refs
        self.assertIn("'ar-' + t", html)
        self.assertIn(".edge.peer", html)           # dashed rule for peer
        self.assertIn("auto-start-reverse", html)   # double-arrow support

    def test_placeholder_splice_is_js_safe(self):
        # A title containing the JSON terminator or </script> must not break
        # the embedded payload's parsing.
        papers = [_paper(1)]
        papers[0]["title"] = 'ends with </script> and "quotes"'
        nodes, edges, _, _ = eg.build_graph_data(papers)
        html = eg.render_html(nodes, edges)
        payload = json.loads(html.split(
            'type="application/json">')[1].split("</script>")[0])
        self.assertIn("</script>", payload["nodes"][0]["title"])


class CliTest(unittest.TestCase):
    def test_main_writes_page_and_reports_dead_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "kb"
            (vault / "papers").mkdir(parents=True)
            (vault / "papers" / "A.md").write_text(
                "---\nid: 1\ntitle: Alpha\nyear: 2024\nshort_name: Alpha\n"
                "relations:\n  - target: 2\n    type: method_similar\n"
                "    direction: successor\n"
                "  - target: 404\n    type: complementary\n    direction: peer\n"
                "---\n\n正文\n", encoding="utf-8")
            (vault / "papers" / "B.md").write_text(
                "---\nid: 2\ntitle: Beta\nyear: 2025\nshort_name: Beta\n---\n\nB\n",
                encoding="utf-8")
            out = Path(tmp) / "graph.html"
            err = io.StringIO()
            saved_err = sys.stderr
            sys.stderr = err
            try:
                code = eg.main(["--vault", str(vault), "-o", str(out)])
            finally:
                sys.stderr = saved_err
            self.assertEqual(code, 0)
            self.assertTrue(out.exists())
            self.assertIn("404", err.getvalue())

    def test_duplicate_id_warning_reaches_cli_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "kb"
            (vault / "papers").mkdir(parents=True)
            for name in ("A.md", "B.md"):
                (vault / "papers" / name).write_text(
                    f"---\nid: 1\ntitle: {name}\nshort_name: {name[:1]}\n"
                    "---\n\nB\n", encoding="utf-8")
            out = Path(tmp) / "graph.html"
            err = io.StringIO()
            saved_err, sys.stderr = sys.stderr, err
            try:
                code = eg.main(["--vault", str(vault), "-o", str(out)])
            finally:
                sys.stderr = saved_err
            self.assertEqual(code, 0)
            self.assertIn("重复", err.getvalue())
            page = out.read_text(encoding="utf-8")
            payload = json.loads(page.split(
                'type="application/json">')[1].split("</script>")[0])
            # one identity per id: relations target ids, two coexisting nodes
            # would make every edge ambiguous — the duplicate must be loud,
            # not graphed twice.
            self.assertEqual(len(payload["nodes"]), 1)
            self.assertEqual(len(payload["edges"]), 0)

    def test_main_empty_vault_is_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            saved_err = sys.stderr
            sys.stderr = err
            try:
                code = eg.main(["--vault", tmp])
            finally:
                sys.stderr = saved_err
            self.assertEqual(code, 1)
            self.assertIn("vault 为空", err.getvalue())


class SimulationStabilityTest(unittest.TestCase):
    """Run the shipped force simulation under node, at vault scale.

    Regression: the spring carried an extra `* d * 0.01`, so F ∝ (d-150)·d —
    a quadratic feedback that is harmless in a 2-paper vault but sent every
    coordinate to Infinity→NaN by frame ~20 at ~150 nodes, blanking the page.
    tick() is lifted VERBATIM from _HTML: a coefficient change to the shipped
    JS fails this test, which unit-side data tests cannot see.
    """

    @staticmethod
    def _tick_source():
        m = re.search(r"function tick\(\) \{.*?\n\}(?=\nfunction frame)",
                      eg._HTML, re.S)
        if not m:
            raise AssertionError("tick() no longer findable in _HTML — "
                                 "update this extractor with the template")
        return m.group(0)

    SCRIPT = """
const N = 150, DATA = {nodes: [], edges: []};
let seed = 12345;
const rnd = () => { seed = (seed * 1103515245 + 12345) & 0x7fffffff;
                    return seed / 0x7fffffff; };
for (let i = 0; i < N; i++)
  DATA.nodes.push({id: i, x: 400 + 600 * rnd() - 300,
                   y: 300 + 400 * rnd() - 200, vx: 0, vy: 0});
for (let i = 0; i < N; i++)
  for (let k = 0, m = 2 + Math.floor(rnd() * 3); k < m; k++) {
    const j = Math.floor(rnd() * N);
    if (j !== i) DATA.edges.push({source: i, target: j});
  }
const byId = {}; DATA.nodes.forEach(n => byId[n.id] = n);
const W = () => 800, H = () => 600;
let pinned = null, alpha = 1;
__TICK__
for (let k = 0; k < 200; k++) tick();
const bad = DATA.nodes.filter(n => !Number.isFinite(n.x)
  || !Number.isFinite(n.y)).length;
const maxAbs = DATA.nodes.reduce((mx, n) =>
  Math.max(mx, Math.abs(n.x), Math.abs(n.y)), 0);
console.log(JSON.stringify({bad, maxAbs}));
"""

    def test_tick_is_stable_at_150_nodes(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not on PATH — JS side unexercised")
        script = self.SCRIPT.replace("__TICK__", self._tick_source())
        r = subprocess.run([node, "-e", script], capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        out = json.loads(r.stdout.strip())
        self.assertEqual(out["bad"], 0)
        self.assertLess(out["maxAbs"], 1e5, "layout diverged")
