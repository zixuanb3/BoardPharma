r"""
Purpose:
Build quarterly firm-level event-study panels for SSR pharma firms from standardized
movement and interlock event tables. Annual roster variants remain annual at the
source level, but their events are anchored at Q1 and merged onto quarterly SSR
price observations for regression. The regression outcomes are the source
price1 and price0 columns; no price is reconstructed from revenue and quantity.

Process:
- Build an SSR base panel at the selected year or quarter frequency.
- Read precomputed firm-time event eligibility from variant-specific event tables.
- Keep pure_event from the unfiltered event table so first_event is not changed
  by req0/req1/req2 filters.
- Preserve the actual movement quarter when the quarter roster is used.
- Use Q1 only as a documented anchor for interlock events whose source files are annual.
- Compute balance tags in annual periods or quarterly periods, as appropriate.
- Export all files under a variant-specific output directory.

Input:
- InterimData/boardex_ssr_price_sample.csv
- `data/roster_variants/<variant>/event_tables/movement_table.csv`

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
- `data/roster_variants/<variant>/<year|quarter>-level[_A|_B]/ssr_firm_panel_*.csv`
"""

import pathlib
import warnings
import numpy as np
import pandas as pd
from functools import lru_cache

from pipeline_variant_config import (
    INTERIM_DATA_PATH,
    PERSONNEL_DEFINITIONS,
    OUTPUT_PROJECT_ROOT,
    ROSTER_VARIANTS,
    configured_personnel_definitions,
    configured_variants,
    get_variant,
    personnel_output_dir,
)

# Suppress future warnings for cleaner output
warnings.filterwarnings("ignore", category=FutureWarning)

# Configure data and output paths independently. Code lives under the copied
# BoardPharma (1) directory, while the raw InterimData lives under BoardPharma.
PROJECT_ROOT = OUTPUT_PROJECT_ROOT
OUTPUT_BASE_PATH = PROJECT_ROOT / "data"
OUTPUT_BASE_PATH.mkdir(parents=True, exist_ok=True)

MOVEMENT_EVENTS = {"to_B_still_in_A", "to_B_not_in_A", "interlock_dissolution"}
INTERLOCK_EVENTS = set()
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
# Source roster levels are stored in pipeline_variant_config.py. The regression
# panel level is quarterly for every variant. Annual source events are anchored
# at Q1 when they are merged onto the quarterly SSR price panel.
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
    ],
    "stay_x_years": 2,
    "balance_window_years": (-1, 1),
    "balance_window_quarters": (-4, 7),
    "treatment_groups": ["B","A"],
    "roster_variants": configured_variants(),
    "personnel_definitions": configured_personnel_definitions(),
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
    group_cols = ["BoardName", "year", "product", "atc3"]
    sort_cols = ["BoardName", "product", "year"]
    if panel_level == "quarter":
        group_cols = ["BoardName", "year", "quarter", "product", "atc3"]
        sort_cols = ["BoardName", "product", "year", "quarter"]

    panel = (
        ssr[group_cols + ["revenue", "quantity", "price1", "price0"]]
        .groupby(group_cols, as_index=False)
        .agg(
            revenue=("revenue", "sum"),
            quantity=("quantity", "sum"),
            price1=("price1", "mean"),
            price0=("price0", "mean"),
        )
        .sort_values(sort_cols)
    )
    if panel_level == "quarter":
        panel["quarter"] = panel["quarter"].astype(np.int8)
    return panel


