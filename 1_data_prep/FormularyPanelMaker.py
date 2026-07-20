r"""
Purpose:
Build memory-safe formulary-level event-study panels from the expanded brand
formulary CSV. The output unit is FORMULARY_ID by BoardName by NDC by quarter.
All movement event types, both A/B treatment sides, balance flags, and
direction-specific ATC1--ATC4 sharing indicators are stored in one panel.

Process:
1. Read FORMULARY_ID only, split complete formularies into fixed blocks, then
   route the raw CSV into disk-backed staging blocks in one additional pass.
2. Process one complete formulary block at a time: add annual event flags in
   Q1, event-specific balanced-window flags, tierA, and ATC sharing outcomes.
3. Write one final CSV per block and immediately delete its staging file so
   the full raw panel and all configured blocks are never held in memory
   together.

Input:
- D:/task1_expanded_brand_panel/task1_expanded_brand_panel.csv
- data/event_tables/movement_table_formulary_large_sample_{definition}.csv
- data/event_tables/movement_event_candidates_formulary_large_sample_{definition}.csv

Output:
- data/formulary_panel/formulary_panel_1.csv through
  formulary_panel_{n_formulary_blocks}.csv
- D:/task1_expanded_brand_panel/formulary_panel_staging/ temporary block files
  while the script is running; each staging file is deleted after its final
  output has been written successfully.
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
EVENT_TABLE_DIR = PROJECT_ROOT / "data" / "event_tables"

RAW_FORMULARY_PATH = Path(r"D:\task1_expanded_brand_panel\task1_expanded_brand_panel.csv")
STAGING_BASE_PATH = RAW_FORMULARY_PATH.parent / "formulary_panel_staging"

PERSONNEL_DEFINITIONS = {"narrow", "medium", "broad"}
MOVEMENT_EVENTS = {
    "to_B_still_in_A",
    "to_B_not_in_A",
    "interlock_dissolution",
}
TREATMENT_GROUPS = {"A", "B"}
ATC_LEVELS = (1, 2, 3, 4)


# ========================== USER CONFIG ==========================
# event_types:
# - Movement events to include in the single combined output panel.
# - The script writes one event column and one balance column for each
#   event_type by treatment_group combination.
#
# panel_levels:
# - Only "quarter" is supported because the raw formulary data are quarterly
#   and annual events are explicitly placed in Q1.
#
# stay_x_years:
# - Must match the stay column used to build the movement event inputs.
#
# balance_window:
# - Annual offsets around an event year.  (-1, 1) requires all 12 quarters
#   in event year -1, event year, and event year +1 for a formulary to be
#   considered balanced at that event year.
#
# treatment_groups:
# - "A" uses FirmA as the treated firm and FirmB as its candidate counterpart.
# - "B" uses FirmB as the treated firm and FirmA as its candidate counterpart.
#
# atc:
# - ATC levels for which direction-specific sharing flags are constructed.
#
# req:
# - 0, 1, or 2.  This selects req0, req1, or req2 from the movement event
#   table and applies the equivalent candidate-level conditions for sharing.
#
# n_formulary_blocks/chunksize:
# - The raw file is routed into n_formulary_blocks complete-formulary staging
#   files.  chunksize controls only CSV streaming memory, not output grouping.
RUN_CONFIG = {
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
    "atc": [1, 2, 3, 4],
    "req": 1,
    "n_formulary_blocks": 30,
    "chunksize": 1_000_000,
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


def event_column(event_type: str, treatment_group: str) -> str:
    """Return the combined-panel event column name for one event direction."""
    return f"event_{event_type}_{treatment_group}"


def balance_column(event_type: str, treatment_group: str) -> str:
    """Return the corresponding event-specific balanced-panel column name."""
    return f"{event_column(event_type, treatment_group)}_balanced"


def sharing_column(event_type: str, treatment_group: str, atc_level: int) -> str:
    """Return the direction-specific sharing column name for one ATC level."""
    return f"{event_column(event_type, treatment_group)}_sharingATC{atc_level}"


def movement_suffix(large_sample: int, personnel_definition: str) -> str:
    """Return the formulary movement-event suffix for the selected definition."""
    if large_sample != 1:
        raise ValueError("FormularyPanelMaker requires large_sample == 1.")
    if personnel_definition not in PERSONNEL_DEFINITIONS:
        raise ValueError("personnel_definition must be narrow, medium, or broad")
    return f"_formulary_large_sample_{personnel_definition}"


def validate_config(config: dict[str, object]) -> tuple[
    list[str], list[str], int, tuple[int, int], int, str, tuple[int, ...], int, int
]:
    """Validate RUN_CONFIG and return normalized values used by the builder."""
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
        raise ValueError("atc must contain one or more values from 1, 2, 3, and 4.")

    req = int(config["req"])
    if req not in {0, 1, 2}:
        raise ValueError("req must be 0, 1, or 2.")

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
    movement_suffix(int(config["large_sample"]), personnel_definition)
    return (
        event_types,
        treatment_groups,
        stay_x_years,
        balance_window,
        int(config["large_sample"]),
        personnel_definition,
        atc_levels,
        req,
        n_blocks,
    )


# ========================== DATA LOADERS ==========================


def required_input_columns(atc_levels: Iterable[int]) -> set[str]:
    """Return columns that must exist in the raw formulary CSV."""
    return {
        "YEAR_Q",
        "FORMULARY_ID",
        "BoardName",
        "NDC",
        "included",
        "tier_raw",
        *(f"ATC{level}" for level in atc_levels),
    }


def validate_raw_schema(atc_levels: Iterable[int]) -> None:
    """Read only the header and validate raw formulary fields needed downstream."""
    if not RAW_FORMULARY_PATH.exists():
        raise FileNotFoundError(f"Raw formulary panel not found: {RAW_FORMULARY_PATH}")
    columns = list(pd.read_csv(RAW_FORMULARY_PATH, nrows=0).columns)
    missing = sorted(required_input_columns(atc_levels) - set(columns))
    if missing:
        raise KeyError(f"Raw formulary panel is missing required columns: {missing}")


def load_event_flags(
    event_types: list[str],
    treatment_groups: list[str],
    req: int,
    suffix: str,
) -> tuple[pd.DataFrame, dict[str, set[int]]]:
    """Load one collapsed firm-year event flag for each event type and side."""
    path = EVENT_TABLE_DIR / f"movement_table{suffix}.csv"
    if not path.exists():
        raise FileNotFoundError(f"Movement event table not found: {path}")

    event_table = pd.read_csv(path, dtype="string")
    required = {"BoardName", "year", "event_type", "firm_type", f"req{req}"}
    missing = sorted(required - set(event_table.columns))
    if missing:
        raise KeyError(f"{path.name} is missing columns: {missing}")

    event_table["BoardName"] = clean_string(event_table["BoardName"], uppercase=True)
    event_table["event_type"] = clean_string(event_table["event_type"])
    event_table["firm_type"] = clean_string(event_table["firm_type"], uppercase=True)
    event_table["year"] = pd.to_numeric(event_table["year"], errors="raise").astype("int16")
    event_table[f"req{req}"] = pd.to_numeric(event_table[f"req{req}"], errors="raise").astype("int8")

    flag_parts: list[pd.DataFrame] = []
    event_years: dict[str, set[int]] = {}
    for event_type in event_types:
        for treatment_group in treatment_groups:
            column = event_column(event_type, treatment_group)
            flagged = event_table.loc[
                event_table["event_type"].eq(event_type)
                & event_table["firm_type"].eq(treatment_group)
                & event_table[f"req{req}"].eq(1),
                ["BoardName", "year"],
            ].drop_duplicates()
            flagged[column] = np.int8(1)
            event_years[column] = set(flagged["year"].astype(int).tolist())
            flag_parts.append(flagged)

    if not flag_parts:
        return pd.DataFrame(columns=["BoardName", "year"]), event_years

    flags = flag_parts[0]
    for flagged in flag_parts[1:]:
        flags = flags.merge(flagged, on=["BoardName", "year"], how="outer", validate="one_to_one")
    event_columns = [event_column(event_type, side) for event_type in event_types for side in treatment_groups]
    flags[event_columns] = flags[event_columns].fillna(0).astype("int8")
    return flags, event_years


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
) -> dict[str, pd.DataFrame]:
    """Load valid directional candidate pairs for ATC overlap construction."""
    path = EVENT_TABLE_DIR / f"movement_event_candidates{suffix}.csv"
    if not path.exists():
        raise FileNotFoundError(f"Movement event candidates not found: {path}")

    candidates = pd.read_csv(path, dtype="string")
    stay_column = f"stay_{stay_x_years}_years"
    required = {
        "event_type",
        "event_year",
        "FirmA",
        "FirmB",
        stay_column,
        "requirement1",
        "requirement2_A",
        "requirement2_B",
    }
    missing = sorted(required - set(candidates.columns))
    if missing:
        raise KeyError(f"{path.name} is missing columns: {missing}")

    candidates["event_type"] = clean_string(candidates["event_type"])
    candidates["event_year"] = pd.to_numeric(candidates["event_year"], errors="raise").astype("int16")
    for column in ("FirmA", "FirmB"):
        candidates[column] = clean_string(candidates[column], uppercase=True)
    for column in (stay_column, "requirement1", "requirement2_A", "requirement2_B"):
        candidates[column] = pd.to_numeric(candidates[column], errors="raise").astype("int8")

    pairs_by_event: dict[str, pd.DataFrame] = {}
    for event_type in event_types:
        for side in treatment_groups:
            column = event_column(event_type, side)
            subset = candidates.loc[
                candidates["event_type"].eq(event_type)
                & candidate_condition(candidates, req, side, stay_column),
                ["event_year", "FirmA", "FirmB"],
            ].dropna()

            if side == "A":
                pairs = subset.rename(columns={"event_year": "year", "FirmA": "BoardName", "FirmB": "BoardNamePair"})
            else:
                pairs = subset.rename(columns={"event_year": "year", "FirmB": "BoardName", "FirmA": "BoardNamePair"})
            pairs = pairs.drop_duplicates().reset_index(drop=True)

            event_keys = event_flags.loc[event_flags[column].eq(1), ["BoardName", "year"]].drop_duplicates()
            candidate_keys = pairs[["BoardName", "year"]].drop_duplicates()
            missing_keys = event_keys.merge(
                candidate_keys,
                on=["BoardName", "year"],
                how="left",
                indicator=True,
            )
            if missing_keys["_merge"].eq("left_only").any():
                examples = missing_keys.loc[
                    missing_keys["_merge"].eq("left_only"), ["BoardName", "year"]
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


def staging_directory(personnel_definition: str, req: int) -> Path:
    """Return a run-specific disk staging directory beside the large raw input."""
    return STAGING_BASE_PATH / f"{personnel_definition}_req{req}"


def stage_paths(stage_dir: Path, n_blocks: int) -> dict[int, Path]:
    """Return all temporary source-block paths for one run."""
    return {block: stage_dir / f"formulary_stage_{block}.csv" for block in range(1, n_blocks + 1)}


def create_staging_blocks(
    block_lookup: dict[str, int],
    n_blocks: int,
    chunksize: int,
    stage_dir: Path,
) -> dict[int, Path]:
    """Route the raw CSV to complete-formulary staging files in one full pass."""
    stage_dir.mkdir(parents=True, exist_ok=True)
    paths = stage_paths(stage_dir, n_blocks)
    existing = [path for path in paths.values() if path.exists()]
    if existing:
        raise FileExistsError(
            f"Staging files already exist in {stage_dir}. Remove or move them before rerunning: "
            f"{existing[:3]}"
        )

    headers_written = {block: False for block in paths}
    reader = pd.read_csv(RAW_FORMULARY_PATH, dtype="string", chunksize=chunksize)
    with ExitStack() as stack:
        handles = {
            block: stack.enter_context(path.open("w", encoding="utf-8", newline=""))
            for block, path in paths.items()
        }
        for chunk in tqdm(reader, desc="Pass 2/2: routing formulary chunks", unit="chunk"):
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
    return paths


# ========================== PANEL CONSTRUCTION ==========================


def add_event_flags(data: pd.DataFrame, event_flags: pd.DataFrame, event_columns: list[str]) -> pd.DataFrame:
    """Merge annual firm events and anchor every quarterly event indicator in Q1."""
    result = data.merge(event_flags, on=["BoardName", "year"], how="left", validate="many_to_one")
    result[event_columns] = result[event_columns].fillna(0).astype("int8")
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


def add_balance_flags(
    data: pd.DataFrame,
    event_types: list[str],
    treatment_groups: list[str],
    event_years: dict[str, set[int]],
    balance_window: tuple[int, int],
    progress: tqdm | None = None,
) -> pd.DataFrame:
    """Add event-specific balance flags only to their corresponding Q1 event rows."""
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


def partner_atc_codes(data: pd.DataFrame, partner_scope: pd.DataFrame, atc_column: str) -> pd.DataFrame:
    """Return atomic year-partner-ATC codes needed by every sharing comparison."""
    if partner_scope.empty:
        return pd.DataFrame(columns=["year", "BoardNamePair", "atc_code"])
    scoped = data.merge(partner_scope, on=["year", "BoardName"], how="inner", validate="many_to_one")
    atoms = explode_atc_codes(scoped, atc_column, ["year", "BoardName"])
    return atoms.rename(columns={"BoardName": "BoardNamePair"}).drop_duplicates()


def add_sharing_flags(
    data: pd.DataFrame,
    event_types: list[str],
    treatment_groups: list[str],
    atc_levels: tuple[int, ...],
    candidate_pairs: dict[str, pd.DataFrame],
    progress: tqdm | None = None,
) -> pd.DataFrame:
    """Add direction-specific event ATC overlap flags without duplicating outcome rows."""
    data["_row_id"] = np.arange(len(data), dtype=np.int64)
    all_pairs = pd.concat(candidate_pairs.values(), ignore_index=True)
    partner_scope = all_pairs[["year", "BoardNamePair"]].drop_duplicates().rename(
        columns={"BoardNamePair": "BoardName"}
    )

    for atc_level in atc_levels:
        atc_column = f"ATC{atc_level}"
        partners = partner_atc_codes(data, partner_scope, atc_column)
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
                    ["_row_id", "year", "BoardName", atc_column],
                ]
                if not event_rows.empty and not pairs.empty and not partners.empty:
                    event_atoms = explode_atc_codes(
                        event_rows,
                        atc_column,
                        ["_row_id", "year", "BoardName"],
                    )
                    if not event_atoms.empty:
                        paired_codes = event_atoms.merge(
                            pairs,
                            on=["year", "BoardName"],
                            how="inner",
                            validate="many_to_many",
                        )
                        matches = paired_codes.merge(
                            partners,
                            on=["year", "BoardNamePair", "atc_code"],
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
    """Copy tier_raw and fill uncovered rows with the current formula-quarter maximum plus one."""
    data["tier_raw"] = pd.to_numeric(data["tier_raw"], errors="coerce")
    current_max = data.groupby(["FORMULARY_ID", "YEAR_Q"])["tier_raw"].transform("max")
    data["tierA"] = data["tier_raw"].where(data["tier_raw"].notna(), current_max + 1)

    unresolved = data["tierA"].isna()
    if unresolved.any():
        examples = data.loc[unresolved, ["FORMULARY_ID", "YEAR_Q"]].drop_duplicates().head(10)
        raise ValueError(
            "tierA cannot be constructed because some FORMULARY_ID by YEAR_Q groups have no nonmissing "
            f"tier_raw value. Examples:\n{examples}"
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
        data["BoardName"] = clean_string(data["BoardName"], uppercase=True)
        data["NDC"] = clean_string(data["NDC"])
        if data[["FORMULARY_ID", "BoardName", "NDC"]].isna().any().any():
            raise ValueError(f"{stage_path.name} contains missing FORMULARY_ID, BoardName, or NDC values.")
        data = parse_year_quarter(data, stage_path.name)
        progress.update(1)

        progress.set_postfix_str("merging event indicators")
        event_columns = [event_column(event_type, side) for event_type in event_types for side in treatment_groups]
        data = add_event_flags(data, event_flags, event_columns)
        progress.update(1)

        progress.set_postfix_str("checking balance-window coverage")
        data = add_balance_flags(
            data,
            event_types,
            treatment_groups,
            event_years,
            balance_window,
            progress,
        )
        data = add_sharing_flags(
            data,
            event_types,
            treatment_groups,
            atc_levels,
            candidate_pairs,
            progress,
        )

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
        n_blocks,
    ) = validate_config(RUN_CONFIG)
    chunksize = int(RUN_CONFIG["chunksize"])
    validate_raw_schema(atc_levels)
    suffix = movement_suffix(large_sample, personnel_definition)

    event_flags, event_years = load_event_flags(event_types, treatment_groups, req, suffix)
    candidate_pairs = load_candidate_pairs(
        event_types,
        treatment_groups,
        req,
        stay_x_years,
        suffix,
        event_flags,
    )

    print(
        "Building formulary panels: "
        f"definition={personnel_definition}, req{req}, blocks={n_blocks}, "
        f"balance_window=t{balance_window[0]:+d}..t{balance_window[1]:+d}"
    )
    block_lookup = build_formulary_blocks(n_blocks, chunksize)
    stage_dir = staging_directory(personnel_definition, req)
    paths = create_staging_blocks(block_lookup, n_blocks, chunksize, stage_dir)

    OUTPUT_BASE_PATH.mkdir(parents=True, exist_ok=True)
    for block_number in tqdm(range(1, n_blocks + 1), desc="Processing formulary blocks", unit="block"):
        stage_path = paths[block_number]
        output_path = OUTPUT_BASE_PATH / f"formulary_panel_{block_number}.csv"
        if output_path.exists():
            raise FileExistsError(
                f"Refusing to overwrite existing output: {output_path}. Move or delete it before rerunning."
            )
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
        )
        stage_path.unlink()
        gc.collect()

    try:
        stage_dir.rmdir()
    except OSError:
        pass
    print(f"Saved {n_blocks} formulary panel blocks to: {OUTPUT_BASE_PATH}")


if __name__ == "__main__":
    main()
