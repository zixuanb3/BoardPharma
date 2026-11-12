"""Purpose: Plot quarterly SSR firm/product counts and ATC3 sharing by cohort.
Process: Read selected policy cohorts, count units, render charts and audit inputs.
Input: Quarterly firm panels and saved static or dynamic event/req2/Not cohorts.
Output: Policy- and sharing-specific CSV summaries, figures and an input manifest.

Plot existing quarterly SSR cohorts with the original annual chart styles.

Read the 1.1 event/req2/Not cohorts, retaining their saved sample and ATC3
sharing definitions. Count unique BoardName and BoardName-product units as in
1_data_prep/CohortPanelMaker.py. Count treated sharing units at the actual
event quarter, replacing the annual Q1 anchor in ATC3DistributionPlotter.py.

The firm/product renderer is the annual CohortPlotter itself, loaded without
running its main block. The sharing renderer copies the annual plot_summary
settings; only its time coordinates, tick text, and x-axis label become quarters.
No input files or existing scripts are changed. Missing cohorts count as zero,
as in the annual plots, only after checking that the upstream event/balance
columns are absent. An input manifest distinguishes missing from observed zero.

Run from any directory: python -B 1.1_ssrquarter/CohortDistributionPlotter.py
Outputs: {figures,csv}/ssrquarter/cohort_distribution_{firm,product}/... and
{figures,csv}/ssrquarter/cohort_sharing_atc3/..., retaining every definition in
the directory path. Each series covers 2009Q1--2018Q4. Existing outputs are
not overwritten. No cohorts or sharing labels are reconstructed.
"""

from __future__ import annotations

import argparse
from itertools import product
from pathlib import Path
import runpy

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
import pandas as pd


# ========================== USER CONFIG ==========================
PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUN_CONFIG = {
    "ever_board": 1,  # 0: legacy roster, 1: all-role continuity.
    "ieoc_ref": 1,  # 0: baseline BP expansion, 1: use IE/OC coverage.
    # Quarterly roster variants and upstream event definitions to plot.
    "roster_variants": ["drop_sentinel_yearquarter"],
    "late_values": [3],
    "req1_specs": ["new"],
    # Allowed stay rules: new, origin, pre2. --stay-spec can select one.
    "stay_specs": ["pre2"],
    # Static sharing accepts new and origin. Dynamic sharing uses only new.
    "share_time_specs": ["new"],
    # 0: read static cohorts. 1: read dynamic cohorts and separate the outputs.
    "dynamic_share": 0,
    # Event types, treated sides (A/B), and inclusive YYYYQn plot range.
    "events": ["to_B_not_in_A", "to_B_still_in_A", "interlock_dissolution"],
    "treatment_groups": ["A", "B"],
    "start_quarter": "2009Q1",
    "end_quarter": "2018Q4",
}
# ================================================================

COHORT_ROOT = PROJECT_ROOT / "data" / "ssrquarter" / "cohort_data"
PANEL_ROOT = PROJECT_ROOT / "data" / "ssrquarter" / "firm_panels"
FIGURE_ROOT = PROJECT_ROOT / "figures" / "ssrquarter"
CSV_ROOT = PROJECT_ROOT / "csv" / "ssrquarter"
MANIFEST_PATH = CSV_ROOT / "cohort_distribution_inputs.csv"
EVENT_STEMS = {
    "to_B_not_in_A": "to_B_not_in_A",
    "to_B_still_in_A": "to_B_still_in_A",
    "interlock_dissolution": "interlock_dissolution_leave_B",
}
FAMILIES = (
    "cohort_distribution_firm",
    "cohort_distribution_product",
    "cohort_sharing_atc3",
)


def configurations(dynamic_share: int = 0, stay_spec: str | None = None) -> list[Path]:
    """Return the selected roster and sharing directories."""
    share_specs = RUN_CONFIG["share_time_specs"]
    stay_specs = [stay_spec] if stay_spec is not None else RUN_CONFIG["stay_specs"]
    suffix = ""
    if dynamic_share:
        if "new" not in share_specs:
            raise ValueError("dynamic_share=1 requires new in share_time_specs")
        share_specs = ["new"]
        suffix = "_ds1"
    return [
        Path(variant) / f"late_{late}" / f"req1_{req1}" / f"stay_{stay}"
        / f"share_time_{share}{suffix}"
        for variant, late, req1, stay, share in product(
            RUN_CONFIG["roster_variants"], RUN_CONFIG["late_values"],
            RUN_CONFIG["req1_specs"], stay_specs,
            share_specs,
        )
    ]


def level_folder(side: str) -> str:
    """Use the existing counterpart-excluded Not cohort directory."""
    return f"quarter-level_{side}_without_{'B' if side == 'A' else 'A'}"


def cohort_path(spec: Path, side: str, event: str, period: pd.Period) -> Path:
    """Locate one original cohort without substituting another input."""
    return (
        COHORT_ROOT / spec / level_folder(side) / "event" / "req2" / "Not"
        / f"{event}_quarter_cohort_{period.year}q{period.quarter}_balanced.csv"
    )


