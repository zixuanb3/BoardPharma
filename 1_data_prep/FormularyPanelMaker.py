r"""
Purpose:
Build memory-safe formulary-level event-study panels from the expanded brand
formulary CSV. In quarterly mode, the output unit is FORMULARY_ID by company id
by NDC by quarter.
All movement event types, both A/B treatment sides, balance flags, and
direction-specific ATC3 sharing indicators are stored in one panel.

Process:
1. Read FORMULARY_ID only, split complete formularies into fixed blocks, then
   route the raw CSV into disk-backed staging blocks in one additional pass.
   During that routing pass, record each NDC's first quarter with included=1.
2. Process one complete formulary block at a time: in annual mode, place
   event flags in Q1; in quarterly mode, place them in their event quarter.
   Add calendar-year balance flags, tierA, and ATC sharing outcomes using
   event-specific counterparts. Quarterly sharing uses partner NDCs first
   seen no later than event t+3; annual sharing retains the event-year-end
   NDC rule.
3. Write one final CSV per block and immediately delete its staging file so
   the full raw panel and all configured blocks are never held in memory
   together.

Input:
- D:/pharma/formulary/task1_expanded_brand_panel.csv
- data/event_tables/movement_table_formulary_large_sample_{definition}.csv
- data/event_tables/movement_event_candidates_formulary_large_sample_{definition}.csv
- data/event_tables/movement_table_formulary_quarter_narrow.csv (quarter=1)
- data/event_tables/movement_event_candidates_formulary_quarter_narrow.csv (quarter=1)

Output:
- quarter=0: data/formulary_panel/shift_q1/formulary_panel_1.csv through
  formulary_panel_{n_formulary_blocks}.csv; metadata:
  data/formulary_metadata/ndc_first_seen_shift_q1.csv; temporary files:
  D:/pharma/formulary/formulary_panel_staging/narrow_req1_shift_q1/.
- quarter=1: data/formulary_panel_quarter/shift_q1/formulary_panel_1.csv
  through formulary_panel_{n_formulary_blocks}.csv; metadata:
  data/formulary_metadata/ndc_first_seen_quarter_shift_q1.csv; temporary
  files: D:/pharma/formulary/formulary_panel_staging_quarter/
  narrow_req1_shift_q1/.
"""

from __future__ import annotations

import gc
from contextlib import ExitStack
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
from tqdm.auto import tqdm


# Configure project directory paths
CURRENT_PATH = Path(__file__).parent.resolve()
PROJECT_ROOT = CURRENT_PATH.parent.parent
OUTPUT_BASE_PATH = PROJECT_ROOT / "data" / "formulary_panel"
FIRST_SEEN_PATH = PROJECT_ROOT / "data" / "formulary_metadata" / "ndc_first_seen.csv"
EVENT_TABLE_DIR = PROJECT_ROOT / "data" / "event_tables"

RAW_FORMULARY_PATH = Path(r"D:\pharma\formulary\task1_expanded_brand_panel.csv")
STAGING_BASE_PATH = RAW_FORMULARY_PATH.parent / "formulary_panel_staging"

PERSONNEL_DEFINITIONS = {"narrow", "medium", "broad"}
MOVEMENT_EVENTS = {
    "to_B_still_in_A",
    "to_B_not_in_A",
    "interlock_dissolution",
}
TREATMENT_GROUPS = {"A", "B"}
ATC_LEVELS = (3,)
FIRST_SEEN_QTIME_COLUMN = "_first_seen_qtime"


# ========================== USER CONFIG ==========================
# event_types:
# - Movement events to include in the single combined output panel.
# - The script writes one event column and one balance column for each
#   event_type by treatment_group combination.
#
# panel_levels:
# - Only "quarter" is supported because the raw formulary data are quarterly
#   even when the event input is annual.
#
# quarter:
# - 0: annual events in Q1; 1: actual quarterly events (narrow formulary only).
#
# stay_x_years:
# - Must match the stay column used to build the movement event inputs.
#
# balance_window:
# - Annual offsets around an event year.  (-1, 1) requires all 12 quarters
#   in event year -1, event year, and event year +1 for a formulary to be
#   considered balanced at that event year. Quarterly mode uses the exact
#   event-relative equivalent: all quarters from t-4 through t+7.
#
# treatment_groups:
# - "A" uses FirmA as the treated firm and FirmB as its candidate counterpart.
# - "B" uses FirmB as the treated firm and FirmA as its candidate counterpart.
#
# atc:
# - ATC levels for which direction-specific sharing flags are constructed.
#
# req:
# - Annual: 0, 1, or 2. Quarterly: 0 or 1. Select the matching event-table
#   flag and apply the equivalent candidate-level condition for sharing.
#
# n_formulary_blocks/chunksize:
# - The raw file is routed into n_formulary_blocks complete-formulary staging
#   files.  chunksize controls only CSV streaming memory, not output grouping.
#
# formulary_time_shift_quarters:
# - Shift raw formulary quarters before event flags are merged.  The default
#   0 preserves the current timing; 1 maps 2019Q4 formulary values to 2020Q1.
RUN_CONFIG = {
    "quarter": 1,
    "event_types": [
        "to_B_not_in_A",
        "to_B_still_in_A",
        "interlock_dissolution",
    ],
    "panel_levels": ["quarter"],
    "stay_x_years": 2,
    "balance_window": (-1, 1),
    "treatment_groups": ["A", "B"],
    "large_sample": 1,
    "personnel_definition": "narrow",
    "atc": [3],
    "req": 1,
    "formulary_time_shift_quarters": 1,
    "n_formulary_blocks": 30,
    "chunksize": 2_000_000,
}
# ===============================================================


# ========================== SHARED HELPERS ==========================


def ensure_list(value: object) -> list[object]:
    """Return a list while allowing single config values."""
    if isinstance(value, (str, int)):
        return [value]
    return list(value)  # type: ignore[arg-type]


