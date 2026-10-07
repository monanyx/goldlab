from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from .metrics import drawdown_series  # noqa: E402


def _daily(eq: pd.Series) -> pd.Series:
    return eq.resample("1D").last().dropna()


def equity_drawdown(eq: pd.Series, title: str, path: Path, split: pd.Timestamp | None = None,
                    extra: dict[str, pd.Series] | None = None) -> None:
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 6.5), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1.2]})
    d = _daily(eq)
    ax1.plot(d.index, d.values, lw=1.4, color="#1f5aa6", label="default params")
    for (name, s), color in zip((extra or {}).items(), ["#d9822b", "#3a9a5b"]):
        sd = _daily(s)
        ax1.plot(sd.index, sd.values, lw=1.2, color=color, label=name)
    ax1.axhline(eq.iloc[0], color="grey", lw=0.8, ls=":")
    dd = drawdown_series(d) * 100
    ax2.fill_between(dd.index, dd.values, 0, color="#b03a2e", alpha=0.45)
    ax2.set_ylabel("Drawdown % (default)")
    ax1.set_ylabel("Equity (USD)")
    if split is not None:
        for ax in (ax1, ax2):
            ax.axvline(split, color="black", lw=1, ls="--")
        ax1.text(split, ax1.get_ylim()[1], "  out-of-sample →", va="top", fontsize=9)
    ax1.set_title(title)
    ax1.legend(loc="upper left", fontsize=8, frameon=False)
    for ax in (ax1, ax2):
        ax.grid(alpha=0.25)
        ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def sensitivity_heatmap(table: pd.DataFrame, x: str, y: str, title: str, path: Path,
                        fixed: dict | None = None) -> None:
    t = table
    for k, v in (fixed or {}).items():
        t = t[t[k].astype(float).round(6) == round(float(v), 6)]
    if t.empty:
        return
    piv = t.pivot_table(index=y, columns=x, values="expectancy_r", aggfunc="mean")
    fig, ax = plt.subplots(figsize=(6.5, 4.5))
    lim = max(0.05, float(piv.abs().max().max()))
    im = ax.imshow(piv.values, cmap="RdBu", vmin=-lim, vmax=lim, aspect="auto", origin="lower")
    ax.set_xticks(range(len(piv.columns)), [f"{c:g}" for c in piv.columns])
    ax.set_yticks(range(len(piv.index)), [f"{c:g}" for c in piv.index])
    ax.set_xlabel(x)
    ax.set_ylabel(y)
    for (r, c), v in pd.DataFrame(piv.values).stack().items():
        ax.text(c, r, f"{v:+.2f}", ha="center", va="center", fontsize=8,
                color="white" if abs(v) > 0.6 * lim else "black")
    fig.colorbar(im, ax=ax, label="expectancy (R)")
    ax.set_title(title, fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
