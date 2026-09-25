"""cite_api.py — external literature fact-checking (no local storage, REST only).

Backs two MCP tools:
  cite_verify      — does a claim about ANOTHER paper actually exist?
                     (title / author / year / venue existence check)
  paper_citations  — citation context of a paper: cited-by count, top citing
                     works (posterior impact), and its own reference list.

Providers (stdlib urllib only — no new dependencies):
  OpenAlex          https://api.openalex.org      (primary; key-free. Optional
                    `openalex_mailto` in settings.json joins the polite pool.)
  Semantic Scholar  https://api.semanticscholar.org/graph/v1
                    (arXiv-id -> DOI resolution and cited-by fallback)

All functions are synchronous; the MCP handlers run them via to_thread.
Every public function returns a JSON-serializable dict and NEVER raises for
network reasons — failures surface as verdict="network_error" so the reading
workflow can degrade to "unverified" labels instead of crashing.
"""
import difflib
import json
import re
import urllib.error
import urllib.parse
import urllib.request

OPENALEX_BASE = "https://api.openalex.org"
S2_BASE = "https://api.semanticscholar.org/graph/v1"
USER_AGENT = "deep_read_paper_skill (paper-reading verification tool)"
TIMEOUT_S = 20

WORK_SELECT = "id,title,publication_year,doi,cited_by_count,authorships,primary_location"


# ─────────────────────────────── HTTP primitives


