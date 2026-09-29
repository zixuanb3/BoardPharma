"""
Purpose:
Build annual or quarterly firm-level event eligibility tables from
RawEventTableMaker candidates. Quarterly formulary output contains req0/req1;
annual output retains req0/req1/req2.

Process:
1. Select movement candidates using the existing annual settings when
   quarter=0. When quarter=1, require formulary=1, ignore large_sample, and use
   the quarterly narrow candidates.
2. Read the stay and requirement flags already calculated by RawMaker.
   Interlock-based requirement1 is in the candidates, so this script does
   not reread firm_interlock_panel.
3. Expand movement candidates to A-side and B-side firm records, retaining
   event year and, in quarterly mode, event quarter.
4. Calculate req0=stay and req1=stay AND requirement1 at the candidate level.
   Annual mode also calculates req2; quarterly mode neither requires nor
   exports requirement2 or req2 fields.
5. Aggregate flags by firm, year, event type, and direction using max; add
   quarter to the grouping key in quarterly mode. Write the movement table.
6. The separate direct/indirect interlock table job remains disabled.

Input:
- data/event_tables/movement_event_candidates.csv when quarter=0 and large_sample=0
- data/event_tables/movement_event_candidates_large_sample_{definition}.csv when quarter=0, large_sample=1, formulary=0
- data/event_tables/movement_event_candidates_formulary_large_sample_{definition}.csv when quarter=0 and formulary=1
- data/event_tables/movement_event_candidates_formulary_quarter_narrow.csv when quarter=1 and formulary=1
- data/event_tables/interlock_event_candidates.csv (disabled independent interlock job)

Output:
- data/event_tables/movement_table.csv when quarter=0 and large_sample=0
- data/event_tables/movement_table_large_sample_{definition}.csv when quarter=0, large_sample=1, formulary=0
- data/event_tables/movement_table_formulary_large_sample_{definition}.csv when quarter=0 and formulary=1
- data/event_tables/movement_table_formulary_quarter_narrow.csv when quarter=1 and formulary=1
- data/event_tables/interlock_table.csv (disabled independent interlock job)
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
    "quarter": 1,  # 0: annual; 1: formulary quarters only.
    "large_sample": 1,  # Ignored when quarter=1.
    "formulary": 1,
    "personnel_definition": "narrow",
}


def build_movement_suffix(
    large_sample: int, formulary: int, personnel_definition: str, quarter: int = 0,
) -> str:
    """Return movement file suffix for the configured sample definition."""
    if quarter not in {0, 1}:
        raise ValueError("quarter must be 0 or 1")
    if quarter:
        if formulary != 1:
            raise ValueError("quarter=1 requires formulary=1")
        if personnel_definition != "narrow":
            raise ValueError("Quarterly formulary roster supports narrow personnel only")
        return "_formulary_quarter_narrow"
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


def build_event_table(
    candidates: pd.DataFrame, table_type: str, source_name: str, quarter: int = 0,
) -> pd.DataFrame:
    """
    Build one firm-level event table from one raw candidate table.

    Movement candidates are expanded to A-side and B-side firm rows with
    firm_type. Interlock candidates are already firm-level and are kept
    direction-free, without firm_type.
    """
    if quarter not in {0, 1}:
        raise ValueError("quarter must be 0 or 1")
    if quarter and table_type != "movement":
        raise ValueError("Quarter mode supports formulary movement events only")
    requirement_columns = ("req0", "req1") if quarter else REQUIREMENT_COLUMNS
    stay_suffix = "_quarters" if quarter else "_years"
    # RawMaker supplies one stay field in the selected time unit.
    stay_columns = [
        column
        for column in candidates.columns
        if column.startswith("stay_") and column.endswith(stay_suffix)
    ]
    if len(stay_columns) != 1:
        raise ValueError(f"{source_name} should contain exactly one stay column, found {stay_columns}")
    stay_column = stay_columns[0]

    if table_type == "movement":
        required = set(MOVEMENT_REQUIRED_COLUMNS)
        if quarter:
            required -= {"requirement2_A", "requirement2_B"}
            required.add("event_quarter")
        missing = sorted({*required, stay_column} - set(candidates.columns))
        if missing:
            raise ValueError(f"{source_name} is missing columns: {missing}")

        # Keep only the three movement-style events.
        movement = candidates.loc[candidates["event_type"].isin(MOVEMENT_EVENT_TYPES)]
        shared_columns = ["event_type", "event_year", stay_column, "requirement1"]
        if quarter:
            shared_columns.append("event_quarter")
        sides = []
        for side in ("A", "B"):
            columns = [*shared_columns, f"Firm{side}"]
            if not quarter:
                columns.append(f"requirement2_{side}")
            rows = movement[columns].rename(columns={
                "event_year": "year", "event_quarter": "quarter",
                stay_column: "stay", f"Firm{side}": "BoardName",
                f"requirement2_{side}": "requirement2",
            })
            rows["firm_type"] = side
            sides.append(rows)

        firm_year = pd.concat(sides, ignore_index=True)
        time_columns = ["year", "quarter"] if quarter else ["year"]
        group_columns = ["BoardName", *time_columns, "event_type", "firm_type"]
        sort_columns = ["event_type", "firm_type", "BoardName", *time_columns]

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
    if not quarter:
        firm_year["year"] = pd.to_numeric(firm_year["year"], errors="raise").astype(int)
    if quarter:
        for column in ("year", "quarter"):
            values = pd.to_numeric(firm_year[column], errors="raise")
            if values.isna().any() or not values.eq(values.round()).all():
                raise ValueError(f"{source_name}: invalid {column}")
            firm_year[column] = values.astype(int)
        if not firm_year["quarter"].isin([1, 2, 3, 4]).all():
            raise ValueError(f"{source_name}: quarter must be 1, 2, 3, or 4")
    flag_columns = ("stay", "requirement1") if quarter else ("stay", "requirement1", "requirement2")
    for column in flag_columns:
        if quarter and not pd.to_numeric(firm_year[column], errors="raise").isin([0, 1]).all():
            raise ValueError(f"{source_name}: {column} must be binary")
        firm_year[column] = pd.to_numeric(
            firm_year[column],
            errors="raise",
        ).astype("int8")

    # req1 must first satisfy req0; req2 must first satisfy req1.
    firm_year["req0"] = firm_year["stay"].eq(1).astype("int8")
    firm_year["req1"] = (
        firm_year["stay"].eq(1) & firm_year["requirement1"].eq(1)
    ).astype("int8")
    if not quarter:
        firm_year["req2"] = (
            firm_year["stay"].eq(1)
            & firm_year["requirement1"].eq(1)
            & firm_year["requirement2"].eq(1)
        ).astype("int8")

    # Multiple candidate rows can map to the same firm-year event; one valid
    # candidate is enough, so collapse with max.
    event_table = (
        firm_year.groupby(group_columns, as_index=False)[list(requirement_columns)]
        .max()
        .sort_values(sort_columns)
        .reset_index(drop=True)
    )
    event_table[list(requirement_columns)] = event_table[
        list(requirement_columns)
    ].astype("int8")
    return event_table[[*group_columns, *requirement_columns]]


def main() -> None:
    """
    Read raw candidate tables and write movement_table.csv and interlock_table.csv.
    """
    quarter = int(RUN_CONFIG["quarter"])
    large_sample = 0 if quarter else int(RUN_CONFIG["large_sample"])
    formulary = int(RUN_CONFIG["formulary"])
    personnel_definition = str(RUN_CONFIG["personnel_definition"])
    movement_suffix = build_movement_suffix(large_sample, formulary, personnel_definition, quarter)
    movement_candidates_path = EVENT_TABLE_DIR / f"movement_event_candidates{movement_suffix}.csv"
    movement_output_path = EVENT_TABLE_DIR / f"movement_table{movement_suffix}.csv"

    # Movement output preserves firm_type because A and B treated-side panels
    # still need different firm definitions.
    movement_candidates = pd.read_csv(movement_candidates_path)
    movement_table = build_event_table(
        movement_candidates,
        "movement",
        movement_candidates_path.name,
        quarter=quarter,
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
