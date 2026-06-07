"""
Purpose:
Build reusable movement and firm-interlock tables for the three movement event
types using BoardEx pharma board memberships.

Process:
- Clean SSR board memberships from `boardex_pharma.dta`.
- Build director-year board lists and expand them to complete yearly panels.
- Construct yearly firm interlock panels from unordered board pairs.
- Construct movement candidate rows with persistence and requirement flags,
  including firm-level requirement2 flags for treated-side A and B.

Input:
- InterimData/boardex_pharma.dta
- InterimData/boardex_interlock_indirect_firmpair.dta

Output:
- data/movement_tables/movement_event_candidates.csv
- data/movement_tables/firm_interlock_panel.csv
- data/movement_tables/indirect_interlock_event_candidates.csv
"""

from __future__ import annotations
from itertools import combinations
from pathlib import Path
import pandas as pd


CURRENT_PATH = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_PATH.parent.parent
INPUT_PATH = PROJECT_ROOT / "InterimData" / "boardex_pharma.dta"
INDIRECT_INPUT_PATH = PROJECT_ROOT / "InterimData" / "boardex_interlock_indirect_firmpair.dta"
OUTPUT_DIR = PROJECT_ROOT / "data" / "movement_tables"
MOVEMENT_CANDIDATES_PATH = OUTPUT_DIR / "movement_event_candidates.csv"
FIRM_INTERLOCK_EDGES_PATH = OUTPUT_DIR / "firm_interlock_panel.csv"
INDIRECT_INTERLOCK_CANDIDATES_PATH = OUTPUT_DIR / "indirect_interlock_event_candidates.csv"


# ========================== USER CONFIG ==========================
RUN_CONFIG = {
    "stay_x_years": 2,
    "requirement2_window": (-1, 1),
}
# ===============================================================

def _sorted_unique_boards(values: pd.Series) -> list[str]:
    """Return sorted unique board names after dropping missing values."""
    return sorted(pd.unique(values.dropna()).tolist())


def load_and_clean_pharma() -> pd.DataFrame:
    """Load the BoardEx pharma file and apply the requested cleaning sequence."""
    # Read only the required columns and keep SSR board memberships.
    pharma = pd.read_stata(
        INPUT_PATH,
        columns=["DirectorID", "year", "BoardName", "inSSR"],
    )

    # Apply the requested cleaning order exactly as specified.
    pharma = pharma.dropna(subset=["DirectorID"])
    pharma = pharma.dropna(subset=["year"])
    pharma = pharma.dropna(subset=["BoardName"])
    pharma = pharma.loc[pharma["inSSR"].eq(1)].copy()
    pharma = pharma[["DirectorID", "year", "BoardName"]].copy()
    pharma["year"] = pharma["year"].astype(int)
    pharma["BoardName"] = pharma["BoardName"].astype(str)
    pharma = pharma.drop_duplicates(subset=["DirectorID", "year", "BoardName"])
    return pharma.reset_index(drop=True)


def build_director_year_board_lists(pharma: pd.DataFrame) -> pd.DataFrame:
    """Aggregate cleaned memberships to DirectorID-year board lists."""
    # Each director-year keeps a sorted unique board list.
    grouped = (
        pharma.groupby(["DirectorID", "year"], as_index=False)
        .agg(board_list=("BoardName", _sorted_unique_boards))
        .sort_values(["DirectorID", "year"])
        .reset_index(drop=True)
    )
    return grouped


def build_complete_director_year_table(board_lists: pd.DataFrame) -> pd.DataFrame:
    """Create the full min_year-1 to max_year+1 skeleton for each director."""
    # Build one complete yearly skeleton per director.
    year_bounds = (
        board_lists.groupby("DirectorID", as_index=False)["year"]
        .agg(min_year="min", max_year="max")
        .sort_values("DirectorID")
        .reset_index(drop=True)
    )

    skeleton_parts = [
        pd.DataFrame(
            {
                "DirectorID": director_id,
                "year": range(min_year - 1, max_year + 2),
            }
        )
        for director_id, min_year, max_year in year_bounds.itertuples(index=False, name=None)
    ]
    skeleton = pd.concat(skeleton_parts, ignore_index=True)

    # Left join observed board lists onto the full skeleton and fill gaps with [].
    complete = (
        skeleton.merge(board_lists, on=["DirectorID", "year"], how="left")
        .sort_values(["DirectorID", "year"])
        .reset_index(drop=True)
    )
    complete["board_list"] = complete["board_list"].apply(
        lambda value: value if isinstance(value, list) else []
    )
    return complete


