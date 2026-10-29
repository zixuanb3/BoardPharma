"""
Purpose:
    Build quarterly SSR event cohorts from the current 1.1 firm panels. Keep
    event/req2/Not, balanced T-4 through T+7 windows, and exclude counterpart-only
    controls, reproducing code new/CohortPanelMaker.py for these settings.

Process:
    1. Read the selected late, req1_spec, and stay_spec upstream files. Recover
       basic-event flags from the full movement table for control exclusion;
       events failing req2 cannot become controls inside the event window.
    2. Select treated companies and complete company-product windows exactly
       as code new. Exclude counterpart-only controls using the original A/B
       set difference, and retain treated companies with other window events.
    3. Select ATC partners from individual candidates satisfying stay==1 AND
       the treated side's requirement1_A/B==1, including dissolution, matching
       the current 1.1 req2 definition. Match the treated product's ATC3 at T
       against every partner's full SSR portfolio: share_time_spec=new uses T;
       origin pools T-2, T-1, T, T+1, including quarters across calendar years.
    4. Preserve input columns and code new's row/column order and additional
       window_other_event, atc3_sharing, other_event_*, and ever_* indicators.
       Basic-event flags are temporary, not restored to saved CSVs. Preserve
       existing first_event metadata; do not generate first_event cohorts.

Input:
    InterimData/boardex_ssr_price_sample.csv (full SSR ATC3 market)
    data/ssrquarter/firm_panels/<variant>/late_<late>/req1_<req1_spec>/
        stay_<stay_spec>/quarter-level_<A|B>/ssr_firm_panel_<event>_req2.csv
    data/ssrquarter/event_tables/<variant>/late_<late>/req1_<req1_spec>/
        stay_<stay_spec>/{movement_event_candidates,movement_table}.csv
    Paths are relative to the BoardPharma project root, not the working folder.
    No substitute inputs or previously saved cohort files are used.

Output:
    data/ssrquarter/cohort_data/<variant>/late_<late>/req1_<req1_spec>/
        stay_<stay_spec>/share_time_<share_time_spec>/
        quarter-level_<side>_without_<other>/event/req2/Not/
        <event>_quarter_cohort_<year>q<quarter>_balanced.csv
    Include 2009Q1-2018Q4; skip cohort periods without upstream event/balance
    columns, exactly as code new does. Only cohort panel CSVs are generated;
    no distribution tables, figures, or first_event cohort files are produced.
    --all-configurations runs late={0,3} and req1_spec/stay_spec/share_time_spec
    ={new,origin}: 16 settings, each for both variants, three events and A/B.
    Existing output files are never overwritten. Input/output roots may be
    overridden on the command line. No sharing_pair_spec is used.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from itertools import product
from pathlib import Path
import re
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SSR_INPUT = PROJECT_ROOT / "InterimData" / "boardex_ssr_price_sample.csv"
PANEL_DIR = PROJECT_ROOT / "data" / "ssrquarter" / "firm_panels"
EVENT_DIR = PROJECT_ROOT / "data" / "ssrquarter" / "event_tables"
OUTPUT_DIR = PROJECT_ROOT / "data" / "ssrquarter" / "cohort_data"
VARIANTS = ("imputed_yearquarter", "drop_sentinel_yearquarter")
RUN_CONFIG = {
    "late": 3,
    "req1_spec": "new",
    "stay_spec": "new",
    "share_time_spec": "new",
}
EVENT_CONFIGS = {
    "to_B_not_in_A": ("to_B_not_in_A", "other_event_not", "ever_not"),
    "to_B_still_in_A": ("to_B_still_in_A", "other_event_still", "ever_still"),
    "interlock_dissolution": (
        "interlock_dissolution_leave_B",
        "other_event_dissolution",
        "ever_dissolution",
    ),
}
TREATMENT_GROUPS = ("B", "A")
PERIODS = tuple(
    (year, quarter) for year in range(2009, 2019) for quarter in range(1, 5)
)
ID_COLUMNS = ["BoardName", "product"]


@dataclass(frozen=True)
class Specification:
    """Select one upstream setting and one ATC portfolio-time rule."""

    late: int
    req1_spec: str
    stay_spec: str
    share_time_spec: str

    def upstream_dir(self, root: Path, variant: str) -> Path:
        return (
            root
            / variant
            / f"late_{self.late}"
            / f"req1_{self.req1_spec}"
            / f"stay_{self.stay_spec}"
        )

    def output_dir(self, root: Path, variant: str) -> Path:
        return self.upstream_dir(root, variant) / f"share_time_{self.share_time_spec}"


def normalize_key(value: object) -> str:
    """Use code new's strip/uppercase keys only for ATC matching."""
    return "" if pd.isna(value) else str(value).strip().upper()


