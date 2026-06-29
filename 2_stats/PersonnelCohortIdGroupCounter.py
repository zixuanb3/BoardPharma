"""
Purpose:
Count unique personnel regression-panel ids by treatment subgroup, matching
the personnel did_imputation id definition: group(A B product data_cohort).

Process:
1. Read personnel product-quarter cohort panels for each selected definition,
   event type, control set, treatment group, and cohort year 2009-2017.
2. Optionally keep only the configured regression event window.
3. Classify ids into control, treated clean/nonshare, treated clean/share,
   treated confounded/nonshare, and treated confounded/share groups.
4. Count unique ids after checking id-level group consistency.
5. For formulary panels, report both ATC2 and ATC3 share/nonshare splits.
6. Count unique formulary / treated-firm-drug exposure units by cohort.
7. Save one summary CSV with counts and file status by cohort specification.

Input:
- data/personnel_regression_panels/{definition}/retain{retain_years}yr/{event_type}/{control_set}/treatment_group_{A|B}/reg_panel_cohort_YYYY_tg{A|B}.csv
- data/personnel_regression_panels/formulary/{definition}/retain{retain_years}yr/{event_type}/{control_set}/treatment_group_{A|B}/reg_panel_cohort_YYYY_tg{A|B}.csv when FORMULARY == 1

Output:
- csv/personnel_cohort_id_counts/personnel_cohort_id_group_counts.csv
- csv/personnel_cohort_id_counts/formulary/personnel_cohort_id_group_counts.csv when FORMULARY == 1
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

DEFINITIONS = ("narrow_board", "medium_board_csuite",)
# medium_board_csuite broad_board_c_vp
EVENT_TYPES = ("to_B_still_in_A", "to_B_not_in_A", "dissolution")
CONTROL_SETS = ("C4",)
TREATMENT_GROUPS = ("A", "B")
COHORT_YEARS = tuple(range(2020, 2023))
# tuple(range(2020, 2023)) tuple(range(2009, 2019))
RETAIN_YEARS = 2
FORMULARY = 1
EVENT_WINDOW_MIN = -4
EVENT_WINDOW_MAX = 7

ID_COLS = ("A", "B", "product", "data_cohort")
FORMULARY_ID_COL = "FORMULARY_ID"
DRUG_COL = "NDC"
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
SHARE_COL_BY_LEVEL = {
    "atc2": "share_atc2",
    "atc3": "share_atc3",
}
DEFAULT_SHARE_LEVELS = ("atc3",)
FORMULARY_SHARE_LEVELS = ("atc2", "atc3")

GROUP_LABELS = (
    "control",
    "treat_clean_nonshare",
    "treat_clean_share",
    "treat_confounded_nonshare",
    "treat_confounded_share",
)
SHARE_DEPENDENT_LABELS = (
    "treat_clean_nonshare",
    "treat_clean_share",
    "treat_confounded_nonshare",
    "treat_confounded_share",
)
ID_TOTAL_FIELDS = (
    "unique_id_total",
    "unique_id_counted",
    "inconsistent_id_count",
    "unclassified_id_count",
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
    input_root: Path
    formulary: int

    @property
    def input_path(self) -> Path:
        """Return the generated personnel regression-panel CSV path."""
        return (
            self.input_root
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
        help="Count all cohort-file rows instead of matching the event-time window.",
    )
    parser.add_argument(
        "--event-window-min",
        "--event_window_min",
        dest="event_window_min",
        type=int,
        default=EVENT_WINDOW_MIN,
        help=f"Minimum event_time kept when the event window is applied. Default: {EVENT_WINDOW_MIN}.",
    )
    parser.add_argument(
        "--event-window-max",
        "--event_window_max",
        dest="event_window_max",
        type=int,
        default=EVENT_WINDOW_MAX,
        help=f"Maximum event_time kept when the event window is applied. Default: {EVENT_WINDOW_MAX}.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=f"Output CSV path. Default: {DEFAULT_OUTPUT_PATH}",
    )
    parser.add_argument(
        "--formulary",
        dest="formulary",
        type=int,
        choices=(0, 1),
        default=FORMULARY,
        help="Use formulary personnel regression panels when set to 1.",
    )
    return parser.parse_args()


def validate_formulary(formulary: int) -> int:
    """Validate and return the formulary sample flag."""
    if formulary not in {0, 1}:
        raise ValueError("formulary must be 0 or 1")
    return formulary


def validate_event_window(window_min: int, window_max: int) -> tuple[int, int]:
    """Validate and return event-window bounds."""
    if window_min > window_max:
        raise ValueError("event_window_min must be <= event_window_max")
    return window_min, window_max


def input_root(formulary: int) -> Path:
    """Return the personnel regression-panel root for the configured sample."""
    return INPUT_ROOT / "formulary" if formulary == 1 else INPUT_ROOT


def default_output_path(formulary: int) -> Path:
    """Return the summary output path for the configured sample."""
    if formulary == 1:
        return DEFAULT_OUTPUT_PATH.parent / "formulary" / DEFAULT_OUTPUT_PATH.name
    return DEFAULT_OUTPUT_PATH


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
    regression_panel_root = input_root(validate_formulary(args.formulary))

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
                            input_root=regression_panel_root,
                            formulary=validate_formulary(args.formulary),
                        )


def id_columns(formulary: int) -> tuple[str, ...]:
    """Return the unique-id columns for the configured sample."""
    if validate_formulary(formulary) == 1:
        return (*ID_COLS, FORMULARY_ID_COL)
    return ID_COLS


def required_columns(formulary: int) -> tuple[str, ...]:
    """Return columns needed for subgroup id counts."""
    if validate_formulary(formulary) == 1:
        return (*REQUIRED_COLS, SHARE_COL_BY_LEVEL["atc2"], FORMULARY_ID_COL, DRUG_COL)
    return REQUIRED_COLS


def share_levels(formulary: int) -> tuple[str, ...]:
    """Return share definitions to summarize for the configured sample."""
    if validate_formulary(formulary) == 1:
        return FORMULARY_SHARE_LEVELS
    return DEFAULT_SHARE_LEVELS


def read_required_columns(path: Path, formulary: int) -> pd.DataFrame:
    """Read only columns needed for subgroup id counts."""
    required_cols = required_columns(formulary)
    header = pd.read_csv(path, nrows=0)
    missing_cols = sorted(set(required_cols) - set(header.columns))
    if missing_cols:
        raise KeyError(f"{path} is missing required columns: {missing_cols}")
    return pd.read_csv(path, usecols=list(required_cols))


def to_numeric(series: pd.Series, column_name: str, path: Path) -> pd.Series:
    """Convert a required column to numeric with a clear error message."""
    converted = pd.to_numeric(series, errors="coerce")
    bad_values = series.notna() & converted.isna()
    if bad_values.any():
        examples = series.loc[bad_values].drop_duplicates().head(10).tolist()
        raise ValueError(f"{path.name}.{column_name} has nonnumeric values: {examples}")
    return converted


def add_group_label(df: pd.DataFrame, path: Path, share_col: str) -> pd.DataFrame:
    """Apply the same group4 construction logic for one share definition."""
    result = df.copy()
    result["treat"] = to_numeric(result["treat"], "treat", path).fillna(0).astype("int8")
    result[share_col] = to_numeric(result[share_col], share_col, path).fillna(0).astype("int8")

    for col in CONFOUNDING_COLS:
        result[col] = to_numeric(result[col], col, path)

    result["D_confounded"] = result.loc[:, CONFOUNDING_COLS].max(axis=1, skipna=True)
    result["D_confounded"] = result["D_confounded"].fillna(0).astype("int8")
    result.loc[result["treat"].eq(0), "D_confounded"] = 0

    result["id_group"] = "unclassified"
    result.loc[result["treat"].eq(0), "id_group"] = "control"
    result.loc[
        result["treat"].eq(1) & result[share_col].eq(0) & result["D_confounded"].eq(0),
        "id_group",
    ] = "treat_clean_nonshare"
    result.loc[
        result["treat"].eq(1) & result[share_col].eq(1) & result["D_confounded"].eq(0),
        "id_group",
    ] = "treat_clean_share"
    result.loc[
        result["treat"].eq(1) & result[share_col].eq(0) & result["D_confounded"].eq(1),
        "id_group",
    ] = "treat_confounded_nonshare"
    result.loc[
        result["treat"].eq(1) & result[share_col].eq(1) & result["D_confounded"].eq(1),
        "id_group",
    ] = "treat_confounded_share"
    return result


def add_base_group_label(df: pd.DataFrame, path: Path) -> pd.DataFrame:
    """Classify ids by treatment and confounding only, without ATC share splits."""
    result = df.copy()
    result["treat"] = to_numeric(result["treat"], "treat", path).fillna(0).astype("int8")

    for col in CONFOUNDING_COLS:
        result[col] = to_numeric(result[col], col, path)

    result["D_confounded"] = result.loc[:, CONFOUNDING_COLS].max(axis=1, skipna=True)
    result["D_confounded"] = result["D_confounded"].fillna(0).astype("int8")
    result.loc[result["treat"].eq(0), "D_confounded"] = 0

    result["base_id_group"] = "unclassified"
    result.loc[result["treat"].eq(0), "base_id_group"] = "control"
    result.loc[
        result["treat"].eq(1) & result["D_confounded"].eq(0),
        "base_id_group",
    ] = "treat_clean"
    result.loc[
        result["treat"].eq(1) & result["D_confounded"].eq(1),
        "base_id_group",
    ] = "treat_confounded"
    return result


def count_unique_ids(df: pd.DataFrame, id_cols: Sequence[str]) -> dict[str, int]:
    """Count unique ids by group after checking id-level group consistency."""
    id_groups = df.loc[:, [*id_cols, "id_group"]].drop_duplicates()
    groups_per_id = id_groups.groupby(list(id_cols), dropna=False)["id_group"].nunique()
    inconsistent_ids = int(groups_per_id.gt(1).sum())

    if inconsistent_ids:
        consistent_keys = groups_per_id[groups_per_id.eq(1)].index
        consistent = (
            id_groups.set_index(list(id_cols))
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


def count_base_unique_ids(df: pd.DataFrame, id_cols: Sequence[str]) -> dict[str, int]:
    """Count ATC-invariant id fields from treatment and confounding status only."""
    id_groups = df.loc[:, [*id_cols, "base_id_group"]].drop_duplicates()
    groups_per_id = id_groups.groupby(list(id_cols), dropna=False)["base_id_group"].nunique()
    inconsistent_ids = int(groups_per_id.gt(1).sum())

    if inconsistent_ids:
        consistent_keys = groups_per_id[groups_per_id.eq(1)].index
        consistent = (
            id_groups.set_index(list(id_cols))
            .loc[consistent_keys]
            .reset_index()
        )
    else:
        consistent = id_groups

    observed_counts = consistent["base_id_group"].value_counts().to_dict()
    control = int(observed_counts.get("control", 0))
    treat_clean = int(observed_counts.get("treat_clean", 0))
    treat_confounded = int(observed_counts.get("treat_confounded", 0))
    return {
        "control": control,
        "unique_id_total": int(groups_per_id.shape[0]),
        "unique_id_counted": control + treat_clean + treat_confounded,
        "inconsistent_id_count": inconsistent_ids,
        "unclassified_id_count": int(observed_counts.get("unclassified", 0)),
    }


def prefix_share_counts(counts: dict[str, int], share_level: str) -> dict[str, int]:
    """Prefix only share-dependent treated count columns with their ATC definition."""
    return {
        f"{share_level}_{key}": int(counts.get(key, 0))
        for key in SHARE_DEPENDENT_LABELS
    }


def combine_id_counts_by_share(
    df: pd.DataFrame,
    path: Path,
    id_cols: Sequence[str],
    formulary: int,
) -> dict[str, int]:
    """Count ids once, splitting only treated share/nonshare cells by ATC level."""
    levels = share_levels(formulary)
    counts_by_level: dict[str, dict[str, int]] = {}
    for share_level in levels:
        share_col = SHARE_COL_BY_LEVEL[share_level]
        grouped = add_group_label(df, path, share_col)
        counts_by_level[share_level] = count_unique_ids(grouped, id_cols)

    if validate_formulary(formulary) == 0:
        return counts_by_level[DEFAULT_SHARE_LEVELS[0]]

    base_counts = count_base_unique_ids(add_base_group_label(df, path), id_cols)
    combined: dict[str, int] = {
        "control": int(base_counts.get("control", 0)),
    }
    for share_level in FORMULARY_SHARE_LEVELS:
        combined.update(prefix_share_counts(counts_by_level[share_level], share_level))
    for field in ID_TOTAL_FIELDS:
        combined[field] = int(base_counts.get(field, 0))
    return combined


def count_unique_exposure_units(
    df: pd.DataFrame,
    treatment_group: str,
    formulary: int,
) -> dict[str, int]:
    """Count unique formulary and treated-firm-drug units in the filtered rows."""
    treatment_firm_col = treatment_group.upper()
    if treatment_firm_col not in {"A", "B"}:
        raise ValueError(f"treatment_group must be A or B, got: {treatment_group}")

    drug_col = DRUG_COL if DRUG_COL in df.columns else "product"
    treatment_drug_cols = [treatment_firm_col, drug_col]
    treatment_drug = df.dropna(subset=treatment_drug_cols)

    counts = {
        "unique_treatment_group_drug_count": int(
            treatment_drug.loc[:, treatment_drug_cols].drop_duplicates().shape[0]
        ),
        "unique_formulary_count": 0,
        "unique_formulary_treatment_group_drug_count": 0,
    }

    if validate_formulary(formulary) == 0:
        return counts

    formulary_drug_cols = [FORMULARY_ID_COL, treatment_firm_col, drug_col]
    formulary_rows = df.dropna(subset=[FORMULARY_ID_COL])
    formulary_treatment_drug = df.dropna(subset=formulary_drug_cols)
    counts["unique_formulary_count"] = int(
        formulary_rows.loc[:, [FORMULARY_ID_COL]].drop_duplicates().shape[0]
    )
    counts["unique_formulary_treatment_group_drug_count"] = int(
        formulary_treatment_drug.loc[:, formulary_drug_cols]
        .drop_duplicates()
        .shape[0]
    )
    return counts


def empty_id_counts(formulary: int) -> dict[str, int]:
    """Return zero-filled id count fields for the configured output layout."""
    if validate_formulary(formulary) == 0:
        counts = {label: 0 for label in GROUP_LABELS}
        counts.update({field: 0 for field in ID_TOTAL_FIELDS})
        return counts

    counts: dict[str, int] = {"control": 0}
    for share_level in FORMULARY_SHARE_LEVELS:
        counts.update({f"{share_level}_{label}": 0 for label in SHARE_DEPENDENT_LABELS})
    counts.update({field: 0 for field in ID_TOTAL_FIELDS})
    return counts


def empty_counts(formulary: int) -> dict[str, int]:
    """Return zero-filled count fields for missing or empty specs."""
    counts = empty_id_counts(formulary)
    counts.update(
        {
            "unique_formulary_count": 0,
            "unique_treatment_group_drug_count": 0,
            "unique_formulary_treatment_group_drug_count": 0,
        }
    )
    return counts


def summarize_spec(
    spec: CohortSpec,
    use_event_window: bool,
    event_window_min: int,
    event_window_max: int,
) -> dict[str, object]:
    """Summarize one cohort CSV into a single output row."""
    base_row: dict[str, object] = {
        "definition": spec.definition,
        "event_type": spec.event_type,
        "control_set": spec.control_set,
        "treatment_group": spec.treatment_group,
        "data_cohort": spec.cohort_year,
        "input_path": spec.input_path.as_posix(),
        "event_window_applied": int(use_event_window),
        "event_window_min": event_window_min if use_event_window else pd.NA,
        "event_window_max": event_window_max if use_event_window else pd.NA,
        "status": "ok",
        "error_message": "",
        "rows_raw": 0,
        "rows_after_event_window": 0,
    }

    if not spec.input_path.exists():
        return {**base_row, "status": "missing_file", **empty_counts(spec.formulary)}

    try:
        df = read_required_columns(spec.input_path, spec.formulary)
        base_row["rows_raw"] = int(len(df))
        df["data_cohort"] = spec.cohort_year
        df["event_time"] = to_numeric(df["event_time"], "event_time", spec.input_path)

        if use_event_window:
            df = df.loc[df["event_time"].between(event_window_min, event_window_max)].copy()
        base_row["rows_after_event_window"] = int(len(df))

        if df.empty:
            return {
                **base_row,
                "status": "no_rows_after_event_window",
                **empty_counts(spec.formulary),
            }

        id_counts = combine_id_counts_by_share(
            df,
            spec.input_path,
            id_columns(spec.formulary),
            spec.formulary,
        )
        return {
            **base_row,
            **id_counts,
            **count_unique_exposure_units(df, spec.treatment_group, spec.formulary),
        }
    except (KeyError, ValueError, pd.errors.ParserError, OSError) as exc:
        return {
            **base_row,
            "status": "error",
            "error_message": str(exc),
            **empty_counts(spec.formulary),
        }


def main() -> None:
    """Run the requested summaries and write one CSV."""
    args = parse_args()
    formulary = validate_formulary(args.formulary)
    event_window_min, event_window_max = validate_event_window(
        args.event_window_min,
        args.event_window_max,
    )
    output_path = args.output if args.output is not None else default_output_path(formulary)
    use_event_window = not args.no_event_window
    specs = list(iter_specs(args))

    rows = []
    for index, spec in enumerate(specs, start=1):
        print(
            f"[{index}/{len(specs)}] "
            f"{spec.definition} {spec.event_type} {spec.control_set} "
            f"tg={spec.treatment_group} cohort={spec.cohort_year}"
        )
        rows.append(
            summarize_spec(
                spec,
                use_event_window,
                event_window_min,
                event_window_max,
            )
        )

    result = pd.DataFrame(rows)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_path, index=False)

    status_counts = result["status"].value_counts().to_dict()
    print(f"Saved {output_path}")
    print(f"Rows: {len(result):,}")
    print(f"Status counts: {status_counts}")


if __name__ == "__main__":
    main()
