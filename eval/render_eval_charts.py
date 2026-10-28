"""
Render the accuracy charts for TRAIN_TICKET_REPORT.md as SVG (no plotting libraries).

The numbers are the measured results recorded in the report (blind first runs and the
final run of eval/run_system_eval.py); update them here when the evaluation is re-run.

    python kitsune/eval/render_eval_charts.py --out kitsune/docs/images/train-ticket
"""
from __future__ import annotations

import argparse
from pathlib import Path

from render_system_images import AXIS, FONT, GRID, INK, INK2, MUTED, SLOTS, SURFACE, escape, text

TRAIN_TICKET = {   # metric: (blind first run, after fixes), percent
    "Link precision": (79.8, 100.0),
    "Link recall": (62.1, 100.0),
    "Service edges found": (67.9, 100.0),
    "Impact questions": (0.0, 100.0),
}
SYSTEMS = {        # system: (first-run link recall, final link recall), percent
    "Sock Shop": (63.0, 100.0),
    "Online Boutique": (100.0, 100.0),
    "PetClinic MS": (0.0, 100.0),
    "Robot Shop": (17.0, 100.0),
    "Train Ticket": (62.1, 100.0),
}


def bar(x, y_base, w, h, color):
    """Bar with a 4px rounded data end, square at the baseline."""
    if h <= 0:
        return f'<line x1="{x:.1f}" y1="{y_base:.1f}" x2="{x + w:.1f}" y2="{y_base:.1f}" stroke="{color}" stroke-width="2"/>'
    r = min(4, h, w / 2)
    top = y_base - h
    return (f'<path d="M{x:.1f},{y_base:.1f} V{top + r:.1f} Q{x:.1f},{top:.1f} {x + r:.1f},{top:.1f} '
            f'H{x + w - r:.1f} Q{x + w:.1f},{top:.1f} {x + w:.1f},{top + r:.1f} V{y_base:.1f} Z" fill="{color}"/>')


def grouped(data: dict, series: tuple[str, str], title: str, subtitle: str, out: Path):
    groups = list(data)
    W, H = 900, 420
    left, right, top, bottom = 64, 24, 112, 64
    pw, ph = W - left - right, H - top - bottom
    gw = pw / len(groups)
    bw = min(56, gw * 0.3)
    base = top + ph
    body = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" '
            f'aria-label="{escape(title)}"><title>{escape(title)}</title>',
            f'<rect width="{W}" height="{H}" fill="{SURFACE}"/>',
            text(left - 40, 30, title, 18, INK, weight="600"), text(left - 40, 52, subtitle, 12.5, INK2)]
    # legend (two series → legend always present)
    lx = left - 40
    for i, s in enumerate(series):
        body += [f'<rect x="{lx}" y="{70}" width="12" height="12" rx="3" fill="{SLOTS[i]}"/>',
                 text(lx + 18, 81, s, 12.5, INK2)]
        lx += 30 + 7.4 * len(s)
    for v in (0, 25, 50, 75, 100):
        y = base - ph * v / 100
        body.append(f'<line x1="{left}" y1="{y:.1f}" x2="{W - right}" y2="{y:.1f}" '
                    f'stroke="{AXIS if v == 0 else GRID}" stroke-width="1"/>')
        body.append(text(left - 8, y + 4, f"{v}%", 11, MUTED, "end"))
    for gi, g in enumerate(groups):
        cx = left + gw * gi + gw / 2
        for si, v in enumerate(data[g]):
            x = cx - bw - 1 + si * (bw + 2)          # 2px surface gap between the pair
            h = ph * v / 100
            body.append(bar(x, base, bw, h, SLOTS[si]))
            body.append(text(x + bw / 2, base - h - 7, f"{v:g}%", 11.5, INK, "middle"))
        body.append(text(cx, base + 22, g, 12.5, INK2, "middle"))
    body.append("</svg>")
    out.write_text("\n".join(body), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    out = Path(ap.parse_args().out)
    out.mkdir(parents=True, exist_ok=True)
    grouped(TRAIN_TICKET, ("Blind first run", "After fixes"), "Train Ticket: accuracy before and after",
            "47 services · answer key written independently before Kitsune ran · 0 wrong links in both runs",
            out / "accuracy-train-ticket.svg")
    grouped(SYSTEMS, ("First run (blind for PetClinic, Robot Shop, Train Ticket)", "Final"),
            "Link recall on five public microservice systems",
            "Precision is 100% on every system in the final run; no forbidden links in first or final runs",
            out / "recall-five-systems.svg")
    print("wrote", sorted(p.name for p in out.glob("*.svg")))


if __name__ == "__main__":
    main()
