"""Purpose:
Build a row-level beneficiary cost file for formulary candidates and calculate
a daily nonpreferred copay.

Process:
1. Read only FORMULARY_ID from the formulary panel and collect unique candidate
   IDs after removing leading zeros.
2. Read selected beneficiary columns in chunks, retain candidate formularies
   with COVERAGE_LEVEL equal to 1, and remove COVERAGE_LEVEL.
3. Calculate copay from the nonpreferred cost fields, divide it by the days
   represented by DAYS_SUPPLY, and flag inconsistent minimum/maximum amounts.
4. Remove the three source amount columns and stream the retained rows to CSV.

Input:
- data/formulary/formulary_panel_with_company_id.csv
- D:/pharma/merged_beneficiary_cost.csv

Output:
- D:/pharma/formulary/beneficiary_cost_with_copay.csv
"""

from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FORMULARY_PANEL = (
    PROJECT_ROOT / "data" / "formulary" / "formulary_panel_with_company_id.csv"
)
BENEFICIARY_COST = Path(r"D:\pharma\merged_beneficiary_cost.csv")
OUTPUT_DIR = Path(r"D:\pharma\formulary")
OUTPUT_PATH = OUTPUT_DIR / "beneficiary_cost_with_copay.csv"

CHUNK_SIZE = 2_000_000
DAYS_SUPPLY_TO_DAYS = {
    "1": 30,
    "4": 60,
    "2": 90,
}

BENEFICIARY_COLUMNS = [
    "CONTRACT_ID",
    "PLAN_ID",
    "SEGMENT_ID",
    "COVERAGE_LEVEL",
    "TIER",
    "DAYS_SUPPLY",
    # "COST_TYPE_PREF",
    # "COST_AMT_PREF",
    # "COST_MIN_AMT_PREF",
    # "COST_MAX_AMT_PREF",
    "COST_TYPE_NONPREF",
    "COST_AMT_NONPREF",
    "COST_MIN_AMT_NONPREF",
    "COST_MAX_AMT_NONPREF",
    # "COST_TYPE_MAIL_PREF",
    # "COST_AMT_MAIL_PREF",
    # "COST_MIN_AMT_MAIL_PREF",
    # "COST_MAX_AMT_MAIL_PREF",
    # "COST_TYPE_MAIL_NONPREF",
    # "COST_AMT_MAIL_NONPREF",
    # "COST_MIN_AMT_MAIL_NONPREF",
    # "COST_MAX_AMT_MAIL_NONPREF",
    "TIER_SPECIALTY_YN",
    "FORMULARY_ID",
    "STATE",
    "COUNTY_CODE",
    "YEAR_Q",
    "MA_REGION_CODE",
    "PDP_REGION_CODE",
]

OUTPUT_COLUMNS = [
    "CONTRACT_ID",
    "PLAN_ID",
    "SEGMENT_ID",
    "TIER",
    "DAYS_SUPPLY",
    "COST_TYPE_NONPREF",
    "TIER_SPECIALTY_YN",
    "FORMULARY_ID",
    "STATE",
    "COUNTY_CODE",
    "YEAR_Q",
    "MA_REGION_CODE",
    "PDP_REGION_CODE",
    "copay",
    "min_bigger_than_max",
]


def normalize_formulary_id(values: pd.Series) -> pd.Series:
    """Return comparable formulary IDs without leading zeros."""
    normalized = values.astype("string").str.strip().str.lstrip("0")
    empty_id = normalized.notna() & normalized.eq("")
    return normalized.mask(empty_id, "0")


def load_formulary_candidates() -> set[str]:
    """Collect unique formulary IDs while reading only the required column."""
    candidates: set[str] = set()
    for chunk in pd.read_csv(
        FORMULARY_PANEL,
        usecols=["FORMULARY_ID"],
        dtype="string",
        chunksize=CHUNK_SIZE,
    ):
        normalized = normalize_formulary_id(chunk["FORMULARY_ID"]).dropna()
        candidates.update(normalized.tolist())
    return candidates


