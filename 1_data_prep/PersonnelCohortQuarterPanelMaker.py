"""
Build firm-pair-product-quarter panels from personnel cohort pair-year panels.

For one personnel definition, event type, control set, and treatment group, this
script reads cohort files for 2009-2017, expands each pair-year row to four
quarters, attaches SSR product outcomes for the selected outcome firm, adds
ATC3 product-sharing indicators, merges directed pairwise kappa, and marks
balanced A-B panels.

Example:
    python 1_data_prep/PersonnelCohortQuarterPanelMaker.py
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Sequence

import pandas as pd


CURRENT_PATH = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_PATH.parent.parent

PERSONNEL_PANEL_ROOT = PROJECT_ROOT / "data" / "personnel_panels"
OUTPUT_ROOT = PROJECT_ROOT / "data" / "personnel_regression_panels"
SSR_OUTCOME_PATH = PROJECT_ROOT / "InterimData" / "boardex_ssr_price_sample.csv"
ATC3_MAPPING_PATH = PROJECT_ROOT / "data" / "atc3mapping" / "atc3mapping_year.csv"
KAPPA_PATH = PROJECT_ROOT / "InterimData" / "ssr_kappa_pairwise_v5.csv"

DEFINITIONS = ("narrow_board", "medium_board_csuite", "broad_board_c_vp")
EVENT_TYPES = ("to_B_still_in_A", "to_B_not_in_A", "dissolution")
CONTROL_SETS = ("C1A", "C1B", "C4", "C6A", "C6B")
TREATMENT_GROUPS = ("A", "B")
COHORT_YEARS = tuple(range(2009, 2018))

ZEROED_ANNUAL_COLS = (
    "total_moves",
    "retain",
    "exit",
    "dissolution",
    "stay_3_years",
)
COHORT_REQUIRED_COLS = ("A", "B", "year", "treat", "event_time", *ZEROED_ANNUAL_COLS)

SSR_COLS = (
    "year",
    "quarter",
    "BoardName",
    "product",
    "atc3",
    "revenue",
    "quantity",
    "price1",
    "price0",
)
SSR_KEY_COLS = ("year", "quarter", "BoardName", "product", "atc3")

ATC3_MAPPING_COLS = ("year", "product", "atc3", "BoardName", "BoardNamePair")
KAPPA_COLS = ("rdate", "firm_j", "firm_k", "kappa")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Build product-quarter personnel regression panels.",
    )
    parser.add_argument("--definition", choices=DEFINITIONS)
    parser.add_argument(
        "--event-type",
        "--event_type",
        dest="event_type",
        choices=EVENT_TYPES,
    )
    parser.add_argument(
        "--control-set",
        "--control_set",
        dest="control_set",
        choices=CONTROL_SETS,
    )
    parser.add_argument(
        "--treatment-group",
        "--treatment_group",
        dest="treatment_group",
        type=str.upper,
        choices=TREATMENT_GROUPS,
    )
    return parser.parse_args()


def require_path(path: Path, source_name: str) -> None:
    """Raise a clear error if an input path does not exist."""
    if not path.exists():
        raise FileNotFoundError(f"{source_name} not found: {path}")


def read_csv_with_required_columns(
    path: Path,
    required_cols: Sequence[str],
    source_name: str,
) -> pd.DataFrame:
    """Read required CSV columns after validating the header."""
    require_path(path, source_name)
    header = pd.read_csv(path, nrows=0)
    missing_cols = sorted(set(required_cols) - set(header.columns))
    if missing_cols:
        raise KeyError(f"{source_name} is missing required columns: {missing_cols}")
    return pd.read_csv(path, usecols=list(required_cols))


def require_columns(
    df: pd.DataFrame,
    required_cols: Sequence[str],
    source_name: str,
) -> None:
    """Validate columns for a DataFrame that has already been loaded."""
    missing_cols = sorted(set(required_cols) - set(df.columns))
    if missing_cols:
        raise KeyError(f"{source_name} is missing required columns: {missing_cols}")


def clean_string_keys(
    df: pd.DataFrame,
    cols: Sequence[str],
    uppercase: bool = False,
) -> pd.DataFrame:
    """Strip string key columns, optionally uppercase them, and blank empty strings."""
    result = df.copy()
    for col in cols:
        result[col] = result[col].astype("string").str.strip()
        if uppercase:
            result[col] = result[col].str.upper()
        result.loc[result[col].eq(""), col] = pd.NA
    return result


def convert_required_int(df: pd.DataFrame, col: str, source_name: str) -> pd.Series:
    """Convert a required numeric column to integer values."""
    converted = pd.to_numeric(df[col], errors="coerce")
    missing_or_invalid = converted.isna()
    if missing_or_invalid.any():
        examples = df.loc[missing_or_invalid, [col]].head(10)
        raise ValueError(
            f"{source_name}.{col} contains missing or nonnumeric values. "
            f"Examples:\n{examples}"
        )
    non_integer = converted.ne(converted.round())
    if non_integer.any():
        examples = df.loc[non_integer, [col]].head(10)
        raise ValueError(
            f"{source_name}.{col} must contain integer values. Examples:\n{examples}"
        )
    return converted.astype("int32")


def convert_optional_numeric(
    df: pd.DataFrame,
    cols: Sequence[str],
    source_name: str,
) -> pd.DataFrame:
    """Convert outcome or kappa columns to numeric while allowing missing values."""
    result = df.copy()
    for col in cols:
        converted = pd.to_numeric(result[col], errors="coerce")
        invalid = converted.isna() & result[col].notna()
        if invalid.any():
            examples = result.loc[invalid, [col]].head(10)
            raise ValueError(
                f"{source_name}.{col} contains nonnumeric values. Examples:\n{examples}"
            )
        result[col] = converted
    return result


def sum_with_missing(series: pd.Series) -> float:
    """Sum a numeric series without turning all-missing groups into zero."""
    return series.sum(min_count=1)


def load_ssr_outcomes() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load SSR outcomes and aggregate to firm-product-quarter level."""
    ssr = read_csv_with_required_columns(
        SSR_OUTCOME_PATH,
        SSR_COLS,
        SSR_OUTCOME_PATH.name,
    )
    ssr = clean_string_keys(ssr, ["product", "atc3"])
    ssr = clean_string_keys(ssr, ["BoardName"], uppercase=True)
    ssr["year"] = convert_required_int(ssr, "year", SSR_OUTCOME_PATH.name)
    ssr["quarter"] = convert_required_int(ssr, "quarter", SSR_OUTCOME_PATH.name)
    bad_quarter = ~ssr["quarter"].between(1, 4)
    if bad_quarter.any():
        examples = ssr.loc[bad_quarter, ["year", "quarter", "BoardName"]].head(10)
        raise ValueError(
            f"{SSR_OUTCOME_PATH.name} has quarters outside 1-4. Examples:\n{examples}"
        )

    ssr = convert_optional_numeric(
        ssr,
        ["revenue", "quantity", "price1", "price0"],
        SSR_OUTCOME_PATH.name,
    )
    ssr = ssr.dropna(subset=["BoardName"])

    outcomes = (
        ssr.groupby(list(SSR_KEY_COLS), as_index=False, dropna=False)
        .agg(
            revenue=("revenue", sum_with_missing),
            quantity=("quantity", sum_with_missing),
            price1=("price1", "mean"),
            price0=("price0", "mean"),
        )
        .sort_values(["BoardName", "year", "quarter", "product", "atc3"])
        .reset_index(drop=True)
    )
    require_unique_keys(outcomes, SSR_KEY_COLS, "aggregated SSR outcomes")

    presence = (
        outcomes[["BoardName", "year", "quarter"]]
        .drop_duplicates()
        .reset_index(drop=True)
    )
    return outcomes, presence


