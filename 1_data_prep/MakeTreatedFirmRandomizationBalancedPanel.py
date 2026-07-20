"""
Purpose:
Build side-specific, balanced stacked base panels for the joint treated-firm
and firm-pair randomization-inference design. The base contains every observed
firm-product window that can enter a 2009--2018 cohort before any pseudo-event
assignment; actual focal-event labels never determine inclusion here.

Process:
1. Read the complete quarter-level SSR panel for each treated side.
2. For each requested cohort year, retain firm-product windows with all twelve
   quarters from t-1 through t+1 and stack them with data_cohort=t.
3. Attach the fixed other-event timing controls used in the existing req1
   to_B_still_in_A regression specification.
4. Write one Stata-ready balanced base panel and compact cohort diagnostics per
   treated side.

Input:
- data/quarter-level_{A|B}/ssr_firm_panel_to_B_still_in_A_req1_large_sample_narrow.csv
- data/quarter-level_{A|B}/ssr_firm_panel_to_B_not_in_A_req1_large_sample_narrow.csv
- data/quarter-level_{A|B}/ssr_firm_panel_interlock_dissolution_leave_B_req1_large_sample_narrow.csv

Output:
- data/random_inference_treated_firm_pair/to_B_still_in_A/req1/
  large_sample_narrow/balanced_base_{A|B}.dta
- data/random_inference_treated_firm_pair/to_B_still_in_A/req1/
  large_sample_narrow/balanced_base_diagnostics.csv
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = PROJECT_ROOT / "data"
OUTPUT_ROOT = DATA_ROOT / "random_inference_treated_firm_pair"


# ========================== USER CONFIG ==========================
RUN_CONFIG = {
    "event": "to_B_still_in_A",
    "event_requirement": 1,
    "cohort_years": list(range(2009, 2019)),
    "treatment_groups": ["A", "B"],
    "large_sample": 1,
    "personnel_definition": "narrow",
    "stata_version": 118,
}
# ================================================================


@dataclass(frozen=True)
class BasePanelConfig:
    """Validated configuration for the balanced randomization base panel."""

    event: str
    event_requirement: int
    cohort_years: tuple[int, ...]
    treatment_groups: tuple[str, ...]
    large_sample: int
    personnel_definition: str
    stata_version: int


def make_config(raw: dict[str, object]) -> BasePanelConfig:
    """Validate user configuration and return an immutable representation."""
    event = str(raw["event"])
    event_requirement = int(raw["event_requirement"])
    large_sample = int(raw["large_sample"])
    treatment_groups = tuple(str(value).upper() for value in raw["treatment_groups"])
    cohort_years = tuple(int(value) for value in raw["cohort_years"])

    if event != "to_B_still_in_A":
        raise ValueError("This builder currently supports only to_B_still_in_A.")
    if event_requirement != 1:
        raise ValueError("This builder currently supports only req1.")
    if large_sample != 1:
        raise ValueError("This builder currently supports only the large SSR sample.")
    if set(treatment_groups) != {"A", "B"}:
        raise ValueError("treatment_groups must contain A and B exactly once.")
    if not cohort_years:
        raise ValueError("cohort_years cannot be empty.")

    return BasePanelConfig(
        event=event,
        event_requirement=event_requirement,
        cohort_years=cohort_years,
        treatment_groups=treatment_groups,
        large_sample=large_sample,
        personnel_definition=str(raw["personnel_definition"]),
        stata_version=int(raw["stata_version"]),
    )


# ---------------------- path and schema helpers ----------------------

def movement_suffix(config: BasePanelConfig) -> str:
    """Return the existing movement suffix for the configured SSR sample."""
    return f"_large_sample_{config.personnel_definition}"


def panel_path(event: str, treatment_group: str, config: BasePanelConfig) -> Path:
    """Return the complete quarter-level source panel for one event and side."""
    stem = "interlock_dissolution_leave_B" if event == "interlock_dissolution" else event
    return (
        DATA_ROOT
        / f"quarter-level_{treatment_group}"
        / f"ssr_firm_panel_{stem}_req{config.event_requirement}{movement_suffix(config)}.csv"
    )


def output_directory(config: BasePanelConfig) -> Path:
    """Return the shared output directory for the configured design."""
    return (
        OUTPUT_ROOT
        / config.event
        / f"req{config.event_requirement}"
        / f"large_sample_{config.personnel_definition}"
    )


def ensure_columns(frame: pd.DataFrame, required: Iterable[str], source: Path) -> None:
    """Raise a readable error when a source misses a required column."""
    missing = sorted(set(required) - set(frame.columns))
    if missing:
        raise KeyError(f"{source} is missing required columns: {missing}")


def stata_safe(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a Stata-ready frame with lowercase, unique variable names."""
    result = frame.copy()
    result.columns = [str(column).lower() for column in result.columns]
    duplicates = result.columns[result.columns.duplicated()].tolist()
    if duplicates:
        raise ValueError(f"Lowercasing created duplicate Stata variable names: {duplicates}")
    return result


# ---------------------- source and timing construction ----------------------

def load_source_panel(treatment_group: str, config: BasePanelConfig) -> pd.DataFrame:
    """Load and normalize the complete firm-level panel used as the base universe."""
    source = panel_path(config.event, treatment_group, config)
    panel = pd.read_csv(source)
    ensure_columns(panel, ["BoardName", "product", "year", "quarter", "atc3", "price"], source)

    panel["BoardName"] = panel["BoardName"].astype(str).str.strip().str.upper()
    panel["product"] = panel["product"].astype(str)
    panel["year"] = pd.to_numeric(panel["year"], errors="raise").astype(int)
    panel["quarter"] = pd.to_numeric(panel["quarter"], errors="raise").astype(int)

    key_columns = ["BoardName", "product", "year", "quarter"]
    if panel.duplicated(key_columns).any():
        examples = panel.loc[panel.duplicated(key_columns, keep=False), key_columns].head(10)
        raise ValueError(f"Duplicate firm-product-quarter observations in {source}:\n{examples}")
    return panel