def time_id(frame: pd.DataFrame) -> pd.Series:
    """Number consecutive quarters exactly as code new."""
    return frame["year"].astype(int) * 4 + frame["quarter"].astype(int) - 1


def level_folder(side: str) -> str:
    return f"quarter-level_{side}_without_{'B' if side == 'A' else 'A'}"


def cohort_path(root: Path, side: str, event: str, year: int, quarter: int) -> Path:
    return (
        root
        / level_folder(side)
        / "event"
        / "req2"
        / "Not"
        / f"{event}_quarter_cohort_{year}q{quarter}_balanced.csv"
    )


def load_atc3_indices(path: Path) -> tuple[dict, dict]:
    """Read full SSR portfolios, keeping code new's product/class preparation."""
    required = ["BoardName", "year", "quarter", "product", "atc3"]
    market = pd.read_csv(path, usecols=required, low_memory=False).dropna(
        subset=required
    )
    for column in ("BoardName", "product", "atc3"):
        market[column] = market[column].astype(str).str.strip()
    for column in ("year", "quarter"):
        market[column] = pd.to_numeric(market[column], errors="raise").astype(int)
    if not market["quarter"].between(1, 4).all():
        raise ValueError(f"{path} contains invalid quarters")
    market["_time_id"] = time_id(market)
    market["_firm_key"] = market["BoardName"].map(normalize_key)
    market["_product_key"] = market["product"].map(normalize_key)
    market = market.drop_duplicates(["_time_id", "_firm_key", "product", "atc3"])
    partner_index = market.groupby(["_time_id", "_firm_key"])["atc3"].agg(set).to_dict()
    product_index = (
        market.groupby(["_time_id", "_firm_key", "_product_key"])["atc3"]
        .agg(set)
        .to_dict()
    )
    return partner_index, product_index


