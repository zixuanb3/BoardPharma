"""
Purpose:
Build raw event-candidate tables for director movement, direct interlock, and
indirect interlock events.

Process:
1. Load in-SSR BoardEx director-board memberships from `boardex_pharma.dta`.
2. Build complete director-year board histories and derive movement events.
3. Derive yearly firm-interlock edges from those director memberships.
4. Add independent `stay`, `requirement1`, and `requirement2` flags to movement
   candidates. No flag is forced to zero because another flag fails.
5. Load direct and indirect interlock source files, then keep only pairs where
   both firms appear in `boardex_ssr_price_sample.csv`.
6. Add independent `stay`, `requirement1`, and `requirement2` flags to direct
   and indirect interlock candidates.
7. Combine direct and indirect interlock candidates into one output table,
   using `event_type` to distinguish `direct_interlock` from
   `indirect_interlock`.
8. Write all raw event tables to `data/event_tables`.

Input:
- InterimData/boardex_pharma.dta
- InterimData/boardex_ssr_price_sample.csv
- InterimData/boardex_interlock_indirect_firmpair.dta
- InterimData/boardex_interlock_direct_firmpair.dta

Output:
- data/event_tables/firm_interlock_panel.csv
- data/event_tables/movement_event_candidates.csv
- data/event_tables/interlock_event_candidates.csv
"""

from __future__ import annotations

from itertools import combinations
from pathlib import Path

import pandas as pd


# Project paths are resolved from this script so it can be run from any cwd.
CURRENT_PATH = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_PATH.parent.parent
INTERIM_DATA_PATH = PROJECT_ROOT / "InterimData"
OUTPUT_DIR = PROJECT_ROOT / "data" / "event_tables"

PHARMA_PATH = INTERIM_DATA_PATH / "boardex_pharma.dta"
SSR_SAMPLE_PATH = INTERIM_DATA_PATH / "boardex_ssr_price_sample.csv"
INDIRECT_INPUT_PATH = INTERIM_DATA_PATH / "boardex_interlock_indirect_firmpair.dta"
DIRECT_INPUT_PATH = INTERIM_DATA_PATH / "boardex_interlock_direct_firmpair.dta"

FIRM_INTERLOCK_EDGES_PATH = OUTPUT_DIR / "firm_interlock_panel.csv"
MOVEMENT_CANDIDATES_PATH = OUTPUT_DIR / "movement_event_candidates.csv"
INTERLOCK_CANDIDATES_PATH = OUTPUT_DIR / "interlock_event_candidates.csv"

PairYearSet = set[tuple[str, str, int]]
CounterpartLookup = dict[tuple[str, int], set[str]]


# ========================== USER CONFIG ==========================
RUN_CONFIG = {
    "stay_x_years": 2,
    "requirement2_window": (-1, 1),
}
# ===============================================================


def build_counterpart_lookup(
    data: pd.DataFrame,
    board_col: str,
    year_col: str,
    counterpart_col: str,
) -> CounterpartLookup:
    """Build a BoardName-year -> counterpart-set lookup used by both classes."""
    if data.empty:
        return {}

    grouped = (
        data.groupby([board_col, year_col])[counterpart_col]
        .agg(lambda values: set(values.tolist()))
        .reset_index()
    )
    return {
        (str(board), int(year)): set(counterparts)
        for board, year, counterparts in grouped.itertuples(index=False, name=None)
    }