def clean_string(series: pd.Series, uppercase: bool = False) -> pd.Series:
    """Strip a string key series and preserve blank cells as missing."""
    result = series.astype("string").str.strip()
    result = result.mask(result.eq(""), pd.NA)
    return result.str.upper() if uppercase else result


def validate_company_id(series: pd.Series, source_name: str) -> pd.Series:
    """Return positive integer company ids and reject missing or fractional values."""
    numeric = pd.to_numeric(series, errors="raise")
    invalid = numeric.isna() | numeric.le(0) | numeric.ne(numeric.round())
    if invalid.any():
        examples = series.loc[invalid].drop_duplicates().head(10).tolist()
        raise ValueError(f"{source_name}.id contains invalid values. Examples: {examples}")
    return numeric.astype("int32")


def parse_year_quarter(data: pd.DataFrame, source_name: str) -> pd.DataFrame:
    """Parse YEAR_Q values such as '2020 Q2' into integer year and quarter."""
    parsed = data["YEAR_Q"].astype("string").str.extract(r"^\s*(\d{4})\s*Q([1-4])\s*$")
    invalid = parsed[0].isna() | parsed[1].isna()
    if invalid.any():
        examples = data.loc[invalid, ["YEAR_Q"]].drop_duplicates().head(10)
        raise ValueError(f"{source_name}.YEAR_Q has invalid values. Examples:\n{examples}")
    data["year"] = parsed[0].astype("int16")
    data["quarter"] = parsed[1].astype("int8")
    return data