def add_copay_fields(chunk: pd.DataFrame) -> pd.DataFrame:
    """Calculate daily copay and the min/max availability-order flag."""
    cost_type = pd.to_numeric(chunk["COST_TYPE_NONPREF"], errors="coerce")
    amount = pd.to_numeric(chunk["COST_AMT_NONPREF"], errors="coerce")
    minimum = pd.to_numeric(chunk["COST_MIN_AMT_NONPREF"], errors="coerce")
    maximum = pd.to_numeric(chunk["COST_MAX_AMT_NONPREF"], errors="coerce")

    range_value = pd.concat([minimum, maximum], axis=1).mean(
        axis=1,
        skipna=True,
    )
    copay = pd.Series(float("nan"), index=chunk.index, dtype="float64")
    type_one = cost_type.eq(1)
    type_two = cost_type.eq(2)
    copay.loc[type_one] = amount.loc[type_one].fillna(range_value.loc[type_one])
    copay.loc[type_two] = range_value.loc[type_two]

    supply_days = (
        chunk["DAYS_SUPPLY"].astype("string").str.strip().map(DAYS_SUPPLY_TO_DAYS)
    )
    chunk["copay"] = copay.div(supply_days)

    minimum_valid = minimum.notna()
    maximum_valid = maximum.notna()
    both_valid = minimum_valid & maximum_valid
    one_valid = minimum_valid ^ maximum_valid

    min_max_flag = pd.Series(3, index=chunk.index, dtype="int8")
    min_max_flag.loc[one_valid] = 2
    min_max_flag.loc[both_valid] = (
        minimum.loc[both_valid] > maximum.loc[both_valid]
    ).astype("int8")
    chunk["min_bigger_than_max"] = min_max_flag

    return chunk.drop(
        columns=[
            "COST_AMT_NONPREF",
            "COST_MIN_AMT_NONPREF",
            "COST_MAX_AMT_NONPREF",
        ]
    )


def main() -> None:
    """Filter beneficiary cost data and stream the calculated rows to CSV."""
    print("Loading formulary candidates...")
    candidates = load_formulary_candidates()
    print(f"Unique formulary candidates: {len(candidates):,}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    temporary_output = OUTPUT_PATH.with_suffix(OUTPUT_PATH.suffix + ".tmp")
    if temporary_output.exists():
        temporary_output.unlink()

    source_rows = 0
    coverage_one_rows = 0
    output_rows = 0
    first_chunk = True

    for chunk_number, chunk in enumerate(
        pd.read_csv(
            BENEFICIARY_COST,
            usecols=BENEFICIARY_COLUMNS,
            dtype="string",
            chunksize=CHUNK_SIZE,
        ),
        start=1,
    ):
        source_rows += len(chunk)

        coverage_one = chunk["COVERAGE_LEVEL"].str.strip().eq("1").fillna(False)
        coverage_one_rows += int(coverage_one.sum())
        chunk = chunk.loc[coverage_one].copy()
        if chunk.empty:
            continue

        chunk["_formulary_key"] = normalize_formulary_id(chunk["FORMULARY_ID"])
        chunk = chunk.loc[chunk["_formulary_key"].isin(candidates)].copy()
        if chunk.empty:
            continue

        chunk = chunk.drop(columns=["COVERAGE_LEVEL", "_formulary_key"])
        chunk = add_copay_fields(chunk)
        chunk = chunk[OUTPUT_COLUMNS]
        output_rows += len(chunk)

        chunk.to_csv(
            temporary_output,
            mode="w" if first_chunk else "a",
            header=first_chunk,
            index=False,
        )
        first_chunk = False

        if chunk_number % 10 == 0:
            print(
                f"Processed {source_rows:,} source rows; "
                f"retained {output_rows:,} rows..."
            )

    if first_chunk:
        raise ValueError("No beneficiary rows matched the requested filters.")

    temporary_output.replace(OUTPUT_PATH)
    print(f"Source rows: {source_rows:,}")
    print(f"Rows with COVERAGE_LEVEL = 1: {coverage_one_rows:,}")
    print(f"Output rows: {output_rows:,}")
    print(f"Saved: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
