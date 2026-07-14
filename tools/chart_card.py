"""Equity-curve card generator for X posts.

Portfolio-app style card (modeled on the Claude Portfolio share card, user
direction 2026-07-13): warm off-white canvas, avatar + "EastEquity's Agent
Portfolio" header with a big signed % return, "S&P 500" column on the right,
both series normalized to % change since inception, endpoint markers with a
halo, a white tooltip chip with the latest date/values, and decorative
timeframe pills. Values color by sign (green up / red down) — the card never
flatters a drawdown.

CLI: python -m tools.chart_card [out.png]
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PAPER = "#f1f0ea"     # warm off-white canvas
INK = "#141413"
SUB = "#9d9c96"       # muted date/label gray
GREEN = "#12a06a"     # portfolio line + positive values
RED = "#dc2626"
GRAY = "#8e8d88"      # S&P line + its values
BASELINE = "#c9c8c1"
CHIP = "#ffffff"
CHIP_BORDER = "#e4e3dd"
PILL_INK = "#141413"
START = 10000.0


def _fmt_date(d: str) -> str:
    return datetime.fromisoformat(d).strftime("%b %-d, %Y")


def _signed(pct: float) -> tuple[str, str, str]:
    """(arrow, text, color) for a signed percent."""
    if pct >= 0:
        return "▲", f"+{pct:.2f}%", GREEN
    return "▼", f"{pct:.2f}%", RED


def render_equity_card(out_path: str | Path | None = None) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch
    from PIL import Image, ImageDraw

    out = Path(out_path) if out_path else ROOT / "state" / "equity_card.png"
    hist = json.loads((ROOT / "dashboard" / "data" / "equity_history.json").read_text())
    equity = [h["equity"] for h in hist]
    agent_pct = [e / START * 100 - 100 for e in equity]

    bench_raw = [h.get("benchmark_close") for h in hist]
    bench_base = next((b for b in bench_raw if b), None)
    bench_xs = [i for i, b in enumerate(bench_raw) if b and bench_base]
    bench_pct = [bench_raw[i] / bench_base * 100 - 100 for i in bench_xs]

    date_range = f"{_fmt_date(hist[0]['date'])} - {_fmt_date(hist[-1]['date'])}"
    a_arrow, a_text, a_color = _signed(agent_pct[-1])
    s_arrow, s_text, s_color = _signed(bench_pct[-1]) if bench_pct else ("", "n/a", SUB)

    W = H = 10.8  # 1080x1080 @ dpi 100
    fig = plt.figure(figsize=(W, H), dpi=100)
    fig.patch.set_facecolor(PAPER)

    # ---- header ----------------------------------------------------------
    fig.text(0.148, 0.948, "EastEquity's Agent Portfolio",
             fontsize=22, fontweight="bold", color=INK, va="center")
    fig.text(0.700, 0.948, "S&P 500",
             fontsize=22, fontweight="bold", color=INK, va="center")

    fig.text(0.048, 0.873, f"{a_arrow} {a_text}",
             fontsize=30, fontweight="bold", color=a_color, va="center")
    fig.text(0.700, 0.873, f"{s_arrow} {s_text}",
             fontsize=30, fontweight="bold", color=s_color, va="center")

    fig.text(0.048, 0.826, date_range, fontsize=13, color=SUB, va="center")
    fig.text(0.700, 0.826, date_range, fontsize=13, color=SUB, va="center")

    # ---- chart -----------------------------------------------------------
    ax = fig.add_axes([0.048, 0.175, 0.904, 0.585])
    ax.set_facecolor(PAPER)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_xticks([])
    ax.set_yticks([])

    ax.axhline(0, color=BASELINE, linewidth=1.2, linestyle=(0, (4, 4)), zorder=1)

    n = len(agent_pct)
    last_x = n - 1
    ax.axvline(last_x, color=BASELINE, linewidth=1.2, zorder=1)

    if bench_pct:
        ax.plot(bench_xs, bench_pct, color=GRAY, linewidth=2.6,
                solid_capstyle="round", zorder=2)
        ax.scatter([bench_xs[-1]], [bench_pct[-1]], s=340, color=GRAY,
                   alpha=0.22, linewidths=0, zorder=3)
        ax.scatter([bench_xs[-1]], [bench_pct[-1]], s=68, color=GRAY,
                   edgecolor=PAPER, linewidth=1.6, zorder=4)

    ax.plot(range(n), agent_pct, color=GREEN, linewidth=3.0,
            solid_capstyle="round", zorder=5)
    ax.scatter([last_x], [agent_pct[-1]], s=340, color=GREEN,
               alpha=0.22, linewidths=0, zorder=6)
    ax.scatter([last_x], [agent_pct[-1]], s=68, color=GREEN,
               edgecolor=PAPER, linewidth=1.6, zorder=7)

    all_vals = agent_pct + bench_pct + [0.0]
    span = max(max(all_vals) - min(all_vals), 0.8)
    ax.set_ylim(min(all_vals) - span * 0.45, max(all_vals) + span * 0.45)
    ax.set_xlim(-0.35, last_x + 0.55)

    # ---- tooltip chip (lower-left: never covers the endpoint markers) -----
    chip = FancyBboxPatch((0.075, 0.205), 0.40, 0.155,
                          boxstyle="round,pad=0.012,rounding_size=0.018",
                          transform=fig.transFigure, facecolor=CHIP,
                          edgecolor=CHIP_BORDER, linewidth=1.2, zorder=10)
    fig.add_artist(chip)
    fig.text(0.098, 0.330, _fmt_date(hist[-1]["date"]), fontsize=13.5,
             color=INK, va="center", zorder=11)
    fig.lines.append(plt.Line2D([0.098, 0.455], [0.305, 0.305],
                                transform=fig.transFigure, color=CHIP_BORDER,
                                linewidth=1.0, zorder=11))
    fig.text(0.098, 0.272, "●", fontsize=12, color=GREEN, va="center", zorder=11)
    fig.text(0.122, 0.272, "EastEquity Agent", fontsize=13.5, color=INK,
             va="center", zorder=11)
    fig.text(0.455, 0.272, a_text, fontsize=13.5, fontweight="bold",
             color=a_color, va="center", ha="right", zorder=11)
    fig.text(0.098, 0.235, "●", fontsize=12, color=GRAY, va="center", zorder=11)
    fig.text(0.122, 0.235, "S&P 500", fontsize=13.5, color=INK,
             va="center", zorder=11)
    fig.text(0.455, 0.235, s_text, fontsize=13.5, fontweight="bold",
             color=s_color, va="center", ha="right", zorder=11)

    # ---- timeframe pills (decorative) -------------------------------------
    for x, label in ((0.335, "1W"), (0.425, "1M"), (0.515, "3M")):
        fig.text(x, 0.085, label, fontsize=14, color=SUB,
                 va="center", ha="center")
    pill = FancyBboxPatch((0.585, 0.066), 0.085, 0.038,
                          boxstyle="round,pad=0.008,rounding_size=0.022",
                          transform=fig.transFigure, facecolor=PILL_INK,
                          edgecolor="none", zorder=10)
    fig.add_artist(pill)
    fig.text(0.6275, 0.085, "MAX", fontsize=14, fontweight="bold",
             color="#ffffff", va="center", ha="center", zorder=11)

    fig.savefig(out, facecolor=PAPER)
    plt.close(fig)

    # ---- avatar, top-left, circular crop ----------------------------------
    avatar_path = ROOT / "dashboard" / "public" / "avatar.jpg"
    if avatar_path.exists():
        card = Image.open(out).convert("RGBA")
        size = 84
        av = Image.open(avatar_path).convert("RGBA").resize((size, size))
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
        x, y = 50, int(card.height * (1 - 0.948) - size / 2 + 4)
        ring = ImageDraw.Draw(card)
        ring.ellipse((x - 3, y - 3, x + size + 3, y + size + 3), fill=CHIP_BORDER)
        card.paste(av, (x, y), mask)
        card.convert("RGB").save(out)
    return out


if __name__ == "__main__":
    import sys
    print(render_equity_card(sys.argv[1] if len(sys.argv) > 1 else None))
