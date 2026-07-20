"""
Purpose:
Create side-specific, stacked wide panels for conditional firm-pair
randomization inference. The design fixes real event firms, event years, and
req0 partner counts; it randomizes partners from the full SSR roster, then
recomputes req1, ATC3 sharing, and counterpart-only control exclusions.

Process:
1. Read complete include-eventpair=1 cohorts for 2009--2018 and stack them.
2. Reproduce observed req1 partner sets, observed ATC3 sharing, and observed
   counterpart-only controls from the raw movement candidates.
3. Draw 1,000 random partner sets per side while preserving each focal firm's
   req0 unique-partner count, then recompute req1 and ATC3 sharing.
4. Add observed and simulated share/onlypair byte columns to one Stata panel
   per side, and save compact replication diagnostics.

Input:
- data/cohort_data/quarter-level_{A|B}_with_{B|A}_large_sample_narrow/event/req1/Not/*.csv
- data/event_tables/movement_event_candidates_large_sample_narrow.csv
- data/event_tables/firm_interlock_panel_large_sample_narrow.csv
- InterimData/ssr_company_roster.csv
- InterimData/boardex_ssr_price_sample.csv
- Existing include-eventpair=0 ATC3 cohorts, used only for observed validation.

Output:
- data/random_inference_firm_pair/.../firm_pair_randomization_{A|B}.dta
- data/random_inference_firm_pair/.../randomization_diagnostics.csv
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

COHORT_DATA_ROOT = DATA_ROOT / "cohort_data"
ATC_SHARING_ROOT = DATA_ROOT / "cohort_data_with_atcsharing_atc3"
EVENT_TABLES_DIR = DATA_ROOT / "event_tables"
OUTPUT_ROOT = DATA_ROOT / "random_inference_firm_pair"


# ========================== USER CONFIG ==========================
RUN_CONFIG = {
    "event": "to_B_still_in_A",
    "event_requirement": 1,
    "control_folder": "Not",
    "cohort_years": list(range(2009, 2019)),
    "treatment_groups": ["A", "B"],
    "large_sample": 1,
    "personnel_definition": "narrow",
    "n_permutations": 1000,
    "base_seed": 20260713,
    "stata_version": 118,
}
# ================================================================


@dataclass(frozen=True)
class RandomizationConfig:
    """Validated configuration for one firm-pair randomization build."""

    event: str
    event_requirement: int
    control_folder: str
    cohort_years: tuple[int, ...]
    treatment_groups: tuple[str, ...]
    large_sample: int
    personnel_definition: str
    n_permutations: int
    base_seed: int
    stata_version: int


def make_config(raw: dict[str, object]) -> RandomizationConfig:
    """Validate user configuration and return its immutable representation."""
    if int(raw["event_requirement"]) != 1:
        raise ValueError("This firm-pair design is defined only for req1.")
    if int(raw["large_sample"]) != 1:
        raise ValueError("This implementation is defined only for the current large SSR sample.")
    if str(raw["event"]) != "to_B_still_in_A":
        raise ValueError("This implementation currently supports only to_B_still_in_A.")
    if int(raw["n_permutations"]) < 1:
        raise ValueError("n_permutations must be positive.")
    treatment_groups = tuple(str(value).upper() for value in raw["treatment_groups"])
    if set(treatment_groups) != {"A", "B"}:
        raise ValueError("treatment_groups must contain A and B exactly once.")

    cohort_years = tuple(int(value) for value in raw["cohort_years"])
    if not cohort_years:
        raise ValueError("cohort_years cannot be empty.")

    return RandomizationConfig(
        event=str(raw["event"]),
        event_requirement=int(raw["event_requirement"]),
        control_folder=str(raw["control_folder"]),
        cohort_years=cohort_years,
        treatment_groups=treatment_groups,
        large_sample=int(raw["large_sample"]),
        personnel_definition=str(raw["personnel_definition"]),
        n_permutations=int(raw["n_permutations"]),
        base_seed=int(raw["base_seed"]),
        stata_version=int(raw["stata_version"]),
    )


# ---------------------- path and key helpers ----------------------

def movement_suffix(config: RandomizationConfig) -> str:
    """Return the existing large-sample suffix used by current SSR panels."""
    return f"_large_sample_{config.personnel_definition}"


def firm_key(value: object) -> str:
    """Normalize a nonmissing firm name for cross-file comparisons."""
    return str(value).strip().upper()


def canonical_pair(first: str, second: str) -> tuple[str, str]:
    """Return the undirected firm-pair key used by the interlock panel."""
    return tuple(sorted((first, second)))


def cohort_directory(
    root: Path,
    treatment_group: str,
    relation: str,
    suffix: str,
    config: RandomizationConfig,
) -> Path:
    """Return one existing cohort directory under a requested pair relation."""
    counterpart = "B" if treatment_group == "A" else "A"
    return (
        root
        / f"quarter-level_{treatment_group}_{relation}_{counterpart}{suffix}"
        / "event"
        / f"req{config.event_requirement}"
        / config.control_folder
    )


def cohort_path(
    root: Path,
    treatment_group: str,
    relation: str,
    cohort_year: int,
    suffix: str,
    config: RandomizationConfig,
    atc_sharing: bool = False,
) -> Path:
    """Return one cohort filename under either raw or ATC-sharing output roots."""
    stem = f"{config.event}_quarter_cohort_{cohort_year}_balanced{suffix}"
    if atc_sharing:
        stem = f"{stem}_atc3"
    return cohort_directory(root, treatment_group, relation, suffix, config) / f"{stem}.csv"


def ensure_required_columns(frame: pd.DataFrame, required: Iterable[str], source: Path) -> None:
    """Raise a readable error when a source file misses required fields."""
    missing = sorted(set(required) - set(frame.columns))
    if missing:
        raise KeyError(f"{source} is missing required columns: {missing}")


# ---------------------- source-table construction ----------------------

def load_ssr_roster() -> np.ndarray:
    """Load the fixed full SSR roster universe used for all event years."""
    source = INTERIM_DATA_DIR / "ssr_company_roster.csv"
    roster = pd.read_csv(source, usecols=["BoardName"])
    names = roster["BoardName"].dropna().map(firm_key)
    universe = np.array(sorted(names.unique()), dtype=object)
    if len(universe) < 2:
        raise ValueError(f"SSR roster must contain at least two firms: {source}")
    return universe


def load_interlock_pairs() -> set[tuple[int, str, str]]:
    """Load actual yearly interlock pairs for req1 reconstruction."""
    source = EVENT_TABLES_DIR / "firm_interlock_panel_large_sample_narrow.csv"
    interlocks = pd.read_csv(source, usecols=["BoardName", "CounterpartBoard", "year"])
    ensure_required_columns(interlocks, ["BoardName", "CounterpartBoard", "year"], source)
    interlocks = interlocks.dropna(subset=["BoardName", "CounterpartBoard", "year"]).copy()
    interlocks["year"] = pd.to_numeric(interlocks["year"], errors="raise").astype(int)
    return {
        (int(year), *canonical_pair(firm_key(board), firm_key(counterpart)))
        for year, board, counterpart in interlocks.loc[
            :, ["year", "BoardName", "CounterpartBoard"]
        ].itertuples(index=False, name=None)
    }


def load_partner_atcs() -> dict[tuple[int, str], set[str]]:
    """Return event-year ATC3 sets for every firm observed in the SSR panel."""
    source = INTERIM_DATA_DIR / "boardex_ssr_price_sample.csv"
    ssr = pd.read_csv(source, usecols=["BoardName", "year", "atc3"])
    ensure_required_columns(ssr, ["BoardName", "year", "atc3"], source)
    ssr = ssr.dropna(subset=["BoardName", "year", "atc3"]).copy()
    ssr["year"] = pd.to_numeric(ssr["year"], errors="raise").astype(int)
    ssr["_firm_key"] = ssr["BoardName"].map(firm_key)
    ssr["atc3"] = ssr["atc3"].astype(str)
    grouped = ssr.groupby(["year", "_firm_key"], sort=False)["atc3"].agg(set)
    return {(int(year), firm): set(atcs) for (year, firm), atcs in grouped.items()}


def load_movement_candidates(config: RandomizationConfig) -> pd.DataFrame:
    """Load and validate raw movement candidates used for req0 partner slots."""
    source = EVENT_TABLES_DIR / f"movement_event_candidates{movement_suffix(config)}.csv"
    candidates = pd.read_csv(source)
    stay_columns = [
        column
        for column in candidates.columns
        if column.startswith("stay_") and column.endswith("_years")
    ]
    if stay_columns != ["stay_2_years"]:
        raise ValueError(f"Expected only stay_2_years in {source}, found {stay_columns}")
    ensure_required_columns(
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


def req1_reference_year(event: str, event_year: int) -> int:
    """Return the year used by the original movement req1 definition."""
    if event == "to_B_still_in_A":
        return event_year - 1
    if event in {"to_B_not_in_A", "interlock_dissolution"}:
        return event_year
    raise ValueError(f"Unsupported movement event: {event}")


def is_req1_valid(
    focal: str,
    partner: str,
    event: str,
    event_year: int,
    interlock_pairs: set[tuple[int, str, str]],
) -> bool:
    """Evaluate movement req1 from actual interlock history for one pair."""
    reference_year = req1_reference_year(event, event_year)
    return (reference_year, *canonical_pair(focal, partner)) not in interlock_pairs


def side_pair_table(
    candidates: pd.DataFrame,
    treatment_group: str,
    cohort_year: int,
    config: RandomizationConfig,
) -> pd.DataFrame:
    """Return unique req0 candidate pairs in focal/partner orientation for one side-year."""
    current = candidates.loc[
        candidates["event_type"].eq(config.event)
        & candidates["event_year"].eq(cohort_year)
        & candidates["stay_2_years"].eq(1),
        ["FirmAKey", "FirmBKey", "requirement1"],
    ].copy()
    if treatment_group == "A":
        current = current.rename(columns={"FirmAKey": "focal", "FirmBKey": "partner"})
    elif treatment_group == "B":
        current = current.rename(columns={"FirmBKey": "focal", "FirmAKey": "partner"})
    else:
        raise ValueError(f"Unsupported treatment group: {treatment_group}")
    return (
        current.groupby(["focal", "partner"], as_index=False, sort=False)["requirement1"]
        .max()
        .reset_index(drop=True)
    )


def validate_observed_req1(
    pair_table: pd.DataFrame,
    event_year: int,
    config: RandomizationConfig,
    interlock_pairs: set[tuple[int, str, str]],
) -> None:
    """Verify that raw candidate requirement1 matches its documented definition."""
    if pair_table.empty:
        return
    recomputed = pair_table.apply(
        lambda row: int(
            is_req1_valid(
                focal=str(row["focal"]),
                partner=str(row["partner"]),
                event=config.event,
                event_year=event_year,
                interlock_pairs=interlock_pairs,
            )
        ),
        axis=1,
    )
    mismatch = pair_table.loc[recomputed.ne(pair_table["requirement1"])]
    if not mismatch.empty:
        raise AssertionError(
            "Raw requirement1 does not match the interlock-panel definition. "
            f"Examples for {event_year}:\n{mismatch.head(10)}"
        )


# ---------------------- cohort and exposure helpers ----------------------

def load_full_cohort(
    treatment_group: str,
    cohort_year: int,
    config: RandomizationConfig,
) -> pd.DataFrame:
    """Load one complete include-eventpair=1 cohort and add stack identifiers."""
    suffix = movement_suffix(config)
    source = cohort_path(
        root=COHORT_DATA_ROOT,
        treatment_group=treatment_group,
        relation="with",
        cohort_year=cohort_year,
        suffix=suffix,
        config=config,
    )
    cohort = pd.read_csv(source)
    event_column = f"event_{cohort_year}"
    ensure_required_columns(
        cohort,
        ["BoardName", "product", "year", "quarter", "atc3", event_column],
        source,
    )
    cohort["year"] = pd.to_numeric(cohort["year"], errors="raise").astype(int)
    cohort["quarter"] = pd.to_numeric(cohort["quarter"], errors="raise").astype(int)
    cohort[event_column] = pd.to_numeric(cohort[event_column], errors="raise").astype("int8")

    relative_quarter = (cohort["year"] - cohort_year) * 4 + (cohort["quarter"] - 1)
    cohort = cohort.loc[relative_quarter.between(-4, 7)].copy()
    cohort["data_cohort"] = np.int16(cohort_year)
    cohort["treated_in_stack"] = cohort[event_column].astype("int8")
    cohort["event_cohort"] = np.where(cohort[event_column].eq(1), cohort_year, np.nan)
    cohort["_firm_key"] = cohort["BoardName"].map(firm_key)
    cohort["_product_key"] = cohort["product"].astype(str)
    return cohort


def treated_product_atcs(cohort: pd.DataFrame, cohort_year: int) -> pd.DataFrame:
    """Return the event-year ATC3 for final treated firm-product observations."""
    products = cohort.loc[
        cohort["treated_in_stack"].eq(1)
        & cohort["year"].eq(cohort_year)
        & cohort["quarter"].eq(1),
        ["_firm_key", "_product_key", "atc3"],
    ].dropna(subset=["atc3"])
    products["atc3"] = products["atc3"].astype(str)
    products = products.drop_duplicates()
    duplicates = products.duplicated(subset=["_firm_key", "_product_key"], keep=False)
    if duplicates.any():
        raise ValueError(
            "A treated firm-product has more than one event-year ATC3. "
            f"Examples:\n{products.loc[duplicates].head(10)}"
        )
    return products


def valid_partner_atcs(
    pairs: pd.DataFrame,
    cohort_year: int,
    partner_atcs: dict[tuple[int, str], set[str]],
) -> dict[str, set[str]]:
    """Union event-year ATCs over each focal firm's req1-valid partner set."""
    atcs_by_focal: dict[str, set[str]] = defaultdict(set)
    for focal, partner in pairs.loc[pairs["req1"].eq(1), ["focal", "partner"]].itertuples(index=False, name=None):
        atcs_by_focal[focal].update(partner_atcs.get((cohort_year, partner), set()))
    return dict(atcs_by_focal)