class CohortPanelMaker:
    """Build the six event/side cohorts for one upstream specification."""

    def __init__(
        self,
        specification: Specification,
        variant: str,
        panel_root: Path,
        event_root: Path,
        atc_indices: tuple[dict, dict],
    ) -> None:
        self.specification = specification
        self.variant = variant
        self.panel_root = specification.upstream_dir(panel_root, variant)
        self.event_root = specification.upstream_dir(event_root, variant)
        self.partner_atc3, self.product_atc3 = atc_indices
        self.candidates = pd.read_csv(self.event_root / "movement_event_candidates.csv")
        stay_columns = [
            c for c in self.candidates if re.fullmatch(r"stay_\d+_years", c)
        ]
        if len(stay_columns) != 1:
            raise ValueError(
                f"Expected exactly one stay_x_years column: {stay_columns}"
            )
        self.stay_column = stay_columns[0]
        for column in (
            "year",
            self.stay_column,
            "FirmA_event_time_id",
            "FirmB_event_time_id",
            "requirement1_A",
            "requirement1_B",
        ):
            dtype = (
                "int8"
                if column
                in {
                    self.stay_column,
                    "requirement1_A",
                    "requirement1_B",
                }
                else int
            )
            self.candidates[column] = pd.to_numeric(
                self.candidates[column],
                errors="raise",
            ).astype(dtype)
        self.movement = pd.read_csv(self.event_root / "movement_table.csv")
        for column in ("year", "quarter"):
            self.movement[column] = pd.to_numeric(
                self.movement[column], errors="raise"
            ).astype(int)
        self.movement["_time_id"] = time_id(self.movement)
        self.movement["firm_type"] = self.movement["firm_type"].astype(str).str.upper()
        for column in ("req0", "req1", "req2"):
            self.movement[column] = pd.to_numeric(
                self.movement[column], errors="raise"
            ).astype("int8")
        self.panels: dict[tuple[str, str], pd.DataFrame] = {}
        self.event_timing: dict[tuple[str, str], pd.DataFrame] = {}
        self.partner_indices: dict[tuple[str, str], dict] = {}

    def panel_path(self, event: str, side: str) -> Path:
        return (
            self.panel_root
            / f"quarter-level_{side}"
            / f"ssr_firm_panel_{EVENT_CONFIGS[event][0]}_req2.csv"
        )

    def load_panel(self, event: str, side: str) -> pd.DataFrame:
        """Restore basic-event flags in memory without changing saved inputs."""
        key = (event, side)
        if key not in self.panels:
            frame = pd.read_csv(self.panel_path(event, side))
            for column in ("year", "quarter"):
                frame[column] = pd.to_numeric(frame[column], errors="raise").astype(int)
            frame["_time_id"] = time_id(frame)
            frame["event"] = pd.to_numeric(frame["event"], errors="raise").astype(
                "int8"
            )
            for column in ("first_event_year", "first_event_quarter"):
                if column in frame:
                    frame[column] = pd.to_numeric(frame[column], errors="coerce")
            basic_events = (
                self.movement.loc[
                    self.movement["event_type"].eq(event)
                    & self.movement["firm_type"].eq(side),
                    ["BoardName", "_time_id"],
                ]
                .dropna()
                .drop_duplicates()
            )
            frame["pure_event"] = (
                pd.MultiIndex.from_frame(
                    frame[["BoardName", "_time_id"]],
                )
                .isin(pd.MultiIndex.from_frame(basic_events))
                .astype("int8")
            )
            self.panels[key] = frame
        return self.panels[key]

    def timing(self, event: str, side: str) -> pd.DataFrame:
        """Get actual req2 event quarters from the same panels as code new."""
        key = (event, side)
        if key not in self.event_timing:
            frame = self.load_panel(event, side)
            self.event_timing[key] = (
                frame.loc[frame["event"].eq(1), ["BoardName", "_time_id"]]
                .drop_duplicates()
                .reset_index(drop=True)
            )
        return self.event_timing[key]

    def eligible_candidates(self, event: str, side: str) -> pd.DataFrame:
        """Apply current upstream req2 to each candidate, including dissolution."""
        return self.candidates.loc[
            self.candidates["event_type"].eq(event)
            & self.candidates[self.stay_column].eq(1)
            & self.candidates[f"requirement1_{side}"].eq(1)
        ]

    def filter_counterpart_controls(
        self,
        controls: pd.DataFrame,
        event: str,
        side: str,
        cohort_time: int,
    ) -> pd.DataFrame:
        """Preserve code new's counterpart-only A/B set-difference exclusion."""
        current = self.eligible_candidates(event, side)
        current = current.loc[current[f"Firm{side}_event_time_id"].eq(cohort_time)]
        firms_a = set(current["FirmA"].dropna())
        firms_b = set(current["FirmB"].dropna())
        drop_firms = firms_b - firms_a if side == "A" else firms_a - firms_b
        return controls.loc[~controls["BoardName"].isin(drop_firms)].copy()

    def partner_index(self, event: str, side: str) -> dict:
        """Map treated-side event quarter/company to its qualified partners."""
        key = (event, side)
        if key not in self.partner_indices:
            other = "B" if side == "A" else "A"
            current = (
                self.eligible_candidates(event, side)[
                    [f"Firm{side}_event_time_id", f"Firm{side}", f"Firm{other}"]
                ]
                .dropna()
                .copy()
            )
            current["_treated_key"] = current[f"Firm{side}"].map(normalize_key)
            current["_partner_key"] = current[f"Firm{other}"].map(normalize_key)
            self.partner_indices[key] = (
                current.groupby([f"Firm{side}_event_time_id", "_treated_key"])[
                    "_partner_key"
                ]
                .agg(set)
                .to_dict()
            )
        return self.partner_indices[key]

    def add_window_other_event(
        self,
        frame: pd.DataFrame,
        event: str,
        side: str,
        cohort_time: int,
        window_periods: list[int],
    ) -> pd.DataFrame:
        """Flag other qualifying events, including same-type events away from T."""
        events = [
            frame.loc[frame["event"].eq(1), ["BoardName", "_time_id"]]
            .drop_duplicates()
            .assign(event_type=event),
        ]
        events.extend(
            self.timing(other, side).assign(event_type=other)
            for other in EVENT_CONFIGS
            if other != event
        )
        all_events = pd.concat(events, ignore_index=True).drop_duplicates()
        all_events = all_events.loc[all_events["_time_id"].isin(window_periods)]
        focal = all_events["event_type"].eq(event) & all_events["_time_id"].eq(
            cohort_time
        )
        firms = set(all_events.loc[~focal, "BoardName"])
        result = frame.copy()
        result["window_other_event"] = result["BoardName"].isin(firms).astype("int8")
        return result

    def add_atc3_sharing(
        self,
        frame: pd.DataFrame,
        event: str,
        side: str,
        cohort_time: int,
        event_col: str,
    ) -> pd.DataFrame:
        """Freeze each treated product's overlap at T across its cohort window."""
        result = frame.copy()
        result["atc3_sharing"] = pd.Series(0, index=result.index, dtype="int8")
        treated_firms = set(
            result.loc[
                result["_time_id"].eq(cohort_time) & result[event_col].eq(1),
                "BoardName",
            ].dropna()
        )
        treated_keys = {normalize_key(value) for value in treated_firms}
        if not treated_keys:
            return result
        partners = self.partner_index(event, side)
        result["_cohort_firm_key"] = result["BoardName"].map(normalize_key)
        result["_product_key"] = result["product"].map(normalize_key)
        event_products = (
            result.loc[
                result["_time_id"].eq(cohort_time)
                & result[event_col].eq(1)
                & result["_cohort_firm_key"].isin(treated_keys),
                ["_cohort_firm_key", "_product_key"],
            ]
            .dropna()
            .drop_duplicates()
        )
        partner_periods = (
            (cohort_time,)
            if self.specification.share_time_spec == "new"
            else range(cohort_time - 2, cohort_time + 2)
        )
        sharing_pairs = set()
        for firm_key, product_key in event_products.itertuples(index=False, name=None):
            own_classes = self.product_atc3.get(
                (cohort_time, firm_key, product_key), set()
            )
            partner_classes: set[str] = set()
            for partner in partners.get((cohort_time, firm_key), set()):
                for period in partner_periods:
                    partner_classes.update(
                        self.partner_atc3.get((period, partner), set())
                    )
            if own_classes.intersection(partner_classes):
                sharing_pairs.add((firm_key, product_key))
        if sharing_pairs:
            row_index = pd.MultiIndex.from_frame(
                result[["_cohort_firm_key", "_product_key"]]
            )
            sharing_index = pd.MultiIndex.from_tuples(
                sharing_pairs,
                names=["_cohort_firm_key", "_product_key"],
            )
            result.loc[row_index.isin(sharing_index), "atc3_sharing"] = 1
        result.loc[~result[event_col].eq(1), "atc3_sharing"] = 0
        return result.drop(columns=["_cohort_firm_key", "_product_key"])

    def add_other_and_ever_events(
        self,
        frame: pd.DataFrame,
        event: str,
        side: str,
        cohort_time: int,
    ) -> pd.DataFrame:
        """Preserve actual-quarter other-event and strictly-before-T ever flags."""
        result = frame.copy()
        for other in EVENT_CONFIGS:
            if other == event:
                continue
            index = pd.MultiIndex.from_frame(self.timing(other, side))
            rows = pd.MultiIndex.from_frame(result[["BoardName", "_time_id"]])
            result[EVENT_CONFIGS[other][1]] = rows.isin(index).astype("int8")
        prior = self.movement.loc[
            self.movement["firm_type"].eq(side)
            & self.movement["req2"].eq(1)
            & self.movement["_time_id"].lt(cohort_time)
        ]
        for other in EVENT_CONFIGS:
            firms = set(prior.loc[prior["event_type"].eq(other), "BoardName"])
            result[EVENT_CONFIGS[other][2]] = (
                result["BoardName"].isin(firms).astype("int8")
            )
        return result

    def build_one_cohort(
        self,
        frame: pd.DataFrame,
        event: str,
        side: str,
        year: int,
        quarter: int,
    ) -> pd.DataFrame:
        """Follow code new's balanced event/req2/Not selection and marker order."""
        label = f"{year}q{quarter}"
        event_col, balance_col = f"event_{label}", f"balance_panel_{label}"
        cohort_time = year * 4 + quarter - 1
        window_periods = list(range(cohort_time - 4, cohort_time + 8))
        window = frame.loc[frame["_time_id"].isin(window_periods)].copy()
        basic_at_t = (
            frame.loc[frame["_time_id"].eq(cohort_time)]
            .groupby("BoardName")["pure_event"]
            .max()
        )
        basic_firms = set(basic_at_t[basic_at_t.eq(1)].index)
        control_firms = set(frame["BoardName"].dropna().unique()) - basic_firms
        req_firms = set(
            frame.loc[frame[event_col].eq(1), "BoardName"].dropna().unique()
        )
        treated = window.loc[window["BoardName"].isin(basic_firms & req_firms)].copy()
        sizes = treated.groupby(ID_COLUMNS).size()
        treated = treated.loc[
            treated.set_index(ID_COLUMNS).index.isin(
                sizes[sizes.eq(len(window_periods))].index
            )
            & treated[balance_col].eq(1)
        ].copy()
        basic_in_window = window.groupby(ID_COLUMNS)["pure_event"].max()
        controls = window.loc[
            window["BoardName"].isin(control_firms)
            & window.set_index(ID_COLUMNS).index.isin(
                basic_in_window[basic_in_window.eq(0)].index
            )
        ].copy()
        sizes = controls.groupby(ID_COLUMNS).size()
        controls = controls.loc[
            controls.set_index(ID_COLUMNS).index.isin(
                sizes[sizes.eq(len(window_periods))].index
            )
        ].copy()
        if not controls.empty:
            controls = self.filter_counterpart_controls(
                controls, event, side, cohort_time
            )
        if not controls.empty and controls[event_col].ne(0).any():
            raise AssertionError(f"Controls must have {event_col}=0: {event}, {side}")
        cohort = pd.concat([treated, controls], axis=0, ignore_index=True)
        cohort = cohort.sort_values(ID_COLUMNS + ["year", "quarter"]).reset_index(
            drop=True
        )
        cohort = self.add_window_other_event(
            cohort, event, side, cohort_time, window_periods
        )
        cohort = self.add_atc3_sharing(cohort, event, side, cohort_time, event_col)
        cohort = self.add_other_and_ever_events(cohort, event, side, cohort_time)
        return cohort.drop(columns=["pure_event"])

    def run(self, output_root: Path) -> int:
        """Write only cohort panels, preserving the original cohort loop order."""
        cohort_count = 0
        for side in TREATMENT_GROUPS:
            for event in EVENT_CONFIGS:
                frame = self.load_panel(event, side)
                for year, quarter in PERIODS:
                    label = f"{year}q{quarter}"
                    columns = (f"event_{label}", f"balance_panel_{label}")
                    if any(column not in frame for column in columns):
                        continue
                    for column in columns:
                        frame[column] = pd.to_numeric(
                            frame[column], errors="raise"
                        ).astype("int8")
                    cohort = self.build_one_cohort(frame, event, side, year, quarter)
                    path = cohort_path(output_root, side, event, year, quarter)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    cohort.to_csv(path, index=False, mode="x")
                    cohort_count += 1
        return cohort_count


