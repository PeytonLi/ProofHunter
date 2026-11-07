"""Dependency-free SVG charts (the research lock may not include a plotting library)."""

from __future__ import annotations

from collections.abc import Sequence
from html import escape

PALETTE = (
    "#1f77b4",
    "#d62728",
    "#2ca02c",
    "#9467bd",
    "#ff7f0e",
    "#8c564b",
    "#e377c2",
    "#17becf",
    "#7f7f7f",
    "#bcbd22",
)
W, H, L, R, T, B = 640, 400, 64, 170, 40, 56

Series = tuple[str, Sequence[tuple[float, float | None]]]


def _frame(title: str, xlabel: str, ylabel: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" font-family="sans-serif" font-size="11">',
        f'<rect width="{W}" height="{H}" fill="white"/>',
        f'<text x="{W / 2}" y="20" text-anchor="middle" font-size="14">{escape(title)}</text>',
        f'<text x="{(L + W - R) / 2}" y="{H - 12}" text-anchor="middle">{escape(xlabel)}</text>',
        f'<text x="16" y="{(T + H - B) / 2}" text-anchor="middle" '
        f'transform="rotate(-90 16 {(T + H - B) / 2})">{escape(ylabel)}</text>',
        f'<line x1="{L}" y1="{H - B}" x2="{W - R}" y2="{H - B}" stroke="black"/>',
        f'<line x1="{L}" y1="{T}" x2="{L}" y2="{H - B}" stroke="black"/>',
    ]


def _ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    if hi <= lo:
        return [lo]
    return [lo + (hi - lo) * i / n for i in range(n + 1)]


def _num(v: float) -> str:
    return f"{v:.3g}" if abs(v) < 1000 else f"{v:,.0f}"


def line_chart(
    title: str,
    xlabel: str,
    ylabel: str,
    series: Sequence[Series],
    *,
    y_max: float | None = 1.0,
    note: str = "",
) -> str:
    points = [(x, y) for _, pts in series for x, y in pts if y is not None]
    xs = [x for x, _ in points] or [0.0, 1.0]
    x_lo, x_hi = min(xs), max(xs)
    y_hi = y_max if y_max is not None else max([y for _, y in points] or [1.0]) or 1.0
    x_hi = x_hi if x_hi > x_lo else x_lo + 1

    def sx(x: float) -> float:
        return L + (x - x_lo) / (x_hi - x_lo) * (W - L - R)

    def sy(y: float) -> float:
        return H - B - y / y_hi * (H - T - B)

    out = _frame(title, xlabel, ylabel)
    for v in _ticks(0, y_hi):
        out.append(f'<text x="{L - 6}" y="{sy(v) + 4:.1f}" text-anchor="end">{_num(v)}</text>')
        out.append(f'<line x1="{L}" y1="{sy(v):.1f}" x2="{W - R}" y2="{sy(v):.1f}" stroke="#eee"/>')
    for v in _ticks(x_lo, x_hi):
        out.append(f'<text x="{sx(v):.1f}" y="{H - B + 16}" text-anchor="middle">{_num(v)}</text>')
    for i, (name, pts) in enumerate(series):
        color = PALETTE[i % len(PALETTE)]
        coords = [(sx(x), sy(y)) for x, y in pts if y is not None]
        if coords:
            path = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
            out.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{path}"/>')
            out.extend(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" fill="{color}"/>' for x, y in coords
            )
        ly = T + 14 + 16 * i
        out.append(f'<rect x="{W - R + 10}" y="{ly - 9}" width="10" height="10" fill="{color}"/>')
        out.append(f'<text x="{W - R + 24}" y="{ly}">{escape(name)}</text>')
    if note:
        out.append(f'<text x="{L}" y="{T - 6}" fill="#555">{escape(note)}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def bar_chart(
    title: str,
    ylabel: str,
    categories: Sequence[str],
    series: Sequence[tuple[str, Sequence[float]]],
    *,
    stacked: bool = False,
    note: str = "",
) -> str:
    n = max(1, len(categories))
    totals = (
        [sum(s[1][i] for s in series) for i in range(n)]
        if stacked and series
        else [v for _, vals in series for v in vals]
    )
    y_hi = max(totals or [1.0]) or 1.0
    slot = (W - L - R) / n
    width = slot * 0.7 / (1 if stacked else max(1, len(series)))

    def sy(y: float) -> float:
        return H - B - y / y_hi * (H - T - B)

    out = _frame(title, "", ylabel)
    for v in _ticks(0, y_hi):
        out.append(f'<text x="{L - 6}" y="{sy(v) + 4:.1f}" text-anchor="end">{_num(v)}</text>')
    for c, name in enumerate(categories):
        x0 = L + slot * c + slot * 0.15
        out.append(
            f'<text x="{L + slot * (c + 0.5):.1f}" y="{H - B + 16}" '
            f'text-anchor="middle">{escape(name)}</text>'
        )
        base = 0.0
        for i, (_, vals) in enumerate(series):
            value = vals[c]
            color = PALETTE[i % len(PALETTE)]
            if stacked:
                x, top, bottom = x0, sy(base + value), sy(base)
                base += value
            else:
                x, top, bottom = x0 + width * i, sy(value), sy(0)
            out.append(
                f'<rect x="{x:.1f}" y="{top:.1f}" width="{width:.1f}" '
                f'height="{max(0.0, bottom - top):.1f}" fill="{color}"/>'
            )
    for i, (name, _) in enumerate(series):
        ly = T + 14 + 16 * i
        out.append(
            f'<rect x="{W - R + 10}" y="{ly - 9}" width="10" height="10" '
            f'fill="{PALETTE[i % len(PALETTE)]}"/>'
        )
        out.append(f'<text x="{W - R + 24}" y="{ly}">{escape(name)}</text>')
    if note:
        out.append(f'<text x="{L}" y="{T - 6}" fill="#555">{escape(note)}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"