def share_product_keys(
    product_atcs: pd.DataFrame,
    partner_atcs_by_focal: dict[str, set[str]],
) -> set[tuple[str, str]]:
    """Return treated firm-product keys sharing an ATC3 with a valid partner."""
    return {
        (firm, product)
        for firm, product, atc3 in product_atcs.itertuples(index=False, name=None)
        if atc3 in partner_atcs_by_focal.get(firm, set())
    }


def counterpart_only_firms(pairs: pd.DataFrame) -> set[str]:
    """Return req1-valid partners that are not focal firms in the same side-year."""
    valid = pairs.loc[pairs["req1"].eq(1), ["focal", "partner"]]
    return set(valid["partner"]) - set(valid["focal"])


def assignment_columns(
    cohort: pd.DataFrame,
    share_keys: set[tuple[str, str]],
    onlypair_firms: set[str],
) -> tuple[np.ndarray, np.ndarray]:
    """Create byte share and counterpart-only columns aligned to one cohort frame."""
    product_keys = list(zip(cohort["_firm_key"], cohort["_product_key"]))
    share = np.fromiter((key in share_keys for key in product_keys), dtype=np.int8, count=len(cohort))
    onlypair = cohort["_firm_key"].isin(onlypair_firms).to_numpy(dtype=np.int8)
    return share, onlypair


