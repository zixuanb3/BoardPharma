"""
Purpose:
    Build quarterly SSR company-product event panels from the movement tables
    produced by 1.1_ssrquarter/EventTableMaker.py, using req2 for event eligibility.

Process:
    1. Read SSR data, remove missing or blank BoardName rows, and aggregate by
       company, year, quarter, product, and ATC3. Sum revenue/quantity and take
       arithmetic means of the existing price1/price0; do not reconstruct prices.
    2. Read each variant's selected late, req1_spec, and stay_spec movement table.
       Select one event type and A/B identity, preserving actual event quarters.
    3. Merge req2-qualified events onto all observed SSR product quarters. Find
       first events from all selected base events, including those failing req2.
       Check company-product completeness over T-4 through T+7 and create the
       same event-quarter membership and balance columns as code new.
    4. Preserve code new's retained values, types, row order, and column order.
       Omit stay_2_years, pure_event, and first_event_period from saved panels;
       keep first_event_year, first_event_quarter, and first_event unchanged.

Input:
    InterimData/boardex_ssr_price_sample.csv
    data/ssrquarter/event_tables/<variant>/late_<late>/
        req1_<req1_spec>/stay_<stay_spec>/movement_table.csv
    Use the original local SSR file and existing parameter-specific movement
    tables. The three parameters select upstream results; no flags are rebuilt.

Output:
    data/ssrquarter/firm_panels/<variant>/late_<late>/
        req1_<req1_spec>/stay_<stay_spec>/quarter-level_<A|B>/
        ssr_firm_panel_<event>_req2.csv
    Dissolution uses the stem interlock_dissolution_leave_B, as in code new.
    Existing outputs are not overwritten. --all-configurations processes
    late={0,3} and req1_spec/stay_spec={new,origin}, for both quarterly variants,
    three event types, and A/B identities: 96 files. Paths can be overridden.
"""

from __future__ import annotations

import argparse
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SSR_INPUT = PROJECT_ROOT / "InterimData" / "boardex_ssr_price_sample.csv"
EVENT_TABLE_DIR = PROJECT_ROOT / "data" / "ssrquarter" / "event_tables"
OUTPUT_DIR = PROJECT_ROOT / "data" / "ssrquarter" / "firm_panels"
VARIANTS = ("imputed_yearquarter", "drop_sentinel_yearquarter")
RUN_CONFIG = {"late": 3, "req1_spec": "new", "stay_spec": "new"}
EVENT_TYPES = ("to_B_not_in_A", "to_B_still_in_A", "interlock_dissolution")
TREATMENT_GROUPS = ("B", "A")
BALANCE_WINDOW = (-4, 7)
MERGE_KEYS = ["BoardName", "year", "quarter"]


def configuration_dir(
    root: Path, variant: str, late: int, req1_spec: str, stay_spec: str
) -> Path:
    """Return the upstream configuration's directory under the selected root."""
    return (
        root / variant / f"late_{late}"
        / f"req1_{req1_spec}" / f"stay_{stay_spec}"
    )


def load_ssr_panel(path: Path) -> pd.DataFrame:
    """Aggregate the original SSR observations after removing empty names."""
    ssr = pd.read_csv(path)
    ssr = ssr.loc[
        ssr["BoardName"].notna() & ssr["BoardName"].astype(str).str.strip().ne("")
    ]
    group_columns = [*MERGE_KEYS, "product", "atc3"]
    panel = (
        ssr[group_columns + ["revenue", "quantity", "price1", "price0"]]
        .groupby(group_columns, as_index=False)
        .agg(
            revenue=("revenue", "sum"),
            quantity=("quantity", "sum"),
            price1=("price1", "mean"),
            price0=("price0", "mean"),
        )
        .sort_values(["BoardName", "product", "year", "quarter"])
    )
    panel["quarter"] = panel["quarter"].astype(np.int8)
    return panel


def load_event_table(path: Path) -> pd.DataFrame:
    """Read quarterly eligibility flags without recalculating upstream rules."""
    table = pd.read_csv(path)
    required = {*MERGE_KEYS, "event_type", "firm_type", "req0", "req1", "req2"}
    missing = sorted(required - set(table.columns))
    if missing:
        raise ValueError(f"{path} is missing columns: {missing}")
    table["BoardName"] = table["BoardName"].astype(str)
    table["event_type"] = table["event_type"].astype(str)
    for column in ("year", "quarter"):
        table[column] = pd.to_numeric(table[column], errors="raise").astype(int)
    if not table["quarter"].between(1, 4).all():
        raise ValueError(f"{path} contains invalid quarters")
    for column in ("req0", "req1", "req2"):
        table[column] = pd.to_numeric(table[column], errors="raise").astype(np.int8)
    table["firm_type"] = table["firm_type"].astype(str).str.upper()
    return table


def add_time_id(frame: pd.DataFrame) -> pd.DataFrame:
    """Add code new's consecutive integer quarter identifier to a copy."""
    frame = frame.copy()
    frame["_time_id"] = frame["year"].astype(int) * 4 + frame["quarter"].astype(int) - 1
    return frame


