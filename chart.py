import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mplfinance as mpf

import config as C

_mc = mpf.make_marketcolors(up="#26a69a", down="#ef5350", wick="inherit", edge="inherit")
STYLE = mpf.make_mpf_style(marketcolors=_mc, gridstyle=":", y_on_right=True)
LEVELS = (("entry", "Вход", "#1565c0"), ("stop", "Стоп", "#c62828"), ("tp", "Тейк", "#2e7d32"))


def render(df, zones, path, title, levels=None, mark=None, bars=C.CHART_BARS):
    with C.PLOT_LOCK:
        return _render(df, zones, path, title, levels, mark, bars)


def _render(df, zones, path, title, levels, mark, bars):
    d = df.tail(bars)
    fig, axes = mpf.plot(d, type="candle", style=STYLE, returnfig=True, figsize=(12, 6.5), title=title)
    ax = axes[0]
    lo, hi = d.low.min(), d.high.max()
    rng = hi - lo
    for z in zones:
        if z["high"] < lo - rng or z["low"] > hi + rng:
            continue
        color = "#ef5350" if z["type"] == "R" else "#26a69a"
        ax.axhspan(z["low"], z["high"], color=color, alpha=0.18)
        ax.text(1, z["high"], f"#{z['id']} {'сопротивление' if z['type'] == 'R' else 'поддержка'}",
                color=color, fontsize=8, va="bottom")
        lo, hi = min(lo, z["low"]), max(hi, z["high"])
    for key, label, color in LEVELS:
        v = (levels or {}).get(key)
        if v is None:
            continue
        ax.axhline(v, color=color, lw=1.2, ls="--")
        ax.text(len(d) - 1, v, f" {label} {v:.2f}", color=color, fontsize=9, va="bottom", ha="right")
        lo, hi = min(lo, v), max(hi, v)
    if mark:
        pos = [k for k, ts in enumerate(d.index) if mark[0] <= ts <= mark[1]]
        if pos:
            ax.axvspan(pos[0] - 0.5, pos[-1] + 0.5, color="orange", alpha=0.25)
    pad = (hi - lo) * 0.05
    ax.set_ylim(lo - pad, hi + pad)
    fig.savefig(path, dpi=110, bbox_inches="tight")
    plt.close(fig)
    return path
