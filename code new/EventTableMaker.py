"""
Purpose:
Build firm-level movement-event eligibility tables from RawEventTableMaker candidates.

Process:
1. Read variant-specific movement candidates.
2. Convert candidate rows to firm-year or firm-quarter event rows. For
   delayed quarterly `to_B_not_in_A` moves, A and B retain their own event
   quarters.
3. Build req0, req1, and req2 flags with nested requirement logic.
4. Collapse duplicate firm-time event rows by groupby max.
5. Write one movement event table per variant.

Input:
- `data/roster_variants/<variant>/leader_tier_<tier>/event_tables/movement_event_candidates.csv`

Output:
- `data/roster_variants/<variant>/leader_tier_<tier>/event_tables/movement_table.csv`
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from pipeline_variant_config import (
    PERSONNEL_DEFINITIONS,
    ROSTER_VARIANTS,
    configured_personnel_definitions,
    configured_variants,
    personnel_output_dir,
)

REQUIREMENT_COLUMNS = ("req0", "req1", "req2")
MOVEMENT_EVENT_TYPES = {
    "to_B_still_in_A",
    "to_B_not_in_A",
    "interlock_dissolution",
}
MOVEMENT_REQUIRED_COLUMNS = {
    "event_type",
    "FirmA",
    "FirmB",
    "requirement1",
}


RUN_CONFIG = {
    "roster_variants": configured_variants(),
    "personnel_definitions": configured_personnel_definitions(),
}


def build_large_sample_suffix(large_sample: int, personnel_definition: str) -> str:
    """Return movement file suffix for the configured sample definition."""
    if large_sample not in {0, 1}:
        raise ValueError("large_sample must be 0 or 1")
    if large_sample == 0:
        return ""
    if personnel_definition not in PERSONNEL_DEFINITIONS:
        raise ValueError("personnel_definition must be one of: narrow, medium, broad")
    return f"_large_sample_{personnel_definition}"


def build_event_table(candidates: pd.DataFrame, table_type: str, source_name: str) -> pd.DataFrame:
    """
    Build one firm-level event table from one raw candidate table.

    Movement candidates are expanded to A-side and B-side firm rows with
    firm_type.
    """
    candidates = candidates.copy()
    if "year" not in candidates.columns and "event_year" in candidates.columns:
        candidates = candidates.rename(columns={"event_year": "year"})
    if "quarter" not in candidates.columns and "event_quarter" in candidates.columns:
        if candidates["event_quarter"].notna().any():
            candidates = candidates.rename(columns={"event_quarter": "quarter"})
        else:
            candidates = candidates.drop(columns=["event_quarter"])
    quarterly_source = "quarter" in candidates.columns
    time_columns = ["year", "quarter"] if quarterly_source else ["year"]
    missing_time = sorted(set(time_columns) - set(candidates.columns))
    if missing_time:
        raise ValueError(f"{source_name} is missing time columns: {missing_time}")

    # RawEventTableMaker emits exactly one stay_{x}_years column under the
    # current run configuration. It is used to construct the stay-based
    # requirement for still and exit events.
    stay_columns = [
        column
        for column in candidates.columns
        if column.startswith("stay_") and column.endswith("_years")
    ]
    if len(stay_columns) != 1:
        raise ValueError(f"{source_name} should contain exactly one stay column, found {stay_columns}")
    stay_column = stay_columns[0]

    if table_type == "movement":
        missing = sorted({*MOVEMENT_REQUIRED_COLUMNS, stay_column} - set(candidates.columns))
        if missing:
            raise ValueError(f"{source_name} is missing columns: {missing}")

        # Keep only the three movement-style events.
        movement = candidates.loc[candidates["event_type"].isin(MOVEMENT_EVENT_TYPES)]
        # RawEventTableMaker populates common event times into these fields for
        # annual and non-delayed rows. Delayed quarterly moves retain distinct
        # A- and B-side event times and requirement1 flags.
        required_side_columns = {
            "FirmA_event_time_id",
            "FirmB_event_time_id",
            "requirement1_A",
            "requirement1_B",
        }
        missing_side_columns = sorted(required_side_columns - set(movement.columns))
        if missing_side_columns:
            raise ValueError(
                f"{source_name} is missing side-specific event columns: "
                f"{missing_side_columns}"
            )

        def make_side_frame(
            side: str,
            firm_column: str,
            time_column: str,
            requirement_column: str,
        ) -> pd.DataFrame:
            """Build one firm-side table with its own event date."""
            frame = movement[
                [
                    "event_type",
                    stay_column,
                    time_column,
                    requirement_column,
                    firm_column,
                ]
            ].copy()
            frame = frame.rename(
                columns={
                    stay_column: "stay",
                    time_column: "time_id",
                    requirement_column: "requirement1",
                    firm_column: "BoardName",
                }
            )
            frame["time_id"] = pd.to_numeric(frame["time_id"], errors="raise").astype(int)
            if quarterly_source:
                frame["year"] = frame["time_id"] // 4
                frame["quarter"] = frame["time_id"] % 4 + 1
            else:
                frame["year"] = frame["time_id"]
            frame["firm_type"] = side
            return frame

        a_side = make_side_frame(
            "A", "FirmA", "FirmA_event_time_id", "requirement1_A"
        )
        b_side = make_side_frame(
            "B", "FirmB", "FirmB_event_time_id", "requirement1_B"
        )

        firm_year = pd.concat([a_side, b_side], ignore_index=True)
        group_columns = ["BoardName", *time_columns, "event_type", "firm_type"]
        sort_columns = ["event_type", "firm_type", "BoardName", *time_columns]

    else:
        raise ValueError("Only movement event candidates are supported")

    # Normalize key and requirement columns before boolean flag construction.
    firm_year = firm_year.dropna(subset=["BoardName", "year"]).copy()
    firm_year["BoardName"] = firm_year["BoardName"].astype(str)
    firm_year["year"] = pd.to_numeric(firm_year["year"], errors="raise").astype(int)
    if "quarter" in time_columns:
        firm_year["quarter"] = pd.to_numeric(firm_year["quarter"], errors="raise").astype(int)
        if not firm_year["quarter"].between(1, 4).all():
            raise ValueError(f"{source_name} contains invalid quarters")
    for column in ("stay", "requirement1"):
        firm_year[column] = pd.to_numeric(
            firm_year[column],
            errors="raise",
        ).astype("int8")

    # Requirement definitions:
    # - req0 is the base event for every candidate row.
    # - req1 adds the two-year stay requirement for still and exit events.
    #   Dissolution has no additional stay filter, so req1 equals req0.
    # - req2 adds the original interlock restriction stored in requirement1.
    firm_year["req0"] = 1
    firm_year["req1"] = (
        firm_year["req0"].eq(1)
        & (
            firm_year["event_type"].eq("interlock_dissolution")
            | firm_year["stay"].eq(1)
        )
    ).astype("int8")
    firm_year["req2"] = (
        firm_year["req1"].eq(1) & firm_year["requirement1"].eq(1)
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
        Read raw movement candidates and write movement_table.csv.
    """
    for variant in RUN_CONFIG["roster_variants"]:
        for personnel_definition in RUN_CONFIG["personnel_definitions"]:
            event_dir = personnel_output_dir(
                str(variant), str(personnel_definition)
            ) / "event_tables"
            movement_candidates_path = event_dir / "movement_event_candidates.csv"
            movement_table = build_event_table(
                pd.read_csv(movement_candidates_path),
                "movement",
                movement_candidates_path.name,
            )
            movement_output_path = event_dir / "movement_table.csv"
            movement_table.to_csv(movement_output_path, index=False)
            print(
                f"[{variant} | {personnel_definition}] "
                f"Saved movement table: {len(movement_table):,} rows"
            )

if __name__ == "__main__":
    main()
