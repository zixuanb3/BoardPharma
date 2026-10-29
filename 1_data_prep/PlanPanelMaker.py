r"""
Purpose:
Build regression-ready cohort panels from quarterly formulary panels and plan
benefit data. Quarterly mode is fixed at the CPS (contract-plan-segment) level,
uses actual event quarters, and collapses complete CPS formulary histories
within each cohort before the NDC merge.

Process:
1. In quarterly mode, read beneficiary cost data once, shift its source quarter
   onto formulary time, and derive both the CPS-quarter-formulary crosswalk and
   CPS-quarter-tier copay. County/state repetitions are removed before copay is
   averaged equally across the observed daily DAYS_SUPPLY values. This is the
   one place to change the DAYS_SUPPLY aggregation specification later.
2. Keep plan units that cover every required quarter of each 2020--2024
   cohort. Legacy mode samples them; path-weighted mode groups equal complete
   formulary histories and records the number of represented CPS units.
3. Read quarterly formulary panels, attach outcomes and events, and compare raw
   tier with the immediately preceding calendar quarter.
4. Apply req1/Not treated-control rules and stream sampled histories into cohort
   CSVs. With path_average=1, also accumulate all-history weighted sums from the
   same expanded data and write one NDC-company-quarter row per cohort.

Input:
- D:/pharma/formulary/beneficiary_cost_with_copay.csv (quarterly mode)
- D:/pharma/copay_avg_with_prefer.csv (only when prefer=1)
- data/formulary_panel_by_time[/shift_q1]/formulary_panel_YYYYQX.csv
- data/formulary_panel_quarter_by_time[/shift_q1]/formulary_panel_YYYYQX.csv
- data/event_tables/movement_table_formulary_quarter_narrow.csv
- data/event_tables/movement_event_candidates_formulary_quarter_narrow.csv
- InterimData/merged_plan_information.csv (annual legacy mode only)
- D:/task1_expanded_brand_panel/task1_expanded_brand_panel.csv
  (annual legacy mode only)
- data/directory/Monthly_Report_By_Contract_YYYY_MM.csv
- data/formulary_metadata/ndc_first_seen[_shift_q1].csv

Output:
- data/formulary_plan_cohort_data/event/req1/Not/{shift}/{level}/
  {event}_plan_quarter_cohort_{year}.csv
 - D:/BoardPharma/data/formulary_path_cohort_data/event/req1/Not/{shift}/{level}/
  {event}_path_quarter_cohort_{year}.csv
- data/formulary_path_cohort_data_quarter/event/req1/Not/{shift}/{level}/
  {event}_path_quarter_cohort_{year}Q{quarter}.csv
- path_average=1 additionally writes the averaged schema under path_average/
  within the corresponding path cohort directory and shared checkpoint.
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, cast

import numpy as np
import pandas as pd
from tqdm.auto import tqdm

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

# Configure project directory paths
CURRENT_PATH = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_PATH.parent.parent
DATA_ROOT = PROJECT_ROOT / "data"
PLAN_INFO_PATH = PROJECT_ROOT / "InterimData" / "merged_plan_information.csv"
LEGACY_EXPANDED_FORMULARY_PATH = Path(
    r"D:\task1_expanded_brand_panel\task1_expanded_brand_panel.csv"
)
BENEFICIARY_COPAY_PATH = Path(
    r"D:\pharma\formulary\beneficiary_cost_with_copay.csv"
)
LEGACY_COPAY_PATH = PROJECT_ROOT / "InterimData" / "aggregated_copay_by_tier.csv"
PREFER_PATH = Path(r"D:\pharma\copay_avg_with_prefer.csv")
FORMULARY_PANEL_ROOT = DATA_ROOT / "formulary_panel_by_time"
EVENT_TABLE_DIR = DATA_ROOT / "event_tables"
DIRECTORY_ROOT = DATA_ROOT / "directory"
FIRST_SEEN_ROOT = DATA_ROOT / "formulary_metadata"

COHORT_ROOT = DATA_ROOT / "formulary_plan_cohort_data" / "event" / "req1" / "Not"
PATH_WEIGHTED_DATA_ROOT = Path(r"D:\BoardPharma\data")
PATH_WEIGHTED_COHORT_ROOT = (
    PATH_WEIGHTED_DATA_ROOT / "formulary_path_cohort_data" / "event" / "req1" / "Not"
)
QUARTER_PATH_WEIGHTED_COHORT_ROOT = (
    DATA_ROOT / "formulary_path_cohort_data_quarter" / "event" / "req1" / "Not"
)

YEAR_Q_PATTERN = re.compile(r"^([12][0-9]{3})Q([1-4])$")
MONTHLY_PATTERN = re.compile(
    r"Monthly_Report_By_Contract_([12][0-9]{3})_([0-9]{2})\.csv$"
)
FORMULARY_FILE_PATTERN = re.compile(r"^formulary_panel_([12][0-9]{3}Q[1-4])\.csv$")

EVENT_TYPES = (
    "to_B_not_in_A",
    "to_B_still_in_A",
    "interlock_dissolution",
)
TREATMENT_GROUPS = ("B","A")
ATC_SHARING_LEVELS = (3,)
COHORT_YEARS = (2020, 2021, 2022, 2023, 2024)


# ========================== USER CONFIG ==========================
# quarter:
# - 0 preserves the annual event cohorts and their original output location.
# - 1 constructs event-quarter path cohorts with relative quarters -4 through +7.
#   This mode requires path_weighted_mode=1 and uses a separate output directory.
# level:
# - Quarterly mode is intentionally fixed at "plan": one analysis unit is one
#   CPS (CONTRACT_ID x PLAN_ID x SEGMENT_ID). State/county rows in the benefit
#   source do not create extra path weight.
# - Annual legacy mode retains its plan/state/county options.
#
# formulary_time_shift_quarters:
# - 0 reads the unshifted formulary panel and leaves all other quarterly
#   inputs on their source quarter.
# - 1 reads the shift_q1 formulary panel and shifts plan, copay, and directory
#   source quarters forward by one.  Event columns are already aligned in the
#   selected formulary panel and are never shifted a second time.
#
# chunksize/max_expanded_rows_per_batch:
# - chunksize controls raw CSV reads. Raw rows are then split by FORMULARY_ID
#   so one expansion batch stays near max_expanded_rows_per_batch.
# sample_fraction/random_seed:
# - sample_fraction is the share of balanced plan units retained separately
#   within each cohort year, before plan-drug expansion.  These two settings
#   apply only when path_weighted_mode=0.
# - random_seed makes the cohort-specific samples reproducible.
# path_sample_fraction/path_sample_seed:
# - In path-weighted mode, sample distinct history_id paths separately within
#   each event-by-cohort output before any rows are written.  Every quarter and
#   NDC on a selected path is retained, and n_path/n_path_copay keep their full
#   CPS counts for regression weighting.
# - path_sample_seed makes these event-cohort path samples reproducible.
# path_weighted_mode:
# - 0 preserves the legacy sampled CPS-by-NDC pipeline exactly.
# - 1 keeps every balanced CPS, collapses identical full formulary histories,
#   and writes history-by-NDC panels with path counts as regression weights.
# path_average:
# - 0 preserves the path-weighted output and sampling behavior.
# - 1 requires path_weighted_mode=1 and adds a full-history weighted-average
#   output without replacing the sampled history-level path output. included
#   uses every history; tier_raw and avg_copay_amt use only history-NDC pairs
#   included in every required cohort quarter. tier_raw uses n_path and copay
#   uses n_path_copay, whose zero weights naturally leave copay out of its mean.
#   Averaged files omit history_id and path weights and are written under the
#   path_average subdirectory.
# prefer:
# - 0 (default) does not read a prefer file and writes missing prefer values.
# - 1 reads PREFER_PATH and builds outcome-specific n_path_prefer weights.
# - The compact quarterly path output supports prefer=0; prefer=1 remains
#   available only for the annual path schema.
# Quarterly copay has no switch: it always comes from BENEFICIARY_COPAY_PATH.
RUN_CONFIG = {
    "quarter": 1,
    "level": "plan",
    "formulary_time_shift_quarters": 1,
    "sample_fraction": 0.01,
    "random_seed": 20250810,
    "path_sample_fraction": 0.1,
    "path_sample_seed": 20260818,
    "path_weighted_mode": 1,
    "path_average": 1,
    "prefer": 0,
    "chunksize": 2_000_000,
    "max_expanded_rows_per_batch": 2_000_000,
}
# ===============================================================


# ========================== ANALYSIS SPEC ==========================


@dataclass(frozen=True)
class AnalysisSpec:
    """Describe the plan geography retained in one analysis panel."""

    level: str
    geography_columns: tuple[str, ...]
    retained_plan_columns: tuple[str, ...]

    @property
    def plan_unit_columns(self) -> list[str]:
        """Return the plan component of the analysis unit before NDC expansion."""
        return [
            "contract_id",
            "plan_id",
            "segment_id",
            *self.geography_columns,
        ]

    @property
    def plan_input_columns(self) -> list[str]:
        """Return the exact merged-plan columns required for this level."""
        return ["YEAR_Q", *self.retained_plan_columns]


@dataclass(frozen=True)
class CohortSample:
    """Store firm rules for one event-year cohort."""

    event_type: str
    cohort_year: int
    required_quarters: tuple[str, ...]
    treated_a: frozenset[str]
    treated_b: frozenset[str]
    excluded_controls_a: frozenset[str]
    excluded_controls_b: frozenset[str]


@dataclass(frozen=True)
class PathCohort:
    """Store one cohort's complete CPS histories after path compression."""

    cohort_year: int
    required_quarters: tuple[str, ...]
    member_crosswalk: pd.DataFrame
    history_quarters: pd.DataFrame


@dataclass(frozen=True)
class QuarterEventSources:
    """Store normalized quarterly movement inputs used by PlanPanelMaker."""

    movement: pd.DataFrame
    candidates: pd.DataFrame
    stay_column: str


ANALYSIS_SPECS = {
    "plan": AnalysisSpec(
        level="plan",
        geography_columns=(),
        retained_plan_columns=(
            "CONTRACT_ID",
            "PLAN_ID",
            "SEGMENT_ID",
            "FORMULARY_ID",
        ),
    ),
    "state": AnalysisSpec(
        level="state",
        geography_columns=("state",),
        retained_plan_columns=(
            "CONTRACT_ID",
            "PLAN_ID",
            "SEGMENT_ID",
            "FORMULARY_ID",
            "STATE",
        ),
    ),
    "county": AnalysisSpec(
        level="county",
        geography_columns=("county_code",),
        retained_plan_columns=(
            "CONTRACT_ID",
            "PLAN_ID",
            "SEGMENT_ID",
            "FORMULARY_ID",
            "STATE",
            "COUNTY_CODE",
            "MA_REGION_CODE",
            "PDP_REGION_CODE",
        ),
    ),
}


# ========================== COLUMN HELPERS ==========================


def event_column(event_type: str, side: str) -> str:
    """Return one event field already stored in the formulary panel."""
    return f"event_{event_type}_{side}"


EVENT_COLUMNS = [
    event_column(event_type, side)
    for event_type in EVENT_TYPES
    for side in TREATMENT_GROUPS
]
SHARING_COLUMNS = [
    f"{event_column(event_type, side)}_sharingATC{atc_level}"
    for atc_level in ATC_SHARING_LEVELS
    for event_type in EVENT_TYPES
    for side in TREATMENT_GROUPS
]
ANNUAL_RAW_FORMULARY_COLUMNS = [
    "YEAR_Q",
    "FORMULARY_ID",
    "NDC",
    "BoardName",
    "ATC3",
    "ATC4",
    "included",
    "tier_raw",
    "max_tier",
    *EVENT_COLUMNS,
    *SHARING_COLUMNS,
]

QUARTER_RAW_FORMULARY_COLUMNS = [
    "YEAR_Q",
    "FORMULARY_ID",
    "NDC",
    "id",
    "ATC3",
    "included",
    "tier_raw",
    "max_tier",
    *EVENT_COLUMNS,
    *SHARING_COLUMNS,
]


# ========================== VALIDATION HELPERS ==========================


def shift_label(shift_quarters: int) -> str:
    """Return the stable directory label for a timing specification."""
    return f"shift_q{shift_quarters:+d}".replace("+", "")


def normalize_text(values: pd.Series, uppercase: bool = False) -> pd.Series:
    """Trim string values and convert blank cells to missing."""
    result = values.astype("string").str.strip()
    result = result.mask(result.eq(""), pd.NA)
    return result.str.upper() if uppercase else result


def normalize_numeric_identifier(values: pd.Series) -> pd.Series:
    """Normalize numeric-like identifiers, making every all-zero value '0'."""
    result = normalize_text(values)
    result = result.str.replace(r"^([0-9]+)\.0$", r"\1", regex=True)
    numeric = result.str.fullmatch(r"[0-9]+", na=False)
    stripped = result.loc[numeric].str.lstrip("0")
    result.loc[numeric] = stripped.mask(stripped.eq(""), "0")
    return result


def normalize_company_id(values: pd.Series, source_name: str) -> pd.Series:
    """Return positive integer company ids as canonical strings."""
    numeric = pd.to_numeric(values, errors="coerce")
    invalid = numeric.isna() | numeric.le(0) | numeric.ne(numeric.round())
    if invalid.any():
        examples = values.loc[invalid].drop_duplicates().head(10).tolist()
        raise ValueError(
            f"{source_name} has invalid company ids. Examples: {examples}"
        )
    return numeric.astype("int64").astype("string")


def normalize_contract(values: pd.Series) -> pd.Series:
    """Normalize contract IDs without changing letter prefixes or zero padding."""
    result = normalize_text(values, uppercase=True)
    return result.str.replace(r"^([0-9]+)\.0$", r"\1", regex=True)


def normalize_county(values: pd.Series) -> pd.Series:
    """Normalize numeric county FIPS codes while preserving five-digit padding."""
    result = normalize_text(values)
    result = result.str.replace(r"^([0-9]+)\.0$", r"\1", regex=True)
    numeric = result.str.fullmatch(r"[0-9]+", na=False)
    result.loc[numeric] = result.loc[numeric].str.zfill(5)
    return result


def normalize_region(values: pd.Series) -> pd.Series:
    """Normalize numeric MA/PDP region codes to two-character strings."""
    result = normalize_text(values, uppercase=True)
    result = result.str.replace(r"^([0-9]+)\.0$", r"\1", regex=True)
    numeric = result.str.fullmatch(r"[0-9]+", na=False)
    result.loc[numeric] = result.loc[numeric].str.zfill(2)
    return result


def normalize_year_q(values: pd.Series, source_name: str) -> pd.Series:
    """Normalize YEAR_Q values such as '2024 Q1' to compact YYYYQX tags."""
    result = normalize_text(values, uppercase=True).str.replace(" ", "", regex=False)
    invalid = result.notna() & ~result.str.fullmatch(YEAR_Q_PATTERN, na=False)
    if invalid.any():
        examples = values.loc[invalid].drop_duplicates().head(10).tolist()
        raise ValueError(f"{source_name} has invalid YEAR_Q values: {examples}")
    return result


def quarter_key(year_q: str) -> tuple[int, int]:
    """Return a sortable year-quarter pair for one compact period tag."""
    match = YEAR_Q_PATTERN.fullmatch(year_q)
    if match is None:
        raise ValueError(f"Invalid quarter tag: {year_q}")
    return int(match.group(1)), int(match.group(2))


