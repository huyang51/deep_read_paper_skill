"""Offline unit tests for mcp_server/cite_api.py — no network access.

All HTTP goes through a URL router backed by fixtures (monkeypatch of
_http_get_json; the *_retry wrapper delegates to it). Includes a regression
test for the real-world "parody paper" trap: OpenAlex ranks
"Attention is all you need: utilizing attention in AI-enabled drug discovery"
above the actual Transformer paper for naive title search — client-side
(sim, author, year) ranking must fix it.

Run from repo root:  python -m unittest discover -s tests -v
"""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcp_server import cite_api  # noqa: E402


def _oa_work(oid, title, year, doi="", cites=100, authors=("Alice Smith",),
             venue="Some Venue"):
    return {
        "id": f"https://api.openalex.org/works/{oid}",
        "title": title,
        "publication_year": year,
        "doi": f"https://doi.org/{doi}" if doi else None,
        "cited_by_count": cites,
        "authorships": [{"author": {"display_name": a}} for a in authors],
        "primary_location": {"source": {"display_name": venue}},
    }


REAL = _oa_work("W2626778328", "Attention Is All You Need", 2017,
                doi="10.48550/arXiv.1706.03762", cites=55000,
                authors=("Ashish Vaswani", "Noam Shazeer"),
                venue="Neural Information Processing Systems")
PARODY = _oa_work("W4390678101",
                  "Attention is all you need: utilizing attention in AI-enabled drug discovery",
                  2023, doi="10.1093/bib/bbad040", cites=378,
                  authors=("Yang Zhang",), venue="Briefings in Bioinformatics")
IRRELEVANT = _oa_work("W3126721948", "Is Space-Time Attention All You Need for Video Understanding",
                      2021, cites=1460, authors=("Gedas Bertasius",))


def make_router(search_payload=None, work=None, cites=None, oa_refs=None,
                s2_paper=None, s2_refs=None, fail=None):
    """fail: substring -> error message, to simulate network failures."""
    calls = []

    def router(url, timeout=20):
        calls.append(url)
        for needle, msg in (fail or {}).items():
            if needle in url:
                return None, msg
        if "semanticscholar" in url:
            if "/references" in url:
                if s2_refs is not None:
                    return {"data": copy.deepcopy(s2_refs)}, None
                return None, "HTTP 404"
            if s2_paper is not None:
                return copy.deepcopy(s2_paper), None
            return None, "HTTP 404"
        if "/works?search=" in url:
            return {"results": copy.deepcopy(search_payload or [])}, None
        if "filter=cites:" in url:
            return {"results": copy.deepcopy(cites or [])}, None
        if "/references" in url:
            return copy.deepcopy(oa_refs) if oa_refs is not None else (None, "unrouted"), None
        if ("/works/doi:" in url or "/works/openalex:" in url
                or "/works/W" in url.split("?")[0]):
            if work is not None:
                return copy.deepcopy(work), None
            return None, "HTTP 404"
        return None, f"unrouted test url: {url}"

    router.calls = calls
    return router


class Patched(unittest.TestCase):
    def setUp(self):
        self._orig = cite_api._http_get_json
        cite_api._http_get_json = self._orig  # restored in tearDown

    def tearDown(self):
        cite_api._http_get_json = self._orig

    def route(self, **kw):
        cite_api._http_get_json = make_router(**kw)


# ---------------------------------------------------------------- pure fns

class SimilarityTest(unittest.TestCase):
    def test_identical_ignoring_case_punct(self):
        self.assertGreaterEqual(
            cite_api.title_similarity("Attention, Is All You Need!",
                                      "attention is all you need"), 0.99)

    def test_superset_lookalike_is_penalised(self):
        """Regression: title-superset variants must NOT score exact without
        the length-balance penalty (live OpenAlex trap, 2026-09)."""
        for other in (
                "Attention is all you need: utilizing attention in AI-enabled drug discovery",
                "Is Space-Time Attention All You Need for Video Understanding"):
            sim = cite_api.title_similarity("Attention is all you need", other)
            self.assertLessEqual(sim, 0.75, other)

    def test_exact_still_wins_over_lookalikes(self):
        exact = cite_api.title_similarity(
            "Attention Is All You Need", "attention is all you need")
        lookalike = cite_api.title_similarity(
            "Attention Is All You Need",
            "Is Space-Time Attention All You Need for Video Understanding")
        self.assertGreaterEqual(exact, 0.99)
        self.assertGreater(exact, lookalike)

    def test_different_titles_low(self):
        self.assertLess(cite_api.title_similarity(
            "Deep Residual Learning for Image Recognition",
            "Generative Adversarial Nets"), 0.5)


