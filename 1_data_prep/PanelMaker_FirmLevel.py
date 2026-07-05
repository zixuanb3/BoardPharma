r"""
Purpose:
Build firm-level event-study panels for SSR pharma firms from standardized movement
and interlock event tables.

Process:
- Build SSR base panel at the selected panel_level (year or quarter).
- Read precomputed firm-side event eligibility from movement_table.csv and
  interlock_table.csv.
- Keep pure_event from the unfiltered event table so first_event is not changed
  by req0/req1/req2 filters.
- Build event and event_YYYY from the selected requirement level, with req0
  exposed as stay_x_years in the output panel.
- In quarter mode, event, pure_event, and stay flags are placed in Q1 of the event year.
- Mark first-event and event-year indicators, then compute balance tags using
    balance_window = (start_offset, end_offset), i.e., t+start through t+end.
- Export movement files to data/{panel_level}-level_{A|B}/ and interlock files
  to data/{panel_level}-level/.

Input:
- InterimData/boardex_ssr_price_sample.csv
- data/event_tables/movement_table.csv
- data/event_tables/movement_table_large_sample_{definition}.csv when RUN_CONFIG["large_sample"] == 1
- data/event_tables/interlock_table.csv

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
- data/year-level_{A|B}/ssr_firm_panel_*.csv
- data/quarter-level_{A|B}/ssr_firm_panel_*.csv
- movement output files add _large_sample_{definition} before .csv when RUN_CONFIG["large_sample"] == 1
- data/year-level/ssr_firm_panel_*.csv
- data/quarter-level/ssr_firm_panel_*.csv
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
EVENT_TABLE_DIR = OUTPUT_BASE_PATH / "event_tables"
INTERLOCK_EVENT_TABLE_PATH = EVENT_TABLE_DIR / "interlock_table.csv"

PERSONNEL_DEFINITIONS = {"narrow", "medium", "broad"}
MOVEMENT_EVENTS = {"to_B_still_in_A", "to_B_not_in_A", "interlock_dissolution"}
INTERLOCK_EVENTS = {"direct_interlock", "indirect_interlock"}
OUTPUT_STEM_OVERRIDES = {"interlock_dissolution": "interlock_dissolution_leave_B"}
EVENT_REQUIREMENTS = ("req0", "req1", "req2")


# ========================== USER CONFIG ==========================
# event_types:
# - "direct_interlock": direct firm interlock treatment
# - "indirect_interlock": indirect firm interlock treatment
# - "to_B_still_in_A": destination firm treatment while the director remains on A
# - "to_B_not_in_A": destination firm treatment after the director leaves A
# - "interlock_dissolution": directional dissolution treatment; output keeps leave_B naming
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
# large_sample/personnel_definition:
# - affect movement event input and movement panel output filenames only
RUN_CONFIG = {
    "event_types": [
        "to_B_not_in_A",
        "to_B_still_in_A",
        "interlock_dissolution",
        #"direct_interlock",
        #"indirect_interlock",
    ],
    "panel_levels": ["quarter"],
    "stay_x_years": 2,
    "balance_window": (-1, 1),
    "treatment_groups": ["B","A"],
    "large_sample": 1,
    "personnel_definition": "narrow",
}
# ===============================================================


# ========================== DATA LOADERS ==========================


def build_large_sample_suffix(large_sample: int, personnel_definition: str) -> str:
    """Return movement file suffix for the configured sample definition."""
    if large_sample not in {0, 1}:
        raise ValueError("large_sample must be 0 or 1")
    if large_sample == 0:
        return ""
    if personnel_definition not in PERSONNEL_DEFINITIONS:
        raise ValueError("personnel_definition must be one of: narrow, medium, broad")
    return f"_large_sample_{personnel_definition}"


@lru_cache(maxsize=None)
def load_ssr_panel(panel_level: str) -> pd.DataFrame:
    """Load and aggregate SSR data to the requested panel level."""
    ssr = pd.read_csv(INTERIM_DATA_PATH / "boardex_ssr_price_sample.csv")
    ssr["BoardName"] = ssr["BoardName"].astype(str).str.upper()
    group_cols = ["BoardName", "year", "product", "atc3"]
    sort_cols = ["BoardName", "product", "year"]
    if panel_level == "quarter":
        group_cols = ["BoardName", "year", "quarter", "product", "atc3"]
        sort_cols = ["BoardName", "product", "year", "quarter"]

    panel = (
        ssr[group_cols + ["revenue", "quantity", "price0"]]
        .groupby(group_cols, as_index=False)
        .agg(revenue=("revenue", "sum"), quantity=("quantity", "sum"), price0=("price0", "mean"))
        .sort_values(sort_cols)
    )
    panel["price"] = panel["revenue"] * 1_000_000 / panel["quantity"]
    if panel_level == "quarter":
        panel["quarter"] = panel["quarter"].astype(np.int8)
    return add_market_hhi(panel, panel_level)


def add_market_hhi(panel: pd.DataFrame, panel_level: str) -> pd.DataFrame:
    """Attach ATC2/ATC3 quantity and revenue HHI columns to the panel."""
    time_cols = ["year"] + (["quarter"] if panel_level == "quarter" else [])
    result = panel.copy()
    result["_atc2_for_hhi"] = result["atc3"].astype(str).str[:-1]

    for market_col, suffix in (("atc3", "atc3"), ("_atc2_for_hhi", "atc2")):
        market_keys = time_cols + [market_col]
        firm_sales = (
            result[market_keys + ["BoardName", "revenue", "quantity"]]
            .groupby(market_keys + ["BoardName"], as_index=False, dropna=False)
            .agg(
                firm_revenue=("revenue", "sum"),
                firm_quantity=("quantity", "sum"),
            )
        )
        market_totals = (
            firm_sales.groupby(market_keys, as_index=False, dropna=False)
            .agg(
                total_revenue=("firm_revenue", "sum"),
                total_quantity=("firm_quantity", "sum"),
            )
        )
        firm_sales = firm_sales.merge(
            market_totals,
            on=market_keys,
            how="left",
            validate="many_to_one",
        )
        firm_sales["revenue_share_sq"] = np.where(
            firm_sales["total_revenue"].gt(0),
            (firm_sales["firm_revenue"] / firm_sales["total_revenue"]) ** 2,
            np.nan,
        )
        firm_sales["quantity_share_sq"] = np.where(
            firm_sales["total_quantity"].gt(0),
            (firm_sales["firm_quantity"] / firm_sales["total_quantity"]) ** 2,
            np.nan,
        )
        hhi = (
            firm_sales.groupby(market_keys, as_index=False, dropna=False)
            .agg(
                **{
                    f"hhi_revenue_{suffix}": ("revenue_share_sq", "sum"),
                    f"hhi_quantity_{suffix}": ("quantity_share_sq", "sum"),
                }
            )
        )
        result = result.merge(hhi, on=market_keys, how="left", validate="many_to_one")

    return result.drop(columns=["_atc2_for_hhi"])


@lru_cache(maxsize=None)
def load_event_table(table_type: str, movement_suffix: str = "") -> pd.DataFrame:
    """
    Load a standardized firm-side event eligibility table.
    """
    if table_type == "movement":
        path = EVENT_TABLE_DIR / f"movement_table{movement_suffix}.csv"
        required_columns = {"BoardName", "year", "event_type", "firm_type", *EVENT_REQUIREMENTS}
    elif table_type == "interlock":
        path = INTERLOCK_EVENT_TABLE_PATH
        required_columns = {"BoardName", "year", "event_type", *EVENT_REQUIREMENTS}
    else:
        raise ValueError("table_type must be either 'movement' or 'interlock'")

    event_table = pd.read_csv(path)
    missing_columns = sorted(required_columns - set(event_table.columns))
    if missing_columns:
        raise ValueError(f"{path.name} is missing required columns: {missing_columns}")

    event_table["BoardName"] = event_table["BoardName"].astype(str)
    event_table["event_type"] = event_table["event_type"].astype(str)
    event_table["year"] = pd.to_numeric(event_table["year"], errors="raise").astype(int)
    for requirement_level in EVENT_REQUIREMENTS:
        event_table[requirement_level] = pd.to_numeric(
            event_table[requirement_level],
            errors="raise",
        ).astype(np.int8)
    if table_type == "movement":
        event_table["firm_type"] = event_table["firm_type"].astype(str).str.upper()
    return event_table


# ========================== PANEL BUILDER ==========================


class EventStudyPanelSSR:
    def __init__(
        self,
        event_type: str,
        panel_level: str = "year",
        stay_x_years: int = 3,
        balance_window: tuple[int, int] = (-4, 3),
        treatment_group: str = "B",
        large_sample: int = 0,
        personnel_definition: str = "narrow",
    ):
        self.event_type = event_type
        self.panel_level = panel_level.lower()
        self.stay_x_years = stay_x_years
        self.stay_col = f"stay_{stay_x_years}_years"
        self.balance_window = balance_window
        self.treatment_group = treatment_group.upper()
        self.movement_suffix = build_large_sample_suffix(large_sample, personnel_definition)

        if self.stay_x_years < 1:
            raise ValueError("stay_x_years must be >= 1")
        if self.panel_level not in {"year", "quarter"}:
            raise ValueError("panel_level must be either 'year' or 'quarter'")
        if len(self.balance_window) != 2 or self.balance_window[0] > self.balance_window[1]:
            raise ValueError("balance_window must be a tuple(start_offset, end_offset) with start <= end")
        if self.treatment_group not in {"A", "B"}:
            raise ValueError("treatment_group must be either 'A' or 'B'")
            
        self.ssr_base = load_ssr_panel(self.panel_level).copy()

    # -------------------------- Shared req/pure/stay panel construction --------------------------

    def _build_event_panel(self, requirement_level: str) -> pd.DataFrame:
        """
        Build one panel under the requested requirement level.
        """
        if requirement_level not in EVENT_REQUIREMENTS:
            raise ValueError(f"Unsupported requirement level: {requirement_level}")

        if self.event_type in MOVEMENT_EVENTS:
            event_table = load_event_table("movement", self.movement_suffix).copy()
            event_table = event_table.loc[
                event_table["event_type"].eq(self.event_type)
                & event_table["firm_type"].eq(self.treatment_group)
            ].copy()
        elif self.event_type in INTERLOCK_EVENTS:
            event_table = load_event_table("interlock").copy()
            event_table = event_table.loc[event_table["event_type"].eq(self.event_type)].copy()
        else:
            raise ValueError(f"Unsupported event type: {self.event_type}")

        pure_event_board_year = (
            event_table[["BoardName", "year"]]
            .dropna(subset=["BoardName", "year"])
            .drop_duplicates()
            .sort_values(["BoardName", "year"])
            .reset_index(drop=True)
        )
        pure_event_board_year["pure_event"] = np.int8(1)

        stay_event_board_year = (
            event_table.loc[event_table["req0"].eq(1), ["BoardName", "year"]]
            .dropna(subset=["BoardName", "year"])
            .drop_duplicates()
            .sort_values(["BoardName", "year"])
            .reset_index(drop=True)
        )
        stay_event_board_year[self.stay_col] = np.int8(1)

        req_event_board_year = (
            event_table.loc[event_table[requirement_level].eq(1), ["BoardName", "year"]]
            .dropna(subset=["BoardName", "year"])
            .drop_duplicates()
            .sort_values(["BoardName", "year"])
            .reset_index(drop=True)
        )
        req_event_board_year["event"] = np.int8(1)

        events = req_event_board_year.copy()
        pure_events = pure_event_board_year.copy()
        stay_events = stay_event_board_year.copy()

        events["year"] = pd.to_numeric(events["year"], errors="raise").astype(int)
        events["event"] = pd.to_numeric(events["event"], errors="raise").astype(np.int8)
        pure_events["year"] = pd.to_numeric(pure_events["year"], errors="raise").astype(int)
        pure_events["pure_event"] = pd.to_numeric(pure_events["pure_event"], errors="raise").astype(np.int8)
        stay_events["year"] = pd.to_numeric(stay_events["year"], errors="raise").astype(int)
        stay_events[self.stay_col] = pd.to_numeric(stay_events[self.stay_col], errors="raise").astype(np.int8)

        # Quarter mode expands each treated board-year into Q1-Q4, with the event anchored at Q1.
        if self.panel_level == "quarter":
            events = events.loc[events.index.repeat(4)].reset_index(drop=True)
            events["quarter"] = events.groupby(["BoardName", "year"]).cumcount() + 1
            events["quarter"] = events["quarter"].astype(np.int8)
            events.loc[events["quarter"] != 1, "event"] = 0

            pure_events = pure_events.loc[pure_events.index.repeat(4)].reset_index(drop=True)
            pure_events["quarter"] = pure_events.groupby(["BoardName", "year"]).cumcount() + 1
            pure_events["quarter"] = pure_events["quarter"].astype(np.int8)
            pure_events.loc[pure_events["quarter"] != 1, "pure_event"] = 0

            stay_events = stay_events.loc[stay_events.index.repeat(4)].reset_index(drop=True)
            stay_events["quarter"] = stay_events.groupby(["BoardName", "year"]).cumcount() + 1
            stay_events["quarter"] = stay_events["quarter"].astype(np.int8)
            stay_events.loc[stay_events["quarter"] != 1, self.stay_col] = 0

        merge_keys = ["BoardName", "year"] + (["quarter"] if self.panel_level == "quarter" else [])
        panel = self.ssr_base.merge(events, on=merge_keys, how="left")
        panel = panel.merge(pure_events, on=merge_keys, how="left")
        panel = panel.merge(stay_events, on=merge_keys, how="left")
        panel["event"] = panel["event"].fillna(0).astype(np.int8)
        panel["pure_event"] = panel["pure_event"].fillna(0).astype(np.int8)
        panel[self.stay_col] = panel[self.stay_col].fillna(0).astype(np.int8)

        first_event = (
            pure_event_board_year.groupby("BoardName", as_index=False)["year"]
            .min()
            .rename(columns={"year": "first_event_year"})
        )
        panel = panel.merge(first_event, on="BoardName", how="left")

        first_mask = (panel["pure_event"] == 1) & (panel["year"] == panel["first_event_year"])
        if self.panel_level == "quarter":
            first_mask = first_mask & panel["quarter"].eq(1)
        panel["first_event"] = first_mask.astype(np.int8)

        start_offset, end_offset = self.balance_window
        event_years = list(range(2007, 2020))

        if self.panel_level == "quarter":
            periods_lookup = {
                board_product: set(zip(group["year"].astype(int), group["quarter"].astype(int)))
                for board_product, group in panel.groupby(["BoardName", "product"])
            }
        else:
            periods_lookup = (
                panel.groupby(["BoardName", "product"])["year"]
                .agg(lambda s: set(s.dropna().astype(int).tolist()))
                .to_dict()
            )

        first_event_stay = (
            first_event.merge(
                stay_event_board_year.rename(columns={"year": "first_event_year"}),
                on=["BoardName", "first_event_year"],
                how="left",
            )
            .set_index("BoardName")[self.stay_col]
            .fillna(0)
            .astype(np.int8)
        )
        first_year_by_board = first_event.set_index("BoardName")["first_event_year"].to_dict()
        qualified_first = set()
        for board_product, periods in periods_lookup.items():
            board_name, _product = board_product
            first_year = first_year_by_board.get(board_name)
            if pd.isna(first_year) or int(first_event_stay.get(board_name, 0)) != 1:
                continue
            if self.panel_level == "quarter":
                required_periods = {
                    (year, quarter)
                    for year in range(int(first_year) + start_offset, int(first_year) + end_offset + 1)
                    for quarter in (1, 2, 3, 4)
                }
            else:
                required_periods = set(range(int(first_year) + start_offset, int(first_year) + end_offset + 1))
            if required_periods.issubset(periods):
                qualified_first.add((board_name, _product))
        panel["balance_panel_first"] = (
            pd.MultiIndex.from_frame(panel[["BoardName", "product"]]).isin(qualified_first).astype(np.int8)
        )

        for y in event_years:
            boards = set(req_event_board_year.loc[req_event_board_year["year"] == y, "BoardName"])
            if self.panel_level == "quarter":
                required_periods = {
                    (year, quarter)
                    for year in range(int(y) + start_offset, int(y) + end_offset + 1)
                    for quarter in (1, 2, 3, 4)
                }
            else:
                required_periods = set(range(int(y) + start_offset, int(y) + end_offset + 1))

            stay_boards = set(stay_event_board_year.loc[stay_event_board_year["year"] == y, "BoardName"])
            qualified = {
                board_product
                for board_product, periods in periods_lookup.items()
                if board_product[0] in boards
                and board_product[0] in stay_boards
                and required_periods.issubset(periods)
            }
            panel[f"balance_panel_{int(y)}"] = (
                pd.MultiIndex.from_frame(panel[["BoardName", "product"]]).isin(qualified).astype(np.int8)
            )

        for y in event_years:
            boards = set(req_event_board_year.loc[req_event_board_year["year"] == y, "BoardName"])
            panel[f"event_{int(y)}"] = panel["BoardName"].isin(boards).astype(np.int8)

        ordered = panel.columns.tolist()
        ordered.insert(4, ordered.pop(ordered.index("event")))
        return panel[ordered]

    # -------------------------- Output dispatch --------------------------

    def merge_event_data(self) -> pd.DataFrame:
        if self.event_type not in MOVEMENT_EVENTS and self.event_type not in INTERLOCK_EVENTS:
            raise ValueError(f"Unsupported event type: {self.event_type}")

        output_path = OUTPUT_BASE_PATH / f"{self.panel_level}-level"
        if self.event_type in MOVEMENT_EVENTS:
            output_path = OUTPUT_BASE_PATH / f"{self.panel_level}-level_{self.treatment_group}"
        output_path.mkdir(parents=True, exist_ok=True)

        output_stem = OUTPUT_STEM_OVERRIDES.get(self.event_type, self.event_type)
        movement_suffix = self.movement_suffix if self.event_type in MOVEMENT_EVENTS else ""
        for requirement_level in EVENT_REQUIREMENTS:
            panel = self._build_event_panel(requirement_level)
            panel.to_csv(
                output_path / f"ssr_firm_panel_{output_stem}_{requirement_level}{movement_suffix}.csv",
                index=False,
            )
        return pd.DataFrame()


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
    large_sample = int(RUN_CONFIG["large_sample"])
    personnel_definition = str(RUN_CONFIG["personnel_definition"])
    
    for panel_level in panel_levels:
        for treatment_group in treatment_groups:
            for event_type in event_types:
                if event_type in INTERLOCK_EVENTS and treatment_group != treatment_groups[0]:
                    continue
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
                    large_sample=large_sample,
                    personnel_definition=personnel_definition,
                ).merge_event_data()
    
    print("All panels generated!")


if __name__ == "__main__":
    main()
