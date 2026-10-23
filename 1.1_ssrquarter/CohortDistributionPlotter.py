"""Plot existing quarterly SSR cohorts with the original annual chart styles.

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

from itertools import product
from pathlib import Path
import runpy

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]

# ========================== USER CONFIG ==========================
RUN_CONFIG = {
    "roster_variants": ["imputed_yearquarter", "drop_sentinel_yearquarter"],
    "late_values": [0, 3],
    "req1_specs": ["new", "origin"],
    "stay_specs": ["new", "origin"],
    "share_time_specs": ["new", "origin"],
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


def configurations() -> list[Path]:
    """Return all selected roster and four-parameter directories."""
    return [
        Path(variant) / f"late_{late}" / f"req1_{req1}" / f"stay_{stay}"
        / f"share_time_{share}"
        for variant, late, req1, stay, share in product(
            RUN_CONFIG["roster_variants"], RUN_CONFIG["late_values"],
            RUN_CONFIG["req1_specs"], RUN_CONFIG["stay_specs"],
            RUN_CONFIG["share_time_specs"],
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
    periods = pd.period_range(
        RUN_CONFIG["start_quarter"], RUN_CONFIG["end_quarter"], freq="Q"
    )
    specs = configurations()
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
