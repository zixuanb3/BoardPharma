"""Purpose:
    Expand every formulary-quarter to the complete set of non-generic NDCs in
    the upstream formulary panel.

Process:
    1. Scan the upstream panel in chunks to collect unique formulary-quarter
       maximum tiers and unique NDC metadata.
    2. Route actual formulary-quarter-NDC records into disk-backed batches.
    3. Cross join each formulary-quarter batch with all NDCs, retain the source
       tier when present, and set included to 1 for source records and 0 for
       newly expanded records.
    4. Stream validated batches to a temporary CSV and replace the output only
       after the complete expansion succeeds.

Input:
    data/formulary/formulary_panel_with_company_id.csv

Output:
    D:/pharma/formulary/task1_expanded_brand_panel.csv
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUT = PROJECT_ROOT / "data" / "formulary" / "formulary_panel_with_company_id.csv"
OUTPUT_DIR = Path(r"D:\pharma\formulary")
OUTPUT = OUTPUT_DIR / "task1_expanded_brand_panel.csv"
TEMP_OUTPUT = OUTPUT.with_name(OUTPUT.name + ".building")

READ_CHUNK_SIZE = 500_000
TARGET_EXPANDED_ROWS = 1_000_000

KEY_COLUMNS = ["YEAR_Q", "FORMULARY_ID", "NDC"]
FORMULARY_COLUMNS = ["YEAR_Q", "FORMULARY_ID", "max_tier"]
ACTUAL_COLUMNS = [*KEY_COLUMNS, "tier_raw"]
REMOVED_COLUMNS = {
    "ProprietaryName",
    "NonProprietaryName",
    "MARKETINGCATEGORYNAME",
    "ATC1",
    "ATC1_name",
    "ATC2",
    "ATC2_name",
    "ATC4",
    "ATC4_name",
}
REQUIRED_COLUMNS = {
    *ACTUAL_COLUMNS,
    "max_tier",
    "id",
    "is_generic",
}


def clean_keys(data: pd.DataFrame) -> pd.DataFrame:
    """Normalize the three source keys while preserving leading zeros."""
    result = data.copy()
    for column in KEY_COLUMNS:
        result[column] = result[column].astype("string").str.strip()
        result[column] = result[column].mask(result[column].eq(""), pd.NA)
    if result[KEY_COLUMNS].isna().any().any():
        examples = result.loc[result[KEY_COLUMNS].isna().any(axis=1), KEY_COLUMNS].head(10)
        raise ValueError(f"Input contains missing expansion keys. Examples:\n{examples}")
    return result


def unique_rows(
    data: pd.DataFrame,
    key_columns: list[str],
    label: str,
) -> pd.DataFrame:
    """Return one row per key and reject conflicting non-key values."""
    deduplicated = data.drop_duplicates()
    conflicts = deduplicated.loc[
        deduplicated.duplicated(key_columns, keep=False)
    ].sort_values(key_columns)
    if not conflicts.empty:
        raise ValueError(
            f"{label} contains conflicting values for the same key. Examples:\n"
            f"{conflicts.head(20)}"
        )
    return deduplicated.drop_duplicates(key_columns).reset_index(drop=True)


def inspect_input_schema() -> tuple[list[str], list[str]]:
    """Return retained source columns and NDC-level metadata columns."""
    if not INPUT.exists():
        raise FileNotFoundError(f"Input panel not found: {INPUT}")

    source_columns = pd.read_csv(INPUT, nrows=0).columns.tolist()
    missing = sorted(REQUIRED_COLUMNS - set(source_columns))
    if missing:
        raise KeyError(f"Input panel is missing required columns: {missing}")

    retained_columns = [
        column for column in source_columns if column not in REMOVED_COLUMNS
    ]
    ndc_metadata_columns = [
        column
        for column in retained_columns
        if column not in {*KEY_COLUMNS, "tier_raw", "max_tier"}
    ]
    return retained_columns, ndc_metadata_columns


def collect_dimensions(
    retained_columns: list[str],
    ndc_metadata_columns: list[str],
) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """Collect unique formulary-quarter rows and unique NDC metadata."""
    formulary_parts: list[pd.DataFrame] = []
    ndc_parts: list[pd.DataFrame] = []
    source_rows = 0

    reader = pd.read_csv(
        INPUT,
        usecols=retained_columns,
        dtype={column: "string" for column in KEY_COLUMNS},
        chunksize=READ_CHUNK_SIZE,
        low_memory=False,
        on_bad_lines="error",
    )
    for chunk_number, chunk in enumerate(reader, start=1):
        chunk = clean_keys(chunk)
        source_rows += len(chunk)

        generic = pd.to_numeric(chunk["is_generic"], errors="coerce")
        invalid_generic = generic.ne(0) | generic.isna()
        if invalid_generic.any():
            examples = chunk.loc[
                invalid_generic, ["NDC", "is_generic"]
            ].drop_duplicates().head(10)
            raise ValueError(
                "The upstream panel must already contain only is_generic=0 rows. "
                f"Examples:\n{examples}"
            )

        formulary_parts.append(chunk[FORMULARY_COLUMNS].drop_duplicates())
        ndc_parts.append(chunk[["NDC", *ndc_metadata_columns]].drop_duplicates())

        if chunk_number % 10 == 0:
            print(f"  Pass 1: scanned {source_rows:,} source rows...")

    if not formulary_parts or not ndc_parts:
        raise ValueError("The input panel contains no rows.")

    formularies = pd.concat(formulary_parts, ignore_index=True)
    formularies["max_tier"] = pd.to_numeric(
        formularies["max_tier"], errors="coerce"
    )
    if formularies["max_tier"].isna().any():
        examples = formularies.loc[
            formularies["max_tier"].isna(), ["YEAR_Q", "FORMULARY_ID"]
        ].drop_duplicates().head(10)
        raise ValueError(f"Formulary-quarters have missing max_tier. Examples:\n{examples}")
    formularies = unique_rows(
        formularies,
        ["YEAR_Q", "FORMULARY_ID"],
        "Formulary-quarter metadata",
    ).sort_values(["YEAR_Q", "FORMULARY_ID"], ignore_index=True)

    ndc_metadata = unique_rows(
        pd.concat(ndc_parts, ignore_index=True),
        ["NDC"],
        "NDC metadata",
    ).sort_values("NDC", ignore_index=True)
    if ndc_metadata["id"].isna().any():
        examples = ndc_metadata.loc[ndc_metadata["id"].isna(), ["NDC"]].head(10)
        raise ValueError(f"NDCs have missing mapping ids. Examples:\n{examples}")

    return formularies, ndc_metadata, source_rows


def route_actual_records(
    batch_lookup: pd.DataFrame,
    staging_dir: Path,
) -> dict[int, Path]:
    """Route source presence and tier records to their expansion batches."""
    stage_paths: dict[int, Path] = {}
    headers_written: set[int] = set()

    reader = pd.read_csv(
        INPUT,
        usecols=ACTUAL_COLUMNS,
        dtype={column: "string" for column in KEY_COLUMNS},
        chunksize=READ_CHUNK_SIZE,
        low_memory=False,
        on_bad_lines="error",
    )
    routed_rows = 0
    for chunk_number, chunk in enumerate(reader, start=1):
        chunk = clean_keys(chunk)
        chunk = chunk.merge(
            batch_lookup,
            on=["YEAR_Q", "FORMULARY_ID"],
            how="left",
            validate="many_to_one",
        )
        if chunk["_batch_id"].isna().any():
            examples = chunk.loc[
                chunk["_batch_id"].isna(), ["YEAR_Q", "FORMULARY_ID"]
            ].drop_duplicates().head(10)
            raise KeyError(
                "Formulary-quarters are missing from the batch lookup. "
                f"Examples:\n{examples}"
            )

        chunk["_batch_id"] = chunk["_batch_id"].astype("int32")
        for batch_id, subset in chunk.groupby("_batch_id", sort=False):
            numeric_batch_id = int(batch_id)
            path = stage_paths.setdefault(
                numeric_batch_id,
                staging_dir / f"actual_batch_{numeric_batch_id:05d}.csv",
            )
            subset[ACTUAL_COLUMNS].to_csv(
                path,
                mode="a",
                header=numeric_batch_id not in headers_written,
                index=False,
            )
            headers_written.add(numeric_batch_id)

        routed_rows += len(chunk)
        if chunk_number % 10 == 0:
            print(f"  Pass 2: routed {routed_rows:,} actual rows...")

    expected_batches = set(batch_lookup["_batch_id"].astype(int).unique())
    missing_batches = sorted(expected_batches - set(stage_paths))
    if missing_batches:
        raise RuntimeError(f"No actual rows were routed to batches: {missing_batches[:10]}")
    return stage_paths


def expand_batches(
    formularies: pd.DataFrame,
    ndc_metadata: pd.DataFrame,
    stage_paths: dict[int, Path],
    retained_columns: list[str],
    formularies_per_batch: int,
) -> tuple[int, int]:
    """Expand, validate, and stream all formulary-quarter batches."""
    output_columns = [
        "YEAR_Q",
        "FORMULARY_ID",
        "NDC",
        "included",
        "tier_raw",
        "max_tier",
        *[
            column
            for column in retained_columns
            if column not in {*KEY_COLUMNS, "tier_raw", "max_tier"}
        ],
    ]

    rows_written = 0
    included_rows = 0
    first_batch = True
    n_batches = len(stage_paths)

    for batch_id in range(n_batches):
        start = batch_id * formularies_per_batch
        stop = min(start + formularies_per_batch, len(formularies))
        formulary_batch = formularies.iloc[start:stop].copy()

        actual = pd.read_csv(
            stage_paths[batch_id],
            dtype={column: "string" for column in KEY_COLUMNS},
            low_memory=False,
            on_bad_lines="error",
        )
        actual = clean_keys(actual)
        actual = unique_rows(actual, KEY_COLUMNS, f"Actual records in batch {batch_id}")
        actual["included"] = pd.Series(1, index=actual.index, dtype="int8")

        expanded = formulary_batch.merge(ndc_metadata, how="cross")
        expanded = expanded.merge(
            actual,
            on=KEY_COLUMNS,
            how="left",
            validate="one_to_one",
        )
        expanded["included"] = expanded["included"].fillna(0).astype("int8")
        batch_included_rows = int(expanded["included"].sum())
        if batch_included_rows != len(actual):
            raise RuntimeError(
                f"Batch {batch_id} included {batch_included_rows:,} rows, "
                f"but contains {len(actual):,} unique actual records."
            )

        invalid_absent_tier = expanded["included"].eq(0) & expanded["tier_raw"].notna()
        if invalid_absent_tier.any():
            raise ValueError(
                f"Batch {batch_id} has expanded rows with included=0 and nonmissing tier_raw."
            )
        if expanded["max_tier"].isna().any():
            raise ValueError(f"Batch {batch_id} has missing max_tier values.")
        if expanded["id"].isna().any():
            raise ValueError(f"Batch {batch_id} has missing id values.")

        expanded = expanded[output_columns]
        expanded.to_csv(
            TEMP_OUTPUT,
            mode="w" if first_batch else "a",
            header=first_batch,
            index=False,
        )
        first_batch = False
        rows_written += len(expanded)
        included_rows += batch_included_rows
        print(
            f"  Expansion: batch {batch_id + 1:,}/{n_batches:,}; "
            f"written {rows_written:,} rows..."
        )

    return rows_written, included_rows


def main() -> None:
    """Build the complete formulary-quarter by NDC expansion."""
    retained_columns, ndc_metadata_columns = inspect_input_schema()
    print(f"Input: {INPUT}")
    print(f"Output: {OUTPUT}")
    print(f"Retained source columns: {retained_columns}")

    print("Pass 1/3: collecting formulary-quarter and NDC dimensions...")
    formularies, ndc_metadata, source_rows = collect_dimensions(
        retained_columns,
        ndc_metadata_columns,
    )
    if ndc_metadata.empty:
        raise ValueError("The input panel contains no NDCs to expand.")

    formularies_per_batch = max(
        1,
        TARGET_EXPANDED_ROWS // len(ndc_metadata),
    )
    formularies["_batch_id"] = (
        formularies.index // formularies_per_batch
    ).astype("int32")
    batch_lookup = formularies[["YEAR_Q", "FORMULARY_ID", "_batch_id"]]
    formulary_values = formularies.drop(columns="_batch_id")
    expected_rows = len(formulary_values) * len(ndc_metadata)

    print(f"Source rows: {source_rows:,}")
    print(f"Unique formulary-quarters: {len(formulary_values):,}")
    print(f"Unique NDCs: {len(ndc_metadata):,}")
    print(f"Formulary-quarters per batch: {formularies_per_batch:,}")
    print(f"Expected expanded rows: {expected_rows:,}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if TEMP_OUTPUT.exists():
        TEMP_OUTPUT.unlink()

    with tempfile.TemporaryDirectory(
        prefix="task1_expand_",
        dir=OUTPUT_DIR,
    ) as temporary_directory:
        staging_dir = Path(temporary_directory)
        print("Pass 2/3: routing actual records to disk-backed batches...")
        stage_paths = route_actual_records(batch_lookup, staging_dir)

        print("Pass 3/3: expanding and writing batches...")
        rows_written, included_rows = expand_batches(
            formulary_values,
            ndc_metadata,
            stage_paths,
            retained_columns,
            formularies_per_batch,
        )

    if rows_written != expected_rows:
        raise RuntimeError(
            f"Expanded row count mismatch: wrote {rows_written:,}, "
            f"expected {expected_rows:,}."
        )

    os.replace(TEMP_OUTPUT, OUTPUT)
    print("Expansion complete.")
    print(f"Output rows: {rows_written:,}")
    print(f"Included rows: {included_rows:,}")
    print(f"Saved: {OUTPUT}")


if __name__ == "__main__":
    main()
