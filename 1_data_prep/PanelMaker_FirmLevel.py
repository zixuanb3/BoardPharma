r"""
Purpose:
Build firm-level event-study panels for SSR pharma firms under multiple treatment concepts.
The script supports both year and quarter panels, and balanced-sample tagging via stay_x_years and a configurable balance window requirement.

Process:
- Build SSR base panel at the selected panel_level (year or quarter).
- Construct treatment events by event_type:
    direct/indirect interlock use symmetric firm-pair links;
    to B still in A / to B not in A use directional director-move events, where
    treatment_group chooses whether firm A or B is treated.
- Apply stay_x_years so treatment requires forward persistence.
- In quarter mode, event and stay flags are placed in Q1 of the event year.
- Mark first-event and event-year indicators, then compute balance tags using
    balance_window = (start_offset, end_offset), i.e., t+start through t+end.
- Export panel files to data/{panel_level}-level_{A|B}_{with|without}_{B|A}.

Input:
- InterimData/boardex_ssr_price_sample.csv
- InterimData/boardex_interlock_direct_firmpair.dta
- InterimData/boardex_interlock_indirect_firmpair.dta
- InterimData/boardex_pharma.dta

Provenance of the `revenue` variable in boardex_ssr_price_sample.csv:
1. /Dropbox/SSR/Stata/codes/1_clean/2_clean_ssr.do:88-91 — picks `avgnet` from
   raw revenue_ssr.csv, renames it to `revenue`, and saves
   data1e_ssr_sample_brand_firm_quarter.dta.
2. /Dropbox/BoardPharma/codes/2_merge/Task4.1.py:28 — reads
   data1e_ssr_sample_brand_firm_quarter.dta, uses `revenue` only to compute
   market-share weights (lines 76-77), then writes boardex_ssr_price_sample.csv
   (line 298).
3. This script (PanelMaker_FirmLevel.py) then consumes
   InterimData/boardex_ssr_price_sample.csv — i.e., the file produced in step 2.
So the `revenue` carried through every downstream panel here is SSR `avgnet`.

Output:
- data/year-level_{A|B}_{with|without}_{B|A}/ssr_firm_panel_*.csv
- data/quarter-level_{A|B}_{with|without}_{B|A}/ssr_firm_panel_*.csv
- data/movement_list/*.csv
"""

import pathlib
import warnings
from itertools import product
import numpy as np
import pandas as pd
from functools import lru_cache

# Suppress future warnings for cleaner output
warnings.filterwarnings("ignore", category=FutureWarning)

# Configure project directory paths
CURRENT_PATH = pathlib.Path(__file__).parent.resolve()
PROJECT_ROOT = CURRENT_PATH.parent.parent
INTERIM_DATA_PATH = PROJECT_ROOT / "InterimData"
OUTPUT_BASE_PATH = PROJECT_ROOT / "data"
OUTPUT_BASE_PATH.mkdir(parents=True, exist_ok=True)


# ========================== USER CONFIG ==========================
# event_types:
# - "direct interlock": symmetric firm-pair interlock treatment
# - "indirect interlock": symmetric firm-pair indirect interlock treatment
# - "to B still in A": directional director-move treatment (destination B) while still on A
# - "to B not in A": directional director-move treatment (destination B) after leaving A
#
# panel_levels:
# - "year": yearly panel
# - "quarter": quarterly panel (event/stay indicator set in Q1 only)
#
# stay_x_years:
# - persistence filter for treatment validity
#
# balance_window:
# - balanced-window rule as (start_offset, end_offset)
# - e.g. (-4, 3) means require periods from t-4 to t+3
#
# treatment_groups:
# - "B": destination firm as treated group (legacy behavior)
# - "A": origin firm as treated group
#
# include_eventpair:
# - 1: keep counterpart-firm observations in panel
# - 0: drop counterpart-firm observations from panel
RUN_CONFIG = {
    "event_types": [
        "direct interlock",
        "indirect interlock",
        "to B not in A",
        "to B still in A",
    ],
    "panel_levels": ["quarter"],
    "stay_x_years": 3,
    "balance_window": (-1, 1),
    "treatment_groups": ["B","A"],
}
# ===============================================================


