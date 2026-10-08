"""Server-side chart geometry. Templates draw the marks as CSS bars; no JS chart library.

Follows the dataviz rules used across the dashboard: one series = one hue, a clean
0-based scale with at most four gridlines, selective direct labels (peak and today),
and every value also reachable without hovering (direct labels, tooltips on focus, table view).
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

_STEPS = (1, 2, 5)


def nice_scale(peak: int, max_ticks: int = 4) -> tuple[int, list[int]]:
    """Smallest 1/2/5x10^k step giving <= max_ticks intervals above 0. Returns (top, ticks)."""
    if peak <= 0:
        return 0, [0]
    magnitude = 1
    while True:
        for base in _STEPS:
            step = base * magnitude
            intervals = math.ceil(peak / step)
            if intervals <= max_ticks:
                top = step * intervals
                return top, list(range(0, top + 1, step))
        magnitude *= 10


def day_columns(points: list[dict[str, Any]]) -> dict[str, Any]:
    """Column chart for repo.company_stats()["signals_by_day"] ([{date, count}], oldest first)."""
    counts = [int(p.get("count") or 0) for p in points]
    peak = max(counts, default=0)
    top, ticks = nice_scale(peak)
    last = len(points) - 1
    # Label the latest peak and today; everything else lives in the tooltip and table view.
    peak_idx = max(range(len(counts)), key=lambda i: (counts[i], i)) if counts else -1
    cols = []
    prev_month = None
    for i, point in enumerate(points):
        day = date.fromisoformat(str(point["date"]))
        n = counts[i]
        xlabel = f"{day.day} {day:%b}" if day.month != prev_month else str(day.day)
        prev_month = day.month
        when = "Today" if i == last else f"{day:%a} {day.day} {day:%b}"
        cols.append({
            "date": day,
            "count": n,
            "pct": round(100 * n / top, 2) if top else 0,
            "xlabel": xlabel,
            "label_value": n > 0 and i in (peak_idx, last),
            "is_today": i == last,
            "tip": f"{when}: {n} signal{'' if n == 1 else 's'}",
        })
    return {
        "cols": cols,
        "ticks": [{"value": t, "pct": round(100 * t / top, 2) if top else 0} for t in ticks],
        "total": sum(counts),
        "peak": peak,
        "empty": peak == 0,
    }


def type_bars(rows: list[dict[str, Any]], limit: int = 8) -> dict[str, Any]:
    """Horizontal bars for repo.company_stats()["signals_by_type"]; the tail folds into 'Other'."""
    rows = sorted(rows, key=lambda r: -int(r.get("count") or 0))
    head = rows[:limit]
    tail = rows[limit:]
    bars = [{"label": r["label"], "type": r["type"], "count": int(r["count"])} for r in head]
    if tail:
        bars.append({"label": f"Other ({len(tail)} types)", "type": "", "count": sum(int(r["count"]) for r in tail)})
    peak = max((b["count"] for b in bars), default=0)
    for bar in bars:
        bar["frac"] = round(bar["count"] / peak, 4) if peak else 0
    return {"bars": bars, "total": sum(b["count"] for b in bars), "empty": peak == 0}
