"""
Purpose:
    Build quarterly firm-level movement-event tables from the eleven-column
    candidates produced by 1.1_ssrquarter/RawEventTableMaker.py.

Process:
    1. Read the two roster variants separately for the selected late,
       req1_spec, and stay_spec combination; do not recalculate upstream flags.
    2. Expand each candidate into A- and B-company records using each company's
       own event time and requirement1_A or requirement1_B.
    3. Set req0=1, req1=(stay==1) for all three event types, and
       req2=(req1==1 AND the corresponding requirement1_A/B==1).
       Dissolution also uses stay: upstream new sets it to 1; upstream origin
       checks the triggering person's joint A/B service in T-4 through T-1.
    4. Collapse records by company, year, quarter, event type, and A/B identity
       using max; preserve code new's output columns and sorting order.

Input:
    data/ssrquarter/event_tables/<variant>/late_<late>/
        req1_<req1_spec>/stay_<stay_spec>/movement_event_candidates.csv
    Use the upstream eleven-column schema; no common requirement1 is needed.
    Command-line options can override the input and output root directories.

Output:
    data/ssrquarter/event_tables/<variant>/late_<late>/
        req1_<req1_spec>/stay_<stay_spec>/movement_table.csv
    Columns: BoardName, year, quarter, event_type, firm_type, req0, req1, req2.
    Existing output files are not overwritten. --all-configurations processes
    late={0,3} and req1_spec/stay_spec={new,origin}, for both roster variants.
"""

from __future__ import annotations

import argparse
from itertools import product
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVENT_TABLE_DIR = PROJECT_ROOT / "data" / "ssrquarter" / "event_tables"
VARIANTS = ("imputed_yearquarter", "drop_sentinel_yearquarter")
RUN_CONFIG = {"late": 3, "req1_spec": "new", "stay_spec": "new"}
REQUIREMENT_COLUMNS = ["req0", "req1", "req2"]
GROUP_COLUMNS = ["BoardName", "year", "quarter", "event_type", "firm_type"]
SORT_COLUMNS = ["event_type", "firm_type", "BoardName", "year", "quarter"]
MOVEMENT_EVENT_TYPES = {
    "to_B_still_in_A",
    "to_B_not_in_A",
    "interlock_dissolution",
}


def configuration_dir(
    root: Path, variant: str, late: int, req1_spec: str, stay_spec: str
) -> Path:
    """Return the same configuration directory structure as RawEventTableMaker."""
    return (
        root
        / variant
        / f"late_{late}"
        / f"req1_{req1_spec}"
        / f"stay_{stay_spec}"
    )