def load_atc3_mapping() -> pd.DataFrame:
    """Load year-level ATC3 mapping rows used to create share_atc3."""
    mapping = read_csv_with_required_columns(
        ATC3_MAPPING_PATH,
        ATC3_MAPPING_COLS,
        ATC3_MAPPING_PATH.name,
    )
    mapping = clean_string_keys(mapping, ["product", "atc3"])
    mapping = clean_string_keys(mapping, ["BoardName", "BoardNamePair"], uppercase=True)
    mapping["year"] = convert_required_int(mapping, "year", ATC3_MAPPING_PATH.name)
    return mapping


def load_kappa() -> pd.DataFrame:
    """Load directed pairwise kappa and derive year-quarter merge keys."""
    kappa = read_csv_with_required_columns(KAPPA_PATH, KAPPA_COLS, KAPPA_PATH.name)
    kappa = clean_string_keys(kappa, ["rdate"])
    kappa = clean_string_keys(kappa, ["firm_j", "firm_k"], uppercase=True)
    kappa = convert_optional_numeric(kappa, ["kappa"], KAPPA_PATH.name)

    parsed_rdate = pd.to_datetime(kappa["rdate"], format="%Y%m%d", errors="coerce")
    invalid_dates = parsed_rdate.isna()
    if invalid_dates.any():
        examples = kappa.loc[invalid_dates, ["rdate", "firm_j", "firm_k"]].head(10)
        raise ValueError(f"{KAPPA_PATH.name} has invalid YYYYMMDD rdate values. Examples:\n{examples}")

    kappa["year"] = parsed_rdate.dt.year.astype("int32")
    kappa["quarter"] = parsed_rdate.dt.quarter.astype("int32")
    result = kappa[["year", "quarter", "firm_j", "firm_k", "kappa"]].copy()
    result = result.dropna(subset=["firm_j", "firm_k"])
    require_unique_keys(result, ["year", "quarter", "firm_j", "firm_k"], "directed kappa")
    return result