@lru_cache(maxsize=1)
def load_ssr_yearly() -> pd.DataFrame:
    """
    Load and aggregate SSR data to firm-product-year.
    """
    ssr = pd.read_csv(INTERIM_DATA_PATH / "boardex_ssr_price_sample.csv")
    ssr = ssr[["BoardName", "year", "product", "atc3", "revenue", "quantity", "price0"]]
    yearly = (
        ssr.groupby(["BoardName", "year", "product", "atc3"], as_index=False)
        .agg(revenue=("revenue", "sum"), quantity=("quantity", "sum"), price0=("price0", "mean"))
        .sort_values(["BoardName", "product", "year"])
    )
    yearly["price"] = yearly["revenue"] * 1_000_000 / yearly["quantity"]
    return yearly


@lru_cache(maxsize=1)
def load_ssr_quarterly() -> pd.DataFrame:
    """
    Load and aggregate SSR data to firm-product-year-quarter.
    """
    ssr = pd.read_csv(INTERIM_DATA_PATH / "boardex_ssr_price_sample.csv")
    ssr = ssr[["BoardName", "year", "quarter", "product", "atc3", "revenue", "quantity", "price0"]]
    quarterly = (
        ssr.groupby(["BoardName", "year", "quarter", "product", "atc3"], as_index=False)
        .agg(revenue=("revenue", "sum"), quantity=("quantity", "sum"), price0=("price0", "mean"))
        .sort_values(["BoardName", "product", "year", "quarter"])
    )
    quarterly["price"] = quarterly["revenue"] * 1_000_000 / quarterly["quantity"]
    quarterly["quarter"] = quarterly["quarter"].astype(np.int8)
    return quarterly

