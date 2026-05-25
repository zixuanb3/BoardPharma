r"""
Purpose:
Build firm-level event-study panels for SSR pharma firms under multiple treatment concepts.
The script supports both year and quarter panels, and balanced-sample tagging via stay_x_years and a configurable balance window requirement.

Process:
- Build SSR base panel at the selected panel_level (year or quarter).
- Construct treatment events by event_type:
    direct/indirect interlock use symmetric firm-pair links;
    movement events read prebuilt director-level candidates from movement tables,
    then select valid treated firm-years under req0 / req1 / req2.
- Apply stay_x_years so movement panel inputs must match the configured stay column.
- In quarter mode, event and stay flags are placed in Q1 of the event year.
- Mark first-event and event-year indicators, then compute balance tags using
    balance_window = (start_offset, end_offset), i.e., t+start through t+end.
- Export panel files to data/{panel_level}-level_{A|B}_{with|without}_{B|A}.

Input:
- InterimData/boardex_ssr_price_sample.csv
- InterimData/boardex_interlock_direct_firmpair.dta
- InterimData/boardex_interlock_indirect_firmpair.dta
- data/movement_tables/movement_event_candidates.csv

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
"""

import pathlib
import warnings
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
MOVEMENT_TABLES_PATH = OUTPUT_BASE_PATH / "movement_tables"
MOVEMENT_CANDIDATES_PATH = MOVEMENT_TABLES_PATH / "movement_event_candidates.csv"

MOVEMENT_EVENT_SPECS = {
    "to B still in A": {
        "candidate_event_type": "to_B_still_in_A",
        "output_stem": "to_B_still_in_A",
    },
    "to B not in A": {
        "candidate_event_type": "to_B_not_in_A",
        "output_stem": "to_B_not_in_A",
    },
    "interlock_dissolution": {
        "candidate_event_type": "interlock_dissolution",
        "output_stem": "interlock_dissolution_leave_B",
    },
}

MOVEMENT_REQUIREMENTS = ("req0", "req1", "req2")


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
RUN_CONFIG = {
    "event_types": [
        "to B not in A",
        "to B still in A",
        "interlock_dissolution",
    ],
    "panel_levels": ["quarter"],
    "stay_x_years": 2,
    "balance_window": (-2, 1),
    "treatment_groups": ["B","A"],
}
#        "direct interlock",
#        "indirect interlock",
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