def build_event_panel(
    ssr_base: pd.DataFrame,
    event_table: pd.DataFrame,
    event_type: str,
    treatment_group: str,
) -> pd.DataFrame:
    """Build one req2 panel while retaining unfiltered first-event timing."""
    if event_type not in EVENT_TYPES or treatment_group not in TREATMENT_GROUPS:
        raise ValueError("Unsupported event type or A/B treatment group")
    selected = event_table.loc[
        event_table["event_type"].eq(event_type)
        & event_table["firm_type"].eq(treatment_group)
    ]
    pure_events = (
        selected[MERGE_KEYS].dropna().drop_duplicates().assign(pure_event=np.int8(1))
    )
    req_events = (
        selected.loc[selected["req2"].eq(1), MERGE_KEYS]
        .dropna().drop_duplicates().assign(event=np.int8(1))
    )
    panel = ssr_base.merge(req_events, on=MERGE_KEYS, how="left")
    panel = panel.merge(pure_events, on=MERGE_KEYS, how="left")
    for column in ("event", "pure_event"):
        panel[column] = panel[column].fillna(0).astype(np.int8)
    panel = add_time_id(panel)
    pure_events = add_time_id(pure_events)
    req_events = add_time_id(req_events)

    first_event = (
        pure_events.groupby("BoardName", as_index=False)["_time_id"]
        .min().rename(columns={"_time_id": "first_event_period"})
    )
    first_event["first_event_year"] = first_event["first_event_period"] // 4
    first_event["first_event_quarter"] = first_event["first_event_period"] % 4 + 1
    panel = panel.merge(first_event, on="BoardName", how="left")
    panel["first_event"] = (
        panel["pure_event"].eq(1)
        & panel["_time_id"].eq(panel["first_event_period"])
    ).astype(np.int8)

    periods_lookup = (
        panel.groupby(["BoardName", "product"])["_time_id"]
        .agg(lambda values: set(values.dropna().astype(int).tolist()))
        .to_dict()
    )
    board_product_keys = pd.MultiIndex.from_frame(panel[["BoardName", "product"]])
    observed_time_ids = set(panel["_time_id"].astype(int))
    event_records = req_events.loc[req_events["_time_id"].isin(observed_time_ids)]
    dynamic_columns: dict[str, np.ndarray] = {}
    # A period is computed once, in its first-encounter order. The reference
    # recomputes it for each event company, overwriting identical column values.
    for event_period in event_records["_time_id"].drop_duplicates():
        event_period = int(event_period)
        label = f"{event_period // 4}q{event_period % 4 + 1}"
        event_boards = set(
            req_events.loc[req_events["_time_id"].eq(event_period), "BoardName"]
        )
        required = set(range(
            event_period + BALANCE_WINDOW[0], event_period + BALANCE_WINDOW[1] + 1
        ))
        qualified = {
            board_product
            for board_product, periods in periods_lookup.items()
            if board_product[0] in event_boards and required.issubset(periods)
        }
        dynamic_columns[f"balance_panel_{label}"] = (
            board_product_keys.isin(qualified).astype(np.int8)
        )
        dynamic_columns[f"event_{label}"] = (
            panel["BoardName"].isin(event_boards).to_numpy(dtype=np.int8)
        )
    if dynamic_columns:
        panel = pd.concat([panel, pd.DataFrame(dynamic_columns, index=panel.index)], axis=1)

    panel = panel.drop(columns=["_time_id", "pure_event", "first_event_period"])
    ordered = panel.columns.tolist()
    ordered.insert(4, ordered.pop(ordered.index("event")))
    return panel[ordered]


def output_filename(event_type: str) -> str:
    """Keep code new's event stems and req2 filename suffix."""
    stem = "interlock_dissolution_leave_B" if event_type == "interlock_dissolution" else event_type
    return f"ssr_firm_panel_{stem}_req2.csv"


def main() -> None:
    """Generate selected panels after checking all inputs and output collisions."""
    parser = argparse.ArgumentParser(description="Build quarterly req2 SSR firm panels.")
    parser.add_argument("--ssr-input", type=Path, default=SSR_INPUT)
    parser.add_argument("--input-dir", type=Path, default=EVENT_TABLE_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
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
            / "movement_table.csv",
            configuration_dir(args.output_dir, variant, late, req1_spec, stay_spec),
        )
        for variant in VARIANTS
        for late, req1_spec, stay_spec in configurations
    ]
    if not args.ssr_input.is_file():
        raise FileNotFoundError(args.ssr_input)
    for *_, input_path, output_dir in jobs:
        if not input_path.is_file():
            raise FileNotFoundError(input_path)
        for treatment_group, event_type in product(TREATMENT_GROUPS, EVENT_TYPES):
            output_path = (
                output_dir / f"quarter-level_{treatment_group}" / output_filename(event_type)
            )
            if output_path.exists():
                raise FileExistsError(
                    f"Output already exists; refusing to overwrite: {output_path}"
                )

    ssr_base = load_ssr_panel(args.ssr_input)
    for variant, late, req1_spec, stay_spec, input_path, output_dir in jobs:
        event_table = load_event_table(input_path)
        for treatment_group, event_type in product(TREATMENT_GROUPS, EVENT_TYPES):
            panel = build_event_panel(ssr_base, event_table, event_type, treatment_group)
            output_path = (
                output_dir / f"quarter-level_{treatment_group}" / output_filename(event_type)
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            panel.to_csv(output_path, index=False, mode="x")
            print(
                f"{variant} | late={late} req1={req1_spec} stay={stay_spec} | "
                f"{treatment_group} {event_type}: "
                f"rows={len(panel):,}, columns={len(panel.columns)}",
                flush=True,
            )


if __name__ == "__main__":
    main()
