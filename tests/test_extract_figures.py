"""Unit tests for tools/extract_figures.py (stdlib unittest, no pytest needed).

Run from repo root:  python -m unittest discover -s tests -v

All PDFs are synthesized with PyMuPDF at KNOWN coordinates, so "cropped in
half?" is settled by math: expected drawing rects must be fully contained in
the crop rectangle.
"""
import sys
import tempfile
import unittest
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import extract_figures as ef  # noqa: E402


# ------------------------------------------------------------ pure functions

class ClusterTest(unittest.TestCase):
    def test_merges_overlapping(self):
        out = ef.cluster_rects([(0, 0, 10, 10), (5, 5, 15, 15)], pad=2)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0], (0, 0, 15, 15))

    def test_merges_within_pad(self):
        out = ef.cluster_rects([(0, 0, 10, 10), (14, 0, 20, 10)], pad=6)
        self.assertEqual(len(out), 1)

    def test_keeps_far_apart(self):
        out = ef.cluster_rects([(0, 0, 10, 10), (40, 40, 60, 60)], pad=6)
        self.assertEqual(len(out), 2)

    def test_transitive_chain(self):
        a, b, c = (0, 0, 10, 10), (12, 0, 22, 10), (24, 0, 34, 10)
        out = ef.cluster_rects([a, c, b], pad=4)  # b bridges a and c
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0], (0, 0, 34, 10))


class CaptionTest(unittest.TestCase):
    def test_variants(self):
        cases = {
            "Figure 1: The architecture.": "1",
            "Fig. 3. Attention heads": "3",
            "Figure 4(a): left panel": "4",
            "图2：栅格示意图。": "2",
            "Figure S1: supplemental": "S1",
        }
        blocks = [(t, (10, 10, 200, 24)) for t in cases]
        caps = ef.parse_captions(blocks)
        self.assertEqual([c["key"] for c in caps], list(cases.values()))
        self.assertTrue(all(c["kind"] == "figure" for c in caps))

    def test_in_text_mention_rejected(self):
        caps = ef.parse_captions(
            [("Figure 3 shows that our method wins across all five benchmarks.",
              (10, 10, 400, 24))])
        self.assertEqual(caps, [])

    def test_table_caption_classified_as_table(self):
        caps = ef.parse_captions([("Table 1: Results on COCO.", (10, 10, 300, 24))])
        self.assertEqual([c["kind"] for c in caps], ["table"])


# ------------------------------------------------------------ fixture PDF

def _shape_rect(page, rect, color, fill):
    sh = page.new_shape()
    sh.draw_rect(fitz.Rect(rect))
    sh.finish(color=color, fill=fill, width=1.5)
    sh.commit()


def build_pdf(path):
    doc = doc = fitz.open()

    # -- page 1: vector figure + caption; paragraph below; uncaptioned rect --
    p = doc.new_page(width=595, height=842)
    p.insert_textbox(fitz.Rect(60, 40, 535, 60), "Synthetic Paper Title", fontsize=16)
    _shape_rect(p, (120, 120, 380, 300), (0, 0, 0), (1, 0.85, 0.85))
    sh = p.new_shape()
    sh.draw_line(fitz.Point(140, 150), fitz.Point(360, 270))
    sh.draw_line(fitz.Point(140, 270), fitz.Point(360, 150))
    sh.finish(color=(0, 0, 0.8), width=2)
    sh.commit()
    p.insert_textbox(fitz.Rect(110, 305, 390, 327),
                     "Figure 1: A synthetic architecture diagram.", fontsize=10)
    p.insert_textbox(fitz.Rect(60, 360, 535, 600),
                     "Lorem ipsum dolor sit amet consectetur adipiscing elit sed do "
                     "eiusmod tempor incididunt ut labore et dolore magna aliqua Ut "
                     "enim ad minim veniam quis nostrud exercitation ullamco laboris.",
                     fontsize=11)
    _shape_rect(p, (120, 640, 380, 700), (0, 0.5, 0), None)  # uncaptioned

    # -- page 2: raster figure + Chinese caption --
    p = doc.new_page(width=595, height=842)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 80, 60))
    for i in range(80):
        for j in range(60):
            pix.set_pixel(i, j, (200 if i < 40 else 30, 30, 200 if i >= 40 else 30))
    p.insert_image(fitz.Rect(100, 100, 300, 240), pixmap=pix)
    p.insert_textbox(fitz.Rect(90, 245, 310, 270), "图2：栅格示意图。",
                     fontsize=11, fontname="china-s")

    # -- page 3: table (caption above grid of rules) --
    p = doc.new_page(width=595, height=842)
    p.insert_textbox(fitz.Rect(100, 80, 500, 96), "Table 1: Results.", fontsize=10)
    sh = p.new_shape()
    for y in (100, 130, 160, 190):
        sh.draw_line(fitz.Point(100, y), fitz.Point(500, y))
    sh.finish(color=(0, 0, 0), width=1)
    sh.commit()
    p.insert_textbox(fitz.Rect(100, 105, 300, 120), "model 92.1", fontsize=10)
    p.insert_textbox(fitz.Rect(100, 135, 300, 150), "baseline 88.4", fontsize=10)

    # -- page 4: two panels sharing figure number 4 --
    p = doc.new_page(width=595, height=842)
    _shape_rect(p, (60, 100, 260, 240), (0, 0, 0), (0.9, 0.9, 1))
    _shape_rect(p, (330, 100, 530, 240), (0, 0, 0), (1, 0.95, 0.8))
    p.insert_textbox(fitz.Rect(60, 245, 260, 262), "Figure 4(a): left panel", fontsize=9)
    p.insert_textbox(fitz.Rect(330, 245, 530, 262), "Figure 4(b): right panel", fontsize=9)

    # -- page 5: caption but no artwork --
    p = doc.new_page(width=595, height=842)
    p.insert_textbox(fitz.Rect(60, 100, 535, 118),
                     "Figure 5: this caption has no artwork attached to it.",
                     fontsize=10)
    doc.save(str(path))
    doc.close()


