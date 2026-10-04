"""Offline unit tests for tools/verify_refs.py — no network access.

cite_api.cite_verify is replaced by a fixture lambda, so these tests pin the
gate semantics rather than the upstream API: the two rules that must never
regress are (a) an identifier that resolved incompletely is DEFER, not "fake",
and (b) an absent record is UNRESOLVED — the tool must never render
"不存在"/"伪造" as a conclusion.

Run from repo root:  python -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import verify_refs as vr  # noqa: E402


def _result(verdict="exact", author_match=True, year_match=True, notes=None,
            match=True, year=2017, title="Attention Is All You Need",
            cited=55000, authors=("Ashish Vaswani", "Noam Shazeer"), sim=0.98):
    """Mirrors cite_api._brief() + the cite_verify enrichment keys EXACTLY —
    a fixture with invented keys lets a wrong .get() in verify_refs pass."""
    matches = []
    if match:
        matches.append({"openalex_id": "W1", "title": title, "year": year,
                        "venue": "NeurIPS", "doi": "10.1/x",
                        "cited_by_count": cited, "authors": list(authors),
                        "similarity": sim, "author_match": author_match,
                        "year_match": year_match})
    return {"verdict": verdict, "matches": matches, "notes": list(notes or [])}


# the live 2026-09-29 parody candidate that OpenAlex ranks under the Transformer
LOOKALIKE = dict(verdict="uncertain", sim=0.62, year=2021,
                 title="Is Attention All You Need?", authors=("Patrick Mineault",))


class ParseTest(unittest.TestCase):
    def test_text_fields_comments_and_identifiers(self):
        text = (
            "# 报告点名的外部工作\n"
            "\n"
            "Attention Is All You Need | Vaswani | 2017 | doi:10.48550/arXiv.1706.03762\n"
            "FLMR | Lin | 2023\n"
            "ColBERT | Khattab | 2020 | https://arxiv.org/abs/2004.12832v2\n"
            "Some Project Page | | | https://example.org/x\n"
        )
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "refs.txt"
            p.write_text(text, encoding="utf-8")
            refs = vr.parse_refs(p)
        self.assertEqual(len(refs), 4)
        self.assertEqual(refs[0]["title"], "Attention Is All You Need")
        self.assertEqual(refs[0]["author"], "Vaswani")
        self.assertEqual(refs[0]["year"], 2017)
        self.assertEqual(refs[0]["doi"], "10.48550/arXiv.1706.03762")
        self.assertEqual(refs[1]["title"], "FLMR")
        self.assertEqual(refs[1]["doi"], "")
        # arXiv link -> id (version suffix kept as written, that is fine)
        self.assertTrue(refs[2]["arxiv_id"].startswith("2004.12832"))
        # non-arXiv URL is recorded, not sent as a fake doi
        self.assertEqual(refs[3]["url"], "https://example.org/x")
        self.assertEqual(refs[3]["doi"], "")

    def test_json_list_and_wrapper(self):
        payload = [{"title": "A", "author": "B", "year": 2021, "doi": "10.1/x"}]
        with tempfile.TemporaryDirectory() as d:
            p1 = Path(d) / "a.json"
            p1.write_text(json.dumps(payload), encoding="utf-8")
            p2 = Path(d) / "b.json"
            p2.write_text(json.dumps({"refs": payload}), encoding="utf-8")
            self.assertEqual(vr.parse_refs(p1)[0]["year"], 2021)
            self.assertEqual(vr.parse_refs(p2)[0]["title"], "A")


class ClassifyTest(unittest.TestCase):
    def test_gates(self):
        cases = [
            (_result("exact"), "Vaswani", "exists_ok"),
            (_result("probable"), "", "exists_ok"),
            (_result("exact", author_match=False), "Vaswani", "discrepancy"),
            # nobody claimed -> nothing to disagree with
            (_result("exact", author_match=False, year_match=False), "", "exists_ok"),
            # only a weak candidate (< 0.75) -> nothing convincing at title level
            (_result("uncertain", sim=0.62), "X", "unresolved"),
            (_result("not_found", match=False), "X", "unresolved"),
            (_result("not_found", match=False,
                     notes=["identifier given but lookup incomplete (OpenAlex "
                            "coverage gap or Semantic Scholar rate-limit window); "
                            "a weak verdict here means DEFER AND RETRY, not "
                            "'claim is fake'."]), "X", "defer"),
            (_result("network_error", match=False), "X", "network"),
        ]
        for result, author, expected in cases:
            self.assertEqual(vr.classify(result, author), expected, result)

    def test_gate_reads_the_pool_not_the_top_rank(self):
        """Live 2026-09-29: OpenAlex ranked the lookalike ('Is Attention All
        You Need?', 53 cites) ABOVE the real Transformer record. Judging on
        matches[0] alone would slander the most-cited paper in ML; the claimed
        author is present one rank down, so the row is clean."""
        result = _result("exact", author_match=False, sim=1.0)
        result["matches"][0]["authors"] = ["Patrick Mineault"]
        result["matches"].append({"openalex_id": "W2", "title": "Attention Is All You Need",
                                  "year": 2025, "venue": "", "doi": "", "cited_by_count": 26892,
                                  "authors": ["Ashish Vaswani", "Noam Shazeer"],
                                  "similarity": 1.0, "author_match": True, "year_match": False})
        self.assertEqual(vr.classify(result, "Vaswani"), "exists_ok")

    def test_strong_title_match_survives_a_verdict_downgrade(self):
        """Two cross-check downgrades (author absent from the top candidate +
        year artifact) push cite_verify to 'uncertain'; a title-level match is
        still an existence fact."""
        self.assertEqual(vr.classify(_result("uncertain"), "Vaswani"), "exists_ok")

    def test_year_mismatch_alone_is_not_a_misattribution(self):
        """Live 2026-09-29: OpenAlex's merged Transformer record says 2025.
        Flagging that as '信息不符' would slander the most-cited paper in ML."""
        result = _result("probable", year_match=False, year=2025)
        self.assertEqual(vr.classify(result, "Vaswani"), "exists_ok")
        note = vr.gate_note("exists_ok", result, {"author": "Vaswani", "year": 2017})
        self.assertIn("2025", note)
        self.assertIn("合并", note)

    def test_author_mismatch_names_the_library_authors(self):
        note = vr.gate_note("discrepancy", _result("exact", author_match=False),
                            {"author": "Vaswani", "year": 2017})
        self.assertIn("Vaswani", note)
        self.assertIn("Ashish Vaswani", note)   # what the library actually has

    def test_unresolved_note_never_claims_nonexistence(self):
        note = vr.gate_note("unresolved", _result("not_found", match=False),
                            {"author": "", "year": None})
        self.assertIn("不等于不存在", note)

    def test_probable_is_marked_in_the_report_row_not_as_console_drama(self):
        entry = {"gate": "exists_ok", "verdict": "probable",
                 "claimed": {"author": "", "year": None}, "match": {"year_match": True}}
        self.assertEqual(vr.gate_note("exists_ok", _result("probable"),
                                      {"author": "", "year": None}), "")
        self.assertIn("probable", vr._short_note(entry))


class BuildLedgerTest(unittest.TestCase):
    def _refs(self):
        return [{"title": "T1", "author": "A", "year": 2017, "doi": "", "arxiv_id": "", "url": ""},
                {"title": "T2", "author": "", "year": None, "doi": "", "arxiv_id": "", "url": ""}]

    def test_passes_claimed_fields_and_counts(self):
        seen = []

        def fake(query="", author="", year=None, doi="", arxiv_id=""):
            seen.append((query, author, year))
            return _result("exact")

        ledger = vr.build_ledger(self._refs(), delay=0, verify=fake)
        self.assertEqual(seen[0], ("T1", "A", 2017))
        self.assertEqual(ledger["counts"]["exists_ok"], 2)
        self.assertEqual(ledger["entries"][0]["match"]["venue"], "NeurIPS")
        # regression: the ledger must carry the API's own key, or the §6 table
        # silently renders an empty citation count
        self.assertEqual(ledger["entries"][0]["match"]["cited_by_count"], 55000)
        self.assertEqual(ledger["entries"][0]["match"]["authors"][0], "Ashish Vaswani")

    def test_one_row_exception_does_not_kill_the_batch(self):
        def fake(query="", **kw):
            if query == "T1":
                raise RuntimeError("boom")
            return _result("exact")

        ledger = vr.build_ledger(self._refs(), delay=0, verify=fake)
        self.assertEqual(ledger["entries"][0]["gate"], "network")
        self.assertIn("boom", ledger["entries"][0]["api_notes"][0])
        self.assertEqual(ledger["entries"][1]["gate"], "exists_ok")

    def test_max_truncates_without_dropping_silently(self):
        ledger = vr.build_ledger(self._refs(), delay=0, max_n=1,
                                 verify=lambda **kw: _result("exact"))
        self.assertEqual(len(ledger["entries"]), 1)
        self.assertEqual(ledger["truncated"], 1)

    def test_url_only_row_is_skipped_but_visible(self):
        refs = [{"title": "", "author": "", "year": None, "doi": "",
                 "arxiv_id": "", "url": "https://example.org/x"}]
        ledger = vr.build_ledger(refs, delay=0, verify=lambda **kw: _result())
        self.assertEqual(ledger["entries"][0]["gate"], "empty")
        self.assertEqual(ledger["entries"][0]["title"], "https://example.org/x")


class RenderTest(unittest.TestCase):
    def test_md_table_and_legend(self):
        ledger = vr.build_ledger(
            [{"title": "T1", "author": "A", "year": 2017, "doi": "", "arxiv_id": "", "url": ""},
             {"title": "T2", "author": "B", "year": 1999, "doi": "", "arxiv_id": "", "url": ""}],
            delay=0,
            verify=lambda query="", **kw: _result("exact") if query == "T1"
            else _result(**LOOKALIKE))
        md = vr.render_md(ledger)
        self.assertIn("| # | 工作（报告点名） | 论文自报 | 核验 | 外部库记录 |", md)
        self.assertIn("✅", md)
        self.assertIn("❓", md)
        self.assertIn("被引 55000", md)        # record cell really carries the data
        self.assertIn("弱匹配", md)             # unresolved rows label the candidate as weak
        self.assertIn("不得写成", md)          # the honesty caveat ships with the table
        # The table BODY must never label a row fabricated / non-existent —
        # "不存在" may only survive inside the caveat phrase.
        body = "\n".join(ln for ln in md.splitlines() if ln.startswith("| ") and "---" not in ln)
        self.assertNotIn("伪造", body)
        self.assertNotIn("不存在", body.replace("不等于不存在", ""))


class MainTest(unittest.TestCase):
    def setUp(self):
        self._orig = vr.cite_api.cite_verify

    def tearDown(self):
        vr.cite_api.cite_verify = self._orig

    def _run(self, text, extra=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        refs = d / "refs.txt"
        refs.write_text(text, encoding="utf-8")
        out = d / "ledger.json"
        md = d / "table.md"
        code = vr.main(["--refs", str(refs), "--out", str(out), "--md", str(md),
                        "--delay", "0"] + (extra or []))
        return code, out, md

    def test_success_writes_ledger_and_md_exit_0(self):
        vr.cite_api.cite_verify = lambda **kw: _result("exact")
        code, out, md = self._run("Alpha | A | 2017\nBeta | B | 2018\n")
        self.assertEqual(code, 0)
        ledger = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(ledger["counts"]["exists_ok"], 2)
        self.assertIn("✅", md.read_text(encoding="utf-8"))

    def test_partial_network_exit_2(self):
        vr.cite_api.cite_verify = lambda **kw: _result("network_error", match=False)
        code, out, _ = self._run("Alpha | A | 2017\n")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["counts"]["network"], 1)

    def test_default_out_and_bad_input(self):
        vr.cite_api.cite_verify = lambda **kw: _result("exact")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        refs = Path(tmp.name) / "refs.txt"
        refs.write_text("Alpha\n", encoding="utf-8")
        self.assertEqual(vr.main(["--refs", str(refs), "--delay", "0"]), 0)
        self.assertTrue((Path(tmp.name) / "cite_ledger.json").is_file())
        self.assertEqual(vr.main(["--refs", str(Path(tmp.name) / "nope.txt")]), 1)
        bad = Path(tmp.name) / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        self.assertEqual(vr.main(["--refs", str(bad)]), 1)


class GateLedgerConsistencyTest(unittest.TestCase):
    """The candidate-pool fix (2026-09-29) reached classify()/gate_note(), but
    build_ledger kept storing matches[0] — so an exists_ok row RENDERED the
    lookalike as the library record and invented a year note about the wrong
    paper (re-found by the 2026-10-04 audit: 假论文 venue/被引 53 上了 §6 表)."""

    def _pool(self):
        lookalike = {"openalex_id": "W9", "title": "Is Attention All You Need?",
                     "year": 2024, "venue": "FakeVenue", "doi": "",
                     "cited_by_count": 53, "authors": ["Patrick Mineault"],
                     "similarity": 0.90, "author_match": False, "year_match": False}
        real = {"openalex_id": "W2", "title": "Attention Is All You Need",
                "year": 2017, "venue": "NeurIPS", "doi": "10.5/r",
                "cited_by_count": 90000, "authors": ["Ashish Vaswani"],
                "similarity": 0.78, "author_match": True, "year_match": True}
        return {"verdict": "probable", "matches": [lookalike, real], "notes": []}

    def test_exists_ok_row_shows_the_judged_record(self):
        refs = [{"title": "Attention Is All You Need", "author": "Vaswani",
                 "year": 2017, "doi": "", "arxiv_id": "", "url": ""}]
        ledger = vr.build_ledger(refs, delay=0, verify=lambda **kw: self._pool())
        entry = ledger["entries"][0]
        self.assertEqual(entry["gate"], "exists_ok")
        self.assertEqual(entry["match"]["title"], "Attention Is All You Need")
        cell = vr._record_cell(entry)
        self.assertIn("NeurIPS", cell)
        self.assertNotIn("FakeVenue", cell)
        self.assertNotIn("库记录年份", vr._short_note(entry),
                         "年份注记必须基于门实际判定的那条记录")

    def test_weak_candidate_still_rendered_when_unresolved(self):
        """The SIM_STRONG-miss fallback keeps "closest thing found" visible —
        the row is unresolved either way."""
        weak = {"openalex_id": "W3", "title": "Whatever", "year": None,
                "venue": "arXiv", "doi": "", "cited_by_count": 1,
                "authors": ["X"], "similarity": 0.42, "author_match": False,
                "year_match": False}
        result = {"verdict": "not_found", "matches": [weak], "notes": []}
        refs = [{"title": "Nonexistent Deep Dive", "author": "", "year": None,
                 "doi": "", "arxiv_id": "", "url": ""}]
        ledger = vr.build_ledger(refs, delay=0, verify=lambda **kw: result)
        entry = ledger["entries"][0]
        self.assertEqual(entry["gate"], "unresolved")
        self.assertIn("弱匹配", vr._record_cell(entry))


if __name__ == "__main__":
    unittest.main()