def preflight(args: argparse.Namespace, specifications: list[Specification]) -> None:
    """Check all inputs and prevent collisions before writing the first file."""
    if not args.ssr_input.is_file():
        raise FileNotFoundError(args.ssr_input)
    for spec in specifications:
        for variant in VARIANTS:
            event_root = spec.upstream_dir(args.event_dir, variant)
            panel_root = spec.upstream_dir(args.panel_dir, variant)
            inputs = [
                event_root / name
                for name in ("movement_event_candidates.csv", "movement_table.csv")
            ]
            inputs.extend(
                panel_root
                / f"quarter-level_{side}"
                / f"ssr_firm_panel_{config[0]}_req2.csv"
                for side in TREATMENT_GROUPS
                for config in EVENT_CONFIGS.values()
            )
            for path in inputs:
                if not path.is_file():
                    raise FileNotFoundError(path)
            outputs = [
                cohort_path(
                    spec.output_dir(args.output_dir, variant),
                    side,
                    event,
                    year,
                    quarter,
                )
                for side in TREATMENT_GROUPS
                for event in EVENT_CONFIGS
                for year, quarter in PERIODS
            ]
            for path in outputs:
                if path.exists():
                    raise FileExistsError(
                        f"Output already exists; refusing to overwrite: {path}"
                    )


