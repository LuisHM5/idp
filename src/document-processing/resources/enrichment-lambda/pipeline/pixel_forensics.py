"""Pixel forensics — layer 2 of the physical-authenticity stage, plus the ink
fingerprints layer 4 stores. numpy only (OpenCV was measured and removed from
this Lambda for size; nothing here needs it).

Two jobs:

1. `background_stats` — did this page ever go through PAPER? A sheet that was
   scanned or photographed carries sensor noise, dust and paper texture in its
   blank areas. A page exported from an office suite (or a screenshot) has
   blank areas that are exactly 255 everywhere, standard deviation 0.000.
   Measured on real letters: genuine captures 0.06–5.3, digital exports 0.000.

   It is a SOFT signal by design (the catalog grades it `review`, never
   `reject`), because genuine captures can look clean too: a scanner's
   background-removal clips whole margins to pure white, and a bilevel scan
   (CCITT/JBIG2) is 0/255 by construction — the caller abstains on those.
   Hence the verdict comes from the NOISIEST blank cells, not the average one:
   a real scan with a clipped margin still has noisy cells elsewhere, while a
   digital page has none at all.

2. Ink fingerprints — `ink_mask`, `split_ink_by_colour`, `tight_ink_bbox`,
   `ink_grid`, `grid_similarity`. A wet signature is never drawn twice the
   same way and a rubber stamp never lands twice with the same ink coverage,
   so two DIFFERENT letters whose signature or stamp are near-identical were
   produced from one reused image. The fingerprint is a small density grid of
   the ink alone, tight-cropped (so where the element sits on the page is
   irrelevant) and compared with a little shift tolerance (so a one-cell
   difference in the crop is too).
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

GRID_SIZE = 32  # fingerprint resolution: GRID_SIZE x GRID_SIZE density cells

DEFAULT_PIXELS: Dict[str, Any] = {
    "grid": 8,
    # A blank cell whose gray std is at or below this has no capture noise.
    "noise_std_min": 0.05,
    # Share of a blank cell's pixels that are pure white (>= 254).
    "pure_white_ratio_min": 0.999,
    # Fewer blank cells than this and the page can't be judged either way.
    "min_regions": 8,
    # Error-level analysis (`error_level_stats`): a JPEG re-saved at this
    # quality shows a uniform error everywhere it was compressed ONCE, and a
    # region pasted from another source (different quality, different
    # generation) stands out. Ratio of the noisiest block to the typical one.
    "ela_quality": 90,
    "ela_block": 32,
    "ela_max_ratio": 6.0,
    "ela_min_blocks": 20,
    "ela_floor": 0.002,
}

# Above this many pixels the ELA difference is computed in horizontal bands
# so a 300-dpi A4 JPEG never holds two float copies of itself at once.
_ELA_BAND_PIXELS = 6_000_000


def to_gray(rgb: Any) -> Any:
    import numpy as np

    if rgb.ndim == 2:
        return rgb.astype(np.float32)
    return (0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]).astype(np.float32)


def is_bilevel(rgb: Any) -> bool:
    """Exactly two gray levels: a 1-bit image decoded as-is. (A bilevel PDF
    image rendered through pdfium is anti-aliased and won't pass this — that
    case is flagged from the PDF structure instead, see `pdf_forensics`.)"""
    import numpy as np

    # Every pixel, not a strided sample: anti-aliased text edges are one pixel
    # thin, and a stride can step right over all of them.
    levels = np.bincount(to_gray(rgb).astype(np.uint8).ravel(), minlength=256)
    return int(np.count_nonzero(levels)) <= 2


def background_stats(rgb: Any, policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Noise statistics over the page's BLANK cells.

    `naturalBackground`: True (capture noise present), False (every blank cell
    is perfectly uniform pure white — never went through paper), None (not
    enough blank area to say, or the raster is bilevel).
    """
    import numpy as np

    cfg = {**DEFAULT_PIXELS, **{k: v for k, v in (policy or {}).items() if k in DEFAULT_PIXELS}}
    gray = to_gray(rgb)
    height, width = gray.shape
    grid = max(int(cfg["grid"]), 2)
    # Ignore a thin outer frame: scanner edges and photo borders are dark and
    # noisy for reasons that say nothing about the paper.
    my, mx = int(height * 0.03), int(width * 0.03)
    inner = gray[my:height - my, mx:width - mx]
    ih, iw = inner.shape
    ch, cw = ih // grid, iw // grid
    if ch < 8 or cw < 8:
        return {"naturalBackground": None, "regions": 0, "blank": False}

    stds, whites = [], []
    ink_cells = 0
    for row in range(grid):
        for col in range(grid):
            cell = inner[row * ch:(row + 1) * ch, col * cw:(col + 1) * cw]
            if float((cell < 128).mean()) > 0.001 or float(cell.mean()) < 200:
                ink_cells += 1
                continue
            stds.append(float(cell.std()))
            whites.append(float((cell >= 254).mean()))

    result: Dict[str, Any] = {
        "regions": len(stds),
        # No ink anywhere: the blank back of a sheet, not a page to judge.
        "blank": ink_cells == 0,
    }
    if len(stds) < int(cfg["min_regions"]) or is_bilevel(rgb):
        result["naturalBackground"] = None
        return result

    std_p90 = float(np.percentile(stds, 90))
    white_p10 = float(np.percentile(whites, 10))
    result.update({
        "noiseStdMedian": round(float(np.median(stds)), 4),
        "noiseStdP90": round(std_p90, 4),
        "pureWhiteRatioP10": round(white_p10, 4),
        "naturalBackground": not (
            std_p90 <= float(cfg["noise_std_min"]) and white_p10 >= float(cfg["pure_white_ratio_min"])
        ),
    })
    return result


