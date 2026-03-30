"""
Build firm-year event table with product lists and event-specific partner boards.

Output schema:
- BoardName
- product (list of products for BoardName-year)
- year
- event
- BoardNamePair (list of partner boards for that event)

Output file:
- data/event.xlsx
"""

from itertools import product
from pathlib import Path

import pandas as pd


CURRENT_PATH = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_PATH.parent
INTERIM_DATA_PATH = PROJECT_ROOT / "InterimData"
OUTPUT_PATH = PROJECT_ROOT / "data" / "event.xlsx"


def _sorted_unique_list(values: pd.Series) -> list:
    return sorted(pd.unique(values.dropna()).tolist())


def build_base_timeline() -> pd.DataFrame:
    """Build BoardName-year timeline and product list from SSR data."""
    ssr = pd.read_csv(INTERIM_DATA_PATH / "boardex_ssr_price_sample.csv")
    ssr = ssr[["BoardName", "year", "product"]]
    ssr["year"] = ssr["year"].astype(int)

    timeline = ssr[["BoardName", "year"]].drop_duplicates()
    product_list = (
        ssr.groupby(["BoardName", "year"], as_index=False)["product"]
        .agg(_sorted_unique_list)
    )

    base = timeline.merge(product_list, on=["BoardName", "year"], how="left")
    return base.sort_values(["BoardName", "year"]).reset_index(drop=True)


def build_interlock_events(
    file_name: str,
    event_name: str,
    valid_boards: set,
    stay_x_years: int = 3,
) -> pd.DataFrame:
    """Build event rows for direct/indirect interlock with stay-qualified pairs."""
    if stay_x_years < 1:
        raise ValueError("stay_x_years must be >= 1")

    interlock = pd.read_stata(INTERIM_DATA_PATH / file_name)
    interlock = interlock[["BoardName1", "BoardName2", "year"]].dropna()
    interlock["year"] = interlock["year"].astype(int)
    interlock = interlock.loc[
        interlock["BoardName1"].isin(valid_boards) & interlock["BoardName2"].isin(valid_boards)
    ].copy()

    if interlock.empty:
        return pd.DataFrame(columns=["BoardName", "year", "event", "BoardNamePair"])

    interlock["pair_min"] = interlock[["BoardName1", "BoardName2"]].min(axis=1)
    interlock["pair_max"] = interlock[["BoardName1", "BoardName2"]].max(axis=1)

    pair_year = interlock[["pair_min", "pair_max", "year"]].drop_duplicates().copy()
    pair_year_set = set(pair_year[["pair_min", "pair_max", "year"]].itertuples(index=False, name=None))

    def _check_stay(r: pd.Series) -> int:
        if stay_x_years <= 1:
            return 1
        for offset in range(1, stay_x_years):
            if (r["pair_min"], r["pair_max"], int(r["year"]) + offset) not in pair_year_set:
                return 0
        return 1

    pair_year["stay_ok"] = pair_year.apply(_check_stay, axis=1)
    pair_year = pair_year.loc[pair_year["stay_ok"].eq(1)].copy()

    if pair_year.empty:
        return pd.DataFrame(columns=["BoardName", "year", "event", "BoardNamePair"])

    # Expand both directions: BoardName1 -> BoardName2 and BoardName2 -> BoardName1.
    left = pair_year.rename(columns={"pair_min": "BoardName", "pair_max": "BoardNamePairItem"})
    right = pair_year.rename(columns={"pair_max": "BoardName", "pair_min": "BoardNamePairItem"})
    pairs = pd.concat([left, right], ignore_index=True)
    pairs = pairs[["BoardName", "year", "BoardNamePairItem"]].drop_duplicates()

    grouped = (
        pairs.groupby(["BoardName", "year"], as_index=False)["BoardNamePairItem"]
        .agg(_sorted_unique_list)
        .rename(columns={"BoardNamePairItem": "BoardNamePair"})
    )
    grouped["event"] = event_name
    return grouped[["BoardName", "year", "event", "BoardNamePair"]]


