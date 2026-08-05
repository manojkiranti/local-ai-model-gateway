"""Pure, dependency-free SVG chart rendering for the create_chart tool.

The gateway draws the chart (the model only supplies data), so the output is
deterministic and contains **no <script> and no external references** — safe to
serve and to render inside an <img>. Bar, line, and pie only.

Colours come from the dataviz skill's validated categorical palette (light
surface); the contrast WARN on some slots is discharged by always drawing a
legend (≥2 series) plus axis / value / slice labels, so series identity is never
carried by colour alone.
"""

from __future__ import annotations

import math

# --- dataviz categorical palette (light surface), fixed order, never cycled ---
PALETTE = [
    "#2a78d6",  # blue
    "#eb6834",  # orange
    "#1baf7a",  # aqua
    "#eda100",  # yellow
    "#e87ba4",  # magenta
    "#008300",  # green
    "#4a3aa7",  # violet
    "#e34948",  # red
]
SURFACE = "#fcfcfb"
INK = "#0b0b0b"       # text-primary
INK_MUTED = "#52514e"  # text-secondary
GRID = "#e8e8e5"       # recessive gridlines / axes

# Canvas + plot geometry.
W, H = 720, 460
M_LEFT, M_RIGHT, M_BOTTOM = 64, 24, 64
FONT = "system-ui, -apple-system, Segoe UI, Roboto, sans-serif"