class EventStudyPanelSSR:
    def __init__(
        self,
        event_type: str,
        panel_level: str = "year",
        stay_x_years: int = 3,
        balance_window: tuple[int, int] = (-4, 3),
        treatment_group: str = "B",
    ):
        self.event_type = event_type
        self.panel_level = panel_level.lower()
        self.stay_x_years = stay_x_years
        self.stay_col = f"stay_{stay_x_years}_years"
        self.balance_window = balance_window
        self.treatment_group = treatment_group.upper()

        if self.stay_x_years < 1:
            raise ValueError("stay_x_years must be >= 1")
        if self.panel_level not in {"year", "quarter"}:
            raise ValueError("panel_level must be either 'year' or 'quarter'")
        if len(self.balance_window) != 2 or self.balance_window[0] > self.balance_window[1]:
            raise ValueError("balance_window must be a tuple(start_offset, end_offset) with start <= end")
        if self.treatment_group not in {"A", "B"}:
            raise ValueError("treatment_group must be either 'A' or 'B'")
            
        self.ssr_yearly = load_ssr_yearly().copy()
        self.ssr_base = self._build_ssr_base()

    def _build_ssr_base(self) -> pd.DataFrame:
        # Year mode keeps firm-product-year observations as-is.
        if self.panel_level == "year":
            return self.ssr_yearly.copy()

        # Quarter mode rebuilds the base at firm-product-year-quarter granularity.
        return load_ssr_quarterly().copy()

    def _required_periods(self, event_year: int) -> set:
        # Balanced-window requirement: include all periods from t+start to t+end.
        start_offset, end_offset = self.balance_window
        if self.panel_level == "quarter":
            return {
                (y, q)
                for y in range(event_year + start_offset, event_year + end_offset + 1)
                for q in (1, 2, 3, 4)
            }
        return set(range(event_year + start_offset, event_year + end_offset + 1))

    @staticmethod
    def _event_mask(df: pd.DataFrame, year: int, boards: set, panel_level: str) -> pd.Series:
        # Quarterly panels register event-year flags in Q1 only.
        mask = (df["year"] == year) & df["BoardName"].isin(boards)
        if panel_level == "quarter" and "quarter" in df.columns:
            mask = mask & (df["quarter"] == 1)
        return mask

    def load_event_data(self) -> pd.DataFrame:
        # event_type controls both data source and treatment meaning.
        event_files = {
            "direct interlock": "boardex_interlock_direct_firmpair.dta",
            "indirect interlock": "boardex_interlock_indirect_firmpair.dta",
            "to B not in A": "boardex_pharma.dta",
            "to B still in A": "boardex_pharma.dta",
        }
        if self.event_type not in event_files:
            if self.event_type == "no event":
                return pd.DataFrame()
            raise ValueError("Unsupported event type")
        return pd.read_stata(INTERIM_DATA_PATH / event_files[self.event_type])

    def _mark_balance_panel(self, group: pd.DataFrame, event_col: str) -> pd.DataFrame:
        # One balance tag per event definition (first event or event_year).
        balance_col = "balance_panel_first" if event_col == "first_event" else f"balance_panel_{event_col.split('_')[-1]}"
        group = group.copy()
        group[balance_col] = 0

        treated = group[group[event_col] == 1]
        if len(treated) > 1:
            raise ValueError(f"More than one event row found for BoardName={group['BoardName'].iloc[0]}")
            
        if treated.empty or treated.iloc[0][self.stay_col] != 1:
            return group

        # Balanced flag requires full coverage of the configured balance window.
        event_year = int(treated.iloc[0]["year"])
        if self.panel_level == "quarter" and "quarter" in group.columns:
            observed_periods = set(zip(group["year"].astype(int), group["quarter"].astype(int)))
        else:
            observed_periods = set(group["year"].astype(int))

        if self._required_periods(event_year).issubset(observed_periods):
            group[balance_col] = 1
        return group

    @staticmethod
    def _add_event_year_columns(df: pd.DataFrame, event_df: pd.DataFrame, panel_level: str) -> pd.DataFrame:
        # event_YYYY columns store whether a board is treated in that calendar year.
        event_years = sorted(event_df["year"].unique())
        for y in event_years:
            df[f"event_{int(y)}"] = 0

        years_by_board = event_df.groupby("BoardName")["year"].agg(lambda x: set(x.tolist())).to_dict()

        def assign(group: pd.DataFrame) -> pd.DataFrame:
            years = years_by_board.get(group["BoardName"].iloc[0], set())
            for y in years:
                group[f"event_{int(y)}"] = 1
            return group

        return df.groupby("BoardName", group_keys=False).apply(assign)

    def _build_board_transitions(self, pharma: pd.DataFrame) -> pd.DataFrame:
        # Keep inSSR mapping for origin/destination validation.
        board_inssr = pharma.dropna(subset=["BoardName"]).set_index("BoardName")["inSSR"].to_dict()

        # Keep full board history first; filtering too early can hide true year-to-year moves.
        pharma_clean = pharma.dropna(subset=["DirectorID", "year", "BoardName"])
        grouped = (
            pharma_clean.groupby(["DirectorID", "year"], as_index=False)
            .agg(BoardName=("BoardName", lambda x: sorted(set(x.tolist()))))
            .sort_values(["DirectorID", "year"])
            .reset_index(drop=True)
        )

        # Fast lookup for persistence checks used by stay_x_years.
        board_lookup = {
            (did, int(y)): set(boards)
            for did, y, boards in grouped[["DirectorID", "year", "BoardName"]].itertuples(index=False)
        }

        # Build directional transitions from prior-year boards (A) to new boards (B).
        rows = []
        for did, year, current in grouped[["DirectorID", "year", "BoardName"]].itertuples(index=False):
            year = int(year)
            
            # Require consecutive years so event timing is not backfilled across gaps.
            previous = board_lookup.get((did, year - 1))
            if not previous:
                continue
                
            new_boards = [b for b in current if b not in previous]
            if not new_boards:
                continue
            
            # Evaluate all A -> B combinations for this director-year.
            for b_last, b_new in product(previous, new_boards):
                # Keep pharma-to-pharma transitions only.
                if board_inssr.get(b_last, 0) != 1 or board_inssr.get(b_new, 0) != 1:
                    continue
                    
                current_set = set(current)
                to_still = int(b_last in current_set and b_new in current_set)
                to_not = int(b_last not in current_set and b_new in current_set)

                # stay_x_years: destination board B must persist in future years.
                stay_met = True
                if self.stay_x_years > 1:
                    for offset in range(1, self.stay_x_years):
                        future_boards = board_lookup.get((did, year + offset), set())
                        if b_new not in future_boards:
                            stay_met = False
                            break
                            
                stay_val = int(stay_met)
                
                rows.append({
                    "DirectorID": did,
                    "year": year,
                    "A": b_last,
                    "B": b_new,
                    "to_B_still_in_A": to_still,
                    "to_B_not_in_A": to_not,
                    f"{self.stay_col}_still": int(to_still == 1 and stay_val == 1),
                    f"{self.stay_col}_not": int(to_not == 1 and stay_val == 1),
                })

        return pd.DataFrame(rows)

    def _build_to_b_panel(self, mode: str) -> tuple[pd.DataFrame, pd.DataFrame]:
        pharma = self.load_event_data()
        transitions = self._build_board_transitions(pharma)
        
        if transitions.empty:
            print(f"Warning: No transitions generated for mode '{mode}'")
            return pd.DataFrame(), pd.DataFrame()

        stay_still_col = f"{self.stay_col}_still"
        stay_not_col = f"{self.stay_col}_not"

        agg_args = {
            "to_B_still_in_A": ("to_B_still_in_A", "max"),
            "to_B_not_in_A": ("to_B_not_in_A", "max"),
            stay_still_col: (stay_still_col, "max"),
            stay_not_col: (stay_not_col, "max"),
        }

        treatment_firm_col = self.treatment_group
        counterpart_col = "A" if treatment_firm_col == "B" else "B"

        collapsed = (
            transitions.groupby(["year", treatment_firm_col], as_index=False)
            .agg(**agg_args)
            .rename(columns={treatment_firm_col: "BoardName"})
        )

        if mode == "still":
            event_col, stay_col_src = "to_B_still_in_A", stay_still_col
            mode_value = "still"
        else:
            event_col, stay_col_src = "to_B_not_in_A", stay_not_col
            mode_value = "not"

        movement_list = (
            transitions.loc[transitions[event_col].eq(1), ["DirectorID", "year", "A", "B"]]
            .rename(columns={"year": "movement_year", "A": "FirmA", "B": "FirmB"})
            .copy()
        )
        movement_list["event_type"] = mode_value
        movement_list = movement_list[["DirectorID", "movement_year", "FirmA", "FirmB", "event_type"]]
        movement_list = movement_list.drop_duplicates().reset_index(drop=True)

        events = (
            collapsed.loc[collapsed[event_col].eq(1), ["year", "BoardName", event_col, stay_col_src]]
            .rename(columns={stay_col_src: self.stay_col})
            .copy()
        )

        if self.panel_level == "quarter":
            # Quarterly output expands each treated board-year into Q1-Q4; event is anchored at Q1.
            events = events.loc[events.index.repeat(4)].reset_index(drop=True)
            events["quarter"] = events.groupby(["BoardName", "year"]).cumcount() + 1
            events["quarter"] = events["quarter"].astype(np.int8)
            events.loc[events["quarter"] != 1, [event_col, self.stay_col]] = 0

        merge_keys = ["BoardName", "year"] + (["quarter"] if self.panel_level == "quarter" else [])
        panel = self.ssr_base.merge(events, on=merge_keys, how="left")
        
        first_event = events.groupby("BoardName", as_index=False)["year"].min().rename(columns={"year": "first_event_year"})
        panel = panel.merge(first_event, on="BoardName", how="left")

        first_mask = (panel[event_col] == 1) & (panel["year"] == panel["first_event_year"])
        if self.panel_level == "quarter":
            first_mask = first_mask & panel["quarter"].eq(1)
        panel["first_event"] = first_mask.astype(int)
        
        # Missing event/stay entries are untreated observations.
        panel[[event_col, self.stay_col]] = panel[[event_col, self.stay_col]].fillna(0).astype(np.int8)

        event_years = sorted(events["year"].unique())
        for y in event_years:
            col = f"event_{int(y)}"
            boards = set(events.loc[events["year"] == y, "BoardName"])
            panel[col] = self._event_mask(panel, y, boards, self.panel_level).astype(np.int8)

        panel = panel.groupby(["BoardName", "product"], group_keys=False).apply(
            lambda g: self._mark_balance_panel(g, "first_event")
        )
        for col in [c for c in panel.columns if c.startswith("event_")]:
            panel = panel.groupby(["BoardName", "product"], group_keys=False).apply(
                lambda g, e=col: self._mark_balance_panel(g, e)
            )

        panel = panel.drop(columns=[c for c in panel.columns if c.startswith("event_")])
        panel = self._add_event_year_columns(panel, events, self.panel_level)

        # Keep a unified event column name for downstream scripts.
        panel = panel.rename(columns={event_col: "event"})
        ordered = panel.columns.tolist()
        ordered.insert(4, ordered.pop(ordered.index("event")))
        return panel[ordered], movement_list

    def _movement_output_group_label(self) -> str:
        return f"{self.treatment_group}"

    def _build_interlock_panel(self) -> pd.DataFrame:
        event_data = self.load_event_data()
        # Restrict interlock events to SSR boards so treatment is defined on panel universe.
        valid_boards = set(self.ssr_yearly["BoardName"].dropna().unique())
        event_data = event_data.loc[
            event_data["BoardName1"].isin(valid_boards) & event_data["BoardName2"].isin(valid_boards)
        ].copy()

        event_data["pair_min"] = np.where(
            event_data["BoardName1"] <= event_data["BoardName2"], event_data["BoardName1"], event_data["BoardName2"]
        )
        event_data["pair_max"] = np.where(
            event_data["BoardName1"] <= event_data["BoardName2"], event_data["BoardName2"], event_data["BoardName1"]
        )

        pair_year = event_data[["pair_min", "pair_max", "year"]].dropna().drop_duplicates().copy()
        pair_year["year"] = pair_year["year"].astype(int)
        pair_year_set = set(pair_year[["pair_min", "pair_max", "year"]].itertuples(index=False, name=None))

        def _check_stay(r):
            # stay_x_years for interlocks: the same pair must remain linked in future years.
            if self.stay_x_years <= 1:
                return 1
            for offset in range(1, self.stay_x_years):
                if (r["pair_min"], r["pair_max"], int(r["year"]) + offset) not in pair_year_set:
                    return 0
            return 1

        pair_year[self.stay_col] = pair_year.apply(_check_stay, axis=1).astype(np.int8)

        event_board_year = (
            pair_year
            .melt(id_vars=["year", self.stay_col], value_vars=["pair_min", "pair_max"], value_name="BoardName")
            [["BoardName", "year", self.stay_col]]
            .dropna()
            .drop_duplicates()
            .groupby(["BoardName", "year"], as_index=False)
            .agg(**{self.stay_col: (self.stay_col, "max")})
        )
        event_board_year["event"] = 1
        event_board_year[self.stay_col] = event_board_year[self.stay_col].astype(np.int8)

        if self.panel_level == "quarter":
            # Quarter mode places treatment and stay markers in Q1.
            event_board_year = event_board_year.loc[event_board_year.index.repeat(4)].reset_index(drop=True)
            event_board_year["quarter"] = event_board_year.groupby(["BoardName", "year"]).cumcount() + 1
            event_board_year["quarter"] = event_board_year["quarter"].astype(np.int8)
            event_board_year.loc[event_board_year["quarter"] != 1, ["event", self.stay_col]] = 0

        merge_keys = ["BoardName", "year"] + (["quarter"] if self.panel_level == "quarter" else [])
        merged = self.ssr_base.merge(event_board_year, on=merge_keys, how="left")
        merged["event"] = merged["event"].fillna(0).astype(np.int8)
        merged[self.stay_col] = merged[self.stay_col].fillna(0).astype(np.int8)

        first_event_map = merged.loc[merged["event"].eq(1)].groupby("BoardName")["year"].min()
        merged["first_event_year"] = merged["BoardName"].map(first_event_map)
        
        first_event_stay_map = (
            event_board_year.loc[event_board_year["event"].eq(1)]
            .sort_values("year")
            .drop_duplicates("BoardName")
            .set_index("BoardName")[self.stay_col]
        )
        merged["first_event_stay"] = merged["BoardName"].map(first_event_stay_map).fillna(0).astype(np.int8)

        event_years = sorted(merged.loc[merged["event"].eq(1), "year"].dropna().unique())
        for y in event_years:
            boards = set(merged.loc[(merged["year"] == y) & merged["event"].eq(1), "BoardName"])
            merged[f"event_{int(y)}"] = merged["BoardName"].isin(boards).astype(np.int8)

        if self.panel_level == "quarter":
            periods_lookup = (
                merged.groupby(["BoardName", "product"])
                .apply(lambda g: set(zip(g["year"].astype(int), g["quarter"].astype(int))), include_groups=False)
                .to_dict()
            )
        else:
            periods_lookup = (
                merged.groupby(["BoardName", "product"])["year"]
                .agg(lambda s: set(s.dropna().astype(int).tolist()))
                .to_dict()
            )

        merged["balance_panel_first"] = merged.apply(
            lambda r: int(
                pd.notna(r["first_event_year"])
                and r["first_event_stay"] == 1
                # first-event balance uses the same configurable balance_window rule.
                and self._required_periods(int(r["first_event_year"])).issubset(
                    periods_lookup.get((r["BoardName"], r["product"]), set())
                )
            ),
            axis=1,
        ).astype(np.int8)

        for y in event_years:
            need_periods = self._required_periods(int(y))
            event_col = f"event_{int(y)}"
            bal_col = f"balance_panel_{int(y)}"
            
            stay_firms = set(
                merged.loc[
                    (merged[event_col] == 1) & (merged[self.stay_col] == 1),
                    "BoardName",
                ].unique()
            )
            
            qualified = {
                (bn, prod)
                for (bn, prod), periods in periods_lookup.items()
                if bn in stay_firms and need_periods.issubset(periods)
            }
            
            # Board-product units qualify only if both stay and window-coverage conditions hold.
            merged[bal_col] = (
                pd.MultiIndex.from_frame(merged[["BoardName", "product"]]).isin(qualified).astype(np.int8)
            )

        return merged

    def merge_event_data(self) -> pd.DataFrame:
        output_group = self._movement_output_group_label()
        output_path = OUTPUT_BASE_PATH / f"{self.panel_level}-level_{output_group}"
        output_path.mkdir(parents=True, exist_ok=True)

        if self.event_type == "to B still in A":
            still_panel, movement_list = self._build_to_b_panel(mode="still")
            if not still_panel.empty:
                still_panel.to_csv(output_path / f"ssr_firm_panel_to_B_still_in_A.csv", index=False)
            return movement_list

        if self.event_type == "to B not in A":
            not_panel, movement_list = self._build_to_b_panel(mode="not")
            if not not_panel.empty:
                not_panel.to_csv(output_path / f"ssr_firm_panel_to_B_not_in_A.csv", index=False)
            return movement_list

        if self.event_type in ["direct interlock", "indirect interlock"]:
            panel = self._build_interlock_panel()
            panel.to_csv(output_path / f"ssr_firm_panel_{self.event_type.replace(' ', '_')}.csv", index=False)
            return pd.DataFrame()

        if self.event_type == "no event":
            return pd.DataFrame()

        raise ValueError("Unsupported event type")


