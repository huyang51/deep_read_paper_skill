#!/usr/bin/env python3
"""extract_figures.py — crop figure regions from a PDF into high-DPI PNGs.

Dual-channel reading pipeline, visual side:
  text channel   : page.get_text()      -> per-page .txt          (SKILL.md 1.1)
  visual channel : this tool            -> figure PNGs + manifest.json

PDF is a geometry-exact format, so this tool NEVER guesses figure borders from
pixels. The recipe:
  1. Candidate rects = raster image placements (page.get_image_info)
     + vector drawing bounding boxes (page.get_drawings), pre-filtered
     (page backgrounds, separator rules, specks removed).
  2. Proximity clustering (transitive merge at --pad pt) reconstructs the
     extent of a vector figure built from dozens of primitives.
  3. Caption blocks are located by regex (Figure|Fig.|图 <N> <colon/dot>).
     Each caption anchors to the nearest cluster BELOW it (or, rarely, above
     it) that horizontally overlaps the caption.
  4. crop = (cluster ∪ caption) + pad, rendered to PNG at --dpi, capped to
     --max-px per side (vision models lose little above ~2000 px/side).
Same figure key on one page (Figure 4(a) + 4(b)) merges into ONE crop.
Clusters with no matching caption are only reported in manifest["skipped"]
unless --include-uncaptioned.

If an auto crop looks imperfect when the model views it, re-crop manually:
  python tools/extract_figures.py --pdf paper.pdf --outdir out \\
      --page 6 --rect 60,180,540,420 --name fig6_fix
(the --rect route writes a manifest entry with kind "manual" and exits 0).

Typical usage (one pass per paper):
  python tools/extract_figures.py --pdf paper.pdf --outdir <dir> \\
      --prefix <short_name> --dpi 300

Exit codes: 0 ok | 2 no figures written | 1 bad args / open failure.
"""
import argparse
import json
import re
import sys
from pathlib import Path

import fitz  # PyMuPDF

# ---------------------------------------------------------------- caption regex

# Captions must look like "Figure 3:", "Fig. 1.", "图2：", "Figure 4(a):" —
# the trailing separator avoids matching in-text mentions ("Figure 3 shows...").
FIG_CAPTION_RE = re.compile(
    r"(?:Figure|Fig\.?|图)\s*([A-Za-z]?\d+[A-Za-z]?)(?:\([a-zA-Z]\))?\s*"
    r"(?=[：:.、\-–—])",
    re.IGNORECASE,
)
TABLE_CAPTION_RE = re.compile(r"(?:Table|Tab\.?|表)\s*\d+", re.IGNORECASE)
KEY_NORMALIZE_RE = re.compile(r"([A-Za-z]*\d+)")

MAX_CAPTION_CHARS = 900  # longer blocks are merged body text, not captions


def page_text_lines(page):
    """Line-level (text, rect4) list. Lines, not blocks: PyMuPDF merges
    side-by-side captions of multi-panel figures into ONE block, which would
    leave the second panel's caption unanchored if we matched block-wise."""
    lines = []
    for blk in page.get_text("dict")["blocks"]:
        if blk.get("type", 1) != 0:
            continue
        for ln in blk.get("lines", []):
            text = "".join(s.get("text", "") for s in ln.get("spans", []))
            if text.strip():
                b = ln["bbox"]
                lines.append((text, (float(b[0]), float(b[1]),
                                     float(b[2]), float(b[3]))))
    return lines


def parse_captions(lines):
    """Caption dicts {key, kind, rect, text} from line-level (text, rect4).
    A multi-line caption (wrapped under one number) is merged into the first
    matched line's entry by growing its rect downward."""
    caps = []
    consumed = set()
    for i, (text, rect) in enumerate(lines):
        if i in consumed:
            continue
        t = (text or "").strip()
        if not t:
            continue
        m = FIG_CAPTION_RE.match(t)
        if m:
            kind = "figure"
            key_m = KEY_NORMALIZE_RE.match(m.group(1))
            key = key_m.group(1) if key_m else m.group(1)
        elif TABLE_CAPTION_RE.match(t):
            kind, key = "table", None
        else:
            continue
        r = list(rect)
        parts = [t]
        for j in range(i + 1, len(lines)):
            if j in consumed:
                continue
            t2, r2 = lines[j]
            if FIG_CAPTION_RE.match(t2.strip()) or TABLE_CAPTION_RE.match(t2.strip()):
                break
            wrapped_below = (r2[1] >= r[3] - 1) and (r2[1] - r[3] < 14) \
                and (min(r[2], r2[2]) - max(r[0], r2[0]) > 0)
            if not wrapped_below or sum(len(p) for p in parts) > MAX_CAPTION_CHARS:
                break
            r[1] = min(r[1], r2[1])
            r[3] = max(r[3], r2[3])
            r[0] = min(r[0], r2[0])
            r[2] = max(r[2], r2[2])
            parts.append(t2.strip())
            consumed.add(j)
        caps.append({
            "key": key,
            "kind": kind,
            "rect": tuple(r),
            "text": " ".join(" ".join(parts).split())[:300],
        })
    return caps