def _esc(text: str) -> str:
    """XML-escape text going into element bodies/attributes."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _fmt(v: float) -> str:
    """Format a number: drop the trailing .0 for whole values."""
    if v == int(v):
        return str(int(v))
    return f"{v:.2f}".rstrip("0").rstrip(".")


# ---- axis ticks (nice round numbers) ----
def _nice(x: float, round_down: bool) -> float:
    if x <= 0:
        return 1.0
    exp = math.floor(math.log10(x))
    f = x / (10 ** exp)
    if round_down:
        nf = 1 if f < 1.5 else 2 if f < 3 else 5 if f < 7 else 10
    else:
        nf = 1 if f <= 1 else 2 if f <= 2 else 5 if f <= 5 else 10
    return nf * (10 ** exp)


def _ticks(dmin: float, dmax: float, count: int = 5) -> tuple[float, float, list[float]]:
    if dmin == dmax:
        dmax = dmin + 1
    step = _nice((dmax - dmin) / max(count - 1, 1), round_down=True)
    nice_min = math.floor(dmin / step) * step
    nice_max = math.ceil(dmax / step) * step
    ticks, v = [], nice_min
    while v <= nice_max + step * 1e-6:
        ticks.append(round(v, 10))
        v += step
    return nice_min, nice_max, ticks


def _header(title: str, series: list[dict], legend: bool) -> tuple[list[str], int]:
    """Draw title + (optional) legend row; return (svg_parts, plot_top_y)."""
    parts: list[str] = []
    top = 20
    if title:
        parts.append(
            f'<text x="{W/2:.0f}" y="26" text-anchor="middle" '
            f'font-size="17" font-weight="600" fill="{INK}">{_esc(title)}</text>'
        )
        top = 40
    if legend:
        # Centered swatch+name row under the title.
        gap = 18
        items = [(s["name"], PALETTE[i % len(PALETTE)]) for i, s in enumerate(series)]
        widths = [len(name) * 7 + gap + 14 for name, _ in items]
        total = sum(widths)
        x = (W - total) / 2
        y = top + 14
        for (name, colour), wdt in zip(items, widths):
            parts.append(
                f'<rect x="{x:.0f}" y="{y-9:.0f}" width="10" height="10" rx="2" fill="{colour}"/>'
            )
            parts.append(
                f'<text x="{x+16:.0f}" y="{y:.0f}" font-size="12" fill="{INK_MUTED}">{_esc(name)}</text>'
            )
            x += wdt
        top = y + 14
    return parts, top


def _axes(parts: list[str], top: int, ticks: list[float], vmin: float, vmax: float) -> tuple[float, float]:
    """Draw y gridlines/labels + baseline. Returns (plot_bottom_y, plot_top_y)."""
    py1 = H - M_BOTTOM
    plot_h = py1 - top

    def y_of(v: float) -> float:
        return py1 - (v - vmin) / (vmax - vmin) * plot_h

    for t in ticks:
        y = y_of(t)
        parts.append(
            f'<line x1="{M_LEFT}" y1="{y:.1f}" x2="{W-M_RIGHT}" y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{M_LEFT-8}" y="{y+4:.1f}" text-anchor="end" font-size="11" fill="{INK_MUTED}">{_fmt(t)}</text>'
        )
    return py1, top


def _x_labels(parts: list[str], labels: list[str], py1: float) -> None:
    n = len(labels)
    plot_w = (W - M_RIGHT) - M_LEFT
    slot = plot_w / n
    for i, lab in enumerate(labels):
        cx = M_LEFT + slot * (i + 0.5)
        parts.append(
            f'<text x="{cx:.1f}" y="{py1+18:.0f}" text-anchor="middle" font-size="11" fill="{INK_MUTED}">{_esc(lab)}</text>'
        )


def _open_svg(title: str, desc: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" '
        f'height="{H}" role="img" font-family="{FONT}">',
        f"<title>{_esc(title or 'Chart')}</title><desc>{_esc(desc)}</desc>",
        f'<rect width="{W}" height="{H}" fill="{SURFACE}"/>',
    ]


# ---- bar ----
def _render_bar(title: str, labels: list[str], series: list[dict]) -> str:
    multi = len(series) > 1
    parts = _open_svg(title, f"Bar chart with {len(labels)} categories and {len(series)} series.")
    head, top = _header(title, series, legend=multi)
    parts += head

    all_vals = [v for s in series for v in s["data"]]
    vmin, vmax, ticks = _ticks(min(0, min(all_vals)), max(all_vals))
    py1, top = _axes(parts, top, ticks, vmin, vmax)
    plot_h = py1 - top
    plot_w = (W - M_RIGHT) - M_LEFT
    slot = plot_w / len(labels)
    y0 = py1 - (0 - vmin) / (vmax - vmin) * plot_h  # baseline (value 0)

    group_w = slot * 0.7
    m = len(series)
    bar_w = group_w / m
    for i in range(len(labels)):
        gx = M_LEFT + slot * i + (slot - group_w) / 2
        for j, s in enumerate(series):
            v = s["data"][i]
            y = py1 - (v - vmin) / (vmax - vmin) * plot_h
            bx = gx + j * bar_w
            bw = max(bar_w - 2, 1)  # 2px gap between adjacent bars
            top_y = min(y, y0)
            bh = abs(y - y0)
            parts.append(
                f'<rect x="{bx:.1f}" y="{top_y:.1f}" width="{bw:.1f}" height="{bh:.1f}" '
                f'rx="2" fill="{PALETTE[j % len(PALETTE)]}"/>'
            )
            if not multi:  # single series: label the value directly
                parts.append(
                    f'<text x="{bx+bw/2:.1f}" y="{top_y-4:.1f}" text-anchor="middle" '
                    f'font-size="10" fill="{INK_MUTED}">{_fmt(v)}</text>'
                )
    _x_labels(parts, labels, py1)
    parts.append("</svg>")
    return "".join(parts)


# ---- horizontal bar ----
def _render_hbar(title: str, labels: list[str], series: list[dict]) -> str:
    multi = len(series) > 1
    parts = _open_svg(title, f"Horizontal bar chart with {len(labels)} categories and {len(series)} series.")
    head, top = _header(title, series, legend=multi)
    parts += head

    # Wider left margin so category labels fit (bounded).
    left = min(180, max(70, max(len(lab) for lab in labels) * 7 + 16))
    px0, px1 = left, W - M_RIGHT
    py1 = H - M_BOTTOM
    plot_w = px1 - px0
    plot_h = py1 - top

    all_vals = [v for s in series for v in s["data"]]
    vmin, vmax, ticks = _ticks(min(0, min(all_vals)), max(all_vals))

    def x_of(v: float) -> float:
        return px0 + (v - vmin) / (vmax - vmin) * plot_w

    # Vertical gridlines + value labels along the bottom.
    for t in ticks:
        x = x_of(t)
        parts.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{py1}" stroke="{GRID}" stroke-width="1"/>')
        parts.append(
            f'<text x="{x:.1f}" y="{py1+18:.0f}" text-anchor="middle" font-size="11" fill="{INK_MUTED}">{_fmt(t)}</text>'
        )

    slot = plot_h / len(labels)
    group_h = slot * 0.7
    m = len(series)
    bar_h = group_h / m
    x0 = x_of(0)  # baseline (value 0)
    for i, lab in enumerate(labels):
        gy = top + slot * i + (slot - group_h) / 2
        # Category label, right-aligned in the left margin, vertically centered.
        parts.append(
            f'<text x="{px0-8:.0f}" y="{top+slot*i+slot/2+4:.1f}" text-anchor="end" '
            f'font-size="11" fill="{INK_MUTED}">{_esc(lab)}</text>'
        )
        for j, s in enumerate(series):
            v = s["data"][i]
            x = x_of(v)
            by = gy + j * bar_h
            bh = max(bar_h - 2, 1)  # 2px gap between adjacent bars
            bx = min(x, x0)
            bw = abs(x - x0)
            parts.append(
                f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bw:.1f}" height="{bh:.1f}" '
                f'rx="2" fill="{PALETTE[j % len(PALETTE)]}"/>'
            )
            if not multi:  # single series: value label at the bar end
                parts.append(
                    f'<text x="{x+4:.1f}" y="{by+bh/2+4:.1f}" font-size="10" '
                    f'fill="{INK_MUTED}">{_fmt(v)}</text>'
                )
    parts.append("</svg>")
    return "".join(parts)


# ---- line / area (shared core) ----
def _line_core(title: str, labels: list[str], series: list[dict], *, fill: bool) -> str:
    multi = len(series) > 1
    kind = "Area" if fill else "Line"
    parts = _open_svg(title, f"{kind} chart with {len(labels)} points and {len(series)} series.")
    head, top = _header(title, series, legend=multi)
    parts += head

    all_vals = [v for s in series for v in s["data"]]
    vmin, vmax, ticks = _ticks(min(0, min(all_vals)), max(all_vals))
    py1, top = _axes(parts, top, ticks, vmin, vmax)
    plot_h = py1 - top
    plot_w = (W - M_RIGHT) - M_LEFT
    slot = plot_w / len(labels)

    def x_of(i: int) -> float:
        return M_LEFT + slot * (i + 0.5)

    def y_of(v: float) -> float:
        return py1 - (v - vmin) / (vmax - vmin) * plot_h

    for j, s in enumerate(series):
        colour = PALETTE[j % len(PALETTE)]
        pts = [(x_of(i), y_of(v)) for i, v in enumerate(s["data"])]
        if fill:  # translucent area down to the baseline, drawn under the line
            area = f"M {pts[0][0]:.1f},{py1:.1f} " + " ".join(f"L {x:.1f},{y:.1f}" for x, y in pts)
            area += f" L {pts[-1][0]:.1f},{py1:.1f} Z"
            parts.append(f'<path d="{area}" fill="{colour}" fill-opacity="0.18" stroke="none"/>')
        line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        parts.append(
            f'<polyline points="{line}" fill="none" stroke="{colour}" '
            f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
        )
        for x, y in pts:  # ≥8px markers, 1.5px surface ring
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{colour}" '
                f'stroke="{SURFACE}" stroke-width="1.5"/>'
            )
    _x_labels(parts, labels, py1)
    parts.append("</svg>")
    return "".join(parts)


# ---- pie / donut (shared core) ----
def _pie_core(title: str, labels: list[str], series: list[dict], *, donut: bool) -> str:
    data = series[0]["data"]
    total = sum(data)
    kind = "Donut" if donut else "Pie"
    parts = _open_svg(title, f"{kind} chart with {len(labels)} slices.")
    head, top = _header(title, series, legend=False)
    parts += head

    cx, cy = 200, (top + (H - 20)) / 2
    r = min(150, (H - 20 - top) / 2)
    ir = r * 0.58 if donut else 0.0
    angle = -math.pi / 2  # start at 12 o'clock
    for i, v in enumerate(data):
        frac = v / total
        end = angle + frac * 2 * math.pi
        large = 1 if frac > 0.5 else 0
        xo0, yo0 = cx + r * math.cos(angle), cy + r * math.sin(angle)
        xo1, yo1 = cx + r * math.cos(end), cy + r * math.sin(end)
        colour = PALETTE[i % len(PALETTE)]
        if donut:  # ring segment: outer arc, in, inner arc back, close
            xi0, yi0 = cx + ir * math.cos(angle), cy + ir * math.sin(angle)
            xi1, yi1 = cx + ir * math.cos(end), cy + ir * math.sin(end)
            d = (
                f"M {xo0:.2f} {yo0:.2f} A {r} {r} 0 {large} 1 {xo1:.2f} {yo1:.2f} "
                f"L {xi1:.2f} {yi1:.2f} A {ir:.1f} {ir:.1f} 0 {large} 0 {xi0:.2f} {yi0:.2f} Z"
            )
        else:  # full wedge from the center
            d = f"M {cx} {cy} L {xo0:.2f} {yo0:.2f} A {r} {r} 0 {large} 1 {xo1:.2f} {yo1:.2f} Z"
        # 2px surface stroke gives the between-slice gap.
        parts.append(f'<path d="{d}" fill="{colour}" stroke="{SURFACE}" stroke-width="2"/>')
        if frac > 0.04:  # % label on larger slices
            mid = (angle + end) / 2
            rmid = (r + ir) / 2 if donut else r * 0.6
            lx, ly = cx + rmid * math.cos(mid), cy + rmid * math.sin(mid)
            parts.append(
                f'<text x="{lx:.1f}" y="{ly+4:.1f}" text-anchor="middle" font-size="11" '
                f'font-weight="600" fill="#ffffff">{frac*100:.0f}%</text>'
            )
        angle = end

    if donut:  # total in the hole
        parts.append(
            f'<text x="{cx}" y="{cy-2:.0f}" text-anchor="middle" font-size="20" '
            f'font-weight="600" fill="{INK}">{_fmt(total)}</text>'
        )
        parts.append(
            f'<text x="{cx}" y="{cy+16:.0f}" text-anchor="middle" font-size="11" fill="{INK_MUTED}">Total</text>'
        )

    # Legend on the right: swatch + label + value.
    lx = 400
    ly = top + 20
    for i, (lab, v) in enumerate(zip(labels, data)):
        colour = PALETTE[i % len(PALETTE)]
        parts.append(f'<rect x="{lx}" y="{ly-10:.0f}" width="11" height="11" rx="2" fill="{colour}"/>')
        parts.append(
            f'<text x="{lx+18}" y="{ly:.0f}" font-size="12" fill="{INK}">'
            f'{_esc(lab)} <tspan fill="{INK_MUTED}">({_fmt(v)})</tspan></text>'
        )
        ly += 24
    parts.append("</svg>")
    return "".join(parts)


def render_chart(chart_type: str, title: str, labels: list[str], series: list[dict]) -> str:
    """Render a validated chart spec to a self-contained SVG string."""
    if chart_type == "bar":
        return _render_bar(title, labels, series)
    if chart_type == "hbar":
        return _render_hbar(title, labels, series)
    if chart_type == "line":
        return _line_core(title, labels, series, fill=False)
    if chart_type == "area":
        return _line_core(title, labels, series, fill=True)
    if chart_type == "pie":
        return _pie_core(title, labels, series, donut=False)
    if chart_type == "donut":
        return _pie_core(title, labels, series, donut=True)
    raise ValueError(f"unsupported chart_type: {chart_type}")