def build_event_table(candidates: pd.DataFrame, source_name: str) -> pd.DataFrame:
    """Convert quarterly candidates to company events, using stay for all types."""
    stay_columns = [
        column
        for column in candidates.columns
        if column.startswith("stay_") and column.endswith("_years")
    ]
    if len(stay_columns) != 1:
        raise ValueError(
            f"{source_name} should contain exactly one stay column, found {stay_columns}"
        )
    stay_column = stay_columns[0]
    required = {
        "event_type", "year", "quarter", "FirmA", "FirmB", stay_column,
        "FirmA_event_time_id", "FirmB_event_time_id",
        "requirement1_A", "requirement1_B",
    }
    missing = sorted(required - set(candidates.columns))
    if missing:
        raise ValueError(f"{source_name} is missing columns: {missing}")
    movement = candidates.loc[candidates["event_type"].isin(MOVEMENT_EVENT_TYPES)]

    sides = []
    for side in ("A", "B"):
        frame = movement[
            [
                "event_type", stay_column, f"Firm{side}_event_time_id",
                f"requirement1_{side}", f"Firm{side}",
            ]
        ].rename(
            columns={
                stay_column: "stay",
                f"Firm{side}_event_time_id": "time_id",
                f"requirement1_{side}": "requirement1",
                f"Firm{side}": "BoardName",
            }
        ).copy()
        frame["time_id"] = pd.to_numeric(frame["time_id"], errors="raise").astype(int)
        frame["year"] = frame["time_id"] // 4
        frame["quarter"] = frame["time_id"] % 4 + 1
        frame["firm_type"] = side
        sides.append(frame)

    firm_quarter = pd.concat(sides, ignore_index=True)
    firm_quarter = firm_quarter.dropna(subset=["BoardName", "year"]).copy()
    firm_quarter["BoardName"] = firm_quarter["BoardName"].astype(str)
    for column in ("year", "quarter"):
        firm_quarter[column] = pd.to_numeric(
            firm_quarter[column], errors="raise"
        ).astype(int)
    if not firm_quarter["quarter"].between(1, 4).all():
        raise ValueError(f"{source_name} contains invalid quarters")
    for column in ("stay", "requirement1"):
        firm_quarter[column] = pd.to_numeric(
            firm_quarter[column], errors="raise"
        ).astype("int8")

    firm_quarter["req0"] = 1
    firm_quarter["req1"] = firm_quarter["stay"].eq(1).astype("int8")
    firm_quarter["req2"] = (
        firm_quarter["req1"].eq(1) & firm_quarter["requirement1"].eq(1)
    ).astype("int8")
    event_table = (
        firm_quarter.groupby(GROUP_COLUMNS, as_index=False)[REQUIREMENT_COLUMNS]
        .max()
        .sort_values(SORT_COLUMNS)
        .reset_index(drop=True)
    )
    event_table[REQUIREMENT_COLUMNS] = event_table[REQUIREMENT_COLUMNS].astype("int8")
    return event_table[[*GROUP_COLUMNS, *REQUIREMENT_COLUMNS]]


def main() -> None:
    """Convert each requested upstream configuration without overwriting files."""
    parser = argparse.ArgumentParser(description="Build quarterly SSR movement tables.")
    parser.add_argument("--input-dir", type=Path, default=EVENT_TABLE_DIR)
    parser.add_argument("--output-dir", type=Path, default=EVENT_TABLE_DIR)
    parser.add_argument("--late", type=int, default=RUN_CONFIG["late"])
    parser.add_argument(
        "--req1-spec", "--req-spec", choices=("new", "origin"),
        default=RUN_CONFIG["req1_spec"],
    )
    parser.add_argument(
        "--stay-spec", choices=("new", "origin"), default=RUN_CONFIG["stay_spec"]
    )
    parser.add_argument(
        "--all-configurations", action="store_true",
        help="Run late={0,3}, req1_spec/stay_spec={new,origin}.",
    )
    args = parser.parse_args()
    if args.late < 0:
        parser.error("--late must be non-negative")
    configurations = (
        list(product((0, 3), ("new", "origin"), ("new", "origin")))
        if args.all_configurations
        else [(args.late, args.req1_spec, args.stay_spec)]
    )
    jobs = [
        (
            variant, late, req1_spec, stay_spec,
            configuration_dir(args.input_dir, variant, late, req1_spec, stay_spec)
            / "movement_event_candidates.csv",
            configuration_dir(args.output_dir, variant, late, req1_spec, stay_spec)
            / "movement_table.csv",
        )
        for variant in VARIANTS
        for late, req1_spec, stay_spec in configurations
    ]
    for *_, input_path, output_path in jobs:
        if not input_path.is_file():
            raise FileNotFoundError(input_path)
        if output_path.exists():
            raise FileExistsError(
                f"Output already exists; refusing to overwrite: {output_path}"
            )

    for variant, late, req1_spec, stay_spec, input_path, output_path in jobs:
        table = build_event_table(pd.read_csv(input_path), str(input_path))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        table.to_csv(output_path, index=False, mode="x")
        print(
            f"{variant} | late={late} req1={req1_spec} stay={stay_spec}: "
            f"movement={len(table):,}",
            flush=True,
        )


if __name__ == "__main__":
    main()
