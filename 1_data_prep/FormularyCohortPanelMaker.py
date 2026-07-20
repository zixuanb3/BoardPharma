r"""
Purpose:
Aggregate quarter-organized formulary rows to drug-firm-quarter outcomes and
build lean, direction-aware event cohorts for formulary did_imputation work.

Process:
1. Stream each required formulary_panel_YYYYQX.csv in chunks and aggregate to
   NDC x BoardName x YEAR_Q without loading a full quarter into memory.
2. Construct four outcomes: included_count, included_share, mean_tiera, and
   mean_tier_raw; retain ATC3 plus req1 event and ATC3-sharing indicators.
3. Reproduce SSR Not controls from pure movement events, and reproduce
   include_eventpair=0 from req1 candidate firm pairs separately for A and B.
4. Save one combined A/B cohort file per event-year with direction-specific
   treated, sample, and sharing flags.

Input:
- data/formulary_panel_by_time/formulary_panel_YYYYQX.csv
- data/event_tables/movement_table_formulary_large_sample_narrow.csv
- data/event_tables/movement_event_candidates_formulary_large_sample_narrow.csv

Output:
- data/formulary_drug_panel_by_time/formulary_drug_panel_YYYYQX.csv
- data/formulary_cohort_data/event/req1/Not/{event}_quarter_cohort_{year}.csv
"""

from __future__ import annotations

import gc
import re
from pathlib import Path

import numpy as np
import pandas as pd
from tqdm.auto import tqdm


# Configure project directory paths
CURRENT_PATH = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_PATH.parent.parent
DATA_ROOT = PROJECT_ROOT / "data"
QUARTER_INPUT_DIR = DATA_ROOT / "formulary_panel_by_time"
DRUG_QUARTER_OUTPUT_DIR = DATA_ROOT / "formulary_drug_panel_by_time"
COHORT_OUTPUT_DIR = DATA_ROOT / "formulary_cohort_data" / "event" / "req1" / "Not"
EVENT_TABLE_DIR = DATA_ROOT / "event_tables"

MOVEMENT_TABLE_PATH = (
    EVENT_TABLE_DIR / "movement_table_formulary_large_sample_narrow.csv"
)
CANDIDATE_PATH = (
    EVENT_TABLE_DIR
    / "movement_event_candidates_formulary_large_sample_narrow.csv"
)

EVENT_TYPES = (
    "to_B_not_in_A",
    "to_B_still_in_A",
    "interlock_dissolution",
)
TREATMENT_GROUPS = ("A", "B")
COHORT_YEARS = {
    "to_B_not_in_A": (2020, 2021, 2022),
    "to_B_still_in_A": (2020, 2021, 2022),
    "interlock_dissolution": (2020, 2021, 2022, 2023, 2024),
}

YEAR_Q_PATTERN = re.compile(r"^(\d{4})Q([1-4])$")
STAY_COLUMN_PATTERN = re.compile(r"^stay_\d+_years$")


# ========================== USER CONFIG ==========================
# chunksize:
# - Controls how many full formulary rows are read at once from one quarter.
#
# window_pre/window_post:
# - A cohort c uses every available quarter in years c-window_pre through
#   c+window_post.  The only intentionally absent period is 2025Q4.
#
# overwrite_*:
# - Keep at 0 to prevent mixing stale and newly generated panels.
RUN_CONFIG = {
    "chunksize": 1_000_000,
    "window_pre": 1,
    "window_post": 1,
    "req": 1,
    "include_eventpair": 0,
    "atc_level": 3,
    "overwrite_drug_quarter_outputs": 0,
    "overwrite_cohort_outputs": 0,
}
# ===============================================================


# ========================== COLUMN HELPERS ==========================


def raw_event_column(event_type: str, side: str) -> str:
    """Return one event column as stored in FormularyPanelMaker output."""
    return f"event_{event_type}_{side}"