def build_pair_year_set(complete_table: pd.DataFrame) -> set[tuple[str, str, int]]:
    """Build the unordered firm-pair-year lookup from director-year board lists."""
    pair_year_set: set[tuple[str, str, int]] = set()

    # Each director-year with at least two boards contributes all unordered pairs.
    for year, board_list in complete_table[["year", "board_list"]].itertuples(index=False, name=None):
        if len(board_list) < 2:
            continue
        for pair_min, pair_max in combinations(board_list, 2):
            pair_year_set.add((pair_min, pair_max, int(year)))

    return pair_year_set


def build_firm_interlock_edges(pair_year_set: set[tuple[str, str, int]]) -> pd.DataFrame:
    """Expand unordered pair-year links into directional firm-year-counterpart edges."""
    rows: list[dict[str, object]] = []

    # Expand each unordered pair into two firm-centered directional rows.
    for pair_min, pair_max, year in sorted(pair_year_set, key=lambda item: (item[0], item[1], item[2])):
        rows.append(
            {
                "BoardName": pair_min,
                "year": year,
                "CounterpartBoard": pair_max,
            }
        )
        rows.append(
            {
                "BoardName": pair_max,
                "year": year,
                "CounterpartBoard": pair_min,
            }
        )

    if not rows:
        return pd.DataFrame(columns=["BoardName", "year", "CounterpartBoard"])

    edges = pd.DataFrame(rows)
    edges = edges.sort_values(["BoardName", "year", "CounterpartBoard"]).reset_index(drop=True)
    return edges


def build_board_year_interlock_lookup(
    firm_interlock_edges: pd.DataFrame,
) -> dict[tuple[str, int], set[str]]:
    """Map each BoardName-year to its interlocked SSR counterpart set."""
    if firm_interlock_edges.empty:
        return {}

    grouped = (
        firm_interlock_edges.groupby(["BoardName", "year"])["CounterpartBoard"]
        .agg(lambda values: set(values.tolist()))
        .reset_index()
    )
    return {
        (str(board_name), int(year)): set(counterparts)
        for board_name, year, counterparts in grouped.itertuples(index=False, name=None)
    }


def _pair_interlock(pair_year_set: set[tuple[str, str, int]], firm_a: str, firm_b: str, year: int) -> int:
    """Look up whether an unordered firm pair is interlocked in a given year."""
    pair_min, pair_max = sorted((firm_a, firm_b))
    return int((pair_min, pair_max, int(year)) in pair_year_set)


def _stay_forward(board_history: dict[int, set[str]], firm_b: str, event_year: int, stay_x_years: int) -> int:
    """Check whether destination board B remains present from t through t+stay_x_years-1."""
    for year in range(event_year, event_year + stay_x_years):
        if firm_b not in board_history.get(year, set()):
            return 0
    return 1


def _stay_backward(
    board_history: dict[int, set[str]],
    firm_a: str,
    firm_b: str,
    event_year: int,
    stay_x_years: int,
) -> int:
    """Check whether boards A and B co-appear for stay_x_years years before t."""
    for year in range(event_year - stay_x_years, event_year):
        boards = board_history.get(year, set())
        if firm_a not in boards or firm_b not in boards:
            return 0
    return 1


def _requirement2_window_years(event_year: int, requirement2_window: tuple[int, int]) -> list[int]:
    """Return the inclusive firm-level requirement2 window around the event year."""
    start_offset, end_offset = requirement2_window
    return list(range(event_year + start_offset, event_year + end_offset + 1))


