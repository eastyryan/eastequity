"""Equity-curve card generator for X posts.

Renders the dashboard's equity-vs-S&P chart as a shareable PNG in the site's
visual language (light background, emerald agent line, gray SPY line), with
the account avatar in the top-right corner so the card reads as @EastEquity's.

CLI: python -m tools.chart_card [out.png]
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PAPER = "#fafafa"
INK = "#18181b"
INK2 = "#52525b"
INK3 = "#a1a1aa"
LINE = "#e4e4e7"
ACCENT = "#047857"
START = 10000.0


def render_equity_card(out_path: str | Path | None = None) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import ticker as mticker
    from PIL import Image, ImageDraw

    out = Path(out_path) if out_path else ROOT / "state" / "equity_card.png"
    hist = json.loads((ROOT / "dashboard" / "data" / "equity_history.json").read_text())
    dates = [h["date"][5:].replace("-", "/") for h in hist]
    equity = [h["equity"] for h in hist]
    bench_raw = [h.get("benchmark_close") for h in hist]
    base = next((b for b in bench_raw if b), None)
    bench = [(b / base) * START if (b and base) else None for b in bench_raw]

    fig, ax = plt.subplots(figsize=(12.8, 6.4), dpi=150)
    fig.patch.set_facecolor(PAPER)
    ax.set_facecolor(PAPER)

    if any(v is not None for v in bench):
        xs = [i for i, v in enumerate(bench) if v is not None]
        ax.plot(xs, [bench[i] for i in xs], color=INK3, linewidth=2,
                label="S&P 500 (SPY), same capital", zorder=2)
    ax.plot(range(len(equity)), equity, color=ACCENT, linewidth=2.5,
            label="Agent", zorder=3)
    if len(equity) <= 20:
        ax.scatter(range(len(equity)), equity, color=ACCENT, s=28,
                   edgecolor=PAPER, linewidth=1.5, zorder=4)

    ax.set_title("East Equity Agent - equity curve", loc="left", fontsize=17,
                 fontweight="bold", color=INK, pad=18)
    ax.text(0, 1.035, "", transform=ax.transAxes)
    ax.set_xticks(range(len(dates)))
    ax.set_xticklabels(dates, color=INK3, fontsize=11)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"${v:,.0f}"))
    ax.tick_params(colors=INK3, length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.grid(axis="y", color=LINE, linewidth=1)
    ax.set_axisbelow(True)
    leg = ax.legend(loc="upper left", frameon=False, fontsize=11, labelcolor=INK2)
    pad = max((max(equity) - min(equity)) * 0.5, 120)
    lo = min([min(equity)] + [v for v in bench if v]) - pad
    hi = max([max(equity)] + [v for v in bench if v]) + pad
    ax.set_ylim(lo, hi)
    fig.tight_layout(pad=2.2)
    fig.savefig(out, facecolor=PAPER)
    plt.close(fig)

    # Avatar badge, top-right, circular crop with a hairline ring.
    avatar_path = ROOT / "dashboard" / "public" / "avatar.jpg"
    if avatar_path.exists():
        card = Image.open(out).convert("RGBA")
        size = 148
        av = Image.open(avatar_path).convert("RGBA").resize((size, size))
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
        x = card.width - size - 42
        y = 34
        ring = ImageDraw.Draw(card)
        ring.ellipse((x - 3, y - 3, x + size + 3, y + size + 3), fill=LINE)
        card.paste(av, (x, y), mask)
        card.convert("RGB").save(out)
    return out


if __name__ == "__main__":
    import sys
    print(render_equity_card(sys.argv[1] if len(sys.argv) > 1 else None))