@lru_cache(maxsize=None)
def load_event_table(
    table_type: str,
    roster_variant: str,
    personnel_definition: str,
) -> pd.DataFrame:
    """
    Load a standardized firm-side event eligibility table.
    """
    if table_type != "movement":
        raise ValueError("Only movement event tables are supported")
    path = (
        personnel_output_dir(roster_variant, personnel_definition)
        / "event_tables"
        / "movement_table.csv"
    )
    required_columns = {"BoardName", "year", "event_type", "firm_type", *EVENT_REQUIREMENTS}

    event_table = pd.read_csv(path)
    missing_columns = sorted(required_columns - set(event_table.columns))
    if missing_columns:
        raise ValueError(f"{path.name} is missing required columns: {missing_columns}")

    event_table["BoardName"] = event_table["BoardName"].astype(str)
    event_table["event_type"] = event_table["event_type"].astype(str)
    event_table["year"] = pd.to_numeric(event_table["year"], errors="raise").astype(int)
    if "quarter" in event_table.columns:
        event_table["quarter"] = pd.to_numeric(event_table["quarter"], errors="raise").astype(int)
        if not event_table["quarter"].between(1, 4).all():
            raise ValueError(f"{path.name} contains invalid quarters")
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
        roster_variant: str = "imputed_year",
        personnel_definition: str = "narrow",
        panel_level: str | None = None,
        stay_x_years: int = 3,
        balance_window: tuple[int, int] | None = None,
        treatment_group: str = "B",
    ):
        self.event_type = event_type
        self.roster_variant = roster_variant
        if personnel_definition not in PERSONNEL_DEFINITIONS:
            allowed = ", ".join(PERSONNEL_DEFINITIONS)
            raise ValueError(
                f"Unknown personnel_definition={personnel_definition}; expected one of: {allowed}"
            )
        self.personnel_definition = personnel_definition
        metadata = get_variant(roster_variant)
        source_level = str(metadata["panel_level"])
        inferred_level = str(metadata.get("regression_panel_level", source_level))
        self.source_panel_level = source_level
        self.panel_level = (panel_level or inferred_level).lower()
        self.stay_x_years = stay_x_years
        self.stay_col = f"stay_{stay_x_years}_years"
        if balance_window is None:
            balance_window = (-1, 2) if self.panel_level == "year" else (-4, 7)
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
            
        self.ssr_base = load_ssr_panel(self.panel_level).copy()

    # -------------------------- Shared req/pure/stay panel construction --------------------------

    def _build_event_panel_legacy(self, requirement_level: str) -> pd.DataFrame:
        """
        Build one panel under the requested requirement level.
        """
        if requirement_level not in EVENT_REQUIREMENTS:
            raise ValueError(f"Unsupported requirement level: {requirement_level}")

        if self.event_type in MOVEMENT_EVENTS:
            event_table = load_event_table(
                "movement", self.roster_variant, self.personnel_definition
            ).copy()
            event_table = event_table.loc[
                event_table["event_type"].eq(self.event_type)
                & event_table["firm_type"].eq(self.treatment_group)
            ].copy()
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
            event_table.loc[event_table["req1"].eq(1), ["BoardName", "year"]]
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
        # Balance flags are generated only for years containing an actual
        # requirement-qualified event. first_event remains descriptive.
        event_years = sorted(req_event_board_year["year"].dropna().astype(int).unique())

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

            qualified = {
                board_product
                for board_product, periods in periods_lookup.items()
                if board_product[0] in boards
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

    def _build_event_panel(self, requirement_level: str) -> pd.DataFrame:
        """Build a panel using the event's actual year or year-quarter."""
        if requirement_level not in EVENT_REQUIREMENTS:
            raise ValueError(f"Unsupported requirement level: {requirement_level}")

        if self.event_type in MOVEMENT_EVENTS:
            event_table = load_event_table(
                "movement", self.roster_variant, self.personnel_definition
            ).copy()
            event_table = event_table.loc[
                event_table["event_type"].eq(self.event_type)
                & event_table["firm_type"].eq(self.treatment_group)
            ].copy()
        else:
            raise ValueError(f"Unsupported event type: {self.event_type}")

        # Annual movement tables do not contain an observed quarter. For the
        # quarterly regression convention, place each annual event in Q1.
        if self.panel_level == "quarter" and "quarter" not in event_table.columns:
            event_table["quarter"] = np.int8(1)

        time_columns = ["year"]
        if self.panel_level == "quarter":
            if "quarter" not in event_table.columns:
                raise ValueError(
                    f"{self.roster_variant} quarter event table has no quarter column"
                )
            time_columns.append("quarter")
        merge_keys = ["BoardName", *time_columns]

        pure_events = (
            event_table[merge_keys]
            .dropna()
            .drop_duplicates()
            .assign(pure_event=np.int8(1))
        )
        stay_events = (
            event_table.loc[event_table["req1"].eq(1), merge_keys]
            .dropna()
            .drop_duplicates()
            .assign(**{self.stay_col: np.int8(1)})
        )
        req_events = (
            event_table.loc[event_table[requirement_level].eq(1), merge_keys]
            .dropna()
            .drop_duplicates()
            .assign(event=np.int8(1))
        )

        panel = self.ssr_base.merge(req_events, on=merge_keys, how="left")
        panel = panel.merge(pure_events, on=merge_keys, how="left")
        panel = panel.merge(stay_events, on=merge_keys, how="left")
        panel["event"] = panel["event"].fillna(0).astype(np.int8)
        panel["pure_event"] = panel["pure_event"].fillna(0).astype(np.int8)
        panel[self.stay_col] = panel[self.stay_col].fillna(0).astype(np.int8)

        def add_time_id(frame: pd.DataFrame) -> pd.DataFrame:
            frame = frame.copy()
            if self.panel_level == "year":
                frame["_time_id"] = frame["year"].astype(int)
            else:
                frame["_time_id"] = frame["year"].astype(int) * 4 + frame["quarter"].astype(int) - 1
            return frame

        panel = add_time_id(panel)
        pure_events = add_time_id(pure_events)
        stay_events = add_time_id(stay_events)
        req_events = add_time_id(req_events)

        first_event = (
            pure_events.groupby("BoardName", as_index=False)["_time_id"]
            .min()
            .rename(columns={"_time_id": "first_event_period"})
        )
        first_event["first_event_year"] = first_event["first_event_period"]
        if self.panel_level == "year":
            first_event["first_event_year"] = first_event["first_event_period"].astype(int)
        else:
            first_event["first_event_year"] = first_event["first_event_period"] // 4
            first_event["first_event_quarter"] = first_event["first_event_period"] % 4 + 1
        panel = panel.merge(first_event, on="BoardName", how="left")
        panel["first_event"] = (
            panel["pure_event"].eq(1)
            & panel["_time_id"].eq(panel["first_event_period"])
        ).astype(np.int8)

        start_offset, end_offset = self.balance_window
        periods_lookup = (
            panel.groupby(["BoardName", "product"])["_time_id"]
            .agg(lambda values: set(values.dropna().astype(int).tolist()))
            .to_dict()
        )
        def board_product_membership(values: set[tuple[str, str]]) -> pd.Series:
            keys = pd.MultiIndex.from_frame(panel[["BoardName", "product"]])
            return keys.isin(values).astype(np.int8)

        observed_time_ids = set(panel["_time_id"].astype(int))
        event_records = req_events.loc[req_events["_time_id"].isin(observed_time_ids)].copy()
        dynamic_columns: dict[str, pd.Series] = {}
        for event_row in event_records[merge_keys + ["_time_id"]].drop_duplicates().itertuples(index=False):
            row_dict = dict(zip(merge_keys + ["_time_id"], event_row))
            event_period = int(row_dict["_time_id"])
            if self.panel_level == "year":
                label = str(int(row_dict["year"]))
            else:
                label = f"{int(row_dict['year'])}q{int(row_dict['quarter'])}"
            event_board = str(row_dict["BoardName"])
            event_boards = set(
                req_events.loc[req_events["_time_id"].eq(event_period), "BoardName"]
            )
            qualified: set[tuple[str, str]] = set()
            required = set(range(event_period + start_offset, event_period + end_offset + 1))
            for board_product, periods in periods_lookup.items():
                # balance_panel only tests data completeness around this
                # actual event. The event column carries req0/req1/req2.
                if board_product[0] in event_boards:
                    if required.issubset(periods):
                        qualified.add(board_product)
            dynamic_columns[f"balance_panel_{label}"] = board_product_membership(qualified)
            dynamic_columns[f"event_{label}"] = panel["BoardName"].isin(event_boards).astype(np.int8)

        if dynamic_columns:
            panel = pd.concat([panel, pd.DataFrame(dynamic_columns, index=panel.index)], axis=1)

        panel = panel.drop(columns=["_time_id"])
        ordered = panel.columns.tolist()
        ordered.insert(4, ordered.pop(ordered.index("event")))
        return panel[ordered]

    # -------------------------- Output dispatch --------------------------

    def merge_event_data(self) -> pd.DataFrame:
        if self.event_type not in MOVEMENT_EVENTS:
            raise ValueError(f"Unsupported event type: {self.event_type}")

        output_path = personnel_output_dir(
            self.roster_variant, self.personnel_definition
        ) / f"{self.panel_level}-level"
        if self.event_type in MOVEMENT_EVENTS:
            output_path = personnel_output_dir(
                self.roster_variant, self.personnel_definition
            ) / f"{self.panel_level}-level_{self.treatment_group}"
        output_path.mkdir(parents=True, exist_ok=True)

        output_stem = OUTPUT_STEM_OVERRIDES.get(self.event_type, self.event_type)
        for requirement_level in EVENT_REQUIREMENTS:
            panel = self._build_event_panel(requirement_level)
            panel.to_csv(
                output_path / f"ssr_firm_panel_{output_stem}_{requirement_level}.csv",
                index=False,
            )
        return pd.DataFrame()


def main() -> None:
    def ensure_list(v):
        # Allow both single-value and list-style config inputs.
        if isinstance(v, str):
            return [v]
        return list(v)

    event_types = ensure_list(RUN_CONFIG["event_types"])
    stay_req = int(RUN_CONFIG["stay_x_years"])
    treatment_groups = [str(x).upper() for x in ensure_list(RUN_CONFIG.get("treatment_groups", ["B"]))]
    roster_variants = ensure_list(RUN_CONFIG["roster_variants"])
    personnel_definitions = ensure_list(RUN_CONFIG["personnel_definitions"])
    
    for roster_variant in roster_variants:
        metadata = get_variant(str(roster_variant))
        panel_level = str(
            metadata.get("regression_panel_level", metadata["panel_level"])
        )
        balance_window = tuple(
            RUN_CONFIG[
                "balance_window_quarters"
                if panel_level == "quarter"
                else "balance_window_years"
            ]
        )
        for personnel_definition in personnel_definitions:
            for treatment_group in treatment_groups:
                for event_type in event_types:
                    if event_type in INTERLOCK_EVENTS and treatment_group != treatment_groups[0]:
                        continue
                    print(
                        f"Generating panel: '{event_type}' | variant={roster_variant} | "
                        f"tier={personnel_definition} | level={panel_level} | "
                        f"treatment_group={treatment_group} | stay_{stay_req}_years | "
                        f"balance_window=t{balance_window[0]:+d}..t{balance_window[1]:+d}"
                    )
                    EventStudyPanelSSR(
                        event_type,
                        roster_variant=str(roster_variant),
                        personnel_definition=str(personnel_definition),
                        panel_level=panel_level,
                        stay_x_years=stay_req,
                        balance_window=balance_window,
                        treatment_group=treatment_group,
                    ).merge_event_data()
    
    print("All panels generated!")


if __name__ == "__main__":
    main()