def _requirement2_holds(
    event_type: str,
    board_name: str,
    event_year: int,
    board_year_interlock_lookup: dict[tuple[str, int], set[str]],
    requirement2_window: tuple[int, int],
) -> int:
    """Evaluate requirement2 on a treated BoardName-event_year using yearly interlock sets."""
    window_years = _requirement2_window_years(event_year, requirement2_window)
    interlock_history = [
        board_year_interlock_lookup.get((board_name, year), set())
        for year in window_years
    ]

    if event_type == "interlock_dissolution":
        return int(
            all(
                current_set.issubset(previous_set)
                for previous_set, current_set in zip(interlock_history, interlock_history[1:])
            )
        )

    if event_type == "to_B_still_in_A":
        return int(
            all(
                current_set.issuperset(previous_set)
                for previous_set, current_set in zip(interlock_history, interlock_history[1:])
            )
        )

    if event_type == "to_B_not_in_A":
        baseline_set = interlock_history[0]
        return int(all(current_set == baseline_set for current_set in interlock_history[1:]))

    raise ValueError(f"Unsupported movement event type for requirement2: {event_type}")


def load_indirect_interlock_pairs() -> pd.DataFrame:
    """Load directed indirect interlock firm-pair-year rows."""
    indirect_pairs = pd.read_stata(
        INDIRECT_INPUT_PATH,
        columns=["BoardName1", "BoardName2", "year"],
    )
    indirect_pairs = indirect_pairs.dropna(subset=["BoardName1", "BoardName2", "year"]).copy()
    indirect_pairs["event_year"] = indirect_pairs["year"].astype(int)
    indirect_pairs["BoardName"] = indirect_pairs["BoardName1"].astype(str)
    indirect_pairs["BoardNamePair"] = indirect_pairs["BoardName2"].astype(str)
    indirect_pairs["pair_min"] = indirect_pairs[["BoardName", "BoardNamePair"]].min(axis=1)
    indirect_pairs["pair_max"] = indirect_pairs[["BoardName", "BoardNamePair"]].max(axis=1)
    indirect_pairs = indirect_pairs.drop_duplicates(
        subset=["BoardName", "BoardNamePair", "event_year"]
    )
    return indirect_pairs.reset_index(drop=True)


def build_indirect_board_year_lookup(
    indirect_pairs: pd.DataFrame,
) -> dict[tuple[str, int], set[str]]:
    """Map each directed BoardName-year to its indirect counterpart set."""
    if indirect_pairs.empty:
        return {}

    grouped = (
        indirect_pairs.groupby(["BoardName", "event_year"])["BoardNamePair"]
        .agg(lambda values: set(values.tolist()))
        .reset_index()
    )
    return {
        (str(board_name), int(event_year)): set(counterparts)
        for board_name, event_year, counterparts in grouped.itertuples(index=False, name=None)
    }


def build_indirect_pair_year_set(
    indirect_pairs: pd.DataFrame,
) -> set[tuple[str, str, int]]:
    """Build unordered indirect pair-year lookup from directed rows."""
    return {
        (str(pair_min), str(pair_max), int(event_year))
        for pair_min, pair_max, event_year in indirect_pairs[
            ["pair_min", "pair_max", "event_year"]
        ].itertuples(index=False, name=None)
    }


def _pair_indirect(
    indirect_pair_year_set: set[tuple[str, str, int]],
    pair_min: str,
    pair_max: str,
    year: int,
) -> int:
    """Look up whether an unordered indirect pair exists in a given year."""
    return int((pair_min, pair_max, int(year)) in indirect_pair_year_set)


def _indirect_stay_forward(
    indirect_pair_year_set: set[tuple[str, str, int]],
    pair_min: str,
    pair_max: str,
    event_year: int,
    stay_x_years: int,
) -> int:
    """Check whether an unordered indirect pair persists from t through t+x-1."""
    for year in range(event_year, event_year + stay_x_years):
        if not _pair_indirect(indirect_pair_year_set, pair_min, pair_max, year):
            return 0
    return 1


def _indirect_requirement2_holds(
    board_name: str,
    event_year: int,
    indirect_board_year_lookup: dict[tuple[str, int], set[str]],
    requirement2_window: tuple[int, int],
) -> int:
    """Check whether a firm's indirect counterpart set weakly expands over the window."""
    window_years = _requirement2_window_years(event_year, requirement2_window)
    indirect_history = [
        indirect_board_year_lookup.get((board_name, year), set())
        for year in window_years
    ]
    return int(
        all(
            current_set.issuperset(previous_set)
            for previous_set, current_set in zip(indirect_history, indirect_history[1:])
        )
    )