def require_unique_keys(df: pd.DataFrame, key_cols: Sequence[str], source_name: str) -> None:
    """Reject duplicate keys that would expand panel rows unexpectedly."""
    duplicate_mask = df.duplicated(list(key_cols), keep=False)
    if duplicate_mask.any():
        examples = df.loc[duplicate_mask, list(key_cols)].sort_values(list(key_cols)).head(20)
        raise ValueError(
            f"{source_name} is not unique by {list(key_cols)}. "
            f"Duplicate rows: {int(duplicate_mask.sum())}. Examples:\n{examples}"
        )


def input_dir(definition: str, event_type: str, control_set: str) -> Path:
    """Return the input cohort directory for one parameter combination."""
    return (
        PERSONNEL_PANEL_ROOT
        / definition
        / "cohort_panels"
        / "retain3yr"
        / event_type
        / control_set
    )


def output_dir(definition: str, event_type: str, control_set: str, treatment_group: str) -> Path:
    """Return the output directory for one parameter combination."""
    return (
        OUTPUT_ROOT
        / definition
        / "retain3yr"
        / event_type
        / control_set
        / f"treatment_group_{treatment_group}"
    )


def cohort_year_from_filename(path: Path) -> int:
    """Parse the cohort year from a reg_panel_cohort_YYYY.csv file name."""
    match = re.fullmatch(r"reg_panel_cohort_(\d{4})\.csv", path.name)
    if not match:
        raise ValueError(f"Cannot parse cohort year from file name: {path.name}")
    return int(match.group(1))


def outcome_and_counterparty_cols(treatment_group: str) -> tuple[str, str]:
    """Return the outcome firm column and counterparty column."""
    if treatment_group == "A":
        return "A", "B"
    if treatment_group == "B":
        return "B", "A"
    raise ValueError("treatment_group must be either A or B")


def load_cohort(path: Path, cohort_year: int) -> pd.DataFrame:
    """Load a cohort pair-year panel and validate the columns used downstream."""
    require_path(path, "cohort file")
    cohort = pd.read_csv(path)
    require_columns(cohort, COHORT_REQUIRED_COLS, path.name)
    cohort = clean_string_keys(cohort, ["A", "B"])

    cohort["year"] = convert_required_int(cohort, "year", path.name)
    cohort["event_time"] = convert_required_int(cohort, "event_time", path.name)
    cohort["treat"] = convert_required_int(cohort, "treat", path.name)
    bad_treat = ~cohort["treat"].isin([0, 1])
    if bad_treat.any():
        examples = cohort.loc[bad_treat, ["A", "B", "year", "treat"]].head(10)
        raise ValueError(f"{path.name}.treat must be 0 or 1. Examples:\n{examples}")

    if "cohort_year" in cohort.columns:
        cohort["cohort_year"] = convert_required_int(cohort, "cohort_year", path.name)
        bad_cohort = cohort["cohort_year"].ne(cohort_year)
        if bad_cohort.any():
            examples = cohort.loc[bad_cohort, ["A", "B", "year", "cohort_year"]].head(10)
            raise ValueError(
                f"{path.name} has cohort_year values that do not match {cohort_year}. "
                f"Examples:\n{examples}"
            )

    return cohort


