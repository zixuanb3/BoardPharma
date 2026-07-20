"""
Purpose:
Create side-specific wide randomization-inference panels for a joint placebo
assignment of treated firms and firm pairs. Every replication first assigns
pseudo pure events across the complete BoardName roster, then preserves the
observed side-year joint distribution of req0 and req1 pair counts while
constructing random partners under the documented req1 interlock rule.

Process:
1. Read the balanced stacked base panels and the full SSR BoardName roster.
2. For every side, replication, and assignment year, randomly assign the
   observed number of pseudo pure-event firms and feasible (req0, req1) pair
   bundles; firms may be selected independently again in later years.
3. Draw req1-valid and req1-invalid partners separately, calculate ATC3 share,
   and convert the full pseudo-event schedule into cohort sample states.
4. Add one byte sample-state column and one byte share column per replication,
   then save full pre-req0 schedules, pair audits, and compact diagnostics.

Input:
- data/random_inference_treated_firm_pair/to_B_still_in_A/req1/
  large_sample_narrow/balanced_base_{A|B}.dta
- data/event_tables/movement_event_candidates_large_sample_narrow.csv
- data/event_tables/firm_interlock_panel_large_sample_narrow.csv
- InterimData/ssr_company_roster.csv
- InterimData/boardex_ssr_price_sample.csv

Output:
- data/random_inference_treated_firm_pair/to_B_still_in_A/req1/
  large_sample_narrow/treated_firm_pair_randomization_{A|B}.dta
- data/random_inference_treated_firm_pair/to_B_still_in_A/req1/
  large_sample_narrow/{A|B}_pseudo_event_assignments.csv
- data/random_inference_treated_firm_pair/to_B_still_in_A/req1/
  large_sample_narrow/{A|B}_pair_assignments.csv
- data/random_inference_treated_firm_pair/to_B_still_in_A/req1/
  large_sample_narrow/treated_firm_pair_randomization_diagnostics.csv
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
from tqdm.auto import tqdm


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = PROJECT_ROOT / "data"
INTERIM_DATA_DIR = PROJECT_ROOT / "InterimData"
EVENT_TABLES_DIR = DATA_ROOT / "event_tables"
OUTPUT_ROOT = DATA_ROOT / "random_inference_treated_firm_pair"


# ========================== USER CONFIG ==========================
RUN_CONFIG = {
    "event": "to_B_still_in_A",
    "event_requirement": 1,
    "cohort_years": list(range(2009, 2019)),
    "assignment_years": list(range(2008, 2020)),
    "treatment_groups": ["A", "B"],
    "large_sample": 1,
    "personnel_definition": "narrow",
    "n_permutations": 1000,
    "base_seed": 20260714,
    "max_assignment_attempts": 10_000,
    "stata_version": 118,
}
# ================================================================


@dataclass(frozen=True)
class RandomizationConfig:
    """Validated configuration for one joint treated-firm randomization build."""

    event: str
    event_requirement: int
    cohort_years: tuple[int, ...]
    assignment_years: tuple[int, ...]
    treatment_groups: tuple[str, ...]
    large_sample: int
    personnel_definition: str
    n_permutations: int
    base_seed: int
    max_assignment_attempts: int
    stata_version: int


def make_config(raw: dict[str, object]) -> RandomizationConfig:
    """Validate user configuration and return an immutable representation."""
    cohort_years = tuple(int(value) for value in raw["cohort_years"])
    assignment_years = tuple(int(value) for value in raw["assignment_years"])
    treatment_groups = tuple(str(value).upper() for value in raw["treatment_groups"])

    if str(raw["event"]) != "to_B_still_in_A":
        raise ValueError("This implementation currently supports only to_B_still_in_A.")
    if int(raw["event_requirement"]) != 1:
        raise ValueError("This implementation currently supports only req1.")
    if int(raw["large_sample"]) != 1:
        raise ValueError("This implementation currently supports only the large SSR sample.")
    if set(treatment_groups) != {"A", "B"}:
        raise ValueError("treatment_groups must contain A and B exactly once.")
    if not cohort_years or not assignment_years:
        raise ValueError("cohort_years and assignment_years cannot be empty.")
    if not set(range(min(cohort_years) - 1, max(cohort_years) + 2)).issubset(assignment_years):
        raise ValueError("assignment_years must cover t-1 through t+1 for every cohort year.")
    if int(raw["n_permutations"]) < 1:
        raise ValueError("n_permutations must be positive.")
    if int(raw["max_assignment_attempts"]) < 1:
        raise ValueError("max_assignment_attempts must be positive.")

    return RandomizationConfig(
        event=str(raw["event"]),
        event_requirement=int(raw["event_requirement"]),
        cohort_years=cohort_years,
        assignment_years=assignment_years,
        treatment_groups=treatment_groups,
        large_sample=int(raw["large_sample"]),
        personnel_definition=str(raw["personnel_definition"]),
        n_permutations=int(raw["n_permutations"]),
        base_seed=int(raw["base_seed"]),
        max_assignment_attempts=int(raw["max_assignment_attempts"]),
        stata_version=int(raw["stata_version"]),
    )


# ---------------------- path and key helpers ----------------------

def movement_suffix(config: RandomizationConfig) -> str:
    """Return the existing suffix used by current large-sample event files."""
    return f"_large_sample_{config.personnel_definition}"


def output_directory(config: RandomizationConfig) -> Path:
    """Return the shared output directory for this randomization design."""
    return (
        OUTPUT_ROOT
        / config.event
        / f"req{config.event_requirement}"
        / f"large_sample_{config.personnel_definition}"
    )


def firm_key(value: object) -> str:
    """Normalize one nonmissing firm name for cross-file comparisons."""
    return str(value).strip().upper()


def canonical_pair(first: str, second: str) -> tuple[str, str]:
    """Return the undirected pair key used by the interlock panel."""
    return tuple(sorted((first, second)))


def ensure_columns(frame: pd.DataFrame, required: Iterable[str], source: Path) -> None:
    """Raise a readable error when a source misses a required column."""
    missing = sorted(set(required) - set(frame.columns))
    if missing:
        raise KeyError(f"{source} is missing required columns: {missing}")


def stata_safe(frame: pd.DataFrame) -> pd.DataFrame:
    """Drop Python-only helpers and ensure Stata-safe lowercase variable names."""
    result = frame.drop(columns=["_firm_key", "_product_key"], errors="ignore").copy()
    result.columns = [str(column).lower() for column in result.columns]
    duplicates = result.columns[result.columns.duplicated()].tolist()
    if duplicates:
        raise ValueError(f"Lowercasing created duplicate Stata variable names: {duplicates}")
    return result


# ---------------------- observed-distribution loaders ----------------------

def load_ssr_roster() -> np.ndarray:
    """Load the fixed complete BoardName universe for pseudo-event assignment."""
    source = INTERIM_DATA_DIR / "ssr_company_roster.csv"
    roster = pd.read_csv(source, usecols=["BoardName"])
    universe = np.array(sorted(roster["BoardName"].dropna().map(firm_key).unique()), dtype=object)
    if len(universe) < 2:
        raise ValueError(f"SSR roster must contain at least two firms: {source}")
    return universe


def load_movement_candidates(config: RandomizationConfig) -> pd.DataFrame:
    """Load raw candidates used to recover observed pure-event and pair bundles."""
    source = EVENT_TABLES_DIR / f"movement_event_candidates{movement_suffix(config)}.csv"
    candidates = pd.read_csv(source)
    ensure_columns(
        candidates,
        ["event_type", "event_year", "FirmA", "FirmB", "stay_2_years", "requirement1"],
        source,
    )
    candidates = candidates.dropna(subset=["event_type", "event_year", "FirmA", "FirmB"]).copy()
    candidates["event_year"] = pd.to_numeric(candidates["event_year"], errors="raise").astype(int)
    candidates["stay_2_years"] = pd.to_numeric(candidates["stay_2_years"], errors="raise").astype("int8")
    candidates["requirement1"] = pd.to_numeric(candidates["requirement1"], errors="raise").astype("int8")
    candidates["FirmAKey"] = candidates["FirmA"].map(firm_key)
    candidates["FirmBKey"] = candidates["FirmB"].map(firm_key)
    return candidates


def load_interlock_lookup() -> dict[tuple[int, str], set[str]]:
    """Load the actual t-1 interlock sets used by the req1 definition."""
    source = EVENT_TABLES_DIR / "firm_interlock_panel_large_sample_narrow.csv"
    interlocks = pd.read_csv(source, usecols=["BoardName", "CounterpartBoard", "year"])
    ensure_columns(interlocks, ["BoardName", "CounterpartBoard", "year"], source)
    interlocks = interlocks.dropna(subset=["BoardName", "CounterpartBoard", "year"]).copy()
    interlocks["year"] = pd.to_numeric(interlocks["year"], errors="raise").astype(int)
    interlocks["BoardName"] = interlocks["BoardName"].map(firm_key)
    interlocks["CounterpartBoard"] = interlocks["CounterpartBoard"].map(firm_key)

    lookup: dict[tuple[int, str], set[str]] = defaultdict(set)
    for board, counterpart, year in interlocks.loc[
        :, ["BoardName", "CounterpartBoard", "year"]
    ].itertuples(index=False, name=None):
        lookup[(int(year), str(board))].add(str(counterpart))
        lookup[(int(year), str(counterpart))].add(str(board))
    return dict(lookup)


def load_partner_atcs() -> dict[tuple[int, str], set[str]]:
    """Return event-year ATC3 sets for firms observed in the SSR product panel."""
    source = INTERIM_DATA_DIR / "boardex_ssr_price_sample.csv"
    ssr = pd.read_csv(source, usecols=["BoardName", "year", "atc3"])
    ensure_columns(ssr, ["BoardName", "year", "atc3"], source)
    ssr = ssr.dropna(subset=["BoardName", "year", "atc3"]).copy()
    ssr["year"] = pd.to_numeric(ssr["year"], errors="raise").astype(int)
    ssr["_firm_key"] = ssr["BoardName"].map(firm_key)
    ssr["atc3"] = ssr["atc3"].astype(str)
    grouped = ssr.groupby(["year", "_firm_key"], sort=False)["atc3"].agg(set)
    return {(int(year), firm): set(atcs) for (year, firm), atcs in grouped.items()}


def side_observed_assignment(
    candidates: pd.DataFrame,
    treatment_group: str,
    assignment_year: int,
    config: RandomizationConfig,
    interlock_lookup: dict[tuple[int, str], set[str]],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return observed pure-event bundles and exact req0 pairs for one side-year."""
    current = candidates.loc[
        candidates["event_type"].eq(config.event) & candidates["event_year"].eq(assignment_year)
    ].copy()
    focal_column = "FirmAKey" if treatment_group == "A" else "FirmBKey"
    partner_column = "FirmBKey" if treatment_group == "A" else "FirmAKey"
    pure_firms = sorted(current[focal_column].unique().tolist())
    bundles = pd.DataFrame({"focal": pure_firms})
    if bundles.empty:
        empty_pairs = pd.DataFrame(columns=["focal", "partner", "req1"])
        return pd.DataFrame(columns=["focal", "d0", "d1"]), empty_pairs

    req0_pairs = current.loc[current["stay_2_years"].eq(1), [focal_column, partner_column, "requirement1"]]
    req0_pairs = req0_pairs.rename(columns={focal_column: "focal", partner_column: "partner"})
    req0_pairs = (
        req0_pairs.groupby(["focal", "partner"], as_index=False, sort=False)["requirement1"]
        .max()
        .reset_index(drop=True)
    )
    req0_pairs = req0_pairs.rename(columns={"requirement1": "req1"})
    recomputed_req1 = req0_pairs.apply(
        lambda row: int(str(row["partner"]) not in interlock_lookup.get((assignment_year - 1, str(row["focal"])), set())),
        axis=1,
    )
    mismatch = req0_pairs.loc[recomputed_req1.ne(req0_pairs["req1"])]
    if not mismatch.empty:
        raise AssertionError(
            "Observed requirement1 does not match the t-1 interlock definition for "
            f"{treatment_group}/{assignment_year}. Examples:\n{mismatch.head(10)}"
        )
    counts = req0_pairs.groupby("focal", as_index=False).agg(
        d0=("partner", "size"),
        d1=("req1", "sum"),
    )
    bundles = bundles.merge(counts, on="focal", how="left")
    bundles[["d0", "d1"]] = bundles[["d0", "d1"]].fillna(0).astype(int)
    bundles = bundles[["focal", "d0", "d1"]].copy()
    if bundles["d1"].gt(bundles["d0"]).any() or bundles[["d0", "d1"]].lt(0).any().any():
        raise AssertionError(f"Invalid observed req0/req1 bundle for {treatment_group}/{assignment_year}.")
    return bundles, req0_pairs


