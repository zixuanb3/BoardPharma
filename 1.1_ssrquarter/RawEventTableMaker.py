"""
Purpose:
    Build quarterly personnel-movement candidates and membership-derived firm
    interlocks from the two SSR board rosters produced by build_ssr_roster.py.
    Preserve code new/RawEventTableMaker.py event logic at the default settings.

Process:
    1. Read each quarterly roster separately; retain board roles or forward
       board continuity and deduplicate person-company-quarter memberships.
    2. Complete each person's history from one quarter before their first
       membership through one quarter after their last, including missing
       quarters as empty memberships. Do not cap at the sample's final quarter.
    3. Build company-pair interlocks from all retained personnel. Compare
       consecutive quarters for still, not, and dissolution candidates;
       allow not entries in the departure quarter or up to late quarters later.
    4. Calculate side-specific interlock conditions using req1_spec and stay
       using stay_spec. Dissolution requires the triggering director to serve
       at both companies for 0, 4, or 8 prior quarters in new, origin, or pre2.
       Keep all candidates, including failed conditions,
       deduplicate and sort, then write the eleven selected candidate columns.

Input:
    data/ssrquarter/ssr_company_roster_imputed_yearquarter.csv
    data/ssrquarter/ssr_company_roster_drop_sentinel_yearquarter.csv
    Required columns: DirectorID, BoardName, year, quarter, leader_tier,
    board_continuity. Command-line options can override input/output folders.

Output:
    data/ssrquarter/event_tables/<variant>/late_<late>/
        req1_<req1_spec>/stay_<stay_spec>/movement_event_candidates.csv
        req1_<req1_spec>/stay_<stay_spec>/firm_interlock_panel.csv
    Candidate columns: event_type, DirectorID, year, quarter, FirmA, FirmB,
    stay_2_years, requirement1_A, requirement1_B, FirmA_event_time_id,
    FirmB_event_time_id. Interlock columns: BoardName, time_id,
    CounterpartBoard, year, quarter. Existing output files are not overwritten.
"""

from __future__ import annotations

import argparse
from itertools import combinations, product
from pathlib import Path

import pandas as pd


# ========================== USER CONFIG ==========================
# Select membership switches; ieoc_ref=1 requires ever_board=1.
# Input/output roots and the existing statistical settings are configured below.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUT_DIR = PROJECT_ROOT / "data" / "ssrquarter"
OUTPUT_DIR = INPUT_DIR / "event_tables"
VARIANTS = ("drop_sentinel_yearquarter",)
# VARIANTS = ("imputed_yearquarter", "drop_sentinel_yearquarter")
RUN_CONFIG = {
    "ever_board": 1,  # 0: legacy roster, 1: all-role continuity.
    "ieoc_ref": 1,  # 0: baseline BP expansion, 1: use IE/OC coverage.
    "late": 3,
    "req1_spec": "new",  # Company-pair interlock rule: new or origin.
    # Dissolution: new waives stay; origin/pre2 require 4/8 prior quarters.
    "stay_spec": "pre2",
}
# Joins use T..T+7. Dissolution looks back by the selected stay rule.
FORWARD_QUARTERS = 8
DISSOLUTION_STAY_QUARTERS = {"new": 0, "origin": 4, "pre2": 8}
CANDIDATE_COLUMNS = [
    "event_type",
    "DirectorID",
    "year",
    "quarter",
    "FirmA",
    "FirmB",
    "stay_2_years",
    "requirement1_A",
    "requirement1_B",
    "FirmA_event_time_id",
    "FirmB_event_time_id",
]
INTERLOCK_COLUMNS = ["BoardName", "time_id", "CounterpartBoard", "year", "quarter"]
History = dict[int, set[str]]
PairSet = set[tuple[str, str, int]]


def load_histories(path: Path) -> dict[int, History]:
    """Read narrow memberships and complete the original quarterly timeline."""
    frame = pd.read_csv(
        path,
        usecols=[
            "DirectorID",
            "BoardName",
            "year",
            "quarter",
            "leader_tier",
            "board_continuity",
        ],
        low_memory=False,
    )
    frame["DirectorID"] = frame["DirectorID"].astype("int64")
    frame["year"] = frame["year"].astype("int64")
    frame["quarter"] = frame["quarter"].astype("int8")
    if not frame["quarter"].between(1, 4).all():
        raise ValueError(f"{path} contains invalid quarter values")
    frame["BoardName"] = frame["BoardName"].astype(str)
    frame["leader_tier"] = frame["leader_tier"].astype("string").str.strip().str.lower()
    # The upstream files contain only SSR memberships and have no inSSR column.
    frame = frame.loc[
        frame["leader_tier"].eq("board") | frame["board_continuity"].eq(1)
    ].copy()
    frame["time_id"] = frame["year"] * 4 + frame["quarter"].astype(int) - 1
    frame = frame.drop_duplicates(["DirectorID", "time_id", "BoardName"])
    board_lists = frame.groupby(["DirectorID", "time_id"], sort=True)["BoardName"].agg(
        lambda values: set(values)
    )
    histories: dict[int, History] = {}
    for director_id, boards in board_lists.groupby(level="DirectorID", sort=True):
        observed = {int(time): names for (_, time), names in boards.items()}
        histories[int(director_id)] = {
            time: observed.get(time, set())
            for time in range(min(observed) - 1, max(observed) + 2)
        }
    return histories


