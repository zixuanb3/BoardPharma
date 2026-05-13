"""
Purpose:
Build a firm-year event table that combines product lists with event-specific
partner boards.

Process:
- Build a full BoardName-year timeline from SSR product data.
- Construct event rows for direct and indirect interlocks with stay_x_years filtering.
- Construct director-move events (to_B_still_in_A and to_B_not_in_A) with the same
    stay_x_years persistence rule.
- Merge events onto the full timeline and export one consolidated table.

Input:
- InterimData/boardex_ssr_price_sample.csv
- InterimData/boardex_interlock_direct_firmpair.dta
- InterimData/boardex_interlock_indirect_firmpair.dta
- InterimData/boardex_pharma.dta

Output:
- data/event_B.xlsx
- data/event_A.xlsx
"""

from itertools import product
from pathlib import Path

import pandas as pd


CURRENT_PATH = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_PATH.parent.parent
INTERIM_DATA_PATH = PROJECT_ROOT / "InterimData"
OUTPUT_PATH_B = PROJECT_ROOT / "data" / "event_B.xlsx"
OUTPUT_PATH_A = PROJECT_ROOT / "data" / "event_A.xlsx"


# ========================== USER CONFIG ==========================
# stay_x_years:
# - Persistence filter used in both interlock and director-move event construction.
# - Interlocks require the same firm pair to remain linked for future years.
# - Director moves require destination board B to remain in future board lists.
RUN_CONFIG = {
    "stay_x_years": 3,
}
# ===============================================================


def _sorted_unique_list(values: pd.Series) -> list:
    return sorted(pd.unique(values.dropna()).tolist())


def build_base_timeline() -> pd.DataFrame:
    """Build complete BoardName-year timeline with product lists."""
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
    """Build direct or indirect interlock events under stay_x_years filtering."""
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

    # Expand symmetric pairs so each board receives its partner list.
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

    # Keep inSSR mapping for final pharma-to-pharma filtering.
    board_inssr = pharma.dropna(subset=["BoardName"]).set_index("BoardName")["inSSR"].to_dict()

    # Keep full board history first so year gaps are preserved.
    pharma_clean = pharma.dropna(subset=["DirectorID", "year", "BoardName"])
    pharma_clean["year"] = pharma_clean["year"].astype(int)

    grouped = (
        pharma_clean.groupby(["DirectorID", "year"], as_index=False)
        .agg(BoardName=("BoardName", lambda x: sorted(set(x.tolist()))))
        .sort_values(["DirectorID", "year"])
        .reset_index(drop=True)
    )

    # Build lookup for consecutive-year and persistence checks.
    board_lookup = {
        (did, int(y)): set(boards)
        for did, y, boards in grouped[["DirectorID", "year", "BoardName"]].itertuples(index=False)
    }

    # Build directional transitions from prior boards A to new boards B.
    rows = []
    for did, year, current in grouped[["DirectorID", "year", "BoardName"]].itertuples(index=False):
        year = int(year)
        
        # Require exact previous year to avoid backfilling across missing years.
        previous = board_lookup.get((did, year - 1))
        if not previous:
            continue
            
        new_boards = [b for b in current if b not in previous]
        if not new_boards:
            continue
            
        # Evaluate all A -> B combinations in this director-year.
        for b_last, b_new in product(previous, new_boards):
            # Keep only pharma-to-pharma transitions.
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


def build_transition_events(stay_x_years: int = 3, treatment_group: str = "B") -> pd.DataFrame:
    """Build movement events with stay_x_years filter for chosen treated group."""
    treatment_group = str(treatment_group).upper()
    if treatment_group not in {"A", "B"}:
        raise ValueError("treatment_group must be one of: A, B")

    pharma = pd.read_stata(INTERIM_DATA_PATH / "boardex_pharma.dta")
    transitions = build_transition_rows(pharma, stay_x_years=stay_x_years)

    if transitions.empty:
        return pd.DataFrame(columns=["BoardName", "year", "event", "BoardNamePair"])

    stay_still_col = f"stay_{stay_x_years}_years_still"
    stay_not_col = f"stay_{stay_x_years}_years_not"

    # Aggregate at A-B-year level, then collapse to B-year partner lists.
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

    counterpart_group = "A" if treatment_group == "B" else "B"

    def collapse(flag_col: str, stay_col: str, event_name: str) -> pd.DataFrame:
        tmp = pair_level.loc[
            pair_level[flag_col].eq(1) & pair_level[stay_col].eq(1),
            ["year", treatment_group, counterpart_group],
        ].dropna()

        if tmp.empty:
            return pd.DataFrame(columns=["BoardName", "year", "event", "BoardNamePair"])

        out = (
            tmp.groupby([treatment_group, "year"], as_index=False)[counterpart_group]
            .agg(_sorted_unique_list)
            .rename(columns={treatment_group: "BoardName", counterpart_group: "BoardNamePair"})
        )
        out["event"] = event_name
        out["year"] = out["year"].astype(int)
        return out[["BoardName", "year", "event", "BoardNamePair"]]

    still = collapse("to_B_still_in_A", stay_still_col, "to_B_still_in_A")
    not_in_a = collapse("to_B_not_in_A", stay_not_col, "to_B_not_in_A")
    return pd.concat([still, not_in_a], ignore_index=True)


def build_all_events(stay_x_years: int = 3, treatment_group: str = "B") -> pd.DataFrame:
    """Build and combine all event types under one persistence setting."""
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
    transition = build_transition_events(
        stay_x_years=stay_x_years,
        treatment_group=treatment_group,
    )

    events = pd.concat([direct, indirect, transition], ignore_index=True)
    # Deduplicate in case source files contain repeated pair-year entries.
    events = events.drop_duplicates(subset=["BoardName", "year", "event"]).reset_index(drop=True)
    return events


def main() -> None:
    """Run event-table construction using USER CONFIG."""
    stay_x_years = int(RUN_CONFIG["stay_x_years"])

    base = build_base_timeline()

    for treatment_group, output_path in (("B", OUTPUT_PATH_B), ("A", OUTPUT_PATH_A)):
        events = build_all_events(
            stay_x_years=stay_x_years,
            treatment_group=treatment_group,
        )

        # Keep the full timeline even when no event exists in a given year.
        result = base.merge(events, on=["BoardName", "year"], how="left")

        # Export schema for downstream scripts.
        result = result[["BoardName", "product", "year", "event", "BoardNamePair"]]
        result = result.sort_values(["BoardName", "year", "event"], na_position="last").reset_index(drop=True)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        result.to_excel(output_path, index=False)

        print(
            f"Saved: {output_path} "
            f"(stay_{stay_x_years}_years, movement_treatment_group={treatment_group})"
        )


if __name__ == "__main__":
    main()