def _http_get_json(url: str, timeout: float = TIMEOUT_S):
    """GET + JSON decode. Returns (data, error_str)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code} for {url}"
    except Exception as e:  # URLError, timeout, JSON decode…
        return None, f"{type(e).__name__}: {e} for {url}"


def _http_get_json_retry(url: str, tries: int = 3, backoff: float = 4.0):
    # 3 tries / 4 s spacing matches the S2 keyless pool's reset cadence
    # (observed live: intermittent 429s seconds apart on the same id).
    """_http_get_json with retry on 429/5xx/timeout (Semantic Scholar's
    keyless pool rate-limits frequently). Returns (data, error_str)."""
    import time
    data, err = None, None
    for attempt in range(tries):
        data, err = _http_get_json(url)
        if data is not None:
            return data, None
        retryable = bool(err) and any(k in err for k in
                                      ("429", "HTTP 5", "timed out", "URLError"))
        if not retryable or attempt == tries - 1:
            return None, err
        time.sleep(backoff)
    return None, err


def _mailto() -> str:
    try:
        from mcp_server.config import _load_config_val
        m = (_load_config_val("openalex_mailto", "") or "").strip()
    except Exception:
        m = ""
    return f"&mailto={urllib.parse.quote(m)}" if m else ""


# ─────────────────────────── normalisation helpers


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (t or "").lower())).strip()


def title_similarity(a: str, b: str) -> float:
    """0..1. Token containment is penalised by a length-balance factor:
    without it, every lookalike whose title SUPERSETS the query ("Is Space-Time
    Attention All You Need…", "…All You Need: drug discovery") scores ~1.0 and
    can steal a resolution or a verdict — observed live on OpenAlex twice."""
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return 0.0
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    containment = len(ta & tb) / max(1, min(len(ta), len(tb)))
    balance = (min(len(ta), len(tb)) / max(len(ta), len(tb))) ** 0.5
    return round(max(ratio, containment * balance), 3)


def _venue_of(work: dict) -> str:
    loc = work.get("primary_location") or {}
    src = loc.get("source") or {}
    return src.get("display_name") or ""


def _authors_of(work: dict, limit: int = 6) -> list:
    names = []
    for a in (work.get("authorships") or [])[:limit]:
        dn = (a.get("author") or {}).get("display_name")
        if dn:
            names.append(dn)
    return names


def _clean_doi(doi) -> str:
    return re.sub(r"^https?://doi\.org/", "", doi or "")


def _brief(work: dict) -> dict:
    m = re.search(r"/(W\d+)$", work.get("id") or "")
    return {
        "openalex_id": m.group(1) if m else "",
        "title": work.get("title") or "",
        "year": work.get("publication_year"),
        "venue": _venue_of(work),
        "doi": _clean_doi(work.get("doi")),
        "cited_by_count": work.get("cited_by_count", 0),
        "authors": _authors_of(work),
    }


# ────────────────────────────── OpenAlex access


def search_works(query: str, limit: int = 5):
    """Pure-title free-text search. Do NOT append author to the query —
    OpenAlex relevance degrades badly with it (verified live); cross-checks
    happen client-side in cite_verify."""
    url = (f"{OPENALEX_BASE}/works?search={urllib.parse.quote_plus(query)}"
           f"&per-page={limit}&select={WORK_SELECT}{_mailto()}")
    data, err = _http_get_json_retry(url, tries=2, backoff=1.5)
    if err:
        return [], err
    return [_brief(w) for w in (data or {}).get("results", [])], None


def _work_by_url(path: str):
    url = f"{OPENALEX_BASE}/works/{path}?select={WORK_SELECT}{_mailto()}"
    return _http_get_json_retry(url, tries=2, backoff=1.5)


def resolve_openalex_id(doi: str = "", arxiv_id: str = "", openalex_id: str = ""):
    """Return (openalex_work_id 'W…', work_dict|None, error|None)."""
    if openalex_id and re.fullmatch(r"W\d+", openalex_id.strip()):
        work, err = _work_by_url(f"openalex:{openalex_id.strip()}")
        return (openalex_id.strip(), work, err) if not err else ("", None, err)
    if doi:
        work, err = _work_by_url(f"doi:{urllib.parse.quote(doi.strip())}")
        if not err:
            wid = _brief(work).get("openalex_id", "")
            return wid, work, None
        return "", None, err
    if arxiv_id:
        arxiv_id = re.sub(r"^arxiv:", "", arxiv_id.strip(), flags=re.I)
        # 1) native arXiv DOI (registered for 2022+ papers, usually absent for
        #    older ones — cheap single probe before hitting rate-limited S2)
        work, err = _work_by_url(
            f"doi:{urllib.parse.quote('10.48550/arXiv.' + arxiv_id)}")
        if work:
            return _brief(work)["openalex_id"], work, None
        # 2) Semantic Scholar resolution (keyless pool rate-limits: retried)
        s2, err = _http_get_json_retry(
            f"{S2_BASE}/paper/arXiv:{urllib.parse.quote(arxiv_id)}"
            "?fields=title,year,externalIds,doi")
        doi_from_s2 = _clean_doi(((s2 or {}).get("externalIds") or {}).get("DOI")) \
            or _clean_doi((s2 or {}).get("doi"))
        if doi_from_s2:
            return resolve_openalex_id(doi=doi_from_s2)
        if (s2 or {}).get("title"):
            # arXiv paper not DOIed yet: fall back to title search
            works, err2 = search_works(s2["title"], limit=5)
            works = sorted(works, key=lambda w: title_similarity(s2["title"], w["title"]),
                           reverse=True)
            if works and title_similarity(s2["title"], works[0]["title"]) > 0.9:
                return works[0]["openalex_id"], None, None
        return "", None, (
            f"arXiv:{arxiv_id} unresolved (S2 may be rate-limited). "
            "Retry, or call again with query=<full paper title>. "
            f"Last error: {err}")
    return "", None, "no identifier given"


# ────────────────────────────────── public API


_VERDICE = ("exact", "probable", "uncertain", "not_found", "network_error")


def cite_verify(query: str = "", author: str = "", year=None,
                doi: str = "", arxiv_id: str = "") -> dict:
    """Verify that a literature claim (paper identity) exists externally.

    Returns {verdict, checked, matches:[{similarity,…}], notes}.
    Callers must treat 'uncertain'/'not_found' as "the assertion may be a
    hallucination — rewrite, drop, or mark it 未核验"."""
    checked = {"query": query, "author": author, "year": year,
               "doi": _clean_doi(doi), "arxiv_id": arxiv_id}
    result = {"verdict": "not_found", "checked": checked, "matches": [], "notes": []}

    matches, err = [], None
    if doi or arxiv_id or re.fullmatch(r"W\d+", (query or "").strip()):
        oid, work, err = resolve_openalex_id(
            doi=doi, arxiv_id=arxiv_id,
            openalex_id=query if re.fullmatch(r"W\d+", (query or "").strip()) else "")
        if work:
            matches = [_brief(work)]
        elif not err:
            works, err = search_works(query, author=author)
            matches = works
    else:
        if not (query or "").strip():
            result["verdict"] = "not_found"
            result["notes"].append("empty query")
            return result
        # Two merged searches (OpenAlex free-text degrades when the author is
        # appended, and its plain relevance is noisy — a title-lookalike can
        # rank first). Client-side (sim, author, year) ranking fixes both.
        matches, err = search_works(query, limit=10)
        if author:
            extra, err2 = search_works(f"{query} {author}", limit=5)
            seen = {m["openalex_id"] for m in matches}
            matches += [m for m in extra if m["openalex_id"] not in seen]
            err = err or err2

    if err and not matches:
        result["verdict"] = "network_error"
        result["notes"].append(err)
        return result

    q = query or (matches[0]["title"] if matches else "")

    def _enrich(m):
        m = dict(m, similarity=title_similarity(q, m["title"]))
        m["author_match"] = bool(author) and any(
            _norm_title(author) in _norm_title(a) for a in m["authors"])
        m["year_match"] = bool(year) and m.get("year") in (year, year - 1, year + 1)
        return m

    # Rank by (title similarity, author hit, year hit): a title-lookalike
    # (e.g. the real "Attention is all you need: ... drug discovery" paper that
    # cites-bombs the Transformer one) must not outrank the actual claim.
    scored = sorted((_enrich(m) for m in matches),
                    key=lambda m: (m["similarity"], m["author_match"], m["year_match"]),
                    reverse=True)
    result["matches"] = scored[:5]
    if not scored:
        result["verdict"] = "not_found"
        result["notes"].append(
            "Do NOT cite this as established fact. Re-check naming, or mark the "
            "assertion 【未核验——仅模型记忆】 in the report.")
        return result

    best = scored[0]
    levels = ["not_found", "uncertain", "probable", "exact"]
    sim = best["similarity"]
    if sim >= 0.92:
        idx = 3
    elif sim >= 0.75:
        idx = 2
    elif sim >= 0.5:
        idx = 1
    else:
        idx = 0
    # Cross-check mismatches downgrade the verdict one level each:
    if author and not best["author_match"]:
        idx -= 1
        result["notes"].append(
            f"author '{author}' not found among candidates' authors "
            f"(first 6 listed) — title match alone may be a lookalike paper")
    if year and not best["year_match"]:
        idx -= 1
        result["notes"].append(
            f"year mismatch: claimed {year}, best match says {best.get('year')}")
    result["verdict"] = levels[max(0, idx)]
    if result["verdict"] in ("uncertain", "not_found"):
        result["notes"].append(
            "Do NOT cite this as established fact. Re-check naming, or mark the "
            "assertion 【未核验——仅模型记忆】 in the report.")
    return result


def paper_citations(query: str = "", doi: str = "", arxiv_id: str = "",
                    openalex_id: str = "", n_citing: int = 15,
                    include_references: bool = True) -> dict:
    """Citation context: count + top citing works (posterior impact) + own refs."""
    result = {"resolved": None, "cited_by_count": None, "top_citing": [],
              "recent_citing": [], "references": [], "notes": []}

    oid, work, err = resolve_openalex_id(doi=doi, arxiv_id=arxiv_id,
                                         openalex_id=openalex_id)
    if not oid and query:
        # title fallback with a similarity floor: never resolve against garbage
        works, err2 = search_works(query, limit=5)
        works = sorted(works, key=lambda w: title_similarity(query, w["title"]),
                       reverse=True)
        if works and title_similarity(query, works[0]["title"]) >= 0.75:
            oid = works[0]["openalex_id"]
        else:
            err = err or (err2 or "title search found no sufficiently similar work")
    if not oid:
        result["notes"].append(f"could not resolve paper: {err or 'no identifiers'}")
        return result

    if not work:
        work, err = _work_by_url(f"openalex:{oid}")
    if work:
        result["resolved"] = _brief(work)
        result["cited_by_count"] = work.get("cited_by_count")

    data, err = _http_get_json(
        f"{OPENALEX_BASE}/works?filter=cites:{oid}&per-page={min(n_citing, 50)}"
        f"&sort=cited_by_count:desc&select={WORK_SELECT}{_mailto()}")
    if data:
        result["top_citing"] = [_brief(w) for w in data.get("results", [])]
    data, err2 = _http_get_json(
        f"{OPENALEX_BASE}/works?filter=cites:{oid}&per-page=5"
        f"&sort=publication_year:desc&select={WORK_SELECT}{_mailto()}")
    if data:
        result["recent_citing"] = [_brief(w) for w in data.get("results", [])]
    if err and err2:  # OpenAlex cited-by failed → S2 fallback (needs doi/arxiv)
        if _clean_doi(doi):
            s2id = f"DOI:{_clean_doi(doi)}"
        elif arxiv_id:
            s2id = f"ARXIV:{re.sub(r'^arxiv:', '', arxiv_id.strip(), flags=re.I)}"
        else:
            s2id = ""
        if s2id:
            data, err3 = _http_get_json(
                f"{S2_BASE}/paper/{urllib.parse.quote(s2id)}/citations"
                "?fields=title,year,venue,citationCount&limit=15")
            if data:
                result["top_citing"] = [
                    {"title": (c.get("citingPaper") or {}).get("title"),
                     "year": (c.get("citingPaper") or {}).get("year"),
                     "venue": (c.get("citingPaper") or {}).get("venue"),
                     "cited_by_count": (c.get("citingPaper") or {}).get("citationCount")}
                    for c in data.get("data", [])]
                err = err3

    if include_references and oid:
        # OpenAlex reference lists exist only for works with a full citation
        # graph (Crossref-registered DOIs); arXiv-centric works return 200 +
        # empty list → try Semantic Scholar before declaring unavailable.
        data, rerr = _http_get_json(
            f"{OPENALEX_BASE}/works/{oid}/references?per-page=100{_mailto()}")
        refs = []
        for item in (data or {}).get("results", []):
            w = item.get("works") or item
            if isinstance(w, dict) and w.get("title"):
                mw = re.search(r"/(W\d+)$", w.get("id") or "")
                refs.append({"title": w.get("title"),
                             "year": w.get("publication_year"),
                             "openalex_id": mw.group(1) if mw else ""})
        if not refs and data is not None:
            s2id = (f"DOI:{_clean_doi(doi)}" if _clean_doi(doi) else
                    (f"ARXIV:{re.sub('^arxiv:', '', arxiv_id.strip(), flags=re.I)}"
                     if arxiv_id else
                     (f"openalex:{oid}" if oid else "")))
            if s2id:
                d2, e2 = _http_get_json_retry(
                    f"{S2_BASE}/paper/{urllib.parse.quote(s2id)}/references"
                    "?fields=title,year&limit=100")
                for item in (d2 or {}).get("data", []):
                    w = item.get("citedPaper") or {}
                    if w.get("title"):
                        refs.append({"title": w["title"], "year": w.get("year"),
                                     "openalex_id": ""})
                if not refs and e2:
                    rerr = e2
        if refs:
            result["references"] = refs
        else:
            result["notes"].append(
                f"references unavailable from both providers: {rerr or 'empty citation graph for this work'}")

    if not result["top_citing"] and err:
        result["notes"].append(f"cited-by unavailable: {err}")
    return result