def random_pairs_for_year(
    req0_pairs: pd.DataFrame,
    universe: np.ndarray,
    cohort_year: int,
    config: RandomizationConfig,
    interlock_pairs: set[tuple[int, str, str]],
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Draw req0-size random partner sets and compute req1 without resampling."""
    rows: list[tuple[str, str, int]] = []
    degrees = req0_pairs.groupby("focal", sort=True).size()
    universe_set = set(universe.tolist())

    for focal, degree in degrees.items():
        if focal not in universe_set:
            raise ValueError(f"Focal firm {focal!r} is absent from the SSR roster universe.")
        choices = universe[universe != focal]
        if int(degree) > len(choices):
            raise ValueError(
                f"Cannot draw {degree} unique partners for {focal!r}; only {len(choices)} are available."
            )
        partners = rng.choice(choices, size=int(degree), replace=False)
        for partner in partners.tolist():
            req1 = int(
                is_req1_valid(
                    focal=focal,
                    partner=str(partner),
                    event=config.event,
                    event_year=cohort_year,
                    interlock_pairs=interlock_pairs,
                )
            )
            rows.append((focal, str(partner), req1))

    return pd.DataFrame(rows, columns=["focal", "partner", "req1"])


# ---------------------- validation and output helpers ----------------------

def observed_without_path(
    treatment_group: str,
    cohort_year: int,
    config: RandomizationConfig,
) -> Path:
    """Return the existing observed include-eventpair=0 ATC-sharing cohort."""
    return cohort_path(
        root=ATC_SHARING_ROOT,
        treatment_group=treatment_group,
        relation="without",
        cohort_year=cohort_year,
        suffix=movement_suffix(config),
        config=config,
        atc_sharing=True,
    )


def validate_observed_cohort(
    full_cohort: pd.DataFrame,
    share_obs: np.ndarray,
    onlypair_obs: np.ndarray,
    treatment_group: str,
    cohort_year: int,
    config: RandomizationConfig,
) -> None:
    """Require rep=0 to reproduce the existing without-eventpair cohort exactly."""
    source = observed_without_path(treatment_group, cohort_year, config)
    observed = pd.read_csv(source)
    event_column = f"event_{cohort_year}"
    ensure_required_columns(observed, ["BoardName", "product", "year", "quarter", "atc_sharing", event_column], source)
    observed["_firm_key"] = observed["BoardName"].map(firm_key)
    observed["_product_key"] = observed["product"].astype(str)

    keep = ~((onlypair_obs == 1) & full_cohort["treated_in_stack"].eq(0).to_numpy())
    reconstructed = full_cohort.loc[keep, ["_firm_key", "_product_key", "year", "quarter"]].copy()
    reconstructed["share_obs"] = share_obs[keep]
    observed_keys = observed[["_firm_key", "_product_key", "year", "quarter", "atc_sharing"]].copy()
    observed_keys["atc_sharing"] = pd.to_numeric(observed_keys["atc_sharing"], errors="raise").astype("int8")

    key_columns = ["_firm_key", "_product_key", "year", "quarter"]
    if reconstructed.duplicated(key_columns).any() or observed_keys.duplicated(key_columns).any():
        raise AssertionError(f"Duplicate cohort observation keys prevent validation for {source}")

    comparison = reconstructed.merge(observed_keys, on=key_columns, how="outer", indicator=True)
    if not comparison["_merge"].eq("both").all():
        raise AssertionError(
            "Observed counterpart-only deletion does not reproduce the existing without-eventpair sample. "
            f"Examples for {treatment_group}/{cohort_year}:\n{comparison.loc[comparison['_merge'].ne('both')].head(10)}"
        )
    if not comparison["share_obs"].eq(comparison["atc_sharing"]).all():
        raise AssertionError(
            "Observed ATC3 sharing does not reproduce the existing without-eventpair labels. "
            f"Examples for {treatment_group}/{cohort_year}:\n"
            f"{comparison.loc[comparison['share_obs'].ne(comparison['atc_sharing'])].head(10)}"
        )


def stata_safe_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Lowercase columns and remove Python-only helper keys before .dta export."""
    result = frame.drop(columns=["_firm_key", "_product_key"], errors="ignore").copy()
    result.columns = [str(column).lower() for column in result.columns]
    duplicates = result.columns[result.columns.duplicated()].tolist()
    if duplicates:
        raise ValueError(f"Lowercasing created duplicate Stata variable names: {duplicates}")
    return result


def build_side_panel(
    treatment_group: str,
    config: RandomizationConfig,
    candidates: pd.DataFrame,
    universe: np.ndarray,
    interlock_pairs: set[tuple[int, str, str]],
    partner_atcs: dict[tuple[int, str], set[str]],
    side_index: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build one wide side panel and compact replication diagnostics."""
    cohort_frames: dict[int, pd.DataFrame] = {}
    req0_pairs_by_year: dict[int, pd.DataFrame] = {}
    treated_atcs_by_year: dict[int, pd.DataFrame] = {}
    observed_pairs_by_year: dict[int, pd.DataFrame] = {}

    for cohort_year in config.cohort_years:
        cohort_frames[cohort_year] = load_full_cohort(treatment_group, cohort_year, config)
        req0_pairs = side_pair_table(candidates, treatment_group, cohort_year, config)
        validate_observed_req1(req0_pairs, cohort_year, config, interlock_pairs)
        req0_pairs_by_year[cohort_year] = req0_pairs
        treated_atcs_by_year[cohort_year] = treated_product_atcs(cohort_frames[cohort_year], cohort_year)
        observed_pairs_by_year[cohort_year] = req0_pairs.loc[req0_pairs["requirement1"].eq(1), ["focal", "partner"]].assign(req1=1)

    observed_columns: dict[str, list[np.ndarray]] = {"share_obs": [], "onlypair_obs": []}
    for cohort_year in config.cohort_years:
        observed_pairs = observed_pairs_by_year[cohort_year]
        atcs = valid_partner_atcs(observed_pairs, cohort_year, partner_atcs)
        share_keys = share_product_keys(treated_atcs_by_year[cohort_year], atcs)
        onlypair = counterpart_only_firms(observed_pairs)
        share, onlypair_column = assignment_columns(cohort_frames[cohort_year], share_keys, onlypair)
        validate_observed_cohort(
            full_cohort=cohort_frames[cohort_year],
            share_obs=share,
            onlypair_obs=onlypair_column,
            treatment_group=treatment_group,
            cohort_year=cohort_year,
            config=config,
        )
        observed_columns["share_obs"].append(share)
        observed_columns["onlypair_obs"].append(onlypair_column)

    stacked = pd.concat([cohort_frames[year] for year in config.cohort_years], ignore_index=True)
    wide_columns: dict[str, np.ndarray] = {
        column: np.concatenate(values).astype(np.int8, copy=False)
        for column, values in observed_columns.items()
    }
    diagnostics: list[dict[str, int | str]] = []

    for rep in tqdm(
        range(1, config.n_permutations + 1),
        desc=f"Randomizing side {treatment_group}",
        unit="rep",
        dynamic_ncols=True,
    ):
        seed = config.base_seed + side_index * 1_000_000 + rep
        rng = np.random.default_rng(seed)
        share_columns: list[np.ndarray] = []
        onlypair_columns: list[np.ndarray] = []
        rep_tag = f"{rep:04d}"

        for cohort_year in config.cohort_years:
            random_pairs = random_pairs_for_year(
                req0_pairs=req0_pairs_by_year[cohort_year],
                universe=universe,
                cohort_year=cohort_year,
                config=config,
                interlock_pairs=interlock_pairs,
                rng=rng,
            )
            atcs = valid_partner_atcs(random_pairs, cohort_year, partner_atcs)
            share_keys = share_product_keys(treated_atcs_by_year[cohort_year], atcs)
            onlypair = counterpart_only_firms(random_pairs)
            share, onlypair_column = assignment_columns(cohort_frames[cohort_year], share_keys, onlypair)
            share_columns.append(share)
            onlypair_columns.append(onlypair_column)
            diagnostics.append(
                {
                    "rep": rep,
                    "seed": seed,
                    "side": treatment_group,
                    "cohort_year": cohort_year,
                    "req0_random_pairs": len(random_pairs),
                    "req1_valid_random_pairs": int(random_pairs["req1"].sum()),
                    "onlypair_firms": len(onlypair),
                    "share_products": len(share_keys),
                    "notshare_products": len(treated_atcs_by_year[cohort_year]) - len(share_keys),
                }
            )

        wide_columns[f"share_ri_{rep_tag}"] = np.concatenate(share_columns).astype(np.int8, copy=False)
        wide_columns[f"onlypair_ri_{rep_tag}"] = np.concatenate(onlypair_columns).astype(np.int8, copy=False)

    wide = pd.concat([stacked, pd.DataFrame(wide_columns, index=stacked.index)], axis=1)
    diagnostic_frame = pd.DataFrame(diagnostics)
    return stata_safe_columns(wide), diagnostic_frame


# ---------------------- main build ----------------------

def main() -> None:
    """Build observed and simulated firm-pair RI panels for A and B sides."""
    config = make_config(RUN_CONFIG)
    output_dir = (
        OUTPUT_ROOT
        / config.event
        / f"req{config.event_requirement}"
        / f"large_sample_{config.personnel_definition}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    candidates = load_movement_candidates(config)
    universe = load_ssr_roster()
    interlock_pairs = load_interlock_pairs()
    partner_atcs = load_partner_atcs()

    all_diagnostics: list[pd.DataFrame] = []
    for side_index, treatment_group in enumerate(config.treatment_groups, start=1):
        wide, diagnostics = build_side_panel(
            treatment_group=treatment_group,
            config=config,
            candidates=candidates,
            universe=universe,
            interlock_pairs=interlock_pairs,
            partner_atcs=partner_atcs,
            side_index=side_index,
        )
        output_path = output_dir / f"firm_pair_randomization_{treatment_group}.dta"
        wide.to_stata(output_path, write_index=False, version=config.stata_version)
        all_diagnostics.append(diagnostics)
        print(f"Saved {len(wide):,} rows and {len(wide.columns):,} columns to {output_path}")

    diagnostics_frame = pd.concat(all_diagnostics, ignore_index=True)
    diagnostics_frame.to_csv(output_dir / "randomization_diagnostics.csv", index=False)
    print(f"Saved randomization diagnostics to {output_dir}")


if __name__ == "__main__":
    main()
