r"""
Purpose:
Aggregate quarter-organized formulary rows to drug-firm-quarter outcomes and
build direction-aware req1 event cohorts. RUN_CONFIG["quarter"] selects annual
events (0) or events in their actual calendar quarter (1).

Process:
1. Build or reuse each required drug-quarter panel. When building, stream
   formulary_panel_YYYYQX.csv and aggregate to NDC x company x YEAR_Q.
2. Construct four outcomes: included_count, included_share, mean_tiera, and
   mean_tier_raw; retain ATC3 plus req1 event and ATC3-sharing indicators.
3. Match company id in quarterly mode or BoardName in annual mode to the
   event tables, reproduce SSR Not controls from pure movement events, and reproduce
   include_eventpair=0 from req1 candidate pairs separately for A and B.
4. In quarter=1, keep four pre-event quarters and eight quarters starting at
   the event; missing quarters outside the available data range are allowed.
   Build the configured first_seen/exit samples, where first_seen=1 requires
   first inclusion no later than the event quarter and exit=1 requires last
   inclusion in or after the cohort's final available quarter. Save separate
   A/B files containing only that direction's sample. Skip a direction when no
   treated drug-company unit survives. Annual mode retains its combined A/B
   files, calendar-year window, and legacy first-seen rule.

Input:
- quarter=0 with shift_q1:
  data/formulary_panel_by_time/shift_q1/formulary_panel_YYYYQX.csv
  data/formulary_metadata/ndc_first_seen_shift_q1.csv
  data/event_tables/movement_table_formulary_large_sample_narrow.csv
  data/event_tables/movement_event_candidates_formulary_large_sample_narrow.csv
- quarter=1 with shift_q1:
  data/formulary_panel_quarter_by_time/shift_q1/formulary_panel_YYYYQX.csv
  data/formulary_metadata/ndc_first_seen_quarter_shift_q1.csv
  data/formulary_metadata/ndc_last_seen_quarter_shift_q1.csv
  data/event_tables/movement_table_formulary_quarter_narrow.csv
  data/event_tables/movement_event_candidates_formulary_quarter_narrow.csv

Output:
- quarter=0 with shift_q1 and first_seen_year_offset=-1:
  data/formulary_drug_panel_by_time/shift_q1/formulary_drug_panel_YYYYQX.csv
  data/formulary_cohort_data/event/req1/Not/shift_q1_seen_y-1_q1/
  {event}_quarter_cohort_{year}.csv
- quarter=1 with shift_q1:
  data/formulary_drug_panel_quarter_by_time/shift_q1/formulary_drug_panel_YYYYQX.csv
  data/formulary_cohort_data_quarter/event/req1/Not/
  shift_q1_first_seen_{0|1}_exit_{0|1}_event_q/
  {A|B}/{event}_quarter_cohort_YYYYQX.csv
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
FIRST_SEEN_PATH = DATA_ROOT / "formulary_metadata" / "ndc_first_seen.csv"
LAST_SEEN_PATH = DATA_ROOT / "formulary_metadata" / "ndc_last_seen.csv"

TREATMENT_GROUPS = ("A", "B")

YEAR_Q_PATTERN = re.compile(r"^(\d{4})Q([1-4])$")


# ========================== USER CONFIG ==========================
# EVENT_TYPES/COHORT_YEARS:
# - Select supported movement events and eligible event years.
EVENT_TYPES = (
    "to_B_not_in_A",
    "to_B_still_in_A",
    "interlock_dissolution",
)
COHORT_YEARS = {
    "to_B_not_in_A": (2020, 2021, 2022, 2023, 2024),
    "to_B_still_in_A": (2020, 2021, 2022, 2023, 2024),
    "interlock_dissolution": (2020, 2021, 2022, 2023, 2024),
}
#
# quarter:
# - 0: annual cohorts with combined A/B output; 1: actual event-quarter cohorts
#   with separate A/B output directories and direction-specific treated checks.
# Fixed design: both directions are built; existing outcome and A/B flag
# columns are retained in each directional file for downstream compatibility.
# chunksize:
# - Controls how many full formulary rows are read at once from one quarter.
#
# window_pre/window_post: annual-event cohorts use calendar years.
# quarter_pre_periods/quarter_post_periods: quarterly-event cohorts use four
# quarters before the event and eight quarters starting with the event.
#
# formulary_time_shift_quarters:
# - Must match FormularyPanelMaker.py and ReorganizeFormularyData.py.
# rebuild_drug_quarter_panels:
# - 0 reuses existing slim files from the same quarter/shift specification;
#   1 regenerates them from the full reorganized formulary files.
#
# first_seen_year_offset: NDC must have first been included by this many years
# before the event, in its event quarter. first_seen_quarter applies only to
# annual mode, whose event quarter is Q1.
#
# sample_restrictions (quarter=1 only):
# - Each pair is (first_seen, exit).
# - first_seen=1 requires NDC first_seen_qtime <= the cohort event quarter.
# - exit=1 requires first_seen=1 and last_seen_qtime >= the final actually
#   available quarter in that cohort window. The unsupported (0, 1) pair is
#   rejected. The maintained run builds exactly (0,0), (1,0), and (1,1).
RUN_CONFIG = {
    "quarter": 1,
    "chunksize": 500_000,
    "window_pre": 1,
    "window_post": 1,
    "quarter_pre_periods": 4,
    "quarter_post_periods": 8,
    "req": 1,
    "include_eventpair": 0,
    "atc_level": 3,
    "formulary_time_shift_quarters": 1,
    "rebuild_drug_quarter_panels": 1,
    "first_seen_year_offset": -1,
    "first_seen_quarter": 1,
    "sample_restrictions": ((0, 0), (1, 0), (1, 1)),
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
    "ATC3",
    "included",
    "tier_raw",
    "tierA",
    *RAW_EVENT_COLUMNS,
    *RAW_SHARING_COLUMNS,
]


def raw_required_columns(quarter: int = 0) -> list[str]:
    """Return raw formulary columns with the identifier for the selected mode."""
    return [*RAW_REQUIRED_COLUMNS, "id" if quarter else "BoardName"]


# ========================== VALIDATION HELPERS ==========================


def normalize_string(values: pd.Series, uppercase: bool = False) -> pd.Series:
    """Strip string values and optionally standardize them to uppercase."""
    result = values.astype("string").str.strip()
    result = result.mask(result.eq(""))
    if uppercase:
        result = result.str.upper()
    return result


def shift_label(shift_quarters: int) -> str:
    """Return the folder/file label for a formulary quarter shift."""
    return f"shift_q{shift_quarters:+d}".replace("+", "")


def first_seen_spec_label(year_offset: int, quarter: int) -> str:
    """Return the folder label for one NDC first-seen cutoff."""
    return f"seen_y{year_offset:+d}_q{quarter}"


def sample_spec_label(shift_quarters: int, year_offset: int, quarter: int) -> str:
    """Return the non-baseline sample folder label."""
    return f"{shift_label(shift_quarters)}_{first_seen_spec_label(year_offset, quarter)}"


def quarter_input_dir(shift_quarters: int, quarter: int = 0) -> Path:
    """Return the quarter-organized formulary input directory."""
    base = QUARTER_INPUT_DIR.with_name("formulary_panel_quarter_by_time") if quarter else QUARTER_INPUT_DIR
    return base if shift_quarters == 0 else base / shift_label(shift_quarters)


def drug_quarter_output_dir(shift_quarters: int, quarter: int = 0) -> Path:
    """Return the slim drug-quarter output directory."""
    base = DRUG_QUARTER_OUTPUT_DIR.with_name("formulary_drug_panel_quarter_by_time") if quarter else DRUG_QUARTER_OUTPUT_DIR
    return base if shift_quarters == 0 else base / shift_label(shift_quarters)


def first_seen_path(shift_quarters: int, quarter: int = 0) -> Path:
    """Return the NDC first-seen metadata produced with the selected panel."""
    base = FIRST_SEEN_PATH.with_name("ndc_first_seen_quarter.csv") if quarter else FIRST_SEEN_PATH
    if shift_quarters == 0:
        return base
    return base.with_name(f"{base.stem}_{shift_label(shift_quarters)}.csv")


def last_seen_path(shift_quarters: int, quarter: int = 0) -> Path:
    """Return the NDC last-seen metadata produced with the selected panel."""
    base = LAST_SEEN_PATH.with_name("ndc_last_seen_quarter.csv") if quarter else LAST_SEEN_PATH
    if shift_quarters == 0:
        return base
    return base.with_name(f"{base.stem}_{shift_label(shift_quarters)}.csv")


def cohort_output_dir(
    shift_quarters: int,
    year_offset: int,
    first_seen_quarter: int,
    quarter: int = 0,
    first_seen: int | None = None,
    exit_restriction: int | None = None,
) -> Path:
    """Return the cohort output directory, preserving the baseline path."""
    base = COHORT_OUTPUT_DIR
    if quarter:
        base = DATA_ROOT / "formulary_cohort_data_quarter" / "event" / "req1" / "Not"
        if first_seen is None or exit_restriction is None:
            raise ValueError("Quarterly cohort paths require first_seen and exit settings.")
        label = (
            f"{shift_label(shift_quarters)}_first_seen_{first_seen}"
            f"_exit_{exit_restriction}_event_q"
        )
        return base / label
    if shift_quarters == 0 and year_offset == 0 and first_seen_quarter == 1:
        return base
    return base / sample_spec_label(shift_quarters, year_offset, first_seen_quarter)


def validate_config(
    config: dict,
) -> tuple[int, int, int, int, int, int, int, int, int, int, tuple[tuple[int, int], ...]]:
    """Validate the fixed req1, Not-control formulary cohort specification."""
    quarter = int(config["quarter"])
    chunksize = int(config["chunksize"])
    window_pre = int(config["window_pre"])
    window_post = int(config["window_post"])
    quarter_pre_periods = int(config["quarter_pre_periods"])
    quarter_post_periods = int(config["quarter_post_periods"])
    req = int(config["req"])
    include_eventpair = int(config["include_eventpair"])
    atc_level = int(config["atc_level"])
    time_shift = int(config["formulary_time_shift_quarters"])
    rebuild_drug_quarter_panels = int(config["rebuild_drug_quarter_panels"])
    first_seen_year_offset = int(config["first_seen_year_offset"])
    first_seen_quarter = int(config["first_seen_quarter"]) if not quarter else 1
    sample_restrictions = tuple(
        (int(first_seen), int(exit_restriction))
        for first_seen, exit_restriction in config["sample_restrictions"]
    )

    if quarter not in {0, 1}:
        raise ValueError("quarter must be 0 or 1.")
    if rebuild_drug_quarter_panels not in {0, 1}:
        raise ValueError("rebuild_drug_quarter_panels must be 0 or 1.")
    if chunksize < 1:
        raise ValueError("chunksize must be at least 1.")
    if (window_pre, window_post) != (1, 1):
        raise ValueError("This design requires window_pre=1 and window_post=1.")
    if (quarter_pre_periods, quarter_post_periods) != (4, 8):
        raise ValueError("Quarterly cohorts require four pre-event and eight event/post-event quarters.")
    if req != 1:
        raise ValueError("This formulary design is fixed at req=1.")
    if include_eventpair != 0:
        raise ValueError("This formulary design is fixed at include_eventpair=0.")
    if atc_level != 3:
        raise ValueError("This formulary design is fixed at ATC3 sharing.")
    if not quarter and first_seen_quarter not in {1, 2, 3, 4}:
        raise ValueError("first_seen_quarter must be 1, 2, 3, or 4.")
    if quarter:
        if not sample_restrictions:
            raise ValueError("quarter=1 requires at least one first_seen/exit sample.")
        if len(set(sample_restrictions)) != len(sample_restrictions):
            raise ValueError("sample_restrictions contains duplicate settings.")
        for first_seen, exit_restriction in sample_restrictions:
            if first_seen not in {0, 1} or exit_restriction not in {0, 1}:
                raise ValueError("first_seen and exit settings must be 0 or 1.")
            if first_seen == 0 and exit_restriction == 1:
                raise ValueError("The (first_seen=0, exit=1) sample is unsupported.")
    return (
        quarter,
        chunksize,
        window_pre,
        window_post,
        quarter_pre_periods,
        quarter_post_periods,
        time_shift,
        rebuild_drug_quarter_panels,
        first_seen_year_offset,
        first_seen_quarter,
        sample_restrictions,
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


def quarter_sort_key(tag: str) -> tuple[int, int]:
    """Return chronological sort key for a compact YYYYQX tag."""
    return int(tag[:4]), int(tag[-1])


def quarter_time(year: int, quarter: int) -> int:
    """Encode a calendar quarter as a consecutive integer."""
    return year * 4 + quarter


def tag_from_quarter_time(value: int) -> str:
    """Convert an encoded calendar quarter to YYYYQX."""
    year, zero_based_quarter = divmod(value - 1, 4)
    return canonical_year_q(year, zero_based_quarter + 1)


def available_quarter_paths(source_dir: Path) -> dict[str, Path]:
    """Inventory the quarter-organized full formulary files."""
    paths: dict[str, Path] = {}
    for path in source_dir.glob("formulary_panel_????Q?.csv"):
        tag, _year, _quarter = parse_quarter_filename(path)
        if tag in paths:
            raise ValueError(f"Duplicate quarter input for {tag}: {paths[tag]} and {path}")
        paths[tag] = path
    if not paths:
        raise FileNotFoundError(f"No quarter-organized panels found in {source_dir}")
    return paths


def expected_cohort_quarters(cohort_year: int, window_pre: int, window_post: int) -> list[str]:
    """Return the nominal 12-quarter window for one cohort."""
    return [
        canonical_year_q(year, quarter)
        for year in range(cohort_year - window_pre, cohort_year + window_post + 1)
        for quarter in range(1, 5)
    ]


def quarterly_cohort_quarters(
    cohort_year: int,
    cohort_quarter: int,
    pre_periods: int,
    post_periods: int,
) -> list[str]:
    """Return four pre-event quarters and eight quarters from the event onward."""
    event_time = quarter_time(cohort_year, cohort_quarter)
    return [
        tag_from_quarter_time(event_time + offset)
        for offset in range(-pre_periods, post_periods)
    ]


def cohort_specifications(movement: pd.DataFrame, quarter: int) -> list[tuple[str, int, int | None]]:
    """Select configured event years and observed req1 event quarters."""
    if not quarter:
        return [
            (event_type, year, None)
            for event_type in EVENT_TYPES
            for year in COHORT_YEARS[event_type]
        ]
    eligible = movement.loc[movement["req1"].eq(1), ["event_type", "year", "quarter"]]
    return [
        (event_type, year, event_quarter)
        for event_type in EVENT_TYPES
        for year, event_quarter in sorted(
            {
                (int(row.year), int(row.quarter))
                for row in eligible.loc[
                    eligible["event_type"].eq(event_type)
                    & eligible["year"].isin(COHORT_YEARS[event_type])
                ].itertuples(index=False)
            }
        )
    ]


def required_quarters(
    available: dict[str, Path],
    window_pre: int,
    window_post: int,
    specifications: list[tuple[str, int, int | None]] | None = None,
    quarter: int = 0,
    quarter_pre_periods: int = 4,
    quarter_post_periods: int = 8,
) -> tuple[list[str], dict[int | tuple[int, int | None], list[str]]]:
    """Validate cohort windows while allowing missing quarters only at data edges."""
    legacy_annual = specifications is None
    if specifications is None:
        specifications = [
            ("", year, None)
            for year in sorted(set().union(*COHORT_YEARS.values()))
        ]
    windows: dict[int | tuple[int, int | None], list[str]] = {}
    all_required: set[str] = set()
    available_keys = {tag: quarter_sort_key(tag) for tag in available}
    min_available = min(available_keys.values())
    max_available = max(available_keys.values())
    for _event_type, cohort_year, cohort_quarter in specifications:
        key = cohort_year if legacy_annual else (cohort_year, cohort_quarter)
        if key in windows:
            continue
        nominal = (
            quarterly_cohort_quarters(
                cohort_year, cohort_quarter, quarter_pre_periods, quarter_post_periods
            )
            if quarter
            else expected_cohort_quarters(cohort_year, window_pre, window_post)
        )
        missing = [tag for tag in nominal if tag not in available]
        unexpected_missing = [
            tag
            for tag in missing
            if min_available <= quarter_sort_key(tag) <= max_available
        ]
        if unexpected_missing:
            raise FileNotFoundError(
                f"Cohort {key} is missing required quarter files: {unexpected_missing}"
            )
        actual = [tag for tag in nominal if tag in available]
        if not actual:
            raise FileNotFoundError(f"Cohort {key} has no available quarters.")
        event_tag = canonical_year_q(cohort_year, cohort_quarter or 1)
        if event_tag not in actual:
            raise FileNotFoundError(f"Cohort {key} is missing its event quarter {event_tag}.")
        windows[key] = actual
        all_required.update(actual)
    return sorted(all_required, key=quarter_sort_key), windows


def prepare_output_path(path: Path, overwrite: bool) -> None:
    """Create a parent directory and replace prior output."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
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