def output_relative(spec: Path, side: str, event: str, family: str) -> Path:
    """Retain all input dimensions in each figure/table path."""
    folder = level_folder(side)
    stem = (
        f"sharing_atc3_{event}"
        if family == "cohort_sharing_atc3"
        else event
    )
    return Path(family) / spec / folder / "event" / "req2" / "Not" / stem


def preflight(specs: list[Path], periods: pd.PeriodIndex) -> set[Path]:
    """Validate all absent cohorts against panel headers before writing output."""
    if MANIFEST_PATH.exists():
        raise FileExistsError(MANIFEST_PATH)
    missing: set[Path] = set()
    headers: dict[Path, set[str]] = {}
    for spec, side, event in product(
        specs, RUN_CONFIG["treatment_groups"], RUN_CONFIG["events"],
    ):
        panel = (
            PANEL_ROOT / spec.parent / f"quarter-level_{side}"
            / f"ssr_firm_panel_{EVENT_STEMS[event]}_req2.csv"
        )
        if panel not in headers:
            headers[panel] = set(pd.read_csv(panel, nrows=0).columns)
        for period in periods:
            label = f"{period.year}q{period.quarter}"
            expected = {f"event_{label}", f"balance_panel_{label}"}.issubset(
                headers[panel]
            )
            source = cohort_path(spec, side, event, period)
            if source.is_file() != expected:
                raise ValueError(
                    f"Cohort/panel availability mismatch: {source}; "
                    f"upstream event/balance columns present={expected}"
                )
            if not expected:
                missing.add(source)
        for family in FAMILIES:
            relative = output_relative(spec, side, event, family)
            for root, suffix in ((FIGURE_ROOT, ".png"), (CSV_ROOT, ".csv")):
                destination = root / relative.with_suffix(suffix)
                if destination.exists():
                    raise FileExistsError(destination)
    return missing


def count_cohort(source: Path, period: pd.Period) -> tuple[int, ...]:
    """Apply annual deduplication rules, anchoring sharing at this event quarter."""
    event_col = f"event_{period.year}q{period.quarter}"
    frame = pd.read_csv(
        source,
        usecols=["BoardName", "product", "year", "quarter", event_col,
                 "atc3_sharing"],
    )
    if not frame[event_col].isin([0, 1]).all():
        raise ValueError(f"Invalid treatment indicator: {source}")
    if not frame["atc3_sharing"].isin([0, 1]).all():
        raise ValueError(f"Invalid sharing indicator: {source}")
    treated = frame.loc[frame[event_col].eq(1)]
    controls = frame.loc[frame[event_col].eq(0)]
    treated_units = treated.loc[
        treated["year"].eq(period.year) & treated["quarter"].eq(period.quarter),
        ["BoardName", "product", "atc3_sharing"],
    ].drop_duplicates(subset=["BoardName", "product"])
    sharing = int(treated_units["atc3_sharing"].sum())
    total = len(treated_units)
    return (
        len(treated[["BoardName"]].drop_duplicates()),
        len(controls[["BoardName"]].drop_duplicates()),
        len(treated[["BoardName", "product"]].drop_duplicates()),
        len(controls[["BoardName", "product"]].drop_duplicates()),
        sharing, total - sharing, total,
    )


def plot_sharing(
    summary: pd.DataFrame, time_ids: list[int], out_png: Path, title: str,
) -> None:
    """Copy annual plot_summary; adapt only the time unit and its display."""
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar(time_ids, summary["sharing_atc"], label="sharing_atc")
    ax.bar(
        time_ids,
        summary["not_sharing_atc"],
        bottom=summary["sharing_atc"],
        label="not_sharing_atc",
    )
    ax.xaxis.set_major_formatter(FuncFormatter(
        lambda value, position: f"{int(round(value)) // 4}Q{int(round(value)) % 4 + 1}"
    ))
    ax.set_xlabel("Quarter")
    ax.set_ylabel("Treated BoardName-product count")
    ax.set_title(title)
    ax.legend()
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(out_png, dpi=300)
    plt.close(fig)


