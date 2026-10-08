"""
Purpose:
Build raw director-movement candidates and membership-derived firm-interlock
panels for the four standardized SSR roster variants.

Process:
1. Load in-SSR BoardEx director-board memberships from `boardex_pharma.dta`.
2. Build movement and firm-interlock histories at the selected year or quarter frequency.
3. Add stay and requirement1 eligibility flags.
4. Run the same logic separately for all four roster variants.

Quarterly `to_B_not_in_A` timing:
- The director leaves firm A at quarter T.
- The director may first join firm B in T, T+1, T+2, or T+3.
- The A-side event is dated at T and the B-side event is dated at the
  director's first qualifying B-entry quarter.

Input:
- InterimData/boardex_pharma.dta
- One of the four standardized roster CSV files under `D:/pharma`.

Output:
- `data/roster_variants/<variant>/leader_tier_<tier>/event_tables/firm_interlock_panel.csv`
- `data/roster_variants/<variant>/leader_tier_<tier>/event_tables/movement_event_candidates.csv`
- No external interlock-candidate table is produced in this version.
"""

from __future__ import annotations

from itertools import combinations
from pathlib import Path

import pandas as pd

from pipeline_variant_config import (
    INTERIM_DATA_PATH,
    PERSONNEL_DEFINITIONS,
    PERSONNEL_TIER_RULES,
    ROSTER_VARIANTS,
    configured_personnel_definitions,
    configured_variants,
    ensure_variant_columns,
    get_variant,
    personnel_output_dir,
)


PairYearSet = set[tuple[str, str, int]]
CounterpartLookup = dict[tuple[str, int], set[str]]