def build_indirect_interlock_candidates(
    indirect_pairs: pd.DataFrame,
    stay_x_years: int,
    requirement2_window: tuple[int, int],
) -> pd.DataFrame:
    """Build directed indirect interlock event candidate rows."""
    stay_col = f"stay_{stay_x_years}_years"
    final_columns = [
        "event_type",
        "event_year",
        "BoardName",
        "BoardNamePair",
        "pair_min",
        "pair_max",
        stay_col,
        "requirement1",
        "requirement2",
        "pair_indirect_t-1",
        "pair_indirect_t",
    ]
    if indirect_pairs.empty:
        return pd.DataFrame(columns=final_columns)

    candidates = indirect_pairs[
        ["event_year", "BoardName", "BoardNamePair", "pair_min", "pair_max"]
    ].copy()
    indirect_pair_year_set = build_indirect_pair_year_set(indirect_pairs)
    indirect_board_year_lookup = build_indirect_board_year_lookup(indirect_pairs)

    candidates["event_type"] = "indirect_interlock"
    candidates["pair_indirect_t-1"] = candidates.apply(
        lambda row: _pair_indirect(
            indirect_pair_year_set,
            str(row["pair_min"]),
            str(row["pair_max"]),
            int(row["event_year"]) - 1,
        ),
        axis=1,
    ).astype("int8")
    candidates["pair_indirect_t"] = candidates.apply(
        lambda row: _pair_indirect(
            indirect_pair_year_set,
            str(row["pair_min"]),
            str(row["pair_max"]),
            int(row["event_year"]),
        ),
        axis=1,
    ).astype("int8")
    candidates[stay_col] = candidates.apply(
        lambda row: _indirect_stay_forward(
            indirect_pair_year_set,
            str(row["pair_min"]),
            str(row["pair_max"]),
            int(row["event_year"]),
            stay_x_years,
        ),
        axis=1,
    ).astype("int8")
    candidates["requirement1"] = (
        candidates["pair_indirect_t-1"].eq(0) & candidates["pair_indirect_t"].eq(1)
    ).astype("int8")
    candidates["requirement2"] = candidates.apply(
        lambda row: _indirect_requirement2_holds(
            board_name=str(row["BoardName"]),
            event_year=int(row["event_year"]),
            indirect_board_year_lookup=indirect_board_year_lookup,
            requirement2_window=requirement2_window,
        ),
        axis=1,
    ).astype("int8")

    candidates = candidates[final_columns].sort_values(
        ["event_year", "BoardName", "BoardNamePair"]
    )
    return candidates.reset_index(drop=True)


def add_requirement2_flags(
    movement_candidates: pd.DataFrame,
    firm_interlock_edges: pd.DataFrame,
    stay_x_years: int,
    requirement2_window: tuple[int, int],
) -> pd.DataFrame:
    """Add treated-side requirement2 flags using the fixed firm-level event window."""
    stay_col = f"stay_{stay_x_years}_years"
    movement_with_req2 = movement_candidates.copy()
    board_year_interlock_lookup = build_board_year_interlock_lookup(firm_interlock_edges)
    final_columns = [
        "event_type",
        "DirectorID",
        "event_year",
        "FirmA",
        "FirmB",
        stay_col,
        "requirement1",
        "requirement2_A",
        "requirement2_B",
        "pair_interlock_t-1",
        "pair_interlock_t",
    ]

    base_mask = (
        movement_with_req2[stay_col].eq(1)
        & movement_with_req2["requirement1"].eq(1)
    )
    treated_side_map = {
        "A": "FirmA",
        "B": "FirmB",
    }

    for treated_side, treated_firm_col in treated_side_map.items():
        requirement2_col = f"requirement2_{treated_side}"
        valid_board_year = (
            movement_with_req2.loc[base_mask, ["event_type", "event_year", treated_firm_col]]
            .rename(columns={treated_firm_col: "BoardName"})
            .dropna(subset=["BoardName", "event_year"])
            .drop_duplicates()
            .sort_values(["event_type", "BoardName", "event_year"])
            .reset_index(drop=True)
        )

        valid_board_year[requirement2_col] = valid_board_year.apply(
            lambda row: _requirement2_holds(
                event_type=str(row["event_type"]),
                board_name=str(row["BoardName"]),
                event_year=int(row["event_year"]),
                board_year_interlock_lookup=board_year_interlock_lookup,
                requirement2_window=requirement2_window,
            ),
            axis=1,
        ).astype("int8")

        movement_with_req2 = movement_with_req2.merge(
            valid_board_year.rename(columns={"BoardName": treated_firm_col}),
            on=["event_type", "event_year", treated_firm_col],
            how="left",
        )
        movement_with_req2[requirement2_col] = (
            movement_with_req2[requirement2_col].fillna(0).astype("int8")
        )

    return movement_with_req2[final_columns].copy()