def raw_sharing_column(event_type: str, side: str) -> str:
    """Return the ATC3-sharing column stored in FormularyPanelMaker output."""
    return f"{raw_event_column(event_type, side)}_sharingATC3"


def output_event_column(event_type: str, side: str) -> str:
    """Return a Stata-friendly lower-case event column."""
    return raw_event_column(event_type, side).lower()


def output_sharing_column(event_type: str, side: str) -> str:
    """Return a Stata-friendly lower-case source sharing column."""
    return raw_sharing_column(event_type, side).lower()


def cohort_sharing_column(side: str) -> str:
    """Return the cohort-specific, time-invariant ATC3-sharing column."""
    return f"sharingatc3_{side.lower()}"


RAW_EVENT_COLUMNS = [
    raw_event_column(event_type, side)
    for event_type in EVENT_TYPES
    for side in TREATMENT_GROUPS
]
RAW_SHARING_COLUMNS = [
    raw_sharing_column(event_type, side)
    for event_type in EVENT_TYPES
    for side in TREATMENT_GROUPS
]
RAW_TO_OUTPUT_COLUMNS = {
    **{
        raw_event_column(event_type, side): output_event_column(event_type, side)
        for event_type in EVENT_TYPES
        for side in TREATMENT_GROUPS
    },
    **{
        raw_sharing_column(event_type, side): output_sharing_column(event_type, side)
        for event_type in EVENT_TYPES
        for side in TREATMENT_GROUPS
    },
}

RAW_REQUIRED_COLUMNS = [
    "YEAR_Q",
    "FORMULARY_ID",
    "NDC",
    "BoardName",
    "ATC3",
    "included",
    "tier_raw",
    "tierA",
    *RAW_EVENT_COLUMNS,
    *RAW_SHARING_COLUMNS,
]


# ========================== VALIDATION HELPERS ==========================


def normalize_string(values: pd.Series, uppercase: bool = False) -> pd.Series:
    """Strip string values and optionally standardize them to uppercase."""
    result = values.astype("string").str.strip()
    result = result.mask(result.eq(""))
    if uppercase:
        result = result.str.upper()
    return result


def validate_config(config: dict) -> tuple[int, int, int, bool, bool]:
    """Validate the fixed req1, Not-control formulary cohort specification."""
    chunksize = int(config["chunksize"])
    window_pre = int(config["window_pre"])
    window_post = int(config["window_post"])
    req = int(config["req"])
    include_eventpair = int(config["include_eventpair"])
    atc_level = int(config["atc_level"])
    overwrite_drug = int(config["overwrite_drug_quarter_outputs"])
    overwrite_cohort = int(config["overwrite_cohort_outputs"])

    if chunksize < 1:
        raise ValueError("chunksize must be at least 1.")
    if (window_pre, window_post) != (1, 1):
        raise ValueError("This design requires window_pre=1 and window_post=1.")
    if req != 1:
        raise ValueError("This formulary design is fixed at req=1.")
    if include_eventpair != 0:
        raise ValueError("This formulary design is fixed at include_eventpair=0.")
    if atc_level != 3:
        raise ValueError("This formulary design is fixed at ATC3 sharing.")
    if overwrite_drug not in {0, 1} or overwrite_cohort not in {0, 1}:
        raise ValueError("overwrite options must be 0 or 1.")
    return (
        chunksize,
        window_pre,
        window_post,
        bool(overwrite_drug),
        bool(overwrite_cohort),
    )


def canonical_year_q(year: int, quarter: int) -> str:
    """Return the compact quarter tag used in filenames and slim panels."""
    return f"{year}Q{quarter}"


def parse_quarter_filename(path: Path) -> tuple[str, int, int]:
    """Parse formulary_panel_YYYYQX.csv into its canonical period values."""
    prefix = "formulary_panel_"
    tag = path.stem.removeprefix(prefix)
    match = YEAR_Q_PATTERN.fullmatch(tag)
    if match is None:
        raise ValueError(f"Unexpected quarter filename: {path.name}")
    year, quarter = int(match.group(1)), int(match.group(2))
    return tag, year, quarter