def aggregate_chunk(
    chunk: pd.DataFrame,
    source_name: str,
    quarter: int = 0,
) -> pd.DataFrame:
    """Build additive drug-firm sufficient statistics for one raw chunk."""
    firm_column = "id" if quarter else "BoardName"
    chunk["NDC"] = normalize_string(chunk["NDC"])
    chunk[firm_column] = normalize_string(
        chunk[firm_column], uppercase=not quarter
    )
    if chunk[["NDC", firm_column, "FORMULARY_ID"]].isna().any().any():
        raise ValueError(
            f"{source_name} contains missing NDC, {firm_column}, or FORMULARY_ID."
        )

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
        chunk.groupby(["NDC", firm_column], as_index=False, sort=False)
        .agg(**aggregation)
        .rename(columns={"NDC": "ndc", firm_column: "boardname"})
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
    quarter_mode: int = 0,
) -> None:
    """Stream, aggregate, and save one calendar quarter."""
    year_q, year, quarter = parse_quarter_filename(source_path)
    prepare_output_path(output_path, overwrite)
    partials: list[pd.DataFrame] = []
    reader = pd.read_csv(
        source_path,
        usecols=raw_required_columns(quarter_mode),
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
        partials.append(aggregate_chunk(chunk, source_path.name, quarter_mode))
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
    destination_dir: Path,
    chunksize: int,
    quarter: int = 0,
) -> dict[str, Path]:
    """Build every required slim drug-quarter file once."""
    outputs = {
        tag: destination_dir / f"formulary_drug_panel_{tag}.csv"
        for tag in quarter_tags
    }
    progress = tqdm(quarter_tags, desc="Building drug-quarter panels", unit="quarter")
    for tag in progress:
        progress.set_postfix_str(tag)
        aggregate_one_quarter(
            sources[tag], outputs[tag], chunksize, overwrite=True, quarter_mode=quarter
        )
    return outputs