def expand_to_quarters(cohort: pd.DataFrame) -> pd.DataFrame:
    """Expand each pair-year row to quarters and recode annual event values."""
    expanded = cohort.loc[cohort.index.repeat(4)].reset_index(drop=True)
    quarters = [quarter for _ in range(len(cohort)) for quarter in range(1, 5)]

    year_position = list(expanded.columns).index("year") + 1
    expanded.insert(year_position, "quarter", quarters)

    non_first_quarter = expanded["quarter"].ne(1)
    expanded.loc[non_first_quarter, list(ZEROED_ANNUAL_COLS)] = 0

    annual_event_time = pd.to_numeric(expanded["event_time"], errors="raise")
    expanded["event_time"] = (annual_event_time * 4 + expanded["quarter"] - 1).astype("int32")
    expanded["quarter"] = expanded["quarter"].astype("int32")
    return expanded


def merge_ssr_outcomes(
    expanded: pd.DataFrame,
    ssr_outcomes: pd.DataFrame,
    treatment_group: str,
) -> pd.DataFrame:
    """Attach product-quarter SSR outcome rows for the selected outcome firm."""
    outcome_col, _ = outcome_and_counterparty_cols(treatment_group)
    merged = expanded.merge(
        ssr_outcomes,
        left_on=[outcome_col, "year", "quarter"],
        right_on=["BoardName", "year", "quarter"],
        how="inner",
    )
    return merged


def add_share_atc3(
    panel: pd.DataFrame,
    mapping: pd.DataFrame,
    cohort_year: int,
    treatment_group: str,
) -> pd.DataFrame:
    """Add share_atc3 according to cohort-year directional ATC3 mapping."""
    result = panel.copy()
    outcome_col, counterparty_col = outcome_and_counterparty_cols(treatment_group)
    map_keys = (
        mapping.loc[mapping["year"].eq(cohort_year), ["BoardName", "BoardNamePair", "product"]]
        .dropna(subset=["BoardName", "BoardNamePair", "product"])
        .drop_duplicates()
        .assign(_share_atc3_match=1)
    )
    require_unique_keys(map_keys, ["BoardName", "BoardNamePair", "product"], f"ATC3 mapping for {cohort_year}")

    left_keys = result[[outcome_col, counterparty_col, "product"]].rename(
        columns={outcome_col: "BoardName", counterparty_col: "BoardNamePair"}
    )
    matches = left_keys.merge(
        map_keys,
        on=["BoardName", "BoardNamePair", "product"],
        how="left",
        validate="many_to_one",
    )["_share_atc3_match"].fillna(0)

    treated = result["treat"].eq(1)
    result["share_atc3"] = (treated & matches.eq(1)).astype("int8")
    validate_share_atc3_consistency(result)
    return result


def validate_share_atc3_consistency(panel: pd.DataFrame) -> None:
    """Ensure share_atc3 is constant within each A-B-product in a cohort file."""
    if panel.empty:
        return
    distinct_counts = panel.groupby(["A", "B", "product"], dropna=False)["share_atc3"].nunique()
    inconsistent = distinct_counts[distinct_counts.gt(1)]
    if not inconsistent.empty:
        examples = inconsistent.reset_index().head(20)
        raise ValueError(
            "share_atc3 is not constant within A-B-product groups. "
            f"Examples:\n{examples}"
        )


def add_kappa(panel: pd.DataFrame, kappa: pd.DataFrame, treatment_group: str) -> pd.DataFrame:
    """Merge directed kappa for the selected outcome-firm direction."""
    outcome_col, counterparty_col = outcome_and_counterparty_cols(treatment_group)
    kappa_for_merge = kappa.rename(columns={"firm_j": outcome_col, "firm_k": counterparty_col})
    result = panel.merge(
        kappa_for_merge,
        on=["year", "quarter", outcome_col, counterparty_col],
        how="left",
        validate="many_to_one",
    )
    return result


