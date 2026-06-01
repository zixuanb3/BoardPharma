"""
Purpose:
Build firm-level SSR kappa data by appending raw pairwise kappa moments to the
existing normalized firm-level kappa file.

Process:
- Load the existing normalized firm-level file and the raw pairwise kappa file.
- Aggregate raw pairwise `kappa` to rdate + firm_j level.
- Enforce exact 1:1 correspondence between rdate + firm_j pairwise groups and
  rdate + firm firm-level rows.
- Validate that cusip and n_pairs are unchanged after the merge.
- Generate year and quarter from rdate for matching cohort panels.
- Export the firm-level file with added year, quarter, kappa_mean,
  kappa_median, and kappa_std.

Input:
- InterimData/ssr_kappa_pairwise_v4.csv
- InterimData/ssr_kappa_firm_level_v4.csv

Output:
- data/kappa/ssr_kappa_firm_level_v4.csv
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


CURRENT_PATH = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_PATH.parent.parent
INTERIM_DATA_PATH = PROJECT_ROOT / "InterimData"
OUTPUT_DIR = PROJECT_ROOT / "data" / "kappa"

PAIRWISE_INPUT_PATH = INTERIM_DATA_PATH / "ssr_kappa_pairwise_v4.csv"
FIRM_LEVEL_INPUT_PATH = INTERIM_DATA_PATH / "ssr_kappa_firm_level_v4.csv"
OUTPUT_PATH = OUTPUT_DIR / "ssr_kappa_firm_level_v4.csv"

KEY_COLS = ["rdate", "firm"]
FIRM_LEVEL_REQUIRED_COLS = [
    "rdate",
    "cusip",
    "firm",
    "kappa_norm_mean",
    "kappa_norm_median",
    "kappa_norm_std",
    "n_pairs",
]
PAIRWISE_REQUIRED_COLS = ["rdate", "cusip_j", "firm_j", "kappa"]
OUTPUT_COLS = [
    "rdate",
    "year",
    "quarter",
    "cusip",
    "firm",
    "kappa_norm_mean",
    "kappa_norm_median",
    "kappa_norm_std",
    "n_pairs",
    "kappa_mean",
    "kappa_median",
    "kappa_std",
]


def _read_csv_with_string_keys(path: Path, key_cols: list[str]) -> pd.DataFrame:
    """Read a CSV and keep identifier columns as stripped strings."""
    df = pd.read_csv(path, dtype={col: str for col in key_cols})
    for col in key_cols:
        df[col] = df[col].astype("string").str.strip()
    return df


def _require_columns(df: pd.DataFrame, required_cols: list[str], source_name: str) -> None:
    """Raise a clear error if required columns are missing."""
    missing_cols = sorted(set(required_cols) - set(df.columns))
    if missing_cols:
        raise KeyError(f"{source_name} is missing required columns: {missing_cols}")


def _require_no_missing_keys(df: pd.DataFrame, key_cols: list[str], source_name: str) -> None:
    """Reject rows with missing merge keys before any aggregation or merge."""
    missing_mask = df[key_cols].isna().any(axis=1)
    if missing_mask.any():
        examples = df.loc[missing_mask, key_cols].head(10)
        raise ValueError(
            f"{source_name} has {int(missing_mask.sum())} rows with missing keys. "
            f"Examples:\n{examples}"
        )


def _require_unique_keys(df: pd.DataFrame, key_cols: list[str], source_name: str) -> None:
    """Reject non-unique merge keys because the final merge must be 1:1."""
    duplicate_mask = df.duplicated(key_cols, keep=False)
    if duplicate_mask.any():
        examples = df.loc[duplicate_mask, key_cols].sort_values(key_cols).head(20)
        raise ValueError(
            f"{source_name} is not unique by {key_cols}. "
            f"Duplicate rows: {int(duplicate_mask.sum())}. Examples:\n{examples}"
        )


def _add_year_quarter_from_rdate(df: pd.DataFrame) -> pd.DataFrame:
    """Add cohort-panel-compatible integer year and quarter columns from rdate."""
    result = df.copy()
    rdate = result["rdate"].astype("string").str.strip()
    parsed = pd.to_datetime(rdate, format="%Y%m%d", errors="coerce")
    invalid_dates = parsed.isna()
    if invalid_dates.any():
        examples = result.loc[invalid_dates, ["rdate", "firm"]].head(10)
        raise ValueError(
            f"rdate must use YYYYMMDD format. Invalid rows: {int(invalid_dates.sum())}. "
            f"Examples:\n{examples}"
        )

    result["year"] = parsed.dt.year.astype("int16")
    result["quarter"] = parsed.dt.quarter.astype("int8")
    return result


def load_firm_level() -> pd.DataFrame:
    """Load the existing normalized firm-level kappa file."""
    firm_level = _read_csv_with_string_keys(
        FIRM_LEVEL_INPUT_PATH,
        key_cols=["rdate", "cusip", "firm"],
    )
    _require_columns(firm_level, FIRM_LEVEL_REQUIRED_COLS, FIRM_LEVEL_INPUT_PATH.name)
    _require_no_missing_keys(firm_level, KEY_COLS, FIRM_LEVEL_INPUT_PATH.name)
    _require_unique_keys(firm_level, KEY_COLS, FIRM_LEVEL_INPUT_PATH.name)
    return _add_year_quarter_from_rdate(firm_level)


def build_raw_kappa_moments() -> pd.DataFrame:
    """Aggregate raw pairwise kappa to rdate + firm_j level."""
    pairwise = _read_csv_with_string_keys(
        PAIRWISE_INPUT_PATH,
        key_cols=["rdate", "cusip_j", "firm_j"],
    )
    _require_columns(pairwise, PAIRWISE_REQUIRED_COLS, PAIRWISE_INPUT_PATH.name)
    _require_no_missing_keys(pairwise, ["rdate", "firm_j", "cusip_j"], PAIRWISE_INPUT_PATH.name)

    pairwise["kappa"] = pd.to_numeric(pairwise["kappa"], errors="coerce")
    missing_kappa = pairwise["kappa"].isna()
    if missing_kappa.any():
        examples = pairwise.loc[missing_kappa, ["rdate", "firm_j", "kappa"]].head(10)
        raise ValueError(
            f"{PAIRWISE_INPUT_PATH.name} has {int(missing_kappa.sum())} rows with missing "
            f"or nonnumeric kappa. Examples:\n{examples}"
        )

    aggregated = (
        pairwise.groupby(["rdate", "firm_j"], as_index=False)
        .agg(
            cusip=("cusip_j", "first"),
            cusip_nunique=("cusip_j", "nunique"),
            kappa_mean=("kappa", "mean"),
            kappa_median=("kappa", "median"),
            kappa_std=("kappa", "std"),
            n_pairs_from_pairwise=("kappa", "count"),
        )
        .rename(columns={"firm_j": "firm"})
    )

    multiple_cusip = aggregated["cusip_nunique"].gt(1)
    if multiple_cusip.any():
        examples = aggregated.loc[multiple_cusip, ["rdate", "firm", "cusip_nunique"]].head(20)
        raise ValueError(
            "Pairwise groups are not unique by cusip_j within rdate + firm_j. "
            f"Problem groups: {int(multiple_cusip.sum())}. Examples:\n{examples}"
        )

    aggregated = aggregated.drop(columns=["cusip_nunique"])
    _require_unique_keys(aggregated, KEY_COLS, "aggregated pairwise kappa")
    return aggregated


def merge_and_validate(firm_level: pd.DataFrame, raw_moments: pd.DataFrame) -> pd.DataFrame:
    """Merge raw kappa moments into the normalized firm-level file with hard checks."""
    merged = firm_level.merge(
        raw_moments,
        on=KEY_COLS,
        how="outer",
        suffixes=("", "_from_pairwise"),
        indicator=True,
        validate="1:1",
        sort=False,
    )

    merge_counts = merged["_merge"].value_counts(dropna=False).to_dict()
    merge_mismatch = ~merged["_merge"].eq("both")
    if merge_mismatch.any():
        examples = merged.loc[
            merge_mismatch,
            ["rdate", "firm", "cusip", "cusip_from_pairwise", "_merge"],
        ].head(20)
        raise ValueError(
            "Firm-level rows and pairwise rdate + firm_j groups do not match 1:1. "
            f"Merge counts: {merge_counts}. Examples:\n{examples}"
        )

    cusip_mismatch = ~merged["cusip"].eq(merged["cusip_from_pairwise"])
    if cusip_mismatch.any():
        examples = merged.loc[
            cusip_mismatch,
            ["rdate", "firm", "cusip", "cusip_from_pairwise"],
        ].head(20)
        raise ValueError(
            f"cusip does not match cusip_j after 1:1 merge. "
            f"Mismatched rows: {int(cusip_mismatch.sum())}. Examples:\n{examples}"
        )

    n_pairs_mismatch = ~merged["n_pairs"].eq(merged["n_pairs_from_pairwise"])
    if n_pairs_mismatch.any():
        examples = merged.loc[
            n_pairs_mismatch,
            ["rdate", "firm", "n_pairs", "n_pairs_from_pairwise"],
        ].head(20)
        raise ValueError(
            f"n_pairs does not match raw pairwise counts. "
            f"Mismatched rows: {int(n_pairs_mismatch.sum())}. Examples:\n{examples}"
        )

    return merged[OUTPUT_COLS].copy()


def main() -> None:
    """Run the kappa firm-level build and export the checked file."""
    firm_level = load_firm_level()
    raw_moments = build_raw_kappa_moments()
    output = merge_and_validate(firm_level, raw_moments)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output.to_csv(OUTPUT_PATH, index=False)

    print(f"Firm-level input rows: {len(firm_level)}")
    print(f"Pairwise rdate + firm_j groups: {len(raw_moments)}")
    print(f"Output rows: {len(output)}")
    print(f"Saved: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
