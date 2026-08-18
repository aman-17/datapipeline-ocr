"""Table and chart density, measured rather than guessed.

Stage 2 already records `n_drawings` as a proxy for "ruled tables and charts".
That proxy is too coarse to *steer acquisition*: it cannot tell a bordered table
from a bar chart, it scores zero on the borderless tables we most want, and it
says nothing about whether a table is 3x4 or 40x12. This module turns the same
free signals into the two quantities a collection campaign actually needs —

    is this page a COMPLEX table page?
    is this page a CHART page?

and it does so deterministically, on the CPU, so the answer is available at
acquisition time and can be used to decide what to collect more of.

Two warnings earned by probing real files:

1. `find_tables(strategy="text")` reports a table on almost every prose page.
   Left unfiltered it is a false-positive machine — on a 119-page arXiv paper it
   claimed a table on 12 of the first 12 pages. Every text-strategy candidate
   therefore has to survive `_is_real_table`, which asks the questions that
   separate a financial table from a paragraph: are the cells short, are they
   numeric, are there at least three columns.

2. A chart is a *region*, not a page property. Counting vector primitives per
   page conflates a page bearing one bar chart with a page bearing a ruled table
   of the same stroke count. So primitives are clustered spatially first and each
   cluster is judged on its own shape.

Raster charts — a chart flattened into a JPEG, which is what a scanned annual
report contains — are explicitly NOT detected here. Nothing that is free can see
them. They are flagged `possible_raster_figure` for a later model pass, and the
counts this module reports are therefore a LOWER BOUND on chart density for
scanned sources.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

import pymupdf

from .inspector import TEXT_LAYER_MIN_CHARS

# --- what counts as a real table -------------------------------------------
# A table needs at least this many columns to be a table rather than a list. Two
# columns is a definition list, a table of contents, or a verse layout.
MIN_COLS = 3
MIN_ROWS = 4
# Prose cells are long. A financial table's cells are labels and numbers.
MAX_MEAN_CELL_CHARS = 28
# At least this share of filled cells must look numeric. Prose blocks fail here
# even when their alignment happens to fool the text strategy.
MIN_NUMERIC_RATIO = 0.15

# --- what counts as COMPLEX -------------------------------------------------
# Any one of these makes a real table hard, and they are the failure modes the
# corpus is meant to target: long tables, wide tables, no ruling to key off,
# and merged/spanned cells.
COMPLEX_MIN_CELLS = 40
COMPLEX_MIN_COLS = 6
COMPLEX_MIN_ROWS_BORDERLESS = 8

# --- chart region thresholds ------------------------------------------------
CHART_MIN_PRIMITIVES = 8
CHART_MIN_AREA_FRAC = 0.01
CHART_MAX_AREA_FRAC = 0.75
CLUSTER_GAP_PT = 12.0        # primitives closer than this belong to one figure

# Thinner than this in either dimension and a primitive is a RULE, not a shape.
# Every phantom chart measured on this corpus was ruling: column rules under a
# financial table, accounting underlines, cell borders. A group made only of
# rules carries no chart evidence at all, whatever colours it is drawn in.
HAIRLINE_PT = 3.0
# A fill spanning (nearly) all of its region is a background wash or a full-width
# row band. Never a data mark: a bar chart leaves room for its axis.
BAND_SPAN_FRAC = 0.9
# A data series is ONE path with many segments. Table rules are one segment per
# path, so summing segments over a group — which is what this used to do — reads
# twelve hairline rules as a twelve-segment polyline.
LINE_PATH_MIN_SEGMENTS = 8
# ...and a data series slopes. Nothing drawn in a table is diagonal, so diagonal
# ink is the one piece of line-chart evidence ruling cannot fake.
MIN_DIAGONAL_SEGMENTS = 4
# ...and it is drawn at plot scale. Without this, the 2pt GPO seal stamped on
# every govinfo page — one path, 18 segments, several of them diagonal — is a
# line chart on any page that carries it.
CHART_PATH_MIN_PT = 24.0
# Smooth series and pie wedges: SEVERAL large curved paths. One is a logo or a
# rounded callout — an infographic page of two circular pictograms was a chart —
# and without the size floor a BIS event calendar, a column of nineteen curved
# pictograms beside a table, is a pie. A wedge is only one or two arcs, so it is
# recognised by the hub it radiates from rather than by its segment count.
CURVE_PATH_MIN_SEGMENTS = 4
CURVE_PATH_MIN_PT = 24.0
MIN_CURVED_PATHS = 3
# Bars standing on one baseline. Three was measured too lax: three left-aligned
# highlight boxes over three consecutive text lines are three "bars".
MIN_BARS = 4
# ...and bars encode data, so they differ by more than a rounding error, and by
# more than one step. Two stacks of shaded label cells whose left margins differ
# by 8% are not a chart, and neither is a two-deep merged header row — fifteen
# cells of exactly two heights, which is how a JPL trade-study table read as a
# bar chart.
BAR_LENGTH_SPREAD = 0.25
MIN_BAR_LENGTHS = 3
# Fallback for charts that are neither polyline, wedge nor bar: a 3-D bar chart
# and a pie whose wedges are single arcs both reach the detector only this way.
# Two colours is a two-tone table header, so this needs three — and the marks
# must be a real share of the region, because a spreadsheet header band is nine
# coloured cells adrift in two hundred rules.
MIN_MARKS = 6
MIN_MARK_COLORS = 3
MIN_MARK_SHARE = 0.1

# --- raster thresholds ------------------------------------------------------
# An image covering more than this much of the page IS the page: a scan, not a
# figure printed on one. Both raster tests read it, so they cannot disagree
# about where "a picture on the page" ends and "a picture of a page" begins.
SCAN_IMAGE_COVER_FRAC = 0.80
RASTER_FIGURE_MIN_FRAC = 0.05

# Numbers as they appear in financial tables: 1,234.5  (89)  -3.2%  $1.2m  12
_NUMERIC = re.compile(r"^[\s(\[]*[-+$€£¥]?\s*\d[\d,.\s]*\s*[%)\]a-zA-Z]{0,3}\s*$")


def _is_numeric(text: str) -> bool:
    t = text.strip()
    return bool(t) and bool(_NUMERIC.match(t))


@dataclass(slots=True)
class TableFacts:
    """One detected table, with the measurements that decide 'complex'."""
    n_rows: int
    n_cols: int
    n_cells: int
    n_filled: int
    numeric_ratio: float
    mean_cell_chars: float
    area_frac: float
    ruled: bool                  # found by the line strategy == has visible rules
    borderless: bool             # found only by the text strategy
    spanned: bool                # merged cells / header narrower than body
    complex: bool

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ChartFacts:
    """One vector-graphic region that looks like a chart."""
    kind: str                    # bar | line | pie | mixed
    n_primitives: int
    n_distinct_colors: int
    area_frac: float
    n_labels: int                # short text runs adjacent to the region

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PageDensity:
    page_no: int
    tables: list[TableFacts] = field(default_factory=list)
    charts: list[ChartFacts] = field(default_factory=list)
    possible_raster_figure: bool = False
    # False on a rasterised page, where a chart is pixels and no free signal can
    # see it. Distinguishing "measured, none present" from "could not measure"
    # is what keeps a scanned corpus from silently reporting zero charts.
    chart_measurable: bool = True
    # False when there is no text layer. Table detection reads glyph positions,
    # so an un-OCR'd scan yields zero tables no matter how many it contains —
    # scoring such a page 'not dense' would reject the scanned financial
    # documents this corpus most wants.
    table_measurable: bool = True
    error: str | None = None

    # -- the two questions the campaign asks ---------------------------------
    @property
    def n_complex_tables(self) -> int:
        return sum(1 for t in self.tables if t.complex)

    @property
    def is_table_page(self) -> bool:
        return self.n_complex_tables > 0

    @property
    def is_chart_page(self) -> bool:
        return bool(self.charts)

    @property
    def is_dense(self) -> bool:
        """The corpus-level predicate: does this page carry the signal we want."""
        return self.is_table_page or self.is_chart_page

    @property
    def measurable(self) -> bool:
        """Could this page be judged at all by free signals?

        An un-OCR'd scan is opaque to every detector here. It must be counted
        separately rather than as a negative, because a corpus of scanned annual
        reports would otherwise score 0% dense and be dropped.
        """
        return self.table_measurable or self.chart_measurable

    def as_row(self) -> dict[str, Any]:
        biggest = max((t.n_rows * t.n_cols for t in self.tables), default=0)
        return {
            "page_no": self.page_no,
            "n_tables": len(self.tables),
            "n_complex_tables": self.n_complex_tables,
            "n_borderless_tables": sum(1 for t in self.tables if t.borderless),
            "n_spanned_tables": sum(1 for t in self.tables if t.spanned),
            "max_table_cells": biggest,
            "n_charts": len(self.charts),
            "chart_kinds": ",".join(sorted({c.kind for c in self.charts})) or None,
            "possible_raster_figure": self.possible_raster_figure,
            "chart_measurable": self.chart_measurable,
            "table_measurable": self.table_measurable,
            "is_table_page": self.is_table_page,
            "is_chart_page": self.is_chart_page,
            "error": self.error,
        }


# --------------------------------------------------------------------------
# tables
# --------------------------------------------------------------------------
def _table_metrics(table: Any, page_area: float) -> tuple[list[list[str]], float, float, float]:
    """Extract cell text and the three shape statistics that gate a real table."""
    try:
        rows = table.extract() or []
    except Exception:  # noqa: BLE001 — malformed content stream inside the bbox
        return [], 0.0, 0.0, 0.0
    cells = [[(c or "") for c in row] for row in rows]
    filled = [c.strip() for row in cells for c in row if c and c.strip()]
    numeric_ratio = (sum(1 for c in filled if _is_numeric(c)) / len(filled)) if filled else 0.0
    mean_chars = (sum(len(c) for c in filled) / len(filled)) if filled else 0.0
    x0, y0, x1, y1 = table.bbox
    area_frac = abs((x1 - x0) * (y1 - y0)) / page_area if page_area else 0.0
    return cells, numeric_ratio, mean_chars, area_frac


def _is_real_table(cells: Sequence[Sequence[str]], numeric_ratio: float,
                   mean_chars: float) -> bool:
    """The gate that stops prose being counted as a table.

    `strategy="text"` infers structure from whitespace alignment, and justified
    prose is aligned. What prose cannot fake all at once is: three or more
    columns, short cells, and numbers in them.
    """
    n_rows = len(cells)
    n_cols = max((len(r) for r in cells), default=0)
    if n_rows < MIN_ROWS or n_cols < MIN_COLS:
        return False
    if mean_chars > MAX_MEAN_CELL_CHARS:
        return False
    return numeric_ratio >= MIN_NUMERIC_RATIO


def _is_spanned(table: Any, cells: Sequence[Sequence[str]]) -> bool:
    """Merged cells, detected two ways.

    PyMuPDF leaves a merged cell as None in `row.cells`, and a table whose header
    is narrower than its body is the classic spanned-header financial layout
    ("Year ended December 31" straddling three year columns).
    """
    try:
        for row in table.rows:
            if any(c is None for c in row.cells):
                return True
    except Exception:  # noqa: BLE001
        pass
    body_cols = max((len(r) for r in cells), default=0)
    try:
        header = table.header
        if header is not None and header.names:
            named = sum(1 for n in header.names if n and n.strip())
            if 0 < named < body_cols:
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _bbox_iou(a: Sequence[float], b: Sequence[float]) -> float:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = abs((a[2] - a[0]) * (a[3] - a[1]))
    area_b = abs((b[2] - b[0]) * (b[3] - b[1]))
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _detect_tables(page: pymupdf.Page) -> tuple[list[TableFacts], list[Sequence[float]]]:
    """Detect tables with both strategies and keep the ones that survive the gate.

    Running both is the point: the line strategy finds ruled tables and tells us
    they are ruled; the text strategy finds the borderless ones the line strategy
    is blind to. A table found by both is ruled; one found only by text is
    borderless — and borderless is exactly the hard slice worth collecting.

    Returns the facts and the accepted bounding boxes, which `measure_page` uses
    to drop the tables that are really a chart's axis labels; table detection is
    far too expensive to run twice.

    The boxes deliberately do NOT feed back into `find_charts` as an exclusion
    zone, which is what this docstring used to promise. It cannot work: on a
    govinfo chart page the plot's own gridlines are themselves reported as a
    ruled table covering the whole plot, so excluding primitives inside accepted
    table boxes would delete the chart rather than protect it. A table's ruling
    is kept out of the chart count by the evidence tests in `find_charts`
    instead.
    """
    page_area = abs(page.rect.width * page.rect.height) or 1.0
    ruled_boxes: list[Sequence[float]] = []
    kept_boxes: list[Sequence[float]] = []
    out: list[TableFacts] = []

    for strategy in ("lines_strict", "text"):
        try:
            found = page.find_tables(strategy=strategy).tables
        except Exception:  # noqa: BLE001 — table finder is not crash-proof on junk
            continue
        for table in found:
            cells, numeric_ratio, mean_chars, area_frac = _table_metrics(table, page_area)
            if not cells:
                continue
            is_ruled = strategy == "lines_strict"
            if is_ruled:
                # Ruled tables get a laxer gate: visible rules are strong evidence
                # of tabular intent, so short/wide ruled tables still count.
                n_cols = max(len(r) for r in cells)
                if len(cells) < 2 or n_cols < 2:
                    continue
                # ...but a SMALL bordered box full of prose is a callout, not a
                # table. Only small ones: a ruled table of descriptions (a BIS
                # event calendar) is long-celled and still perfectly tabular.
                if (len(cells) < MIN_ROWS and n_cols < MIN_COLS
                        and mean_chars > MAX_MEAN_CELL_CHARS):
                    continue
                ruled_boxes.append(table.bbox)
            else:
                if not _is_real_table(cells, numeric_ratio, mean_chars):
                    continue
                # Already reported as a ruled table? Then it is not borderless.
                if any(_bbox_iou(table.bbox, rb) > 0.5 for rb in ruled_boxes):
                    continue

            n_rows = len(cells)
            n_cols = max(len(r) for r in cells)
            n_cells = sum(len(r) for r in cells)
            n_filled = sum(1 for r in cells for c in r if c and c.strip())
            spanned = _is_spanned(table, cells)
            borderless = not is_ruled
            complex_ = (
                n_cells >= COMPLEX_MIN_CELLS
                or n_cols >= COMPLEX_MIN_COLS
                # A merged cell only means "hard" on something table-shaped.
                # `_is_spanned` fires on any None in row.cells, which is routine
                # for a bordered callout, so on its own it promoted 2x2 prose
                # boxes to complex and made their page a table page.
                or (spanned and (n_rows >= MIN_ROWS or n_cols >= MIN_COLS))
                or (borderless and n_rows >= COMPLEX_MIN_ROWS_BORDERLESS)
            )
            out.append(TableFacts(
                n_rows=n_rows, n_cols=n_cols, n_cells=n_cells, n_filled=n_filled,
                numeric_ratio=round(numeric_ratio, 3),
                mean_cell_chars=round(mean_chars, 1),
                area_frac=round(area_frac, 4),
                ruled=is_ruled, borderless=borderless, spanned=spanned,
                complex=complex_,
            ))
            kept_boxes.append(table.bbox)
    return out, kept_boxes


def find_tables(page: pymupdf.Page) -> list[TableFacts]:
    """Tables on a page, filtered to the ones that are really tables."""
    return _detect_tables(page)[0]


# --------------------------------------------------------------------------
# charts
# --------------------------------------------------------------------------
@dataclass(slots=True)
class _Prim:
    """One drawing PATH, reduced to what chart classification needs.

    The segment counts are per path on purpose. A line chart is one path with
    many segments; a ruled table is many paths with one segment each. Totalling
    segments over a region throws that distinction away, and it was the reason a
    page of hairline rules read as a line chart.
    """
    x0: float
    y0: float
    x1: float
    y1: float
    fill: tuple | None
    n_rects: int
    n_lines: int
    n_curves: int
    n_diagonals: int             # line segments that are neither horizontal nor vertical

    @property
    def height(self) -> float:
        return abs(self.y1 - self.y0)

    @property
    def width(self) -> float:
        return abs(self.x1 - self.x0)

    @property
    def is_hairline(self) -> bool:
        """A stroke rather than a shape: too thin in one dimension to carry data."""
        return min(self.width, self.height) < HAIRLINE_PT


def _is_white(fill: tuple | None) -> bool:
    """White fill == invisible on white paper: a knockout box, not a data mark.

    Borderless statement tables are routinely drawn as a lattice of white cell
    rectangles (a Marion County ACFR page is 20 of them). Counting those as
    coloured shapes made a chart out of an unshaded table.
    """
    if not fill:
        return False
    return min(fill) >= 0.97 if len(fill) <= 3 else max(fill) <= 0.03


def _primitives(page: pymupdf.Page) -> list[_Prim]:
    try:
        drawings = page.get_drawings()
    except Exception:  # noqa: BLE001
        return []
    prims: list[_Prim] = []
    for d in drawings:
        rect = d.get("rect")
        if rect is None:
            continue
        n_rects = n_lines = n_curves = n_diag = 0
        for item in d.get("items", ()):
            kind = item[0]
            if kind == "re":
                n_rects += 1
            elif kind == "l":
                n_lines += 1
                p1, p2 = item[1], item[2]
                if abs(p1.x - p2.x) > 1.0 and abs(p1.y - p2.y) > 1.0:
                    n_diag += 1
            elif kind in ("c", "qu"):
                n_curves += 1
        fill = d.get("fill")
        prims.append(_Prim(rect.x0, rect.y0, rect.x1, rect.y1,
                           tuple(fill) if fill else None,
                           n_rects, n_lines, n_curves, n_diag))
    return prims


def _cluster(prims: Sequence[_Prim], gap: float = CLUSTER_GAP_PT) -> list[list[_Prim]]:
    """Group primitives into figures by bbox proximity (union-find, O(n^2)).

    Pages carry at most a few hundred drawings once table rules are excluded, so
    the quadratic pass is cheaper than building an index.
    """
    n = len(prims)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    for i in range(n):
        a = prims[i]
        for j in range(i + 1, n):
            b = prims[j]
            if (a.x0 - gap <= b.x1 and b.x0 - gap <= a.x1
                    and a.y0 - gap <= b.y1 and b.y0 - gap <= a.y1):
                union(i, j)

    groups: dict[int, list[_Prim]] = {}
    for i, p in enumerate(prims):
        groups.setdefault(find(i), []).append(p)
    return list(groups.values())


def _short_labels_near(page: pymupdf.Page, box: tuple[float, float, float, float],
                       pad: float = 24.0) -> int:
    """Count short text runs in and around a region — axis ticks and legends.

    A chart is surrounded by them; a decorative rule or a logo is not.
    """
    x0, y0, x1, y1 = box
    region = pymupdf.Rect(x0 - pad, y0 - pad, x1 + pad, y1 + pad)
    try:
        words = page.get_text("words", clip=region)
    except Exception:  # noqa: BLE001
        return 0
    return sum(1 for w in words if len(w[4]) <= 8)


def _looks_like_grid(group: Sequence[_Prim], tol: float = 2.0) -> bool:
    """True when the shapes tile a row/column grid — i.e. they are table cells.

    This is the test that stops a ruled or shaded table being reported as a bar
    chart, and it was added because a 15-row insurance table was confidently
    classified as two bar charts: its cell borders are filled rectangles in two
    colours with short numeric labels beside them, which satisfies every naive
    chart test.

    It used to compare a PRIMITIVE count against a rows x columns CELL count.
    That ratio is (h+v)/(h*v) for a line-ruled table and collapses as the table
    grows — 0.53 at 5x3, 0.15 at 25x9 — so it let through exactly the large
    tables this corpus targets, and it missed the rect-drawn case it was written
    for by 0.015. A grid is a PRODUCT structure, so test the product directly:
    the same x-extent repeating down the page AND the same y-extent repeating
    across it. A bar chart has neither — every bar is its own width and height.
    """
    shapes = [p for p in group if not p.is_hairline]
    if len(shapes) < 8:
        return False
    cols: dict[tuple[int, int], int] = {}
    rows: dict[tuple[int, int], int] = {}
    for p in shapes:
        key_x = (round(p.x0 / tol), round(p.x1 / tol))
        key_y = (round(p.y0 / tol), round(p.y1 / tol))
        cols[key_x] = cols.get(key_x, 0) + 1
        rows[key_y] = rows.get(key_y, 0) + 1
    repeated_cols = sum(1 for n in cols.values() if n >= 3)
    repeated_rows = sum(1 for n in rows.values() if n >= 3)
    return repeated_cols >= 3 and repeated_rows >= 3


def _looks_like_table_fill(group: Sequence[_Prim], tol: float = 2.0) -> bool:
    """Many congruent filled rectangles — row banding, not a chart.

    A shaded financial statement (EDINET's are typical) draws one filled
    rectangle per cell: 134 of them, two colours, short numeric labels all
    around. That satisfies every naive chart test.

    Two corrections, both measured. Hairline fills are excluded: a filled path
    can be a rule, and a *vertical* rule is tall, so the old height-only test
    fired on the hairline scaffolding of a real govinfo line chart and threw the
    chart away. And uniform height alone is not enough evidence — a horizontal
    bar chart's bars are all exactly one bar-height tall, and only their widths
    carry the data. Banding is congruent in BOTH dimensions.
    """
    fills = [p for p in group if p.fill is not None and not p.is_hairline]
    if len(fills) < 8:
        return False
    for extents in ([p.height for p in fills], [p.width for p in fills]):
        mean = sum(extents) / len(extents)
        if mean <= 0:
            return False
        variance = sum((e - mean) ** 2 for e in extents) / len(extents)
        if (variance ** 0.5) / mean >= 0.25:
            return False
    return True


def _data_marks(group: Sequence[_Prim],
                region: tuple[float, float, float, float]) -> list[_Prim]:
    """The filled shapes in a region that could actually encode a datum.

    Excluded, because each of them produced a phantom chart on a real page:
    hairline fills (column rules and accounting underlines — an IBM 10-K pension
    note is 29 of them and nothing else), white fills (invisible knockout boxes
    behind borderless table cells), and fills spanning the whole region (page and
    row background washes).
    """
    x0, y0, x1, y1 = region
    span_w = (x1 - x0) * BAND_SPAN_FRAC
    span_h = (y1 - y0) * BAND_SPAN_FRAC
    return [p for p in group
            if p.fill is not None and not p.is_hairline and not _is_white(p.fill)
            and not (p.width >= span_w or p.height >= span_h)]


def _shares_a_hub(shapes: Sequence[_Prim], tol: float = 2.5) -> bool:
    """Do several curved shapes have a bbox corner at one common point? A pie.

    Wedges cannot be found by segment count — a wedge is one arc and two radii,
    fewer curves than a rounded callout box — so counting large curved paths
    alone made a chart of any slide bearing three rounded boxes. What a pie has
    and a scatter of pictograms has not is a HUB: the centre is a bbox CORNER of
    every wedge that does not cross an axis, so the corners pile up on one point
    and the wedges fan out from it.
    """
    corners = [(cx, cy) for p in shapes
               for cx, cy in ((p.x0, p.y0), (p.x1, p.y0), (p.x0, p.y1), (p.x1, p.y1))]
    for hx, hy in corners:
        at_hub = sum(1 for cx, cy in corners
                     if abs(cx - hx) <= tol and abs(cy - hy) <= tol)
        covering = sum(1 for q in shapes
                       if q.x0 - tol <= hx <= q.x1 + tol and q.y0 - tol <= hy <= q.y1 + tol)
        # Corners alone are not enough: the two faces of a 3-D bar share one.
        # A hub is also INSIDE the wedges that straddle it.
        if at_hub >= MIN_CURVED_PATHS and covering > MIN_CURVED_PATHS:
            return True
    return False


def _bar_baseline(marks: Sequence[_Prim], tol: float = 2.0) -> str | None:
    """'v' or 'h' when enough marks stand on one baseline at differing lengths.

    Vertical bars share a bottom edge and differ in height; horizontal bars share
    a left edge and differ in width. Only the bottom was checked before, so every
    horizontal bar chart in the corpus was accepted for the wrong reason and then
    labelled 'line' — a BIS stacked bar chart is the worked example.

    Equal lengths on a shared edge are a legend or a banded row, not bars.
    """
    for axis, edge, extent in (("v", "y1", "height"), ("h", "x0", "width")):
        by_edge: dict[int, list[_Prim]] = {}
        for p in marks:
            by_edge.setdefault(round(getattr(p, edge) / tol), []).append(p)
        for bars in by_edge.values():
            if len(bars) < MIN_BARS:
                continue
            lengths = [getattr(b, extent) for b in bars]
            spread = max(lengths) - min(lengths)
            if spread <= tol * 2 or spread < BAR_LENGTH_SPREAD * max(lengths):
                continue
            if len({round(v / tol) for v in lengths}) >= MIN_BAR_LENGTHS:
                return axis
    return None


def find_charts(page: pymupdf.Page) -> tuple[list[ChartFacts], list[tuple[float, ...]]]:
    """Classify vector-graphic regions that behave like charts.

    The discriminating facts, in order of how much work they save:
      - a chart occupies a bounded fraction of the page (a full-page vector wash
        is a background, a 0.5% one is a bullet glyph)
      - it does not tile a grid, and is not made only of rules (that is a table)
      - it shows POSITIVE evidence a table cannot fake: a sloping many-segment
        path, wedges radiating from a hub, several large curved paths, bars on a
        baseline, or several colours of data mark
      - it is surrounded by short text labels

    The positive-evidence requirement is the correction that matters. The tests
    used to be "two fill colours OR twelve line segments OR six curves", each of
    which a financial table satisfies by accident — two-tone row banding, twelve
    hairline column rules, a column of pictograms — and the `labels >= 3` backstop
    is no backstop at all when the labels being counted are the table's own
    numbers. Adjudicating twelve randomly sampled flagged pages by eye put the
    false-positive rate at 8/12: EDINET ruled statements, an IBM 10-K pension
    note reported as two bar charts, a BIS event calendar reported as a pie.

    Charts are found WITHOUT reference to detected tables, and the regions are
    returned so the caller can drop tables that are really a chart's axis
    labels. The dependency used to run the other way — tables won any overlap —
    and that was wrong in both directions at once: a chart's tick labels are
    short numeric cells in aligned columns, so the text strategy reports them as
    a table, and that phantom table then suppressed the real chart underneath it.
    """
    prims = _primitives(page)
    if len(prims) < CHART_MIN_PRIMITIVES:
        return [], []
    page_area = abs(page.rect.width * page.rect.height) or 1.0
    out: list[ChartFacts] = []
    regions: list[tuple[float, ...]] = []

    for group in _cluster(prims):
        if len(group) < CHART_MIN_PRIMITIVES:
            continue
        x0 = min(p.x0 for p in group)
        y0 = min(p.y0 for p in group)
        x1 = max(p.x1 for p in group)
        y1 = max(p.y1 for p in group)
        area_frac = abs((x1 - x0) * (y1 - y0)) / page_area
        if not (CHART_MIN_AREA_FRAC <= area_frac <= CHART_MAX_AREA_FRAC):
            continue
        # Nothing but rules: a table's ruling, whatever its stroke count.
        if all(p.is_hairline for p in group):
            continue
        if _looks_like_grid(group) or _looks_like_table_fill(group):
            continue

        marks = _data_marks(group, (x0, y0, x1, y1))
        colors = {p.fill for p in marks}
        n_curves = sum(p.n_curves for p in group)
        n_lines = sum(p.n_lines for p in group)
        labels = _short_labels_near(page, (x0, y0, x1, y1))

        # All three conditions are per PATH, and all three are needed. The most
        # common chart style in government statistics is a monochrome dual-axis
        # line chart — one colour, one path, 156 segments — so colour cannot be
        # required; a table's rules are one axis-aligned segment per path, so
        # totalling segments over the region cannot be allowed; and a 2pt vector
        # seal is 18 sloping segments, so size cannot be ignored.
        polyline = any(p.n_lines >= LINE_PATH_MIN_SEGMENTS
                       and p.n_diagonals >= MIN_DIAGONAL_SEGMENTS
                       and max(p.width, p.height) >= CHART_PATH_MIN_PT for p in group)
        big_curved = [p for p in group if p.n_curves >= 1
                      and min(p.width, p.height) >= CURVE_PATH_MIN_PT]
        curvy = sum(1 for p in big_curved
                    if p.n_curves >= CURVE_PATH_MIN_SEGMENTS) >= MIN_CURVED_PATHS
        pie = len(big_curved) >= MIN_CURVED_PATHS and _shares_a_hub(big_curved)
        bars = _bar_baseline(marks)
        colourful = (len(marks) >= MIN_MARKS and len(colors) >= MIN_MARK_COLORS
                     and len(marks) >= MIN_MARK_SHARE * len(group))
        if not (polyline or curvy or pie or bars or colourful):
            continue
        if labels < 3:
            continue

        if pie:
            kind = "pie"
        elif bars:
            kind = "bar"
        elif polyline or n_lines > n_curves:
            kind = "line"
        else:
            kind = "mixed"

        out.append(ChartFacts(kind=kind, n_primitives=len(group),
                              n_distinct_colors=len(colors),
                              area_frac=round(area_frac, 4), n_labels=labels))
        regions.append((x0, y0, x1, y1))
    return out, regions


def _contained(inner: Sequence[float], outer: Sequence[float]) -> float:
    """Fraction of `inner`'s area that lies inside `outer`."""
    ix0, iy0 = max(inner[0], outer[0]), max(inner[1], outer[1])
    ix1, iy1 = min(inner[2], outer[2]), min(inner[3], outer[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    area = abs((inner[2] - inner[0]) * (inner[3] - inner[1]))
    return ((ix1 - ix0) * (iy1 - iy0)) / area if area else 0.0


def _image_area_fracs(page: pymupdf.Page) -> list[float]:
    """Each embedded image's placed area as a fraction of the page."""
    try:
        infos = page.get_image_info()
    except Exception:  # noqa: BLE001
        return []
    page_area = abs(page.rect.width * page.rect.height) or 1.0
    fracs = []
    for info in infos:
        bbox = info.get("bbox")
        if not bbox:
            continue
        fracs.append(abs((bbox[2] - bbox[0]) * (bbox[3] - bbox[1])) / page_area)
    return fracs


def _possible_raster_figure(page: pymupdf.Page) -> bool:
    """An embedded image that is neither tiny nor full-page.

    On a scanned page the single full-bleed image is the scan itself. On a
    born-digital report a mid-sized image is very often a chart exported as a
    bitmap — invisible to every vector test above.
    """
    return any(RASTER_FIGURE_MIN_FRAC <= f <= SCAN_IMAGE_COVER_FRAC
               for f in _image_area_fracs(page))


# --------------------------------------------------------------------------
# page / document entry points
# --------------------------------------------------------------------------
def _is_rasterised(page: pymupdf.Page) -> bool:
    """The page is a picture of a document, so its charts are pixels.

    Its tables are still measurable when a scanner left an OCR text layer, but
    its charts are not measurable by any means available here.

    The test used to be "zero drawings and an image", and a single stray vector
    mark — a scanner frame, a redaction box, a stamp — flipped it. Such pages
    were then counted as measured and voted 'not dense', which is the opposite
    of what the opaque bucket exists for. What matters is whether a chart COULD
    have been seen: under the detector's own primitive floor, on a page an image
    already covers, it could not have been.
    """
    try:
        drawings = page.get_drawings()
        if len(drawings) >= CHART_MIN_PRIMITIVES:
            return False
        if max(_image_area_fracs(page), default=0.0) > SCAN_IMAGE_COVER_FRAC:
            return True
        # A scan tiled into many small images has no one covering image, so keep
        # the original zero-vector-ink signature as well.
        return not drawings and bool(page.get_images(full=True))
    except Exception:  # noqa: BLE001
        return False


def measure_page(page: pymupdf.Page, page_no: int) -> PageDensity:
    density = PageDensity(page_no=page_no)
    try:
        tables, boxes = _detect_tables(page)
        density.charts, chart_regions = find_charts(page)
        # Drop tables that are really a chart's axis labels. Containment, not
        # IoU: a chart region is larger than the tick-label block inside it, so
        # IoU stays low even when the "table" is entirely the chart's own text.
        density.tables = [
            t for t, box in zip(tables, boxes)
            if not any(_contained(box, region) > 0.75 for region in chart_regions)
        ]
        density.possible_raster_figure = _possible_raster_figure(page)
        density.chart_measurable = not _is_rasterised(page)
        try:
            density.table_measurable = len((page.get_text() or "").strip()) >= TEXT_LAYER_MIN_CHARS
        except Exception:  # noqa: BLE001
            density.table_measurable = False
    except Exception as exc:  # noqa: BLE001 — one bad page must not lose the doc
        density.error = f"{type(exc).__name__}: {exc}"
        # A page that raised was not measured, and the flags default to True.
        # Left alone they put it in document_score's `judged` denominator with
        # zero tables and zero charts — an unreadable page voting 'not dense'.
        density.table_measurable = density.chart_measurable = False
    return density


def measure_pdf(path: str, *, pages: Iterable[int] | None = None,
                max_pages: int | None = None) -> list[PageDensity]:
    """Measure a document. `pages` is 1-based; None means every page (up to cap).

    Table finding costs real time — tens of milliseconds a page — so callers
    scoring a corpus for acquisition decisions should sample rather than sweep.
    """
    out: list[PageDensity] = []
    with pymupdf.open(path) as doc:
        if pages is None:
            wanted = range(1, doc.page_count + 1)
            if max_pages:
                wanted = range(1, min(doc.page_count, max_pages) + 1)
        else:
            wanted = [p for p in pages if 1 <= p <= doc.page_count]
        for page_no in wanted:
            try:
                out.append(measure_page(doc[page_no - 1], page_no))
            except Exception as exc:  # noqa: BLE001 — unmeasurable, not measured-empty
                out.append(PageDensity(page_no=page_no,
                                       error=f"{type(exc).__name__}: {exc}",
                                       table_measurable=False,
                                       chart_measurable=False))
    return out


def sample_pages(n_pages: int, k: int = 8) -> list[int]:
    """Evenly spaced 1-based page numbers, skipping the cover.

    Front matter is unrepresentative — a report's cover, contents and letter from
    the chair carry no tables — so the sample starts after it where possible.

    Spread over the CLOSED interval, and de-duplicated. Dividing the half-open
    span dropped the last 1/k of every document — in a 100-page annual report
    that is pages 88 to 100, which is exactly where the financial statements
    sit — and at n_pages=9 it returned page 2 twice, which measure_pdf then
    measured twice and document_score counted twice while the catalogue's
    ON CONFLICT DO NOTHING stored it once.
    """
    if n_pages <= 0:
        return []
    if n_pages <= k:
        return list(range(1, n_pages + 1))
    start = 2 if n_pages > 4 else 1
    if k <= 1:
        return [start]
    span = n_pages - start
    return sorted({start + round(i * span / (k - 1)) for i in range(k)})


def document_score(densities: Sequence[PageDensity]) -> dict[str, Any]:
    """Roll page measurements up to the decision a collector makes: keep or not.

    `dense_frac` is computed over pages that could actually be measured, not
    over every page sampled. An un-OCR'd scan is not evidence of absence, and
    dividing by it would systematically reject scanned sources — which are the
    ones carrying the typewritten, rotated and historical tables this corpus is
    being built to cover. `opaque_pages` carries that uncertainty forward
    instead of burying it.
    """
    n = len(densities)
    if not n:
        return {"pages_measured": 0, "pages_opaque": 0, "table_pages": 0,
                "chart_pages": 0, "chart_blind_pages": 0, "dense_pages": 0,
                "dense_frac": 0.0, "raster_figure_pages": 0, "max_table_cells": 0,
                "borderless_pages": 0, "is_dense_doc": False}
    table_pages = sum(1 for d in densities if d.is_table_page)
    chart_pages = sum(1 for d in densities if d.is_chart_page)
    # Only judged pages can vote. An opaque page contributes to neither side of
    # the ratio, so a page that raised half way through measurement cannot make
    # dense_frac exceed 1 with the facts it had already collected.
    dense = sum(1 for d in densities if d.is_dense and d.measurable)
    opaque = sum(1 for d in densities if not d.measurable)
    judged = n - opaque
    return {
        "pages_measured": n,
        # Sampled but un-judgeable: image-only pages with no text layer.
        "pages_opaque": opaque,
        "table_pages": table_pages,
        "chart_pages": chart_pages,
        # Pages whose charts are pixels. `chart_pages` is a floor, not a count,
        # whenever this is non-zero — scanned corpora need a model to settle it.
        "chart_blind_pages": sum(1 for d in densities if not d.chart_measurable),
        "dense_pages": dense,
        "dense_frac": round(dense / judged, 3) if judged else 0.0,
        "raster_figure_pages": sum(1 for d in densities if d.possible_raster_figure),
        "max_table_cells": max((max((t.n_rows * t.n_cols for t in d.tables), default=0)
                                for d in densities), default=0),
        "borderless_pages": sum(1 for d in densities
                                if any(t.borderless and t.complex for t in d.tables)),
        # One judged page in three is what makes a document worth its bytes. A
        # wholly opaque document is undecided, not dense.
        "is_dense_doc": bool(judged) and (dense / judged) >= 0.33,
    }