def build_movement_candidates(
    complete_table: pd.DataFrame,
    pair_year_set: set[tuple[str, str, int]],
    stay_x_years: int,
    requirement2_window: tuple[int, int],
) -> pd.DataFrame:
    """Build movement candidate rows and all requested flags."""
    stay_col = f"stay_{stay_x_years}_years"
    candidate_rows: list[dict[str, object]] = []

    start_offset, end_offset = requirement2_window
    forward_stay_years = min(stay_x_years, max(0, end_offset) + 1)
    backward_stay_years = min(stay_x_years, max(0, -start_offset))

    # Traverse adjacent years within each director's completed yearly history.
    for director_id, director_panel in complete_table.groupby("DirectorID", sort=False):
        director_panel = director_panel.sort_values("year").reset_index(drop=True)
        year_board_pairs = list(
            director_panel[["year", "board_list"]].itertuples(index=False, name=None)
        )
        board_history = {
            int(year): set(board_list)
            for year, board_list in year_board_pairs
        }

        for idx in range(1, len(year_board_pairs)):
            prev_year, prev_list = year_board_pairs[idx - 1]
            curr_year, curr_list = year_board_pairs[idx]
            prev_year = int(prev_year)
            curr_year = int(curr_year)

            if curr_year != prev_year + 1:
                raise ValueError(
                    f"DirectorID={director_id} has a non-consecutive year gap after skeleton expansion."
                )

            prev_set = set(prev_list)
            curr_set = set(curr_list)
            stayed_set = prev_set & curr_set
            new_set = curr_set - prev_set
            left_set = prev_set - curr_set

            # Stayed A plus new B generates to_B_still_in_A candidates.
            for firm_a in sorted(stayed_set):
                for firm_b in sorted(new_set):
                    pair_tm1 = _pair_interlock(pair_year_set, firm_a, firm_b, curr_year - 1)
                    pair_t = _pair_interlock(pair_year_set, firm_a, firm_b, curr_year)
                    candidate_rows.append(
                        {
                            "event_type": "to_B_still_in_A",
                            "DirectorID": director_id,
                            "event_year": curr_year,
                            "FirmA": firm_a,
                            "FirmB": firm_b,
                            stay_col: _stay_forward(board_history, firm_b, curr_year, forward_stay_years),
                            "requirement1": int(pair_tm1 == 0),
                            "pair_interlock_t-1": pair_tm1,
                            "pair_interlock_t": pair_t,
                        }
                    )

            # Left A plus new B generates to_B_not_in_A candidates.
            for firm_a in sorted(left_set):
                for firm_b in sorted(new_set):
                    pair_tm1 = _pair_interlock(pair_year_set, firm_a, firm_b, curr_year - 1)
                    pair_t = _pair_interlock(pair_year_set, firm_a, firm_b, curr_year)
                    candidate_rows.append(
                        {
                            "event_type": "to_B_not_in_A",
                            "DirectorID": director_id,
                            "event_year": curr_year,
                            "FirmA": firm_a,
                            "FirmB": firm_b,
                            stay_col: _stay_forward(board_history, firm_b, curr_year, forward_stay_years),
                            "requirement1": int(pair_t == 0),
                            "pair_interlock_t-1": pair_tm1,
                            "pair_interlock_t": pair_t,
                        }
                    )

            # Each departing B paired with remaining counterparts generates dissolution candidates.
            for firm_b in sorted(left_set):
                counterpart_set = stayed_set | (left_set - {firm_b})
                for firm_a in sorted(counterpart_set):
                    pair_tm1 = _pair_interlock(pair_year_set, firm_a, firm_b, curr_year - 1)
                    pair_t = _pair_interlock(pair_year_set, firm_a, firm_b, curr_year)
                    candidate_rows.append(
                        {
                            "event_type": "interlock_dissolution",
                            "DirectorID": director_id,
                            "event_year": curr_year,
                            "FirmA": firm_a,
                            "FirmB": firm_b,
                            stay_col: _stay_backward(
                                board_history,
                                firm_a,
                                firm_b,
                                curr_year,
                                backward_stay_years,
                            ),
                            "requirement1": int(pair_t == 0),
                            "pair_interlock_t-1": pair_tm1,
                            "pair_interlock_t": pair_t,
                        }
                    )

    if not candidate_rows:
        empty_columns = [
            "event_type",
            "DirectorID",
            "event_year",
            "FirmA",
            "FirmB",
            stay_col,
            "requirement1",
            "pair_interlock_t-1",
            "pair_interlock_t",
        ]
        return pd.DataFrame(columns=empty_columns)

    movement_columns = [
        "event_type",
        "DirectorID",
        "event_year",
        "FirmA",
        "FirmB",
        stay_col,
        "requirement1",
        "pair_interlock_t-1",
        "pair_interlock_t",
    ]
    movement_candidates = pd.DataFrame(candidate_rows, columns=movement_columns)

    # Finalize the candidate file with the required deduplication and sort order.
    movement_candidates = movement_candidates.drop_duplicates(
        subset=["event_type", "DirectorID", "event_year", "FirmA", "FirmB"]
    )
    movement_candidates = movement_candidates.sort_values(
        ["event_type", "DirectorID", "event_year", "FirmA", "FirmB"]
    ).reset_index(drop=True)
    return movement_candidates