def existing_drug_quarter_panels(quarter_tags: list[str], directory: Path) -> dict[str, Path]:
    """Reuse complete slim quarters and reject missing or incompatible files."""
    required = {
        "ndc", "boardname", "year_q", "year", "quarter", "atc3",
        "included_count", "n_formularies_observed", "included_share",
        "mean_tiera", "mean_tier_raw", *RAW_TO_OUTPUT_COLUMNS.values(),
    }
    outputs: dict[str, Path] = {}
    for tag in quarter_tags:
        path = directory / f"formulary_drug_panel_{tag}.csv"
        if not path.exists():
            raise FileNotFoundError(
                f"Missing slim quarter {path}; set rebuild_drug_quarter_panels=1 to create it."
            )
        sample = pd.read_csv(path, nrows=1)
        missing = sorted(required - set(sample.columns))
        if missing:
            raise KeyError(f"{path.name} is missing slim-panel columns: {missing}")
        if sample.empty or str(sample.loc[0, "year_q"]) != tag:
            raise ValueError(f"{path.name} is empty or contains the wrong quarter tag.")
        board_names = sample["boardname"].astype("string")
        if (board_names.str.startswith("{") & board_names.ne("{}")).any():
            raise ValueError(
                f"{path.name} still contains JSON BoardName values. Rebuild the full formulary panel "
                "and then set rebuild_drug_quarter_panels=1."
            )
        outputs[tag] = path
    return outputs