def quarter_time(values: pd.Series) -> pd.Series:
    """Return integer quarter indices for validated compact YEAR_Q values."""
    year = pd.to_numeric(values.str.slice(0, 4), errors="raise").astype("int32")
    quarter = pd.to_numeric(values.str.slice(-1), errors="raise").astype("int32")
    return year * 4 + quarter


def cohort_year_quarter(cohort_key: int, quarter: int) -> tuple[int, int]:
    """Decode a quarterly cohort key; annual keys remain calendar years."""
    if not quarter:
        return cohort_key, 1
    return (cohort_key - 1) // 4, (cohort_key - 1) % 4 + 1


def shift_year_q(year_q: str, shift_quarters: int) -> str:
    """Shift one compact quarter tag by the configured number of quarters."""
    year, quarter = quarter_key(year_q)
    shifted = year * 4 + quarter + shift_quarters
    if shifted <= 0:
        raise ValueError(f"Cannot shift {year_q} by {shift_quarters} quarters.")
    shifted_year = (shifted - 1) // 4
    shifted_quarter = (shifted - 1) % 4 + 1
    return f"{shifted_year}Q{shifted_quarter}"


def previous_year_q(year_q: str) -> str:
    """Return the immediately preceding calendar-quarter tag."""
    return shift_year_q(year_q, -1)


def representative_month(source_year_q: str) -> str:
    """Apply the project rule Q1=March, Q2=June, Q3=September, Q4=next January."""
    year, quarter = quarter_key(source_year_q)
    if quarter == 1:
        return f"{year}_03"
    if quarter == 2:
        return f"{year}_06"
    if quarter == 3:
        return f"{year}_09"
    return f"{year + 1}_01"


def prepare_output_path(path: Path) -> None:
    """Create an output parent and replace only the explicit target file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()


def append_path_build_log(log_path: Path, message: str) -> None:
    """Append one timestamped, persistent path-build status line."""
    timestamp = datetime.now().isoformat(timespec="seconds")
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"{timestamp} {message}\n")


def write_path_checkpoint(
    checkpoint_path: Path,
    path_completed_quarters: list[str],
    average_completed_quarters: list[str],
    ordered_quarters: list[str],
    output_paths: dict[tuple[str, int], Path],
    output_rows: dict[tuple[str, int], int],
    average_output_paths: dict[tuple[str, int], Path],
    average_output_rows: dict[tuple[str, int], int],
    spec: AnalysisSpec,
    time_shift: int,
    quarter: int = 0,
    prefer_mode: int = 0,
    path_sample_fraction: float = 1.0,
    path_sample_seed: int = 0,
    path_average: int = 0,
) -> None:
    """Atomically record the last fully committed quarter and output sizes."""
    has_average = bool(average_output_paths)
    payload = {
        "version": 4 if has_average else 3,
        "level": spec.level,
        "time_shift": time_shift,
        "prefer": prefer_mode,
        "path_sample_fraction": path_sample_fraction,
        "path_sample_seed": path_sample_seed,
        "schemas": path_output_schemas(
            output_paths, average_output_paths, quarter
        ),
        "ordered_quarters": ordered_quarters,
        "file_sizes": {
            label: path.stat().st_size if path.exists() else 0
            for label, path in tracked_path_outputs(
                output_paths, average_output_paths
            ).items()
        },
        "output_rows": (
            {
                **{
                    f"path/{output_paths[key].name}": int(count)
                    for key, count in output_rows.items()
                },
                **{
                    f"path_average/{average_output_paths[key].name}": int(count)
                    for key, count in average_output_rows.items()
                },
            }
            if has_average
            else {
                output_paths[key].name: int(count)
                for key, count in output_rows.items()
            }
        ),
    }
    if has_average:
        payload.update(
            path_average=path_average,
            path_completed_quarters=path_completed_quarters,
            average_completed_quarters=average_completed_quarters,
        )
    else:
        payload["completed_quarters"] = path_completed_quarters
    temporary_path = checkpoint_path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary_path.replace(checkpoint_path)


def restore_checkpoint_outputs(
    tracked_paths: dict[str, Path],
    saved_sizes: dict[str, object],
    saved_rows: dict[str, object],
    validate_only: bool = False,
) -> tuple[dict[str, bool], dict[str, int]]:
    """Roll tracked files back to checkpoint sizes and restore row counters."""
    headers: dict[str, bool] = {}
    rows: dict[str, int] = {}
    for label, path in tracked_paths.items():
        if label not in saved_sizes or label not in saved_rows:
            raise ValueError(f"Checkpoint is missing size or row count for {path}.")
        expected_size = int(saved_sizes.get(label, 0))
        actual_size = path.stat().st_size if path.exists() else 0
        if actual_size < expected_size:
            raise ValueError(
                f"Output {path} is smaller than its checkpoint size "
                f"({actual_size:,} < {expected_size:,})."
            )
        headers[label] = expected_size > 0
        rows[label] = int(saved_rows.get(label, 0))
    # Validate every file before rolling back any partially written output.
    if not validate_only:
        for label, path in tracked_paths.items():
            expected_size = int(saved_sizes[label])
            actual_size = path.stat().st_size if path.exists() else 0
            if path.exists() and actual_size == expected_size:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("r+b" if path.exists() else "wb") as output_file:
                output_file.truncate(expected_size)
    return headers, rows


def validate_completed_quarters(
    completed: list[str], ordered: list[str], label: str,
) -> None:
    """Require one output's committed quarters to be a chronological prefix."""
    if completed != ordered[: len(completed)]:
        raise ValueError(f"{label} completed quarters are not a chronological prefix.")


def initialize_path_outputs(
    checkpoint_path: Path,
    ordered_quarters: list[str],
    output_paths: dict[tuple[str, int], Path],
    average_output_paths: dict[tuple[str, int], Path],
    spec: AnalysisSpec,
    time_shift: int,
    quarter: int = 0,
    prefer_mode: int = 0,
    path_sample_fraction: float = 1.0,
    path_sample_seed: int = 0,
    path_average: int = 0,
    validate_only: bool = False,
) -> tuple[
    list[str],
    list[str],
    dict[tuple[str, int], bool],
    dict[tuple[str, int], int],
    dict[tuple[str, int], bool],
    dict[tuple[str, int], int],
]:
    """Start fresh or roll partial output back to the last completed quarter."""
    if not checkpoint_path.exists():
        if not validate_only:
            for path in tracked_path_outputs(output_paths, average_output_paths).values():
                prepare_output_path(path)
        return (
            [],
            [],
            {key: False for key in output_paths},
            {key: 0 for key in output_paths},
            {key: False for key in average_output_paths},
            {key: 0 for key in average_output_paths},
        )

    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    migrate_legacy = bool(average_output_paths) and checkpoint.get("version") == 3
    checkpoint_average_paths = {} if migrate_legacy else average_output_paths
    expected = {
        "version": 3 if migrate_legacy else (4 if average_output_paths else 3),
        "level": spec.level,
        "time_shift": time_shift,
        "prefer": prefer_mode,
        "path_sample_fraction": path_sample_fraction,
        "path_sample_seed": path_sample_seed,
        "schemas": path_output_schemas(
            output_paths, checkpoint_average_paths, quarter
        ),
        "ordered_quarters": ordered_quarters,
    }
    if path_average and not migrate_legacy:
        expected["path_average"] = path_average
    mismatched = [
        key for key, value in expected.items()
        if key != "schemas" and checkpoint.get(key) != value
    ]
    saved_schemas = checkpoint.get("schemas", {})
    expected_schemas = expected["schemas"]
    # A v3 build may include cohorts with no treated firms in the current run.
    # Keep their files intact; require every active output's columns to match.
    schemas_match = isinstance(saved_schemas, dict) and (
        all(saved_schemas.get(name) == columns for name, columns in expected_schemas.items())
        if migrate_legacy
        else saved_schemas == expected_schemas
    )
    if not schemas_match:
        mismatched.append("schemas")
    if mismatched:
        raise ValueError(
            "Path checkpoint is incompatible with the current build: "
            f"{mismatched}. Remove {checkpoint_path} only if a fresh rebuild is intended."
        )

    for label, path in tracked_path_outputs(
        output_paths, checkpoint_average_paths
    ).items():
        saved_size = int(checkpoint.get("file_sizes", {}).get(label, 0))
        if saved_size > 0 and path.exists():
            with path.open("r", encoding="utf-8-sig", newline="") as output_file:
                header = output_file.readline().rstrip("\r\n").split(",")
            if header != expected["schemas"][label]:
                raise ValueError(f"Actual CSV header does not match checkpoint for {path}.")

    saved_sizes = checkpoint.get("file_sizes", {})
    saved_rows = checkpoint.get("output_rows", {})
    if migrate_legacy:
        path_completed = [
            str(value) for value in checkpoint.get("completed_quarters", [])
        ]
        average_completed: list[str] = []
        validate_completed_quarters(path_completed, ordered_quarters, "Path")
        tracked_headers, tracked_rows = restore_checkpoint_outputs(
            {path.name: path for path in output_paths.values()},
            saved_sizes,
            saved_rows,
            validate_only,
        )
        if not validate_only:
            for path in average_output_paths.values():
                prepare_output_path(path)
        path_prefix = ""
    else:
        if average_output_paths:
            path_completed = [
                str(value)
                for value in checkpoint.get("path_completed_quarters", [])
            ]
            average_completed = [
                str(value)
                for value in checkpoint.get("average_completed_quarters", [])
            ]
        else:
            path_completed = [
                str(value) for value in checkpoint.get("completed_quarters", [])
            ]
            average_completed = []
        validate_completed_quarters(path_completed, ordered_quarters, "Path")
        validate_completed_quarters(average_completed, ordered_quarters, "Path-average")
        tracked_headers, tracked_rows = restore_checkpoint_outputs(
            tracked_path_outputs(output_paths, average_output_paths),
            saved_sizes,
            saved_rows,
            validate_only,
        )
        path_prefix = "path/" if average_output_paths else ""

    headers_written = {
        key: tracked_headers[f"{path_prefix}{path.name}"]
        for key, path in output_paths.items()
    }
    output_rows = {
        key: tracked_rows[f"{path_prefix}{path.name}"]
        for key, path in output_paths.items()
    }
    average_headers_written = {
        key: (
            False
            if migrate_legacy
            else tracked_headers[f"path_average/{path.name}"]
        )
        for key, path in average_output_paths.items()
    }
    average_output_rows = {
        key: (
            0
            if migrate_legacy
            else tracked_rows[f"path_average/{path.name}"]
        )
        for key, path in average_output_paths.items()
    }
    return (
        path_completed,
        average_completed,
        headers_written,
        output_rows,
        average_headers_written,
        average_output_rows,
    )


def analysis_spec(config: dict[str, object]) -> AnalysisSpec:
    """Validate and return the selected plan, state, or county analysis level."""
    level = str(config["level"]).strip().lower()
    if level not in ANALYSIS_SPECS:
        raise ValueError(f"level must be one of {sorted(ANALYSIS_SPECS)}; found {level!r}.")
    return ANALYSIS_SPECS[level]


def validate_config(
    config: dict[str, object],
) -> tuple[AnalysisSpec, int, float, int, float, int, int, int, int, int, int, int]:
    """Validate the small runtime configuration."""
    spec = analysis_spec(config)
    time_shift = int(str(config["formulary_time_shift_quarters"]))
    sample_fraction = float(str(config["sample_fraction"]))
    random_seed = int(str(config["random_seed"]))
    path_average = int(str(config.get("path_average", 0)))
    path_sample_fraction = float(str(config["path_sample_fraction"]))
    path_sample_seed = int(str(config["path_sample_seed"]))
    path_weighted_mode = int(str(config["path_weighted_mode"]))
    chunksize = int(str(config["chunksize"]))
    max_expanded_rows = int(str(config["max_expanded_rows_per_batch"]))
    quarter = int(str(config.get("quarter", 0)))
    prefer_mode = int(str(config.get("prefer", 0)))
    if quarter not in {0, 1}:
        raise ValueError("quarter must be 0 or 1.")
    if quarter and spec.level != "plan":
        raise ValueError(
            "Quarterly PlanPanelMaker is fixed at level='plan' (one unit per CPS)."
        )
    if prefer_mode not in {0, 1}:
        raise ValueError("prefer must be 0 or 1.")
    if quarter and prefer_mode != 0:
        raise ValueError(
            "The compact quarterly path schema requires prefer=0."
        )
    if time_shift not in {0, 1}:
        raise ValueError("formulary_time_shift_quarters must be 0 or 1.")
    if not 0 < sample_fraction <= 1:
        raise ValueError("sample_fraction must be greater than 0 and no greater than 1.")
    if random_seed < 0:
        raise ValueError("random_seed must be nonnegative.")
    if not 0 < path_sample_fraction <= 1:
        raise ValueError(
            "path_sample_fraction must be greater than 0 and no greater than 1."
        )
    if path_sample_seed < 0:
        raise ValueError("path_sample_seed must be nonnegative.")
    if path_weighted_mode not in {0, 1}:
        raise ValueError("path_weighted_mode must be 0 or 1.")
    if path_average not in {0, 1}:
        raise ValueError("path_average must be 0 or 1.")
    if path_average and path_weighted_mode != 1:
        raise ValueError("path_average=1 requires path_weighted_mode=1.")
    if chunksize < 1:
        raise ValueError("chunksize must be positive.")
    if max_expanded_rows < 1:
        raise ValueError("max_expanded_rows_per_batch must be positive.")
    return (
        spec,
        time_shift,
        sample_fraction,
        random_seed,
        path_sample_fraction,
        path_sample_seed,
        path_weighted_mode,
        chunksize,
        max_expanded_rows,
        quarter,
        prefer_mode,
        path_average,
    )


# ========================== INPUT INVENTORY ==========================


def formulary_input_dir(time_shift: int, quarter: int = 0) -> Path:
    """Return the raw formulary quarter directory for one timing specification."""
    root = FORMULARY_PANEL_ROOT.with_name("formulary_panel_quarter_by_time") if quarter else FORMULARY_PANEL_ROOT
    return root if time_shift == 0 else root / shift_label(time_shift)


def quarter_panel_paths(source_dir: Path) -> dict[str, Path]:
    """Inventory and validate available quarter-organized formulary panels."""
    paths: dict[str, Path] = {}
    for path in source_dir.glob("formulary_panel_????Q?.csv"):
        match = FORMULARY_FILE_PATTERN.fullmatch(path.name)
        if match is None:
            continue
        tag = match.group(1)
        if tag in paths:
            raise ValueError(f"Duplicate formulary panel for {tag}: {paths[tag]} and {path}")
        paths[tag] = path
    if not paths:
        raise FileNotFoundError(f"No formulary quarter panels found in {source_dir}")
    return paths