def main() -> None:
    """Run the selected configuration or all sixteen without overwriting files."""
    parser = argparse.ArgumentParser(
        description="Build quarterly event/req2/Not SSR cohorts."
    )
    parser.add_argument("--ssr-input", type=Path, default=SSR_INPUT)
    parser.add_argument("--panel-dir", type=Path, default=PANEL_DIR)
    parser.add_argument("--event-dir", type=Path, default=EVENT_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--late", type=int, default=RUN_CONFIG["late"])
    parser.add_argument(
        "--req1-spec",
        "--req-spec",
        choices=("new", "origin"),
        default=RUN_CONFIG["req1_spec"],
    )
    parser.add_argument(
        "--stay-spec", choices=("new", "origin"), default=RUN_CONFIG["stay_spec"]
    )
    parser.add_argument(
        "--share-time-spec",
        choices=("new", "origin"),
        default=RUN_CONFIG["share_time_spec"],
    )
    parser.add_argument(
        "--all-configurations",
        action="store_true",
        help="Run all 16 settings for both variants.",
    )
    args = parser.parse_args()
    if args.late < 0:
        parser.error("--late must be non-negative")
    specifications = (
        [
            Specification(*values)
            for values in product(
                (0, 3), ("new", "origin"), ("new", "origin"), ("new", "origin")
            )
        ]
        if args.all_configurations
        else [
            Specification(
                args.late, args.req1_spec, args.stay_spec, args.share_time_spec
            )
        ]
    )
    preflight(args, specifications)
    indices = load_atc3_indices(args.ssr_input)
    total_cohorts = 0
    for spec in specifications:
        for variant in VARIANTS:
            maker = CohortPanelMaker(
                spec, variant, args.panel_dir, args.event_dir, indices
            )
            cohorts = maker.run(spec.output_dir(args.output_dir, variant))
            total_cohorts += cohorts
            print(
                f"{variant} | late={spec.late} req1={spec.req1_spec} stay={spec.stay_spec} "
                f"share_time={spec.share_time_spec}: {cohorts} cohort CSVs",
                flush=True,
            )
    print(f"Finished: {total_cohorts} cohort CSVs.", flush=True)


if __name__ == "__main__":
    main()
