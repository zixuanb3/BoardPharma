"""
Purpose:
Build annual or quarterly director-movement candidates and membership-derived
firm-interlock panels. Quarterly mode supports the formulary narrow roster;
annual mode retains the existing sample and personnel definitions.

Process:
1. Select the membership input. When quarter=0, use the existing annual sample
   settings. When quarter=1, require formulary=1, ignore large_sample, and load
   the audited quarterly roster with personnel_definition="narrow".
2. Deduplicate memberships, complete director-period histories, and compare
   adjacent years or quarters to identify the three movement event types.
   Quarterly mode identifies firms by mapping id and outputs idA/idB; annual
   mode continues to identify firms by BoardName and outputs FirmA/FirmB.
3. Derive firm-interlock edges from all selected directors' memberships and
   use the in-memory lookup to calculate each candidate's requirement1.
4. In annual mode, calculate stay and requirement2 with the existing rules.
   In quarterly mode, convert stay_x_years to four times as many consecutive
   quarters: two years means eight quarters, forward for joins and backward
   for dissolution. Skip requirement2 and its window truncation.
5. Write the interlock panel and movement candidates to data/event_tables.
   Quarterly panels add quarter; candidates add event_quarter and use
   stay_8_quarters at the default two-year setting.
6. The separate direct/indirect interlock candidate job remains disabled.

Input:
- InterimData/boardex_pharma.dta when quarter=0 and large_sample=0
- InterimData/ssr_company_roster.csv when quarter=0, large_sample=1, formulary=0
- InterimData/formulary_company_roster.csv when quarter=0 and formulary=1
- data/formulary_roster/formulary_roster_2019_2025.csv when quarter=1 and formulary=1
- InterimData/boardex_ssr_price_sample.csv (disabled independent interlock job)
- InterimData/boardex_interlock_indirect_firmpair.dta (disabled independent interlock job)
- InterimData/boardex_interlock_direct_firmpair.dta (disabled independent interlock job)

Output:
- data/event_tables/firm_interlock_panel.csv when quarter=0 and large_sample=0
- data/event_tables/movement_event_candidates.csv when quarter=0 and large_sample=0
- data/event_tables/firm_interlock_panel_large_sample_{definition}.csv when quarter=0, large_sample=1, formulary=0
- data/event_tables/movement_event_candidates_large_sample_{definition}.csv when quarter=0, large_sample=1, formulary=0
- data/event_tables/firm_interlock_panel_formulary_large_sample_{definition}.csv when quarter=0 and formulary=1
- data/event_tables/movement_event_candidates_formulary_large_sample_{definition}.csv when quarter=0 and formulary=1
- data/event_tables/firm_interlock_panel_formulary_quarter_narrow.csv when quarter=1 and formulary=1
- data/event_tables/movement_event_candidates_formulary_quarter_narrow.csv when quarter=1 and formulary=1
- data/event_tables/interlock_event_candidates.csv (disabled independent interlock job)
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
LARGE_SAMPLE_ROSTER_PATH = INTERIM_DATA_PATH / "ssr_company_roster.csv"
FORMULARY_ROSTER_PATH = INTERIM_DATA_PATH / "formulary_company_roster.csv"
FORMULARY_QUARTER_ROSTER_PATH = (
    PROJECT_ROOT / "data" / "formulary_roster" / "formulary_roster_2018_2026.csv"
)
SSR_SAMPLE_PATH = INTERIM_DATA_PATH / "boardex_ssr_price_sample.csv"
INDIRECT_INPUT_PATH = INTERIM_DATA_PATH / "boardex_interlock_indirect_firmpair.dta"
DIRECT_INPUT_PATH = INTERIM_DATA_PATH / "boardex_interlock_direct_firmpair.dta"

INTERLOCK_CANDIDATES_PATH = OUTPUT_DIR / "interlock_event_candidates.csv"

PairPeriodSet = set[tuple[str, str, int]]
CounterpartLookup = dict[tuple[str, int], set[str]]


# ========================== USER CONFIG ==========================
RUN_CONFIG = {
    "quarter": 1,  # 0: existing annual workflow; 1: formulary quarters only.
    "stay_x_years": 2,
    "requirement2_window": (-1, 1),  # Annual mode only.
    "large_sample": 1,  # Ignored when quarter=1.
    "formulary": 1,
    "personnel_definition": "narrow",
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
    """Build annual or quarterly movement events from board membership histories."""

    PERSONNEL_TIERS = {
        "narrow": {"board"},
        "medium": {"board", "csuite"},
        "broad": {"board", "csuite", "vp_tech_hr"},
    }

    def __init__(
        self,
        input_path: Path,
        stay_x_years: int,
        requirement2_window: tuple[int, int] | None,
        large_sample: int = 0,
        personnel_definition: str = "narrow",
        quarter: int = 0,
    ) -> None:
        """Store movement-event configuration and derive stay-window lengths."""
        if quarter not in {0, 1}:
            raise ValueError("quarter must be 0 or 1")
        if stay_x_years < 1:
            raise ValueError("stay_x_years must be >= 1")
        if quarter and personnel_definition != "narrow":
            raise ValueError("Quarterly formulary roster supports narrow personnel only")
        if not quarter and large_sample not in {0, 1}:
            raise ValueError("large_sample must be 0 or 1")
        if large_sample == 1 and personnel_definition not in self.PERSONNEL_TIERS:
            raise ValueError("personnel_definition must be one of: narrow, medium, broad")

        self.input_path = input_path
        self.stay_x_years = stay_x_years
        self.requirement2_window = requirement2_window
        self.large_sample = large_sample
        self.personnel_definition = personnel_definition
        self.quarter = quarter
        if quarter:
            self.stay_col = f"stay_{stay_x_years * 4}_quarters"
            self.forward_stay_periods = stay_x_years * 4
            self.backward_stay_periods = (stay_x_years - 1) * 4
        else:
            self.stay_col = f"stay_{stay_x_years}_years"
            if requirement2_window is None:
                raise ValueError("Annual mode requires requirement2_window")
            start_offset, end_offset = requirement2_window
            self.forward_stay_periods = min(stay_x_years, max(0, end_offset) + 1)
            self.backward_stay_periods = min(stay_x_years, max(0, -start_offset))

    def load_quarter_memberships(self) -> pd.DataFrame:
        """Deduplicate roster seats and encode quarters as consecutive integers."""
        memberships = pd.read_csv(
            self.input_path,
            usecols=["DirectorID", "Year", "Quarter", "id"],
            dtype={
                "DirectorID": "Int64",
                "Year": "Int64",
                "Quarter": "Int64",
                "id": "Int64",
            },
        )
        if memberships.isna().any().any():
            raise ValueError("Quarterly roster has missing identity or time fields")
        if not memberships["Quarter"].isin([1, 2, 3, 4]).all():
            raise ValueError("Quarter must be 1, 2, 3, or 4")
        # The shared transition engine operates on consecutive integer periods.
        memberships["period"] = (
            (memberships["Year"] - 1960) * 4 + memberships["Quarter"] - 1
        ).astype("int64")
        memberships["DirectorID"] = memberships["DirectorID"].astype("int64")
        memberships["id"] = memberships["id"].astype("int64")
        return memberships[["DirectorID", "period", "id"]].drop_duplicates()

    def build(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Return movement candidates and membership-derived firm-interlock edges."""
        if self.quarter:
            memberships = self.load_quarter_memberships()
        else:
            # 1) Load movement source universe.
            if self.large_sample == 1:
                memberships = pd.read_csv(
                    self.input_path,
                    usecols=["DirectorID", "year", "BoardName", "inSSR", "leader_tier"],
                )
                memberships = memberships.loc[
                    memberships["leader_tier"].isin(self.PERSONNEL_TIERS[self.personnel_definition])
                ].copy()
            else:
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
            memberships["BoardName"] = memberships["BoardName"].astype(str).str.upper()
            memberships = memberships.drop_duplicates(subset=["DirectorID", "year", "BoardName"])
            memberships = memberships.rename(columns={"year": "period"})

        # 2) Collapse each director-period to a sorted board list, then complete
        #    each timeline from min_period - 1 through one period after its
        #    max_period, capped at the sample's final observed period.
        firm_col = "id" if self.quarter else "BoardName"
        board_lists = (
            memberships.groupby(["DirectorID", "period"], as_index=False)
            .agg(
                board_list=(
                    firm_col,
                    lambda values: sorted(pd.unique(values.dropna()).tolist()),
                )
            )
            .sort_values(["DirectorID", "period"])
            .reset_index(drop=True)
        )
        if board_lists.empty:
            complete_history = pd.DataFrame(columns=["DirectorID", "period", "board_list"])
        else:
            sample_max_period = int(board_lists["period"].max())
            period_bounds = board_lists.groupby("DirectorID", as_index=False)["period"].agg(
                min_period="min",
                max_period="max",
            )
            skeleton = pd.concat(
                [
                    pd.DataFrame(
                        {
                            "DirectorID": director_id,
                            "period": range(
                                int(min_period) - 1,
                                min(int(max_period) + 1, sample_max_period) + 1,
                            ),
                        }
                    )
                    for director_id, min_period, max_period in period_bounds.itertuples(index=False, name=None)
                ],
                ignore_index=True,
            )
            complete_history = skeleton.merge(board_lists, on=["DirectorID", "period"], how="left")
            complete_history = complete_history.sort_values(["DirectorID", "period"]).reset_index(drop=True)
            complete_history["board_list"] = complete_history["board_list"].apply(
                lambda value: value if isinstance(value, list) else []
            )

        # 3) Build membership-derived firm interlocks. The internal lookup is
        #    undirected; the output edge table is directed and firm-centered.
        pair_period_set: PairPeriodSet = set()
        for period, board_list in complete_history[["period", "board_list"]].itertuples(index=False, name=None):
            pair_period_set.update(
                (firm_a, firm_b, int(period))
                for firm_a, firm_b in combinations(sorted(board_list), 2)
            )

        edge_a_col, edge_b_col = (
            ("idA", "idB") if self.quarter else ("BoardName", "CounterpartBoard")
        )
        edge_rows = [
            {edge_a_col: firm_a, "period": period, edge_b_col: firm_b}
            for firm_a, firm_b, period in sorted(pair_period_set)
        ] + [
            {edge_a_col: firm_b, "period": period, edge_b_col: firm_a}
            for firm_a, firm_b, period in sorted(pair_period_set)
        ]
        firm_interlock_edges = (
            pd.DataFrame(edge_rows).sort_values([edge_a_col, "period", edge_b_col]).reset_index(drop=True)
            if edge_rows
            else pd.DataFrame(columns=[edge_a_col, "period", edge_b_col])
        )

        # 4) Compare adjacent director-periods and write movement candidates.
        candidate_a_col, candidate_b_col = (
            ("idA", "idB") if self.quarter else ("FirmA", "FirmB")
        )
        movement_rows: list[dict[str, object]] = []
        for director_id, director_panel in complete_history.groupby("DirectorID", sort=False):
            period_board_pairs = list(
                director_panel.sort_values("period")[["period", "board_list"]].itertuples(
                    index=False,
                    name=None,
                )
            )
            board_history = {int(period): set(board_list) for period, board_list in period_board_pairs}

            for (prev_period, prev_list), (event_period, current_list) in zip(
                period_board_pairs,
                period_board_pairs[1:],
            ):
                prev_period = int(prev_period)
                event_period = int(event_period)
                if event_period != prev_period + 1:
                    raise ValueError(
                        f"DirectorID={director_id} has a non-consecutive period gap after skeleton expansion."
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
                        pair_tm1 = int((firm_low, firm_high, event_period - 1) in pair_period_set)
                        pair_t = int((firm_low, firm_high, event_period) in pair_period_set)
                        stay = int(
                            all(
                                firm_b in board_history.get(period, set())
                                for period in range(event_period, event_period + self.forward_stay_periods)
                            )
                        )
                        movement_rows.append(
                            {
                                "event_type": "to_B_still_in_A",
                                "DirectorID": director_id,
                                "event_period": event_period,
                                candidate_a_col: firm_a,
                                candidate_b_col: firm_b,
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
                        pair_tm1 = int((firm_low, firm_high, event_period - 1) in pair_period_set)
                        pair_t = int((firm_low, firm_high, event_period) in pair_period_set)
                        stay = int(
                            all(
                                firm_b in board_history.get(period, set())
                                for period in range(event_period, event_period + self.forward_stay_periods)
                            )
                        )
                        movement_rows.append(
                            {
                                "event_type": "to_B_not_in_A",
                                "DirectorID": director_id,
                                "event_period": event_period,
                                candidate_a_col: firm_a,
                                candidate_b_col: firm_b,
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
                        pair_tm1 = int((firm_low, firm_high, event_period - 1) in pair_period_set)
                        pair_t = int((firm_low, firm_high, event_period) in pair_period_set)
                        stay = int(
                            all(
                                {firm_a, firm_b}.issubset(board_history.get(period, set()))
                                for period in range(event_period - self.backward_stay_periods, event_period)
                            )
                        )
                        movement_rows.append(
                            {
                                "event_type": "interlock_dissolution",
                                "DirectorID": director_id,
                                "event_period": event_period,
                                candidate_a_col: firm_a,
                                candidate_b_col: firm_b,
                                self.stay_col: stay,
                                "requirement1": int(pair_t == 0),
                                "pair_interlock_t-1": pair_tm1,
                                "pair_interlock_t": pair_t,
                            }
            )

        movement_columns = [
            "event_type",
            "DirectorID",
            "event_period",
            candidate_a_col,
            candidate_b_col,
            self.stay_col,
            "requirement1",
            "pair_interlock_t-1",
            "pair_interlock_t",
        ]
        # Deduplicate exact director-firm-pair candidates after all director-period transitions are scanned.
        movement_candidates = (
            pd.DataFrame(movement_rows, columns=movement_columns)
            if movement_rows
            else pd.DataFrame(columns=movement_columns)
        )
        movement_candidates = (
            movement_candidates.drop_duplicates(
                subset=[
                    "event_type",
                    "DirectorID",
                    "event_period",
                    candidate_a_col,
                    candidate_b_col,
                ]
            )
            .sort_values(
                ["event_type", "DirectorID", "event_period", candidate_a_col, candidate_b_col]
            )
            .reset_index(drop=True)
        )

        if self.quarter:
            return self.export_periods(movement_candidates, firm_interlock_edges)

        # 5) Add independent movement requirement2 for A and B sides.
        interlock_lookup = build_counterpart_lookup(
            firm_interlock_edges,
            "BoardName",
            "period",
            "CounterpartBoard",
        )
        for side, firm_col in {"A": "FirmA", "B": "FirmB"}.items():
            requirement_col = f"requirement2_{side}"
            board_periods = (
                movement_candidates[["event_type", "event_period", firm_col]]
                .rename(columns={firm_col: "BoardName"})
                .dropna(subset=["BoardName", "event_period"])
                .drop_duplicates()
                .sort_values(["event_type", "BoardName", "event_period"])
                .reset_index(drop=True)
            )

            requirement_values: list[int] = []
            for row in board_periods.itertuples(index=False):
                # Requirement2 is evaluated over the configured event-period window and is not
                # conditioned on stay or requirement1.
                history = [
                    interlock_lookup.get((str(row.BoardName), period), set())
                    for period in range(
                        int(row.event_period) + self.requirement2_window[0],
                        int(row.event_period) + self.requirement2_window[1] + 1,
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

            board_periods[requirement_col] = requirement_values
            movement_candidates = movement_candidates.merge(
                board_periods.rename(columns={"BoardName": firm_col}),
                on=["event_type", "event_period", firm_col],
                how="left",
            )
            movement_candidates[requirement_col] = movement_candidates[requirement_col].fillna(0).astype("int8")

        movement_candidates = movement_candidates[
            [
                "event_type",
                "DirectorID",
                "event_period",
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
        return self.export_periods(movement_candidates, firm_interlock_edges)


    def export_periods(
        self, candidates: pd.DataFrame, edges: pd.DataFrame,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Restore calendar fields after the shared integer-period calculations."""
        candidates = candidates.rename(columns={"event_period": "event_year"})
        edges = edges.rename(columns={"period": "year"})
        if self.quarter:
            for frame, year_col, quarter_col in (
                (candidates, "event_year", "event_quarter"),
                (edges, "year", "quarter"),
            ):
                periods = pd.to_numeric(frame[year_col], errors="raise").astype("int64")
                frame.insert(frame.columns.get_loc(year_col) + 1, quarter_col, periods % 4 + 1)
                frame[year_col] = periods // 4 + 1960
        return candidates, edges


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
        ssr["BoardName"] = ssr["BoardName"].astype(str).str.upper()
        ssr_boards = set(ssr["BoardName"].dropna().astype(str).unique())
        all_interlock_parts: list[pd.DataFrame] = []

        for interlock_type, input_path in input_paths:
            pairs = pd.read_stata(input_path, columns=["BoardName1", "BoardName2", "year"])
            pairs = pairs.dropna(subset=["BoardName1", "BoardName2", "year"]).copy()
            pairs["BoardName1"] = pairs["BoardName1"].astype(str).str.upper()
            pairs["BoardName2"] = pairs["BoardName2"].astype(str).str.upper()
            pairs["BoardName"] = pairs["BoardName1"]
            pairs["BoardNamePair"] = pairs["BoardName2"]
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
    quarter = int(RUN_CONFIG["quarter"])
    if quarter not in {0, 1}:
        raise ValueError("quarter must be 0 or 1")
    requirement2_window = None if quarter else tuple(RUN_CONFIG["requirement2_window"])
    if stay_x_years < 1:
        raise ValueError("stay_x_years must be >= 1")
    if not quarter and (len(requirement2_window) != 2 or requirement2_window[0] > requirement2_window[1]):
        raise ValueError("requirement2_window must be (start_offset, end_offset) with start <= end")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Build and save movement-side raw tables first.
    large_sample = 0 if quarter else int(RUN_CONFIG["large_sample"])
    formulary = int(RUN_CONFIG["formulary"])
    personnel_definition = str(RUN_CONFIG["personnel_definition"])
    if formulary not in {0, 1}:
        raise ValueError("formulary must be 0 or 1")
    if quarter and formulary != 1:
        raise ValueError("quarter=1 requires formulary=1")
    if not quarter and formulary == 1 and large_sample != 1:
        raise ValueError("formulary requires large_sample == 1")

    if quarter:
        movement_input_path = FORMULARY_QUARTER_ROSTER_PATH
        movement_output_suffix = "_formulary_quarter_narrow"
    elif formulary == 1:
        movement_input_path = FORMULARY_ROSTER_PATH
        movement_output_suffix = f"_formulary_large_sample_{personnel_definition}"
    elif large_sample == 1:
        movement_input_path = LARGE_SAMPLE_ROSTER_PATH
        movement_output_suffix = f"_large_sample_{personnel_definition}"
    else:
        movement_input_path = PHARMA_PATH
        movement_output_suffix = ""
    firm_interlock_edges_path = OUTPUT_DIR / f"firm_interlock_panel{movement_output_suffix}.csv"
    movement_candidates_path = OUTPUT_DIR / f"movement_event_candidates{movement_output_suffix}.csv"
    movement_candidates, firm_interlock_edges = MovementEventBuilder(
        input_path=movement_input_path,
        stay_x_years=stay_x_years,
        requirement2_window=requirement2_window,
        large_sample=large_sample,
        personnel_definition=personnel_definition,
        quarter=quarter,
    ).build()
    firm_interlock_edges.to_csv(firm_interlock_edges_path, index=False)
    movement_candidates.to_csv(movement_candidates_path, index=False)
    print(f"Saved {len(firm_interlock_edges):,} rows to {firm_interlock_edges_path}")
    print(f"Saved {len(movement_candidates):,} rows to {movement_candidates_path}")

"""
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

    print(f"Saved {len(interlock_candidates):,} rows to {INTERLOCK_CANDIDATES_PATH}")
"""

if __name__ == "__main__":
    main()