def main() -> None:
    """Write three annual-style charts for every selected event/side definition."""
    global COHORT_ROOT, PANEL_ROOT, FIGURE_ROOT, CSV_ROOT, MANIFEST_PATH
    from ever_board_config import (
        membership_path,
        validate_policies,
    )

    parser = argparse.ArgumentParser(description="Plot quarterly SSR cohorts by membership policy.")
    parser.add_argument("--ever-board", "--ever_board", type=int, choices=(0, 1), default=RUN_CONFIG["ever_board"])
    parser.add_argument("--ieoc-ref", "--ieoc_ref", type=int, choices=(0, 1), default=RUN_CONFIG["ieoc_ref"])
    parser.add_argument(
        "--stay-spec", choices=("new", "origin", "pre2"),
        help="Plot one stay rule instead of the stay rules in USER CONFIG.",
    )
    parser.add_argument(
        "--dynamic-share", "--dynamic_share", type=int, choices=(0, 1),
        default=RUN_CONFIG["dynamic_share"],
        help="Read dynamic sharing cohorts; use only the new sharing-time rule.",
    )
    args = parser.parse_args()
    try:
        validate_policies(args.ever_board, args.ieoc_ref)
        specs = configurations(args.dynamic_share, args.stay_spec)
    except ValueError as error:
        parser.error(str(error))
    COHORT_ROOT = membership_path(COHORT_ROOT, args.ever_board, args.ieoc_ref)
    PANEL_ROOT = membership_path(PANEL_ROOT, args.ever_board, args.ieoc_ref)
    FIGURE_ROOT = membership_path(FIGURE_ROOT, args.ever_board, args.ieoc_ref)
    CSV_ROOT = membership_path(CSV_ROOT, args.ever_board, args.ieoc_ref)
    manifest_suffix = "_ds1" if args.dynamic_share else ""
    manifest_stays = [args.stay_spec] if args.stay_spec is not None else RUN_CONFIG["stay_specs"]
    if len(manifest_stays) == 1:
        manifest_suffix += f"_stay_{manifest_stays[0]}"
    MANIFEST_PATH = CSV_ROOT / f"cohort_distribution_inputs{manifest_suffix}.csv"
    periods = pd.period_range(
        RUN_CONFIG["start_quarter"], RUN_CONFIG["end_quarter"], freq="Q"
    )
    missing = preflight(specs, periods)
    annual = runpy.run_path(
        str(PROJECT_ROOT / "codes" / "1_data_prep" / "CohortPanelMaker.py")
    )
    renderer = annual["CohortPlotter"](annual["RUN_CONFIG"])
    labels = [str(period) for period in periods]
    time_ids = [period.year * 4 + period.quarter - 1 for period in periods]
    manifest: list[dict] = []
    figure_count = 0
    for spec, side, event in product(
        specs, RUN_CONFIG["treatment_groups"], RUN_CONFIG["events"],
    ):
        counts = []
        for period in periods:
            source = cohort_path(spec, side, event, period)
            values = (0,) * 7 if source in missing else count_cohort(source, period)
            counts.append(values)
            manifest.append({
                "definition": spec.as_posix(), "side": side, "event": event,
                "Quarter": str(period), "source_path": str(source),
                "source_exists": source not in missing,
                "status": "no_upstream_event_column" if source in missing else "observed",
                "treated_firms": values[0], "control_firms": values[1],
                "treated_products": values[2], "control_products": values[3],
                "sharing_atc": values[4], "not_sharing_atc": values[5],
                "total": values[6],
            })
        for unit, offset in (("firm", 0), ("product", 2)):
            family = f"cohort_distribution_{unit}"
            relative = output_relative(spec, side, event, family)
            summary = pd.DataFrame({
                "Quarter": labels, "Treated": [row[offset] for row in counts],
                "Control": [row[offset + 1] for row in counts],
            })
            csv_path = CSV_ROOT / relative.with_suffix(".csv")
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            summary.to_csv(csv_path, index=False, mode="x")
            unit_config = annual["DISTRIBUTION_CONFIGS"][unit]
            renderer.draw_two_bar_plot(
                years=labels, treated_counts=summary["Treated"].tolist(),
                control_counts=summary["Control"].tolist(), unit_config=unit_config,
                title=(f"{level_folder(side)} | {unit_config['title_label']} | "
                       f"{event} | event | req2 | not | balanced"),
                out_path=FIGURE_ROOT / relative.with_suffix(".png"),
            )
        summary = pd.DataFrame({
            "Quarter": labels, "sharing_atc": [row[4] for row in counts],
            "not_sharing_atc": [row[5] for row in counts],
            "total": [row[6] for row in counts],
        })
        if not summary["total"].eq(
            summary["sharing_atc"] + summary["not_sharing_atc"]
        ).all():
            raise AssertionError(f"Sharing counts do not reconcile: {spec}/{side}/{event}")
        relative = output_relative(spec, side, event, "cohort_sharing_atc3")
        csv_path = CSV_ROOT / relative.with_suffix(".csv")
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        summary.to_csv(csv_path, index=False, mode="x")
        plot_sharing(
            summary, time_ids, FIGURE_ROOT / relative.with_suffix(".png"),
            f"{event} | {level_folder(side)} | req2 | atc3",
        )
        figure_count += 3
        print(f"Saved 3 charts: {spec.as_posix()} | {side} | {event}", flush=True)
    pd.DataFrame(manifest).to_csv(MANIFEST_PATH, index=False, mode="x")
    print(
        f"Finished: {len(specs)} roster/definition combinations; "
        f"{figure_count} figures and matching tables; {len(missing)} missing "
        f"cohort positions plotted as zero under the annual rule. "
        f"Manifest: {MANIFEST_PATH}", flush=True,
    )


if __name__ == "__main__":
    main()
