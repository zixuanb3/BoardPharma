"""
Build a reusable mapped formulary panel cache for personnel cohort panels.

The raw formulary panel is large. This script reads it in chunks, keeps rows
with a nonmissing BoardName, derives year/quarter/product keys, and writes a
Parquet cache under InterimData. Downstream scripts can then avoid repeatedly
scanning the full raw CSV.
"""

from __future__ import annotations

import argparse
import gc
import os
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


CURRENT_PATH = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_PATH.parent.parent
INTERIM_DATA_PATH = PROJECT_ROOT / "InterimData"

RAW_FORMULARY_PANEL_PATH = Path(r"D:\task1_final_panel_with_atc.csv")
CACHED_FORMULARY_PANEL_PATH = (
    INTERIM_DATA_PATH / "task1_final_panel_with_atc_boardname.parquet"
)
DEFAULT_CHUNKSIZE = 2_500_000
STRING_KEY_COLS = (
    "YEAR_Q",
    "FORMULARY_ID",
    "NDC",
    "BoardName",
    "ATC2",
    "ATC3",
    "ATC4",
    "ATC5",
)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Cache the large formulary panel after dropping missing BoardName rows.",
    )
    parser.add_argument(
        "--input",
        dest="input_path",
        type=Path,
        default=RAW_FORMULARY_PANEL_PATH,
        help="Raw formulary CSV path.",
    )
    parser.add_argument(
        "--output",
        dest="output_path",
        type=Path,
        default=CACHED_FORMULARY_PANEL_PATH,
        help="Output Parquet cache path.",
    )
    parser.add_argument(
        "--chunksize",
        dest="chunksize",
        type=int,
        default=DEFAULT_CHUNKSIZE,
        help="Rows per CSV chunk.",
    )
    return parser.parse_args()


def parse_year_quarter(data: pd.DataFrame, source_name: str) -> tuple[pd.Series, pd.Series]:
    """Parse YEAR_Q values such as '2019 Q1' into integer year and quarter."""
    parsed = data["YEAR_Q"].astype("string").str.extract(r"^\s*(\d{4})\s*Q([1-4])\s*$")
    invalid = parsed[0].isna() | parsed[1].isna()
    if invalid.any():
        examples = data.loc[invalid, ["YEAR_Q"]].drop_duplicates().head(10)
        raise ValueError(f"{source_name}.YEAR_Q has invalid values. Examples:\n{examples}")
    return parsed[0].astype("int32"), parsed[1].astype("int8")


def clean_key_columns(data: pd.DataFrame) -> None:
    """Strip key columns in place and blank empty strings."""
    for col in STRING_KEY_COLS:
        if col not in data.columns:
            continue
        cleaned = data[col].astype("string").str.strip()
        data[col] = cleaned.mask(cleaned.eq(""), pd.NA)


def normalize_chunk(chunk: pd.DataFrame, source_name: str) -> pd.DataFrame:
    """Clean one chunk and keep only rows with mapped BoardName."""
    clean_key_columns(chunk)
    data = chunk
    data["BoardName"] = data["BoardName"].str.upper()
    before = len(data)
    data = data.dropna(subset=["BoardName"]).copy()
    if data.empty:
        return data.assign(year=pd.Series(dtype="int32"), quarter=pd.Series(dtype="int32"))

    data["year"], data["quarter"] = parse_year_quarter(data, source_name)
    data["product"] = data["NDC"]
    print(f"    kept {len(data):,}/{before:,} rows in chunk")
    return data


def build_cache(input_path: Path, output_path: Path, chunksize: int) -> None:
    """Build the filtered Parquet cache from the raw CSV."""
    if not input_path.exists():
        raise FileNotFoundError(f"Raw formulary panel not found: {input_path}")
    if chunksize < 1:
        raise ValueError("chunksize must be >= 1")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_output_path = output_path.with_name(
        f"{output_path.stem}.tmp.{os.getpid()}{output_path.suffix}"
    )
    if temp_output_path.exists():
        temp_output_path.unlink()

    writer: pq.ParquetWriter | None = None
    total_rows = 0
    kept_rows = 0
    chunks = 0

    try:
        for chunk in pd.read_csv(input_path, dtype="string", chunksize=chunksize):
            data = None
            table = None
            try:
                chunks += 1
                total_rows += len(chunk)
                data = normalize_chunk(chunk, input_path.name)
                kept_rows += len(data)
                if data.empty:
                    continue

                table = pa.Table.from_pandas(data, preserve_index=False)
                if writer is None:
                    writer = pq.ParquetWriter(temp_output_path, table.schema, compression="zstd")
                writer.write_table(table)
            finally:
                del table
                del data
                del chunk
                gc.collect()
    finally:
        if writer is not None:
            writer.close()

    if writer is None:
        raise ValueError("No rows with nonmissing BoardName were found; cache was not created.")

    dropped_rows = total_rows - kept_rows
    drop_rate = dropped_rows / total_rows if total_rows else 0
    print(f"Processed chunks: {chunks:,}")
    print(f"Total rows: {total_rows:,}")
    print(f"Kept rows: {kept_rows:,}")
    print(f"Dropped missing BoardName rows: {dropped_rows:,} ({drop_rate:.2%})")
    try:
        if output_path.exists():
            output_path.unlink()
        temp_output_path.replace(output_path)
    except PermissionError as exc:
        raise PermissionError(
            f"Cannot replace {output_path} because Windows reports it is in use. "
            "Close any program reading it, or stop the previous Python process, then rerun. "
            f"The newly written temporary file is: {temp_output_path}"
        ) from exc
    print(f"Saved cache: {output_path}")


def main() -> None:
    """CLI entry point."""
    args = parse_args()
    build_cache(
        input_path=args.input_path,
        output_path=args.output_path,
        chunksize=args.chunksize,
    )


if __name__ == "__main__":
    main()