def load_other_event_timing(
    event: str,
    treatment_group: str,
    config: BasePanelConfig,
) -> pd.MultiIndex:
    """Return Q1 firm-year timing for one fixed real other-event control."""
    source = panel_path(event, treatment_group, config)
    timing = pd.read_csv(source, usecols=["BoardName", "year", "event"])
    ensure_columns(timing, ["BoardName", "year", "event"], source)
    timing["BoardName"] = timing["BoardName"].astype(str).str.strip().str.upper()
    timing["year"] = pd.to_numeric(timing["year"], errors="raise").astype(int)
    timing["event"] = pd.to_numeric(timing["event"], errors="raise").astype("int8")
    event_rows = timing.loc[timing["event"].eq(1), ["BoardName", "year"]].drop_duplicates()
    return pd.MultiIndex.from_frame(event_rows)


def select_balanced_window(panel: pd.DataFrame, cohort_year: int) -> pd.DataFrame:
    """Return complete t-1..t+1 firm-product windows for one cohort year."""
    start_year = cohort_year - 1
    end_year = cohort_year + 1
    expected_periods = {
        (year, quarter)
        for year in range(start_year, end_year + 1)
        for quarter in range(1, 5)
    }
    window = panel.loc[panel["year"].between(start_year, end_year)].copy()
    period_sets = window.groupby(["BoardName", "product"], sort=False).apply(
        lambda group: set(zip(group["year"].astype(int), group["quarter"].astype(int)))
    )
    complete_index = period_sets.index[period_sets.eq(expected_periods)]
    if complete_index.empty:
        return window.iloc[0:0].copy()
    return window.loc[window.set_index(["BoardName", "product"]).index.isin(complete_index)].copy()


def attach_other_event_markers(
    window: pd.DataFrame,
    timing_by_column: dict[str, pd.MultiIndex],
) -> pd.DataFrame:
    """Attach fixed Q1 other-event markers used by the existing Stata specification."""
    result = window.copy()
    row_index = pd.MultiIndex.from_frame(result[["BoardName", "year"]])
    q1_mask = result["quarter"].eq(1)
    for column, timing_index in timing_by_column.items():
        result[column] = (q1_mask & row_index.isin(timing_index)).astype("int8")
    return result


def drop_actual_event_construction_columns(panel: pd.DataFrame) -> pd.DataFrame:
    """Remove labels that belong to the observed, rather than pseudo, event process."""
    generated_prefixes = ("event_", "balance_panel_")
    generated_exact = {"event", "pure_event", "stay_2_years", "first_event", "first_event_year"}
    drop_columns = [
        column
        for column in panel.columns
        if column in generated_exact or column.startswith(generated_prefixes)
    ]
    return panel.drop(columns=drop_columns, errors="ignore")


# ---------------------- side-panel build ----------------------

def build_side_base_panel(treatment_group: str, config: BasePanelConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build one balanced stacked base panel and one cohort-level diagnostic table."""
    source_panel = load_source_panel(treatment_group, config)
    timing_by_column = {
        "other_event_not": load_other_event_timing("to_B_not_in_A", treatment_group, config),
        "other_event_dissolution": load_other_event_timing(
            "interlock_dissolution", treatment_group, config
        ),
    }

    cohort_frames: list[pd.DataFrame] = []
    diagnostics: list[dict[str, int | str]] = []
    for cohort_year in config.cohort_years:
        balanced = select_balanced_window(source_panel, cohort_year)
        balanced = attach_other_event_markers(balanced, timing_by_column)
        balanced["data_cohort"] = np.int16(cohort_year)
        cohort_frames.append(balanced)
        diagnostics.append(
            {
                "side": treatment_group,
                "cohort_year": cohort_year,
                "firm_products": int(balanced[["BoardName", "product"]].drop_duplicates().shape[0]),
                "firms": int(balanced["BoardName"].nunique()),
                "observations": int(len(balanced)),
            }
        )

    stacked = pd.concat(cohort_frames, ignore_index=True)
    stacked = drop_actual_event_construction_columns(stacked)
    stacked = stacked.sort_values(["BoardName", "product", "data_cohort", "year", "quarter"]).reset_index(drop=True)
    return stata_safe(stacked), pd.DataFrame(diagnostics)


# ---------------------- main build ----------------------

def main() -> None:
    """Build balanced, side-specific Stata base panels for joint randomization inference."""
    config = make_config(RUN_CONFIG)
    destination = output_directory(config)
    destination.mkdir(parents=True, exist_ok=True)

    diagnostics: list[pd.DataFrame] = []
    for treatment_group in config.treatment_groups:
        base, side_diagnostics = build_side_base_panel(treatment_group, config)
        output_path = destination / f"balanced_base_{treatment_group}.dta"
        base.to_stata(output_path, write_index=False, version=config.stata_version)
        diagnostics.append(side_diagnostics)
        print(f"Saved {len(base):,} rows to {output_path}")

    diagnostic_path = destination / "balanced_base_diagnostics.csv"
    pd.concat(diagnostics, ignore_index=True).to_csv(diagnostic_path, index=False)
    print(f"Saved balanced-base diagnostics to {diagnostic_path}")


if __name__ == "__main__":
    main()