def load_quarter_event_sources() -> QuarterEventSources:
    """Load quarterly req1 movement inputs using company id/idA/idB columns.

    PlanPanelMaker owns this quarterly adapter so it does not depend on, modify,
    or require a separate run of FormularyCohortPanelMaker.
    """
    movement_path = EVENT_TABLE_DIR / "movement_table_formulary_quarter_narrow.csv"
    candidate_path = (
        EVENT_TABLE_DIR / "movement_event_candidates_formulary_quarter_narrow.csv"
    )
    movement = pd.read_csv(movement_path, dtype="string")
    candidates = pd.read_csv(candidate_path, dtype="string")
    stay_column = "stay_8_quarters"

    movement_required = {
        "id", "event_type", "firm_type", "year", "quarter", "req1"
    }
    candidate_required = {
        "idA", "idB", "event_type", "event_year", "event_quarter",
        stay_column, "requirement1",
    }
    for path, data, required in (
        (movement_path, movement, movement_required),
        (candidate_path, candidates, candidate_required),
    ):
        missing = sorted(required - set(data.columns))
        if missing:
            raise KeyError(f"{path.name} is missing columns: {missing}")

    movement["id"] = normalize_company_id(movement["id"], movement_path.name)
    movement["event_type"] = normalize_text(movement["event_type"])
    movement["firm_type"] = normalize_text(
        movement["firm_type"], uppercase=True
    )
    movement["year"] = pd.to_numeric(
        movement["year"], errors="raise"
    ).astype("int16")
    movement["quarter"] = pd.to_numeric(
        movement["quarter"], errors="raise"
    ).astype("int8")
    movement["req1"] = pd.to_numeric(
        movement["req1"], errors="raise"
    ).astype("int8")
    if not movement["quarter"].isin((1, 2, 3, 4)).all():
        raise ValueError(f"{movement_path.name} has invalid event quarters.")
    if not movement["req1"].isin((0, 1)).all():
        raise ValueError(f"{movement_path.name} has invalid req1 flags.")

    for column in ("idA", "idB"):
        candidates[column] = normalize_company_id(
            candidates[column], candidate_path.name
        )
    candidates["event_type"] = normalize_text(candidates["event_type"])
    candidates["event_year"] = pd.to_numeric(
        candidates["event_year"], errors="raise"
    ).astype("int16")
    candidates["event_quarter"] = pd.to_numeric(
        candidates["event_quarter"], errors="raise"
    ).astype("int8")
    if not candidates["event_quarter"].isin((1, 2, 3, 4)).all():
        raise ValueError(f"{candidate_path.name} has invalid event quarters.")
    for column in (stay_column, "requirement1"):
        candidates[column] = pd.to_numeric(
            candidates[column], errors="raise"
        ).astype("int8")
        if not candidates[column].isin((0, 1)).all():
            raise ValueError(f"{candidate_path.name} has invalid {column} flags.")
    return QuarterEventSources(movement, candidates, stay_column)


def quarter_cohort_specifications(
    movement: pd.DataFrame,
) -> list[tuple[str, int, int]]:
    """Return observed req1 event-type/year/quarter cohort specifications."""
    eligible = movement.loc[
        movement["req1"].eq(1), ["event_type", "year", "quarter"]
    ]
    return [
        (event_type, year, event_quarter)
        for event_type in EVENT_TYPES
        for year, event_quarter in sorted(
            {
                (int(row.year), int(row.quarter))
                for row in eligible.loc[
                    eligible["event_type"].eq(event_type)
                    & eligible["year"].isin(COHORT_YEARS)
                ].itertuples(index=False)
            }
        )
    ]


def quarter_tag_from_time(value: int) -> str:
    """Convert the integer quarter convention year*4+quarter to YYYYQX."""
    year, zero_based_quarter = divmod(value - 1, 4)
    return f"{year}Q{zero_based_quarter + 1}"


def required_quarter_windows(
    available: dict[str, Path],
    specifications: list[tuple[str, int, int]],
) -> tuple[list[str], dict[tuple[int, int], list[str]]]:
    """Build t-4 through t+7 windows and reject internal missing quarters."""
    available_keys = {tag: quarter_key(tag) for tag in available}
    min_available = min(available_keys.values())
    max_available = max(available_keys.values())
    windows: dict[tuple[int, int], list[str]] = {}
    required: set[str] = set()
    for _event_type, cohort_year, cohort_quarter in specifications:
        key = (cohort_year, cohort_quarter)
        if key in windows:
            continue
        event_time = cohort_year * 4 + cohort_quarter
        nominal = [
            quarter_tag_from_time(event_time + offset)
            for offset in range(-4, 8)
        ]
        missing_internal = [
            tag for tag in nominal
            if tag not in available
            and min_available <= quarter_key(tag) <= max_available
        ]
        if missing_internal:
            raise FileNotFoundError(
                f"Cohort {key} is missing required quarter files: "
                f"{missing_internal}"
            )
        actual = [tag for tag in nominal if tag in available]
        event_tag = f"{cohort_year}Q{cohort_quarter}"
        if event_tag not in actual:
            raise FileNotFoundError(
                f"Cohort {key} is missing its event quarter {event_tag}."
            )
        windows[key] = actual
        required.update(actual)
    return sorted(required, key=quarter_key), windows


def cohort_quarters(cohort_year: int, available: set[str], time_shift: int) -> list[str]:
    """Return the t-1 through t+1 quarters with only the agreed edge omission."""
    nominal = [
        f"{year}Q{quarter}"
        for year in range(cohort_year - 1, cohort_year + 2)
        for quarter in range(1, 5)
    ]
    missing = set(nominal) - available
    allowed_missing: set[str] = set()
    if time_shift == 1 and cohort_year == 2020:
        allowed_missing = {"2019Q1"}
    elif time_shift == 0 and cohort_year == 2024:
        allowed_missing = {"2025Q4"}
    unexpected = missing - allowed_missing
    if unexpected:
        raise FileNotFoundError(
            f"Cohort {cohort_year} is missing internal quarters: "
            f"{sorted(unexpected, key=quarter_key)}"
        )
    return [year_q for year_q in nominal if year_q in available]


# ========================== PLAN CROSSWALK ==========================


def normalize_plan_chunk(
    chunk: pd.DataFrame,
    spec: AnalysisSpec,
    time_shift: int,
    target_quarters: set[str],
) -> pd.DataFrame:
    """Normalize one merged-plan chunk and retain rows used by the selected panels."""
    result = chunk.copy()
    result["source_year_q"] = normalize_year_q(result["YEAR_Q"], PLAN_INFO_PATH.name)
    result["year_q"] = result["source_year_q"].map(lambda value: shift_year_q(str(value), time_shift))
    result = result.loc[result["year_q"].isin(target_quarters)].copy()
    if result.empty:
        return pd.DataFrame()

    result["contract_id"] = normalize_contract(result["CONTRACT_ID"])
    result["plan_id"] = normalize_numeric_identifier(result["PLAN_ID"])
    result["segment_id"] = normalize_numeric_identifier(result["SEGMENT_ID"])
    result["formulary_id"] = normalize_numeric_identifier(result["FORMULARY_ID"])
    if spec.level != "plan":
        result["state"] = normalize_text(result["STATE"], uppercase=True)
    if spec.level == "county":
        result["county_code"] = normalize_county(result["COUNTY_CODE"])
        result["ma_region_code"] = normalize_region(result["MA_REGION_CODE"])
        result["pdp_region_code"] = normalize_region(result["PDP_REGION_CODE"])

    # Contract, plan, segment, and formulary identify the structural record.
    # Geography is retained as an analysis/control variable.  Do not discard a
    # plan merely because CMS leaves its state/county/region field blank.
    identifiers = ["contract_id", "plan_id", "segment_id", "formulary_id"]
    if result[identifiers].isna().any().any():
        examples = result.loc[result[identifiers].isna().any(axis=1), identifiers].head(10)
        raise ValueError(f"{PLAN_INFO_PATH.name} has missing plan identifiers. Examples:\n{examples}")

    columns = [
        "year_q",
        "source_year_q",
        "contract_id",
        "plan_id",
        "segment_id",
        "formulary_id",
    ]
    if spec.level != "plan":
        columns.append("state")
    if spec.level == "county":
        columns.extend(["county_code", "ma_region_code", "pdp_region_code"])
    return result[columns].drop_duplicates().reset_index(drop=True)


def build_plan_crosswalks(
    spec: AnalysisSpec,
    time_shift: int,
    target_quarters: set[str],
) -> dict[str, pd.DataFrame]:
    """Build one normalized, unique plan crosswalk for every output quarter."""
    data = pd.read_csv(
        PLAN_INFO_PATH,
        usecols=spec.plan_input_columns,
        dtype="string",
        keep_default_na=False,
    )
    normalized = normalize_plan_chunk(data, spec, time_shift, target_quarters)

    crosswalks: dict[str, pd.DataFrame] = {}
    plan_key = ["year_q", *spec.plan_unit_columns]
    for year_q in sorted(target_quarters, key=quarter_key):
        crosswalk = normalized.loc[normalized["year_q"].eq(year_q)].drop_duplicates()
        if crosswalk.empty:
            raise ValueError(f"No plan-information rows map to formulary quarter {year_q}.")
        formula_counts = crosswalk.groupby(plan_key, dropna=False)["formulary_id"].nunique()
        conflicting = formula_counts[formula_counts.gt(1)]
        if not conflicting.empty:
            examples = conflicting.head(10).reset_index()
            raise ValueError(
                f"{year_q} assigns more than one formulary to a plan geography. Examples:\n{examples}"
            )
        if crosswalk.duplicated(plan_key).any():
            examples = crosswalk.loc[crosswalk.duplicated(plan_key, keep=False), plan_key].head(10)
            raise ValueError(f"{year_q} plan crosswalk is not unique. Examples:\n{examples}")
        crosswalks[year_q] = crosswalk.sort_values(plan_key).reset_index(drop=True)
    return crosswalks