# ---------------------------------------------------------------- clustering


def _intersects(a, b, pad):
    return (a[0] - pad <= b[2] and b[0] - pad <= a[2]
            and a[1] - pad <= b[3] and b[1] - pad <= a[3])


def _union(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def cluster_rects(rects, pad=6.0, max_passes=12):
    """Transitively merge 4-tuples that come within pad pt of each other."""
    clusters = [tuple(float(v) for v in r) for r in rects
                if r and r[2] > r[0] - 1 and r[3] > r[1] - 1]
    for _ in range(max_passes):
        clusters.sort(key=lambda r: r[0])
        out = []
        merged = False
        for c in clusters:
            for i in range(len(out)):
                if _intersects(c, out[i], pad):
                    out[i] = _union(out[i], c)
                    merged = True
                    break
            else:
                out.append(c)
        clusters = out
        if not merged:
            break
    return clusters


def _rect_area(r):
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def _contained_frac(inner, outer):
    """Fraction of inner rect's area that lies inside outer rect."""
    a = _rect_area(inner)
    if a <= 0:
        return 0.0
    ov = (max(inner[0], outer[0]), max(inner[1], outer[1]),
          min(inner[2], outer[2]), min(inner[3], outer[3]))
    return _rect_area(ov) / a


# ---------------------------------------------------------------- page mining


def collect_candidate_rects(page):
    """Return (raster_rects, vector_rects) with page-level noise filtered."""
    pw, ph = page.rect.width, page.rect.height
    rasters = []
    for info in page.get_image_info():
        b = info.get("bbox")
        if not b:
            continue
        r = (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
        if r[2] <= r[0] or r[3] <= r[1]:
            continue
        if _rect_area(r) >= 200:  # logos/icons are filtered later by cluster size
            rasters.append(r)

    vectors = []
    for d in page.get_drawings():
        r = d.get("rect")
        if r is None or r.is_infinite:
            continue
        w, h = abs(r.width), abs(r.height)
        if w < 1 and h < 1:  # specks / anchor points
            continue
        x0 = min(float(r.x0), float(r.x1))
        x1 = max(float(r.x0), float(r.x1))
        y0 = min(float(r.y0), float(r.y1))
        y1 = max(float(r.y0), float(r.y1))
        rr = (x0, y0, x1, y1)
        # page-sized background fills: page border / scanned backdrop
        if w >= 0.92 * pw and h >= 0.92 * ph:
            continue
        # footnote / table separators and rules are page furniture, not art
        if (h <= 2.5 and w >= 0.5 * pw) or (w <= 2.5 and h >= 0.5 * ph):
            continue
        vectors.append(rr)
    return rasters, vectors


OVERLAP_TOL = 40.0  # a caption may sit ON the artwork's bottom edge (gap < 0)


def _matching_ids(caption_rect, clusters, below: bool, max_gap):
    """All cluster ids that are (near) below/above the caption rect with
    sufficient horizontal overlap. gap may be negative up to OVERLAP_TOL —
    captions commonly overlap the last lines of a full-page figure artwork."""
    cx0, cy0, cx1, cy1 = caption_rect
    ids = []
    for ci, (lx0, ly0, lx1, ly1) in enumerate(clusters):
        overlap = min(cx1, lx1) - max(cx0, lx0)
        if overlap <= 0:
            continue
        denom = min(cx1 - cx0, lx1 - lx0) or 1.0
        if overlap / denom < 0.35:
            continue
        gap = (ly0 - cy1) if below else (cy0 - ly1)
        if -OVERLAP_TOL <= gap <= max_gap:
            ids.append(ci)
    return ids


def anchor_captions(captions, clusters, max_gap=120.0):
    """Assign clusters to figure captions. Returns {key: {cluster_ids,
    caption_rects, caption_texts}} (multi-panel captions share one key).
    A caption owns EVERY nearby cluster (side-by-side panels), preferring the
    artwork ABOVE it; only when nothing is above do we look BELOW (captions
    placed on top, some venues)."""
    fig_caps = [c for c in captions if c["kind"] == "figure" and c["key"]]
    grouped = {}
    for cap in fig_caps:
        entry = grouped.setdefault(cap["key"], {
            "cluster_ids": set(), "caption_rects": [], "caption_texts": []})
        ids = _matching_ids(cap["rect"], clusters, below=False, max_gap=max_gap)
        if not ids:
            ids = _matching_ids(cap["rect"], clusters, below=True, max_gap=max_gap)
        entry["cluster_ids"].update(ids)
        entry["caption_rects"].append(cap["rect"])
        entry["caption_texts"].append(cap["text"])
    return grouped


def classify_cluster(cluster, rasters, vectors, pad=2.0):
    cx0, cy0, cx1, cy1 = cluster
    has_raster = any(_contained_frac((max(r[0], cx0 - pad), max(r[1], cy0 - pad),
                                     min(r[2], cx1 + pad), min(r[3], cy1 + pad)),
                                    cluster) > 0.5 for r in rasters)
    has_vector = any(_contained_frac(v, cluster) > 0.9 for v in vectors)
    if has_raster and has_vector:
        return "mixed"
    return "raster" if has_raster else "vector"


# ---------------------------------------------------------------- rendering


def render_crop(page, crop, out_path, dpi, max_px):
    zoom = dpi / 72.0
    w_px = (crop[2] - crop[0]) * zoom
    h_px = (crop[3] - crop[1]) * zoom
    scale = 1.0
    biggest = max(w_px, h_px)
    if biggest > max_px:
        scale = (max_px / biggest) * 0.998  # guard against ceil rounding at cap
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom * scale, zoom * scale),
                          clip=fitz.Rect(crop))
    pix.save(str(out_path))
    return pix.width, pix.height


