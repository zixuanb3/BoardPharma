"""
Purpose:
Count unique personnel regression-panel ids by treatment subgroup, matching
the personnel did_imputation id definition: group(A B product data_cohort).

Process:
1. Read personnel product-quarter cohort panels for each selected definition,
   event type, control set, treatment group, and cohort year 2009-2017.
2. Optionally keep only the regression event window, event_time in [-8, 11].
3. Classify ids into control, treated clean/nonshare, treated clean/share,
   treated confounded/nonshare, and treated confounded/share groups.
4. Count unique ids after checking id-level group consistency.
5. Save one summary CSV with counts and file status by cohort specification.

Input:
- data/personnel_regression_panels/{definition}/retain{retain_years}yr/{event_type}/{control_set}/treatment_group_{A|B}/reg_panel_cohort_YYYY_tg{A|B}.csv

Output:
- csv/personnel_cohort_id_counts/personnel_cohort_id_group_counts.csv
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd


CODE_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = CODE_ROOT.parent
INPUT_ROOT = PROJECT_ROOT / "data" / "personnel_regression_panels"
DEFAULT_OUTPUT_PATH = (
    PROJECT_ROOT
    / "csv"
    / "personnel_cohort_id_counts"
    / "personnel_cohort_id_group_counts.csv"
)

DEFINITIONS = ("narrow_board", "medium_board_csuite")
# broad_board_c_vp
EVENT_TYPES = ("to_B_still_in_A", "to_B_not_in_A", "dissolution")
CONTROL_SETS = ("C4", "C6A", "C6B")
TREATMENT_GROUPS = ("A", "B")
COHORT_YEARS = tuple(range(2009, 2019))
RETAIN_YEARS = 2

ID_COLS = ("A", "B", "product", "data_cohort")
CONFOUNDING_COLS = (
    "pre_retain_W",
    "pre_exit_W",
    "pre_dissolved_W",
    "sameq_retain",
    "sameq_exit",
    "sameq_dissolved",
    "post_retain",
    "post_exit",
    "post_dissolved",
)
REQUIRED_COLS = (
    "A",
    "B",
    "product",
    "treat",
    "event_time",
    "share_atc3",
    *CONFOUNDING_COLS,
)

GROUP_LABELS = (
    "control",
    "treat_clean_nonshare",
    "treat_clean_share",
    "treat_confounded_nonshare",
    "treat_confounded_share",
)


@dataclass(frozen=True)
class CohortSpec:
    """One parameter combination and cohort year."""

    definition: str
    event_type: str
    control_set: str
    treatment_group: str
    cohort_year: int
    retain_years: int

    @property
    def input_path(self) -> Path:
        """Return the generated personnel regression-panel CSV path."""
        return (
            INPUT_ROOT
            / self.definition
            / f"retain{self.retain_years}yr"
            / self.event_type
            / self.control_set
            / f"treatment_group_{self.treatment_group}"
            / f"reg_panel_cohort_{self.cohort_year}_tg{self.treatment_group}.csv"
        )


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Count unique A-B-product-cohort ids by personnel treatment subgroup.",
    )
    parser.add_argument("--definition", choices=DEFINITIONS)
    parser.add_argument("--event-type", "--event_type", dest="event_type", choices=EVENT_TYPES)
    parser.add_argument("--control-set", "--control_set", dest="control_set", choices=CONTROL_SETS)
    parser.add_argument(
        "--treatment-group",
        "--treatment_group",
        dest="treatment_group",
        choices=TREATMENT_GROUPS,
    )
    parser.add_argument("--cohort-year", type=int, choices=COHORT_YEARS)
    parser.add_argument(
        "--retain-years",
        "--retain_years",
        dest="retain_years",
        type=int,
        default=RETAIN_YEARS,
        help="Years a director must stay on B in the upstream personnel panels.",
    )
    parser.add_argument(
        "--no-event-window",
        action="store_true",
        help="Count all cohort-file rows instead of matching the regression window [-8, 11].",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help=f"Output CSV path. Default: {DEFAULT_OUTPUT_PATH}",
    )
    return parser.parse_args()


def selected_values(value: str | int | None, defaults: Sequence[str] | Sequence[int]) -> list:
    """Return either a user-selected singleton or all configured defaults."""
    if value is None:
        return list(defaults)
    return [value]


def iter_specs(args: argparse.Namespace) -> Iterable[CohortSpec]:
    """Yield all requested combination-by-cohort specs."""
    definitions = selected_values(args.definition, DEFINITIONS)
    event_types = selected_values(args.event_type, EVENT_TYPES)
    control_sets = selected_values(args.control_set, CONTROL_SETS)
    treatment_groups = selected_values(args.treatment_group, TREATMENT_GROUPS)
    cohort_years = selected_values(args.cohort_year, COHORT_YEARS)

    for definition in definitions:
        for event_type in event_types:
            for control_set in control_sets:
                for treatment_group in treatment_groups:
                    for cohort_year in cohort_years:
                        yield CohortSpec(
                            definition=str(definition),
                            event_type=str(event_type),
                            control_set=str(control_set),
                            treatment_group=str(treatment_group),
                            cohort_year=int(cohort_year),
                            retain_years=args.retain_years,
                        )


def read_required_columns(path: Path) -> pd.DataFrame:
    """Read only columns needed for subgroup id counts."""
    header = pd.read_csv(path, nrows=0)
    missing_cols = sorted(set(REQUIRED_COLS) - set(header.columns))
    if missing_cols:
        raise KeyError(f"{path} is missing required columns: {missing_cols}")
    return pd.read_csv(path, usecols=list(REQUIRED_COLS))


def to_numeric(series: pd.Series, column_name: str, path: Path) -> pd.Series:
    """Convert a required column to numeric with a clear error message."""
    converted = pd.to_numeric(series, errors="coerce")
    bad_values = series.notna() & converted.isna()
    if bad_values.any():
        examples = series.loc[bad_values].drop_duplicates().head(10).tolist()
        raise ValueError(f"{path.name}.{column_name} has nonnumeric values: {examples}")
    return converted


def add_group_label(df: pd.DataFrame, path: Path) -> pd.DataFrame:
    """Apply the same group4 construction logic used by the Stata do-file."""
    result = df.copy()
    result["treat"] = to_numeric(result["treat"], "treat", path).fillna(0).astype("int8")
    result["share_atc3"] = to_numeric(result["share_atc3"], "share_atc3", path).fillna(0).astype("int8")

    for col in CONFOUNDING_COLS:
        result[col] = to_numeric(result[col], col, path)

    result["D_confounded"] = result.loc[:, CONFOUNDING_COLS].max(axis=1, skipna=True)
    result["D_confounded"] = result["D_confounded"].fillna(0).astype("int8")
    result.loc[result["treat"].eq(0), "D_confounded"] = 0

    result["id_group"] = "unclassified"
    result.loc[result["treat"].eq(0), "id_group"] = "control"
    result.loc[
        result["treat"].eq(1) & result["share_atc3"].eq(0) & result["D_confounded"].eq(0),
        "id_group",
    ] = "treat_clean_nonshare"
    result.loc[
        result["treat"].eq(1) & result["share_atc3"].eq(1) & result["D_confounded"].eq(0),
        "id_group",
    ] = "treat_clean_share"
    result.loc[
        result["treat"].eq(1) & result["share_atc3"].eq(0) & result["D_confounded"].eq(1),
        "id_group",
    ] = "treat_confounded_nonshare"
    result.loc[
        result["treat"].eq(1) & result["share_atc3"].eq(1) & result["D_confounded"].eq(1),
        "id_group",
    ] = "treat_confounded_share"
    return result


def count_unique_ids(df: pd.DataFrame) -> dict[str, int]:
    """Count unique ids by group after checking id-level group consistency."""
    id_groups = df.loc[:, [*ID_COLS, "id_group"]].drop_duplicates()
    groups_per_id = id_groups.groupby(list(ID_COLS), dropna=False)["id_group"].nunique()
    inconsistent_ids = int(groups_per_id.gt(1).sum())

    if inconsistent_ids:
        consistent_keys = groups_per_id[groups_per_id.eq(1)].index
        consistent = (
            id_groups.set_index(list(ID_COLS))
            .loc[consistent_keys]
            .reset_index()
        )
    else:
        consistent = id_groups

    counts = {label: 0 for label in GROUP_LABELS}
    observed_counts = consistent["id_group"].value_counts().to_dict()
    for label in GROUP_LABELS:
        counts[label] = int(observed_counts.get(label, 0))

    counts["unique_id_total"] = int(groups_per_id.shape[0])
    counts["unique_id_counted"] = int(sum(counts[label] for label in GROUP_LABELS))
    counts["inconsistent_id_count"] = inconsistent_ids
    counts["unclassified_id_count"] = int(observed_counts.get("unclassified", 0))
    return counts


def empty_counts() -> dict[str, int]:
    """Return zero-filled count fields for missing or empty specs."""
    counts = {label: 0 for label in GROUP_LABELS}
    counts.update(
        {
            "unique_id_total": 0,
            "unique_id_counted": 0,
            "inconsistent_id_count": 0,
            "unclassified_id_count": 0,
        }
    )
    return counts


def summarize_spec(spec: CohortSpec, use_event_window: bool) -> dict[str, object]:
    """Summarize one cohort CSV into a single output row."""
    base_row: dict[str, object] = {
        "definition": spec.definition,
        "event_type": spec.event_type,
        "control_set": spec.control_set,
        "treatment_group": spec.treatment_group,
        "data_cohort": spec.cohort_year,
        "input_path": spec.input_path.as_posix(),
        "event_window_applied": int(use_event_window),
        "event_window_min": -8 if use_event_window else pd.NA,
        "event_window_max": 11 if use_event_window else pd.NA,
        "status": "ok",
        "error_message": "",
        "rows_raw": 0,
        "rows_after_event_window": 0,
    }

    if not spec.input_path.exists():
        return {**base_row, "status": "missing_file", **empty_counts()}

    try:
        df = read_required_columns(spec.input_path)
        base_row["rows_raw"] = int(len(df))
        df["data_cohort"] = spec.cohort_year
        df["event_time"] = to_numeric(df["event_time"], "event_time", spec.input_path)

        if use_event_window:
            df = df.loc[df["event_time"].between(-8, 11)].copy()
        base_row["rows_after_event_window"] = int(len(df))

        if df.empty:
            return {**base_row, "status": "no_rows_after_event_window", **empty_counts()}

        grouped = add_group_label(df, spec.input_path)
        return {**base_row, **count_unique_ids(grouped)}
    except (KeyError, ValueError, pd.errors.ParserError, OSError) as exc:
        return {
            **base_row,
            "status": "error",
            "error_message": str(exc),
            **empty_counts(),
        }


def main() -> None:
    """Run the requested summaries and write one CSV."""
    args = parse_args()
    use_event_window = not args.no_event_window
    specs = list(iter_specs(args))

    rows = []
    for index, spec in enumerate(specs, start=1):
        print(
            f"[{index}/{len(specs)}] "
            f"{spec.definition} {spec.event_type} {spec.control_set} "
            f"tg={spec.treatment_group} cohort={spec.cohort_year}"
        )
        rows.append(summarize_spec(spec, use_event_window))

    result = pd.DataFrame(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(args.output, index=False)

    status_counts = result["status"].value_counts().to_dict()
    print(f"Saved {args.output}")
    print(f"Rows: {len(result):,}")
    print(f"Status counts: {status_counts}")


if __name__ == "__main__":
    main()