def apply_quarter_shift(data: pd.DataFrame, shift_quarters: int, source_name: str) -> pd.DataFrame:
    """Shift YEAR_Q labels by a fixed number of quarters in place."""
    if shift_quarters == 0:
        return data
    data = parse_year_quarter(data, source_name)
    qtime = data["year"].astype("int32") * 4 + data["quarter"].astype("int32") + shift_quarters
    if qtime.le(0).any():
        examples = data.loc[qtime.le(0), ["YEAR_Q"]].drop_duplicates().head(10)
        raise ValueError(
            f"{source_name}.YEAR_Q cannot be shifted by {shift_quarters} quarters. "
            f"Examples:\n{examples}"
        )
    shifted_year = ((qtime - 1) // 4).astype("int16")
    shifted_quarter = (qtime - shifted_year.astype("int32") * 4).astype("int8")
    data["year"] = shifted_year
    data["quarter"] = shifted_quarter
    data["YEAR_Q"] = shifted_year.astype("string") + " Q" + shifted_quarter.astype("string")
    return data


def event_column(event_type: str, treatment_group: str) -> str:
    """Return the combined-panel event column name for one event direction."""
    return f"event_{event_type}_{treatment_group}"


def balance_column(event_type: str, treatment_group: str) -> str:
    """Return the corresponding event-specific balanced-panel column name."""
    return f"{event_column(event_type, treatment_group)}_balanced"


def sharing_column(event_type: str, treatment_group: str, atc_level: int) -> str:
    """Return the direction-specific sharing column name for one ATC level."""
    return f"{event_column(event_type, treatment_group)}_sharingATC{atc_level}"


def movement_suffix(large_sample: int, personnel_definition: str, quarter: int = 0) -> str:
    """Return the formulary movement-event suffix for the selected definition."""
    if quarter not in {0, 1}:
        raise ValueError("quarter must be 0 or 1")
    if quarter:
        if personnel_definition != "narrow":
            raise ValueError("Quarterly formulary events require narrow personnel")
        return "_formulary_quarter_narrow"
    if large_sample != 1:
        raise ValueError("FormularyPanelMaker requires large_sample == 1.")
    if personnel_definition not in PERSONNEL_DEFINITIONS:
        raise ValueError("personnel_definition must be narrow, medium, or broad")
    return f"_formulary_large_sample_{personnel_definition}"


def shift_label(shift_quarters: int) -> str:
    """Return the folder/file label for a formulary quarter shift."""
    return f"shift_q{shift_quarters:+d}".replace("+", "")


def output_base_path(shift_quarters: int, quarter: int = 0) -> Path:
    """Return the panel output directory for one timing specification."""
    base = OUTPUT_BASE_PATH.with_name("formulary_panel_quarter") if quarter else OUTPUT_BASE_PATH
    return base if shift_quarters == 0 else base / shift_label(shift_quarters)


def first_seen_path(shift_quarters: int, quarter: int = 0) -> Path:
    """Return the NDC first-seen path for one timing specification."""
    base = FIRST_SEEN_PATH.with_name("ndc_first_seen_quarter.csv") if quarter else FIRST_SEEN_PATH
    if shift_quarters == 0:
        return base
    return base.with_name(f"{base.stem}_{shift_label(shift_quarters)}.csv")


def validate_config(config: dict[str, object]) -> tuple[
    list[str], list[str], int, tuple[int, int], int, str, tuple[int, ...], int, int, int, int
]:
    """Validate RUN_CONFIG and return normalized values used by the builder."""
    quarter = int(config["quarter"])
    if quarter not in {0, 1}:
        raise ValueError("quarter must be 0 or 1")
    event_types = [str(value) for value in ensure_list(config["event_types"])]
    invalid_events = sorted(set(event_types) - MOVEMENT_EVENTS)
    if invalid_events:
        raise ValueError(f"Unsupported movement events: {invalid_events}")

    panel_levels = [str(value).lower() for value in ensure_list(config["panel_levels"])]
    if panel_levels != ["quarter"]:
        raise ValueError("FormularyPanelMaker currently supports panel_levels == ['quarter'] only.")

    treatment_groups = [str(value).upper() for value in ensure_list(config["treatment_groups"])]
    if set(treatment_groups) != TREATMENT_GROUPS or len(treatment_groups) != 2:
        raise ValueError("treatment_groups must contain exactly ['A', 'B'].")

    balance_window = tuple(int(value) for value in config["balance_window"])  # type: ignore[arg-type]
    if len(balance_window) != 2 or balance_window[0] > balance_window[1]:
        raise ValueError("balance_window must be a two-value tuple with start <= end.")

    atc_levels = tuple(sorted({int(value) for value in ensure_list(config["atc"])}))
    if not atc_levels or set(atc_levels) - set(ATC_LEVELS):
        raise ValueError("atc must contain ATC level 3 only.")

    req = int(config["req"])
    if req not in ({0, 1} if quarter else {0, 1, 2}):
        raise ValueError("req must be 0 or 1 in quarterly mode, or 0, 1, or 2 in annual mode.")

    time_shift = int(config["formulary_time_shift_quarters"])

    n_blocks = int(config["n_formulary_blocks"])
    if n_blocks < 1:
        raise ValueError("n_formulary_blocks must be at least 1.")

    chunksize = int(config["chunksize"])
    if chunksize < 1:
        raise ValueError("chunksize must be at least 1.")

    stay_x_years = int(config["stay_x_years"])
    if stay_x_years < 1:
        raise ValueError("stay_x_years must be at least 1.")

    personnel_definition = str(config["personnel_definition"])
    movement_suffix(int(config["large_sample"]), personnel_definition, quarter)
    return (
        event_types,
        treatment_groups,
        stay_x_years,
        balance_window,
        int(config["large_sample"]),
        personnel_definition,
        atc_levels,
        req,
        time_shift,
        n_blocks,
        quarter,
    )


# ========================== DATA LOADERS ==========================


def required_input_columns(atc_levels: Iterable[int], quarter: int = 0) -> set[str]:
    """Return columns that must exist in the raw formulary CSV."""
    return {
        "YEAR_Q",
        "FORMULARY_ID",
        "id" if quarter else "BoardName",
        "NDC",
        "included",
        "tier_raw",
        "max_tier",
        *(f"ATC{level}" for level in atc_levels),
    }


def validate_raw_schema(atc_levels: Iterable[int], quarter: int = 0) -> None:
    """Read only the header and validate raw formulary fields needed downstream."""
    if not RAW_FORMULARY_PATH.exists():
        raise FileNotFoundError(f"Raw formulary panel not found: {RAW_FORMULARY_PATH}")
    columns = list(pd.read_csv(RAW_FORMULARY_PATH, nrows=0).columns)
    missing = sorted(required_input_columns(atc_levels, quarter) - set(columns))
    if missing:
        raise KeyError(f"Raw formulary panel is missing required columns: {missing}")


def load_event_flags(
    event_types: list[str],
    treatment_groups: list[str],
    req: int,
    suffix: str,
    quarter: int = 0,
) -> tuple[pd.DataFrame, dict[str, set[int]]]:
    """Load firm-year or firm-quarter event flags for each type and side."""
    path = EVENT_TABLE_DIR / f"movement_table{suffix}.csv"
    if not path.exists():
        raise FileNotFoundError(f"Movement event table not found: {path}")

    event_table = pd.read_csv(path, dtype="string")
    firm_column = "id" if quarter else "BoardName"
    required = {firm_column, "year", "event_type", "firm_type", f"req{req}"}
    if quarter:
        required.add("quarter")
    missing = sorted(required - set(event_table.columns))
    if missing:
        raise KeyError(f"{path.name} is missing columns: {missing}")

    if quarter:
        event_table[firm_column] = validate_company_id(event_table[firm_column], path.name)
    else:
        event_table[firm_column] = clean_string(event_table[firm_column], uppercase=True)
    event_table["event_type"] = clean_string(event_table["event_type"])
    event_table["firm_type"] = clean_string(event_table["firm_type"], uppercase=True)
    event_table["year"] = pd.to_numeric(event_table["year"], errors="raise").astype("int16")
    if quarter:
        event_table["quarter"] = validate_event_quarter(event_table["quarter"], path.name)
    event_table[f"req{req}"] = pd.to_numeric(event_table[f"req{req}"], errors="raise").astype("int8")

    flag_parts: list[pd.DataFrame] = []
    event_years: dict[str, set[int]] = {}
    keys = [firm_column, "year", "quarter"] if quarter else [firm_column, "year"]
    for event_type in event_types:
        for treatment_group in treatment_groups:
            column = event_column(event_type, treatment_group)
            flagged = event_table.loc[
                event_table["event_type"].eq(event_type)
                & event_table["firm_type"].eq(treatment_group)
                & event_table[f"req{req}"].eq(1),
                keys,
            ].drop_duplicates()
            flagged[column] = np.int8(1)
            event_years[column] = set(flagged["year"].astype(int).tolist())
            flag_parts.append(flagged)

    if not flag_parts:
        return pd.DataFrame(columns=keys), event_years

    flags = flag_parts[0]
    for flagged in flag_parts[1:]:
        flags = flags.merge(flagged, on=keys, how="outer", validate="one_to_one")
    event_columns = [event_column(event_type, side) for event_type in event_types for side in treatment_groups]
    flags[event_columns] = flags[event_columns].fillna(0).astype("int8")
    return flags, event_years


def validate_event_quarter(values: pd.Series, source_name: str) -> pd.Series:
    """Require event quarters to be nonmissing integers from 1 through 4."""
    numeric = pd.to_numeric(values, errors="raise")
    if numeric.isna().any() or not numeric.isin([1, 2, 3, 4]).all():
        raise ValueError(f"{source_name}: event quarter must be 1, 2, 3, or 4")
    return numeric.astype("int8")


def candidate_condition(data: pd.DataFrame, req: int, side: str, stay_column: str) -> pd.Series:
    """Return the candidate-level condition that exactly corresponds to req0/1/2."""
    condition = data[stay_column].eq(1)
    if req >= 1:
        condition &= data["requirement1"].eq(1)
    if req == 2:
        condition &= data[f"requirement2_{side}"].eq(1)
    return condition


def load_candidate_pairs(
    event_types: list[str],
    treatment_groups: list[str],
    req: int,
    stay_x_years: int,
    suffix: str,
    event_flags: pd.DataFrame,
    quarter: int = 0,
) -> dict[str, pd.DataFrame]:
    """Load valid directional candidate pairs for ATC overlap construction."""
    path = EVENT_TABLE_DIR / f"movement_event_candidates{suffix}.csv"
    if not path.exists():
        raise FileNotFoundError(f"Movement event candidates not found: {path}")

    candidates = pd.read_csv(path, dtype="string")
    stay_column = f"stay_{stay_x_years * 4}_quarters" if quarter else f"stay_{stay_x_years}_years"
    source_a, source_b = ("idA", "idB") if quarter else ("FirmA", "FirmB")
    firm_column = "id" if quarter else "BoardName"
    pair_column = "idPair" if quarter else "BoardNamePair"
    required = {
        "event_type",
        "event_year",
        source_a,
        source_b,
        stay_column,
        "requirement1",
    }
    if quarter:
        required.add("event_quarter")
    else:
        required.update(("requirement2_A", "requirement2_B"))
    missing = sorted(required - set(candidates.columns))
    if missing:
        raise KeyError(f"{path.name} is missing columns: {missing}")

    candidates["event_type"] = clean_string(candidates["event_type"])
    candidates["event_year"] = pd.to_numeric(candidates["event_year"], errors="raise").astype("int16")
    if quarter:
        candidates["event_quarter"] = validate_event_quarter(candidates["event_quarter"], path.name)
    for column in (source_a, source_b):
        if quarter:
            candidates[column] = validate_company_id(candidates[column], path.name)
        else:
            candidates[column] = clean_string(candidates[column], uppercase=True)
    condition_columns = (stay_column, "requirement1") if quarter else (
        stay_column, "requirement1", "requirement2_A", "requirement2_B"
    )
    for column in condition_columns:
        candidates[column] = pd.to_numeric(candidates[column], errors="raise").astype("int8")

    pairs_by_event: dict[str, pd.DataFrame] = {}
    keys = [firm_column, "year", "quarter"] if quarter else [firm_column, "year"]
    candidate_columns = ["event_year", "event_quarter"] if quarter else ["event_year"]
    for event_type in event_types:
        for side in treatment_groups:
            column = event_column(event_type, side)
            subset = candidates.loc[
                candidates["event_type"].eq(event_type)
                & candidate_condition(candidates, req, side, stay_column),
                [*candidate_columns, source_a, source_b],
            ].dropna()

            if side == "A":
                pairs = subset.rename(columns={
                    "event_year": "year",
                    source_a: firm_column,
                    source_b: pair_column,
                })
            else:
                pairs = subset.rename(columns={
                    "event_year": "year",
                    source_b: firm_column,
                    source_a: pair_column,
                })
            if quarter:
                pairs = pairs.rename(columns={"event_quarter": "quarter"})
            pairs = pairs.drop_duplicates().reset_index(drop=True)

            event_keys = event_flags.loc[event_flags[column].eq(1), keys].drop_duplicates()
            candidate_keys = pairs[keys].drop_duplicates()
            missing_keys = event_keys.merge(
                candidate_keys,
                on=keys,
                how="left",
                indicator=True,
            )
            if missing_keys["_merge"].eq("left_only").any():
                examples = missing_keys.loc[
                    missing_keys["_merge"].eq("left_only"), keys
                ].head(10)
                raise ValueError(
                    f"{column} has valid event-table keys missing from matching candidates. "
                    f"Do not mix event inputs across personnel definitions. Examples:\n{examples}"
                )
            pairs_by_event[column] = pairs
    return pairs_by_event


# ========================== BLOCK STAGING ==========================


def build_formulary_blocks(n_blocks: int, chunksize: int) -> dict[str, int]:
    """Read only FORMULARY_ID and assign every complete formulary to one block."""
    formulary_ids: set[str] = set()
    reader = pd.read_csv(
        RAW_FORMULARY_PATH,
        usecols=["FORMULARY_ID"],
        dtype="string",
        chunksize=chunksize,
    )
    for chunk in tqdm(reader, desc="Pass 1/2: reading FORMULARY_ID", unit="chunk"):
        clean_ids = clean_string(chunk["FORMULARY_ID"])
        if clean_ids.isna().any():
            raise ValueError("Raw formulary data contain missing FORMULARY_ID values.")
        formulary_ids.update(clean_ids.astype(str).unique().tolist())
        del chunk, clean_ids
        gc.collect()

    if len(formulary_ids) < n_blocks:
        raise ValueError(
            f"Only {len(formulary_ids)} unique formularies are available for {n_blocks} requested blocks."
        )

    block_lookup: dict[str, int] = {}
    for block_number, block_ids in enumerate(np.array_split(np.array(sorted(formulary_ids)), n_blocks), start=1):
        for formulary_id in block_ids.tolist():
            block_lookup[str(formulary_id)] = block_number
    return block_lookup


def staging_directory(
    personnel_definition: str, req: int, shift_quarters: int, quarter: int = 0,
) -> Path:
    """Return a run-specific disk staging directory beside the large raw input."""
    suffix = f"{personnel_definition}_req{req}"
    if shift_quarters != 0:
        suffix = f"{suffix}_{shift_label(shift_quarters)}"
    base = STAGING_BASE_PATH.with_name("formulary_panel_staging_quarter") if quarter else STAGING_BASE_PATH
    return base / suffix


def stage_paths(stage_dir: Path, n_blocks: int) -> dict[int, Path]:
    """Return all temporary source-block paths for one run."""
    return {block: stage_dir / f"formulary_stage_{block}.csv" for block in range(1, n_blocks + 1)}


def update_first_seen_lookup(
    chunk: pd.DataFrame,
    first_seen_qtime: dict[str, int],
    observed_ndcs: set[str],
    included_ndcs_by_quarter: dict[int, set[str]] | None = None,
) -> None:
    """Update the earliest included quarter for every NDC in one raw chunk."""
    chunk["NDC"] = clean_string(chunk["NDC"])
    if chunk["NDC"].isna().any():
        raise ValueError("Raw formulary data contain missing NDC values.")
    observed_ndcs.update(chunk["NDC"].astype(str).unique().tolist())

    included = pd.to_numeric(chunk["included"], errors="coerce")
    invalid = included.isna() | ~included.isin([0, 1])
    if invalid.any():
        examples = chunk.loc[invalid, ["included"]].drop_duplicates().head(10)
        raise ValueError(f"Raw formulary data contain invalid included values. Examples:\n{examples}")

    actual = chunk.loc[included.eq(1), ["YEAR_Q", "NDC"]].copy()
    if actual.empty:
        return
    actual = parse_year_quarter(actual, RAW_FORMULARY_PATH.name)
    actual["qtime"] = actual["year"].astype("int32") * 4 + actual["quarter"].astype("int32")
    if included_ndcs_by_quarter is not None:
        for qtime, group in actual.groupby("qtime"):
            included_ndcs_by_quarter.setdefault(int(qtime), set()).update(group["NDC"].astype(str).unique())
    chunk_minimums = actual.groupby("NDC")["qtime"].min()
    for ndc, qtime in chunk_minimums.items():
        key = str(ndc)
        first_seen_qtime[key] = min(first_seen_qtime.get(key, int(qtime)), int(qtime))


def first_seen_frame(first_seen_qtime: dict[str, int]) -> pd.DataFrame:
    """Return a readable, chronologically encoded NDC first-seen table."""
    result = pd.DataFrame(
        sorted(first_seen_qtime.items()),
        columns=["NDC", "first_seen_qtime"],
    )
    result["first_seen_year"] = ((result["first_seen_qtime"] - 1) // 4).astype("int16")
    result["first_seen_quarter"] = (
        result["first_seen_qtime"] - result["first_seen_year"].astype("int32") * 4
    ).astype("int8")
    result["first_seen_YEAR_Q"] = (
        result["first_seen_year"].astype("string")
        + " Q"
        + result["first_seen_quarter"].astype("string")
    )
    return result[
        ["NDC", "first_seen_YEAR_Q", "first_seen_year", "first_seen_quarter", "first_seen_qtime"]
    ]


def save_first_seen_lookup(first_seen_qtime: dict[str, int], output_path: Path) -> None:
    """Save the compact NDC first-seen lookup, replacing prior output."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    first_seen_frame(first_seen_qtime).to_csv(output_path, index=False)


def create_staging_blocks(
    block_lookup: dict[str, int],
    n_blocks: int,
    chunksize: int,
    stage_dir: Path,
    shift_quarters: int,
    quarter: int = 0,
) -> tuple[dict[int, Path], dict[str, int], dict[int, set[str]]]:
    """Route raw rows to staging files and collect NDC first-seen quarters."""
    stage_dir.mkdir(parents=True, exist_ok=True)
    paths = stage_paths(stage_dir, n_blocks)
    for path in paths.values():
        if path.exists():
            path.unlink()

    headers_written = {block: False for block in paths}
    first_seen_qtime: dict[str, int] = {}
    included_ndcs_by_quarter: dict[int, set[str]] = {}
    observed_ndcs: set[str] = set()
    reader = pd.read_csv(RAW_FORMULARY_PATH, dtype="string", chunksize=chunksize)
    with ExitStack() as stack:
        handles = {
            block: stack.enter_context(path.open("w", encoding="utf-8", newline=""))
            for block, path in paths.items()
        }
        for chunk in tqdm(reader, desc="Pass 2/2: routing formulary chunks", unit="chunk"):
            chunk = apply_quarter_shift(chunk, shift_quarters, RAW_FORMULARY_PATH.name)
            update_first_seen_lookup(
                chunk, first_seen_qtime, observed_ndcs,
                included_ndcs_by_quarter if quarter else None,
            )
            chunk["FORMULARY_ID"] = clean_string(chunk["FORMULARY_ID"])
            block_id = chunk["FORMULARY_ID"].map(block_lookup)
            if block_id.isna().any():
                examples = chunk.loc[block_id.isna(), ["FORMULARY_ID"]].drop_duplicates().head(10)
                raise KeyError(f"FORMULARY_ID values missing from block map. Examples:\n{examples}")
            chunk["_block"] = block_id.astype("int8")

            for block, subset in chunk.groupby("_block", sort=False):
                subset = subset.drop(columns="_block")
                subset.to_csv(
                    handles[int(block)],
                    index=False,
                    header=not headers_written[int(block)],
                )
                headers_written[int(block)] = True
                del subset
            del chunk, block_id
            gc.collect()

    missing_blocks = [block for block, wrote_header in headers_written.items() if not wrote_header]
    if missing_blocks:
        raise RuntimeError(f"No raw rows were routed to blocks: {missing_blocks}")
    missing_first_seen = sorted(observed_ndcs - set(first_seen_qtime))
    if missing_first_seen:
        raise ValueError(
            "Some expanded NDCs never have included=1, so first-seen timing is undefined. "
            f"Examples: {missing_first_seen[:10]}"
        )
    if not first_seen_qtime:
        raise ValueError("No included NDC rows were found while building the first-seen lookup.")
    return paths, first_seen_qtime, included_ndcs_by_quarter


# ========================== PANEL CONSTRUCTION ==========================


def add_event_flags(
    data: pd.DataFrame, event_flags: pd.DataFrame, event_columns: list[str], quarter: int = 0,
) -> pd.DataFrame:
    """Merge firm events in Q1 (annual) or their actual quarter (quarterly)."""
    firm_column = "id" if quarter else "BoardName"
    keys = [firm_column, "year", "quarter"] if quarter else [firm_column, "year"]
    result = data.merge(event_flags, on=keys, how="left", validate="many_to_one")
    result[event_columns] = result[event_columns].fillna(0).astype("int8")
    if not quarter:
        result.loc[result["quarter"].ne(1), event_columns] = 0
    return result


def balanced_formulary_years(
    data: pd.DataFrame,
    event_years: set[int],
    balance_window: tuple[int, int],
) -> set[tuple[str, int]]:
    """Return formula-year pairs with complete quarterly support in the balance window."""
    if not event_years:
        return set()

    start_offset, end_offset = balance_window
    presence = data[["FORMULARY_ID", "year", "quarter"]].drop_duplicates().copy()
    presence["qtime"] = presence["year"].astype("int32") * 4 + presence["quarter"].astype("int32")

    balanced: set[tuple[str, int]] = set()
    for event_year in sorted(event_years):
        required_periods = {
            year * 4 + quarter
            for year in range(event_year + start_offset, event_year + end_offset + 1)
            for quarter in range(1, 5)
        }
        counts = (
            presence.loc[presence["qtime"].isin(required_periods)]
            .groupby("FORMULARY_ID")["qtime"]
            .nunique()
        )
        balanced.update((str(formulary_id), event_year) for formulary_id in counts[counts.eq(len(required_periods))].index)
    return balanced


def balanced_formulary_quarters(
    data: pd.DataFrame,
    event_qtimes: set[int],
) -> set[tuple[str, int]]:
    """Return formulary-event-quarter pairs observed throughout event t-4 through t+7."""
    if not event_qtimes:
        return set()

    presence = data[["FORMULARY_ID", "year", "quarter"]].drop_duplicates().copy()
    presence["qtime"] = presence["year"].astype("int32") * 4 + presence["quarter"].astype("int32")

    balanced: set[tuple[str, int]] = set()
    for event_qtime in sorted(event_qtimes):
        required_periods = set(range(event_qtime - 4, event_qtime + 8))
        counts = (
            presence.loc[presence["qtime"].isin(required_periods)]
            .groupby("FORMULARY_ID")["qtime"]
            .nunique()
        )
        balanced.update(
            (str(formulary_id), event_qtime)
            for formulary_id in counts[counts.eq(len(required_periods))].index
        )
    return balanced


def add_balance_flags(
    data: pd.DataFrame,
    event_types: list[str],
    treatment_groups: list[str],
    event_years: dict[str, set[int]],
    balance_window: tuple[int, int],
    progress: tqdm | None = None,
    quarter: int = 0,
) -> pd.DataFrame:
    """Add balance flags using annual years or event-relative quarterly windows."""
    if quarter:
        row_qtime = data["year"].astype("int32") * 4 + data["quarter"].astype("int32")
        event_columns = [
            event_column(event_type, treatment_group)
            for event_type in event_types
            for treatment_group in treatment_groups
        ]
        event_qtimes = set(
            row_qtime.loc[data[event_columns].eq(1).any(axis=1)].astype(int).unique()
        )
        balanced_pairs = balanced_formulary_quarters(data, event_qtimes)
        formula_quarter = pd.DataFrame({
            "FORMULARY_ID": data["FORMULARY_ID"],
            "event_qtime": row_qtime,
        })
        balanced_mask = pd.MultiIndex.from_frame(formula_quarter).isin(balanced_pairs)
    else:
        all_years = set().union(*event_years.values()) if event_years else set()
        balanced_pairs = balanced_formulary_years(data, all_years, balance_window)
        formula_year_index = pd.MultiIndex.from_frame(data[["FORMULARY_ID", "year"]])
        balanced_mask = formula_year_index.isin(balanced_pairs)

    for event_type in event_types:
        for treatment_group in treatment_groups:
            event_col = event_column(event_type, treatment_group)
            data[balance_column(event_type, treatment_group)] = (
                data[event_col].eq(1) & balanced_mask
            ).astype("int8")
            if progress is not None:
                progress.set_postfix_str(f"balance {event_type}/{treatment_group}")
                progress.update(1)
    return data


def explode_atc_codes(data: pd.DataFrame, value_column: str, id_columns: list[str]) -> pd.DataFrame:
    """Expand semicolon-delimited ATC codes, preserving only nonempty atomic values."""
    work = data[id_columns + [value_column]].dropna(subset=[value_column]).copy()
    if work.empty:
        return pd.DataFrame(columns=[*id_columns, "atc_code"])
    work["atc_code"] = work[value_column].astype("string").str.split(";")
    work = work.drop(columns=value_column).explode("atc_code", ignore_index=True)
    work["atc_code"] = clean_string(work["atc_code"])
    return work.dropna(subset=["atc_code"]).drop_duplicates().reset_index(drop=True)


def available_by_event_year_end(data: pd.DataFrame) -> pd.Series:
    """Return whether each NDC had appeared by the end of its row year."""
    event_year_end_qtime = data["year"].astype("int32") * 4 + 4
    return data[FIRST_SEEN_QTIME_COLUMN].le(event_year_end_qtime)


def partner_atc_codes(
    data: pd.DataFrame,
    partner_scope: pd.DataFrame,
    atc_column: str,
    partner_available_mask: pd.Series,
) -> pd.DataFrame:
    """Return available atomic year-partner-ATC codes for sharing comparisons."""
    if partner_scope.empty:
        return pd.DataFrame(columns=["year", "BoardNamePair", "atc_code"])
    scoped = data.loc[
        partner_available_mask,
        ["year", "BoardName", atc_column],
    ].merge(
        partner_scope,
        on=["year", "BoardName"],
        how="inner",
        validate="many_to_one",
    )
    atoms = explode_atc_codes(scoped, atc_column, ["year", "BoardName"])
    return atoms.rename(columns={"BoardName": "BoardNamePair"}).drop_duplicates()


def partner_atc_codes_quarter(
    data: pd.DataFrame,
    partner_scope: pd.DataFrame,
    atc_column: str,
) -> pd.DataFrame:
    """Collect partner ATC codes from NDCs first seen by event t+3."""
    output_columns = ["year", "quarter", "idPair", "atc_code"]
    if partner_scope.empty:
        return pd.DataFrame(columns=output_columns)

    scope = partner_scope.rename(columns={"idPair": "id"}).copy()
    scope["event_qtime"] = scope["year"].astype("int32") * 4 + scope["quarter"].astype("int32")
    source = data.loc[
        data[atc_column].notna(),
        ["year", "quarter", "id", "NDC", atc_column, FIRST_SEEN_QTIME_COLUMN],
    ].drop_duplicates()
    source = source.loc[source["id"].isin(scope["id"])].drop_duplicates()
    if source.empty:
        return pd.DataFrame(columns=output_columns)

    scoped = source.merge(
        scope,
        on=["year", "quarter", "id"],
        how="inner",
        validate="many_to_many",
    )
    scoped = scoped.loc[
        scoped[FIRST_SEEN_QTIME_COLUMN].le(scoped["event_qtime"] + 3)
    ].copy()
    if scoped.empty:
        return pd.DataFrame(columns=output_columns)

    atoms = explode_atc_codes(scoped, atc_column, ["year", "quarter", "id"])
    return atoms.rename(columns={"id": "idPair"})[output_columns].drop_duplicates()


def add_sharing_flags(
    data: pd.DataFrame,
    event_types: list[str],
    treatment_groups: list[str],
    atc_levels: tuple[int, ...],
    candidate_pairs: dict[str, pd.DataFrame],
    progress: tqdm | None = None,
    quarter: int = 0,
    included_ndcs_by_quarter: dict[int, set[str]] | None = None,
) -> pd.DataFrame:
    """Add direction-specific event ATC overlap flags without duplicating outcome rows."""
    data["_row_id"] = np.arange(len(data), dtype=np.int64)
    all_pairs = pd.concat(candidate_pairs.values(), ignore_index=True)
    pair_column = "idPair" if quarter else "BoardNamePair"
    firm_column = "id" if quarter else "BoardName"
    partner_keys = ["year", "quarter", pair_column] if quarter else ["year", pair_column]
    partner_scope = all_pairs[partner_keys].drop_duplicates()
    if not quarter:
        partner_scope = partner_scope.rename(columns={"BoardNamePair": "BoardName"})
        partner_available_mask = available_by_event_year_end(data)
    event_keys = ["year", "quarter", firm_column] if quarter else ["year", firm_column]

    for atc_level in atc_levels:
        atc_column = f"ATC{atc_level}"
        if quarter:
            partners = partner_atc_codes_quarter(
                data, partner_scope, atc_column,
            )
        else:
            partners = partner_atc_codes(
                data, partner_scope, atc_column, partner_available_mask,
            )
        if progress is not None:
            progress.set_postfix_str(f"ATC{atc_level}: preparing partner products")
            progress.update(1)
        for event_type in event_types:
            for treatment_group in treatment_groups:
                event_col = event_column(event_type, treatment_group)
                share_col = sharing_column(event_type, treatment_group, atc_level)
                data[share_col] = np.int8(0)
                pairs = candidate_pairs[event_col]
                event_rows = data.loc[
                    data[event_col].eq(1),
                    ["_row_id", *event_keys, atc_column],
                ]
                if not event_rows.empty and not pairs.empty and not partners.empty:
                    event_atoms = explode_atc_codes(
                        event_rows,
                        atc_column,
                        ["_row_id", *event_keys],
                    )
                    if not event_atoms.empty:
                        paired_codes = event_atoms.merge(
                            pairs,
                            on=event_keys,
                            how="inner",
                            validate="many_to_many",
                        )
                        matches = paired_codes.merge(
                            partners,
                            on=["year", "quarter", pair_column, "atc_code"] if quarter
                            else ["year", "BoardNamePair", "atc_code"],
                            how="inner",
                            validate="many_to_many",
                        )
                        if not matches.empty:
                            data.loc[matches["_row_id"].unique(), share_col] = np.int8(1)
                        del paired_codes, matches
                    del event_atoms
                if progress is not None:
                    progress.set_postfix_str(f"ATC{atc_level}: {event_type}/{treatment_group}")
                    progress.update(1)
                gc.collect()
        del partners
        gc.collect()

    data.drop(columns="_row_id", inplace=True)
    return data


def add_tier_a(data: pd.DataFrame) -> pd.DataFrame:
    """Copy tier_raw and fill uncovered rows with the supplied max_tier plus one."""
    data["tier_raw"] = pd.to_numeric(data["tier_raw"], errors="coerce")
    data["max_tier"] = pd.to_numeric(data["max_tier"], errors="raise")

    missing_max = data["max_tier"].isna()
    if missing_max.any():
        examples = data.loc[
            missing_max, ["FORMULARY_ID", "YEAR_Q"]
        ].drop_duplicates().head(10)
        raise ValueError(f"max_tier is missing for some formulary-quarters. Examples:\n{examples}")

    max_tier_counts = data.groupby(["FORMULARY_ID", "YEAR_Q"])["max_tier"].nunique()
    inconsistent = max_tier_counts[max_tier_counts.ne(1)]
    if not inconsistent.empty:
        raise ValueError(
            "max_tier is not unique within some FORMULARY_ID by YEAR_Q groups. "
            f"Examples:\n{inconsistent.head(10)}"
        )

    tier_exceeds_max = data["tier_raw"].notna() & data["tier_raw"].gt(data["max_tier"])
    if tier_exceeds_max.any():
        examples = data.loc[
            tier_exceeds_max,
            ["FORMULARY_ID", "YEAR_Q", "NDC", "tier_raw", "max_tier"],
        ].head(10)
        raise ValueError(f"tier_raw exceeds max_tier. Examples:\n{examples}")

    data["tierA"] = data["tier_raw"].fillna(data["max_tier"] + 1)

    unresolved = data["tierA"].isna()
    if unresolved.any():
        examples = data.loc[unresolved, ["FORMULARY_ID", "YEAR_Q"]].drop_duplicates().head(10)
        raise ValueError(
            f"tierA could not be constructed. Examples:\n{examples}"
        )
    return data


def process_block(
    stage_path: Path,
    output_path: Path,
    block_number: int,
    event_flags: pd.DataFrame,
    event_types: list[str],
    treatment_groups: list[str],
    event_years: dict[str, set[int]],
    balance_window: tuple[int, int],
    atc_levels: tuple[int, ...],
    candidate_pairs: dict[str, pd.DataFrame],
    first_seen_qtime: dict[str, int],
    quarter: int = 0,
    included_ndcs_by_quarter: dict[int, set[str]] | None = None,
) -> None:
    """Build and save one complete-formulary block, then leave no large object in memory."""
    n_event_columns = len(event_types) * len(treatment_groups)
    total_steps = 5 + n_event_columns + len(atc_levels) * (1 + n_event_columns)
    with tqdm(total=total_steps, desc=f"Block {block_number}: building panel", unit="step", leave=False) as progress:
        progress.set_postfix_str("loading staging CSV")
        data = pd.read_csv(stage_path, dtype="string")
        progress.update(1)

        progress.set_postfix_str("validating identifiers and quarters")
        data["FORMULARY_ID"] = clean_string(data["FORMULARY_ID"])
        data["NDC"] = clean_string(data["NDC"])
        firm_column = "id" if quarter else "BoardName"
        if quarter:
            data[firm_column] = validate_company_id(data[firm_column], stage_path.name)
        else:
            data[firm_column] = clean_string(data[firm_column], uppercase=True)
            data = data.dropna(subset=[firm_column])
        required_identifiers = ["FORMULARY_ID", firm_column, "NDC"]
        if data[required_identifiers].isna().any().any():
            raise ValueError(
                f"{stage_path.name} contains missing values in {required_identifiers}."
            )
        data = parse_year_quarter(data, stage_path.name)
        data[FIRST_SEEN_QTIME_COLUMN] = data["NDC"].map(first_seen_qtime)
        if data[FIRST_SEEN_QTIME_COLUMN].isna().any():
            examples = data.loc[
                data[FIRST_SEEN_QTIME_COLUMN].isna(), ["NDC"]
            ].drop_duplicates().head(10)
            raise KeyError(f"NDC values are missing from the first-seen lookup. Examples:\n{examples}")
        data[FIRST_SEEN_QTIME_COLUMN] = data[FIRST_SEEN_QTIME_COLUMN].astype("int32")
        progress.update(1)

        progress.set_postfix_str("merging event indicators")
        event_columns = [event_column(event_type, side) for event_type in event_types for side in treatment_groups]
        data = add_event_flags(data, event_flags, event_columns, quarter)
        progress.update(1)

        progress.set_postfix_str("checking balance-window coverage")
        data = add_balance_flags(
            data,
            event_types,
            treatment_groups,
            event_years,
            balance_window,
            progress,
            quarter,
        )
        data = add_sharing_flags(
            data,
            event_types,
            treatment_groups,
            atc_levels,
            candidate_pairs,
            progress,
            quarter,
            included_ndcs_by_quarter,
        )
        data.drop(columns=FIRST_SEEN_QTIME_COLUMN, inplace=True)

        progress.set_postfix_str("constructing tierA")
        data = add_tier_a(data)
        progress.update(1)

        progress.set_postfix_str("writing final CSV")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        data.to_csv(output_path, index=False)
        progress.update(1)
    del data
    gc.collect()


# ========================== OUTPUT DISPATCH ==========================


def main() -> None:
    """Build the configured complete-formulary output blocks for this specification."""
    (
        event_types,
        treatment_groups,
        stay_x_years,
        balance_window,
        large_sample,
        personnel_definition,
        atc_levels,
        req,
        time_shift,
        n_blocks,
        quarter,
    ) = validate_config(RUN_CONFIG)
    chunksize = int(RUN_CONFIG["chunksize"])
    validate_raw_schema(atc_levels, quarter)
    suffix = movement_suffix(large_sample, personnel_definition, quarter)

    event_flags, event_years = load_event_flags(event_types, treatment_groups, req, suffix, quarter)
    candidate_pairs = load_candidate_pairs(
        event_types,
        treatment_groups,
        req,
        stay_x_years,
        suffix,
        event_flags,
        quarter,
    )

    print(
        "Building formulary panels: "
        f"quarter={quarter}, definition={personnel_definition}, req{req}, "
        f"{shift_label(time_shift)}, blocks={n_blocks}, "
        f"balance_window=t{balance_window[0]:+d}..t{balance_window[1]:+d}"
    )
    block_lookup = build_formulary_blocks(n_blocks, chunksize)
    stage_dir = staging_directory(personnel_definition, req, time_shift, quarter)
    paths, first_seen_qtime, included_ndcs_by_quarter = create_staging_blocks(
        block_lookup,
        n_blocks,
        chunksize,
        stage_dir,
        time_shift,
        quarter,
    )
    first_seen_output_path = first_seen_path(time_shift, quarter)
    save_first_seen_lookup(first_seen_qtime, first_seen_output_path)

    panel_output_dir = output_base_path(time_shift, quarter)
    panel_output_dir.mkdir(parents=True, exist_ok=True)
    for block_number in tqdm(range(1, n_blocks + 1), desc="Processing formulary blocks", unit="block"):
        stage_path = paths[block_number]
        output_path = panel_output_dir / f"formulary_panel_{block_number}.csv"
        if output_path.exists():
            output_path.unlink()
        process_block(
            stage_path=stage_path,
            output_path=output_path,
            block_number=block_number,
            event_flags=event_flags,
            event_types=event_types,
            treatment_groups=treatment_groups,
            event_years=event_years,
            balance_window=balance_window,
            atc_levels=atc_levels,
            candidate_pairs=candidate_pairs,
            first_seen_qtime=first_seen_qtime,
            quarter=quarter,
            included_ndcs_by_quarter=included_ndcs_by_quarter,
        )
        stage_path.unlink()
        gc.collect()

    try:
        stage_dir.rmdir()
    except OSError:
        pass
    print(f"Saved {n_blocks} formulary panel blocks to: {panel_output_dir}")
    print(f"Saved NDC first-seen lookup to: {first_seen_output_path}")


if __name__ == "__main__":
    main()