# ========================== EVENT SOURCE TABLES ==========================


def load_seen_lookup(path: Path, qtime_column: str) -> dict[str, int]:
    """Load one unique NDC-to-quarter first/last-seen lookup."""
    if not path.exists():
        raise FileNotFoundError(f"NDC seen-quarter lookup not found: {path}")
    seen = pd.read_csv(path, dtype={"NDC": "string"})
    required = {"NDC", qtime_column}
    missing = sorted(required - set(seen.columns))
    if missing:
        raise KeyError(f"{path.name} is missing columns: {missing}")

    seen["NDC"] = normalize_string(seen["NDC"])
    if seen["NDC"].isna().any() or seen["NDC"].duplicated().any():
        raise ValueError(f"{path.name} must contain one nonmissing row per NDC.")
    seen[qtime_column] = pd.to_numeric(seen[qtime_column], errors="raise").astype("int32")
    return dict(
        zip(
            seen["NDC"].astype(str),
            seen[qtime_column].astype(int),
        )
    )


def load_first_seen_lookup(path: Path) -> dict[str, int]:
    """Load the earliest included quarter for every expanded NDC."""
    return load_seen_lookup(path, "first_seen_qtime")


def load_last_seen_lookup(path: Path) -> dict[str, int]:
    """Load the latest included quarter for every expanded NDC."""
    return load_seen_lookup(path, "last_seen_qtime")