class BriefTest(unittest.TestCase):
    def test_normalisation(self):
        b = cite_api._brief(REAL)
        self.assertEqual(b["openalex_id"], "W2626778328")
        self.assertEqual(b["year"], 2017)
        self.assertEqual(b["doi"], "10.48550/arXiv.1706.03762")
        self.assertEqual(b["venue"], "Neural Information Processing Systems")
        self.assertIn("Ashish Vaswani", b["authors"])


# ---------------------------------------------------------------- verify

class CiteVerifyTest(Patched):
    def test_real_paper_wins_over_parody(self):
        """Regression: parody must not outrank the claimed Vaswani/2017 work."""
        self.route(search_payload=[PARODY, IRRELEVANT, REAL])  # parody first!
        r = cite_api.cite_verify(query="Attention is All You Need",
                                 author="Vaswani", year=2017)
        self.assertEqual(r["verdict"], "exact")
        self.assertEqual(r["matches"][0]["openalex_id"], "W2626778328")
        self.assertTrue(r["matches"][0]["author_match"])

    def test_author_mismatch_downgrades(self):
        self.route(search_payload=[REAL, PARODY])
        r = cite_api.cite_verify(query="Attention is All You Need",
                                 author="NobodyHere")
        self.assertEqual(r["verdict"], "probable")
        self.assertTrue(any("author" in n for n in r["notes"]))

    def test_not_found_guidance(self):
        self.route(search_payload=[])
        r = cite_api.cite_verify(query="Quantum Odor XYZ 2099")
        self.assertEqual(r["verdict"], "not_found")
        self.assertTrue(any("未核验" in n for n in r["notes"]))

    def test_network_error_verdict(self):
        cite_api._http_get_json = make_router(
            fail={"api.openalex.org": "URLError: unreachable",
                  "semanticscholar": "URLError: unreachable"})
        r = cite_api.cite_verify(query="Anything at all")
        self.assertEqual(r["verdict"], "network_error")

    def test_identifier_failure_still_searches_title(self):
        """Regression (field run 2026-09-25, friction F4): an arXiv id that
        cannot resolve (S2 rate-limited) used to SUPPRESS the OpenAlex title
        search fallback, so a real paper verdicted not_found/network_error.
        Candidates must now accumulate from every available signal."""
        self.route(fail={"semanticscholar": "HTTP 404 s2 miss"},
                   search_payload=[PARODY, REAL])  # doi probe → 404; title search hits
        r = cite_api.cite_verify(query="Attention is All You Need",
                                 author="Vaswani", year=2017,
                                 arxiv_id="1706.03762")
        self.assertEqual(r["verdict"], "exact")
        self.assertEqual(r["matches"][0]["openalex_id"], "W2626778328")

    def test_empty_inputs(self):
        self.route(search_payload=[])
        r = cite_api.cite_verify()
        self.assertEqual(r["verdict"], "not_found")
        self.assertIn("empty query", r["notes"])

    def test_title_filter_fallback_rescues_weak_candidates(self):
        """friction F4: free-text search returns only lookalikes (all sim<0.75)
        → title.search filter pass must run and can surface the real record."""
        def router(url, timeout=20):
            if "filter=title.search" in url:
                return {"results": [REAL]}, None
            if "/works?search=" in url:
                return {"results": [PARODY]}, None
            return None, "HTTP 404"
        cite_api._http_get_json = router
        r = cite_api.cite_verify(query="Attention is All You Need",
                                 author="Vaswani", year=2017)
        self.assertEqual(r["verdict"], "exact")
        self.assertEqual(r["matches"][0]["openalex_id"], "W2626778328")

    def test_unresolved_identifier_flagged_for_defer(self):
        """friction F4: id given but unresolvable + weak candidates → the note
        must tell the caller this is DEFER-AND-RETRY, not a refutation."""
        def router(url, timeout=20):
            if "semanticscholar" in url:
                return None, "HTTP 429 rate limited"
            if "/works?search=" in url:
                return {"results": [IRRELEVANT]}, None
            return None, "HTTP 404"
        cite_api._http_get_json = router
        r = cite_api.cite_verify(query="Attention is All You Need",
                                 arxiv_id="1706.99999")
        self.assertTrue(any("DEFER AND RETRY" in n for n in r["notes"]))

    def test_doi_lookup(self):
        self.route(work=REAL)
        r = cite_api.cite_verify(query="Attention is All You Need",
                                 doi="10.48550/arXiv.1706.03762", year=2017)
        self.assertEqual(r["verdict"], "exact")