# ---------------------- random assignment and pair construction ----------------------

def partner_pools(
    focal: str,
    event_year: int,
    universe: np.ndarray,
    interlock_lookup: dict[tuple[int, str], set[str]],
) -> tuple[np.ndarray, np.ndarray]:
    """Return req1-valid and req1-invalid partner pools for one pseudo focal firm."""
    all_other_firms = universe[universe != focal]
    interlocked = interlock_lookup.get((event_year - 1, focal), set())
    invalid = np.array(sorted(set(all_other_firms.tolist()) & interlocked), dtype=object)
    valid = np.array(sorted(set(all_other_firms.tolist()) - interlocked), dtype=object)
    return valid, invalid


def draw_feasible_event_bundle_assignment(
    bundles: pd.DataFrame,
    assignment_year: int,
    universe: np.ndarray,
    interlock_lookup: dict[tuple[int, str], set[str]],
    rng: np.random.Generator,
    max_attempts: int,
) -> tuple[pd.DataFrame, int]:
    """Draw pseudo pure-event firms and randomly attach feasible observed bundles."""
    event_count = len(bundles)
    if event_count == 0:
        return pd.DataFrame(columns=["focal", "d0", "d1"]), 1
    if event_count > len(universe):
        raise ValueError(f"Cannot draw {event_count} event firms from a roster of {len(universe)} firms.")

    bundle_values = bundles[["d0", "d1"]].to_numpy(dtype=int)
    for attempt in range(1, max_attempts + 1):
        focal_firms = rng.choice(universe, size=event_count, replace=False)
        shuffled_bundles = bundle_values[rng.permutation(event_count)]
        feasible = True
        for focal, (d0, d1) in zip(focal_firms.tolist(), shuffled_bundles.tolist()):
            valid, invalid = partner_pools(str(focal), assignment_year, universe, interlock_lookup)
            if int(d1) > len(valid) or int(d0 - d1) > len(invalid):
                feasible = False
                break
        if feasible:
            assignment = pd.DataFrame(
                {
                    "focal": focal_firms.astype(object),
                    "d0": shuffled_bundles[:, 0].astype(int),
                    "d1": shuffled_bundles[:, 1].astype(int),
                }
            )
            return assignment, attempt

    raise RuntimeError(
        "No feasible pseudo-event/bundle assignment was found after "
        f"{max_attempts:,} draws for year {assignment_year}. "
        "The observed req1-invalid pair distribution is incompatible with the "
        "uniform roster draw under the actual t-1 interlock capacities."
    )