class MovementEventBuilder:
    """Build director-movement events from in-SSR BoardEx membership histories."""

    def __init__(self, input_path: Path, stay_x_years: int, requirement2_window: tuple[int, int]) -> None:
        """Store movement-event configuration and derive stay-window lengths."""
        self.input_path = input_path
        self.stay_x_years = stay_x_years
        self.requirement2_window = requirement2_window
        self.stay_col = f"stay_{stay_x_years}_years"

        start_offset, end_offset = requirement2_window
        self.forward_stay_years = min(stay_x_years, max(0, end_offset) + 1)
        self.backward_stay_years = min(stay_x_years, max(0, -start_offset))

    def build(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Return movement candidates and membership-derived firm-interlock edges."""
        # 1) Load movement source universe from boardex_pharma.dta.
        memberships = pd.read_stata(
            self.input_path,
            columns=["DirectorID", "year", "BoardName", "inSSR"],
        )
        memberships = memberships.dropna(subset=["DirectorID", "year", "BoardName"])
        # Movement events are defined only on directors' in-SSR board seats.
        memberships = memberships.loc[
            memberships["inSSR"].eq(1),
            ["DirectorID", "year", "BoardName"],
        ].copy()
        memberships["year"] = memberships["year"].astype(int)
        memberships["BoardName"] = memberships["BoardName"].astype(str)
        memberships = memberships.drop_duplicates(subset=["DirectorID", "year", "BoardName"])

        # 2) Collapse each director-year to a sorted board list, then complete
        #    each director's timeline from min_year - 1 through max_year + 1.
        board_lists = (
            memberships.groupby(["DirectorID", "year"], as_index=False)
            .agg(board_list=("BoardName", lambda values: sorted(pd.unique(values.dropna()).tolist())))
            .sort_values(["DirectorID", "year"])
            .reset_index(drop=True)
        )
        if board_lists.empty:
            complete_history = pd.DataFrame(columns=["DirectorID", "year", "board_list"])
        else:
            year_bounds = board_lists.groupby("DirectorID", as_index=False)["year"].agg(
                min_year="min",
                max_year="max",
            )
            skeleton = pd.concat(
                [
                    pd.DataFrame(
                        {
                            "DirectorID": director_id,
                            "year": range(int(min_year) - 1, int(max_year) + 2),
                        }
                    )
                    for director_id, min_year, max_year in year_bounds.itertuples(index=False, name=None)
                ],
                ignore_index=True,
            )
            complete_history = skeleton.merge(board_lists, on=["DirectorID", "year"], how="left")
            complete_history = complete_history.sort_values(["DirectorID", "year"]).reset_index(drop=True)
            complete_history["board_list"] = complete_history["board_list"].apply(
                lambda value: value if isinstance(value, list) else []
            )

        # 3) Build membership-derived firm interlocks. The internal lookup is
        #    undirected; the output edge table is directed and firm-centered.
        pair_year_set: PairYearSet = set()
        for year, board_list in complete_history[["year", "board_list"]].itertuples(index=False, name=None):
            pair_year_set.update(
                (firm_a, firm_b, int(year))
                for firm_a, firm_b in combinations(sorted(board_list), 2)
            )

        edge_rows = [
            {"BoardName": firm_a, "year": year, "CounterpartBoard": firm_b}
            for firm_a, firm_b, year in sorted(pair_year_set)
        ] + [
            {"BoardName": firm_b, "year": year, "CounterpartBoard": firm_a}
            for firm_a, firm_b, year in sorted(pair_year_set)
        ]
        firm_interlock_edges = (
            pd.DataFrame(edge_rows).sort_values(["BoardName", "year", "CounterpartBoard"]).reset_index(drop=True)
            if edge_rows
            else pd.DataFrame(columns=["BoardName", "year", "CounterpartBoard"])
        )

        # 4) Compare adjacent director-years and write movement candidates.
        movement_rows: list[dict[str, object]] = []
        for director_id, director_panel in complete_history.groupby("DirectorID", sort=False):
            year_board_pairs = list(
                director_panel.sort_values("year")[["year", "board_list"]].itertuples(
                    index=False,
                    name=None,
                )
            )
            board_history = {int(year): set(board_list) for year, board_list in year_board_pairs}

            for (prev_year, prev_list), (event_year, current_list) in zip(
                year_board_pairs,
                year_board_pairs[1:],
            ):
                prev_year = int(prev_year)
                event_year = int(event_year)
                if event_year != prev_year + 1:
                    raise ValueError(
                        f"DirectorID={director_id} has a non-consecutive year gap after skeleton expansion."
                    )

                previous_boards = set(prev_list)
                current_boards = set(current_list)
                stayed_boards = previous_boards & current_boards
                new_boards = current_boards - previous_boards
                left_boards = previous_boards - current_boards

                # to_B_still_in_A: director stays on A and newly joins B.
                for firm_a in sorted(stayed_boards):
                    for firm_b in sorted(new_boards):
                        firm_low, firm_high = sorted((firm_a, firm_b))
                        pair_tm1 = int((firm_low, firm_high, event_year - 1) in pair_year_set)
                        pair_t = int((firm_low, firm_high, event_year) in pair_year_set)
                        stay = int(
                            all(
                                firm_b in board_history.get(year, set())
                                for year in range(event_year, event_year + self.forward_stay_years)
                            )
                        )
                        movement_rows.append(
                            {
                                "event_type": "to_B_still_in_A",
                                "DirectorID": director_id,
                                "event_year": event_year,
                                "FirmA": firm_a,
                                "FirmB": firm_b,
                                self.stay_col: stay,
                                "requirement1": int(pair_tm1 == 0),
                                "pair_interlock_t-1": pair_tm1,
                                "pair_interlock_t": pair_t,
                            }
                        )

                # to_B_not_in_A: director leaves A and newly joins B.
                for firm_a in sorted(left_boards):
                    for firm_b in sorted(new_boards):
                        firm_low, firm_high = sorted((firm_a, firm_b))
                        pair_tm1 = int((firm_low, firm_high, event_year - 1) in pair_year_set)
                        pair_t = int((firm_low, firm_high, event_year) in pair_year_set)
                        stay = int(
                            all(
                                firm_b in board_history.get(year, set())
                                for year in range(event_year, event_year + self.forward_stay_years)
                            )
                        )
                        movement_rows.append(
                            {
                                "event_type": "to_B_not_in_A",
                                "DirectorID": director_id,
                                "event_year": event_year,
                                "FirmA": firm_a,
                                "FirmB": firm_b,
                                self.stay_col: stay,
                                "requirement1": int(pair_t == 0),
                                "pair_interlock_t-1": pair_tm1,
                                "pair_interlock_t": pair_t,
                            }
                        )

                # interlock_dissolution: departing B paired with remaining/other prior boards.
                for firm_b in sorted(left_boards):
                    for firm_a in sorted(stayed_boards | (left_boards - {firm_b})):
                        firm_low, firm_high = sorted((firm_a, firm_b))
                        pair_tm1 = int((firm_low, firm_high, event_year - 1) in pair_year_set)
                        pair_t = int((firm_low, firm_high, event_year) in pair_year_set)
                        stay = int(
                            all(
                                {firm_a, firm_b}.issubset(board_history.get(year, set()))
                                for year in range(event_year - self.backward_stay_years, event_year)
                            )
                        )
                        movement_rows.append(
                            {
                                "event_type": "interlock_dissolution",
                                "DirectorID": director_id,
                                "event_year": event_year,
                                "FirmA": firm_a,
                                "FirmB": firm_b,
                                self.stay_col: stay,
                                "requirement1": int(pair_t == 0),
                                "pair_interlock_t-1": pair_tm1,
                                "pair_interlock_t": pair_t,
                            }
            )

        movement_columns = [
            "event_type",
            "DirectorID",
            "event_year",
            "FirmA",
            "FirmB",
            self.stay_col,
            "requirement1",
            "pair_interlock_t-1",
            "pair_interlock_t",
        ]
        # Deduplicate exact director-firm-pair candidates after all director-year transitions are scanned.
        movement_candidates = (
            pd.DataFrame(movement_rows, columns=movement_columns)
            if movement_rows
            else pd.DataFrame(columns=movement_columns)
        )
        movement_candidates = (
            movement_candidates.drop_duplicates(subset=["event_type", "DirectorID", "event_year", "FirmA", "FirmB"])
            .sort_values(["event_type", "DirectorID", "event_year", "FirmA", "FirmB"])
            .reset_index(drop=True)
        )

        # 5) Add independent movement requirement2 for A and B sides.
        interlock_lookup = build_counterpart_lookup(
            firm_interlock_edges,
            "BoardName",
            "year",
            "CounterpartBoard",
        )
        for side, firm_col in {"A": "FirmA", "B": "FirmB"}.items():
            requirement_col = f"requirement2_{side}"
            board_years = (
                movement_candidates[["event_type", "event_year", firm_col]]
                .rename(columns={firm_col: "BoardName"})
                .dropna(subset=["BoardName", "event_year"])
                .drop_duplicates()
                .sort_values(["event_type", "BoardName", "event_year"])
                .reset_index(drop=True)
            )

            requirement_values: list[int] = []
            for row in board_years.itertuples(index=False):
                # Requirement2 is evaluated over the configured event-year window and is not
                # conditioned on stay or requirement1.
                history = [
                    interlock_lookup.get((str(row.BoardName), year), set())
                    for year in range(
                        int(row.event_year) + self.requirement2_window[0],
                        int(row.event_year) + self.requirement2_window[1] + 1,
                    )
                ]
                if row.event_type == "interlock_dissolution":
                    value = int(all(current.issubset(previous) for previous, current in zip(history, history[1:])))
                elif row.event_type == "to_B_still_in_A":
                    value = int(all(current.issuperset(previous) for previous, current in zip(history, history[1:])))
                elif row.event_type == "to_B_not_in_A":
                    value = int(all(current == history[0] for current in history[1:]))
                else:
                    raise ValueError(f"Unsupported movement event type for requirement2: {row.event_type}")
                requirement_values.append(value)

            board_years[requirement_col] = requirement_values
            movement_candidates = movement_candidates.merge(
                board_years.rename(columns={"BoardName": firm_col}),
                on=["event_type", "event_year", firm_col],
                how="left",
            )
            movement_candidates[requirement_col] = movement_candidates[requirement_col].fillna(0).astype("int8")

        movement_candidates = movement_candidates[
            [
                "event_type",
                "DirectorID",
                "event_year",
                "FirmA",
                "FirmB",
                self.stay_col,
                "requirement1",
                "requirement2_A",
                "requirement2_B",
                "pair_interlock_t-1",
                "pair_interlock_t",
            ]
        ].copy()
        return movement_candidates, firm_interlock_edges


class InterlockEventBuilder:
    """Build one combined direct/indirect interlock event table."""

    def __init__(self, ssr_sample_path: Path, stay_x_years: int, requirement2_window: tuple[int, int]) -> None:
        """Store interlock-event configuration shared by direct and indirect inputs."""
        self.ssr_sample_path = ssr_sample_path
        self.stay_x_years = stay_x_years
        self.requirement2_window = requirement2_window
        self.stay_col = f"stay_{stay_x_years}_years"

    def build(self, input_paths: list[tuple[str, Path]]) -> pd.DataFrame:
        """Return one combined interlock candidate table for all provided interlock inputs."""
        # SSR universe comes from the SSR price sample, not boardex_pharma.
        ssr = pd.read_csv(self.ssr_sample_path, usecols=["BoardName"])
        ssr_boards = set(ssr["BoardName"].dropna().astype(str).unique())
        all_interlock_parts: list[pd.DataFrame] = []

        for interlock_type, input_path in input_paths:
            pairs = pd.read_stata(input_path, columns=["BoardName1", "BoardName2", "year"])
            pairs = pairs.dropna(subset=["BoardName1", "BoardName2", "year"]).copy()
            pairs["BoardName"] = pairs["BoardName1"].astype(str)
            pairs["BoardNamePair"] = pairs["BoardName2"].astype(str)
            # Keep only pair-years where both firms are in the SSR price-sample universe.
            pairs = pairs.loc[
                pairs["BoardName"].isin(ssr_boards) & pairs["BoardNamePair"].isin(ssr_boards)
            ].copy()
            pairs["event_year"] = pairs["year"].astype(int)
            candidates = pairs[["event_year", "BoardName", "BoardNamePair"]].drop_duplicates().reset_index(drop=True)

            columns = [
                "event_type",
                "event_year",
                "BoardName",
                "BoardNamePair",
                self.stay_col,
                "requirement1",
                "requirement2",
                "pair_interlock_t-1",
                "pair_interlock_t",
            ]
            if candidates.empty:
                all_interlock_parts.append(pd.DataFrame(columns=columns))
                continue

            # The raw interlock files are already directed, so no pair_min/pair_max normalization is used.
            pair_rows = list(candidates[["BoardName", "BoardNamePair", "event_year"]].itertuples(index=False, name=None))
            pair_year_set = set(pair_rows)
            interlock_lookup = build_counterpart_lookup(candidates, "BoardName", "event_year", "BoardNamePair")

            candidates["event_type"] = f"{interlock_type}_interlock"
            candidates["pair_interlock_t-1"] = [
                int((board_name, board_name_pair, int(event_year) - 1) in pair_year_set)
                for board_name, board_name_pair, event_year in pair_rows
            ]
            candidates["pair_interlock_t"] = [
                int((board_name, board_name_pair, int(event_year)) in pair_year_set)
                for board_name, board_name_pair, event_year in pair_rows
            ]
            candidates[self.stay_col] = [
                int(
                    all(
                        (board_name, board_name_pair, int(event_year) + offset) in pair_year_set
                        for offset in range(self.stay_x_years)
                    )
                )
                for board_name, board_name_pair, event_year in pair_rows
            ]
            candidates["requirement1"] = (
                candidates["pair_interlock_t-1"].eq(0) & candidates["pair_interlock_t"].eq(1)
            ).astype("int8")

            requirement2_values: list[int] = []
            for row in candidates.itertuples(index=False):
                # Direct/indirect interlock requirement2 is weak expansion of a firm's
                # directed counterpart set over the configured event window.
                history = [
                    interlock_lookup.get((str(row.BoardName), year), set())
                    for year in range(
                        int(row.event_year) + self.requirement2_window[0],
                        int(row.event_year) + self.requirement2_window[1] + 1,
                    )
                ]
                requirement2_values.append(
                    int(all(current.issuperset(previous) for previous, current in zip(history, history[1:])))
                )
            candidates["requirement2"] = requirement2_values

            int_columns = [
                self.stay_col,
                "requirement1",
                "requirement2",
                "pair_interlock_t-1",
                "pair_interlock_t",
            ]
            candidates[int_columns] = candidates[int_columns].astype("int8")
            all_interlock_parts.append(candidates[columns])

        interlock_candidates = pd.concat(all_interlock_parts, ignore_index=True)
        # One output file keeps direct and indirect rows together; event_type identifies the source event.
        return interlock_candidates.sort_values(
            ["event_type", "event_year", "BoardName", "BoardNamePair"]
        ).reset_index(drop=True)


def main() -> None:
    """Run the raw event-table pipeline and write CSV outputs."""
    stay_x_years = int(RUN_CONFIG["stay_x_years"])
    requirement2_window = tuple(RUN_CONFIG["requirement2_window"])
    if stay_x_years < 1:
        raise ValueError("stay_x_years must be >= 1")
    if len(requirement2_window) != 2 or requirement2_window[0] > requirement2_window[1]:
        raise ValueError("requirement2_window must be (start_offset, end_offset) with start <= end")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Build and save movement-side raw tables first because they use boardex_pharma memberships.
    movement_candidates, firm_interlock_edges = MovementEventBuilder(
        input_path=PHARMA_PATH,
        stay_x_years=stay_x_years,
        requirement2_window=requirement2_window,
    ).build()
    firm_interlock_edges.to_csv(FIRM_INTERLOCK_EDGES_PATH, index=False)
    movement_candidates.to_csv(MOVEMENT_CANDIDATES_PATH, index=False)

    # Build and save one combined direct/indirect interlock raw table.
    interlock_candidates = InterlockEventBuilder(
        ssr_sample_path=SSR_SAMPLE_PATH,
        stay_x_years=stay_x_years,
        requirement2_window=requirement2_window,
    ).build(
        input_paths=[
            ("indirect", INDIRECT_INPUT_PATH),
            ("direct", DIRECT_INPUT_PATH),
        ]
    )
    interlock_candidates.to_csv(INTERLOCK_CANDIDATES_PATH, index=False)

    print(f"Saved {len(firm_interlock_edges):,} rows to {FIRM_INTERLOCK_EDGES_PATH}")
    print(f"Saved {len(movement_candidates):,} rows to {MOVEMENT_CANDIDATES_PATH}")
    print(f"Saved {len(interlock_candidates):,} rows to {INTERLOCK_CANDIDATES_PATH}")


if __name__ == "__main__":
    main()