def error_level_stats(jpeg_bytes: bytes, policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Error-level analysis of a JPEG: re-encode it once more and look at how
    much each block changes. A file compressed once changes about the same
    everywhere; a region that came from somewhere else (a pasted crop, a
    patch saved at another quality) changes differently, and stands out
    against its neighbours.

    `errorLevelUniform`: True (no block stands out), False (one does), None
    (not a JPEG, too small, or too little content to compare). Soft by
    nature — text edges and a heavily recompressed genuine scan can look
    non-uniform too — so the block grid is median-filtered and the ratio is
    taken against a floor. Only ever run on bytes the FILE holds (the upload
    or a scan's `/DCTDecode` stream), never on the pipeline's own raster.
    """
    import numpy as np

    cfg = {**DEFAULT_PIXELS, **{k: v for k, v in (policy or {}).items() if k in DEFAULT_PIXELS}}
    result: Dict[str, Any] = {"errorLevelUniform": None}
    if not jpeg_bytes or jpeg_bytes[:2] != b"\xff\xd8":
        result["reason"] = "not_jpeg"
        return result
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(jpeg_bytes)) as img:
            if img.format != "JPEG":
                result["reason"] = "not_jpeg"
                return result
            quant = getattr(img, "quantization", None) or {}
            if quant:
                table = list(quant.get(0) or [])
                if table:
                    result["estimatedQuality"] = _quality_from_table(table)
            original = img.convert("RGB")
            width, height = original.size
            if max(width, height) < 256:
                result["reason"] = "too_small"
                return result
            buf = io.BytesIO()
            original.save(buf, format="JPEG", quality=int(cfg["ela_quality"]))
            buf.seek(0)
            with Image.open(buf) as resaved:
                resaved = resaved.convert("RGB")
                block = max(int(cfg["ela_block"]), 8)
                rows, cols = height // block, width // block
                if rows < 2 or cols < 2:
                    result["reason"] = "too_small"
                    return result
                levels = np.zeros((rows, cols), dtype=np.float32)
                energy = np.zeros((rows, cols), dtype=np.float32)
                band_rows = max(1, _ELA_BAND_PIXELS // max(width * block, 1))
                for r0 in range(0, rows, band_rows):
                    r1 = min(rows, r0 + band_rows)
                    y0, y1 = r0 * block, r1 * block
                    a = np.asarray(original.crop((0, y0, cols * block, y1)), dtype=np.float32)
                    b = np.asarray(resaved.crop((0, y0, cols * block, y1)), dtype=np.float32)
                    diff = np.abs(a - b).mean(axis=2)
                    gray = a.mean(axis=2)
                    # Re-compression error scales with local detail: a flat
                    # margin changes ~0 and a text edge changes a lot whatever
                    # the file's history. Normalizing by edge energy compares
                    # like with like, so a text block from another source is
                    # what stands out — not every text block.
                    gx = np.abs(np.diff(gray, axis=1, append=gray[:, -1:]))
                    gy = np.abs(np.diff(gray, axis=0, append=gray[-1:, :]))
                    levels[r0:r1] = diff.reshape(r1 - r0, block, cols, block).mean(axis=(1, 3))
                    energy[r0:r1] = (gx + gy).reshape(r1 - r0, block, cols, block).mean(axis=(1, 3))
    except Exception as e:
        result["reason"] = f"error: {str(e)[:80]}"
        return result

    content = energy >= 1.0
    blocks = int(content.sum())
    result["blocks"] = blocks
    if blocks < int(cfg["ela_min_blocks"]):
        result["reason"] = "too_little_content"
        return result
    filtered = _median3(np.where(content, levels / np.maximum(energy, 0.5), 0.0))
    values = filtered[content]
    median = float(np.median(values))
    peak = float(values.max())
    ratio = peak / max(median, float(cfg["ela_floor"]))
    result.update({
        "median": round(median, 5),
        "p95": round(float(np.percentile(values, 95)), 5),
        "max": round(peak, 5),
        "ratio": round(ratio, 2),
        "errorLevelUniform": ratio <= float(cfg["ela_max_ratio"]),
    })
    if ratio > float(cfg["ela_max_ratio"]):
        row, col = np.unravel_index(int(np.argmax(np.where(content, filtered, -1.0))), filtered.shape)
        result["peakBlock"] = [round(float(col) / cols, 3), round(float(row) / rows, 3),
                               round(float(col + 1) / cols, 3), round(float(row + 1) / rows, 3)]
    return result


def _median3(grid: Any) -> Any:
    """3x3 median filter of a small 2-D grid, edges padded by replication."""
    import numpy as np

    padded = np.pad(grid, 1, mode="edge")
    stack = np.stack([padded[dy:dy + grid.shape[0], dx:dx + grid.shape[1]] for dy in range(3) for dx in range(3)])
    return np.median(stack, axis=0)


def _quality_from_table(table: Any) -> Optional[int]:
    """Rough IJG quality of a luminance quantization table (the standard
    table scaled): informational only."""
    _STANDARD = [16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55]
    try:
        values = [int(v) for v in list(table)[:16]]
        if len(values) < 16 or any(v <= 0 for v in _STANDARD):
            return None
        scale = sum(values) / sum(_STANDARD) * 100.0
        quality = 5000.0 / scale if scale >= 100 else 100.0 - scale / 2.0
        return int(max(1, min(100, round(quality))))
    except Exception:
        return None


# ------------------------------------------------------------------ ink


def ink_mask(rgb: Any) -> Any:
    """Boolean mask of ink pixels in a crop, relative to ITS OWN paper level —
    a gray photocopy and a bright phone photo both resolve to "clearly darker
    than the background here"."""
    import numpy as np

    gray = to_gray(rgb)
    background = float(np.percentile(gray, 90))
    threshold = background - max(40.0, 0.2 * background)
    return gray < threshold


def split_ink_by_colour(rgb: Any) -> Tuple[Any, Any]:
    """(dark_mask, colour_mask): black/gray ink vs saturated ink. A blue stamp
    struck over a black signature is how the two are told apart when their
    boxes overlap."""
    import numpy as np

    mask = ink_mask(rgb)
    if rgb.ndim == 2:
        return mask, np.zeros_like(mask)
    channels = rgb[:, :, :3].astype(np.int16)
    saturated = (channels.max(axis=2) - channels.min(axis=2)) > 40
    return mask & ~saturated, mask & saturated


def dominant_ink(rgb: Any) -> str:
    """'NEGRA' | 'COLOR' | 'SIN_TINTA' — coarse, for the audit trail."""
    dark, colour = split_ink_by_colour(rgb)
    dark_n, colour_n = int(dark.sum()), int(colour.sum())
    if dark_n + colour_n < 20:
        return "SIN_TINTA"
    return "COLOR" if colour_n > dark_n else "NEGRA"


def tight_ink_bbox(mask: Any, trim: float = 0.002) -> Optional[Tuple[int, int, int, int]]:
    """Pixel box around the ink, shaving off each side the outermost rows and
    columns that together hold less than `trim` of the ink — so a dust speck
    at the edge of a generous crop doesn't stretch the box.

    Trimmed by ink MASS, not by row density, on purpose: density depends on how
    wide the crop happens to be, and the same signature cropped with more
    margin would get a different box and a different fingerprint. Mass depends
    only on the ink. None when there is no ink."""
    import numpy as np

    total = float(mask.sum()) if mask.size else 0.0
    if total <= 0:
        return None

    def span(sums: Any) -> Tuple[int, int]:
        forward = np.cumsum(sums) / total
        backward = np.cumsum(sums[::-1]) / total
        first = int(np.argmax(forward >= trim))
        last = len(sums) - int(np.argmax(backward >= trim))
        return first, max(last, first + 1)

    y0, y1 = span(mask.sum(axis=1))
    x0, x1 = span(mask.sum(axis=0))
    return x0, y0, x1, y1


def ink_grid(mask: Any, size: int = GRID_SIZE) -> Optional[bytes]:
    """The fingerprint: ink density per cell of a `size`x`size` grid laid over
    the tight ink box, one byte per cell (0..255). None without enough ink."""
    import numpy as np

    box = tight_ink_bbox(mask)
    if box is None:
        return None
    x0, y0, x1, y1 = box
    ink = mask[y0:y1, x0:x1].astype(np.float32)
    if ink.shape[0] < 4 or ink.shape[1] < 4 or float(ink.sum()) < 30:
        return None
    # Area-average down to the grid via an integral image: exact block means
    # for any crop size, no interpolation library needed.
    integral = np.pad(ink.cumsum(axis=0).cumsum(axis=1), ((1, 0), (1, 0)))
    ys = np.linspace(0, ink.shape[0], size + 1).round().astype(int)
    xs = np.linspace(0, ink.shape[1], size + 1).round().astype(int)
    sums = (integral[ys[1:], :][:, xs[1:]] - integral[ys[:-1], :][:, xs[1:]]
            - integral[ys[1:], :][:, xs[:-1]] + integral[ys[:-1], :][:, xs[:-1]])
    areas = np.outer(np.diff(ys), np.diff(xs)).astype(np.float32)
    areas[areas == 0] = 1.0
    return (np.clip(sums / areas, 0.0, 1.0) * 255).round().astype(np.uint8).tobytes()


def grid_similarity(a: Optional[bytes], b: Optional[bytes], max_shift: int = 2, size: int = GRID_SIZE) -> float:
    """Weighted Jaccard (sum of mins / sum of maxes) between two fingerprints,
    best over shifts of up to `max_shift` cells in each axis. 1.0 = the same
    ink; unrelated signatures land far lower."""
    import numpy as np

    if not a or not b or len(a) != size * size or len(b) != size * size:
        return 0.0
    ga = np.frombuffer(a, dtype=np.uint8).reshape(size, size).astype(np.float32)
    gb = np.frombuffer(b, dtype=np.uint8).reshape(size, size).astype(np.float32)
    best = 0.0
    for dy in range(-max_shift, max_shift + 1):
        for dx in range(-max_shift, max_shift + 1):
            ya0, ya1 = max(dy, 0), size + min(dy, 0)
            xa0, xa1 = max(dx, 0), size + min(dx, 0)
            sa = ga[ya0:ya1, xa0:xa1]
            sb = gb[ya0 - dy:ya1 - dy, xa0 - dx:xa1 - dx]
            # Cells shifted off the grid still count against the match.
            denom = float(np.maximum(sa, sb).sum()) + (float(ga.sum()) - float(sa.sum())) \
                + (float(gb.sum()) - float(sb.sum()))
            if denom > 0:
                best = max(best, float(np.minimum(sa, sb).sum()) / denom)
    return round(best, 4)


# ------------------------------------------------------------------ region proposals

# Three passes over the page's ink, each a (mask, horizontal gap, vertical gap)
# in grid cells. Why three, measured on real letters:
#   fine    dark ink, no vertical joining: a signature stays apart from the
#           printed name under it, and every text line is its own thin blob
#           (dropped below as text).
#   block   dark ink, joined vertically: a black stamp whose border is broken
#           is just 3-4 short text lines to the fine pass — and gets dropped as
#           text. Joined, it is a compact block; a PARAGRAPH joined the same
#           way is huge and is dropped by size instead.
#   colour  saturated ink, joined generously: stamps are usually struck in
#           blue and often have no border at all.
_REGION_PASSES = (("fine", "dark", 2, 0), ("block", "dark", 3, 2), ("colour", "colour", 4, 3))


def propose_regions(rgb: Any, max_regions: int = 12, cell_fraction: float = 0.005) -> list:
    """Where on the page is there ink that is NOT body text? Deterministic
    region proposals, each `{"box": [x0, y0, x1, y1] (0..1), "ink", "area"}`.

    This exists because a vision model asked WHERE the signature is answers
    badly (measured: boxes ~20% of the page height off, landing on the body
    text), while the same model asked WHAT a given crop is answers well. So the
    geometry is computed here, and the model only classifies.
    """
    import numpy as np

    height, width = rgb.shape[:2]
    cell = max(int(width * cell_fraction), 2)
    gh, gw = height // cell, width // cell
    if gh < 20 or gw < 20:
        return []
    dark, colour = split_ink_by_colour(rgb)
    masks = {"dark": _without_rules(dark), "colour": colour}

    regions = []
    for name, ink, gap_x, gap_y in _REGION_PASSES:
        grid = masks[ink][:gh * cell, :gw * cell].reshape(gh, cell, gw, cell).mean(axis=(1, 3)) > 0.03
        for x0, y0, x1, y1, cells in _components(grid, gap_x, gap_y):
            box = [x0 * cell / width, y0 * cell / height, x1 * cell / width, y1 * cell / height]
            w, h = box[2] - box[0], box[3] - box[1]
            if w * h < 0.0008 or cells < 8:
                continue  # a speck
            if ink == "dark" and h < 0.024 and w / h > 3.5:
                continue  # a line of text
            if ink == "dark" and w > 0.5 and h > 0.05:
                continue  # a paragraph
            on_edge = box[0] < 0.01 or box[1] < 0.008 or box[2] > 0.99 or box[3] > 0.992
            if on_edge and (w < 0.12 or h < 0.05) and w * h < 0.03:
                continue  # scanner border / page shadow
            regions.append({"box": [round(float(v), 4) for v in box], "ink": ink, "pass": name,
                            "area": round(float(w * h), 5)})

    # The same ink found by two passes: keep the TIGHTER box (a signature alone
    # beats signature+name), unless the wider one adds real extent.
    # A FRAGMENT is not a region: the colour pass finds the saturated letters of
    # a stamp as a blob of their own, several times smaller than the stamp that
    # contains them (measured: a model then took "RED" out of a stamp for the
    # company logo). Not to be confused with a signature inside the block
    # signature+printed-name, which is only ~2x bigger and must survive.
    regions = [
        r for r in regions
        if not any(o is not r and o["area"] >= 4 * r["area"] and _covered(r["box"], o["box"]) > 0.85
                   for o in regions)
    ]
    regions.sort(key=lambda r: r["area"])
    kept: list = []
    for region in regions:
        if any(_covered(kept_region["box"], region["box"]) > 0.85 and region["area"] < kept_region["area"] * 1.6
               for kept_region in kept):
            continue
        kept.append(region)
    # Colour first (stamps and blue-ink signatures are the likeliest elements),
    # then by size: the cap is a cost guardrail, and must not cost a signature.
    kept.sort(key=lambda r: (r["ink"] != "colour", -r["area"]))
    return kept[:max_regions]


def _without_rules(mask: Any, min_run_fraction: float = 0.06) -> Any:
    """The mask minus long horizontal strokes: the signature line and the top
    and bottom of a stamp's frame. Measured need: a signature line ending 0.6%
    of the page away from a stamp's border welds signature, printed name and
    stamp into one wide blob — indistinguishable from a paragraph. Text never
    contains an unbroken horizontal run this long (letters have gaps); a ruled
    line is nothing else. Three rows are pooled first so a slightly skewed line
    still reads as one run."""
    import numpy as np

    height, width = mask.shape
    run = max(int(width * min_run_fraction), 8)
    if width <= run + 1:
        return mask
    pooled = mask.copy()
    pooled[1:, :] |= mask[:-1, :]
    pooled[:-1, :] |= mask[1:, :]
    sums = np.cumsum(np.pad(pooled.astype(np.int32), ((0, 0), (1, 0))), axis=1)
    starts = (sums[:, run:] - sums[:, :-run]) == run          # a full run begins here
    covered = np.zeros_like(mask)
    hits = np.cumsum(np.pad(starts.astype(np.int32), ((0, 0), (run, run))), axis=1)
    covered[:, :] = (hits[:, run:run + width] - hits[:, :width]) > 0   # any run covering this pixel
    return mask & ~covered


def _covered(inner: Any, outer: Any) -> float:
    """Share of `inner` that lies inside `outer`."""
    ix = max(min(inner[2], outer[2]) - max(inner[0], outer[0]), 0.0)
    iy = max(min(inner[3], outer[3]) - max(inner[1], outer[1]), 0.0)
    area = (inner[2] - inner[0]) * (inner[3] - inner[1])
    return (ix * iy) / area if area > 0 else 0.0


def _components(grid: Any, gap_x: int, gap_y: int) -> list:
    """Connected components of a boolean grid after bridging gaps of up to
    `gap_x`/`gap_y` cells — plain numpy + a flood fill (no scipy/OpenCV in this
    Lambda). Returns `(x0, y0, x1, y1, ink_cells)` in grid cells."""
    import numpy as np

    joined = grid.copy()
    for shift in range(1, gap_x + 1):
        joined[:, shift:] |= grid[:, :-shift]
        joined[:, :-shift] |= grid[:, shift:]
    bridged = joined.copy()
    for shift in range(1, gap_y + 1):
        bridged[shift:, :] |= joined[:-shift, :]
        bridged[:-shift, :] |= joined[shift:, :]

    gh, gw = bridged.shape
    seen = np.zeros(bridged.shape, dtype=bool)
    out = []
    for y, x in zip(*np.nonzero(bridged)):
        if seen[y, x]:
            continue
        stack = [(int(y), int(x))]
        seen[y, x] = True
        y0 = y1 = int(y)
        x0 = x1 = int(x)
        while stack:
            a, b = stack.pop()
            y0, y1, x0, x1 = min(y0, a), max(y1, a), min(x0, b), max(x1, b)
            for u in (a - 1, a, a + 1):
                for v in (b - 1, b, b + 1):
                    if 0 <= u < gh and 0 <= v < gw and bridged[u, v] and not seen[u, v]:
                        seen[u, v] = True
                        stack.append((u, v))
        out.append((x0, y0, x1 + 1, y1 + 1, int(grid[y0:y1 + 1, x0:x1 + 1].sum())))
    return out


def fingerprint(rgb_crop: Any, ink: str = "all") -> Optional[bytes]:
    """Fingerprint of a crop's ink. `ink`: "all" | "dark" | "colour" — used to
    fingerprint a signature and a stamp separately when they overlap."""
    if rgb_crop is None or getattr(rgb_crop, "size", 0) == 0:
        return None
    if ink == "all":
        return ink_grid(ink_mask(rgb_crop))
    dark, colour = split_ink_by_colour(rgb_crop)
    return ink_grid(dark if ink == "dark" else colour)