# ========================== USER CONFIG ==========================
RUN_CONFIG = {
    "stay_x_years": 2,
    "delayed_entry_quarters": 3,
    "roster_variants": configured_variants(),
    "personnel_definitions": configured_personnel_definitions(),
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

    def __init__(
        self,
        input_path: Path,
        stay_x_years: int,
        panel_level: str,
        personnel_definition: str,
        delayed_entry_quarters: int = 3,
    ) -> None:
        """Store movement-event configuration and derive period-window lengths."""
        if panel_level not in {"year", "quarter"}:
            raise ValueError("panel_level must be either 'year' or 'quarter'")
        self.input_path = input_path
        self.stay_x_years = stay_x_years
        self.panel_level = panel_level
        if personnel_definition not in PERSONNEL_TIER_RULES:
            allowed = ", ".join(PERSONNEL_DEFINITIONS)
            raise ValueError(
                f"Unknown personnel_definition={personnel_definition}; expected one of: {allowed}"
            )
        self.personnel_definition = personnel_definition
        self.allowed_leader_tiers = PERSONNEL_TIER_RULES[personnel_definition]
        self.periods_per_year = 1 if panel_level == "year" else 4
        self.stay_col = f"stay_{stay_x_years}_years"

        self.forward_stay_periods = self.stay_periods
        if delayed_entry_quarters < 0:
            raise ValueError("delayed_entry_quarters must be non-negative")
        self.delayed_entry_quarters = int(delayed_entry_quarters)

    @property
    def stay_periods(self) -> int:
        """Return the persistence horizon in panel periods."""
        return self.stay_x_years * self.periods_per_year

    def _add_time_id(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Add a sortable annual or quarterly time index."""
        frame = frame.copy()
        if self.panel_level == "year":
            frame["time_id"] = frame["year"].astype(int)
        else:
            frame["time_id"] = frame["year"].astype(int) * 4 + frame["quarter"].astype(int) - 1
        return frame

    def _time_columns(self) -> list[str]:
        """Return time columns that must be carried to event outputs."""
        return ["year"] if self.panel_level == "year" else ["year", "quarter"]

    def _time_fields(self, time_id: int) -> dict[str, int]:
        """Convert the internal time index back to calendar fields."""
        if self.panel_level == "year":
            return {"time_id": int(time_id), "year": int(time_id)}
        return {
            "time_id": int(time_id),
            "year": int(time_id) // 4,
            "quarter": int(time_id) % 4 + 1,
        }

    def build(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Return movement candidates and membership-derived firm-interlock edges."""
        # 1) Load one of the four standardized roster universes.
        usecols = [
            "DirectorID",
            "year",
            "BoardName",
            "inSSR",
            "leader_tier",
            "board_continuity",
        ]
        if self.panel_level == "quarter":
            usecols.extend(["quarter", "yearquarter"])
        available_columns = pd.read_csv(self.input_path, nrows=0).columns.tolist()
        selected_columns = [column for column in usecols if column in available_columns]
        memberships = ensure_variant_columns(
            pd.read_csv(self.input_path, usecols=selected_columns, low_memory=False),
            self.input_path.stem.replace("ssr_company_roster_", ""),
        )
        if "board_continuity" not in memberships.columns:
            memberships["board_continuity"] = 0
        memberships = memberships.dropna(subset=["DirectorID", "year", "BoardName"])
        # Movement events use tier-specific leadership memberships. In the
        # narrow tier, a continuous IE/OC spell that starts as a board role
        # remains active after a board-to-CEO title change, so the transition
        # cannot be misclassified as an exit.
        tier_mask = memberships["leader_tier"].isin(self.allowed_leader_tiers)
        if self.personnel_definition == "narrow":
            tier_mask = tier_mask | memberships["board_continuity"].eq(1)
        memberships = memberships.loc[
            memberships["inSSR"].eq(1)
            & tier_mask,
            ["DirectorID", "year", "BoardName"] + (["quarter"] if self.panel_level == "quarter" else []),
        ].copy()
        memberships["year"] = memberships["year"].astype(int)
        memberships["BoardName"] = memberships["BoardName"].astype(str)
        if self.panel_level == "quarter":
            memberships["quarter"] = memberships["quarter"].astype(int)
        memberships = self._add_time_id(memberships)
        memberships = memberships.drop_duplicates(subset=["DirectorID", "time_id", "BoardName"])

        # 2) Collapse each director-year to a sorted board list, then complete
        #    each director's timeline from min_year - 1 through max_year + 1.
        board_lists = (
            memberships.groupby(["DirectorID", "time_id"], as_index=False)
            .agg(board_list=("BoardName", lambda values: sorted(pd.unique(values.dropna()).tolist())))
            .sort_values(["DirectorID", "time_id"])
            .reset_index(drop=True)
        )
        if board_lists.empty:
            complete_history = pd.DataFrame(columns=["DirectorID", "time_id", "board_list"])
        else:
            time_bounds = board_lists.groupby("DirectorID", as_index=False)["time_id"].agg(
                min_time="min",
                max_time="max",
            )
            skeleton = pd.concat(
                [
                    pd.DataFrame(
                        {
                            "DirectorID": director_id,
                            "time_id": range(int(min_time) - 1, int(max_time) + 2),
                        }
                    )
                    for director_id, min_time, max_time in time_bounds.itertuples(index=False, name=None)
                ],
                ignore_index=True,
            )
            complete_history = skeleton.merge(board_lists, on=["DirectorID", "time_id"], how="left")
            complete_history = complete_history.sort_values(["DirectorID", "time_id"]).reset_index(drop=True)
            complete_history["board_list"] = complete_history["board_list"].apply(
                lambda value: value if isinstance(value, list) else []
            )

        # 3) Build membership-derived firm interlocks. The internal lookup is
        #    undirected; the output edge table is directed and firm-centered.
        pair_year_set: PairYearSet = set()
        for time_id, board_list in complete_history[["time_id", "board_list"]].itertuples(index=False, name=None):
            pair_year_set.update(
                (firm_a, firm_b, int(time_id))
                for firm_a, firm_b in combinations(sorted(board_list), 2)
            )

        edge_rows = [
            {"BoardName": firm_a, "time_id": time_id, "CounterpartBoard": firm_b}
            for firm_a, firm_b, time_id in sorted(pair_year_set)
        ] + [
            {"BoardName": firm_b, "time_id": time_id, "CounterpartBoard": firm_a}
            for firm_a, firm_b, time_id in sorted(pair_year_set)
        ]
        firm_interlock_edges = (
            pd.DataFrame(edge_rows).sort_values(["BoardName", "time_id", "CounterpartBoard"]).reset_index(drop=True)
            if edge_rows
            else pd.DataFrame(columns=["BoardName", "time_id", "CounterpartBoard"])
        )
        if self.panel_level == "year":
            firm_interlock_edges["year"] = firm_interlock_edges["time_id"]
        else:
            firm_interlock_edges["year"] = firm_interlock_edges["time_id"] // 4
            firm_interlock_edges["quarter"] = firm_interlock_edges["time_id"] % 4 + 1

        def pair_flags(firm_a: str, firm_b: str, event_time: int) -> dict[str, int]:
            """Return the interlock flags around one event time."""
            firm_low, firm_high = sorted((firm_a, firm_b))
            return {
                "pair_interlock_t-1": int((firm_low, firm_high, event_time - 1) in pair_year_set),
                "pair_interlock_t": int((firm_low, firm_high, event_time) in pair_year_set),
                "pair_interlock_t+1": int((firm_low, firm_high, event_time + 1) in pair_year_set),
            }

        def side_fields(
            firm_a_time: int,
            firm_b_time: int,
            requirement1_a: int,
            requirement1_b: int,
            flags_a: dict[str, int],
            flags_b: dict[str, int],
        ) -> dict[str, int]:
            """Return side-specific event times and requirement diagnostics."""
            return {
                "FirmA_event_time_id": int(firm_a_time),
                "FirmB_event_time_id": int(firm_b_time),
                "requirement1_A": int(requirement1_a),
                "requirement1_B": int(requirement1_b),
                "pair_B_interlock_t-1": int(flags_b["pair_interlock_t-1"]),
                "pair_B_interlock_t": int(flags_b["pair_interlock_t"]),
                "pair_B_interlock_t+1": int(flags_b["pair_interlock_t+1"]),
            }

        # 4) Compare adjacent director periods and write movement candidates.
        movement_rows: list[dict[str, object]] = []
        for director_id, director_panel in complete_history.groupby("DirectorID", sort=False):
            year_board_pairs = list(
                director_panel.sort_values("time_id")[["time_id", "board_list"]].itertuples(
                    index=False,
                    name=None,
                )
            )
            board_history = {int(time_id): set(board_list) for time_id, board_list in year_board_pairs}

            for (prev_time, prev_list), (event_time, current_list) in zip(
                year_board_pairs,
                year_board_pairs[1:],
            ):
                prev_time = int(prev_time)
                event_time = int(event_time)
                if event_time != prev_time + 1:
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
                        pair_tm1 = int((firm_low, firm_high, event_time - 1) in pair_year_set)
                        pair_t = int((firm_low, firm_high, event_time) in pair_year_set)
                        pair_tp1 = int((firm_low, firm_high, event_time + 1) in pair_year_set)
                        stay = int(
                            all(
                                firm_b in board_history.get(year, set())
                                for year in range(event_time, event_time + self.forward_stay_periods)
                            )
                        )
                        movement_row = {
                                "event_type": "to_B_still_in_A",
                                "DirectorID": director_id,
                                "FirmA": firm_a,
                                "FirmB": firm_b,
                                self.stay_col: stay,
                                "requirement1": int(pair_tm1 == 0),
                                "pair_interlock_t-1": pair_tm1,
                                "pair_interlock_t": pair_t,
                                "pair_interlock_t+1": pair_tp1,
                            }
                        movement_row.update(self._time_fields(event_time))
                        movement_rows.append(movement_row)

                # to_B_not_in_A: the director leaves A at event_time and
                # joins B either immediately or within the next three quarters.
                # Annual variants retain the original same-period definition.
                if self.panel_level == "quarter":
                    delayed_b_entries: dict[str, int] = {
                        firm_b: event_time
                        for firm_b in sorted(new_boards)
                    }
                    for lag in range(1, self.delayed_entry_quarters + 1):
                        future_time = event_time + lag
                        future_boards = board_history.get(future_time, set())
                        prior_future_boards = set().union(
                            *[
                                board_history.get(event_time + prior_lag, set())
                                for prior_lag in range(0, lag)
                            ]
                        )
                        for firm_b in sorted(future_boards):
                            if firm_b in previous_boards or firm_b in prior_future_boards:
                                continue
                            delayed_b_entries.setdefault(firm_b, future_time)

                    not_in_a_pairs = [
                        (firm_a, firm_b, b_time)
                        for firm_a in sorted(left_boards)
                        for firm_b, b_time in sorted(delayed_b_entries.items())
                    ]
                else:
                    not_in_a_pairs = [
                        (firm_a, firm_b, event_time)
                        for firm_a in sorted(left_boards)
                        for firm_b in sorted(new_boards)
                    ]

                for firm_a, firm_b, firm_b_time in not_in_a_pairs:
                    flags_a = pair_flags(firm_a, firm_b, event_time)
                    flags_b = pair_flags(firm_a, firm_b, firm_b_time)
                    requirement1_a = int(all(value == 0 for value in flags_a.values()))
                    requirement1_b = int(all(value == 0 for value in flags_b.values()))
                    stay = int(
                        all(
                            firm_b in board_history.get(
                                firm_b_time + offset,
                                set(),
                            )
                            for offset in range(self.forward_stay_periods)
                        )
                    )
                    movement_row = {
                        "event_type": "to_B_not_in_A",
                        "DirectorID": director_id,
                        "FirmA": firm_a,
                        "FirmB": firm_b,
                        self.stay_col: stay,
                        # Keep the legacy requirement1 column as the joint
                        # diagnostic; EventTableMaker uses side-specific flags.
                        "requirement1": int(requirement1_a and requirement1_b),
                        **flags_a,
                    }
                    movement_row.update(self._time_fields(event_time))
                    movement_row.update(
                        side_fields(
                            firm_a_time=event_time,
                            firm_b_time=firm_b_time,
                            requirement1_a=requirement1_a,
                            requirement1_b=requirement1_b,
                            flags_a=flags_a,
                            flags_b=flags_b,
                        )
                    )
                    movement_rows.append(movement_row)

                # interlock_dissolution: the director jointly serves on A and B
                # in t-1 and leaves at least one of the two firms at t. The
                # base event does not require the pair interlock to disappear
                # immediately; that restriction belongs to requirement1.
                for firm_b in sorted(left_boards):
                    for firm_a in sorted(stayed_boards | (left_boards - {firm_b})):
                        firm_low, firm_high = sorted((firm_a, firm_b))
                        pair_tm1 = int((firm_low, firm_high, event_time - 1) in pair_year_set)
                        pair_t = int((firm_low, firm_high, event_time) in pair_year_set)
                        pair_tp1 = int((firm_low, firm_high, event_time + 1) in pair_year_set)
                        # Base event: the pair was interlocked in t-1 and the
                        # director leaves at least one of A/B at t. This permits
                        # leaving A, leaving B, or leaving both A and B.
                        if pair_tm1 != 1:
                            continue
                        movement_row = {
                                "event_type": "interlock_dissolution",
                                "DirectorID": director_id,
                                "FirmA": firm_a,
                                "FirmB": firm_b,
                                # Dissolution is not required to satisfy stay_2_years.
                                self.stay_col: 1,
                                # REQ1: interlock in t-1, no interlock in t,
                                # and no interlock in t+1.
                                "requirement1": int(
                                    pair_tm1 == 1 and pair_t == 0 and pair_tp1 == 0
                                ),
                                "pair_interlock_t-1": pair_tm1,
                                "pair_interlock_t": pair_t,
                                "pair_interlock_t+1": pair_tp1,
                            }
                        movement_row.update(self._time_fields(event_time))
                        movement_rows.append(movement_row)

        movement_columns = [
            "event_type",
            "DirectorID",
            "time_id",
            *self._time_columns(),
            "FirmA",
            "FirmB",
            self.stay_col,
            "requirement1",
            "pair_interlock_t-1",
            "pair_interlock_t",
            "pair_interlock_t+1",
            "FirmA_event_time_id",
            "FirmB_event_time_id",
            "requirement1_A",
            "requirement1_B",
            "pair_B_interlock_t-1",
            "pair_B_interlock_t",
            "pair_B_interlock_t+1",
        ]
        # Deduplicate exact director-firm-pair candidates after all director-year transitions are scanned.
        movement_candidates = (
            pd.DataFrame(movement_rows, columns=movement_columns)
            if movement_rows
            else pd.DataFrame(columns=movement_columns)
        )
        # Older event types use one common event time. Populate the new
        # side-specific fields so downstream code can use one stable schema.
        movement_candidates["FirmA_event_time_id"] = movement_candidates[
            "FirmA_event_time_id"
        ].fillna(movement_candidates["time_id"])
        movement_candidates["FirmB_event_time_id"] = movement_candidates[
            "FirmB_event_time_id"
        ].fillna(movement_candidates["time_id"])
        movement_candidates["requirement1_A"] = movement_candidates[
            "requirement1_A"
        ].fillna(movement_candidates["requirement1"])
        movement_candidates["requirement1_B"] = movement_candidates[
            "requirement1_B"
        ].fillna(movement_candidates["requirement1"])
        for column in (
            "pair_B_interlock_t-1",
            "pair_B_interlock_t",
            "pair_B_interlock_t+1",
        ):
            source_column = column.replace("pair_B_", "pair_")
            movement_candidates[column] = movement_candidates[column].fillna(
                movement_candidates[source_column]
            )
        movement_candidates[
            [
                "FirmA_event_time_id",
                "FirmB_event_time_id",
                "requirement1_A",
                "requirement1_B",
                "pair_B_interlock_t-1",
                "pair_B_interlock_t",
                "pair_B_interlock_t+1",
            ]
        ] = movement_candidates[
            [
                "FirmA_event_time_id",
                "FirmB_event_time_id",
                "requirement1_A",
                "requirement1_B",
                "pair_B_interlock_t-1",
                "pair_B_interlock_t",
                "pair_B_interlock_t+1",
            ]
        ].astype("int64")
        movement_candidates = (
            movement_candidates.drop_duplicates(
                subset=["event_type", "DirectorID", "time_id", "FirmA", "FirmB"]
            )
            .sort_values(["event_type", "DirectorID", "time_id", "FirmA", "FirmB"])
            .reset_index(drop=True)
        )

        return movement_candidates, firm_interlock_edges


def main() -> None:
    """Run movement and membership-interlock raw tables for each roster variant."""
    stay_x_years = int(RUN_CONFIG["stay_x_years"])
    if stay_x_years < 1:
        raise ValueError("stay_x_years must be >= 1")

    for variant in RUN_CONFIG["roster_variants"]:
        metadata = get_variant(str(variant))
        panel_level = str(metadata["panel_level"])
        for personnel_definition in RUN_CONFIG["personnel_definitions"]:
            output_dir = personnel_output_dir(
                str(variant), str(personnel_definition)
            ) / "event_tables"
            output_dir.mkdir(parents=True, exist_ok=True)

            movement_candidates, firm_interlock_edges = MovementEventBuilder(
                input_path=Path(metadata["path"]),
                stay_x_years=stay_x_years,
                panel_level=panel_level,
                personnel_definition=str(personnel_definition),
                delayed_entry_quarters=int(RUN_CONFIG["delayed_entry_quarters"]),
            ).build()
            firm_interlock_edges.to_csv(output_dir / "firm_interlock_panel.csv", index=False)
            movement_candidates.to_csv(output_dir / "movement_event_candidates.csv", index=False)
            print(
                f"[{variant} | {personnel_definition}] "
                f"Saved {len(firm_interlock_edges):,} firm-interlock rows"
            )
            print(
                f"[{variant} | {personnel_definition}] "
                f"Saved {len(movement_candidates):,} movement rows"
            )

if __name__ == "__main__":
    main()
