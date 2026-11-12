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
       the current 1.1 req2 definition. With dynamic_share=0, match the treated
       product's ATC3 at T against every partner's full SSR portfolio at T
       (new), or pooled across T-2 to T+1 (origin). With dynamic_share=1,
       match both portfolios in each observation quarter using the same partners.
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
        stay_<stay_spec>/share_time_<share_time_spec>[_ds1]/
        quarter-level_<side>_without_<other>/event/req2/Not/
        <event>_quarter_cohort_<year>q<quarter>_balanced.csv
    Include 2009Q1-2018Q4; skip cohorts without balanced treated rows or without
    upstream event/balance columns. The optional dissolution restriction writes
    under dai1 and applies after constructing the original treated and control
    groups. The earlier same-pair still event must satisfy req2 and occur in
    2009Q1 or later.
    Only cohort panel CSVs are generated;
    no distribution tables, figures, or first_event cohort files are produced.
    --all-configurations runs late={0,3}, req1_spec={new,origin}, and
    stay_spec={new,origin,pre2}.
    It runs both share_time_spec values in static mode and only new in dynamic
    mode. Each setting covers three events and A/B for the selected variants.
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


# ========================== USER CONFIG ==========================
# Select membership policies.
# Input/output roots and the existing statistical settings are configured below.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SSR_INPUT = PROJECT_ROOT / "InterimData" / "boardex_ssr_price_sample.csv"
PANEL_DIR = PROJECT_ROOT / "data" / "ssrquarter" / "firm_panels"
EVENT_DIR = PROJECT_ROOT / "data" / "ssrquarter" / "event_tables"
OUTPUT_DIR = PROJECT_ROOT / "data" / "ssrquarter" / "cohort_data"
# Select roster variants for a no-argument run; --variant selects one explicitly.
VARIANTS = ("drop_sentinel_yearquarter",)
RUN_CONFIG = {
    "ever_board": 1,  # 0: legacy roster, 1: all-role continuity.
    "ieoc_ref": 1,  # 0: baseline BP expansion, 1: use IE/OC coverage.
    # Nonnegative years used by upstream event files.
    "late": 3,
    # Upstream req1 rule: new or origin. Upstream stay: new, origin, or pre2.
    "req1_spec": "new",
    "stay_spec": "new",
    # Static sharing partner portfolios: new uses T, origin pools T-2 to T+1.
    "share_time_spec": "new",
    # 0: static sharing. 1: update sharing every quarter, requires new.
    "dynamic_share": 0,
    # 1: require an earlier req2-qualified still event for the same pair since 2009Q1.
    "dissolution_after_interlock": 1,
}
# Fixed cohort definitions and observation range.
STAY_SPECS = ("new", "origin", "pre2")
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
COHORT_START_YEAR = 2009
PERIODS = tuple(
    (year, quarter) for year in range(COHORT_START_YEAR, 2019) for quarter in range(1, 5)
)
ID_COLUMNS = ["BoardName", "product"]
DISSOLUTION_EVENT = "interlock_dissolution"
# ================================================================