def main() -> None:
    """Run the full artifact-building pipeline and write both CSV outputs."""
    stay_x_years = int(RUN_CONFIG["stay_x_years"])
    requirement2_window = tuple(RUN_CONFIG["requirement2_window"])
    if stay_x_years < 1:
        raise ValueError("stay_x_years must be >= 1")
    if len(requirement2_window) != 2 or requirement2_window[0] > requirement2_window[1]:
        raise ValueError(
            "requirement2_window must be a tuple(start_offset, end_offset) with start <= end"
        )

    # Create the output folder before writing final artifacts.
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Build the cleaned director-year history and interlock lookup.
    pharma = load_and_clean_pharma()
    board_lists = build_director_year_board_lists(pharma)
    complete_table = build_complete_director_year_table(board_lists)
    pair_year_set = build_pair_year_set(complete_table)

    # Write the firm-year counterpart edge table.
    firm_interlock_edges = build_firm_interlock_edges(pair_year_set)
    firm_interlock_edges.to_csv(FIRM_INTERLOCK_EDGES_PATH, index=False)

    # Write the movement candidate table used by later requirement logic.
    movement_candidates = build_movement_candidates(
        complete_table=complete_table,
        pair_year_set=pair_year_set,
        stay_x_years=stay_x_years,
        requirement2_window=requirement2_window,
    )

    movement_candidates = add_requirement2_flags(
        movement_candidates=movement_candidates,
        firm_interlock_edges=firm_interlock_edges,
        stay_x_years=stay_x_years,
        requirement2_window=requirement2_window,
    )
    movement_candidates.to_csv(MOVEMENT_CANDIDATES_PATH, index=False)

    indirect_pairs = load_indirect_interlock_pairs()
    indirect_candidates = build_indirect_interlock_candidates(
        indirect_pairs=indirect_pairs,
        stay_x_years=stay_x_years,
        requirement2_window=requirement2_window,
    )
    indirect_candidates.to_csv(INDIRECT_INTERLOCK_CANDIDATES_PATH, index=False)

    print(f"Saved {len(firm_interlock_edges):,} rows to {FIRM_INTERLOCK_EDGES_PATH}")
    print(f"Saved {len(movement_candidates):,} rows to {MOVEMENT_CANDIDATES_PATH}")
    print(f"Saved {len(indirect_candidates):,} rows to {INDIRECT_INTERLOCK_CANDIDATES_PATH}")


if __name__ == "__main__":
    main()