def build_transition_rows(pharma: pd.DataFrame, stay_x_years: int = 3) -> pd.DataFrame:
    """Reproduce transition logic and return per-transition rows with stay_x_years flags."""
    if stay_x_years < 1:
        raise ValueError("stay_x_years must be >= 1")

    stay_still_col = f"stay_{stay_x_years}_years_still"
    stay_not_col = f"stay_{stay_x_years}_years_not"

    # 1. Store BoardName's inSSR mapping for final checking
    board_inssr = pharma.dropna(subset=["BoardName"]).set_index("BoardName")["inSSR"].to_dict()

    # 2. Extract ALL board holdings WITHOUT filtering inSSR to correctly capture gaps
    pharma_clean = pharma.dropna(subset=["DirectorID", "year", "BoardName"])
    pharma_clean["year"] = pharma_clean["year"].astype(int)

    grouped = (
        pharma_clean.groupby(["DirectorID", "year"], as_index=False)
        .agg(BoardName=("BoardName", lambda x: sorted(set(x.tolist()))))
        .sort_values(["DirectorID", "year"])
        .reset_index(drop=True)
    )

    # 3. Build fast lookup dictionary: (DirectorID, Year) -> {Boards...}
    board_lookup = {
        (did, int(y)): set(boards)
        for did, y, boards in grouped[["DirectorID", "year", "BoardName"]].itertuples(index=False)
    }

    # 4. Process valid transitions step-by-step
    rows = []
    for did, year, current in grouped[["DirectorID", "year", "BoardName"]].itertuples(index=False):
        year = int(year)
        
        # Use EXACT previous year! (not just the last observed record via shift)
        previous = board_lookup.get((did, year - 1))
        if not previous:
            continue
            
        new_boards = [b for b in current if b not in previous]
        if not new_boards:
            continue
            
        # Check all transitions from previous year's boards to current year's NEW boards
        for b_last, b_new in product(previous, new_boards):
            # Apply inSSR filter AT THE END: both A and B must be pharma
            if board_inssr.get(b_last, 0) != 1 or board_inssr.get(b_new, 0) != 1:
                continue
                
            current_set = set(current)
            to_still = int(b_last in current_set and b_new in current_set)
            to_not = int(b_last not in current_set and b_new in current_set)

            stay_met = True
            if stay_x_years > 1:
                for offset in range(1, stay_x_years):
                    future_boards = board_lookup.get((did, year + offset), set())
                    if b_new not in future_boards:
                        stay_met = False
                        break

            stay_still = int(to_still == 1 and stay_met)
            stay_not = int(to_not == 1 and stay_met)

            rows.append(
                {
                    "year": year,
                    "A": b_last,
                    "B": b_new,
                    "to_B_still_in_A": to_still,
                    "to_B_not_in_A": to_not,
                    stay_still_col: stay_still,
                    stay_not_col: stay_not,
                }
            )

    return pd.DataFrame(rows)


def build_transition_events(stay_x_years: int = 3) -> pd.DataFrame:
    """Build movement events from A-B pair level, keeping only stay-qualified A."""
    pharma = pd.read_stata(INTERIM_DATA_PATH / "boardex_pharma.dta")
    transitions = build_transition_rows(pharma, stay_x_years=stay_x_years)

    if transitions.empty:
        return pd.DataFrame(columns=["BoardName", "year", "event", "BoardNamePair"])

    stay_still_col = f"stay_{stay_x_years}_years_still"
    stay_not_col = f"stay_{stay_x_years}_years_not"

    # Aggregate at A-B-year level first, then collapse to B-year list of qualified A.
    pair_level = (
        transitions.groupby(["year", "A", "B"], as_index=False)
        .agg(
            to_B_still_in_A=("to_B_still_in_A", "max"),
            to_B_not_in_A=("to_B_not_in_A", "max"),
            **{
                stay_still_col: (stay_still_col, "max"),
                stay_not_col: (stay_not_col, "max"),
            },
        )
    )

    def collapse(flag_col: str, stay_col: str, event_name: str) -> pd.DataFrame:
        tmp = pair_level.loc[
            pair_level[flag_col].eq(1) & pair_level[stay_col].eq(1),
            ["year", "B", "A"],
        ].dropna()

        if tmp.empty:
            return pd.DataFrame(columns=["BoardName", "year", "event", "BoardNamePair"])

        out = (
            tmp.groupby(["B", "year"], as_index=False)["A"]
            .agg(_sorted_unique_list)
            .rename(columns={"B": "BoardName", "A": "BoardNamePair"})
        )
        out["event"] = event_name
        out["year"] = out["year"].astype(int)
        return out[["BoardName", "year", "event", "BoardNamePair"]]

    still = collapse("to_B_still_in_A", stay_still_col, "to_B_still_in_A")
    not_in_a = collapse("to_B_not_in_A", stay_not_col, "to_B_not_in_A")
    return pd.concat([still, not_in_a], ignore_index=True)


def build_all_events(stay_x_years: int = 3) -> pd.DataFrame:
    ssr = pd.read_csv(INTERIM_DATA_PATH / "boardex_ssr_price_sample.csv", usecols=["BoardName"])
    valid_boards = set(ssr["BoardName"].dropna().unique())

    direct = build_interlock_events(
        file_name="boardex_interlock_direct_firmpair.dta",
        event_name="direct_interlock",
        valid_boards=valid_boards,
        stay_x_years=stay_x_years,
    )
    indirect = build_interlock_events(
        file_name="boardex_interlock_indirect_firmpair.dta",
        event_name="indirect_interlock",
        valid_boards=valid_boards,
        stay_x_years=stay_x_years,
    )
    transition = build_transition_events(stay_x_years=stay_x_years)

    events = pd.concat([direct, indirect, transition], ignore_index=True)
    # Guarantee uniqueness in case raw source has duplicated pair records.
    events = events.drop_duplicates(subset=["BoardName", "year", "event"]).reset_index(drop=True)
    return events


def main(stay_x_years: int = 1) -> None:
    base = build_base_timeline()
    events = build_all_events(stay_x_years=stay_x_years)

    # Left merge keeps full BoardName-year timeline.
    result = base.merge(events, on=["BoardName", "year"], how="left")

    # Keep requested column order.
    result = result[["BoardName", "product", "year", "event", "BoardNamePair"]]
    result = result.sort_values(["BoardName", "year", "event"], na_position="last").reset_index(drop=True)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    result.to_excel(OUTPUT_PATH, index=False)

    print(f"Saved: {OUTPUT_PATH} (stay_{stay_x_years}_years)")


if __name__ == "__main__":
    main(stay_x_years=3)