def build_interlocks(histories: dict[int, History]) -> tuple[PairSet, pd.DataFrame]:
    """Return undirected pair membership and the original directed edge table."""
    pairs = {
        (firm_a, firm_b, time)
        for history in histories.values()
        for time, boards in history.items()
        for firm_a, firm_b in combinations(sorted(boards), 2)
    }
    rows = [
        (board, time, counterpart, time // 4, time % 4 + 1)
        for firm_a, firm_b, time in sorted(pairs)
        for board, counterpart in ((firm_a, firm_b), (firm_b, firm_a))
    ]
    edges = pd.DataFrame(rows, columns=INTERLOCK_COLUMNS)
    for column in ("time_id", "year", "quarter"):
        edges[column] = edges[column].astype("int64")
    edges = edges.sort_values(["BoardName", "time_id", "CounterpartBoard"]).reset_index(
        drop=True
    )
    return pairs, edges


def build_candidates(
    histories: dict[int, History],
    pairs: PairSet,
    late: int = 3,
    req1_spec: str = "new",
    stay_spec: str = "new",
) -> pd.DataFrame:
    """Build the three event types with independent requirement/stay settings."""
    if isinstance(late, bool) or not isinstance(late, int) or late < 0:
        raise ValueError("late must be a non-negative integer")
    if req1_spec not in {"new", "origin"}:
        raise ValueError("req1_spec must be new or origin")
    if stay_spec not in DISSOLUTION_STAY_QUARTERS:
        raise ValueError("stay_spec must be new, origin, or pre2")
    prior_quarters = DISSOLUTION_STAY_QUARTERS[stay_spec]
    rows: list[tuple[object, ...]] = []

    def flags(firm_a: str, firm_b: str, time: int) -> tuple[int, int, int]:
        low, high = sorted((firm_a, firm_b))
        return tuple(int((low, high, time + offset) in pairs) for offset in (-1, 0, 1))

    def append_event(
        event_type: str,
        director: int,
        history: History,
        firm_a: str,
        firm_b: str,
        a_time: int,
        b_time: int,
    ) -> None:
        a_flags = flags(firm_a, firm_b, a_time)
        b_flags = flags(firm_a, firm_b, b_time)
        if event_type == "to_B_still_in_A":
            req_a = req_b = int(a_flags[0] == 0)
        elif event_type == "to_B_not_in_A":
            req_a = (
                int(not any(a_flags)) if req1_spec == "new" else int(a_flags[1] == 0)
            )
            req_b = (
                int(not any(b_flags)) if req1_spec == "new" else int(b_flags[1] == 0)
            )
        else:
            if a_flags[0] != 1:
                return
            req_a = req_b = (
                int(a_flags == (1, 0, 0))
                if req1_spec == "new"
                else int(a_flags[1] == 0)
            )

        if event_type == "interlock_dissolution":
            # With no required quarters, new passes the stay condition.
            stay = int(
                all(
                    {firm_a, firm_b}.issubset(history.get(time, set()))
                    for time in range(a_time - prior_quarters, a_time)
                )
            )
        else:
            stay = int(
                all(
                    firm_b in history.get(time, set())
                    for time in range(b_time, b_time + FORWARD_QUARTERS)
                )
            )
        rows.append(
            (
                event_type,
                director,
                a_time // 4,
                a_time % 4 + 1,
                firm_a,
                firm_b,
                stay,
                req_a,
                req_b,
                a_time,
                b_time,
            )
        )

    for director, history in histories.items():
        times = list(history)
        for previous_time, time in zip(times, times[1:]):
            previous, current = history[previous_time], history[time]
            stayed, entered, left = (
                previous & current,
                current - previous,
                previous - current,
            )
            for firm_a in sorted(stayed):
                for firm_b in sorted(entered):
                    append_event(
                        "to_B_still_in_A", director, history, firm_a, firm_b, time, time
                    )

            if left:
                entries = {firm_b: time for firm_b in sorted(entered)}
                seen = set(current)
                for lag in range(1, late + 1):
                    future = history.get(time + lag, set())
                    for firm_b in sorted(future - previous - seen):
                        entries.setdefault(firm_b, time + lag)
                    seen.update(future)
                for firm_a in sorted(left):
                    for firm_b, b_time in sorted(entries.items()):
                        append_event(
                            "to_B_not_in_A",
                            director,
                            history,
                            firm_a,
                            firm_b,
                            time,
                            b_time,
                        )

            for firm_b in sorted(left):
                for firm_a in sorted(stayed | (left - {firm_b})):
                    append_event(
                        "interlock_dissolution",
                        director,
                        history,
                        firm_a,
                        firm_b,
                        time,
                        time,
                    )

    candidates = pd.DataFrame(rows, columns=CANDIDATE_COLUMNS)
    for column in CANDIDATE_COLUMNS:
        if column not in {"event_type", "FirmA", "FirmB"}:
            candidates[column] = candidates[column].astype("int64")
    keys = ["event_type", "DirectorID", "FirmA_event_time_id", "FirmA", "FirmB"]
    return candidates.drop_duplicates(keys).sort_values(keys).reset_index(drop=True)


def main() -> None:
    """Write each requested configuration to its own non-overwriting folder."""
    parser = argparse.ArgumentParser(
        description="Build quarterly SSR movement and interlock tables."
    )
    parser.add_argument("--input-dir", type=Path, default=INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--late", type=int, default=RUN_CONFIG["late"])
    parser.add_argument(
        "--req1-spec",
        "--req-spec",
        choices=("new", "origin"),
        default=RUN_CONFIG["req1_spec"],
    )
    parser.add_argument(
        "--stay-spec", choices=tuple(DISSOLUTION_STAY_QUARTERS),
        default=RUN_CONFIG["stay_spec"],
    )
    parser.add_argument(
        "--all-configurations",
        action="store_true",
        help="Run late={0,3}, req1_spec={new,origin}, stay_spec={new,origin,pre2}.",
    )
    from ever_board_config import (
        membership_path,
        validate_policies,
    )

    parser.add_argument("--ever-board", "--ever_board", type=int, choices=(0, 1), default=RUN_CONFIG["ever_board"])
    parser.add_argument("--ieoc-ref", "--ieoc_ref", type=int, choices=(0, 1), default=RUN_CONFIG["ieoc_ref"])
    args = parser.parse_args()
    try:
        validate_policies(args.ever_board, args.ieoc_ref)
    except ValueError as error:
        parser.error(str(error))
    args.input_dir = membership_path(args.input_dir, args.ever_board, args.ieoc_ref)
    args.output_dir = membership_path(args.output_dir, args.ever_board, args.ieoc_ref)
    if args.late < 0:
        parser.error("--late must be non-negative")
    configurations = (
        list(product((0, 3), ("new", "origin"), DISSOLUTION_STAY_QUARTERS))
        if args.all_configurations
        else [(args.late, args.req1_spec, args.stay_spec)]
    )
    jobs = [
        (
            variant,
            late,
            req1,
            stay,
            args.output_dir
            / variant
            / f"late_{late}"
            / f"req1_{req1}"
            / f"stay_{stay}",
        )
        for variant in VARIANTS
        for late, req1, stay in configurations
    ]
    for variant in VARIANTS:
        path = args.input_dir / f"ssr_company_roster_{variant}.csv"
        if not path.is_file():
            raise FileNotFoundError(path)
    for *_, directory in jobs:
        for name in ("movement_event_candidates.csv", "firm_interlock_panel.csv"):
            if (directory / name).exists():
                raise FileExistsError(
                    f"Output already exists; refusing to overwrite: {directory / name}"
                )

    for variant in VARIANTS:
        histories = load_histories(args.input_dir / f"ssr_company_roster_{variant}.csv")
        pairs, edges = build_interlocks(histories)
        for job_variant, late, req1, stay, directory in jobs:
            if job_variant != variant:
                continue
            candidates = build_candidates(histories, pairs, late, req1, stay)
            directory.mkdir(parents=True, exist_ok=True)
            edges.to_csv(directory / "firm_interlock_panel.csv", index=False)
            candidates.to_csv(directory / "movement_event_candidates.csv", index=False)
            print(
                f"{variant} | late={late} req1={req1} stay={stay}: candidates={len(candidates):,}, interlocks={len(edges):,}",
                flush=True,
            )


if __name__ == "__main__":
    main()