@lru_cache(maxsize=1)
def load_movement_candidates_table() -> pd.DataFrame:
    """
    Load the prebuilt director-level movement candidate table.
    """
    if not MOVEMENT_CANDIDATES_PATH.exists():
        raise FileNotFoundError(
            "Movement candidate table not found at "
            f"{MOVEMENT_CANDIDATES_PATH}. Run MovementTableMaker.py first."
        )
    return pd.read_csv(MOVEMENT_CANDIDATES_PATH)

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

    def _cohort_event_years(self) -> list[int]:
        """
        Return the full cohort-year range that must exist in event_YYYY and
        balance_panel_YYYY columns, even when a given year has no valid events.
        """
        start_offset, end_offset = self.balance_window
        pre_length = max(0, -int(start_offset))
        post_length = max(0, int(end_offset))
        start_year = 2007 + pre_length
        end_year = 2019 - post_length
        if start_year > end_year:
            return []
        return list(range(start_year, end_year + 1))

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
        }
        if self.event_type not in event_files:
            if self.event_type == "no event":
                return pd.DataFrame()
            if self.event_type in MOVEMENT_EVENT_SPECS:
                raise ValueError(
                    f"Movement event '{self.event_type}' should load from "
                    f"{MOVEMENT_CANDIDATES_PATH.name}, not directly from boardex_pharma.dta."
                )
            raise ValueError("Unsupported event type")
        return pd.read_stata(INTERIM_DATA_PATH / event_files[self.event_type])

    def _load_movement_candidates(self) -> pd.DataFrame:
        """
        Load and validate the prebuilt director-level movement candidate table.
        """
        # Movement panels must read the prebuilt director-level candidate table.
        movement = load_movement_candidates_table().copy()
        requirement2_col = f"requirement2_{self.treatment_group}"
        required_cols = {
            "event_type",
            "DirectorID",
            "event_year",
            "FirmA",
            "FirmB",
            "requirement1",
            self.stay_col,
            requirement2_col,
        }
        missing_cols = sorted(required_cols - set(movement.columns))
        if missing_cols:
            raise ValueError(
                "Movement candidate table is missing required columns: "
                f"{missing_cols}. Expected stay column '{self.stay_col}'. "
                "Regenerate movement_event_candidates.csv with the same stay_x_years setting."
            )

        movement["event_year"] = pd.to_numeric(movement["event_year"], errors="raise").astype(int)
        movement["requirement1"] = pd.to_numeric(movement["requirement1"], errors="raise").astype(np.int8)
        movement[self.stay_col] = pd.to_numeric(movement[self.stay_col], errors="raise").astype(np.int8)
        movement[requirement2_col] = pd.to_numeric(
            movement[requirement2_col],
            errors="raise",
        ).astype(np.int8)
        return movement

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
    def _add_event_year_columns(
        df: pd.DataFrame,
        event_df: pd.DataFrame,
        event_years: list[int],
    ) -> pd.DataFrame:
        # event_YYYY columns store whether a board is treated in that calendar year.
        allowed_years = {int(y) for y in event_years}
        for y in event_years:
            df[f"event_{int(y)}"] = 0

        years_by_board = event_df.groupby("BoardName")["year"].agg(lambda x: set(x.tolist())).to_dict()

        def assign(group: pd.DataFrame) -> pd.DataFrame:
            # Restrict event_YYYY columns to the fixed cohort-year range only.
            years = {
                int(y)
                for y in years_by_board.get(group["BoardName"].iloc[0], set())
                if int(y) in allowed_years
            }
            for y in years:
                group[f"event_{int(y)}"] = 1
            return group

        return df.groupby("BoardName", group_keys=False).apply(assign)

    def _build_panel_from_board_year_events(self, event_board_year: pd.DataFrame) -> pd.DataFrame:
        """
        Convert a unique BoardName-year event table into a firm-level SSR panel.
        """
        # Movement panels start from unique treated BoardName-year rows.
        events = event_board_year.copy()
        events["year"] = pd.to_numeric(events["year"], errors="raise").astype(int)
        events["event"] = pd.to_numeric(events["event"], errors="raise").astype(np.int8)
        events[self.stay_col] = pd.to_numeric(events[self.stay_col], errors="raise").astype(np.int8)

        # Quarter mode expands each treated board-year into Q1-Q4, with the event anchored at Q1.
        if self.panel_level == "quarter":
            events = events.loc[events.index.repeat(4)].reset_index(drop=True)
            events["quarter"] = events.groupby(["BoardName", "year"]).cumcount() + 1
            events["quarter"] = events["quarter"].astype(np.int8)
            events.loc[events["quarter"] != 1, ["event", self.stay_col]] = 0

        merge_keys = ["BoardName", "year"] + (["quarter"] if self.panel_level == "quarter" else [])
        panel = self.ssr_base.merge(events, on=merge_keys, how="left")
        panel["event"] = panel["event"].fillna(0).astype(np.int8)
        panel[self.stay_col] = panel[self.stay_col].fillna(0).astype(np.int8)

        # first_event_year is the earliest valid treated year under the current requirement.
        first_event = (
            event_board_year.groupby("BoardName", as_index=False)["year"]
            .min()
            .rename(columns={"year": "first_event_year"})
        )
        panel = panel.merge(first_event, on="BoardName", how="left")

        first_mask = (panel["event"] == 1) & (panel["year"] == panel["first_event_year"])
        if self.panel_level == "quarter":
            first_mask = first_mask & panel["quarter"].eq(1)
        panel["first_event"] = first_mask.astype(np.int8)

        # Use the full cohort-year grid so downstream cohort files exist even
        # when a particular requirement has no treated events in some years.
        event_years = self._cohort_event_years()
        event_row_cols = []
        for y in event_years:
            event_row_col = f"event_row_{int(y)}"
            boards = set(event_board_year.loc[event_board_year["year"] == y, "BoardName"])
            panel[event_row_col] = self._event_mask(panel, y, boards, self.panel_level).astype(np.int8)
            event_row_cols.append(event_row_col)

        panel = panel.groupby(["BoardName", "product"], group_keys=False).apply(
            lambda g: self._mark_balance_panel(g, "first_event")
        )
        for event_row_col in event_row_cols:
            panel = panel.groupby(["BoardName", "product"], group_keys=False).apply(
                lambda g, e=event_row_col: self._mark_balance_panel(g, e)
            )

        # Final event_YYYY columns are board-level constants, so drop temporary row-level event columns.
        panel = panel.drop(columns=event_row_cols)
        panel = self._add_event_year_columns(panel, event_board_year, event_years)

        ordered = panel.columns.tolist()
        ordered.insert(4, ordered.pop(ordered.index("event")))
        return panel[ordered]

    def _build_movement_board_year_events(self, requirement_level: str) -> pd.DataFrame:
        """
        Collapse director-level movement candidates to unique treated BoardName-year rows.
        """
        # Filter director-level movement candidates to the current event type and requirement.
        if requirement_level not in MOVEMENT_REQUIREMENTS:
            raise ValueError(f"Unsupported movement requirement level: {requirement_level}")

        spec = MOVEMENT_EVENT_SPECS[self.event_type]
        movement = self._load_movement_candidates()
        requirement2_col = f"requirement2_{self.treatment_group}"
        movement = movement.loc[
            movement["event_type"].eq(spec["candidate_event_type"])
        ].copy()

        movement = movement.loc[movement[self.stay_col].eq(1)].copy()
        if requirement_level in {"req1", "req2"}:
            movement = movement.loc[movement["requirement1"].eq(1)].copy()
        if requirement_level == "req2":
            movement = movement.loc[movement[requirement2_col].eq(1)].copy()

        # treatment_group picks which side of the movement becomes the treated firm.
        treated_firm_col = "FirmB" if self.treatment_group == "B" else "FirmA"
        movement = (
            movement.rename(columns={treated_firm_col: "BoardName", "event_year": "year"})
            [["BoardName", "year"]]
            .dropna(subset=["BoardName", "year"])
            .drop_duplicates()
            .sort_values(["BoardName", "year"])
            .reset_index(drop=True)
        )
        movement["event"] = 1
        movement[self.stay_col] = 1
        movement["event"] = movement["event"].astype(np.int8)
        movement[self.stay_col] = movement[self.stay_col].astype(np.int8)
        return movement

    def _build_movement_panel(self, requirement_level: str) -> pd.DataFrame:
        """
        Build one movement panel under the requested requirement level.
        """
        event_board_year = self._build_movement_board_year_events(requirement_level)
        return self._build_panel_from_board_year_events(event_board_year)

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

        # Build the full cohort-year grid so every downstream event cohort file
        # has a matching event_YYYY and balance_panel_YYYY column.
        event_years = self._cohort_event_years()
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

        if self.event_type in MOVEMENT_EVENT_SPECS:
            output_stem = MOVEMENT_EVENT_SPECS[self.event_type]["output_stem"]
            for requirement_level in MOVEMENT_REQUIREMENTS:
                movement_panel = self._build_movement_panel(requirement_level)
                movement_panel.to_csv(
                    output_path / f"ssr_firm_panel_{output_stem}_{requirement_level}.csv",
                    index=False,
                )
            return pd.DataFrame()

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
    
    for panel_level in panel_levels:
        for treatment_group in treatment_groups:
            for event_type in event_types:
                print(
                    f"Generating panel: '{event_type}' | level={panel_level} | "
                    f"treatment_group={treatment_group} | "
                    f"stay_{stay_req}_years | balance_window=t{balance_window[0]:+d}..t{balance_window[1]:+d}"
                )
                EventStudyPanelSSR(
                    event_type,
                    panel_level=panel_level,
                    stay_x_years=stay_req,
                    balance_window=balance_window,
                    treatment_group=treatment_group,
                ).merge_event_data()
    
    print("All panels generated!")


if __name__ == "__main__":
    main()