def draw_pairs_for_assignment(
    assignment: pd.DataFrame,
    event_year: int,
    universe: np.ndarray,
    interlock_lookup: dict[tuple[int, str], set[str]],
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Draw the exact req1-valid and req1-invalid pair counts in each assigned bundle."""
    rows: list[tuple[str, str, int]] = []
    for focal, d0, d1 in assignment.itertuples(index=False, name=None):
        if int(d0) == 0:
            continue
        valid, invalid = partner_pools(str(focal), event_year, universe, interlock_lookup)
        valid_partners = rng.choice(valid, size=int(d1), replace=False)
        invalid_partners = rng.choice(invalid, size=int(d0 - d1), replace=False)
        rows.extend((str(focal), str(partner), 1) for partner in valid_partners.tolist())
        rows.extend((str(focal), str(partner), 0) for partner in invalid_partners.tolist())
    return pd.DataFrame(rows, columns=["focal", "partner", "req1"])


def partner_atcs_by_focal(
    pairs: pd.DataFrame,
    event_year: int,
    partner_atcs: dict[tuple[int, str], set[str]],
) -> dict[str, set[str]]:
    """Union the event-year ATC3 sets of req1-valid random partners by focal firm."""
    atcs_by_focal: dict[str, set[str]] = defaultdict(set)
    for focal, partner in pairs.loc[pairs["req1"].eq(1), ["focal", "partner"]].itertuples(index=False, name=None):
        atcs_by_focal[str(focal)].update(partner_atcs.get((event_year, str(partner)), set()))
    return dict(atcs_by_focal)


# ---------------------- base-panel exposure construction ----------------------

def load_base_panel(treatment_group: str, config: RandomizationConfig) -> pd.DataFrame:
    """Load one balanced stacked base panel and add Python-only matching keys."""
    source = output_directory(config) / f"balanced_base_{treatment_group}.dta"
    base = pd.read_stata(source, convert_categoricals=False)
    ensure_columns(
        base,
        ["boardname", "product", "year", "quarter", "data_cohort", "atc3", "price", "other_event_not", "other_event_dissolution"],
        source,
    )
    base["boardname"] = base["boardname"].astype(str).str.strip().str.upper()
    base["product"] = base["product"].astype(str)
    for column in ("year", "quarter", "data_cohort"):
        base[column] = pd.to_numeric(base[column], errors="raise").astype(int)
    base["_firm_key"] = base["boardname"].map(firm_key)
    base["_product_key"] = base["product"].astype(str)
    required_cohorts = set(config.cohort_years)
    found_cohorts = set(base["data_cohort"].unique().tolist())
    if found_cohorts != required_cohorts:
        raise ValueError(f"{source} has cohort years {sorted(found_cohorts)}, expected {sorted(required_cohorts)}.")
    return base.sort_values(["data_cohort", "_firm_key", "_product_key", "year", "quarter"]).reset_index(drop=True)


def treated_product_atcs(base: pd.DataFrame, cohort_year: int, treated_firms: set[str]) -> pd.DataFrame:
    """Return event-year product ATC3 values for pseudo-treated firms with panel rows."""
    products = base.loc[
        base["data_cohort"].eq(cohort_year)
        & base["year"].eq(cohort_year)
        & base["quarter"].eq(1)
        & base["_firm_key"].isin(treated_firms),
        ["_firm_key", "_product_key", "atc3"],
    ].dropna(subset=["atc3"])
    products["atc3"] = products["atc3"].astype(str)
    duplicates = products.duplicated(["_firm_key", "_product_key"], keep=False)
    if duplicates.any():
        raise ValueError(
            "A pseudo-treated firm-product has multiple event-year ATC3 values. "
            f"Examples:\n{products.loc[duplicates].head(10)}"
        )
    return products.drop_duplicates()


def cohort_state_and_share(
    base: pd.DataFrame,
    cohort_year: int,
    pure_event_schedule: dict[int, set[str]],
    pairs: pd.DataFrame,
    partner_atcs: dict[tuple[int, str], set[str]],
) -> tuple[np.ndarray, np.ndarray, dict[str, int]]:
    """Create state/share vectors for one cohort from the full pseudo-event schedule."""
    row_mask = base["data_cohort"].eq(cohort_year)
    firm_values = base.loc[row_mask, "_firm_key"].to_numpy(dtype=object)
    product_values = base.loc[row_mask, "_product_key"].to_numpy(dtype=object)

    req1_pairs = pairs.loc[pairs["req1"].eq(1)].copy()
    treated_firms = set(req1_pairs["focal"].tolist())
    onlypair_firms = set(req1_pairs["partner"].tolist()) - treated_firms
    blocked_control_firms = set().union(
        pure_event_schedule.get(cohort_year - 1, set()),
        pure_event_schedule.get(cohort_year, set()),
        pure_event_schedule.get(cohort_year + 1, set()),
    )

    state = np.zeros(len(firm_values), dtype=np.int8)
    clean_control = np.fromiter(
        (firm not in blocked_control_firms for firm in firm_values), dtype=bool, count=len(firm_values)
    )
    state[clean_control] = 1
    treated_mask = np.fromiter((firm in treated_firms for firm in firm_values), dtype=bool, count=len(firm_values))
    state[treated_mask] = 2
    onlypair_mask = np.fromiter((firm in onlypair_firms for firm in firm_values), dtype=bool, count=len(firm_values))
    state[(state == 1) & onlypair_mask] = 3

    product_atcs = treated_product_atcs(base, cohort_year, treated_firms)
    atcs_by_focal = partner_atcs_by_focal(req1_pairs, cohort_year, partner_atcs)
    share_keys = {
        (firm, product)
        for firm, product, atc3 in product_atcs.itertuples(index=False, name=None)
        if atc3 in atcs_by_focal.get(firm, set())
    }
    share = np.fromiter(
        ((firm, product) in share_keys for firm, product in zip(firm_values, product_values)),
        dtype=np.int8,
        count=len(firm_values),
    )
    share[state != 2] = 0

    diagnostics = {
        "treated_firms_assigned": len(treated_firms),
        "onlypair_firms_assigned": len(onlypair_firms),
        "share_products_with_rows": len(share_keys),
        "base_rows_in_sample_before_onlypair": int((state > 0).sum()),
        "base_rows_dropped_onlypair": int((state == 3).sum()),
    }
    return state, share, diagnostics


# ---------------------- audit and side-panel build ----------------------

def append_csv(frame: pd.DataFrame, path: Path, include_header: bool) -> None:
    """Append one audit chunk while writing the header exactly once."""
    frame.to_csv(path, index=False, mode="a", header=include_header)


def build_side_panel(
    treatment_group: str,
    side_index: int,
    config: RandomizationConfig,
    candidates: pd.DataFrame,
    universe: np.ndarray,
    interlock_lookup: dict[tuple[int, str], set[str]],
    partner_atcs: dict[tuple[int, str], set[str]],
    destination: Path,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build one wide side panel and write its pre-req0 schedule and pair audits."""
    base = load_base_panel(treatment_group, config)
    observed_by_year = {
        year: side_observed_assignment(
            candidates,
            treatment_group,
            year,
            config,
            interlock_lookup,
        )
        for year in config.assignment_years
    }
    bundles_by_year = {
        year: assignment[["d0", "d1"]].copy()
        for year, (assignment, _pairs) in observed_by_year.items()
    }
    base_index_by_cohort = {
        year: np.flatnonzero(base["data_cohort"].eq(year).to_numpy()) for year in config.cohort_years
    }
    schedule_path = destination / f"{treatment_group}_pseudo_event_assignments.csv"
    pair_path = destination / f"{treatment_group}_pair_assignments.csv"
    schedule_path.unlink(missing_ok=True)
    pair_path.unlink(missing_ok=True)

    observed_pure_schedule = {
        year: set(assignment["focal"].tolist())
        for year, (assignment, _pairs) in observed_by_year.items()
    }
    observed_state = np.zeros(len(base), dtype=np.int8)
    observed_share = np.zeros(len(base), dtype=np.int8)
    for cohort_year in config.cohort_years:
        _assignment, observed_pairs = observed_by_year[cohort_year]
        state, share, _diagnostics = cohort_state_and_share(
            base=base,
            cohort_year=cohort_year,
            pure_event_schedule=observed_pure_schedule,
            pairs=observed_pairs,
            partner_atcs=partner_atcs,
        )
        row_index = base_index_by_cohort[cohort_year]
        observed_state[row_index] = state
        observed_share[row_index] = share

    wide_columns: dict[str, np.ndarray] = {
        "sample_state_obs": observed_state,
        "share_obs": observed_share,
    }
    diagnostics: list[dict[str, int | str]] = []
    schedule_header = True
    pair_header = True

    for rep in tqdm(
        range(1, config.n_permutations + 1),
        desc=f"Randomizing treated firms, side {treatment_group}",
        unit="rep",
        dynamic_ncols=True,
    ):
        seed = config.base_seed + side_index * 1_000_000 + rep
        rng = np.random.default_rng(seed)
        assignments_by_year: dict[int, pd.DataFrame] = {}
        pairs_by_year: dict[int, pd.DataFrame] = {}
        pure_event_schedule: dict[int, set[str]] = {}
        schedule_chunks: list[pd.DataFrame] = []
        pair_chunks: list[pd.DataFrame] = []

        for assignment_year in config.assignment_years:
            assignment, attempts = draw_feasible_event_bundle_assignment(
                bundles=bundles_by_year[assignment_year],
                assignment_year=assignment_year,
                universe=universe,
                interlock_lookup=interlock_lookup,
                rng=rng,
                max_attempts=config.max_assignment_attempts,
            )
            assignments_by_year[assignment_year] = assignment
            pure_event_schedule[assignment_year] = set(assignment["focal"].tolist())
            schedule_chunks.append(
                assignment.assign(
                    rep=rep,
                    seed=seed,
                    side=treatment_group,
                    assignment_year=assignment_year,
                    draw_attempts=attempts,
                    pseudo_pure_event=1,
                    pseudo_req0=lambda frame: frame["d0"].gt(0).astype("int8"),
                )[[
                    "rep",
                    "seed",
                    "side",
                    "assignment_year",
                    "focal",
                    "pseudo_pure_event",
                    "pseudo_req0",
                    "d0",
                    "d1",
                    "draw_attempts",
                ]]
            )
            if assignment_year in config.cohort_years:
                pairs = draw_pairs_for_assignment(
                    assignment=assignment,
                    event_year=assignment_year,
                    universe=universe,
                    interlock_lookup=interlock_lookup,
                    rng=rng,
                )
                pairs_by_year[assignment_year] = pairs
                pair_chunks.append(
                    pairs.assign(
                        rep=rep,
                        seed=seed,
                        side=treatment_group,
                        cohort_year=assignment_year,
                    )[["rep", "seed", "side", "cohort_year", "focal", "partner", "req1"]]
                )

        append_csv(pd.concat(schedule_chunks, ignore_index=True), schedule_path, schedule_header)
        schedule_header = False
        if pair_chunks:
            append_csv(pd.concat(pair_chunks, ignore_index=True), pair_path, pair_header)
            pair_header = False

        state_column = np.zeros(len(base), dtype=np.int8)
        share_column = np.zeros(len(base), dtype=np.int8)
        rep_diagnostics: list[dict[str, int | str]] = []
        for cohort_year in config.cohort_years:
            state, share, cohort_diagnostics = cohort_state_and_share(
                base=base,
                cohort_year=cohort_year,
                pure_event_schedule=pure_event_schedule,
                pairs=pairs_by_year[cohort_year],
                partner_atcs=partner_atcs,
            )
            row_index = base_index_by_cohort[cohort_year]
            state_column[row_index] = state
            share_column[row_index] = share
            assignment = assignments_by_year[cohort_year]
            pairs = pairs_by_year[cohort_year]
            rep_diagnostics.append(
                {
                    "rep": rep,
                    "seed": seed,
                    "side": treatment_group,
                    "cohort_year": cohort_year,
                    "pseudo_pure_event_firms": len(assignment),
                    "pseudo_req0_firms": int(assignment["d0"].gt(0).sum()),
                    "pseudo_req1_firms": int(assignment["d1"].gt(0).sum()),
                    "req0_pairs": int(assignment["d0"].sum()),
                    "req1_pairs": int(assignment["d1"].sum()),
                    "random_pairs_drawn": len(pairs),
                    **cohort_diagnostics,
                }
            )

        rep_tag = f"{rep:04d}"
        wide_columns[f"sample_state_ri_{rep_tag}"] = state_column
        wide_columns[f"share_ri_{rep_tag}"] = share_column
        diagnostics.extend(rep_diagnostics)

    wide = pd.concat([base, pd.DataFrame(wide_columns, index=base.index)], axis=1)
    return stata_safe(wide), pd.DataFrame(diagnostics)


# ---------------------- main build ----------------------

def main() -> None:
    """Build the wide joint treated-firm/pair randomization panels for both sides."""
    config = make_config(RUN_CONFIG)
    destination = output_directory(config)
    destination.mkdir(parents=True, exist_ok=True)

    candidates = load_movement_candidates(config)
    universe = load_ssr_roster()
    interlock_lookup = load_interlock_lookup()
    partner_atcs = load_partner_atcs()

    all_diagnostics: list[pd.DataFrame] = []
    for side_index, treatment_group in enumerate(config.treatment_groups, start=1):
        wide, diagnostics = build_side_panel(
            treatment_group=treatment_group,
            side_index=side_index,
            config=config,
            candidates=candidates,
            universe=universe,
            interlock_lookup=interlock_lookup,
            partner_atcs=partner_atcs,
            destination=destination,
        )
        output_path = destination / f"treated_firm_pair_randomization_{treatment_group}.dta"
        wide.to_stata(output_path, write_index=False, version=config.stata_version)
        all_diagnostics.append(diagnostics)
        print(f"Saved {len(wide):,} rows and {len(wide.columns):,} columns to {output_path}")

    diagnostics_path = destination / "treated_firm_pair_randomization_diagnostics.csv"
    pd.concat(all_diagnostics, ignore_index=True).to_csv(diagnostics_path, index=False)
    print(f"Saved joint-randomization diagnostics to {diagnostics_path}")


if __name__ == "__main__":
    main()