def available_quarter_paths() -> dict[str, Path]:
    """Inventory the quarter-organized full formulary files."""
    paths: dict[str, Path] = {}
    for path in QUARTER_INPUT_DIR.glob("formulary_panel_????Q?.csv"):
        tag, _year, _quarter = parse_quarter_filename(path)
        if tag in paths:
            raise ValueError(f"Duplicate quarter input for {tag}: {paths[tag]} and {path}")
        paths[tag] = path
    if not paths:
        raise FileNotFoundError(f"No quarter-organized panels found in {QUARTER_INPUT_DIR}")
    return paths


def expected_cohort_quarters(cohort_year: int, window_pre: int, window_post: int) -> list[str]:
    """Return the nominal 12-quarter window for one cohort."""
    return [
        canonical_year_q(year, quarter)
        for year in range(cohort_year - window_pre, cohort_year + window_post + 1)
        for quarter in range(1, 5)
    ]


def required_quarters(
    available: dict[str, Path],
    window_pre: int,
    window_post: int,
) -> tuple[list[str], dict[int, list[str]]]:
    """Validate all cohort windows while allowing only the known missing 2025Q4."""
    windows: dict[int, list[str]] = {}
    all_required: set[str] = set()
    for cohort_year in sorted(set().union(*COHORT_YEARS.values())):
        nominal = expected_cohort_quarters(cohort_year, window_pre, window_post)
        missing = [tag for tag in nominal if tag not in available]
        unexpected_missing = [tag for tag in missing if tag != "2025Q4"]
        if unexpected_missing:
            raise FileNotFoundError(
                f"Cohort {cohort_year} is missing required quarter files: {unexpected_missing}"
            )
        actual = [tag for tag in nominal if tag in available]
        if cohort_year == 2024 and len(actual) != 11:
            raise ValueError(
                f"The 2024 cohort must contain 11 available quarters through 2025Q3; found {len(actual)}."
            )
        if cohort_year != 2024 and len(actual) != 12:
            raise ValueError(
                f"Cohort {cohort_year} must contain 12 quarters; found {len(actual)}."
            )
        windows[cohort_year] = actual
        all_required.update(actual)
    return sorted(all_required, key=lambda tag: (int(tag[:4]), int(tag[-1]))), windows


