"""
Generate the movement firm-side event eligibility table.

This script reads movement event candidates, expands each candidate into
A-side and B-side firm rows, computes row-level eligibility flags, and writes
one grouped event table.

Input:
- data/movement_tables/movement_event_candidates.csv

Output:
- data/event_table.csv
"""

from pathlib import Path

import pandas as pd


CURRENT_PATH = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_PATH.parent.parent
INPUT_PATH = PROJECT_ROOT / "data" / "movement_tables" / "movement_event_candidates.csv"
OUTPUT_PATH = PROJECT_ROOT / "data" / "event_table.csv"

EVENT_TYPES = {
    "to_B_still_in_A",
    "to_B_not_in_A",
    "interlock_dissolution",
}
BASE_REQUIRED_COLUMNS = {
    "event_type",
    "event_year",
    "FirmA",
    "FirmB",
    "requirement1",
    "requirement2_A",
    "requirement2_B",
}
OUTPUT_COLUMNS = ["BoardName", "year", "event_type", "firm_type", "req0", "req1", "req2"]
GROUP_KEYS = ["BoardName", "year", "event_type", "firm_type"]


def detect_stay_column(columns: pd.Index) -> str:
    """Return the unique stay_{x}_years column name."""
    stay_columns = [
        col
        for col in columns
        if col.startswith("stay_") and col.endswith("_years")
    ]
    if len(stay_columns) != 1:
        raise ValueError(
            "Expected exactly one stay_{x}_years column; "
            f"found {len(stay_columns)}: {stay_columns}"
        )
    return stay_columns[0]


def validate_columns(df: pd.DataFrame, stay_column: str) -> None:
    """Validate the columns required to build the event table."""
    required_columns = BASE_REQUIRED_COLUMNS | {stay_column}
    missing_columns = sorted(required_columns - set(df.columns))
    if missing_columns:
        raise ValueError(f"Missing required columns: {missing_columns}")


def expand_firm_sides(df: pd.DataFrame, stay_column: str) -> pd.DataFrame:
    """Expand candidate rows into A-side and B-side firm rows."""
    shared_columns = ["event_type", "event_year", stay_column, "requirement1"]

    a_side = df[shared_columns + ["FirmA", "requirement2_A"]].rename(
        columns={
            "FirmA": "BoardName",
            "event_year": "year",
            stay_column: "stay",
            "requirement2_A": "requirement2",
        }
    )
    a_side["firm_type"] = "A"

    b_side = df[shared_columns + ["FirmB", "requirement2_B"]].rename(
        columns={
            "FirmB": "BoardName",
            "event_year": "year",
            stay_column: "stay",
            "requirement2_B": "requirement2",
        }
    )
    b_side["firm_type"] = "B"

    expanded = pd.concat([a_side, b_side], ignore_index=True)
    expanded = expanded.dropna(subset=["BoardName", "year"]).copy()
    expanded["year"] = expanded["year"].astype(int)
    return expanded


def build_event_table(candidates: pd.DataFrame) -> pd.DataFrame:
    """Build grouped movement firm-side event eligibility flags."""
    stay_column = detect_stay_column(candidates.columns)
    validate_columns(candidates, stay_column)

    filtered = candidates.loc[candidates["event_type"].isin(EVENT_TYPES)].copy()
    expanded = expand_firm_sides(filtered, stay_column)

    stay_met = expanded["stay"].eq(1)
    requirement1_met = expanded["requirement1"].eq(1)
    requirement2_met = expanded["requirement2"].eq(1)

    expanded["req0"] = stay_met.astype("int8")
    expanded["req1"] = (stay_met & requirement1_met).astype("int8")
    expanded["req2"] = (stay_met & requirement1_met & requirement2_met).astype("int8")

    event_table = (
        expanded.groupby(GROUP_KEYS, as_index=False)[["req0", "req1", "req2"]]
        .max()
        .sort_values(["event_type", "firm_type", "BoardName", "year"])
        .reset_index(drop=True)
    )
    event_table[["req0", "req1", "req2"]] = event_table[["req0", "req1", "req2"]].astype("int8")
    return event_table[OUTPUT_COLUMNS]


def main() -> None:
    """Read movement candidates and write the firm-side event table."""
    candidates = pd.read_csv(INPUT_PATH)
    event_table = build_event_table(candidates)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    event_table.to_csv(OUTPUT_PATH, index=False)
    print(f"Saved: {OUTPUT_PATH} ({len(event_table):,} rows)")


if __name__ == "__main__":
    main()
