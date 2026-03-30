r"""
OVERVIEW:
This module creates event study panels at the firm level, linking board interlocks and 
director transitions with firm-level SSR Data. 

INPUT:
- boardex_ssr_price_sample.csv: SSR (pharmaceutical firms) price and quantity data
- boardex_interlock_direct_firmpair.dta: Direct board interlock events (firm pairs)
- boardex_interlock_indirect_firmpair.dta: Indirect board interlock events (firm pairs)
- boardex_pharma.dta: Director transition events to/from pharmaceutical firms

OUTPUT:
- ssr_firm_panel_direct_interlock.csv: Panel with direct interlock events
- ssr_firm_panel_indirect_interlock.csv: Panel with indirect interlock events
- ssr_firm_panel_to_B_still_in_A.csv: Panel with transitions while staying on origin board
- ssr_firm_panel_to_B_not_in_A.csv: Panel with transitions after leaving origin board
"""

import pathlib
import warnings
from itertools import product
import numpy as np
import pandas as pd

# Suppress future warnings for cleaner output
warnings.filterwarnings("ignore", category=FutureWarning)

# Configure project directory paths
CURRENT_PATH = pathlib.Path(__file__).parent.resolve()
PROJECT_ROOT = CURRENT_PATH.parent
INTERIM_DATA_PATH = PROJECT_ROOT / "InterimData"
OUTPUT_BASE_PATH = PROJECT_ROOT / "data"
OUTPUT_BASE_PATH.mkdir(parents=True, exist_ok=True)


def load_ssr_yearly() -> pd.DataFrame:
    """
    Load and aggregate SSR (pharmaceutical firm) price data to yearly level.
    """
    ssr = pd.read_csv(INTERIM_DATA_PATH / "boardex_ssr_price_sample.csv")
    ssr = ssr[["BoardName", "year", "product", "atc3", "revenue", "quantity"]]
    yearly = (
        ssr.groupby(["BoardName", "year", "product", "atc3"], as_index=False)
        .agg(revenue=("revenue", "sum"), quantity=("quantity", "sum"))
        .sort_values(["BoardName", "product", "year"])
    )
    yearly["price"] = yearly["revenue"] * 1_000_000 / yearly["quantity"]
    return yearly