# -------------------------------------------------------------- citations

class PaperCitationsTest(Patched):
    def test_title_fallback_floor_rejects_garbage(self):
        self.route(search_payload=[IRRELEVANT])
        c = cite_api.paper_citations(query="Attention is All You Need",
                                     include_references=False)
        self.assertIsNone(c["resolved"])
        self.assertTrue(c["notes"] and "could not resolve" in c["notes"][0])

    def test_doi_path_with_s2_reference_fallback(self):
        """OpenAlex citation graph is empty for arXiv-centric works →
        references must fall back to Semantic Scholar."""
        s2_refs = [{"citedPaper": {"title": "Layer Normalization", "year": 2016}}]
        self.route(work=REAL, cites=[PARODY],
                   oa_refs={"results": []}, s2_refs=s2_refs)
        c = cite_api.paper_citations(doi="10.48550/arXiv.1706.03762",
                                     n_citing=2, include_references=True)
        self.assertEqual(c["resolved"]["openalex_id"], "W2626778328")
        self.assertEqual(c["cited_by_count"], 55000)
        self.assertEqual([r["title"] for r in c["references"]],
                         ["Layer Normalization"])
        self.assertEqual(len(c["top_citing"]), 1)

    def test_query_resolution_and_top_citing(self):
        self.route(search_payload=[PARODY, REAL], work=REAL,
                   cites=[IRRELEVANT])
        c = cite_api.paper_citations(query="Attention is All You Need",
                                     n_citing=5, include_references=False)
        self.assertEqual(c["resolved"]["openalex_id"], "W2626778328")
        self.assertEqual(c["cited_by_count"], 55000)
        self.assertEqual(len(c["top_citing"]), 1)

    def test_cite_verify_via_arxiv_reaches_search_branch(self):
        """Regression (periphery audit 2026-09-25): arxiv_id resolving through
        the S2-title path returns no work payload — cite_verify must fall
        through to a title search (branch previously raised TypeError)."""
        s2 = {"title": "Attention Is All You Need",
              "externalIds": {"ArXiv": "1706.03762"}}
        self.route(s2_paper=s2, search_payload=[PARODY, REAL])
        r = cite_api.cite_verify(query="Attention is All You Need",
                                 author="Vaswani", arxiv_id="1706.03762")
        self.assertIn(r["verdict"], ("exact", "probable"))
        self.assertTrue(r["matches"])

    def test_cite_verify_uses_oid_refetched_payload(self):
        """Regression (friction F4 residual): when the arXiv route resolves an
        id WITHOUT a work payload, cite_verify must refetch by that id —
        free-text search alone leaves only lookalikes (field ReAct case)."""
        import copy
        s2 = {"title": "Attention Is All You Need",   # no DOI in externalIds
              "externalIds": {"ArXiv": "1706.03622"}}

        def router(url, timeout=20):
            if "semanticscholar" in url:
                return copy.deepcopy(s2), None
            if "/works?search=" in url:
                # resolve()'s internal title pass (capital "Is") sees REAL;
                # cite_verify's own query search sees only the lookalike
                return {"results": [copy.deepcopy(REAL)
                                    if "Is+All" in url
                                    else copy.deepcopy(PARODY)]}, None
            if "doi:10.48550" in url:
                return None, "HTTP 404"               # old arXiv: no registered DOI
            if "/works/openalex:" in url:
                return copy.deepcopy(REAL), None      # the refetch under test
            return None, "HTTP 404"

        cite_api._http_get_json = router
        r = cite_api.cite_verify(query="Attention is All You Need",
                                 author="Vaswani", arxiv_id="1706.03762")
        self.assertEqual(r["verdict"], "exact")
        self.assertEqual(r["matches"][0]["openalex_id"], "W2626778328")

    def test_arxiv_via_s2_without_doi_uses_title_search(self):
        """Old arXiv papers lack a DOI: S2 gives title, we resolve via search."""
        s2 = {"title": "Attention Is All You Need",
              "externalIds": {"ArXiv": "1706.03762"}}
        self.route(s2_paper=s2, search_payload=[PARODY, REAL], cites=[])
        oid, work, err = cite_api.resolve_openalex_id(arxiv_id="1706.03762")
        self.assertEqual(oid, "W2626778328")


if __name__ == "__main__":
    unittest.main(verbosity=2)