def _is_blank(png_path):
    pix = fitz.Pixmap(str(png_path))
    samples = pix.samples
    step = max(1, len(samples) // 4000)
    return all(b >= 250 for b in samples[::step])


class ExtractIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pdf = Path(cls.tmp.name) / "synthetic.pdf"
        build_pdf(cls.pdf)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.outdir = Path(tempfile.mkdtemp(dir=self.tmp.name))

    def _extract(self, **kw):
        return ef.extract_figures(self.pdf, self.outdir, **kw)

    def _fig(self, manifest, name_part):
        hits = [f for f in manifest["figures"] if name_part in f["png"]]
        self.assertEqual(len(hits), 1, f"expected exactly one {name_part}")
        return hits[0]

    def test_vector_figure_crop_contains_all_primitives(self):
        m = self._extract()
        fig = self._fig(m, "fig1")
        self.assertEqual(fig["page"], 1)
        self.assertIn(fig["kind"], ("vector", "mixed"))
        x0, y0, x1, y1 = fig["rect_pt"]
        # every drawing rect (120,120,380,300 + lines) and the visible caption
        # text (bbox bottom ~318.7 for a 10pt font in a textbox ending at 327)
        # must be inside the crop
        self.assertLessEqual(x0, 120)
        self.assertLessEqual(y0, 120)
        self.assertGreaterEqual(x1, 380)
        self.assertGreaterEqual(y1, 318)
        png = self.outdir / fig["png"]
        self.assertTrue(png.is_file())
        self.assertFalse(_is_blank(png))
        self.assertNotIn("touches_page_bottom", fig.get("notes", []))

    def test_raster_figure_chinese_caption(self):
        m = self._extract()
        fig = self._fig(m, "fig2")
        self.assertEqual(fig["page"], 2)
        self.assertEqual(fig["kind"], "raster")
        # insert_image keeps aspect: an 80x60 pixmap in a 200x140 rect lands
        # letterboxed at x 106.67..293.33 — the crop must cover the REAL bbox
        x0, y0, x1, y1 = fig["rect_pt"]
        self.assertLessEqual(x0, 106.67)
        self.assertLessEqual(y0, 100)
        self.assertGreaterEqual(x1, 293.33)
        self.assertGreaterEqual(y1, 240)
        self.assertFalse(_is_blank(self.outdir / fig["png"]))

    def test_table_not_extracted(self):
        m = self._extract()
        self.assertEqual([f for f in m["figures"] if f["page"] == 3], [])

    def test_multi_panels_merge_into_one_crop(self):
        m = self._extract()
        hits = [f for f in m["figures"] if f["page"] == 4]
        self.assertEqual(len(hits), 1)
        x0, y0, x1, y1 = hits[0]["rect_pt"]
        self.assertLessEqual(x0, 60)
        self.assertGreaterEqual(x1, 530)

    def test_uncaptioned_cluster_skipped_by_default(self):
        m = self._extract()
        self.assertEqual([f for f in m["figures"] if f["page"] == 1
                          and "fig1" not in f["png"]], [])
        skipped = [s for s in m["skipped"]
                   if s["page"] == 1 and s["kind"] == "uncaptioned_cluster"]
        self.assertEqual(len(skipped), 1)
        self.assertAlmostEqual(skipped[0]["rect_pt"][1], 640, places=0)

    def test_include_uncaptioned_exports_extra(self):
        m = self._extract(include_uncaptioned=True)
        extra = [f for f in m["figures"] if f["page"] == 1 and "_u" in f["png"]]
        self.assertEqual(len(extra), 1)

    def test_caption_without_figure_recorded(self):
        m = self._extract()
        self.assertEqual([f for f in m["figures"] if f["page"] == 5], [])
        self.assertIn("caption_without_figure",
                      {s["kind"] for s in m["skipped"] if s["page"] == 5})

    def test_pages_filter(self):
        m = self._extract(pages="2")
        self.assertTrue(all(f["page"] == 2 for f in m["figures"]))
        self.assertGreaterEqual(len(m["figures"]), 1)

    def test_manual_rect_mode(self):
        m = self._extract(manual=(1, (120, 120, 380, 300), "m1"))
        self.assertEqual(len(m["figures"]), 1)
        fig = m["figures"][0]
        self.assertEqual(fig["kind"], "manual")
        self.assertTrue((self.outdir / fig["png"]).is_file())

    def test_max_px_cap(self):
        m = self._extract(dpi=600, max_px=1000)
        for fig in m["figures"]:
            self.assertLessEqual(max(fig["width_px"], fig["height_px"]), 1000)

    def test_manifest_written_utf8_with_captions(self):
        m = self._extract()
        raw = (self.outdir / "manifest.json").read_text(encoding="utf-8")
        self.assertIn("栅格示意图", raw)  # caption text preserved, not escaped


class AnchorToleranceTest(unittest.TestCase):
    """Regression (field run 2026-09-25, friction F1): SR1 Fig 1's caption sat
    42.8pt INSIDE the artwork bbox; the old flat OVERLAP_TOL=40 missed it.
    Tolerance now scales with cluster height: min(60pt, 0.5*h)."""

    def test_deep_overlap_anchors_on_tall_cluster(self):
        clusters = [(100, 100, 380, 360)]  # h=260 → tol=60; caption overlaps by 42.8
        cap = {"kind": "figure", "key": "1", "rect": (110, 317.2, 390, 339),
               "text": "Figure 1: deep-overlapped caption"}
        g = ef.anchor_captions([cap], clusters)
        self.assertEqual(g["1"]["cluster_ids"], {0})

    def test_short_cluster_still_rejects_deep_overlap(self):
        clusters = [(100, 100, 380, 140)]  # h=40 → tol=20; overlap -30 exceeds
        cap = {"kind": "figure", "key": "1", "rect": (110, 110, 390, 132),
               "text": "Figure 1: caption inside short bar"}
        g = ef.anchor_captions([cap], clusters)
        self.assertEqual(g["1"]["cluster_ids"], set())


class ManifestMergeTest(unittest.TestCase):
    """Regression (friction F2): a manual --rect run used to OVERWRITE the
    auto manifest in the same outdir, silently losing every auto figure."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pdf = Path(cls.tmp.name) / "synthetic.pdf"
        build_pdf(cls.pdf)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_manual_appends_to_auto_manifest(self):
        out = Path(tempfile.mkdtemp(dir=self.tmp.name))
        m1 = ef.extract_figures(self.pdf, out)
        n1 = len(m1["figures"])
        self.assertGreaterEqual(n1, 3)
        m2 = ef.extract_figures(self.pdf, out, manual=(1, (120, 120, 380, 300), "m9"))
        names = {f["png"] for f in m2["figures"]}
        self.assertIn("synthetic_m9.png", names)
        self.assertEqual(len(m2["figures"]), n1 + 1)
        import json
        disk = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(disk["figures"]), n1 + 1)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pdf = Path(self.tmp.name) / "synthetic.pdf"
        build_pdf(self.pdf)
        self.outdir = Path(self.tmp.name) / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def test_exit_0_when_figures_found(self):
        rc = ef.main(["--pdf", str(self.pdf), "--outdir", str(self.outdir)])
        self.assertEqual(rc, 0)

    def test_manual_mode_via_cli(self):
        rc = ef.main(["--pdf", str(self.pdf), "--outdir", str(self.outdir),
                      "--page", "1", "--rect", "120,120,380,300", "--name", "x1"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(list(self.outdir.glob("*x1.png"))), 1)

    def test_bad_args_exit_1(self):
        rc = ef.main(["--pdf", str(self.pdf), "--outdir", str(self.outdir),
                      "--page", "1"])  # rect missing
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
