"""Tests for tools/export_graph.py — the standalone HTML graph page.

The Python side is pure data-shaping (nodes, deduped edges, dead-target
reporting); the page must carry the structural hooks the JS relies on
(embedded JSON, per-type arrow markers, peer dash class) — a regression in
either half makes the exported page lie quietly.
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
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


class BuildGraphDataTest(unittest.TestCase):
    def test_mutual_declarations_collapse_to_one_edge(self):
        papers = [_paper(1, relations=[
                       {"target": 2, "type": "method_similar",
                        "direction": "successor", "note": "n1"}]),
                  _paper(2, relations=[
                       {"target": 1, "type": "method_similar",
                        "direction": "predecessor", "note": "n2"}])]
        nodes, edges, dead = eg.build_graph_data(papers)
        self.assertEqual([n["id"] for n in nodes], [1, 2])
        self.assertEqual(len(edges), 1)
        e = edges[0]
        self.assertEqual((e["source"], e["target"]), (2, 1))  # later → earlier
        self.assertFalse(e["peer"])
        self.assertEqual(e["types"], ["method_similar"])

    def test_peer_edge_stays_dashed_bidirectional(self):
        papers = [_paper(1, relations=[
                       {"target": 2, "type": "problem_related",
                        "direction": "peer"}]),
                  _paper(2)]
        _, edges, _ = eg.build_graph_data(papers)
        self.assertTrue(edges[0]["peer"])
        self.assertEqual(edges[0]["source"], 1)  # no direction: as declared

    def test_dead_target_reported_and_edge_skipped(self):
        papers = [_paper(1, relations=[
                       {"target": 99, "type": "complementary",
                        "direction": "peer"}])]
        nodes, edges, dead = eg.build_graph_data(papers)
        self.assertEqual(len(edges), 0)
        self.assertEqual(dead, {99})
        self.assertEqual(len(nodes), 1)  # the node still renders

    def test_label_uses_file_stem_not_short_name(self):
        papers = [_paper(1, short_name="FLMR v2")]
        papers[0]["file"] = "FLMR-v2.md"
        nodes, _, _ = eg.build_graph_data(papers)
        self.assertEqual(nodes[0]["label"], "FLMR-v2")


class RenderHtmlTest(unittest.TestCase):
    def test_page_carries_data_markers_and_colors(self):
        papers = [_paper(1, relations=[
                       {"target": 2, "type": "method_similar",
                        "direction": "predecessor"}]),
                  _paper(2, relations=[
                       {"target": 3, "type": "problem_related",
                        "direction": "peer"}]),
                  _paper(3)]
        nodes, edges, _ = eg.build_graph_data(papers)
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
        nodes, edges, _ = eg.build_graph_data(papers)
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
            page = out.read_text(encoding="utf-8")
            payload = json.loads(page.split(
                'type="application/json">')[1].split("</script>")[0])
            self.assertEqual(len(payload["nodes"]), 2)
            self.assertEqual(len(payload["edges"]), 1)

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