def main() -> None:
    def ensure_list(v):
        # Allow both single-value and list-style config inputs.
        if isinstance(v, str):
            return [v]
        return list(v)

    panel_levels = ensure_list(RUN_CONFIG["panel_levels"])
    event_types = ensure_list(RUN_CONFIG["event_types"])
    stay_req = int(RUN_CONFIG["stay_x_years"])
    balance_window = tuple(RUN_CONFIG["balance_window"])
    treatment_groups = [str(x).upper() for x in ensure_list(RUN_CONFIG.get("treatment_groups", ["B"]))]
    movement_event_types = {"to B not in A", "to B still in A"}
    movement_output_path = OUTPUT_BASE_PATH / "movement_list"
    movement_output_path.mkdir(parents=True, exist_ok=True)
    
    for panel_level in panel_levels:
        for treatment_group in treatment_groups:
            movement_parts = []
            for event_type in event_types:
                print(
                    f"Generating panel: '{event_type}' | level={panel_level} | "
                    f"treatment_group={treatment_group} | "
                    f"stay_{stay_req}_years | balance_window=t{balance_window[0]:+d}..t{balance_window[1]:+d}"
                )
                movement_rows = EventStudyPanelSSR(
                    event_type,
                    panel_level=panel_level,
                    stay_x_years=stay_req,
                    balance_window=balance_window,
                    treatment_group=treatment_group,
                ).merge_event_data()

                if event_type in movement_event_types and not movement_rows.empty:
                    movement_parts.append(movement_rows)

            selected_movement_types = [et for et in event_types if et in movement_event_types]
            if selected_movement_types:
                if movement_parts:
                    movement_list = (
                        pd.concat(movement_parts, ignore_index=True)
                        .drop_duplicates()
                        .sort_values(["DirectorID", "movement_year", "FirmA", "FirmB", "event_type"])
                        .reset_index(drop=True)
                    )
                else:
                    movement_list = pd.DataFrame(
                        columns=["DirectorID", "movement_year", "FirmA", "FirmB", "event_type"]
                    )

                if len(set(selected_movement_types)) == 2:
                    movement_name = "to_B_movement"
                else:
                    movement_name = selected_movement_types[0].replace(" ", "_")

                movement_list.to_csv(
                    movement_output_path / f"{movement_name}.csv",
                    index=False,
                )
    
    print("All panels generated!")


if __name__ == "__main__":
    main()