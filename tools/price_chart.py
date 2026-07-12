"""Candlestick chart renderer - gives the brain actual charts to look at.

The scanner's numeric indicators can't convey visual structure (bases, failed
breakouts, support shelves). This renders a 6-month daily candlestick chart
with 20/50-day averages per ticker into data/charts/<TICKER>.png; charts are
committed by the data relay so cloud runs can Read them too.

CLI: python -m tools.price_chart DELL HPE
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHART_DIR = ROOT / "data" / "charts"


def render_charts(tickers: list[str]) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import yfinance as yf

    CHART_DIR.mkdir(parents=True, exist_ok=True)
    # Prune stale charts so the folder only holds this run's focus names.
    for old in CHART_DIR.glob("*.png"):
        old.unlink()

    rendered = []
    for t in tickers:
        try:
            df = yf.Ticker(t).history(period="6mo", interval="1d", auto_adjust=True).dropna()
            if len(df) < 30:
                continue
            sma20 = df["Close"].rolling(20).mean()
            sma50 = df["Close"].rolling(50).mean()

            fig, ax = plt.subplots(figsize=(12.8, 6.0), dpi=110)
            fig.patch.set_facecolor("#fafafa")
            ax.set_facecolor("#fafafa")
            x = range(len(df))
            for i, (_, row) in enumerate(df.iterrows()):
                up = row["Close"] >= row["Open"]
                color = "#047857" if up else "#b91c1c"
                ax.plot([i, i], [row["Low"], row["High"]], color=color, linewidth=0.7)
                ax.add_patch(plt.Rectangle(
                    (i - 0.35, min(row["Open"], row["Close"])), 0.7,
                    max(abs(row["Close"] - row["Open"]), 0.01),
                    facecolor=color, edgecolor=color))
            ax.plot(x, sma20, color="#2563eb", linewidth=1.2, label="20d avg")
            ax.plot(x, sma50, color="#d97706", linewidth=1.2, label="50d avg")
            ticks = list(range(0, len(df), max(len(df) // 6, 1)))
            ax.set_xticks(ticks)
            ax.set_xticklabels([df.index[i].strftime("%b %d") for i in ticks],
                               color="#a1a1aa", fontsize=10)
            ax.tick_params(colors="#a1a1aa", length=0)
            for sp in ax.spines.values():
                sp.set_visible(False)
            ax.grid(axis="y", color="#e4e4e7", linewidth=0.8)
            ax.set_axisbelow(True)
            ax.set_title(f"{t} - 6 months, daily", loc="left", fontsize=15,
                         fontweight="bold", color="#18181b", pad=12)
            ax.legend(loc="upper left", frameon=False, fontsize=10)
            fig.tight_layout(pad=2)
            out = CHART_DIR / f"{t.upper()}.png"
            fig.savefig(out, facecolor="#fafafa")
            plt.close(fig)
            rendered.append(str(out))
        except Exception:
            continue
    return rendered


if __name__ == "__main__":
    import sys
    for p in render_charts(sys.argv[1:] or ["DELL"]):
        print(p)
