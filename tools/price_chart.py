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
    # DO NOT prune up front. On cloud runs the live yfinance feed is blocked, so
    # every fetch below fails - if we deleted first we would wipe the charts the
    # relay already committed and hand the brain an empty folder (the exact bug
    # observed 2026-07-14). Instead we overwrite each chart only after its fetch
    # succeeds, and prune stale NON-focus names at the end. A focus name whose
    # fetch fails this run keeps its last-good (relay-committed) chart.
    focus_set = {str(t).upper() for t in tickers}

    rendered = []
    for t in tickers:
        try:
            df = yf.Ticker(t).history(period="6mo", interval="1d", auto_adjust=True).dropna()
            if len(df) < 30:
                continue
            sma20 = df["Close"].rolling(20).mean()
            sma50 = df["Close"].rolling(50).mean()
            vol_ma50 = df["Volume"].rolling(50).mean()

            # Two panes: price (top) + volume (bottom). Volume is the conviction
            # dimension - reversals and real breakouts print in it first.
            fig, (ax, axv) = plt.subplots(
                2, 1, figsize=(12.8, 7.6), dpi=110, sharex=True,
                gridspec_kw={"height_ratios": [3, 1], "hspace": 0.06})
            fig.patch.set_facecolor("#fafafa")
            ax.set_facecolor("#fafafa")
            axv.set_facecolor("#fafafa")
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

            # Volume pane: bars colored by day direction; 50d average line; bars at
            # >=2x average darkened (effort spikes worth reading against the range).
            vols = df["Volume"].tolist()
            vma = vol_ma50.tolist()
            for i, (_, row) in enumerate(df.iterrows()):
                up = row["Close"] >= row["Open"]
                base = "#047857" if up else "#b91c1c"
                heavy = vma[i] and vols[i] >= 2 * vma[i]
                axv.bar(i, vols[i], width=0.7, color=base,
                        alpha=0.95 if heavy else 0.45, linewidth=0)
            axv.plot(x, vma, color="#52525b", linewidth=1.1, label="50d avg vol")
            # Annotate the volume read so the picture and the numbers agree.
            try:
                from tools.volume_analysis import analyze_volume
                vs = analyze_volume(
                    [float(v) for v in df["Open"]], [float(v) for v in df["High"]],
                    [float(v) for v in df["Low"]], [float(v) for v in df["Close"]],
                    [float(v) for v in vols])
                axv.set_title(f"volume - read: {vs.get('read')} | rvol {vs.get('rvol')} | "
                              f"CMF20 {vs.get('cmf_20')} | U/D {vs.get('updown_vol_ratio_25d')} | "
                              f"OBV div: {vs.get('obv_divergence')}",
                              loc="left", fontsize=10, color="#52525b", pad=6)
            except Exception:
                pass
            axv.tick_params(colors="#a1a1aa", length=0, labelsize=9)
            axv.grid(axis="y", color="#e4e4e7", linewidth=0.8)
            axv.set_axisbelow(True)
            for sp in axv.spines.values():
                sp.set_visible(False)

            ticks = list(range(0, len(df), max(len(df) // 6, 1)))
            axv.set_xticks(ticks)
            axv.set_xticklabels([df.index[i].strftime("%b %d") for i in ticks],
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

    # Prune only charts that are NOT part of this run's focus set (leftovers from
    # a previous focus). Focus names we could not refresh keep their prior chart.
    for old in CHART_DIR.glob("*.png"):
        if old.stem.upper() not in focus_set:
            try:
                old.unlink()
            except OSError:
                pass
    return rendered


if __name__ == "__main__":
    import sys
    for p in render_charts(sys.argv[1:] or ["DELL"]):
        print(p)