def parse_pages(spec, page_count):
    """'1,3,5-8' -> [1,3,5,6,7,8]; empty -> all pages."""
    if not spec:
        return list(range(1, page_count + 1))
    pages = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            pages.update(range(int(lo), int(hi) + 1))
        else:
            pages.add(int(part))
    return sorted(p for p in pages if 1 <= p <= page_count)


# ---------------------------------------------------------------- driver


def extract_figures(pdf_path, outdir, pages=None, dpi=300.0, pad=6.0,
                    prefix=None, max_px=2400, min_cluster_area=1200.0,
                    include_uncaptioned=False, manual=None):
    """Runs the whole pipeline; writes PNGs + manifest.json into outdir.
    Returns the manifest dict. manual: (page_no, rect4, name) -> crop only."""
    pdf_path = Path(pdf_path)
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(pdf_path))
    stem = prefix or re.sub(r"[^\w\-]+", "_", pdf_path.stem)[:40]

    manifest = {
        "pdf": pdf_path.resolve().as_posix(),
        "n_pages": len(doc),
        "dpi": dpi,
        "figures": [],
        "skipped": [],
    }

    def _save_entry(page, crop, name, kind, caption=""):
        out_png = f"{stem}_{name}.png"
        w_px, h_px = render_crop(page, crop, outdir / out_png, dpi, max_px)
        manifest["figures"].append({
            "name": name,
            "page": page.number + 1,
            "kind": kind,
            "caption": caption,
            "rect_pt": [round(v, 2) for v in crop],
            "png": out_png,
            "width_px": w_px,
            "height_px": h_px,
        })

    if manual:
        page_no, rect4, name = manual
        if not (1 <= page_no <= len(doc)):
            raise ValueError(f"--page {page_no} out of range 1..{len(doc)}")
        page = doc[page_no - 1]
        crop = (max(rect4[0], page.rect.x0), max(rect4[1], page.rect.y0),
                min(rect4[2], page.rect.x1), min(rect4[3], page.rect.y1))
        _save_entry(page, crop, name or f"manual_p{page_no}", "manual")
        doc.close()
        _write_manifest(manifest, outdir)
        return manifest

    for pno in parse_pages(pages, len(doc)):
        page = doc[pno - 1]
        page_area = page.rect.width * page.rect.height
        rasters, vectors = collect_candidate_rects(page)
        clusters = cluster_rects(rasters + vectors, pad=pad)

        text_blocks = page_text_lines(page)
        captions = parse_captions(text_blocks)
        grouped = anchor_captions(captions, clusters)
        used_ids = set()

        for key, g in sorted(grouped.items(), key=lambda kv: kv[1]["caption_rects"][0][1]):
            ids = sorted(g["cluster_ids"])
            used_ids.update(ids)
            if not ids:
                # caption exists but no artwork above/below it was detected
                manifest["skipped"].append({
                    "page": pno, "kind": "caption_without_figure",
                    "caption": (g["caption_texts"] or [""])[0]})
                continue
            parts = [clusters[i] for i in ids] + list(g["caption_rects"])
            crop = parts[0]
            for p in parts[1:]:
                crop = _union(crop, p)
            crop = (crop[0] - pad, crop[1] - pad, crop[2] + pad, crop[3] + pad)
            crop = (max(crop[0], page.rect.x0), max(crop[1], page.rect.y0),
                    min(crop[2], page.rect.x1), min(crop[3], page.rect.y1))
            if _rect_area(crop) < min_cluster_area:
                continue
            # A crop that is mostly body text is a mis-anchor (table/paragraph),
            # not a figure.
            density = sum(_contained_frac(b[1], crop) for b in text_blocks)
            density /= max(_rect_area(crop), 1.0)
            kind = classify_cluster(crop, rasters, vectors)
            if density > 0.55 and kind != "raster":
                manifest["skipped"].append({
                    "page": pno, "kind": "text_dense_region",
                    "rect_pt": [round(v, 2) for v in crop],
                    "caption": (g["caption_texts"] or [""])[0]})
                continue
            notes = []
            if crop[3] >= page.rect.y1 - 2:
                # cluster reaches the page bottom: possible cross-page split,
                # the vision-review loop should check this crop for completeness
                notes.append("touches_page_bottom")
            _save_entry(page, crop, f"p{pno:02d}_fig{key}", kind,
                        (g["caption_texts"] or [""])[0])
            if notes:
                manifest["figures"][-1]["notes"] = notes

        skip_threshold = max(min_cluster_area, 0.01 * page_area)
        for ci, cl in enumerate(clusters):
            if ci in used_ids or _rect_area(cl) < skip_threshold:
                continue
            if not include_uncaptioned:
                manifest["skipped"].append({
                    "page": pno, "kind": "uncaptioned_cluster",
                    "rect_pt": [round(v, 2) for v in cl]})
                continue
            crop = (cl[0] - pad, cl[1] - pad, cl[2] + pad, cl[3] + pad)
            crop = (max(crop[0], page.rect.x0), max(crop[1], page.rect.y0),
                    min(crop[2], page.rect.x1), min(crop[3], page.rect.y1))
            _save_entry(page, crop, f"p{pno:02d}_u{ci:02d}",
                        classify_cluster(cl, rasters, vectors))

    doc.close()
    _write_manifest(manifest, outdir)
    return manifest


