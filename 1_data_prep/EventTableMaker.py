"""
Purpose:
Build firm-level event eligibility tables from RawEventTableMaker candidate files.

Process:
1. Read movement and interlock candidate tables from data/event_tables.
2. Convert candidate rows to firm-year event rows.
3. Build req0, req1, and req2 flags with nested requirement logic.
4. Collapse duplicate firm-year event rows by groupby max.
5. Write one movement event table and one interlock event table.

Input:
- data/event_tables/movement_event_candidates.csv
- data/event_tables/movement_event_candidates_large_sample_{definition}.csv when RUN_CONFIG["large_sample"] == 1
- data/event_tables/movement_event_candidates_formulary_large_sample_{definition}.csv when RUN_CONFIG["formulary"] == 1
- data/event_tables/interlock_event_candidates.csv

Output:
- data/event_tables/movement_table.csv
- data/event_tables/movement_table_large_sample_{definition}.csv when RUN_CONFIG["large_sample"] == 1
- data/event_tables/movement_table_formulary_large_sample_{definition}.csv when RUN_CONFIG["formulary"] == 1
- data/event_tables/interlock_table.csv
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


CURRENT_PATH = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_PATH.parent.parent
EVENT_TABLE_DIR = PROJECT_ROOT / "data" / "event_tables"

INTERLOCK_CANDIDATES_PATH = EVENT_TABLE_DIR / "interlock_event_candidates.csv"
INTERLOCK_OUTPUT_PATH = EVENT_TABLE_DIR / "interlock_table.csv"

PERSONNEL_DEFINITIONS = {"narrow", "medium", "broad"}
REQUIREMENT_COLUMNS = ("req0", "req1", "req2")
MOVEMENT_EVENT_TYPES = {
    "to_B_still_in_A",
    "to_B_not_in_A",
    "interlock_dissolution",
}
INTERLOCK_EVENT_TYPES = {"direct_interlock", "indirect_interlock"}
MOVEMENT_REQUIRED_COLUMNS = {
    "event_type",
    "event_year",
    "FirmA",
    "FirmB",
    "requirement1",
    "requirement2_A",
    "requirement2_B",
}
INTERLOCK_REQUIRED_COLUMNS = {
    "event_type",
    "event_year",
    "BoardName",
    "requirement1",
    "requirement2",
}


RUN_CONFIG = {
    "large_sample": 1,
    "formulary": 1,
    "personnel_definition": "narrow",
}


def build_movement_suffix(large_sample: int, formulary: int, personnel_definition: str) -> str:
    """Return movement file suffix for the configured sample definition."""
    if large_sample not in {0, 1}:
        raise ValueError("large_sample must be 0 or 1")
    if formulary not in {0, 1}:
        raise ValueError("formulary must be 0 or 1")
    if formulary == 1 and large_sample != 1:
        raise ValueError("formulary requires large_sample == 1")
    if large_sample == 0:
        return ""
    if personnel_definition not in PERSONNEL_DEFINITIONS:
        raise ValueError("personnel_definition must be one of: narrow, medium, broad")
    if formulary == 1:
        return f"_formulary_large_sample_{personnel_definition}"
    return f"_large_sample_{personnel_definition}"


def build_event_table(candidates: pd.DataFrame, table_type: str, source_name: str) -> pd.DataFrame:
    """
    Build one firm-level event table from one raw candidate table.

    Movement candidates are expanded to A-side and B-side firm rows with
    firm_type. Interlock candidates are already firm-level and are kept
    direction-free, without firm_type.
    """
    # RawEventTableMaker emits exactly one stay_{x}_years column under the
    # current run configuration; req0 is defined from that column.
    stay_columns = [
        column
        for column in candidates.columns
        if column.startswith("stay_") and column.endswith("_years")
    ]
    if len(stay_columns) != 1:
        raise ValueError(f"{source_name} should contain exactly one stay column, found {stay_columns}")
    stay_column = stay_columns[0]

    if table_type == "movement":
        # Movement rows need FirmA/FirmB and side-specific requirement2 flags.
        missing = sorted({*MOVEMENT_REQUIRED_COLUMNS, stay_column} - set(candidates.columns))
        if missing:
            raise ValueError(f"{source_name} is missing columns: {missing}")

        # Keep only the three movement-style events.
        movement = candidates.loc[candidates["event_type"].isin(MOVEMENT_EVENT_TYPES)]
        shared_columns = ["event_type", "event_year", stay_column, "requirement1"]

        # A-side treated rows use FirmA and requirement2_A.
        a_side = movement[shared_columns + ["FirmA", "requirement2_A"]].rename(
            columns={
                "event_year": "year",
                stay_column: "stay",
                "FirmA": "BoardName",
                "requirement2_A": "requirement2",
            }
        )
        a_side["firm_type"] = "A"

        # B-side treated rows use FirmB and requirement2_B.
        b_side = movement[shared_columns + ["FirmB", "requirement2_B"]].rename(
            columns={
                "event_year": "year",
                stay_column: "stay",
                "FirmB": "BoardName",
                "requirement2_B": "requirement2",
            }
        )
        b_side["firm_type"] = "B"

        firm_year = pd.concat([a_side, b_side], ignore_index=True)
        group_columns = ["BoardName", "year", "event_type", "firm_type"]
        sort_columns = ["event_type", "firm_type", "BoardName", "year"]

    elif table_type == "interlock":
        # Interlock events are direction-free here, so no A/B firm_type is added.
        missing = sorted({*INTERLOCK_REQUIRED_COLUMNS, stay_column} - set(candidates.columns))
        if missing:
            raise ValueError(f"{source_name} is missing columns: {missing}")

        # Keep direct and indirect interlock rows in one output table.
        interlock = candidates.loc[candidates["event_type"].isin(INTERLOCK_EVENT_TYPES)]
        firm_year = interlock[
            ["event_type", "event_year", stay_column, "BoardName", "requirement1", "requirement2"]
        ].rename(
            columns={
                "event_year": "year",
                stay_column: "stay",
            }
        )
        group_columns = ["BoardName", "year", "event_type"]
        sort_columns = ["event_type", "BoardName", "year"]

    else:
        raise ValueError("table_type must be either 'movement' or 'interlock'")

    # Normalize key and requirement columns before boolean flag construction.
    firm_year = firm_year.dropna(subset=["BoardName", "year"]).copy()
    firm_year["BoardName"] = firm_year["BoardName"].astype(str)
    firm_year["year"] = pd.to_numeric(firm_year["year"], errors="raise").astype(int)
    for column in ("stay", "requirement1", "requirement2"):
        firm_year[column] = pd.to_numeric(
            firm_year[column],
            errors="raise",
        ).astype("int8")

    # req1 must first satisfy req0; req2 must first satisfy req1.
    firm_year["req0"] = firm_year["stay"].eq(1).astype("int8")
    firm_year["req1"] = (
        firm_year["stay"].eq(1) & firm_year["requirement1"].eq(1)
    ).astype("int8")
    firm_year["req2"] = (
        firm_year["stay"].eq(1)
        & firm_year["requirement1"].eq(1)
        & firm_year["requirement2"].eq(1)
    ).astype("int8")

    # Multiple candidate rows can map to the same firm-year event; one valid
    # candidate is enough, so collapse with max.
    event_table = (
        firm_year.groupby(group_columns, as_index=False)[list(REQUIREMENT_COLUMNS)]
        .max()
        .sort_values(sort_columns)
        .reset_index(drop=True)
    )
    event_table[list(REQUIREMENT_COLUMNS)] = event_table[
        list(REQUIREMENT_COLUMNS)
    ].astype("int8")
    return event_table[[*group_columns, *REQUIREMENT_COLUMNS]]


def main() -> None:
    """
    Read raw candidate tables and write movement_table.csv and interlock_table.csv.
    """
    large_sample = int(RUN_CONFIG["large_sample"])
    formulary = int(RUN_CONFIG["formulary"])
    personnel_definition = str(RUN_CONFIG["personnel_definition"])
    movement_suffix = build_movement_suffix(large_sample, formulary, personnel_definition)
    movement_candidates_path = EVENT_TABLE_DIR / f"movement_event_candidates{movement_suffix}.csv"
    movement_output_path = EVENT_TABLE_DIR / f"movement_table{movement_suffix}.csv"

    # Movement output preserves firm_type because A and B treated-side panels
    # still need different firm definitions.
    movement_candidates = pd.read_csv(movement_candidates_path)
    movement_table = build_event_table(
        movement_candidates,
        "movement",
        movement_candidates_path.name,
    )
    movement_output_path.parent.mkdir(parents=True, exist_ok=True)
    movement_table.to_csv(movement_output_path, index=False)
    print(f"Saved: {movement_output_path} ({len(movement_table):,} rows)")

"""
    # Interlock output is direction-free and combines direct and indirect events.
    interlock_candidates = pd.read_csv(INTERLOCK_CANDIDATES_PATH)
    interlock_table = build_event_table(
        interlock_candidates,
        "interlock",
        INTERLOCK_CANDIDATES_PATH.name,
    )
    INTERLOCK_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    interlock_table.to_csv(INTERLOCK_OUTPUT_PATH, index=False)
    print(f"Saved: {INTERLOCK_OUTPUT_PATH} ({len(interlock_table):,} rows)")
"""

if __name__ == "__main__":
    main()