@dataclass(frozen=True)
class Specification:
    """Select one upstream setting and one ATC portfolio-time rule."""

    late: int
    req1_spec: str
    stay_spec: str
    share_time_spec: str
    dynamic_share: int = 0
    dissolution_after_interlock: int = 0

    @property
    def output_events(self) -> tuple[str, ...]:
        return (DISSOLUTION_EVENT,) if self.dissolution_after_interlock else tuple(EVENT_CONFIGS)

    def upstream_dir(self, root: Path, variant: str) -> Path:
        return (
            root
            / variant
            / f"late_{self.late}"
            / f"req1_{self.req1_spec}"
            / f"stay_{self.stay_spec}"
        )

    def output_dir(self, root: Path, variant: str) -> Path:
        suffix = "_ds1" if self.dynamic_share else ""
        path = self.upstream_dir(root, variant) / f"share_time_{self.share_time_spec}{suffix}"
        return path / "dai1" if self.dissolution_after_interlock else path


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
        self.dissolution_partners: dict[str, dict[int, dict[str, set[str]]]] = {}

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
        partners: dict[tuple[int, str], set[str]] | None = None,
    ) -> pd.DataFrame:
        """Label treated products using fixed event partners and the selected time rule."""
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
        if partners is None:
            partners = self.partner_index(event, side)
        result["_cohort_firm_key"] = result["BoardName"].map(normalize_key)
        result["_product_key"] = result["product"].map(normalize_key)
        treated = result[event_col].eq(1) & result["_cohort_firm_key"].isin(treated_keys)
        result["_sharing_time"] = (
            result["_time_id"] if self.specification.dynamic_share else cohort_time
        )
        source_rows = treated
        if not self.specification.dynamic_share:
            source_rows = treated & result["_time_id"].eq(cohort_time)
        match_columns = ["_sharing_time", "_cohort_firm_key", "_product_key"]
        products = result.loc[source_rows, match_columns].dropna().drop_duplicates()
        sharing_keys: set[tuple[int, str, str]] = set()
        for period, firm_key, product_key in products.itertuples(index=False, name=None):
            own_classes = self.product_atc3.get(
                (period, firm_key, product_key), set()
            )
            partner_periods = (
                (period,)
                if self.specification.share_time_spec == "new"
                else range(cohort_time - 2, cohort_time + 2)
            )
            partner_classes: set[str] = set()
            for partner in partners.get((cohort_time, firm_key), set()):
                for partner_period in partner_periods:
                    partner_classes.update(
                        self.partner_atc3.get((partner_period, partner), set())
                    )
            if own_classes.intersection(partner_classes):
                sharing_keys.add((period, firm_key, product_key))
        if sharing_keys:
            row_index = pd.MultiIndex.from_frame(result[match_columns])
            result.loc[
                result[event_col].eq(1) & row_index.isin(sharing_keys), "atc3_sharing"
            ] = 1
        return result.drop(columns=match_columns)

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

    def restricted_partners(self, side: str) -> dict[int, dict[str, set[str]]]:
        """Index qualified partners with a prior req2 still event since 2009Q1."""
        if side in self.dissolution_partners:
            return self.dissolution_partners[side]
        first_still: dict[tuple[str, ...], int] = {}
        still = self.candidates.loc[
            self.candidates["event_type"].eq("to_B_still_in_A")
            & self.candidates["FirmA_event_time_id"].ge(COHORT_START_YEAR * 4)
            & self.candidates[self.stay_column].eq(1)
            & self.candidates["requirement1_A"].eq(1)
            & self.candidates["requirement1_B"].eq(1),
            ["FirmA", "FirmB", "FirmA_event_time_id"],
        ].dropna()
        for a, b, time in still.itertuples(index=False, name=None):
            pair = tuple(sorted((a, b)))
            first_still[pair] = min(time, first_still.get(pair, time))

        result: dict[int, dict[str, set[str]]] = {}
        other = "B" if side == "A" else "A"
        candidates = self.eligible_candidates(DISSOLUTION_EVENT, side)
        columns = [f"Firm{side}", f"Firm{other}", f"Firm{side}_event_time_id"]
        for firm, partner, time in candidates[columns].dropna().itertuples(index=False, name=None):
            prior = first_still.get(tuple(sorted((firm, partner))))
            if prior is not None and prior < time:
                result.setdefault(time, {}).setdefault(firm, set()).add(partner)
        self.dissolution_partners[side] = result
        return result

    def restrict_dissolution_cohort(
        self, cohort: pd.DataFrame, side: str, year: int, quarter: int,
    ) -> pd.DataFrame:
        """Drop noneligible treated windows and update sharing for retained treatments."""
        cohort_time = year * 4 + quarter - 1
        event_col = f"event_{year}q{quarter}"
        partners = self.restricted_partners(side).get(cohort_time, {})
        treated = cohort[event_col].eq(1)
        retained_treated = treated & cohort["BoardName"].isin(partners)
        result = cohort.loc[~treated | retained_treated].copy()
        if retained_treated.any():
            sharing_partners = {
                (cohort_time, normalize_key(firm)): {normalize_key(partner) for partner in others}
                for firm, others in partners.items()
            }
            sharing = self.add_atc3_sharing(
                cohort.loc[retained_treated], DISSOLUTION_EVENT, side, cohort_time,
                event_col, partners=sharing_partners,
            )
            result.loc[sharing.index, "atc3_sharing"] = sharing["atc3_sharing"]
        return result

    def run(self, output_root: Path) -> int:
        """Write only cohort panels, preserving the original cohort loop order."""
        cohort_count = 0
        for side in TREATMENT_GROUPS:
            for event in self.specification.output_events:
                frame = self.load_panel(event, side)
                if self.specification.dissolution_after_interlock:
                    year, quarter = PERIODS[0]
                    cohort_path(output_root, side, event, year, quarter).parent.mkdir(
                        parents=True, exist_ok=True,
                    )
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
                    if self.specification.dissolution_after_interlock:
                        cohort = self.restrict_dissolution_cohort(cohort, side, year, quarter)
                    if not cohort[f"event_{label}"].eq(1).any():
                        continue
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
        for variant in args.variants:
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
                for event in spec.output_events
                for year, quarter in PERIODS
            ]
            for path in outputs:
                if path.exists():
                    raise FileExistsError(
                        f"Output already exists; refusing to overwrite: {path}"
                    )