def load_event_sources(quarter: int = 0) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """Load matching annual or quarterly movement and candidate tables."""
    suffix = "formulary_quarter_narrow" if quarter else "formulary_large_sample_narrow"
    movement_path = EVENT_TABLE_DIR / f"movement_table_{suffix}.csv"
    candidate_path = EVENT_TABLE_DIR / f"movement_event_candidates_{suffix}.csv"
    movement = pd.read_csv(movement_path, dtype="string")
    candidate = pd.read_csv(candidate_path, dtype="string")

    movement_firm = "id" if quarter else "BoardName"
    candidate_a, candidate_b = ("idA", "idB") if quarter else ("FirmA", "FirmB")
    movement_required = {movement_firm, "event_type", "firm_type", "year", "req1"}
    candidate_required = {candidate_a, candidate_b, "event_type"}
    if quarter:
        movement_required.add("quarter")
        candidate_required.update(
            (
                "idA_event_time_id",
                "idB_event_time_id",
                "requirement1_A",
                "requirement1_B",
            )
        )
        stay_column = "stay_8_quarters"
    else:
        movement_required.update(("req0", "req2"))
        candidate_required.update(
            ("event_year", "requirement1", "requirement2_A", "requirement2_B")
        )
        stay_columns = [column for column in candidate if re.fullmatch(r"stay_\d+_years", column)]
        if len(stay_columns) != 1:
            raise ValueError(f"Expected exactly one stay_x_years column; found {stay_columns}")
        stay_column = stay_columns[0]
    candidate_required.add(stay_column)
    for path, data, required in (
        (movement_path, movement, movement_required),
        (candidate_path, candidate, candidate_required),
    ):
        missing = sorted(required - set(data.columns))
        if missing:
            raise KeyError(f"{path.name} is missing columns: {missing}")

    movement[movement_firm] = normalize_string(
        movement[movement_firm], uppercase=not quarter
    )
    movement["event_type"] = normalize_string(movement["event_type"])
    movement["firm_type"] = normalize_string(movement["firm_type"], uppercase=True)
    movement["year"] = pd.to_numeric(movement["year"], errors="raise").astype("int16")
    if quarter:
        movement["quarter"] = pd.to_numeric(movement["quarter"], errors="raise").astype("int8")
        if not movement["quarter"].isin((1, 2, 3, 4)).all():
            raise ValueError(f"{movement_path.name} has invalid event quarters.")
    for column in (("req1",) if quarter else ("req0", "req1", "req2")):
        movement[column] = pd.to_numeric(movement[column], errors="raise").astype("int8")
        if not movement[column].isin((0, 1)).all():
            raise ValueError(f"{movement_path.name} has invalid {column} flags.")

    candidate[candidate_a] = normalize_string(
        candidate[candidate_a], uppercase=not quarter
    )
    candidate[candidate_b] = normalize_string(
        candidate[candidate_b], uppercase=not quarter
    )
    candidate["event_type"] = normalize_string(candidate["event_type"])
    if quarter:
        for side in TREATMENT_GROUPS:
            time_ids = pd.to_numeric(
                candidate[f"id{side}_event_time_id"], errors="raise"
            ).astype("int64")
            candidate[f"event_year_{side}"] = (time_ids // 4).astype("int16")
            candidate[f"event_quarter_{side}"] = (time_ids % 4 + 1).astype("int8")
        condition_columns = (stay_column, "requirement1_A", "requirement1_B")
    else:
        candidate["event_year"] = pd.to_numeric(
            candidate["event_year"], errors="raise"
        ).astype("int16")
        condition_columns = (
            stay_column,
            "requirement1",
            "requirement2_A",
            "requirement2_B",
        )
    for column in condition_columns:
        candidate[column] = pd.to_numeric(candidate[column], errors="raise").astype("int8")
        if not candidate[column].isin((0, 1)).all():
            raise ValueError(f"{candidate_path.name} has invalid {column} flags.")
    return movement, candidate, stay_column


def treated_firms(
    movement: pd.DataFrame,
    event_type: str,
    side: str,
    cohort_year: int,
    cohort_quarter: int | None = None,
) -> set[str]:
    """Return firms satisfying the fixed req1 treatment definition at cohort entry."""
    firm_column = "id" if cohort_quarter is not None else "BoardName"
    condition = (
        movement["event_type"].eq(event_type)
        & movement["firm_type"].eq(side)
        & movement["year"].eq(cohort_year)
        & movement["req1"].eq(1)
    )
    if cohort_quarter is not None:
        condition &= movement["quarter"].eq(cohort_quarter)
    rows = movement.loc[condition, firm_column]
    return set(rows.dropna().astype(str))


def pure_event_firms_in_window(
    movement: pd.DataFrame,
    event_type: str,
    side: str,
    window_tags: list[str] | set[int],
    quarter: int = 0,
) -> set[str]:
    """Return firms with any raw event-table row, regardless of req flags."""
    firm_column = "id" if quarter else "BoardName"
    condition = (
        movement["event_type"].eq(event_type)
        & movement["firm_type"].eq(side)
    )
    if quarter:
        event_tags = movement["year"].astype(str) + "Q" + movement["quarter"].astype(str)
        condition &= event_tags.isin(window_tags)
    else:
        condition &= movement["year"].isin(
            {int(str(tag)[:4]) for tag in window_tags}
        )
    rows = movement.loc[condition, firm_column]
    return set(rows.dropna().astype(str))


def counterpart_only_firms(
    candidate: pd.DataFrame,
    stay_column: str,
    event_type: str,
    side: str,
    cohort_year: int,
    cohort_quarter: int | None = None,
) -> set[str]:
    """Reproduce SSR include_eventpair=0 using the current req1 candidate set."""
    if cohort_quarter is not None:
        condition = (
            candidate["event_type"].eq(event_type)
            & candidate[f"event_year_{side}"].eq(cohort_year)
            & candidate[f"event_quarter_{side}"].eq(cohort_quarter)
            & candidate[stay_column].eq(1)
            & candidate[f"requirement1_{side}"].eq(1)
        )
        firm_a, firm_b = "idA", "idB"
    else:
        condition = (
            candidate["event_type"].eq(event_type)
            & candidate["event_year"].eq(cohort_year)
            & candidate[stay_column].eq(1)
            & candidate["requirement1"].eq(1)
        )
        firm_a, firm_b = "FirmA", "FirmB"
    current = candidate.loc[condition]
    firms_a = set(current[firm_a].dropna().astype(str))
    firms_b = set(current[firm_b].dropna().astype(str))
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


def keep_available_ndcs(
    cohort: pd.DataFrame,
    first_seen_qtime: dict[str, int],
    cohort_year: int,
    first_seen_year_offset: int,
    first_seen_quarter: int,
    cohort_quarter: int | None = None,
) -> pd.DataFrame:
    """Keep NDCs first included by the cutoff, clipped to the observed data edge."""
    if cohort.empty:
        return cohort.copy()

    first_seen = cohort["ndc"].map(first_seen_qtime)
    if first_seen.isna().any():
        examples = cohort.loc[first_seen.isna(), "ndc"].drop_duplicates().head(10).tolist()
        raise KeyError(f"Cohort NDCs are missing from the first-seen lookup: {examples}")
    cutoff_quarter = cohort_quarter if cohort_quarter is not None else first_seen_quarter
    requested_cutoff = quarter_time(cohort_year + first_seen_year_offset, cutoff_quarter)
    available_qtime = cohort["year"].astype("int32") * 4 + cohort["quarter"].astype("int32")
    cutoff_qtime = max(requested_cutoff, int(available_qtime.min()))
    return cohort.loc[first_seen.le(cutoff_qtime)].copy()


def keep_quarterly_eligible_ndcs(
    cohort: pd.DataFrame,
    first_seen_qtime: dict[str, int],
    last_seen_qtime: dict[str, int],
    cohort_year: int,
    cohort_quarter: int,
    quarter_tags: list[str],
    first_seen: int,
    exit_restriction: int,
) -> pd.DataFrame:
    """Apply the configured event-quarter first/last-seen NDC restrictions."""
    if cohort.empty or (first_seen == 0 and exit_restriction == 0):
        return cohort.copy()
    if first_seen not in {0, 1} or exit_restriction not in {0, 1}:
        raise ValueError("first_seen and exit settings must be 0 or 1.")
    if first_seen == 0 and exit_restriction == 1:
        raise ValueError("The (first_seen=0, exit=1) sample is unsupported.")

    result = cohort.copy()
    first_seen_values = result["ndc"].map(first_seen_qtime)
    if first_seen_values.isna().any():
        examples = result.loc[first_seen_values.isna(), "ndc"].drop_duplicates().head(10).tolist()
        raise KeyError(f"Cohort NDCs are missing from the first-seen lookup: {examples}")
    event_qtime = quarter_time(cohort_year, cohort_quarter)
    result = result.loc[first_seen_values.le(event_qtime)].copy()

    if exit_restriction == 1 and not result.empty:
        last_seen_values = result["ndc"].map(last_seen_qtime)
        if last_seen_values.isna().any():
            examples = result.loc[last_seen_values.isna(), "ndc"].drop_duplicates().head(10).tolist()
            raise KeyError(f"Cohort NDCs are missing from the last-seen lookup: {examples}")
        final_year, final_quarter = quarter_sort_key(quarter_tags[-1])
        final_qtime = quarter_time(final_year, final_quarter)
        result = result.loc[last_seen_values.ge(final_qtime)].copy()
    return result


def validate_panel_treatment_flags(
    cohort: pd.DataFrame,
    event_type: str,
    side: str,
    cohort_year: int,
    expected_firms: set[str],
    cohort_quarter: int | None = None,
) -> None:
    """Ensure aggregated req1 flags match the movement table at event time."""
    event_column = output_event_column(event_type, side)
    entry = cohort.loc[
        cohort["year"].eq(cohort_year)
        & cohort["quarter"].eq(cohort_quarter or 1)
    ]
    observed = set(entry.loc[entry[event_column].eq(1), "boardname"].dropna().astype(str))
    universe = set(entry["boardname"].dropna().astype(str))
    expected = expected_firms & universe
    if observed != expected:
        raise ValueError(
            f"Req1 event mismatch for {event_type}, side {side}, "
            f"{canonical_year_q(cohort_year, cohort_quarter or 1)}. "
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
    quarter_tags: list[str],
    cohort_quarter: int | None = None,
) -> pd.DataFrame:
    """Add treated/sample/sharing flags for A and B without duplicating rows."""
    result = cohort.copy()
    universe = set(result["boardname"].dropna().astype(str))
    id_columns = ["ndc", "boardname"]

    for side in TREATMENT_GROUPS:
        side_lower = side.lower()
        treated = treated_firms(movement, event_type, side, cohort_year, cohort_quarter)
        validate_panel_treatment_flags(
            result,
            event_type,
            side,
            cohort_year,
            treated,
            cohort_quarter,
        )
        pure_event = pure_event_firms_in_window(
            movement,
            event_type,
            side,
            quarter_tags,
            int(cohort_quarter is not None),
        )
        excluded_counterparts = counterpart_only_firms(
            candidate,
            stay_column,
            event_type,
            side,
            cohort_year,
            cohort_quarter,
        )
        treated_boards = treated & universe
        controls = universe - pure_event - excluded_counterparts

        treated_column = f"treated_{side_lower}"
        sample_column = f"sample_{side_lower}"
        share_column = cohort_sharing_column(side)
        source_share_column = output_sharing_column(event_type, side)
        result[treated_column] = result["boardname"].isin(treated_boards).astype("int8")
        result[sample_column] = (
            result[treated_column].eq(1) | result["boardname"].isin(controls)
        ).astype("int8")

        entry_share = result.loc[
            result["year"].eq(cohort_year)
            & result["quarter"].eq(cohort_quarter or 1),
            [*id_columns, source_share_column],
        ].rename(columns={source_share_column: share_column})
        if entry_share.duplicated(id_columns).any():
            raise ValueError(
                f"Cohort-entry sharing lookup is not unique for {event_type}, {side}, {cohort_year}."
            )
        result = result.merge(entry_share, on=id_columns, how="left", validate="many_to_one")
        result[share_column] = result[share_column].fillna(0).astype("int8")
        result.loc[result[treated_column].eq(0), share_column] = np.int8(0)

    return result


def cohort_output_columns(quarter: int = 0) -> list[str]:
    """Return the intentionally lean cohort schema."""
    cohort_id_columns = (
        ["data_cohort_year", "data_cohort_quarter", "data_cohort_qtime"]
        if quarter else ["data_cohort"]
    )
    return [
        "ndc",
        "boardname",
        "year_q",
        "year",
        "quarter",
        *cohort_id_columns,
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
    first_seen_qtime: dict[str, int],
    last_seen_qtime: dict[str, int],
    first_seen_year_offset: int,
    first_seen_quarter: int,
    first_seen: int,
    exit_restriction: int,
    movement: pd.DataFrame,
    candidate: pd.DataFrame,
    stay_column: str,
    event_type: str,
    cohort_year: int,
    cohort_quarter: int | None = None,
) -> pd.DataFrame:
    """Build one combined-direction, balanced drug-firm cohort."""
    cohort = read_cohort_window(drug_quarter_paths, quarter_tags)
    cohort = keep_complete_drug_ids(cohort, expected_quarters=len(quarter_tags))
    if cohort_quarter is None:
        cohort = keep_available_ndcs(
            cohort,
            first_seen_qtime,
            cohort_year,
            first_seen_year_offset,
            first_seen_quarter,
            cohort_quarter,
        )
        cohort["data_cohort"] = np.int16(cohort_year)
    else:
        cohort = keep_quarterly_eligible_ndcs(
            cohort,
            first_seen_qtime,
            last_seen_qtime,
            cohort_year,
            cohort_quarter,
            quarter_tags,
            first_seen,
            exit_restriction,
        )
        cohort["data_cohort_year"] = np.int16(cohort_year)
        cohort["data_cohort_quarter"] = np.int8(cohort_quarter)
        cohort["data_cohort_qtime"] = np.int32(quarter_time(cohort_year, cohort_quarter))
    cohort = add_direction_flags(
        cohort,
        movement,
        candidate,
        stay_column,
        event_type,
        cohort_year,
        quarter_tags,
        cohort_quarter,
    )
    cohort = cohort.loc[cohort["sample_a"].eq(1) | cohort["sample_b"].eq(1)].copy()
    return (
        cohort[cohort_output_columns(int(cohort_quarter is not None))]
        .sort_values(["boardname", "ndc", "year", "quarter"])
        .reset_index(drop=True)
    )


def build_cohort_outputs(
    drug_quarter_paths: dict[str, Path],
    cohort_windows: dict[tuple[int, int | None], list[str]],
    first_seen_qtime: dict[str, int],
    last_seen_qtime: dict[str, int],
    first_seen_year_offset: int,
    first_seen_quarter: int,
    first_seen: int,
    exit_restriction: int,
    movement: pd.DataFrame,
    candidate: pd.DataFrame,
    stay_column: str,
    destination_dir: Path,
    specifications: list[tuple[str, int, int | None]],
) -> None:
    """Write annual combined cohorts or quarterly samples separately by direction."""
    progress = tqdm(specifications, desc="Building formulary cohorts", unit="cohort")
    for event_type, cohort_year, cohort_quarter in progress:
        cohort_tag = (
            canonical_year_q(cohort_year, cohort_quarter)
            if cohort_quarter is not None else str(cohort_year)
        )
        progress.set_postfix_str(f"{event_type}/{cohort_tag}")
        output_path = destination_dir / f"{event_type}_quarter_cohort_{cohort_tag}.csv"
        cohort = build_one_cohort(
            drug_quarter_paths,
            cohort_windows[(cohort_year, cohort_quarter)],
            first_seen_qtime,
            last_seen_qtime,
            first_seen_year_offset,
            first_seen_quarter,
            first_seen,
            exit_restriction,
            movement,
            candidate,
            stay_column,
            event_type,
            cohort_year,
            cohort_quarter,
        )
        if cohort_quarter is None:
            prepare_output_path(output_path, overwrite=True)
            progress.set_postfix_str(f"{event_type}/{cohort_tag}: writing cohort CSV")
            cohort.to_csv(output_path, index=False)
        else:
            for side in TREATMENT_GROUPS:
                side_lower = side.lower()
                side_cohort = cohort.loc[cohort[f"sample_{side_lower}"].eq(1)]
                side_path = destination_dir / side / output_path.name
                if not side_cohort[f"treated_{side_lower}"].eq(1).any():
                    if side_path.exists():
                        side_path.unlink()
                    print(
                        f"Skipping {event_type}/{cohort_tag}/{side}: "
                        "no treated drug-company ids remain after panel and first-seen filters."
                    )
                    continue
                prepare_output_path(side_path, overwrite=True)
                progress.set_postfix_str(f"{event_type}/{cohort_tag}/{side}: writing cohort CSV")
                side_cohort.to_csv(side_path, index=False)
        del cohort
        gc.collect()


def clear_quarterly_cohort_outputs(destination_dir: Path) -> None:
    """Remove only stale event cohort CSVs from one maintained sample folder."""
    for side in TREATMENT_GROUPS:
        side_dir = destination_dir / side
        if not side_dir.exists():
            continue
        for event_type in EVENT_TYPES:
            for path in side_dir.glob(f"{event_type}_quarter_cohort_*.csv"):
                path.unlink()


# ========================== OUTPUT DISPATCH ==========================


def main() -> None:
    """Build slim drug-quarter panels, then construct all requested cohorts."""
    (
        quarter,
        chunksize,
        window_pre,
        window_post,
        quarter_pre_periods,
        quarter_post_periods,
        time_shift,
        rebuild_drug_quarter_panels,
        first_seen_year_offset,
        first_seen_quarter,
        sample_restrictions,
    ) = validate_config(RUN_CONFIG)
    source_dir = quarter_input_dir(time_shift, quarter)
    drug_output_dir = drug_quarter_output_dir(time_shift, quarter)
    movement, candidate, stay_column = load_event_sources(quarter)
    specifications = cohort_specifications(movement, quarter)
    if not specifications:
        raise ValueError("No req1 event cohorts were found for the configured years.")
    sources = available_quarter_paths(source_dir)
    quarter_tags, cohort_windows = required_quarters(
        sources,
        window_pre,
        window_post,
        specifications,
        quarter,
        quarter_pre_periods,
        quarter_post_periods,
    )
    first_seen_qtime = load_first_seen_lookup(first_seen_path(time_shift, quarter))
    last_seen_qtime = (
        load_last_seen_lookup(last_seen_path(time_shift, quarter)) if quarter else {}
    )
    if rebuild_drug_quarter_panels:
        drug_quarter_paths = build_drug_quarter_panels(
            quarter_tags, sources, drug_output_dir, chunksize, quarter
        )
    else:
        drug_quarter_paths = existing_drug_quarter_panels(quarter_tags, drug_output_dir)
    action = "Saved" if rebuild_drug_quarter_panels else "Reused"
    print(f"{action} drug-quarter panels at: {drug_output_dir}")
    if quarter:
        for first_seen, exit_restriction in sample_restrictions:
            cohort_destination_dir = cohort_output_dir(
                time_shift,
                first_seen_year_offset,
                first_seen_quarter,
                quarter,
                first_seen,
                exit_restriction,
            )
            clear_quarterly_cohort_outputs(cohort_destination_dir)
            build_cohort_outputs(
                drug_quarter_paths,
                cohort_windows,
                first_seen_qtime,
                last_seen_qtime,
                first_seen_year_offset,
                first_seen_quarter,
                first_seen,
                exit_restriction,
                movement,
                candidate,
                stay_column,
                cohort_destination_dir,
                specifications,
            )
            print(
                "Saved separate A/B cohorts for "
                f"first_seen={first_seen}, exit={exit_restriction} to: "
                f"{cohort_destination_dir}"
            )
    else:
        cohort_destination_dir = cohort_output_dir(
            time_shift,
            first_seen_year_offset,
            first_seen_quarter,
            quarter,
        )
        build_cohort_outputs(
            drug_quarter_paths,
            cohort_windows,
            first_seen_qtime,
            last_seen_qtime,
            first_seen_year_offset,
            first_seen_quarter,
            1,
            0,
            movement,
            candidate,
            stay_column,
            cohort_destination_dir,
            specifications,
        )
        print(f"Saved combined-direction cohorts to: {cohort_destination_dir}")


if __name__ == "__main__":
    main()