def add_balanced_panel(panel: pd.DataFrame, ssr_presence: pd.DataFrame, cohort_year: int) -> pd.DataFrame:
    """Mark whether each A-B pair has full SSR presence for both firms in the five-year window."""
    result = panel.copy()
    if result.empty:
        result["balanced_panel"] = pd.Series(dtype="int8")
        return result

    pairs = result[["A", "B"]].drop_duplicates().reset_index(drop=True)
    needed_firms = set(pairs["A"]).union(set(pairs["B"]))
    window_years = set(range(cohort_year - 2, cohort_year + 3))
    required_quarter_count = len(window_years) * 4

    window_presence = ssr_presence[
        ssr_presence["year"].isin(window_years)
        & ssr_presence["quarter"].between(1, 4)
        & ssr_presence["BoardName"].isin(needed_firms)
    ].drop_duplicates(["BoardName", "year", "quarter"])

    firm_counts = window_presence.groupby("BoardName").size()
    balanced_firms = set(firm_counts[firm_counts.eq(required_quarter_count)].index)

    pairs["balanced_panel"] = (
        pairs["A"].isin(balanced_firms) & pairs["B"].isin(balanced_firms)
    ).astype("int8")
    result = result.merge(pairs, on=["A", "B"], how="left", validate="many_to_one")
    result["balanced_panel"] = result["balanced_panel"].fillna(0).astype("int8")
    return result


def process_cohort_file(
    cohort_path: Path,
    ssr_outcomes: pd.DataFrame,
    ssr_presence: pd.DataFrame,
    mapping: pd.DataFrame,
    kappa: pd.DataFrame,
    treatment_group: str,
) -> pd.DataFrame:
    """Build one cohort-year product-quarter panel."""
    cohort_year = cohort_year_from_filename(cohort_path)
    cohort = load_cohort(cohort_path, cohort_year)
    expanded = expand_to_quarters(cohort)
    panel = merge_ssr_outcomes(expanded, ssr_outcomes, treatment_group)
    panel = add_share_atc3(panel, mapping, cohort_year, treatment_group)
    panel = add_kappa(panel, kappa, treatment_group)
    panel = add_balanced_panel(panel, ssr_presence, cohort_year)
    return panel


def run(definition: str, event_type: str, control_set: str, treatment_group: str) -> None:
    """Run all 2009-2017 cohort files for one parameter combination."""
    in_dir = input_dir(definition, event_type, control_set)
    out_dir = output_dir(definition, event_type, control_set, treatment_group)
    require_path(in_dir, "input cohort directory")
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Loading shared inputs...")
    ssr_outcomes, ssr_presence = load_ssr_outcomes()
    mapping = load_atc3_mapping()
    kappa = load_kappa()

    print(f"Input directory: {in_dir}")
    print(f"Output directory: {out_dir}")

    for cohort_year in COHORT_YEARS:
        cohort_path = in_dir / f"reg_panel_cohort_{cohort_year}.csv"
        require_path(cohort_path, "cohort file")
        panel = process_cohort_file(
            cohort_path=cohort_path,
            ssr_outcomes=ssr_outcomes,
            ssr_presence=ssr_presence,
            mapping=mapping,
            kappa=kappa,
            treatment_group=treatment_group,
        )
        out_path = out_dir / f"reg_panel_cohort_{cohort_year}_tg{treatment_group}.csv"
        panel.to_csv(out_path, index=False)
        n_pairs = panel[["A", "B"]].drop_duplicates().shape[0] if not panel.empty else 0
        print(f"Saved {out_path} | rows={len(panel):,} | pairs={n_pairs:,}")


def main() -> None:
    """CLI entry point."""
    args = parse_args()
    definitions = [args.definition] if args.definition else list(DEFINITIONS)
    event_types = [args.event_type] if args.event_type else list(EVENT_TYPES)
    control_sets = [args.control_set] if args.control_set else list(CONTROL_SETS)
    treatment_groups = (
        [args.treatment_group] if args.treatment_group else list(TREATMENT_GROUPS)
    )

    total = (
        len(definitions)
        * len(event_types)
        * len(control_sets)
        * len(treatment_groups)
    )
    current = 0
    for definition in definitions:
        for event_type in event_types:
            for control_set in control_sets:
                for treatment_group in treatment_groups:
                    current += 1
                    print(
                        f"\n=== Combination {current}/{total}: "
                        f"definition={definition}, event_type={event_type}, "
                        f"control_set={control_set}, treatment_group={treatment_group} ==="
                    )
                    run(
                        definition=definition,
                        event_type=event_type,
                        control_set=control_set,
                        treatment_group=treatment_group,
                    )


if __name__ == "__main__":
    main()