def main() -> None:
    """Run the selected settings without overwriting existing cohort files."""
    parser = argparse.ArgumentParser(
        description="Build quarterly event/req2/Not SSR cohorts."
    )
    parser.add_argument("--ssr-input", type=Path, default=SSR_INPUT)
    parser.add_argument("--panel-dir", type=Path, default=PANEL_DIR)
    parser.add_argument("--event-dir", type=Path, default=EVENT_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument(
        "--variant", choices=("imputed_yearquarter", "drop_sentinel_yearquarter"),
        help="Run just one roster variant.",
    )
    parser.add_argument("--late", type=int, default=RUN_CONFIG["late"])
    parser.add_argument(
        "--req1-spec",
        "--req-spec",
        choices=("new", "origin"),
        default=RUN_CONFIG["req1_spec"],
    )
    parser.add_argument(
        "--stay-spec", choices=STAY_SPECS, default=RUN_CONFIG["stay_spec"]
    )
    parser.add_argument(
        "--share-time-spec",
        choices=("new", "origin"),
        default=RUN_CONFIG["share_time_spec"],
    )
    parser.add_argument(
        "--dynamic-share", "--dynamic_share", type=int, choices=(0, 1),
        default=RUN_CONFIG["dynamic_share"],
        help="Use each observation quarter for ATC3 overlap (requires share-time-spec=new).",
    )
    parser.add_argument(
        "--dissolution-after-interlock", type=int, choices=(0, 1),
        default=RUN_CONFIG["dissolution_after_interlock"],
    )
    parser.add_argument(
        "--all-configurations",
        action="store_true",
        help="Run all 16 static settings or all 8 dynamic settings per selected variant.",
    )
    from ever_board_config import (
        membership_path,
        validate_policies,
    )

    parser.add_argument("--ever-board", "--ever_board", type=int, choices=(0, 1), default=RUN_CONFIG["ever_board"])
    parser.add_argument("--ieoc-ref", "--ieoc_ref", type=int, choices=(0, 1), default=RUN_CONFIG["ieoc_ref"])
    args = parser.parse_args()
    args.variants = (args.variant,) if args.variant else VARIANTS
    try:
        validate_policies(args.ever_board, args.ieoc_ref)
    except ValueError as error:
        parser.error(str(error))
    args.panel_dir = membership_path(args.panel_dir, args.ever_board, args.ieoc_ref)
    args.event_dir = membership_path(args.event_dir, args.ever_board, args.ieoc_ref)
    args.output_dir = membership_path(args.output_dir, args.ever_board, args.ieoc_ref)
    if args.late < 0:
        parser.error("--late must be non-negative")
    if args.dynamic_share and args.share_time_spec != "new":
        parser.error("--dynamic-share 1 requires --share-time-spec new")
    share_time_specs = ("new",) if args.dynamic_share else ("new", "origin")
    specifications = (
        [
            Specification(
                *values, dynamic_share=args.dynamic_share,
                dissolution_after_interlock=args.dissolution_after_interlock,
            )
            for values in product(
                (0, 3), ("new", "origin"), STAY_SPECS, share_time_specs
            )
        ]
        if args.all_configurations
        else [
            Specification(
                args.late, args.req1_spec, args.stay_spec, args.share_time_spec,
                args.dynamic_share,
                args.dissolution_after_interlock,
            )
        ]
    )
    preflight(args, specifications)
    indices = load_atc3_indices(args.ssr_input)
    total_cohorts = 0
    for spec in specifications:
        for variant in args.variants:
            maker = CohortPanelMaker(
                spec, variant, args.panel_dir, args.event_dir, indices
            )
            cohorts = maker.run(spec.output_dir(args.output_dir, variant))
            total_cohorts += cohorts
            print(
                f"{variant} | late={spec.late} req1={spec.req1_spec} stay={spec.stay_spec} "
                f"share_time={spec.share_time_spec} dynamic_share={spec.dynamic_share}: "
                f"{cohorts} cohort CSVs",
                flush=True,
            )
    print(f"Finished: {total_cohorts} cohort CSVs.", flush=True)


if __name__ == "__main__":
    main()
