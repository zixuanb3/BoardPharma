"""
Purpose:
    Prepare separate BoardEx IE and OC role histories and quarterly observations.

Process:
    Retain IE sector and map it to OC by companyid using the complete IE input.
    Keep the requested fields, remove exact duplicates, and assign OC record IDs.
    Convert date flags to start/end quarters and reject reversed intervals.
    Save complete role histories, then select board roles and clip to the window
    before expanding each eligible role over its inclusive quarterly interval.
    Quarterly records exclude IE brdposition=No and retain only OC seniority
    Executive Director or Supervisory Director.

Input:
    D:/individual_employment.csv
    D:/organization_composition.csv

Output:
    data/boardex/individual_employment_interval.csv
    data/boardex/organization_composition_interval.csv
    data/boardex/individual_employment_record.csv
    data/boardex/organization_composition_record.csv
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


# ========================== USER CONFIG ==========================
@dataclass(frozen=True)
class Config:
    """Input/output paths, open-role endpoint, and quarterly output window."""

    ie_input_path: Path = Path("D:/individual_employment.csv")
    oc_input_path: Path = Path("D:/organization_composition.csv")
    output_dir: Path = Path(__file__).resolve().parents[2] / "data" / "boardex"
    open_end_year: int = 2026
    open_end_quarter: int = 4
    # A None year leaves that side unbounded; its quarter applies when set.
    window_start_year: int | None = 2017
    window_start_quarter: int = 1
    window_end_year: int | None = 2026
    window_end_quarter: int = 4


RUN_CONFIG = Config()


# ========================== FIELD DEFINITIONS ==========================
DATE_COLUMNS = (
    "datestartrole", "datestartroleflag", "dateendrole", "dateendroleflag"
)
COMMON_COLUMNS = (
    "directorid", "directorname", "companyid", "companyname", *DATE_COLUMNS
)
SOURCE_COLUMNS = {
    "ie": (*COMMON_COLUMNS, "brdposition", "rolename", "primarykeyid", "hocountryname", "sector"),
    "oc": (*COMMON_COLUMNS, "rolename", "seniority", "sector"),
}
SOURCE_NAMES = {"ie": "individual_employment", "oc": "organization_composition"}


# ========================== ROLE HISTORY PREPARATION ==========================
def load_sector_lookup(path: Path) -> pd.Series:
    """Validate company-to-sector uniqueness across all IE dates and roles."""
    pairs = pd.read_csv(
        path, usecols=["companyid", "sector"], dtype="string[pyarrow]",
        keep_default_na=False,
    ).drop_duplicates()
    conflicts = pairs["companyid"].duplicated(keep=False)
    if conflicts.any():
        raise ValueError(
            "IE companyid has conflicting sectors (including blank values): "
            f"{pairs.loc[conflicts].head().to_dict('records')}"
        )
    return pairs.set_index("companyid")["sector"]


def prepare_intervals(
    source: str, path: Path, config: Config, sector_lookup: pd.Series,
) -> pd.DataFrame:
    """Deduplicate retained fields, assign OC IDs, and derive quarterly bounds."""
    columns = [c for c in SOURCE_COLUMNS[source] if source == "ie" or c != "sector"]
    data = pd.read_csv(
        path, usecols=columns, dtype="string[pyarrow]",
        keep_default_na=False,
    )[columns].drop_duplicates(ignore_index=True)
    if source == "oc":
        # Use all IE companies, including histories excluded from quarterly records.
        data["sector"] = data["companyid"].map(sector_lookup).fillna("").astype(
            "string[pyarrow]"
        )
        data["oc_record_id"] = np.arange(1, len(data) + 1, dtype=np.int64)

    for side, unknown_flag, annual_flag, annual_quarter in (
        ("start", "75", "30", 1), ("end", "80", "25", 4)
    ):
        dates = data[f"date{side}role"]
        flags = data[f"date{side}roleflag"]
        year = dates.str[:4].astype("Int32")
        quarter = (dates.str[5:7].astype("Int32") - 1) // 3 + 1
        quarter.loc[flags.eq(annual_flag)] = annual_quarter
        if side == "end":
            year.loc[flags.eq("40")] = config.open_end_year
            quarter.loc[flags.eq("40")] = config.open_end_quarter
        known = flags.ne(unknown_flag)
        data[f"year_{side}"] = year.where(known)
        data[f"quarter_{side}"] = quarter.where(known)

    start = data["year_start"] * 4 + data["quarter_start"]
    end = data["year_end"] * 4 + data["quarter_end"]
    reversed_rows = end.lt(start).fillna(False)
    if reversed_rows.any():
        examples = data.loc[reversed_rows, [
            "directorid", "companyid", "year_start", "quarter_start",
            "year_end", "quarter_end",
        ]].head().to_dict("records")
        raise ValueError(
            f"{source.upper()}: end quarter precedes start quarter. "
            f"Examples: {examples}"
        )
    return data


# ========================== WINDOW FILTER AND EXPANSION ==========================
def write_quarters(
    data: pd.DataFrame, source: str, config: Config, path: Path
) -> None:
    """Select board roles and clip, then expand bounded batches with NumPy arrays."""
    columns = [c for c in SOURCE_COLUMNS[source] if c not in DATE_COLUMNS]
    if source == "oc":
        columns.append("oc_record_id")
    start = data["year_start"] * 4 + data["quarter_start"] - 1
    end = data["year_end"] * 4 + data["quarter_end"] - 1
    eligible = start.notna() & end.notna()
    if source == "ie":
        eligible &= data["brdposition"].ne("No").fillna(False)
    else:
        eligible &= data["seniority"].isin(
            ("Executive Director", "Supervisory Director")
        )
    if config.window_start_year is not None:
        lower = config.window_start_year * 4 + config.window_start_quarter - 1
        eligible &= end.ge(lower).fillna(False)
        start = start.clip(lower=lower)
    if config.window_end_year is not None:
        upper = config.window_end_year * 4 + config.window_end_quarter - 1
        eligible &= start.le(upper).fillna(False)
        end = end.clip(upper=upper)

    attributes = data.loc[eligible, columns].reset_index(drop=True)
    starts = start.loc[eligible].to_numpy(dtype=np.int64)
    counts = end.loc[eligible].to_numpy(dtype=np.int64) - starts + 1
    offsets = np.concatenate(([0], np.cumsum(counts)))

    with path.open("w", encoding="utf-8", newline="") as output:
        pd.DataFrame(columns=[*columns, "year", "quarter"]).to_csv(output, index=False)
        first = 0
        while first < len(attributes):
            # Bound each batch by expanded rows, not by the number of input roles.
            last = max(first + 1, int(np.searchsorted(
                offsets, offsets[first] + 250_000, side="right"
            )) - 1)
            rows = np.repeat(np.arange(first, last), counts[first:last])
            periods = (
                starts[rows] + np.arange(offsets[first], offsets[last])
                - np.repeat(offsets[first:last], counts[first:last])
            )
            batch = attributes.iloc[rows].copy()
            batch["year"] = periods // 4
            batch["quarter"] = periods % 4 + 1
            batch.to_csv(output, index=False, header=False)
            first = last
    print(f"{path.name}: {offsets[-1]:,} rows", flush=True)


# ========================== OUTPUT AND RUN ==========================
def main(config: Config = RUN_CONFIG) -> None:
    """Build both interval files and their window-restricted quarterly outputs."""
    if any(
        quarter not in (1, 2, 3, 4)
        for quarter in (
            config.open_end_quarter, config.window_start_quarter,
            config.window_end_quarter,
        )
    ):
        raise ValueError("Quarter parameters must be between 1 and 4.")
    if (
        config.window_start_year is not None
        and config.window_end_year is not None
        and (config.window_start_year, config.window_start_quarter)
        > (config.window_end_year, config.window_end_quarter)
    ):
        raise ValueError("Window end must not precede window start.")

    config.output_dir.mkdir(parents=True, exist_ok=True)
    print("Loading sector lookup from complete IE input...", flush=True)
    sector_lookup = load_sector_lookup(config.ie_input_path)
    intervals = {}
    for source, path in (("ie", config.ie_input_path), ("oc", config.oc_input_path)):
        print(f"Preparing {source.upper()} role intervals...", flush=True)
        intervals[source] = prepare_intervals(source, path, config, sector_lookup)

    for source, data in intervals.items():
        path = config.output_dir / f"{SOURCE_NAMES[source]}_interval.csv"
        data.to_csv(path, index=False, chunksize=100_000)
        print(f"{path.name}: {len(data):,} rows", flush=True)
    del data

    for source in SOURCE_COLUMNS:
        data = intervals.pop(source)
        path = config.output_dir / f"{SOURCE_NAMES[source]}_record.csv"
        write_quarters(data, source, config, path)
        del data


if __name__ == "__main__":
    main()