class EventStudyPanelSSR:
    def __init__(self, event_type: str, panel_level: str = "year", stay_x_years: int = 3):
        self.event_type = event_type
        self.panel_level = panel_level.lower()
        self.stay_x_years = stay_x_years
        self.stay_col = f"stay_{stay_x_years}_years"

        if self.panel_level not in {"year", "quarter"}:
            raise ValueError("panel_level must be either 'year' or 'quarter'")
            
        self.ssr_yearly = load_ssr_yearly()
        self.ssr_base = self._build_ssr_base()

    def _build_ssr_base(self) -> pd.DataFrame:
        if self.panel_level == "year":
            return self.ssr_yearly

        ssr = pd.read_csv(INTERIM_DATA_PATH / "boardex_ssr_price_sample.csv")
        ssr = ssr[["BoardName", "year", "quarter", "product", "atc3", "revenue", "quantity"]]
        quarterly = (
            ssr.groupby(["BoardName", "year", "quarter", "product", "atc3"], as_index=False)
            .agg(revenue=("revenue", "sum"), quantity=("quantity", "sum"))
            .sort_values(["BoardName", "product", "year", "quarter"])
        )
        quarterly["price"] = quarterly["revenue"] * 1_000_000 / quarterly["quantity"]
        quarterly["quarter"] = quarterly["quarter"].astype(np.int8)
        return quarterly

    @staticmethod
    def _required_periods(event_year: int, panel_level: str) -> set:
        if panel_level == "quarter":
            return {(y, q) for y in range(event_year - 1, event_year + 2) for q in (1, 2, 3, 4)}
        return set(range(event_year - 1, event_year + 2))

    @staticmethod
    def _event_mask(df: pd.DataFrame, year: int, boards: set, panel_level: str) -> pd.Series:
        mask = (df["year"] == year) & df["BoardName"].isin(boards)
        if panel_level == "quarter" and "quarter" in df.columns:
            mask = mask & (df["quarter"] == 1)
        return mask

    def load_event_data(self) -> pd.DataFrame:
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
        balance_col = "balance_panel_first" if event_col == "first_event" else f"balance_panel_{event_col.split('_')[-1]}"
        group = group.copy()
        group[balance_col] = 0

        treated = group[group[event_col] == 1]
        if len(treated) > 1:
            raise ValueError(f"More than one event row found for BoardName={group['BoardName'].iloc[0]}")
            
        if treated.empty or treated.iloc[0][self.stay_col] != 1:
            return group

        event_year = int(treated.iloc[0]["year"])
        if self.panel_level == "quarter" and "quarter" in group.columns:
            observed_periods = set(zip(group["year"].astype(int), group["quarter"].astype(int)))
        else:
            observed_periods = set(group["year"].astype(int))

        if self._required_periods(event_year, self.panel_level).issubset(observed_periods):
            group[balance_col] = 1
        return group

    @staticmethod
    def _add_event_year_columns(df: pd.DataFrame, event_df: pd.DataFrame, panel_level: str) -> pd.DataFrame:
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
        # 1. Store BoardName's inSSR mapping for final checking
        board_inssr = pharma.dropna(subset=["BoardName"]).set_index("BoardName")["inSSR"].to_dict()

        # 2. Extract ALL board holdings WITHOUT filtering inSSR to correctly capture gaps
        pharma_clean = pharma.dropna(subset=["DirectorID", "year", "BoardName"])
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
            
            # Use EXACT previous year! (not just the last observed record)
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

                # Check dynamic Stay X Years criteria
                stay_met = True
                if self.stay_x_years > 1:
                    for offset in range(1, self.stay_x_years):
                        future_boards = board_lookup.get((did, year + offset), set())
                        if b_new not in future_boards:
                            stay_met = False
                            break
                            
                stay_val = int(stay_met)
                
                rows.append({
                    "year": year,
                    "A": b_last,
                    "B": b_new,
                    "to_B_still_in_A": to_still,
                    "to_B_not_in_A": to_not,
                    f"{self.stay_col}_still": int(to_still == 1 and stay_val == 1),
                    f"{self.stay_col}_not": int(to_not == 1 and stay_val == 1),
                })

        return pd.DataFrame(rows)

    def _build_to_b_panel(self, mode: str) -> pd.DataFrame:
        pharma = self.load_event_data()
        transitions = self._build_board_transitions(pharma)
        
        if transitions.empty:
            print(f"Warning: No transitions generated for mode '{mode}'")
            return pd.DataFrame()

        stay_still_col = f"{self.stay_col}_still"
        stay_not_col = f"{self.stay_col}_not"

        agg_args = {
            "to_B_still_in_A": ("to_B_still_in_A", "max"),
            "to_B_not_in_A": ("to_B_not_in_A", "max"),
            stay_still_col: (stay_still_col, "max"),
            stay_not_col: (stay_not_col, "max"),
        }

        collapsed = (
            transitions.groupby(["year", "B"], as_index=False)
            .agg(**agg_args)
            .rename(columns={"B": "BoardName"})
        )

        if mode == "still":
            event_col, stay_col_src = "to_B_still_in_A", stay_still_col
        else:
            event_col, stay_col_src = "to_B_not_in_A", stay_not_col

        events = (
            collapsed.loc[collapsed[event_col].eq(1), ["year", "BoardName", event_col, stay_col_src]]
            .rename(columns={stay_col_src: self.stay_col})
            .copy()
        )

        if self.panel_level == "quarter":
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

        panel = panel.rename(columns={event_col: "event"})
        ordered = panel.columns.tolist()
        ordered.insert(4, ordered.pop(ordered.index("event")))
        return panel[ordered]

    def _build_interlock_panel(self) -> pd.DataFrame:
        event_data = self.load_event_data()
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
                and self._required_periods(int(r["first_event_year"]), self.panel_level).issubset(
                    periods_lookup.get((r["BoardName"], r["product"]), set())
                )
            ),
            axis=1,
        ).astype(np.int8)

        for y in event_years:
            need_periods = self._required_periods(int(y), self.panel_level)
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
            
            merged[bal_col] = (
                pd.MultiIndex.from_frame(merged[["BoardName", "product"]]).isin(qualified).astype(np.int8)
            )

        return merged

    def merge_event_data(self) -> None:
        output_path = OUTPUT_BASE_PATH / f"{self.panel_level}-level"
        output_path.mkdir(parents=True, exist_ok=True)

        if self.event_type in ["to B not in A", "to B still in A"]:
            still_panel = self._build_to_b_panel(mode="still")
            not_panel = self._build_to_b_panel(mode="not")

            if not still_panel.empty:
                still_panel.to_csv(output_path / f"ssr_firm_panel_to_B_still_in_A.csv", index=False)
            if not not_panel.empty:
                not_panel.to_csv(output_path / f"ssr_firm_panel_to_B_not_in_A.csv", index=False)
            return

        if self.event_type in ["direct interlock", "indirect interlock"]:
            panel = self._build_interlock_panel()
            panel.to_csv(output_path / f"ssr_firm_panel_{self.event_type.replace(' ', '_')}.csv", index=False)
            return

        if self.event_type == "no event":
            return

        raise ValueError("Unsupported event type")


def main() -> None:
    panel_levels = ["quarter"]
    event_types = ["direct interlock", "indirect interlock", "to B not in A", "to B still in A"] 
    
    # You can customize stay_x_years. Example below uses 3 (same as original code)
    stay_req = 3
    
    for panel_level in panel_levels:
        for event_type in event_types:
            print(f"Generating panel: '{event_type}' at {panel_level} level with stay_{stay_req}_years requirement...")
            EventStudyPanelSSR(event_type, panel_level=panel_level, stay_x_years=stay_req).merge_event_data()
    
    print("All panels generated!")


if __name__ == "__main__":
    main()