def _write_manifest(manifest, outdir):
    with open(Path(outdir) / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- CLI


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        description="Crop PDF figures to high-DPI PNGs (geometry-based, no "
                    "pixel guessing). See module docstring.")
    ap.add_argument("--pdf", required=True, help="input PDF path")
    ap.add_argument("--outdir", required=True, help="output directory")
    ap.add_argument("--pages", default="", help="e.g. '1,3,5-8' (default: all)")
    ap.add_argument("--dpi", type=float, default=300.0)
    ap.add_argument("--pad", type=float, default=6.0,
                    help="clustering margin + crop padding, in pt")
    ap.add_argument("--max-px", dest="max_px", type=int, default=2400,
                    help="cap longest PNG side in px (default 2400)")
    ap.add_argument("--min-cluster-area", dest="min_cluster_area",
                    type=float, default=1200.0,
                    help="ignore clusters smaller than this in pt^2")
    ap.add_argument("--prefix", default=None,
                    help="filename prefix (default: sanitized PDF stem)")
    ap.add_argument("--include-uncaptioned", action="store_true",
                    help="also export clusters that no caption matched")
    ap.add_argument("--page", type=int, default=None,
                    help="manual mode: 1-based page number (with --rect)")
    ap.add_argument("--rect", default=None,
                    help="manual mode: 'x0,y0,x1,y1' in pt (with --page)")
    ap.add_argument("--name", default=None, help="manual mode: output name tag")
    args = ap.parse_args(argv)

    pdf = Path(args.pdf)
    if not pdf.is_file():
        print(f"[ERROR] PDF not found: {pdf}")
        return 1

    manual = None
    if args.rect or args.page:
        if not (args.rect and args.page):
            print("[ERROR] manual mode needs BOTH --page and --rect")
            return 1
        try:
            rect4 = tuple(float(v) for v in args.rect.split(","))
            assert len(rect4) == 4
        except (ValueError, AssertionError):
            print("[ERROR] --rect must be 'x0,y0,x1,y1' (4 numbers, pt)")
            return 1
        manual = (args.page, rect4, args.name)

    try:
        manifest = extract_figures(
            pdf, args.outdir, pages=args.pages or None, dpi=args.dpi,
            pad=args.pad, prefix=args.prefix, max_px=args.max_px,
            min_cluster_area=args.min_cluster_area,
            include_uncaptioned=args.include_uncaptioned, manual=manual)
    except Exception as exc:  # keep CLI failure message simple and actionable
        print(f"[ERROR] {type(exc).__name__}: {exc}")
        return 1

    n = len(manifest["figures"])
    s = len(manifest["skipped"])
    print(f"[{'OK' if n else 'WARN'}] {n} figure(s), {s} skipped -> "
          f"{args.outdir}/manifest.json")
    for fig in manifest["figures"]:
        print(f"  p{fig['page']:02d} {fig['kind']:<7} {fig['png']} "
              f"({fig['width_px']}x{fig['height_px']}px)")
    return 0 if n else 2


if __name__ == "__main__":
    sys.exit(main())