def prepare_output_path(path: Path, overwrite: bool) -> None:
    """Create a parent directory and enforce the configured overwrite policy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return
    if not overwrite:
        raise FileExistsError(
            f"Refusing to overwrite existing output: {path}. Move/delete it or enable overwrite."
        )
    path.unlink()


# ========================== DRUG-QUARTER AGGREGATION ==========================


def numeric_column(data: pd.DataFrame, column: str, source_name: str) -> pd.Series:
    """Parse a required numeric column and reject nonnumeric nonmissing values."""
    original = data[column]
    numeric = pd.to_numeric(original, errors="coerce")
    invalid = original.notna() & normalize_string(original).notna() & numeric.isna()
    if invalid.any():
        examples = original.loc[invalid].head(10).tolist()
        raise ValueError(f"{source_name} has invalid {column} values: {examples}")
    return numeric


def aggregate_chunk(chunk: pd.DataFrame, source_name: str) -> pd.DataFrame:
    """Build additive drug-firm sufficient statistics for one raw chunk."""
    chunk["NDC"] = normalize_string(chunk["NDC"])
    chunk["BoardName"] = normalize_string(chunk["BoardName"], uppercase=True)
    if chunk[["NDC", "BoardName", "FORMULARY_ID"]].isna().any().any():
        raise ValueError(f"{source_name} contains missing NDC, BoardName, or FORMULARY_ID.")

    chunk["included"] = numeric_column(chunk, "included", source_name)
    if not chunk["included"].isin([0, 1]).all():
        raise ValueError(f"{source_name} contains included values outside 0/1.")
    chunk["tierA"] = numeric_column(chunk, "tierA", source_name)
    chunk["tier_raw"] = numeric_column(chunk, "tier_raw", source_name)
    if chunk["tierA"].isna().any():
        raise ValueError(f"{source_name} contains missing tierA values.")

    for column in [*RAW_EVENT_COLUMNS, *RAW_SHARING_COLUMNS]:
        chunk[column] = numeric_column(chunk, column, source_name)
        if not chunk[column].isin([0, 1]).all():
            raise ValueError(f"{source_name} contains {column} values outside 0/1.")

    chunk["_tierA_sum"] = chunk["tierA"]
    chunk["_tierA_count"] = chunk["tierA"].notna().astype("int32")
    chunk["_tier_raw_sum"] = chunk["tier_raw"].fillna(0)
    chunk["_tier_raw_count"] = chunk["tier_raw"].notna().astype("int32")

    aggregation: dict[str, tuple[str, str]] = {
        "atc3": ("ATC3", "first"),
        "included_count": ("included", "sum"),
        "n_formularies_observed": ("FORMULARY_ID", "size"),
        "_tierA_sum": ("_tierA_sum", "sum"),
        "_tierA_count": ("_tierA_count", "sum"),
        "_tier_raw_sum": ("_tier_raw_sum", "sum"),
        "_tier_raw_count": ("_tier_raw_count", "sum"),
    }
    aggregation.update(
        {
            RAW_TO_OUTPUT_COLUMNS[column]: (column, "max")
            for column in [*RAW_EVENT_COLUMNS, *RAW_SHARING_COLUMNS]
        }
    )
    return (
        chunk.groupby(["NDC", "BoardName"], as_index=False, sort=False)
        .agg(**aggregation)
        .rename(columns={"NDC": "ndc", "BoardName": "boardname"})
    )


def combine_chunk_aggregates(
    partials: list[pd.DataFrame],
    year_q: str,
    year: int,
    quarter: int,
) -> pd.DataFrame:
    """Combine additive chunk statistics into one drug-firm-quarter panel."""
    if not partials:
        raise ValueError(f"No data chunks were aggregated for {year_q}.")
    combined = pd.concat(partials, ignore_index=True)
    aggregation: dict[str, tuple[str, str]] = {
        "atc3": ("atc3", "first"),
        "included_count": ("included_count", "sum"),
        "n_formularies_observed": ("n_formularies_observed", "sum"),
        "_tierA_sum": ("_tierA_sum", "sum"),
        "_tierA_count": ("_tierA_count", "sum"),
        "_tier_raw_sum": ("_tier_raw_sum", "sum"),
        "_tier_raw_count": ("_tier_raw_count", "sum"),
    }
    aggregation.update(
        {
            column: (column, "max")
            for column in RAW_TO_OUTPUT_COLUMNS.values()
        }
    )
    result = combined.groupby(["ndc", "boardname"], as_index=False, sort=False).agg(
        **aggregation
    )
    result["included_count"] = result["included_count"].astype("int32")
    result["n_formularies_observed"] = result["n_formularies_observed"].astype("int32")
    result["included_share"] = (
        result["included_count"] / result["n_formularies_observed"]
    )
    result["mean_tiera"] = result["_tierA_sum"] / result["_tierA_count"]
    result["mean_tier_raw"] = (
        result["_tier_raw_sum"] / result["_tier_raw_count"].replace(0, np.nan)
    )
    if result["n_formularies_observed"].le(0).any():
        raise AssertionError(f"{year_q} contains a drug group with no formulary rows.")
    if result["included_count"].gt(result["n_formularies_observed"]).any():
        raise AssertionError(f"{year_q} has included_count above the formulary denominator.")
    if not result["included_share"].between(0, 1).all():
        raise AssertionError(f"{year_q} has included_share outside [0, 1].")
    if result["mean_tiera"].isna().any():
        raise AssertionError(f"{year_q} has missing mean_tiera after aggregation.")
    result["year_q"] = year_q
    result["year"] = np.int16(year)
    result["quarter"] = np.int8(quarter)

    flag_columns = list(RAW_TO_OUTPUT_COLUMNS.values())
    result[flag_columns] = result[flag_columns].astype("int8")
    result = result.drop(
        columns=["_tierA_sum", "_tierA_count", "_tier_raw_sum", "_tier_raw_count"]
    )
    ordered = [
        "ndc",
        "boardname",
        "year_q",
        "year",
        "quarter",
        "atc3",
        "included_count",
        "n_formularies_observed",
        "included_share",
        "mean_tiera",
        "mean_tier_raw",
        *[output_event_column(event, side) for event in EVENT_TYPES for side in TREATMENT_GROUPS],
        *[output_sharing_column(event, side) for event in EVENT_TYPES for side in TREATMENT_GROUPS],
    ]
    return result[ordered].sort_values(["boardname", "ndc"]).reset_index(drop=True)


def aggregate_one_quarter(
    source_path: Path,
    output_path: Path,
    chunksize: int,
    overwrite: bool,
) -> None:
    """Stream, aggregate, and save one calendar quarter."""
    year_q, year, quarter = parse_quarter_filename(source_path)
    prepare_output_path(output_path, overwrite)
    partials: list[pd.DataFrame] = []
    reader = pd.read_csv(
        source_path,
        usecols=RAW_REQUIRED_COLUMNS,
        dtype="string",
        chunksize=chunksize,
    )
    chunk_progress = tqdm(
        reader,
        desc=f"  Aggregating {year_q}",
        unit="chunk",
        leave=False,
    )
    expected_raw_year_q = f"{year} Q{quarter}"
    for chunk in chunk_progress:
        observed = set(normalize_string(chunk["YEAR_Q"], uppercase=True).dropna().unique())
        if observed != {expected_raw_year_q}:
            raise ValueError(
                f"{source_path.name} must contain only {expected_raw_year_q}; found {sorted(observed)}"
            )
        partials.append(aggregate_chunk(chunk, source_path.name))
        chunk_progress.set_postfix_str(f"partial groups={sum(len(part) for part in partials):,}")
        del chunk
        gc.collect()

    final_progress = tqdm(
        total=2,
        desc=f"  Finalizing {year_q}",
        unit="stage",
        leave=False,
    )
    final_progress.set_postfix_str("combining drug-level chunk summaries")
    result = combine_chunk_aggregates(partials, year_q, year, quarter)
    if result.duplicated(["ndc", "boardname", "year_q"]).any():
        raise AssertionError(f"{year_q} aggregation is not unique by drug-firm-quarter.")
    final_progress.update(1)
    final_progress.set_postfix_str("writing slim drug-quarter CSV")
    result.to_csv(output_path, index=False)
    final_progress.update(1)
    final_progress.close()
    del partials, result
    gc.collect()


def build_drug_quarter_panels(
    quarter_tags: list[str],
    sources: dict[str, Path],
    chunksize: int,
    overwrite: bool,
) -> dict[str, Path]:
    """Build every required slim drug-quarter file once."""
    outputs = {
        tag: DRUG_QUARTER_OUTPUT_DIR / f"formulary_drug_panel_{tag}.csv"
        for tag in quarter_tags
    }
    progress = tqdm(quarter_tags, desc="Building drug-quarter panels", unit="quarter")
    for tag in progress:
        progress.set_postfix_str(tag)
        aggregate_one_quarter(sources[tag], outputs[tag], chunksize, overwrite)
    return outputs


# ========================== EVENT SOURCE TABLES ==========================


def load_event_sources() -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """Load the small canonical firm-year table and candidate-pair table."""
    movement = pd.read_csv(MOVEMENT_TABLE_PATH, dtype="string")
    candidate = pd.read_csv(CANDIDATE_PATH, dtype="string")

    movement["BoardName"] = normalize_string(movement["BoardName"], uppercase=True)
    movement["event_type"] = normalize_string(movement["event_type"])
    movement["firm_type"] = normalize_string(movement["firm_type"], uppercase=True)
    movement["year"] = pd.to_numeric(movement["year"], errors="raise").astype("int16")
    for column in ("req0", "req1", "req2"):
        movement[column] = pd.to_numeric(movement[column], errors="raise").astype("int8")

    candidate["FirmA"] = normalize_string(candidate["FirmA"], uppercase=True)
    candidate["FirmB"] = normalize_string(candidate["FirmB"], uppercase=True)
    candidate["event_type"] = normalize_string(candidate["event_type"])
    candidate["event_year"] = pd.to_numeric(
        candidate["event_year"], errors="raise"
    ).astype("int16")
    stay_columns = [column for column in candidate if STAY_COLUMN_PATTERN.fullmatch(column)]
    if len(stay_columns) != 1:
        raise ValueError(f"Expected exactly one stay_x_years column; found {stay_columns}")
    stay_column = stay_columns[0]
    for column in (stay_column, "requirement1", "requirement2_A", "requirement2_B"):
        candidate[column] = pd.to_numeric(candidate[column], errors="raise").astype("int8")
    return movement, candidate, stay_column


def treated_firms(
    movement: pd.DataFrame,
    event_type: str,
    side: str,
    cohort_year: int,
) -> set[str]:
    """Return firms satisfying the fixed req1 treatment definition at cohort entry."""
    rows = movement.loc[
        movement["event_type"].eq(event_type)
        & movement["firm_type"].eq(side)
        & movement["year"].eq(cohort_year)
        & movement["req1"].eq(1),
        "BoardName",
    ]
    return set(rows.dropna().astype(str))


def pure_event_firms_in_window(
    movement: pd.DataFrame,
    event_type: str,
    side: str,
    window_years: set[int],
) -> set[str]:
    """Return firms with any raw event-table row, regardless of req flags."""
    rows = movement.loc[
        movement["event_type"].eq(event_type)
        & movement["firm_type"].eq(side)
        & movement["year"].isin(window_years),
        "BoardName",
    ]
    return set(rows.dropna().astype(str))


def counterpart_only_firms(
    candidate: pd.DataFrame,
    stay_column: str,
    event_type: str,
    side: str,
    cohort_year: int,
) -> set[str]:
    """Reproduce SSR include_eventpair=0 using the current req1 candidate set."""
    current = candidate.loc[
        candidate["event_type"].eq(event_type)
        & candidate["event_year"].eq(cohort_year)
        & candidate[stay_column].eq(1)
        & candidate["requirement1"].eq(1)
    ]
    firms_a = set(current["FirmA"].dropna().astype(str))
    firms_b = set(current["FirmB"].dropna().astype(str))
    return firms_b - firms_a if side == "A" else firms_a - firms_b


# ========================== COHORT CONSTRUCTION ==========================


def read_cohort_window(paths: dict[str, Path], quarter_tags: list[str]) -> pd.DataFrame:
    """Read and stack the already aggregated slim quarters for one cohort."""
    frames: list[pd.DataFrame] = []
    quarter_progress = tqdm(
        quarter_tags,
        desc="  Loading slim cohort quarters",
        unit="quarter",
        leave=False,
    )
    for tag in quarter_progress:
        quarter_progress.set_postfix_str(tag)
        frames.append(
            pd.read_csv(
                paths[tag],
                dtype={"ndc": "string", "boardname": "string"},
            )
        )
    cohort = pd.concat(frames, ignore_index=True)
    cohort["ndc"] = normalize_string(cohort["ndc"])
    cohort["boardname"] = normalize_string(cohort["boardname"], uppercase=True)
    duplicate = cohort.duplicated(["ndc", "boardname", "year_q"], keep=False)
    if duplicate.any():
        examples = cohort.loc[duplicate, ["ndc", "boardname", "year_q"]].head(20)
        raise ValueError(f"Drug-quarter files are not unique by id and time. Examples:\n{examples}")
    return cohort


def keep_complete_drug_ids(cohort: pd.DataFrame, expected_quarters: int) -> pd.DataFrame:
    """Keep drug-firm ids observed in every actually available cohort quarter."""
    counts = cohort.groupby(["ndc", "boardname"])["year_q"].nunique()
    complete_ids = counts[counts.eq(expected_quarters)].index
    id_index = pd.MultiIndex.from_frame(cohort[["ndc", "boardname"]])
    return cohort.loc[id_index.isin(complete_ids)].copy()


def validate_panel_treatment_flags(
    cohort: pd.DataFrame,
    event_type: str,
    side: str,
    cohort_year: int,
    expected_firms: set[str],
) -> None:
    """Ensure aggregated req1 flags match the canonical movement table in Q1."""
    event_column = output_event_column(event_type, side)
    q1 = cohort.loc[
        cohort["year"].eq(cohort_year) & cohort["quarter"].eq(1)
    ]
    observed = set(q1.loc[q1[event_column].eq(1), "boardname"].dropna().astype(str))
    universe = set(q1["boardname"].dropna().astype(str))
    expected = expected_firms & universe
    if observed != expected:
        raise ValueError(
            f"Req1 event mismatch for {event_type}, side {side}, {cohort_year}. "
            f"Only in panel: {sorted(observed - expected)[:10]}; "
            f"only in movement table: {sorted(expected - observed)[:10]}"
        )


def add_direction_flags(
    cohort: pd.DataFrame,
    movement: pd.DataFrame,
    candidate: pd.DataFrame,
    stay_column: str,
    event_type: str,
    cohort_year: int,
) -> pd.DataFrame:
    """Add treated/sample/sharing flags for A and B without duplicating rows."""
    result = cohort.copy()
    window_years = set(range(cohort_year - 1, cohort_year + 2))
    universe = set(result["boardname"].dropna().astype(str))
    id_columns = ["ndc", "boardname"]

    for side in TREATMENT_GROUPS:
        side_lower = side.lower()
        treated = treated_firms(movement, event_type, side, cohort_year)
        validate_panel_treatment_flags(
            result,
            event_type,
            side,
            cohort_year,
            treated,
        )
        pure_event = pure_event_firms_in_window(
            movement,
            event_type,
            side,
            window_years,
        )
        excluded_counterparts = counterpart_only_firms(
            candidate,
            stay_column,
            event_type,
            side,
            cohort_year,
        )
        controls = universe - pure_event - excluded_counterparts

        treated_column = f"treated_{side_lower}"
        sample_column = f"sample_{side_lower}"
        share_column = cohort_sharing_column(side)
        source_share_column = output_sharing_column(event_type, side)
        result[treated_column] = result["boardname"].isin(treated).astype("int8")
        result[sample_column] = (
            result[treated_column].eq(1) | result["boardname"].isin(controls)
        ).astype("int8")

        q1_share = result.loc[
            result["year"].eq(cohort_year) & result["quarter"].eq(1),
            [*id_columns, source_share_column],
        ].rename(columns={source_share_column: share_column})
        if q1_share.duplicated(id_columns).any():
            raise ValueError(
                f"Cohort-entry sharing lookup is not unique for {event_type}, {side}, {cohort_year}."
            )
        result = result.merge(q1_share, on=id_columns, how="left", validate="many_to_one")
        result[share_column] = result[share_column].fillna(0).astype("int8")
        result.loc[result[treated_column].eq(0), share_column] = np.int8(0)

    return result


def cohort_output_columns() -> list[str]:
    """Return the intentionally lean cohort schema."""
    return [
        "ndc",
        "boardname",
        "year_q",
        "year",
        "quarter",
        "data_cohort",
        "atc3",
        "included_count",
        "n_formularies_observed",
        "included_share",
        "mean_tiera",
        "mean_tier_raw",
        "treated_a",
        "treated_b",
        "sample_a",
        "sample_b",
        "sharingatc3_a",
        "sharingatc3_b",
        *[output_event_column(event, side) for event in EVENT_TYPES for side in TREATMENT_GROUPS],
    ]


def build_one_cohort(
    drug_quarter_paths: dict[str, Path],
    quarter_tags: list[str],
    movement: pd.DataFrame,
    candidate: pd.DataFrame,
    stay_column: str,
    event_type: str,
    cohort_year: int,
) -> pd.DataFrame:
    """Build one combined-direction, balanced drug-firm cohort."""
    cohort = read_cohort_window(drug_quarter_paths, quarter_tags)
    cohort = keep_complete_drug_ids(cohort, expected_quarters=len(quarter_tags))
    cohort["data_cohort"] = np.int16(cohort_year)
    cohort = add_direction_flags(
        cohort,
        movement,
        candidate,
        stay_column,
        event_type,
        cohort_year,
    )
    cohort = cohort.loc[cohort["sample_a"].eq(1) | cohort["sample_b"].eq(1)].copy()
    return (
        cohort[cohort_output_columns()]
        .sort_values(["boardname", "ndc", "year", "quarter"])
        .reset_index(drop=True)
    )


def build_cohort_outputs(
    drug_quarter_paths: dict[str, Path],
    cohort_windows: dict[int, list[str]],
    movement: pd.DataFrame,
    candidate: pd.DataFrame,
    stay_column: str,
    overwrite: bool,
) -> None:
    """Write the configured event-year cohorts with visible progress."""
    specifications = [
        (event_type, cohort_year)
        for event_type in EVENT_TYPES
        for cohort_year in COHORT_YEARS[event_type]
    ]
    progress = tqdm(specifications, desc="Building formulary cohorts", unit="cohort")
    for event_type, cohort_year in progress:
        progress.set_postfix_str(f"{event_type}/{cohort_year}")
        output_path = COHORT_OUTPUT_DIR / f"{event_type}_quarter_cohort_{cohort_year}.csv"
        prepare_output_path(output_path, overwrite)
        cohort = build_one_cohort(
            drug_quarter_paths,
            cohort_windows[cohort_year],
            movement,
            candidate,
            stay_column,
            event_type,
            cohort_year,
        )
        progress.set_postfix_str(f"{event_type}/{cohort_year}: writing cohort CSV")
        cohort.to_csv(output_path, index=False)
        del cohort
        gc.collect()


# ========================== OUTPUT DISPATCH ==========================


def main() -> None:
    """Build slim drug-quarter panels, then construct all requested cohorts."""
    (
        chunksize,
        window_pre,
        window_post,
        overwrite_drug,
        overwrite_cohort,
    ) = validate_config(RUN_CONFIG)
    sources = available_quarter_paths()
    quarter_tags, cohort_windows = required_quarters(
        sources,
        window_pre,
        window_post,
    )
    movement, candidate, stay_column = load_event_sources()
    drug_quarter_paths = build_drug_quarter_panels(
        quarter_tags,
        sources,
        chunksize,
        overwrite_drug,
    )
    build_cohort_outputs(
        drug_quarter_paths,
        cohort_windows,
        movement,
        candidate,
        stay_column,
        overwrite_cohort,
    )
    print(f"Saved drug-quarter panels to: {DRUG_QUARTER_OUTPUT_DIR}")
    print(f"Saved combined-direction cohorts to: {COHORT_OUTPUT_DIR}")


if __name__ == "__main__":
    main()
