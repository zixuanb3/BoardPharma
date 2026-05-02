"""
Purpose:
Reproduce did_imputation event-study figures with formatting tailored for
side-by-side LaTeX placement. For each (control, scaling, target, mode) combo,
the `to_B_not_in_A` and `to_B_still_in_A` figures are drawn with a shared
vertical-axis range so they can be compared directly when placed side by side.

Process:
- Read per-coefficient CSVs emitted by did_imputation_event_study.do.
- For each group (event_type=event, control, scaling, mode, target), collect
  the CI bounds across both paired events and derive a common y-range.
- Plot each figure separately (still two files per pair) using the shared
  range, matching the event_plot visual style (black dot = sharingatc3=0,
  navy triangle = sharingatc3=1, -0.5 vertical dashed cut, horizontal zero
  line).

Input:
- csv/did_imputation_event_study_sharingatc3/quarter/{event}/{target}/
    {event}_{target}_event_{control}_{scaling}_{mode}_sharingatc3.csv

Output:
- figures/did_imputation_event_study_sharingatc3_formatted/quarter/
    {control}_{scaling}/{mode}/{event}_{target}.png
"""

from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

mpl.rcParams.update(
    {
        "font.family": "serif",
        "font.serif": ["Times New Roman", "Times", "DejaVu Serif"],
        "mathtext.fontset": "stix",
    }
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CSV_ROOT = PROJECT_ROOT / "csv" / "did_imputation_event_study_sharingatc3" / "quarter"
FIG_ROOT = (
    PROJECT_ROOT
    / "figures"
    / "did_imputation_event_study_sharingatc3_formatted"
    / "quarter"
)

EVENT_PAIR = ("to_B_not_in_A", "to_B_still_in_A")
TARGETS = ("price", "quantity", "revenue")
MODES = ("hetby", "separate")

# (control_filename_token, scaling). event_type is always "event" per request.
GROUPS = [
    ("not_yet", "normalize"),
    ("not_yet", "standardize"),
    ("not", "normalize"),
    ("not", "standardize"),
    ("pure_control", "normalize"),
    ("pure_control", "standardize"),
]

CONTROL_TITLE = {
    "not_yet": "Not-yet",
    "not": "Not",
    "pure_control": "Pure control",
}

SERIES = {
    0: {"label": "sharingatc3=0", "color": "black", "marker": "o", "offset": -0.15},
    1: {"label": "sharingatc3=1", "color": "navy", "marker": "^", "offset": 0.15},
}

X_MIN, X_MAX = -2, 7


def csv_path(event: str, target: str, control: str, scaling: str, mode: str) -> Path:
    stub = f"{event}_{target}_event_{control}_{scaling}_{mode}_sharingatc3"
    return CSV_ROOT / event / target / f"{stub}.csv"


def load_series(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df[(df["rel_quarter"] >= X_MIN) & (df["rel_quarter"] <= X_MAX)].copy()
    return df


def shared_ylim(frames: list[pd.DataFrame], pad: float = 0.08) -> tuple[float, float]:
    lows, highs = [], []
    for df in frames:
        for col in ("ci_lb_95", "ci_ub_95", "estimate"):
            vals = pd.to_numeric(df[col], errors="coerce").dropna()
            if not vals.empty:
                lows.append(vals.min())
                highs.append(vals.max())
    if not lows:
        return -1.0, 1.0
    lo, hi = min(lows), max(highs)
    span = hi - lo if hi > lo else max(abs(hi), 1.0)
    return lo - pad * span, hi + pad * span


def plot_single(df: pd.DataFrame, title: str, ytitle: str, ylim, out_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(7.2, 4.8))

    for sval, opts in SERIES.items():
        sub = df[df["sharingatc3"] == sval].copy()
        sub = sub.dropna(subset=["estimate"])
        if sub.empty:
            continue
        xs = sub["rel_quarter"].to_numpy() + opts["offset"]
        ys = sub["estimate"].to_numpy()
        lb = pd.to_numeric(sub["ci_lb_95"], errors="coerce").to_numpy()
        ub = pd.to_numeric(sub["ci_ub_95"], errors="coerce").to_numpy()
        yerr = np.vstack([ys - lb, ub - ys])
        ax.errorbar(
            xs,
            ys,
            yerr=yerr,
            fmt=opts["marker"],
            color=opts["color"],
            markerfacecolor="none" if opts["marker"] == "^" else opts["color"],
            markeredgecolor=opts["color"],
            ecolor=opts["color"],
            capsize=3,
            linestyle="none",
            label=opts["label"],
        )

    ax.axhline(0, color="gray", linewidth=0.8)
    ax.axvline(-0.5, color="gray", linestyle="--", linewidth=0.8)
    ax.set_xlim(X_MIN - 0.6, X_MAX + 0.6)
    ax.set_xticks(range(X_MIN, X_MAX + 1))
    ax.set_ylim(*ylim)
    ax.set_xlabel("Periods since the event")
    ax.set_ylabel(ytitle)
    ax.set_title(title, fontsize=11)
    ax.grid(axis="y", linestyle=":", linewidth=0.4, alpha=0.6)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, -0.25), ncol=2, frameon=False)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    plt.close(fig)


def format_event_name(event: str) -> str:
    return event.replace("_", " ")


def main() -> None:
    missing = []
    written = 0
    for control, scaling in GROUPS:
        for mode in MODES:
            for target in TARGETS:
                frames = {}
                for event in EVENT_PAIR:
                    path = csv_path(event, target, control, scaling, mode)
                    if not path.exists():
                        missing.append(str(path))
                        continue
                    frames[event] = load_series(path)

                if len(frames) < 2:
                    continue

                ylim = shared_ylim(list(frames.values()))

                group_dir = FIG_ROOT / f"{control}_{scaling}" / mode
                mode_title = "hetby" if mode == "hetby" else "separate samples"
                for event, df in frames.items():
                    title = (
                        f"did_imputation ({mode_title}): "
                        f"{format_event_name(event)} event "
                        f"{CONTROL_TITLE[control]} {scaling}"
                    )
                    out_path = group_dir / f"{event}_{target}.png"
                    plot_single(df, title=title, ytitle=target, ylim=ylim, out_path=out_path)
                    written += 1

    print(f"Wrote {written} figures to {FIG_ROOT}")
    if missing:
        print(f"Skipped {len(missing)} missing CSV(s). First few:")
        for p in missing[:5]:
            print(f"  - {p}")


if __name__ == "__main__":
    main()