def normalize_quarter_copay_chunk(
    chunk: pd.DataFrame,
    source_name: str,
    time_shift: int,
    target_quarters: set[str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Normalize one beneficiary-cost chunk for CPS and copay aggregation."""
    current = pd.DataFrame(index=chunk.index)
    current["source_year_q"] = normalize_year_q(chunk["YEAR_Q"], source_name)
    current["year_q"] = current["source_year_q"].map(
        lambda value: shift_year_q(str(value), time_shift)
    )
    current = current.loc[current["year_q"].isin(target_quarters)].copy()
    if current.empty:
        return pd.DataFrame(), pd.DataFrame()

    selected = chunk.loc[current.index]
    current["contract_id"] = normalize_contract(selected["CONTRACT_ID"])
    current["plan_id"] = normalize_numeric_identifier(selected["PLAN_ID"])
    current["segment_id"] = normalize_numeric_identifier(selected["SEGMENT_ID"])
    current["formulary_id"] = normalize_numeric_identifier(
        selected["FORMULARY_ID"]
    )
    current["tier"] = pd.to_numeric(
        selected["TIER"], errors="raise"
    ).astype("Int64")
    current["days_supply"] = normalize_numeric_identifier(
        selected["DAYS_SUPPLY"]
    )
    current["copay"] = parse_numeric(selected["copay"], "copay", source_name)
    cps_columns = ["contract_id", "plan_id", "segment_id"]
    tier_keys = ["year_q", *cps_columns, "tier"]
    required = [*cps_columns, "formulary_id", "tier", "days_supply"]
    if current[required].isna().any().any():
        examples = current.loc[
            current[required].isna().any(axis=1),
            ["source_year_q", *required],
        ].head(10)
        raise ValueError(
            f"{source_name} has missing quarterly CPS/copay keys. Examples:\n"
            f"{examples}"
        )
    crosswalk = current[
        ["year_q", "source_year_q", *cps_columns, "formulary_id"]
    ].drop_duplicates()
    daily_copay = current[[*tier_keys, "days_supply", "copay"]].drop_duplicates()
    return crosswalk, daily_copay


def finalize_quarter_copay_inputs(
    crosswalk_parts: list[pd.DataFrame],
    daily_copay_parts: list[pd.DataFrame],
    source_name: str,
    target_quarters: set[str],
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Validate and aggregate normalized beneficiary-cost chunks."""
    if not crosswalk_parts or not daily_copay_parts:
        raise ValueError(
            f"{source_name} has no rows for analysis quarters "
            f"{sorted(target_quarters, key=quarter_key)}."
        )
    cps_columns = ["contract_id", "plan_id", "segment_id"]
    tier_keys = ["year_q", *cps_columns, "tier"]
    crosswalk = pd.concat(crosswalk_parts, ignore_index=True).drop_duplicates()
    plan_quarter_keys = ["year_q", *cps_columns]
    formulary_counts = crosswalk.groupby(
        plan_quarter_keys, dropna=False
    )["formulary_id"].nunique()
    if formulary_counts.gt(1).any():
        examples = formulary_counts[formulary_counts.gt(1)].head(10).reset_index()
        raise ValueError(
            "Quarterly beneficiary data assigns multiple formularies to one "
            f"CPS-quarter. Examples:\n{examples}"
        )
    if crosswalk.duplicated(plan_quarter_keys).any():
        examples = crosswalk.loc[
            crosswalk.duplicated(plan_quarter_keys, keep=False),
            [*plan_quarter_keys, "formulary_id"],
        ].head(10)
        raise ValueError(
            f"Quarterly CPS crosswalk is not unique. Examples:\n{examples}"
        )
    crosswalks = {
        year_q: crosswalk.loc[crosswalk["year_q"].eq(year_q)]
        .sort_values(plan_quarter_keys)
        .reset_index(drop=True)
        for year_q in sorted(target_quarters, key=quarter_key)
    }
    empty_quarters = [
        year_q for year_q, frame in crosswalks.items() if frame.empty
    ]
    if empty_quarters:
        raise ValueError(f"No CPS rows found for quarters: {empty_quarters}")

    daily_copay = pd.concat(
        daily_copay_parts, ignore_index=True
    ).drop_duplicates()
    day_keys = [*tier_keys, "days_supply"]
    daily_conflicts = daily_copay.groupby(day_keys, dropna=False)["copay"].nunique(
        dropna=True
    )
    if daily_conflicts.gt(1).any():
        examples = daily_conflicts[daily_conflicts.gt(1)].head(10).reset_index()
        raise ValueError(
            "Daily copay differs across geographic duplicates for the same "
            f"CPS-tier-DAYS_SUPPLY. Examples:\n{examples}"
        )
    copay = (
        daily_copay.groupby(tier_keys, dropna=False, as_index=False)["copay"]
        .mean()
        .rename(columns={"copay": "avg_copay_amt"})
    )
    if copay.duplicated(tier_keys).any():
        raise AssertionError("Quarterly copay is not unique by CPS-quarter-tier.")
    return crosswalks, copay


def load_quarter_copay_inputs(
    path: Path,
    time_shift: int,
    target_quarters: set[str],
    chunksize: int,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Derive the quarterly CPS crosswalk and plan-tier copay in one pass.

    The beneficiary file is already restricted upstream to COVERAGE_LEVEL=1;
    that restriction intentionally defines the quarterly CPS universe here.
    Source quarters are shifted onto formulary time before filtering.

    `copay` is already expressed per day. Geographic repetitions are removed
    by retaining one unique CPS-tier-DAYS_SUPPLY-copay record. The remaining
    observed daily values are then averaged equally across DAYS_SUPPLY. Change
    this aggregation here if a later specification should privilege one supply
    length or use different weights. Rows flagged min_bigger_than_max are kept
    by design, and missing geography never removes a CPS.
    """
    if not path.exists():
        raise FileNotFoundError(f"Beneficiary copay source does not exist: {path}")
    usecols = [
        "YEAR_Q",
        "CONTRACT_ID",
        "PLAN_ID",
        "SEGMENT_ID",
        "FORMULARY_ID",
        "TIER",
        "DAYS_SUPPLY",
        "copay",
    ]
    crosswalk_parts: list[pd.DataFrame] = []
    daily_copay_parts: list[pd.DataFrame] = []
    reader = pd.read_csv(
        path,
        usecols=usecols,
        dtype="string",
        keep_default_na=False,
        chunksize=chunksize,
    )
    progress = tqdm(reader, desc="Reading quarterly CPS and copay", unit="chunk")
    for chunk in progress:
        crosswalk, daily_copay = normalize_quarter_copay_chunk(
            chunk, path.name, time_shift, target_quarters
        )
        if crosswalk.empty:
            continue
        crosswalk_parts.append(crosswalk)
        daily_copay_parts.append(daily_copay)
    return finalize_quarter_copay_inputs(
        crosswalk_parts,
        daily_copay_parts,
        path.name,
        target_quarters,
    )


def load_panel_formulary_availability(
    source_paths: dict[str, Path],
    chunksize: int,
) -> dict[str, frozenset[str]]:
    """Return formularies actually present in each reorganized quarter panel."""
    availability: dict[str, frozenset[str]] = {}
    progress = tqdm(
        sorted(source_paths.items(), key=lambda item: quarter_key(item[0])),
        desc="Checking quarter-panel formulary availability",
        unit="quarter",
    )
    for year_q, path in progress:
        formularies: set[str] = set()
        for chunk in pd.read_csv(
            path,
            usecols=["FORMULARY_ID"],
            dtype="string",
            chunksize=chunksize,
        ):
            normalized = normalize_numeric_identifier(chunk["FORMULARY_ID"])
            if normalized.isna().any():
                raise ValueError(f"{path.name} has missing FORMULARY_ID values.")
            formularies.update(normalized.astype(str))
        if not formularies:
            raise ValueError(f"{path.name} contains no formularies.")
        availability[year_q] = frozenset(formularies)
    return availability


def load_formulary_quarter_availability(
    path: Path,
    time_shift: int,
    target_quarters: set[str],
    chunksize: int,
) -> dict[str, frozenset[str]]:
    """Return formularies observed in the expanded source in each output quarter.

    Availability is defined at formulary-quarter level.  Each chunk is reduced
    to unique source quarter/formulary pairs before normalization, so repeated
    NDC rows do not add work or memory pressure.
    """
    if not path.exists():
        raise FileNotFoundError(f"Expanded formulary source does not exist: {path}")

    observed: dict[str, set[str]] = {year_q: set() for year_q in target_quarters}
    reader = pd.read_csv(
        path,
        usecols=["YEAR_Q", "FORMULARY_ID"],
        dtype="string",
        keep_default_na=False,
        chunksize=chunksize,
    )
    progress = tqdm(
        reader,
        desc="Reading formulary-quarter availability",
        unit="chunk",
    )
    for chunk in progress:
        pairs = chunk.drop_duplicates(["YEAR_Q", "FORMULARY_ID"]).copy()
        pairs["source_year_q"] = normalize_year_q(pairs["YEAR_Q"], path.name)
        pairs["year_q"] = pairs["source_year_q"].map(
            lambda value: shift_year_q(str(value), time_shift)
        )
        pairs["formulary_id"] = normalize_numeric_identifier(pairs["FORMULARY_ID"])
        invalid = pairs["formulary_id"].isna()
        if invalid.any():
            examples = pairs.loc[invalid, ["YEAR_Q", "FORMULARY_ID"]].head(10)
            raise ValueError(
                f"{path.name} has missing formulary-quarter identifiers. Examples:\n{examples}"
            )
        pairs = pairs.loc[pairs["year_q"].isin(target_quarters)]
        for year_q, values in pairs.groupby("year_q", sort=False)["formulary_id"]:
            observed[str(year_q)].update(values.astype(str))
        progress.set_postfix_str(
            f"unique pairs={sum(len(values) for values in observed.values()):,}"
        )

    empty_quarters = [year_q for year_q, values in observed.items() if not values]
    if empty_quarters:
        raise ValueError(
            f"{path.name} has no formulary observations for output quarters: "
            f"{sorted(empty_quarters, key=quarter_key)}"
        )
    return {year_q: frozenset(values) for year_q, values in observed.items()}


def unit_index(data: pd.DataFrame, unit_columns: list[str]) -> pd.MultiIndex:
    """Create a stable unit index, including rows with missing geography values."""
    values = data[unit_columns].astype("string").fillna("<MISSING>")
    return pd.MultiIndex.from_frame(values)


def balanced_plan_units(
    crosswalks: dict[str, pd.DataFrame],
    cohort_windows: dict[int, tuple[str, ...]],
    spec: AnalysisSpec,
    formulary_availability: dict[str, frozenset[str]] | None = None,
) -> dict[int, pd.MultiIndex]:
    """Return units with plan and formulary-source coverage in every cohort quarter."""
    result: dict[int, pd.MultiIndex] = {}
    for cohort_year, quarters in cohort_windows.items():
        frames: list[pd.DataFrame] = []
        for year_q in quarters:
            current_crosswalk = crosswalks[year_q]
            if formulary_availability is None:
                available = pd.Series(True, index=current_crosswalk.index)
            else:
                available_formularies = formulary_availability.get(year_q, frozenset())
                available = current_crosswalk["formulary_id"].isin(available_formularies)
            current = current_crosswalk.loc[available, spec.plan_unit_columns].copy()
            current["year_q"] = year_q
            frames.append(current)
        presence = pd.concat(frames, ignore_index=True).drop_duplicates()
        counts = presence.groupby(spec.plan_unit_columns, dropna=False)["year_q"].nunique()
        complete = counts[counts.eq(len(quarters))].reset_index()[spec.plan_unit_columns]
        result[cohort_year] = unit_index(complete, spec.plan_unit_columns)
    return result


def sample_plan_units(
    balanced_units: dict[int, pd.MultiIndex],
    sample_fraction: float,
    random_seed: int,
) -> dict[int, pd.MultiIndex]:
    """Reproducibly sample balanced plan units separately by cohort year."""
    sampled: dict[int, pd.MultiIndex] = {}
    for cohort_year, units in balanced_units.items():
        unit_count = len(units)
        if unit_count == 0:
            sampled[cohort_year] = units
            continue
        sample_count = min(
            unit_count,
            max(1, round(unit_count * sample_fraction)),
        )
        random_generator = np.random.default_rng(
            np.random.SeedSequence([random_seed, cohort_year])
        )
        positions = np.sort(
            random_generator.choice(unit_count, size=sample_count, replace=False)
        )
        sampled[cohort_year] = units.take(positions)
    return sampled


def sample_path_histories(
    path_cohorts: dict[int, PathCohort],
    samples: dict[tuple[str, int], CohortSample],
    sample_fraction: float,
    random_seed: int,
) -> dict[tuple[str, int], frozenset[str]]:
    """Sample complete paths reproducibly within each event-cohort output."""
    sampled: dict[tuple[str, int], frozenset[str]] = {}
    event_codes = {event_type: position for position, event_type in enumerate(EVENT_TYPES)}
    for key in sorted(samples, key=lambda value: (value[1], value[0])):
        event_type, cohort_year = key
        path_cohort = path_cohorts[cohort_year]
        history_ids = np.sort(
            path_cohort.history_quarters["history_id"].dropna().unique()
        )
        history_count = len(history_ids)
        if history_count == 0:
            sampled[key] = frozenset()
            continue
        sample_count = min(
            history_count,
            max(1, int(np.ceil(history_count * sample_fraction))),
        )
        random_generator = np.random.default_rng(
            np.random.SeedSequence(
                [random_seed, event_codes[event_type], cohort_year]
            )
        )
        positions = np.sort(
            random_generator.choice(
                history_count,
                size=sample_count,
                replace=False,
            )
        )
        sampled[key] = frozenset(history_ids[positions].astype(str))
    return sampled


def build_path_cohorts(
    crosswalks: dict[str, pd.DataFrame],
    balanced_units: dict[int, pd.MultiIndex],
    cohort_windows: dict[int, tuple[str, ...]],
    spec: AnalysisSpec,
    quarter: int = 0,
) -> dict[int, PathCohort]:
    """Collapse complete CPS formulary sequences within each cohort window."""
    cohorts: dict[int, PathCohort] = {}
    unit_columns = spec.plan_unit_columns
    for cohort_year, quarters in cohort_windows.items():
        member_frames: list[pd.DataFrame] = []
        for year_q in quarters:
            current = crosswalks[year_q]
            keep = unit_index(current, unit_columns).isin(balanced_units[cohort_year])
            selected = current.loc[keep].copy()
            if selected.empty:
                raise ValueError(f"Cohort {cohort_year} has no balanced CPS in {year_q}.")
            member_frames.append(selected)

        members = pd.concat(member_frames, ignore_index=True)
        members = members.sort_values([*unit_columns, "year_q"]).reset_index(drop=True)
        expected = len(quarters)
        member_counts = members.groupby(unit_columns, dropna=False)["year_q"].nunique()
        if not member_counts.eq(expected).all():
            raise ValueError(f"Cohort {cohort_year} contains a non-balanced CPS after filtering.")

        formulary_sequence = members.groupby(unit_columns, dropna=False)["formulary_id"].agg(
            lambda values: "\x1f".join(values.astype(str))
        )
        history_map = formulary_sequence.rename("history_signature").reset_index()
        history_code = pd.factorize(history_map["history_signature"], sort=True)[0] + 1
        year, event_quarter = cohort_year_quarter(cohort_year, quarter)
        cohort_tag = f"{year}Q{event_quarter}" if quarter else str(year)
        history_map["history_id"] = (
            f"H{cohort_tag}_" + pd.Series(history_code, index=history_map.index).astype(str)
        )
        history_map["n_path"] = history_map.groupby("history_id", dropna=False)[
            "history_id"
        ].transform("size").astype("int32")
        members = members.merge(
            history_map[[*unit_columns, "history_id", "n_path"]],
            on=unit_columns,
            how="left",
            validate="many_to_one",
        )
        members["data_cohort"] = np.int16(cohort_year)

        # Within-cohort predecessor only: the first cohort quarter has none.
        # This intentionally does not consult the preceding calendar quarter.
        members["previous_formulary_id"] = members.groupby(
            unit_columns,
            dropna=False,
        )["formulary_id"].shift(1)

        history_keys = ["history_id", "data_cohort", "year_q"]
        history_quarters = members[
            [
                *history_keys,
                "source_year_q",
                "formulary_id",
                "previous_formulary_id",
                "n_path",
            ]
        ].drop_duplicates()
        if history_quarters.duplicated(history_keys).any():
            raise ValueError(
                f"Cohort {cohort_year} history path does not uniquely determine a formulary quarter."
            )
        cohorts[cohort_year] = PathCohort(
            cohort_year=cohort_year,
            required_quarters=quarters,
            member_crosswalk=members,
            history_quarters=history_quarters,
        )
    return cohorts


def build_samples(
    cohort_windows: dict[int, tuple[str, ...]],
    quarter: int = 0,
    quarter_sources: QuarterEventSources | None = None,
) -> dict[tuple[str, int], CohortSample]:
    """Build req1/Not treated and excluded-control firm sets."""
    if quarter:
        sources = quarter_sources or load_quarter_event_sources()
        movement = sources.movement
        candidate = sources.candidates
        stay_column = sources.stay_column
    else:
        import FormularyCohortPanelMaker as formulary_cohort

        movement, candidate, stay_column = formulary_cohort.load_event_sources(0)
    samples: dict[tuple[str, int], CohortSample] = {}
    for cohort_key, quarters in cohort_windows.items():
        cohort_year, cohort_quarter = cohort_year_quarter(cohort_key, quarter)
        window_periods = (
            list(quarters) if quarter
            else {quarter_key(tag)[0] for tag in quarters}
        )
        for event_type in EVENT_TYPES:
            treated: dict[str, set[str]] = {}
            excluded: dict[str, set[str]] = {}
            if quarter:
                event_tags = (
                    movement["year"].astype(str)
                    + "Q"
                    + movement["quarter"].astype(str)
                )
                counterpart_condition = (
                    candidate["event_type"].eq(event_type)
                    & candidate["event_year"].eq(cohort_year)
                    & candidate["event_quarter"].eq(cohort_quarter)
                    & candidate[stay_column].eq(1)
                    & candidate["requirement1"].eq(1)
                )
                current_pairs = candidate.loc[counterpart_condition]
                counterpart_a = set(current_pairs["idA"].dropna().astype(str))
                counterpart_b = set(current_pairs["idB"].dropna().astype(str))
            for side in TREATMENT_GROUPS:
                if quarter:
                    treated_condition = (
                        movement["event_type"].eq(event_type)
                        & movement["firm_type"].eq(side)
                        & movement["year"].eq(cohort_year)
                        & movement["quarter"].eq(cohort_quarter)
                        & movement["req1"].eq(1)
                    )
                    treated[side] = set(
                        movement.loc[treated_condition, "id"].dropna().astype(str)
                    )
                    pure_condition = (
                        movement["event_type"].eq(event_type)
                        & movement["firm_type"].eq(side)
                        & event_tags.isin(window_periods)
                    )
                    pure_event = set(
                        movement.loc[pure_condition, "id"].dropna().astype(str)
                    )
                    counterpart_only = (
                        counterpart_b - counterpart_a
                        if side == "A"
                        else counterpart_a - counterpart_b
                    )
                else:
                    treated[side] = formulary_cohort.treated_firms(
                        movement, event_type, side, cohort_year
                    )
                    pure_event = formulary_cohort.pure_event_firms_in_window(
                        movement, event_type, side, window_periods
                    )
                    counterpart_only = formulary_cohort.counterpart_only_firms(
                        candidate,
                        stay_column,
                        event_type,
                        side,
                        cohort_year,
                    )
                excluded[side] = pure_event | counterpart_only
            samples[(event_type, cohort_key)] = CohortSample(
                event_type=event_type,
                cohort_year=cohort_key,
                required_quarters=quarters,
                treated_a=frozenset(treated["A"]),
                treated_b=frozenset(treated["B"]),
                excluded_controls_a=frozenset(excluded["A"]),
                excluded_controls_b=frozenset(excluded["B"]),
            )
    return samples


# ========================== FEATURE LOOKUPS ==========================


def normalize_plan_feature_keys(data: pd.DataFrame, source_name: str, time_shift: int) -> pd.DataFrame:
    """Normalize a plan-tier feature table and shift its quarter onto analysis time."""
    result = data.copy()
    result["source_year_q"] = normalize_year_q(result["YEAR_Q"], source_name)
    result["year_q"] = result["source_year_q"].map(lambda value: shift_year_q(str(value), time_shift))
    result["contract_id"] = normalize_contract(result["CONTRACT_ID"])
    result["plan_id"] = normalize_numeric_identifier(result["PLAN_ID"])
    result["segment_id"] = normalize_numeric_identifier(result["SEGMENT_ID"])
    result["tier"] = pd.to_numeric(result["TIER"], errors="raise").astype("Int64")
    return result


def load_copay_lookup(time_shift: int) -> pd.DataFrame:
    """Load the annual legacy plan-segment-tier average copay feature."""
    data = pd.read_csv(LEGACY_COPAY_PATH, dtype="string", keep_default_na=False)
    required = {"YEAR_Q", "CONTRACT_ID", "PLAN_ID", "SEGMENT_ID", "TIER", "avg_cost"}
    missing = sorted(required - set(data.columns))
    if missing:
        raise KeyError(f"{LEGACY_COPAY_PATH.name} is missing columns: {missing}")
    result = normalize_plan_feature_keys(data, LEGACY_COPAY_PATH.name, time_shift)
    result["avg_copay_amt"] = pd.to_numeric(result["avg_cost"], errors="coerce")
    keys = ["year_q", "contract_id", "plan_id", "segment_id", "tier"]
    if result.duplicated(keys).any():
        raise ValueError(f"{LEGACY_COPAY_PATH.name} is not unique by plan tier.")
    return result[[*keys, "avg_copay_amt"]]


def load_prefer_lookup(time_shift: int) -> pd.DataFrame:
    """Load the preferred-tier indicator without duplicating average copay."""
    data = pd.read_csv(PREFER_PATH, dtype="string", keep_default_na=False)
    required = {"YEAR_Q", "CONTRACT_ID", "PLAN_ID", "SEGMENT_ID", "TIER", "prefer"}
    missing = sorted(required - set(data.columns))
    if missing:
        raise KeyError(f"{PREFER_PATH.name} is missing columns: {missing}")
    result = normalize_plan_feature_keys(data, PREFER_PATH.name, time_shift)
    result["prefer"] = pd.to_numeric(result["prefer"], errors="raise").astype("Int8")
    keys = ["year_q", "contract_id", "plan_id", "segment_id", "tier"]
    if result.duplicated(keys).any():
        raise ValueError(f"{PREFER_PATH.name} is not unique by plan tier.")
    return result[[*keys, "prefer"]]


def combine_plan_tier_features(
    copay: pd.DataFrame, prefer: pd.DataFrame | None
) -> pd.DataFrame:
    """Attach optional preferred-tier values to the required copay lookup."""
    if prefer is None:
        return copay.assign(
            prefer=pd.Series(pd.NA, index=copay.index, dtype="Int8")
        )
    keys = ["year_q", "contract_id", "plan_id", "segment_id", "tier"]
    return copay.merge(prefer, on=keys, how="outer", validate="one_to_one")


def monthly_directory_paths() -> dict[str, Path]:
    """Return the unique CMS Monthly Report by Contract file for every month."""
    paths: dict[str, Path] = {}
    for path in DIRECTORY_ROOT.rglob("Monthly_Report_By_Contract_*.csv"):
        match = MONTHLY_PATTERN.fullmatch(path.name)
        if match is None:
            continue
        year_month = f"{match.group(1)}_{match.group(2)}"
        if year_month in paths:
            raise ValueError(f"Duplicate directory files for {year_month}: {paths[year_month]} and {path}")
        paths[year_month] = path
    if not paths:
        raise FileNotFoundError(f"No monthly directory files found under {DIRECTORY_ROOT}")
    return paths


def read_parent_organization(path: Path, year_month: str) -> pd.DataFrame:
    """Read one monthly contract-to-Parent Organization mapping."""
    try:
        data = pd.read_csv(path, dtype="string", keep_default_na=False, encoding="utf-8-sig")
    except UnicodeDecodeError:
        data = pd.read_csv(path, dtype="string", keep_default_na=False, encoding="cp1252")
    data.columns = [column.strip() for column in data.columns]
    required = {"Contract Number", "Parent Organization"}
    missing = sorted(required - set(data.columns))
    if missing:
        raise KeyError(f"{path.name} is missing columns: {missing}")
    result = data[["Contract Number", "Parent Organization"]].copy()
    result["contract_id"] = normalize_contract(result["Contract Number"])
    result["parent_organization"] = normalize_text(result["Parent Organization"])
    # A blank parent name is an unavailable feature, not an alternative parent
    # assignment.  Removing it here avoids retaining a blank duplicate when
    # the same contract also has its valid organization on another row.
    result = result.loc[
        result["contract_id"].notna() & result["parent_organization"].notna(),
        ["contract_id", "parent_organization"],
    ]
    conflicts = result.groupby("contract_id", dropna=False)["parent_organization"].nunique(dropna=True)
    if conflicts.gt(1).any():
        raise ValueError(f"{path.name} maps a contract to multiple Parent Organizations.")
    return result.drop_duplicates("contract_id").assign(representative_month=year_month)


def load_parent_lookup(source_quarters: Iterable[str], time_shift: int) -> pd.DataFrame:
    """Load only directory months needed by the plan panel's source quarters."""
    source_quarters = sorted(set(source_quarters), key=quarter_key)
    required_months = {representative_month(year_q) for year_q in source_quarters}
    paths = monthly_directory_paths()
    missing = sorted(required_months - set(paths))
    if missing:
        raise FileNotFoundError(f"Missing directory months: {missing}")
    frames = [read_parent_organization(paths[month], month) for month in sorted(required_months)]
    result = pd.concat(frames, ignore_index=True)
    month_to_source = {representative_month(year_q): year_q for year_q in source_quarters}
    result["source_year_q"] = result["representative_month"].map(month_to_source)
    result["year_q"] = result["source_year_q"].map(lambda value: shift_year_q(str(value), time_shift))
    keys = ["year_q", "contract_id"]
    if result.duplicated(keys).any():
        raise ValueError("Parent Organization lookup is not unique by analysis quarter and contract.")
    return result[[*keys, "parent_organization"]]


def first_seen_path(time_shift: int) -> Path:
    """Return original-time global NDC first-seen metadata for every panel shift."""
    base = FIRST_SEEN_ROOT / "ndc_first_seen.csv"
    return base


def load_first_seen_lookup(time_shift: int) -> pd.DataFrame:
    """Load one original-time global first-included quarter per NDC."""
    path = first_seen_path(time_shift)
    data = pd.read_csv(path, dtype="string", keep_default_na=False)
    required = {"NDC", "first_seen_YEAR_Q"}
    missing = sorted(required - set(data.columns))
    if missing:
        raise KeyError(f"{path.name} is missing columns: {missing}")
    result = data[["NDC", "first_seen_YEAR_Q"]].copy()
    result["ndc"] = normalize_text(result["NDC"])
    result["first_seen_year_q"] = normalize_year_q(result["first_seen_YEAR_Q"], path.name)
    if result["ndc"].isna().any() or result.duplicated("ndc").any():
        raise ValueError(f"{path.name} must have one nonmissing first-seen record per NDC.")
    result["first_seen_qtime"] = quarter_time(result["first_seen_year_q"])
    return result[["ndc", "first_seen_year_q", "first_seen_qtime"]]


# ========================== FORMULARY PANEL BUILD ==========================


def parse_numeric(values: pd.Series, column: str, source_name: str) -> pd.Series:
    """Parse numeric values and reject nonnumeric nonmissing source values."""
    result = pd.to_numeric(values, errors="coerce")
    nonmissing = normalize_text(values).notna()
    invalid = nonmissing & result.isna()
    if invalid.any():
        examples = values.loc[invalid].drop_duplicates().head(10).tolist()
        raise ValueError(f"{source_name} has invalid {column} values: {examples}")
    return result


def normalize_raw_chunk(
    chunk: pd.DataFrame, source_name: str, quarter: int = 0,
) -> pd.DataFrame:
    """Normalize one raw formulary chunk while preserving all requested outcomes."""
    result = chunk.copy()
    result["year_q"] = normalize_year_q(result["YEAR_Q"], source_name)
    result["formulary_id"] = normalize_numeric_identifier(result["FORMULARY_ID"])
    result["ndc"] = normalize_text(result["NDC"])
    identity_column = "id" if quarter else "boardname"
    if quarter:
        result["id"] = normalize_company_id(result["id"], source_name)
    else:
        result["boardname"] = normalize_text(result["BoardName"], uppercase=True)
    result["atc3"] = normalize_text(result["ATC3"], uppercase=True)
    if not quarter:
        result["atc4"] = normalize_text(result["ATC4"], uppercase=True)
    identifiers = ["formulary_id", "ndc", identity_column]
    if result[identifiers].isna().any().any():
        examples = result.loc[result[identifiers].isna().any(axis=1), identifiers].head(10)
        raise ValueError(f"{source_name} has missing formulary drug identifiers. Examples:\n{examples}")
    for column in ("included", "tier_raw", "max_tier", *EVENT_COLUMNS, *SHARING_COLUMNS):
        result[column] = parse_numeric(result[column], column, source_name)
    if not result["included"].isin([0, 1]).all():
        raise ValueError(f"{source_name} has included values outside 0/1.")
    for column in [*EVENT_COLUMNS, *SHARING_COLUMNS]:
        if not result[column].isin([0, 1]).all():
            raise ValueError(f"{source_name} has {column} values outside 0/1.")
        result[column] = result[column].astype("int8")
    columns = [
        "year_q",
        "formulary_id",
        "ndc",
        identity_column,
        "atc3",
    ]
    if not quarter:
        columns.append("atc4")
    return result[
        [
            *columns,
            "included",
            "tier_raw",
            "max_tier",
            *EVENT_COLUMNS,
            *SHARING_COLUMNS,
        ]
    ]


def add_tier_key(data: pd.DataFrame, source_name: str) -> pd.DataFrame:
    """Create the integer copay-tier key from nonmissing raw formulary tiers."""
    result = data
    tier = result["tier_raw"]
    nonmissing = tier.notna()
    noninteger = nonmissing & ~np.isclose(tier, np.round(tier))
    if noninteger.any():
        examples = tier.loc[noninteger].drop_duplicates().head(10).tolist()
        raise ValueError(f"{source_name} has noninteger tier_raw values: {examples}")
    result["tier"] = pd.Series(pd.NA, index=result.index, dtype="Int64")
    result.loc[nonmissing, "tier"] = np.round(tier.loc[nonmissing]).astype("int64")
    return result


def build_formulary_tier_schedule(
    source_paths: dict[str, Path],
    path_cohorts: dict[int, PathCohort],
    chunksize: int,
    path_average: int = 0,
) -> tuple[pd.DataFrame, dict[int, pd.DataFrame] | None]:
    """Read active tiers and optionally find full-cohort included history-NDCs."""
    used_formularies: dict[str, set[str]] = {}
    history_frames_by_quarter: dict[str, list[pd.DataFrame]] = {}
    for path_cohort in path_cohorts.values():
        for year_q, frame in path_cohort.history_quarters.groupby("year_q", sort=False):
            used_formularies.setdefault(str(year_q), set()).update(
                frame["formulary_id"].dropna().astype(str)
            )
            history_frames_by_quarter.setdefault(str(year_q), []).append(
                frame[["history_id", "data_cohort", "formulary_id"]]
            )

    histories_by_quarter = {
        year_q: pd.concat(frames, ignore_index=True)
        for year_q, frames in history_frames_by_quarter.items()
    }

    tier_frames: list[pd.DataFrame] = []
    eligible: dict[int, pd.DataFrame | None] = {
        cohort_year: None for cohort_year in path_cohorts
    }
    scanned_quarters: dict[int, list[str]] = {
        cohort_year: [] for cohort_year in path_cohorts
    }
    usecols = ["FORMULARY_ID", "tier_raw"]
    if path_average:
        usecols.extend(["NDC", "included"])
    source_progress = tqdm(
        sorted(source_paths.items(), key=lambda item: quarter_key(item[0])),
        desc="Reading formulary tier schedules",
        unit="quarter",
    )
    for year_q, source_path in source_progress:
        formularies = used_formularies.get(year_q, set())
        if not formularies:
            continue
        source_progress.set_postfix_str(year_q)
        numeric_formularies = {
            int(formulary_id)
            for formulary_id in formularies
            if formulary_id.isdigit()
        }
        nonnumeric_formularies = formularies - {
            formulary_id for formulary_id in formularies if formulary_id.isdigit()
        }
        histories = histories_by_quarter[year_q]
        included_parts: dict[int, list[pd.DataFrame]] = {
            int(cohort_year): []
            for cohort_year in histories["data_cohort"].unique()
        }
        reader = pd.read_csv(source_path, usecols=usecols, dtype="string", chunksize=chunksize)
        for chunk in tqdm(
            reader,
            desc=f"Tier schedule {year_q}",
            unit="chunk",
            leave=False,
        ):
            raw_formulary = normalize_text(chunk["FORMULARY_ID"])
            raw_numeric = pd.to_numeric(raw_formulary, errors="coerce")
            keep = raw_formulary.isin(nonnumeric_formularies)
            if numeric_formularies:
                keep |= raw_numeric.isin(numeric_formularies)
            if not keep.any():
                continue
            current = pd.DataFrame(index=chunk.index[keep])
            current["year_q"] = year_q
            selected_raw = raw_formulary.loc[keep]
            selected_numeric = raw_numeric.loc[keep]
            current["formulary_id"] = selected_numeric.astype("Int64").astype("string")
            nonnumeric = selected_numeric.isna()
            if nonnumeric.any():
                current.loc[nonnumeric, "formulary_id"] = normalize_numeric_identifier(
                    selected_raw.loc[nonnumeric]
                )
            current["tier_raw"] = parse_numeric(
                chunk.loc[keep, "tier_raw"],
                "tier_raw",
                source_path.name,
            )
            current = current.loc[current["formulary_id"].isin(formularies)].copy()
            if current.empty:
                continue
            if path_average:
                current["ndc"] = normalize_text(chunk.loc[current.index, "NDC"])
                current["included"] = parse_numeric(
                    chunk.loc[current.index, "included"],
                    "included",
                    source_path.name,
                )
                if current["ndc"].isna().any():
                    examples = current.loc[current["ndc"].isna()].head(10)
                    raise ValueError(
                        f"{source_path.name} has missing NDC values. Examples:\n{examples}"
                    )
                if not current["included"].isin([0, 1]).all():
                    raise ValueError(f"{source_path.name} has included values outside 0/1.")
                current_included = current.loc[
                    current["included"].eq(1), ["formulary_id", "ndc"]
                ].drop_duplicates()
                if not current_included.empty:
                    included_histories = current_included.merge(
                        histories,
                        on="formulary_id",
                        how="inner",
                        validate="many_to_many",
                    )
                    for cohort_year, cohort_data in included_histories.groupby(
                        "data_cohort", sort=False
                    ):
                        included_parts[int(cohort_year)].append(
                            cohort_data[["history_id", "ndc"]].drop_duplicates()
                        )
            current = add_tier_key(current, source_path.name)
            tier_frames.append(
                current.loc[current["tier"].notna(), ["year_q", "formulary_id", "tier"]]
            )
        if path_average:
            for cohort_year, parts in included_parts.items():
                current_eligible = (
                    pd.concat(parts, ignore_index=True).drop_duplicates()
                    if parts
                    else pd.DataFrame(columns=["history_id", "ndc"])
                )
                previous_eligible = eligible[cohort_year]
                eligible[cohort_year] = (
                    current_eligible
                    if previous_eligible is None
                    else previous_eligible.merge(
                        current_eligible,
                        on=["history_id", "ndc"],
                        how="inner",
                        validate="one_to_one",
                    )
                )
                scanned_quarters[cohort_year].append(year_q)
    if not tier_frames:
        raise ValueError("No active formulary tiers were found for the complete path cohorts.")
    formulary_tiers = pd.concat(tier_frames, ignore_index=True).drop_duplicates()
    if not path_average:
        return formulary_tiers, None

    eligible_histories: dict[int, pd.DataFrame] = {}
    for cohort_year, path_cohort in path_cohorts.items():
        if tuple(scanned_quarters[cohort_year]) != path_cohort.required_quarters:
            raise ValueError(
                f"Cohort {cohort_year} inclusion scan covered "
                f"{scanned_quarters[cohort_year]}, expected "
                f"{list(path_cohort.required_quarters)}."
            )
        cohort_eligible = eligible[cohort_year]
        if cohort_eligible is None:
            cohort_eligible = pd.DataFrame(columns=["history_id", "ndc"])
        eligible_histories[cohort_year] = cohort_eligible.assign(
            __path_average_eligible=True
        )
    return formulary_tiers, eligible_histories


def build_benefit_path_features(
    path_cohorts: dict[int, PathCohort],
    formulary_tiers: pd.DataFrame,
    plan_tier: pd.DataFrame,
    spec: AnalysisSpec,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Compute separate complete-path weights and means for copay and prefer.

    A CPS is valid for an outcome only when that outcome is observed for every
    active raw formulary tier in every cohort quarter.  A raw-missing
    formulary-quarter therefore invalidates the affected CPS for both benefit
    outcomes, while leaving it in the structural path sample.
    """
    value_keys = ["history_id", "data_cohort", "year_q", "tier"]
    history_keys = ["history_id", "data_cohort"]
    copay_value_frames: list[pd.DataFrame] = []
    prefer_value_frames: list[pd.DataFrame] = []
    copay_count_frames: list[pd.DataFrame] = []
    prefer_count_frames: list[pd.DataFrame] = []
    cps_columns = spec.plan_unit_columns
    plan_tier_keys = ["year_q", "contract_id", "plan_id", "segment_id", "tier"]
    for cohort_year, path_cohort in path_cohorts.items():
        members = path_cohort.member_crosswalk
        schedule = members.merge(
            formulary_tiers,
            on=["year_q", "formulary_id"],
            how="left",
            validate="many_to_many",
        )
        schedule = schedule.merge(
            plan_tier,
            on=plan_tier_keys,
            how="left",
            validate="many_to_one",
        )
        for outcome, count_column, value_frames, count_frames in (
            ("avg_copay_amt", "n_path_copay", copay_value_frames, copay_count_frames),
            ("prefer", "n_path_prefer", prefer_value_frames, prefer_count_frames),
        ):
            complete = schedule.groupby(cps_columns, dropna=False)[outcome].agg(
                lambda values: values.notna().all()
            )
            complete_mask = complete.eq(True).fillna(False)
            complete_members = complete.loc[complete_mask].reset_index()[cps_columns]
            if complete_members.empty:
                continue
            valid_schedule = schedule.merge(
                complete_members,
                on=cps_columns,
                how="inner",
                validate="many_to_one",
            )
            counts = valid_schedule[[*cps_columns, *history_keys]].drop_duplicates()
            count_frames.append(
                counts.groupby(history_keys, dropna=False).size().rename(count_column).reset_index()
            )
            value_frames.append(
                valid_schedule.groupby(value_keys, dropna=False, as_index=False)[outcome].mean()
            )

    def concatenate_or_empty(
        frames: list[pd.DataFrame], columns: list[str]
    ) -> pd.DataFrame:
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=columns)

    copay_values = concatenate_or_empty(copay_value_frames, [*value_keys, "avg_copay_amt"])
    prefer_values = concatenate_or_empty(prefer_value_frames, [*value_keys, "prefer"])
    path_values = copay_values.merge(
        prefer_values,
        on=value_keys,
        how="outer",
        validate="one_to_one",
    )
    copay_counts = concatenate_or_empty(copay_count_frames, [*history_keys, "n_path_copay"])
    prefer_counts = concatenate_or_empty(prefer_count_frames, [*history_keys, "n_path_prefer"])
    path_counts = copay_counts.merge(
        prefer_counts,
        on=history_keys,
        how="outer",
        validate="one_to_one",
    )
    return path_values, path_counts


def add_previous_formulary(
    crosswalk: pd.DataFrame,
    previous_crosswalk: pd.DataFrame | None,
    spec: AnalysisSpec,
) -> pd.DataFrame:
    """Attach the preceding quarter's formulary for every current plan geography."""
    result = crosswalk.copy()
    result["previous_formulary_id"] = pd.Series(pd.NA, index=result.index, dtype="string")
    if previous_crosswalk is None:
        return result
    prior = previous_crosswalk[[*spec.plan_unit_columns, "formulary_id"]].rename(
        columns={"formulary_id": "previous_formulary_id"}
    )
    if prior.duplicated(spec.plan_unit_columns).any():
        raise ValueError("Previous plan crosswalk is not unique by plan geography.")
    result = result.drop(columns="previous_formulary_id").merge(
        prior,
        on=spec.plan_unit_columns,
        how="left",
        validate="many_to_one",
    )
    return result


def add_plan_tier_features(data: pd.DataFrame, plan_tier: pd.DataFrame) -> pd.DataFrame:
    """Left-join the combined copay/preferred-tier lookup after plan expansion."""
    tier_keys = ["year_q", "contract_id", "plan_id", "segment_id", "tier"]
    return data.merge(plan_tier, on=tier_keys, how="left", validate="many_to_one")


def add_first_seen(data: pd.DataFrame, first_seen: pd.DataFrame) -> pd.DataFrame:
    """Attach NDC timing before plan expansion and construct the unscreened seen flag."""
    result = data.merge(first_seen, on="ndc", how="left", validate="many_to_one")
    current_qtime = quarter_time(result["year_q"])
    result["seen"] = pd.Series(pd.NA, index=result.index, dtype="Int8")
    observed = result["first_seen_qtime"].notna()
    result.loc[observed, "seen"] = (
        current_qtime.loc[observed].ge(result.loc[observed, "first_seen_qtime"])
    ).astype("int8")
    return result


def add_tier_transitions(
    data: pd.DataFrame,
    previous_lookup: pd.DataFrame | None,
) -> pd.DataFrame:
    """Mark raw-tier upgrades and downgrades against the preceding calendar quarter."""
    result = data
    if previous_lookup is None:
        result["previous_tier_raw"] = np.nan
    else:
        result = result.merge(
            previous_lookup,
            on=["previous_formulary_id", "ndc"],
            how="left",
            validate="many_to_one",
        )
    comparable = result["tier_raw"].notna() & result["previous_tier_raw"].notna()
    result["tier_upgrade"] = (
        comparable & result["tier_raw"].lt(result["previous_tier_raw"])
    ).astype("int8")
    result["tier_downgrade"] = (
        comparable & result["tier_raw"].gt(result["previous_tier_raw"])
    ).astype("int8")
    return result


def expansion_batches(
    raw: pd.DataFrame,
    plan_counts: pd.Series,
    max_expanded_rows: int,
) -> Iterable[pd.DataFrame]:
    """Yield FORMULARY_ID-preserving raw batches with bounded expanded size."""
    pending: list[pd.DataFrame] = []
    pending_rows = 0
    for formulary_id, group in raw.groupby("formulary_id", sort=False):
        multiplier = int(plan_counts.get(formulary_id, 0))
        if multiplier == 0:
            continue
        raw_rows_per_piece = max(1, max_expanded_rows // multiplier)
        for start in range(0, len(group), raw_rows_per_piece):
            piece = group.iloc[start : start + raw_rows_per_piece]
            expanded_rows = len(piece) * multiplier
            if pending and pending_rows + expanded_rows > max_expanded_rows:
                yield pd.concat(pending, ignore_index=True)
                pending = []
                pending_rows = 0
            pending.append(piece)
            pending_rows += expanded_rows
    if pending:
        yield pd.concat(pending, ignore_index=True)


def panel_columns(spec: AnalysisSpec) -> list[str]:
    """Return the stable merged columns written into every cohort panel."""
    if spec.level == "plan":
        geography: list[str] = []
    elif spec.level == "state":
        geography = ["state"]
    else:
        geography = ["state", "county_code", "ma_region_code", "pdp_region_code"]
    return [
        "year_q",
        "source_year_q",
        "contract_id",
        "plan_id",
        "segment_id",
        *geography,
        "formulary_id",
        "previous_formulary_id",
        "ndc",
        "boardname",
        "atc3",
        "atc4",
        "included",
        "tier_raw",
        "max_tier",
        "tier_upgrade",
        "tier_downgrade",
        "tier",
        "avg_copay_amt",
        "prefer",
        "parent_organization",
        "first_seen_year_q",
        "seen",
        *EVENT_COLUMNS,
        *SHARING_COLUMNS,
    ]


def cohort_output_dir(spec: AnalysisSpec, time_shift: int) -> Path:
    """Return the direct cohort output directory."""
    return COHORT_ROOT / shift_label(time_shift) / spec.level


def cohort_output_path(
    spec: AnalysisSpec,
    time_shift: int,
    event_type: str,
    cohort_year: int,
) -> Path:
    """Return one event-year cohort CSV path."""
    return cohort_output_dir(spec, time_shift) / (
        f"{event_type}_plan_quarter_cohort_{cohort_year}.csv"
    )


def path_panel_columns() -> list[str]:
    """Return the final history-by-NDC columns for path-weighted estimation."""
    return [
        "history_id",
        "data_cohort",
        "n_path",
        "n_path_copay",
        "n_path_prefer",
        "formulary_id",
        "ndc",
        "boardname",
        "atc3",
        "included",
        "tier_raw",
        "max_tier",
        "tier_upgrade",
        "tier_downgrade",
        "avg_copay_amt",
        "prefer",
        *EVENT_COLUMNS,
        *SHARING_COLUMNS,
        "treated_a",
        "treated_b",
        "sample_a",
        "sample_b",
        "year",
        "quarter",
    ]


def quarterly_path_panel_columns(event_type: str) -> list[str]:
    """Return the compact quarterly path schema used by the path do-files."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unsupported quarterly path event type: {event_type}")
    other_event_columns = [
        event_column(other_event, side)
        for other_event in EVENT_TYPES
        if other_event != event_type
        for side in TREATMENT_GROUPS
    ]
    event_sharing_columns = [
        f"{event_column(event_type, side)}_sharingATC3"
        for side in TREATMENT_GROUPS
    ]
    return [
        "history_id",
        "data_cohort_year",
        "data_cohort_quarter",
        "data_cohort_qtime",
        "n_path",
        "n_path_copay",
        "ndc",
        "id",
        "included",
        "tier_raw",
        "avg_copay_amt",
        *other_event_columns,
        *event_sharing_columns,
        "treated_a",
        "treated_b",
        "sample_a",
        "sample_b",
        "year",
        "quarter",
    ]


def path_output_schemas(
    output_paths: dict[tuple[str, int], Path],
    average_output_paths: dict[tuple[str, int], Path],
    quarter: int,
) -> dict[str, list[str]]:
    """Return the exact schema recorded for every path output file."""
    normal = {
        (f"path/{path.name}" if average_output_paths else path.name): (
            quarterly_path_panel_columns(key[0])
            if quarter
            else path_panel_columns()
        )
        for key, path in output_paths.items()
    }
    if not average_output_paths:
        return normal
    return {
        **normal,
        **{
            f"path_average/{path.name}": path_average_columns(key[0], quarter)
            for key, path in average_output_paths.items()
        },
    }


def tracked_path_outputs(
    output_paths: dict[tuple[str, int], Path],
    average_output_paths: dict[tuple[str, int], Path],
) -> dict[str, Path]:
    """Label normal and averaged files uniquely for checkpoint bookkeeping."""
    if not average_output_paths:
        return {path.name: path for path in output_paths.values()}
    return {
        **{
            f"path/{path.name}": path
            for path in output_paths.values()
        },
        **{
            f"path_average/{path.name}": path
            for path in average_output_paths.values()
        },
    }


PATH_AVERAGE_WEIGHTS = {
    "included": "n_path",
    "tier_raw": "n_path",
    "avg_copay_amt": "n_path_copay",
}
PATH_AVERAGE_INTENSIVE_OUTCOMES = {"tier_raw", "avg_copay_amt"}


def path_average_columns(event_type: str, quarter: int) -> list[str]:
    """Describe the NDC-company-quarter output after averaging complete paths."""
    source = quarterly_path_panel_columns(event_type) if quarter else path_panel_columns()
    retained = {
        "data_cohort", "data_cohort_year", "data_cohort_quarter",
        "data_cohort_qtime", "ndc", "id", "boardname", "year", "quarter",
        "treated_a", "treated_b", "sample_a", "sample_b",
        *PATH_AVERAGE_WEIGHTS,
    }
    return [column for column in source if column in retained or column.startswith("event_")]


def reduce_path_average_totals(
    data: pd.DataFrame, keys: list[str], constants: list[str],
) -> pd.DataFrame:
    """Sum sufficient statistics, rejecting inconsistent cohort/event/sample flags."""
    grouped = data.groupby(keys, sort=False, dropna=False)
    inconsistent = grouped[constants].nunique(dropna=False).gt(1)
    if inconsistent.any().any():
        examples = inconsistent.loc[inconsistent.any(axis=1)].head(5)
        raise ValueError(f"Nonconstant fields across paths for NDC-company-quarter:\n{examples}")
    totals = [column for column in data if column.startswith("__path_average_")]
    return grouped[constants].first().join(grouped[totals].sum()).reset_index()


def accumulate_path_average(
    previous: pd.DataFrame | None,
    data: pd.DataFrame,
    columns: list[str],
    quarter: int,
) -> pd.DataFrame:
    """Accumulate weighted sums across paths and CSV chunks without averaging means."""
    keys = ["ndc", "id" if quarter else "boardname", "year", "quarter"]
    constants = [
        column for column in columns if column not in {*keys, *PATH_AVERAGE_WEIGHTS}
    ]
    values = data[[*keys, *constants]].copy()
    for outcome, weight_column in PATH_AVERAGE_WEIGHTS.items():
        weight = data[weight_column]
        if weight.isna().any() or not np.isfinite(weight).all() or weight.lt(0).any():
            raise ValueError(f"Invalid {weight_column} in path averaging.")
        valid = data[outcome].notna() & weight.gt(0)
        if outcome in PATH_AVERAGE_INTENSIVE_OUTCOMES:
            eligible = data["__path_average_eligible"].eq(True)
            unexpected_missing = eligible & weight.gt(0) & data[outcome].isna()
            if unexpected_missing.any():
                examples = data.loc[
                    unexpected_missing,
                    [
                        "history_id",
                        "data_cohort",
                        "ndc",
                        "year_q",
                        outcome,
                        weight_column,
                    ],
                ].head(10)
                raise ValueError(
                    f"Eligible path-average histories have missing {outcome}. "
                    f"Examples:\n{examples}"
                )
            valid &= eligible
        values[f"__path_average_{outcome}_numerator"] = (
            data[outcome].where(valid, 0) * weight.where(valid, 0)
        )
        values[f"__path_average_{outcome}_denominator"] = weight.where(valid, 0)
    current = reduce_path_average_totals(values, keys, constants)
    if previous is None:
        return current
    return reduce_path_average_totals(
        pd.concat([previous, current], ignore_index=True), keys, constants,
    )


def finish_path_average(totals: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Divide each outcome's weighted sum by its own nonmissing-value weight."""
    result = totals.copy()
    for outcome in PATH_AVERAGE_WEIGHTS:
        denominator = result[f"__path_average_{outcome}_denominator"]
        result[outcome] = (
            result[f"__path_average_{outcome}_numerator"]
            / denominator.where(denominator.gt(0))
        )
    return result[columns]


def path_cohort_output_dir(spec: AnalysisSpec, time_shift: int, quarter: int = 0) -> Path:
    """Return the path-weighted cohort output directory."""
    root = QUARTER_PATH_WEIGHTED_COHORT_ROOT if quarter else PATH_WEIGHTED_COHORT_ROOT
    return root / shift_label(time_shift) / spec.level


def path_cohort_output_path(
    spec: AnalysisSpec,
    time_shift: int,
    event_type: str,
    cohort_year: int,
    quarter: int = 0,
) -> Path:
    """Return one event-year path-weighted cohort CSV path."""
    year, event_quarter = cohort_year_quarter(cohort_year, quarter)
    cohort_tag = f"{year}Q{event_quarter}" if quarter else str(year)
    return path_cohort_output_dir(spec, time_shift, quarter) / (
        f"{event_type}_path_quarter_cohort_{cohort_tag}.csv"
    )


def add_sample_columns(
    data: pd.DataFrame, sample: CohortSample, quarter: int = 0,
) -> pd.DataFrame:
    """Apply one event cohort's A/B treated-control rules and retain its sample."""
    firm_id = data["id" if quarter else "boardname"]
    treated_a = firm_id.isin(sample.treated_a).astype("int8")
    treated_b = firm_id.isin(sample.treated_b).astype("int8")
    sample_a = (
        treated_a.eq(1) | ~firm_id.isin(sample.excluded_controls_a)
    ).astype("int8")
    sample_b = (
        treated_b.eq(1) | ~firm_id.isin(sample.excluded_controls_b)
    ).astype("int8")
    keep = sample_a.eq(1) | sample_b.eq(1)
    result = data.loc[keep].copy()
    result["treated_a"] = treated_a.loc[keep]
    result["treated_b"] = treated_b.loc[keep]
    result["sample_a"] = sample_a.loc[keep]
    result["sample_b"] = sample_b.loc[keep]
    result["data_cohort"] = np.int16(sample.cohort_year)
    if quarter:
        cohort_year, cohort_quarter = cohort_year_quarter(sample.cohort_year, quarter)
        result["data_cohort_year"] = np.int16(cohort_year)
        result["data_cohort_quarter"] = np.int8(cohort_quarter)
        result["data_cohort_qtime"] = np.int32(sample.cohort_year)
    result["year"] = pd.to_numeric(
        result["year_q"].str.slice(0, 4),
        errors="raise",
    ).astype("int16")
    result["quarter"] = pd.to_numeric(
        result["year_q"].str.slice(-1),
        errors="raise",
    ).astype("int8")
    return result


def build_quarter_panel(
    source_path: Path,
    crosswalk: pd.DataFrame,
    previous_crosswalk: pd.DataFrame | None,
    previous_lookup: pd.DataFrame | None,
    spec: AnalysisSpec,
    chunksize: int,
    max_expanded_rows: int,
    plan_tier: pd.DataFrame,
    parent: pd.DataFrame,
    first_seen: pd.DataFrame,
    cohort_windows: dict[int, tuple[str, ...]],
    sampled_units: dict[int, pd.MultiIndex],
    samples: dict[tuple[str, int], CohortSample],
    output_paths: dict[tuple[str, int], Path],
    headers_written: dict[tuple[str, int], bool],
    output_rows: dict[tuple[str, int], int],
) -> tuple[int, pd.DataFrame]:
    """Build one quarter once and dispatch it directly to relevant cohorts."""
    year_q = source_path.stem.removeprefix("formulary_panel_")
    if set(crosswalk["year_q"].unique()) != {year_q}:
        raise ValueError(f"Crosswalk does not contain exactly {year_q}.")
    relevant_cohorts = [
        cohort_year
        for cohort_year, quarters in cohort_windows.items()
        if year_q in quarters
    ]
    plan_keys = unit_index(crosswalk, spec.plan_unit_columns)
    retained = np.zeros(len(crosswalk), dtype=bool)
    for cohort_year in relevant_cohorts:
        retained |= plan_keys.isin(sampled_units[cohort_year])
    selected_crosswalk = crosswalk.loc[retained].copy()
    enriched_crosswalk = add_previous_formulary(
        selected_crosswalk,
        previous_crosswalk,
        spec,
    )
    enriched_crosswalk = enriched_crosswalk.merge(
        parent,
        on=["year_q", "contract_id"],
        how="left",
        validate="many_to_one",
    )
    plan_counts = enriched_crosswalk.groupby("formulary_id", dropna=False).size()
    used_formularies = set(plan_counts.index.dropna().astype(str))
    expanded_rows = 0
    written_rows = 0
    tier_parts: list[pd.DataFrame] = []
    board_lookup: pd.DataFrame | None = None
    reader = pd.read_csv(
        source_path,
        usecols=ANNUAL_RAW_FORMULARY_COLUMNS,
        dtype="string",
        chunksize=chunksize,
    )
    progress = tqdm(reader, desc=f"Building {year_q} plan panel", unit="chunk", leave=False)
    for raw_chunk in progress:
        raw = normalize_raw_chunk(raw_chunk, source_path.name)
        observed_quarters = set(raw["year_q"].dropna().astype(str).unique())
        if observed_quarters != {year_q}:
            raise ValueError(f"{source_path.name} contains unexpected quarters: {sorted(observed_quarters)}")
        raw = raw.loc[raw["formulary_id"].isin(used_formularies)].copy()
        if raw.empty:
            continue
        tier_parts.append(
            raw[["formulary_id", "ndc", "tier_raw"]].rename(
                columns={
                    "formulary_id": "previous_formulary_id",
                    "tier_raw": "previous_tier_raw",
                }
            )
        )
        current_boards = raw[["ndc", "boardname"]].drop_duplicates()
        board_lookup = (
            current_boards
            if board_lookup is None
            else pd.concat([board_lookup, current_boards], ignore_index=True).drop_duplicates()
        )
        raw = add_tier_key(raw, source_path.name)
        raw = add_first_seen(raw, first_seen)
        for batch in expansion_batches(raw, plan_counts, max_expanded_rows):
            expanded = batch.merge(
                enriched_crosswalk,
                on=["year_q", "formulary_id"],
                how="inner",
                validate="many_to_many",
            )
            if expanded.empty:
                continue
            expanded = add_tier_transitions(expanded, previous_lookup)
            expanded = add_plan_tier_features(expanded, plan_tier)
            base = expanded[panel_columns(spec)]
            expanded_plan_keys = unit_index(expanded, spec.plan_unit_columns)
            expanded_rows += len(base)
            for cohort_year in relevant_cohorts:
                cohort_mask = expanded_plan_keys.isin(sampled_units[cohort_year])
                if not cohort_mask.any():
                    continue
                cohort_data = base.loc[cohort_mask]
                for event_type in EVENT_TYPES:
                    key = (event_type, cohort_year)
                    selected = add_sample_columns(cohort_data, samples[key])
                    if selected.empty:
                        continue
                    selected.to_csv(
                        output_paths[key],
                        mode="a",
                        index=False,
                        header=not headers_written[key],
                    )
                    headers_written[key] = True
                    output_rows[key] += len(selected)
                    written_rows += len(selected)
            progress.set_postfix_str(
                f"expanded={expanded_rows:,}, written={written_rows:,}"
            )
            del batch, expanded, base, expanded_plan_keys
        del raw_chunk, raw

    if expanded_rows == 0:
        raise ValueError(f"No plan-drug rows match the plan crosswalk in {year_q}.")
    lookup_keys = ["previous_formulary_id", "ndc"]
    current_lookup = pd.concat(tier_parts, ignore_index=True).drop_duplicates(
        lookup_keys,
        keep="last",
    )
    board_counts = cast(pd.DataFrame, board_lookup).groupby(
        "ndc",
        dropna=False,
    )["boardname"].nunique()
    if board_counts.gt(1).any():
        examples = board_counts[board_counts.gt(1)].head(10).reset_index()
        raise ValueError(f"{source_path.name} maps NDCs to multiple BoardName values. Examples:\n{examples}")
    return expanded_rows, current_lookup


def build_quarter_path_panel(
    source_path: Path,
    path_cohorts: dict[int, PathCohort],
    previous_lookup: pd.DataFrame | None,
    chunksize: int,
    benefit_values: pd.DataFrame,
    benefit_counts: pd.DataFrame,
    samples: dict[tuple[str, int], CohortSample],
    sampled_histories: dict[tuple[str, int], frozenset[str]],
    path_average_eligible: dict[int, pd.DataFrame] | None,
    output_paths: dict[tuple[str, int], Path],
    headers_written: dict[tuple[str, int], bool],
    output_rows: dict[tuple[str, int], int],
    average_output_paths: dict[tuple[str, int], Path],
    average_headers_written: dict[tuple[str, int], bool],
    average_output_rows: dict[tuple[str, int], int],
    quarter: int = 0,
    write_path_output: bool = True,
    write_average_output: bool = False,
) -> tuple[int, pd.DataFrame]:
    """Write one raw formulary quarter directly at history-by-NDC level."""
    year_q = source_path.stem.removeprefix("formulary_panel_")
    history_frames = [
        path_cohort.history_quarters.loc[
            path_cohort.history_quarters["year_q"].eq(year_q)
        ]
        for path_cohort in path_cohorts.values()
        if year_q in path_cohort.required_quarters
    ]
    if not history_frames:
        return 0, pd.DataFrame(columns=["previous_formulary_id", "ndc", "previous_tier_raw"])
    histories = pd.concat(history_frames, ignore_index=True)
    histories = histories.merge(
        benefit_counts,
        on=["history_id", "data_cohort"],
        how="left",
        validate="many_to_one",
    )
    histories["n_path_copay"] = histories["n_path_copay"].fillna(0).astype("int32")
    histories["n_path_prefer"] = histories["n_path_prefer"].fillna(0).astype("int32")
    used_formularies = set(histories["formulary_id"].dropna().astype(str))

    tier_parts: list[pd.DataFrame] = []
    identity_lookup: pd.DataFrame | None = None
    expanded_rows = 0
    written_rows = 0
    average_totals: dict[tuple[str, int], pd.DataFrame] = {}
    raw_columns = (
        QUARTER_RAW_FORMULARY_COLUMNS if quarter
        else ANNUAL_RAW_FORMULARY_COLUMNS
    )
    reader = pd.read_csv(
        source_path,
        usecols=raw_columns,
        dtype="string",
        chunksize=chunksize,
    )
    for chunk_number, raw_chunk in enumerate(reader, start=1):
        raw = normalize_raw_chunk(raw_chunk, source_path.name, quarter)
        observed_quarters = set(raw["year_q"].dropna().astype(str).unique())
        if observed_quarters != {year_q}:
            raise ValueError(f"{source_path.name} contains unexpected quarters: {sorted(observed_quarters)}")
        raw = raw.loc[raw["formulary_id"].isin(used_formularies)].copy()
        if raw.empty:
            continue
        tier_parts.append(
            raw[["formulary_id", "ndc", "tier_raw"]].rename(
                columns={
                    "formulary_id": "previous_formulary_id",
                    "tier_raw": "previous_tier_raw",
                }
            )
        )
        identity_column = "id" if quarter else "boardname"
        current_identities = raw[["ndc", identity_column]].drop_duplicates()
        identity_lookup = (
            current_identities
            if identity_lookup is None
            else pd.concat(
                [identity_lookup, current_identities], ignore_index=True
            ).drop_duplicates()
        )
        raw = add_tier_key(raw, source_path.name)
        expanded = raw.merge(
            histories,
            on=["year_q", "formulary_id"],
            how="inner",
            validate="many_to_many",
        )
        if expanded.empty:
            continue
        expanded = add_tier_transitions(expanded, previous_lookup)
        # No pre-cohort formulary is used.  Consequently the first cohort
        # quarter has no defined tier transition rather than a zero change.
        no_within_cohort_predecessor = expanded["previous_formulary_id"].isna()
        expanded.loc[
            no_within_cohort_predecessor,
            ["tier_upgrade", "tier_downgrade"],
        ] = pd.NA
        expanded = expanded.merge(
            benefit_values,
            on=["history_id", "data_cohort", "year_q", "tier"],
            how="left",
            validate="many_to_one",
        )
        expanded_rows += len(expanded)
        for cohort_year in sorted(expanded["data_cohort"].unique()):
            cohort_data = expanded.loc[expanded["data_cohort"].eq(cohort_year)]
            if write_average_output:
                if path_average_eligible is None:
                    raise ValueError("Path-average eligibility was not initialized.")
                cohort_data = cohort_data.merge(
                    path_average_eligible[int(cohort_year)],
                    on=["history_id", "ndc"],
                    how="left",
                    validate="many_to_one",
                )
                cohort_data["__path_average_eligible"] = cohort_data[
                    "__path_average_eligible"
                ].fillna(False)
            for event_type in EVENT_TYPES:
                key = (event_type, int(cohort_year))
                if key not in output_paths:
                    continue
                selected_source = (
                    cohort_data
                    if write_average_output
                    else cohort_data.loc[
                        cohort_data["history_id"].isin(sampled_histories[key])
                    ]
                )
                if selected_source.empty:
                    continue
                selected = add_sample_columns(selected_source, samples[key], quarter)
                if selected.empty:
                    continue
                if write_average_output:
                    average_totals[key] = accumulate_path_average(
                        average_totals.get(key), selected,
                        path_average_columns(event_type, quarter), quarter,
                    )
                if not write_path_output:
                    continue
                sampled = (
                    selected.loc[
                        selected["history_id"].isin(sampled_histories[key])
                    ]
                    if write_average_output
                    else selected
                )
                if sampled.empty:
                    continue
                output_columns = (
                    quarterly_path_panel_columns(event_type)
                    if quarter
                    else path_panel_columns()
                )
                sampled[output_columns].to_csv(
                    output_paths[key],
                    mode="a",
                    index=False,
                    header=not headers_written[key],
                )
                headers_written[key] = True
                output_rows[key] += len(sampled)
                written_rows += len(sampled)
        print(
            f"[path] {year_q} chunk {chunk_number}: "
            f"path_rows={expanded_rows:,}, written={written_rows:,}",
            flush=True,
        )

    if expanded_rows == 0:
        raise ValueError(f"No path-by-NDC rows match the path histories in {year_q}.")
    if identity_lookup is not None:
        identity_column = "id" if quarter else "boardname"
        identity_counts = identity_lookup.groupby("ndc", dropna=False)[
            identity_column
        ].nunique()
        if identity_counts.gt(1).any():
            examples = identity_counts[identity_counts.gt(1)].head(10).reset_index()
            raise ValueError(
                f"{source_path.name} maps NDCs to multiple {identity_column} "
                f"values. Examples:\n{examples}"
            )
    for key, totals in average_totals.items():
        averaged = finish_path_average(totals, path_average_columns(key[0], quarter))
        averaged.to_csv(
            average_output_paths[key],
            mode="a",
            index=False,
            header=not average_headers_written[key],
        )
        average_headers_written[key] = True
        average_output_rows[key] += len(averaged)
        print(f"[path average] {year_q} {key}: wrote {len(averaged):,} rows", flush=True)
    lookup_keys = ["previous_formulary_id", "ndc"]
    current_lookup = pd.concat(tier_parts, ignore_index=True).drop_duplicates(
        lookup_keys,
        keep="last",
    )
    return expanded_rows, current_lookup


def load_quarter_tier_lookup(source_path: Path, chunksize: int) -> pd.DataFrame:
    """Rebuild the preceding-quarter tier lookup when resuming a path build."""
    parts: list[pd.DataFrame] = []
    reader = pd.read_csv(
        source_path,
        usecols=["FORMULARY_ID", "NDC", "tier_raw"],
        dtype="string",
        chunksize=chunksize,
    )
    for chunk in reader:
        current = pd.DataFrame(index=chunk.index)
        current["previous_formulary_id"] = normalize_numeric_identifier(
            chunk["FORMULARY_ID"]
        )
        current["ndc"] = normalize_text(chunk["NDC"])
        current["previous_tier_raw"] = parse_numeric(
            chunk["tier_raw"],
            "tier_raw",
            source_path.name,
        )
        if current[["previous_formulary_id", "ndc"]].isna().any().any():
            examples = current.loc[
                current[["previous_formulary_id", "ndc"]].isna().any(axis=1)
            ].head(10)
            raise ValueError(
                f"{source_path.name} has missing tier-lookup identifiers. Examples:\n{examples}"
            )
        parts.append(current)
    if not parts:
        raise ValueError(f"{source_path.name} contains no tier rows for checkpoint resume.")
    return pd.concat(parts, ignore_index=True).drop_duplicates(
        ["previous_formulary_id", "ndc"],
        keep="last",
    )


def path_build_output_paths(
    samples: dict[tuple[str, int], CohortSample],
    spec: AnalysisSpec,
    time_shift: int,
    quarter: int,
    path_average: int,
) -> tuple[dict[tuple[str, int], Path], dict[tuple[str, int], Path]]:
    """Resolve active event-cohort outputs for preflight and actual building."""
    output_paths = {
        key: path_cohort_output_path(spec, time_shift, *key, quarter)
        for key, sample in samples.items()
        if not quarter or sample.treated_a or sample.treated_b
    }
    if not output_paths:
        raise ValueError("No active event cohorts have treated firms.")
    average_paths = {
        key: path.parent / "path_average" / path.name
        for key, path in output_paths.items()
    } if path_average else {}
    return output_paths, average_paths


def build_path_weighted_panels(
    spec: AnalysisSpec,
    time_shift: int,
    source_paths: dict[str, Path],
    crosswalks: dict[str, pd.DataFrame],
    cohort_windows: dict[int, tuple[str, ...]],
    balanced_units: dict[int, pd.MultiIndex],
    chunksize: int,
    plan_tier: pd.DataFrame,
    samples: dict[tuple[str, int], CohortSample],
    quarter: int = 0,
    prefer_mode: int = 0,
    path_sample_fraction: float = 1.0,
    path_sample_seed: int = 0,
    path_average: int = 0,
) -> None:
    """Build all event cohort panels from full CPS formulary paths."""
    output_paths, average_output_paths = path_build_output_paths(
        samples, spec, time_shift, quarter, path_average
    )
    ordered_quarters = sorted(source_paths, key=quarter_key)
    output_dir = next(iter(output_paths.values())).parent
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / "_path_build_checkpoint.json"
    build_log_path = output_dir / "_path_build.log"
    checkpoint_existed = checkpoint_path.exists()
    checkpoint_was_legacy = False
    if checkpoint_existed and average_output_paths:
        checkpoint_was_legacy = (
            json.loads(checkpoint_path.read_text(encoding="utf-8")).get("version") == 3
        )
    (
        path_completed_quarters,
        average_completed_quarters,
        headers_written,
        output_rows,
        average_headers_written,
        average_output_rows,
    ) = initialize_path_outputs(
        checkpoint_path,
        ordered_quarters,
        output_paths,
        average_output_paths,
        spec,
        time_shift,
        quarter,
        prefer_mode,
        path_sample_fraction,
        path_sample_seed,
        path_average,
    )
    if not checkpoint_existed or checkpoint_was_legacy:
        write_path_checkpoint(
            checkpoint_path,
            path_completed_quarters,
            average_completed_quarters,
            ordered_quarters,
            output_paths,
            output_rows,
            average_output_paths,
            average_output_rows,
            spec,
            time_shift,
            quarter,
            prefer_mode,
            path_sample_fraction,
            path_sample_seed,
            path_average,
        )
        log_message = (
            "MIGRATE completed v3 path output; start path-average output"
            if checkpoint_was_legacy
            else "START fresh path-weighted build"
        )
        append_path_build_log(build_log_path, log_message)

    jointly_completed_count = (
        min(len(path_completed_quarters), len(average_completed_quarters))
        if average_output_paths
        else len(path_completed_quarters)
    )
    jointly_completed = ordered_quarters[:jointly_completed_count]
    if path_completed_quarters:
        print(
            f"[path] Completed {len(path_completed_quarters)}/"
            f"{len(ordered_quarters)} sampled-path quarters.",
            flush=True,
        )
    if average_output_paths:
        print(
            f"[path average] Completed {len(average_completed_quarters)}/"
            f"{len(ordered_quarters)} averaged quarters.",
            flush=True,
        )
    if len(jointly_completed) == len(ordered_quarters):
        print("[path] All requested outputs are already complete.", flush=True)
        return
    if jointly_completed:
        append_path_build_log(
            build_log_path,
            f"RESUME joint output after {jointly_completed[-1]} "
            f"({len(jointly_completed)}/{len(ordered_quarters)} quarters)",
        )

    print("[path] Building complete CPS formulary histories...", flush=True)
    path_cohorts = build_path_cohorts(
        crosswalks,
        balanced_units,
        cohort_windows,
        spec,
        quarter,
    )
    for cohort_year, path_cohort in sorted(path_cohorts.items()):
        history_count = path_cohort.history_quarters["history_id"].nunique()
        cps_count = int(
            path_cohort.history_quarters["n_path"].sum()
            / len(path_cohort.required_quarters)
        )
        print(
            f"[path] Cohort {cohort_year}: {cps_count:,} balanced CPS "
            f"in {history_count:,} histories.",
            flush=True,
        )
    path_pending = len(path_completed_quarters) < len(ordered_quarters)
    sampled_histories = (
        sample_path_histories(
            path_cohorts,
            samples,
            path_sample_fraction,
            path_sample_seed,
        )
        if path_pending
        else {}
    )
    if path_pending:
        print(
            f"[path] Sampled {path_sample_fraction:.1%} of distinct histories "
            f"within each event-cohort (seed={path_sample_seed}).",
            flush=True,
        )
    average_pending = bool(average_output_paths) and (
        len(average_completed_quarters) < len(ordered_quarters)
    )
    if average_pending:
        print(
            "[path average] The additional averages use all complete histories.",
            flush=True,
        )
    print("[path] Identifying active formulary tiers for benefit completeness...", flush=True)
    formulary_tiers, path_average_eligible = build_formulary_tier_schedule(
        source_paths,
        path_cohorts,
        chunksize,
        int(average_pending),
    )
    print("[path] Computing outcome-specific valid CPS counts and path-level means...", flush=True)
    benefit_values, benefit_counts = build_benefit_path_features(
        path_cohorts,
        formulary_tiers,
        plan_tier,
        spec,
    )
    print(
        f"[path] Outcome-specific benefit features ready for "
        f"{len(benefit_counts):,} histories; writing pending outputs...",
        flush=True,
    )

    previous_lookup: pd.DataFrame | None = None
    previous_output_quarter: str | None = None
    if jointly_completed and len(jointly_completed) < len(ordered_quarters):
        previous_output_quarter = jointly_completed[-1]
        next_quarter = ordered_quarters[len(jointly_completed)]
        if previous_output_quarter == previous_year_q(next_quarter):
            print(
                f"[path] Rebuilding {previous_output_quarter} tier lookup for resume...",
                flush=True,
            )
            previous_lookup = load_quarter_tier_lookup(
                source_paths[previous_output_quarter],
                chunksize,
            )

    for quarter_position, year_q in enumerate(ordered_quarters, start=1):
        write_path_output = year_q not in path_completed_quarters
        write_average_output = (
            bool(average_output_paths)
            and year_q not in average_completed_quarters
        )
        if not write_path_output and not write_average_output:
            continue
        print(
            f"[path] START quarter {quarter_position}/{len(ordered_quarters)}: {year_q}",
            flush=True,
        )
        append_path_build_log(
            build_log_path,
            f"START quarter {quarter_position}/{len(ordered_quarters)} {year_q}",
        )
        prior_year_q = previous_year_q(year_q)
        tier_lookup = previous_lookup if previous_output_quarter == prior_year_q else None
        try:
            _, current_lookup = build_quarter_path_panel(
                source_paths[year_q],
                path_cohorts,
                tier_lookup,
                chunksize,
                benefit_values,
                benefit_counts,
                samples,
                sampled_histories,
                path_average_eligible,
                output_paths,
                headers_written,
                output_rows,
                average_output_paths,
                average_headers_written,
                average_output_rows,
                quarter,
                write_path_output,
                write_average_output,
            )
        except BaseException:
            append_path_build_log(
                build_log_path,
                f"FAILED {year_q}\n{traceback.format_exc()}",
            )
            print(
                f"[path] FAILED {year_q}. Rerun the same command to roll back "
                "this partial quarter and resume.",
                flush=True,
            )
            raise
        previous_lookup = current_lookup
        previous_output_quarter = year_q
        if write_path_output:
            path_completed_quarters.append(year_q)
        if write_average_output:
            average_completed_quarters.append(year_q)
        write_path_checkpoint(
            checkpoint_path,
            path_completed_quarters,
            average_completed_quarters,
            ordered_quarters,
            output_paths,
            output_rows,
            average_output_paths,
            average_output_rows,
            spec,
            time_shift,
            quarter,
            prefer_mode,
            path_sample_fraction,
            path_sample_seed,
            path_average,
        )
        print(
            f"[path] COMMITTED quarter {quarter_position}/{len(ordered_quarters)}: {year_q}",
            flush=True,
        )
        append_path_build_log(
            build_log_path,
            f"COMMITTED quarter {quarter_position}/{len(ordered_quarters)} {year_q}",
        )

    empty = [key for key, wrote in headers_written.items() if not wrote]
    if empty:
        raise ValueError(f"No path-weighted cohort rows were written for: {empty}")
    average_empty = [
        key for key, wrote in average_headers_written.items() if not wrote
    ]
    if average_empty:
        raise ValueError(f"No path-average cohort rows were written for: {average_empty}")
    append_path_build_log(build_log_path, "COMPLETE all path-weighted quarters")
    for path in sorted(output_paths.values()):
        size_gb = path.stat().st_size / (1024**3)
        print(f"Wrote path panel ({size_gb:.2f} GB): {path}")
    for path in sorted(average_output_paths.values()):
        size_gb = path.stat().st_size / (1024**3)
        print(f"Wrote path-average panel ({size_gb:.2f} GB): {path}")


# ========================== OUTPUT DISPATCH ==========================


def main() -> None:
    """Build either legacy sampled CPS panels or full path-weighted panels."""
    (
        spec,
        time_shift,
        sample_fraction,
        random_seed,
        path_sample_fraction,
        path_sample_seed,
        path_weighted_mode,
        chunksize,
        max_expanded_rows,
        quarter,
        prefer_mode,
        path_average,
    ) = validate_config(RUN_CONFIG)
    if quarter and path_weighted_mode != 1:
        raise ValueError("Quarterly event cohorts require path_weighted_mode=1")
    if path_weighted_mode == 1:
        print("[path] Inventorying quarterly formulary inputs...", flush=True)
    source_paths = quarter_panel_paths(formulary_input_dir(time_shift, quarter))
    quarter_sources: QuarterEventSources | None = None
    if quarter:
        quarter_sources = load_quarter_event_sources()
        specifications = quarter_cohort_specifications(quarter_sources.movement)
        if not specifications:
            raise ValueError("No quarterly req1 event cohorts were found")
        required, windows = required_quarter_windows(source_paths, specifications)
        cohort_windows = {
            year * 4 + event_quarter: tuple(windows[(year, event_quarter)])
            for _, year, event_quarter in specifications
        }
        source_paths = {tag: source_paths[tag] for tag in required}
    else:
        cohort_windows = {
            cohort_year: tuple(cohort_quarters(cohort_year, set(source_paths), time_shift))
            for cohort_year in COHORT_YEARS
        }
    target_quarters = set(source_paths)
    source_quarters = {shift_year_q(year_q, -time_shift) for year_q in target_quarters}
    samples = build_samples(cohort_windows, quarter, quarter_sources)

    if path_weighted_mode == 1:
        output_paths, average_paths = path_build_output_paths(
            samples, spec, time_shift, quarter, path_average
        )
        checkpoint_path = (
            next(iter(output_paths.values())).parent / "_path_build_checkpoint.json"
        )
        print("[path] Checking checkpoint and existing output headers...", flush=True)
        initialize_path_outputs(
            checkpoint_path,
            sorted(source_paths, key=quarter_key),
            output_paths,
            average_paths,
            spec,
            time_shift,
            quarter,
            prefer_mode,
            path_sample_fraction,
            path_sample_seed,
            path_average,
            validate_only=True,
        )
        print("[path] Checkpoint validation passed.", flush=True)

    if path_weighted_mode == 1:
        print("[path] Loading complete CPS-quarter-formulary crosswalk...", flush=True)
    if quarter:
        crosswalks, copay = load_quarter_copay_inputs(
            BENEFICIARY_COPAY_PATH,
            time_shift,
            target_quarters,
            chunksize,
        )
        print(
            "[balance] Loading availability from reorganized quarter panels...",
            flush=True,
        )
        formulary_availability = load_panel_formulary_availability(
            source_paths, chunksize
        )
    else:
        crosswalks = build_plan_crosswalks(spec, time_shift, target_quarters)
        print(
            "[balance] Loading formulary availability from the legacy expanded source...",
            flush=True,
        )
        formulary_availability = load_formulary_quarter_availability(
            LEGACY_EXPANDED_FORMULARY_PATH,
            time_shift,
            target_quarters,
            chunksize,
        )
        copay = load_copay_lookup(time_shift)
    print(
        "[balance] Selecting CPS with complete plan and formulary coverage...",
        flush=True,
    )
    balanced_units = balanced_plan_units(
        crosswalks,
        cohort_windows,
        spec,
        formulary_availability=formulary_availability,
    )
    prefer = load_prefer_lookup(time_shift) if prefer_mode else None
    plan_tier = combine_plan_tier_features(copay, prefer)
    if path_weighted_mode == 1:
        build_path_weighted_panels(
            spec,
            time_shift,
            source_paths,
            crosswalks,
            cohort_windows,
            balanced_units,
            chunksize,
            plan_tier,
            samples,
            quarter,
            prefer_mode,
            path_sample_fraction,
            path_sample_seed,
            path_average,
        )
        return

    first_seen = load_first_seen_lookup(time_shift)
    sampled_units = sample_plan_units(
        balanced_units,
        sample_fraction,
        random_seed,
    )
    parent = load_parent_lookup(source_quarters, time_shift)
    output_paths = {
        (event_type, cohort_year): cohort_output_path(
            spec,
            time_shift,
            event_type,
            cohort_year,
        )
        for event_type in EVENT_TYPES
        for cohort_year in COHORT_YEARS
    }
    for path in output_paths.values():
        prepare_output_path(path)
    headers_written = {key: False for key in output_paths}
    output_rows = {key: 0 for key in output_paths}

    ordered_quarters = sorted(target_quarters, key=quarter_key)
    progress = tqdm(ordered_quarters, desc="Building plan-drug cohorts", unit="quarter")
    previous_lookup: pd.DataFrame | None = None
    previous_output_quarter: str | None = None
    for year_q in progress:
        progress.set_postfix_str(year_q)
        prior_year_q = previous_year_q(year_q)
        tier_lookup = previous_lookup if previous_output_quarter == prior_year_q else None
        _, current_lookup = build_quarter_panel(
            source_paths[year_q],
            crosswalks[year_q],
            crosswalks.get(prior_year_q),
            tier_lookup,
            spec,
            chunksize,
            max_expanded_rows,
            plan_tier,
            parent,
            first_seen,
            cohort_windows,
            sampled_units,
            samples,
            output_paths,
            headers_written,
            output_rows,
        )
        previous_lookup = current_lookup
        previous_output_quarter = year_q

    empty = [key for key, wrote in headers_written.items() if not wrote]
    if empty:
        raise ValueError(f"No cohort rows were written for: {empty}")
    for key, count in sorted(output_rows.items()):
        print(f"Wrote {count:,} rows: {output_paths[key]}")


if __name__ == "__main__":
    main()
