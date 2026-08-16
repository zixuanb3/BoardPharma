r"""
Purpose:
Aggregate quarter-organized formulary rows to a configurable drug-firm-state,
drug-firm-insurer, or drug-firm-state-insurer level and build balanced,
direction-aware formulary event cohorts.

Process:
1. Map each source-quarter formulary to the configured state, CMS Parent
   Organization, or joint state-insurer dimension. State modes expand blank-state
   PDP and MA region rows with the saved region-state crosswalks.
2. Apply the configured formulary quarter shift to mapping timing, then stream
   each formulary quarter and aggregate within the selected cell x YEAR_Q.
3. Construct included_count, included_share, mean_tiera, and mean_tier_raw
   within each new cell while retaining the original req1 event and
   event-direction-specific ATC3-sharing indicators.
4. Keep globally defined NDC first-seen timing and balanced new-cell ids. Side A
   treats FirmA as treated and FirmB as counterpart; side B treats FirmB as
   treated and FirmA as counterpart. Treatment is assigned by BoardName, not
   NDC first-seen timing, and share remains the precomputed ATC3-sharing measure.

Input:
- data/formulary_panel_by_time/formulary_panel_YYYYQX.csv
- InterimData/merged_plan_information.csv
- data/directory/Monthly_Report_By_Contract_YYYY_MM.csv
- crosswalks/pdp_region_state_crosswalk.csv
- crosswalks/ma_region_state_crosswalk.csv
- The first-seen and event-table inputs used by FormularyCohortPanelMaker.py

Output:
- data/formulary_{dimension}_crosswalk_by_time/
  formulary_{dimension}_crosswalk_YYYYQX.csv
- data/formulary_drug_{dimension}_panel_by_time/
  formulary_drug_{dimension}_panel_YYYYQX.csv
- data/formulary_{dimension}_cohort_data/event/req1/Not/
  {event}_quarter_cohort_{year}.csv

Here {dimension} is state, insurer, or state_insurer.
"""

from __future__ import annotations

import gc
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from tqdm.auto import tqdm

import FormularyCohortPanelMaker as base


# Configure project directory paths
CURRENT_PATH = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_PATH.parent.parent
DATA_ROOT = PROJECT_ROOT / "data"
PLAN_INFO_PATH = PROJECT_ROOT / "InterimData" / "merged_plan_information.csv"
DIRECTORY_ROOT = DATA_ROOT / "directory"
REGION_CROSSWALK_DIR = PROJECT_ROOT / "crosswalks"
PDP_REGION_CROSSWALK_PATH = REGION_CROSSWALK_DIR / "pdp_region_state_crosswalk.csv"
MA_REGION_CROSSWALK_PATH = REGION_CROSSWALK_DIR / "ma_region_state_crosswalk.csv"

MONTHLY_PATTERN = re.compile(
    r"Monthly_Report_By_Contract_([12][0-9]{3})_([0-9]{2})\.csv$"
)
YEAR_Q_PATTERN = re.compile(r"^([12][0-9]{3})Q([1-4])$")
UNMAPPED_STATE = "UNMAPPED_STATE"
UNMAPPED_PARENT_ORGANIZATION = "UNMAPPED_PARENT_ORGANIZATION"

EVENT_TYPES = base.EVENT_TYPES
TREATMENT_GROUPS = base.TREATMENT_GROUPS
COHORT_YEARS = base.COHORT_YEARS
RAW_EVENT_COLUMNS = base.RAW_EVENT_COLUMNS
RAW_SHARING_COLUMNS = base.RAW_SHARING_COLUMNS
RAW_TO_OUTPUT_COLUMNS = base.RAW_TO_OUTPUT_COLUMNS
RAW_REQUIRED_COLUMNS = base.RAW_REQUIRED_COLUMNS
DRUG_ID_COLUMNS = ["ndc", "boardname"]


# ========================== USER CONFIG ==========================
# chunksize:
# - Controls how many full formulary or plan-information rows are read at once.
#
# window_pre/window_post:
# - A cohort c uses every available quarter in years c-window_pre through
#   c+window_post, subject to the same data-edge rule as the original script.
#
# formulary_time_shift_quarters:
# - Must match FormularyPanelMaker.py and ReorganizeFormularyData.py. State and
#   insurer data are selected in the source quarter and shifted by the same lag.
#
# analysis_dimension:
# - "state" aggregates by NDC x BoardName x state.
# - "insurer" aggregates by NDC x BoardName x CMS Parent Organization.
# - "both" preserves the joint NDC x BoardName x state x insurer analysis.
#
# first_seen_year_offset/first_seen_quarter:
# - Applies the original global NDC first-seen cutoff. It never recomputes
#   first-seen timing within the selected cells.
RUN_CONFIG = {
    "chunksize": 500_000,
    "window_pre": 1,
    "window_post": 1,
    "req": 1,
    "include_eventpair": 0,
    "atc_level": 3,
    "formulary_time_shift_quarters": 1,
    "analysis_dimension": "state",
    "first_seen_year_offset": 1,
    "first_seen_quarter": 4,
}
# ===============================================================


# ========================== ANALYSIS DIMENSION ==========================


@dataclass(frozen=True)
class AnalysisSpec:
    """Describe one supported geographic/insurer aggregation level."""

    name: str
    dimensions: tuple[str, ...]
    mapping_flags: tuple[str, ...]

    @property
    def slug(self) -> str:
        """Return the filename and directory token for this specification."""
        return "state_insurer" if self.name == "both" else self.name

    @property
    def label(self) -> str:
        """Return a readable label for progress and validation messages."""
        return self.slug.replace("_", "-")

    @property
    def includes_state(self) -> bool:
        """Return whether state mapping is required."""
        return "state" in self.dimensions

    @property
    def includes_insurer(self) -> bool:
        """Return whether Parent Organization mapping is required."""
        return "parent_organization" in self.dimensions

    @property
    def cell_id_columns(self) -> list[str]:
        """Return the complete drug-firm cell identifier."""
        return [*DRUG_ID_COLUMNS, *self.dimensions]

    @property
    def plan_info_columns(self) -> list[str]:
        """Return only merged-plan columns needed for this specification."""
        columns = ["YEAR_Q", "CONTRACT_ID", "FORMULARY_ID"]
        if self.includes_state:
            columns.extend(["STATE", "PDP_REGION_CODE", "MA_REGION_CODE"])
        return columns

    @property
    def crosswalk_columns(self) -> list[str]:
        """Return the saved crosswalk schema in stable column order."""
        columns = [
            "year_q",
            "source_year_q",
            "formulary_id",
            *self.dimensions,
            *self.mapping_flags,
        ]
        if self.includes_state:
            columns.append("state_source")
        columns.append("source_contract_count")
        if self.includes_insurer:
            columns.append("representative_month")
        return columns

    @property
    def crosswalk_output_dir(self) -> Path:
        """Return the unshifted crosswalk output root."""
        return DATA_ROOT / f"formulary_{self.slug}_crosswalk_by_time"

    @property
    def drug_output_dir(self) -> Path:
        """Return the unshifted drug-quarter output root."""
        return DATA_ROOT / f"formulary_drug_{self.slug}_panel_by_time"

    @property
    def cohort_output_dir(self) -> Path:
        """Return the unshifted cohort output root."""
        return (
            DATA_ROOT / f"formulary_{self.slug}_cohort_data" / "event" / "req1" / "Not"
        )


ANALYSIS_SPECS = {
    "state": AnalysisSpec("state", ("state",), ("state_mapped",)),
    "insurer": AnalysisSpec(
        "insurer",
        ("parent_organization",),
        ("parent_org_matched",),
    ),
    "both": AnalysisSpec(
        "both",
        ("state", "parent_organization"),
        ("state_mapped", "parent_org_matched"),
    ),
}


def analysis_spec(config: dict) -> AnalysisSpec:
    """Validate and return the configured analysis dimension."""
    name = str(config.get("analysis_dimension", "")).strip().lower()
    if name not in ANALYSIS_SPECS:
        allowed = ", ".join(ANALYSIS_SPECS)
        raise ValueError(
            f"analysis_dimension must be one of: {allowed}; found {name!r}."
        )
    return ANALYSIS_SPECS[name]


# ===============================================================


# ========================== MAPPING HELPERS ==========================


def normalize_contract(values: pd.Series) -> pd.Series:
    """Normalize CMS contract identifiers without changing letter prefixes."""
    result = base.normalize_string(values, uppercase=True)
    return result.str.replace(r"^([0-9]+)\.0$", r"\1", regex=True)


def normalize_formulary(values: pd.Series) -> pd.Series:
    """Normalize formulary identifiers so zero padding does not affect merges."""
    result = base.normalize_string(values)
    result = result.str.replace(r"^([0-9]+)\.0$", r"\1", regex=True)
    stripped = result.str.lstrip("0")
    return stripped.mask(result.notna() & stripped.eq(""), "0")


def normalize_region_code(values: pd.Series) -> pd.Series:
    """Normalize PDP and MA region codes to two-character strings."""
    result = base.normalize_string(values, uppercase=True)
    result = result.str.replace(r"^([0-9]+)\.0$", r"\1", regex=True)
    numeric = result.str.fullmatch(r"[0-9]+", na=False)
    result.loc[numeric] = result.loc[numeric].str.zfill(2)
    return result


def normalize_year_q(values: pd.Series, source_name: str) -> pd.Series:
    """Normalize values such as '2020 Q2' to compact YYYYQX labels."""
    text = (
        values.astype("string")
        .str.strip()
        .str.upper()
        .str.replace(" ", "", regex=False)
    )
    parsed = text.str.extract(r"^([12][0-9]{3})Q([1-4])$")
    invalid = text.notna() & text.ne("") & parsed[0].isna()
    if invalid.any():
        examples = values.loc[invalid].drop_duplicates().head(10).tolist()
        raise ValueError(f"{source_name} has invalid YEAR_Q values: {examples}")
    return parsed[0] + "Q" + parsed[1]


def shift_year_q(year_q: str, shift_quarters: int) -> str:
    """Shift one compact quarter label by a fixed number of quarters."""
    match = YEAR_Q_PATTERN.fullmatch(year_q)
    if match is None:
        raise ValueError(f"Invalid compact YEAR_Q value: {year_q}")
    qtime = int(match.group(1)) * 4 + int(match.group(2)) + shift_quarters
    if qtime <= 0:
        raise ValueError(f"{year_q} cannot be shifted by {shift_quarters} quarters.")
    shifted_year = (qtime - 1) // 4
    shifted_quarter = (qtime - 1) % 4 + 1
    return base.canonical_year_q(shifted_year, shifted_quarter)


def representative_month(source_year_q: str) -> str:
    """Apply Q1=March, Q2=June, Q3=September, Q4=next January."""
    match = YEAR_Q_PATTERN.fullmatch(source_year_q)
    if match is None:
        raise ValueError(f"Invalid source YEAR_Q value: {source_year_q}")
    year = int(match.group(1))
    quarter = int(match.group(2))
    if quarter == 1:
        return f"{year}_03"
    if quarter == 2:
        return f"{year}_06"
    if quarter == 3:
        return f"{year}_09"
    return f"{year + 1}_01"


def shifted_output_dir(base_dir: Path, shift_quarters: int) -> Path:
    """Return a timing-specific output directory."""
    if shift_quarters == 0:
        return base_dir
    return base_dir / base.shift_label(shift_quarters)


def cohort_output_dir(
    spec: AnalysisSpec,
    shift_quarters: int,
    first_seen_year_offset: int,
    first_seen_quarter: int,
) -> Path:
    """Return the selected cohort destination for one sample definition."""
    if shift_quarters == 0 and first_seen_year_offset == 0 and first_seen_quarter == 1:
        return spec.cohort_output_dir
    label = base.sample_spec_label(
        shift_quarters,
        first_seen_year_offset,
        first_seen_quarter,
    )
    return spec.cohort_output_dir / label


def read_region_crosswalk(path: Path, region_type: str) -> pd.DataFrame:
    """Read and validate one saved long-format region-state crosswalk."""
    if not path.exists():
        raise FileNotFoundError(f"{region_type} region crosswalk not found: {path}")
    crosswalk = pd.read_csv(
        path,
        dtype="string",
        keep_default_na=False,
    )
    required = {"region_code", "state"}
    missing = sorted(required - set(crosswalk.columns))
    if missing:
        raise KeyError(f"{path.name} is missing columns: {missing}")

    crosswalk = crosswalk[["region_code", "state"]].copy()
    crosswalk["region_code"] = normalize_region_code(crosswalk["region_code"])
    crosswalk["state"] = base.normalize_string(
        crosswalk["state"],
        uppercase=True,
    )
    if crosswalk.isna().any().any():
        raise ValueError(f"{path.name} contains blank region codes or states.")
    if not crosswalk["state"].str.fullmatch(r"[A-Z]{2}").all():
        examples = crosswalk.loc[
            ~crosswalk["state"].str.fullmatch(r"[A-Z]{2}"),
            "state",
        ].head(10)
        raise ValueError(
            f"{path.name} contains invalid state abbreviations: " f"{examples.tolist()}"
        )
    if crosswalk.duplicated(["region_code", "state"]).any():
        raise ValueError(f"{path.name} contains duplicate region-state rows.")
    return crosswalk.sort_values(["region_code", "state"]).reset_index(drop=True)


def monthly_directory_paths() -> dict[str, Path]:
    """Inventory the CMS Monthly Report by Contract files by year-month."""
    paths: dict[str, Path] = {}
    for path in DIRECTORY_ROOT.rglob("Monthly_Report_By_Contract_*.csv"):
        match = MONTHLY_PATTERN.fullmatch(path.name)
        if match is None:
            continue
        year_month = f"{match.group(1)}_{match.group(2)}"
        if year_month in paths:
            raise ValueError(
                f"Duplicate CMS monthly files for {year_month}: "
                f"{paths[year_month]} and {path}"
            )
        paths[year_month] = path
    if not paths:
        raise FileNotFoundError(
            f"No CMS Monthly Report by Contract files found under {DIRECTORY_ROOT}"
        )
    return paths


def read_one_monthly_directory(path: Path, year_month: str) -> pd.DataFrame:
    """Read contract-to-Parent Organization values from one CMS month."""
    try:
        data = pd.read_csv(
            path,
            dtype="string",
            keep_default_na=False,
            encoding="utf-8-sig",
        )
    except UnicodeDecodeError:
        data = pd.read_csv(
            path,
            dtype="string",
            keep_default_na=False,
            encoding="cp1252",
        )
    data.columns = [column.strip() for column in data.columns]
    required = {"Contract Number", "Parent Organization"}
    missing = sorted(required - set(data.columns))
    if missing:
        raise KeyError(f"{path.name} is missing columns: {missing}")

    result = data[["Contract Number", "Parent Organization"]].copy()
    result["contract_id"] = normalize_contract(result["Contract Number"])
    result["parent_organization"] = base.normalize_string(result["Parent Organization"])
    result = result.loc[
        result["contract_id"].notna() & result["parent_organization"].notna(),
        ["contract_id", "parent_organization"],
    ].drop_duplicates()

    conflicts = result.groupby("contract_id")["parent_organization"].nunique()
    conflicts = conflicts[conflicts.gt(1)]
    if not conflicts.empty:
        examples = conflicts.head(10).index.tolist()
        raise ValueError(
            f"{path.name} assigns multiple Parent Organizations to contracts: "
            f"{examples}"
        )
    result = result.drop_duplicates("contract_id")
    result["representative_month"] = year_month
    return result


def load_parent_organization_lookup(
    source_quarters: set[str],
) -> pd.DataFrame:
    """Load only representative CMS months needed by the requested quarters."""
    required_months = sorted(
        {representative_month(year_q) for year_q in source_quarters}
    )
    paths = monthly_directory_paths()
    missing = [month for month in required_months if month not in paths]
    if missing:
        raise FileNotFoundError(
            f"Missing CMS representative-month directories: {missing}"
        )
    frames = [
        read_one_monthly_directory(paths[month], month) for month in required_months
    ]
    lookup = pd.concat(frames, ignore_index=True)
    if lookup.duplicated(["contract_id", "representative_month"]).any():
        raise ValueError("CMS directory lookup is not unique by contract and month.")
    return lookup


def expand_plan_chunk_to_states(
    plan: pd.DataFrame,
    pdp_crosswalk: pd.DataFrame,
    ma_crosswalk: pd.DataFrame,
) -> pd.DataFrame:
    """Expand explicit-state and blank-state region rows to state-level rows."""
    plan = plan.reset_index(drop=True).copy()
    plan["_plan_row_id"] = np.arange(len(plan), dtype=np.int64)
    explicit = plan.loc[plan["state"].notna()].copy()
    explicit["state_source"] = "explicit_state"

    blank = plan.loc[plan["state"].isna()].copy()
    expanded_parts = [explicit]
    resolved_row_ids: set[int] = set()

    for code_column, region_type, region_crosswalk in (
        ("pdp_region_code", "pdp_region", pdp_crosswalk),
        ("ma_region_code", "ma_region", ma_crosswalk),
    ):
        region_rows = blank.loc[blank[code_column].notna()].copy()
        if region_rows.empty:
            continue
        expanded = region_rows.drop(columns="state").merge(
            region_crosswalk,
            how="inner",
            left_on=code_column,
            right_on="region_code",
            validate="many_to_many",
        )
        if expanded.empty:
            continue
        expanded["state_source"] = region_type
        expanded_parts.append(expanded)
        resolved_row_ids.update(expanded["_plan_row_id"].astype(int))

    unresolved = blank.loc[~blank["_plan_row_id"].isin(resolved_row_ids)].copy()
    if not unresolved.empty:
        unresolved["state"] = UNMAPPED_STATE
        unresolved["state_source"] = "unmapped_state"
        expanded_parts.append(unresolved)

    expanded_plan = pd.concat(expanded_parts, ignore_index=True, sort=False)
    expanded_plan["state_mapped"] = (
        expanded_plan["state"].ne(UNMAPPED_STATE).astype("int8")
    )
    return expanded_plan


def prepare_plan_chunk(
    chunk: pd.DataFrame,
    source_quarters: set[str],
    time_shift: int,
    spec: AnalysisSpec,
    insurer_lookup: pd.DataFrame | None,
    pdp_crosswalk: pd.DataFrame | None,
    ma_crosswalk: pd.DataFrame | None,
    source_name: str,
) -> pd.DataFrame:
    """Normalize and map one plan-information chunk to selected dimensions."""
    chunk["source_year_q"] = normalize_year_q(chunk["YEAR_Q"], source_name)
    chunk = chunk.loc[chunk["source_year_q"].isin(source_quarters)].copy()
    if chunk.empty:
        return pd.DataFrame(columns=spec.crosswalk_columns)

    chunk["year_q"] = chunk["source_year_q"].map(
        lambda value: shift_year_q(value, time_shift)
    )
    chunk["contract_id"] = normalize_contract(chunk["CONTRACT_ID"])
    chunk["formulary_id"] = normalize_formulary(chunk["FORMULARY_ID"])
    chunk = chunk.loc[chunk["formulary_id"].notna()].copy()

    if spec.includes_insurer:
        if insurer_lookup is None:
            raise AssertionError("Insurer lookup is required for this mode.")
        chunk["representative_month"] = chunk["source_year_q"].map(representative_month)
        chunk = chunk.merge(
            insurer_lookup,
            how="left",
            on=["contract_id", "representative_month"],
            validate="many_to_one",
        )
        chunk["parent_org_matched"] = (
            chunk["parent_organization"].notna().astype("int8")
        )
        chunk["parent_organization"] = chunk["parent_organization"].fillna(
            UNMAPPED_PARENT_ORGANIZATION
        )

    if spec.includes_state:
        if pdp_crosswalk is None or ma_crosswalk is None:
            raise AssertionError("Region crosswalks are required for this mode.")
        chunk["state"] = base.normalize_string(
            chunk["STATE"],
            uppercase=True,
        )
        invalid_state = chunk["state"].notna() & ~chunk["state"].str.fullmatch(
            r"[A-Z]{2}", na=False
        )
        if invalid_state.any():
            examples = chunk.loc[invalid_state, "STATE"].drop_duplicates().head(10)
            raise ValueError(
                f"{source_name} has invalid explicit STATE values: "
                f"{examples.tolist()}"
            )
        chunk["pdp_region_code"] = normalize_region_code(chunk["PDP_REGION_CODE"])
        chunk["ma_region_code"] = normalize_region_code(chunk["MA_REGION_CODE"])
        chunk = expand_plan_chunk_to_states(
            chunk,
            pdp_crosswalk,
            ma_crosswalk,
        )

    keep_columns = [
        "year_q",
        "source_year_q",
        "formulary_id",
        *spec.dimensions,
        *spec.mapping_flags,
        "contract_id",
    ]
    if spec.includes_state:
        keep_columns.append("state_source")
    if spec.includes_insurer:
        keep_columns.append("representative_month")
    return chunk[keep_columns].drop_duplicates().reset_index(drop=True)


def collapse_plan_crosswalk(
    plan: pd.DataFrame,
    spec: AnalysisSpec,
) -> pd.DataFrame:
    """Collapse plan rows to unique selected formulary-quarter cells."""
    keys = ["year_q", "formulary_id", *spec.dimensions]
    aggregation: dict[str, tuple[str, object]] = {
        "source_year_q": ("source_year_q", "first"),
        **{column: (column, "max") for column in spec.mapping_flags},
        "source_contract_count": ("contract_id", "nunique"),
    }
    if spec.includes_state:
        aggregation["state_source"] = (
            "state_source",
            lambda values: "|".join(sorted(set(values.dropna()))),
        )
    if spec.includes_insurer:
        aggregation["representative_month"] = (
            "representative_month",
            "first",
        )
    result = plan.groupby(keys, as_index=False, sort=False, dropna=False).agg(
        **aggregation
    )
    if result.duplicated(keys).any():
        raise AssertionError(
            f"Collapsed crosswalk is not unique by formulary-{spec.label}-quarter."
        )
    result[list(spec.mapping_flags)] = result[list(spec.mapping_flags)].astype("int8")
    result["source_contract_count"] = result["source_contract_count"].astype("int32")
    return (
        result[spec.crosswalk_columns]
        .sort_values(["year_q", "formulary_id", *spec.dimensions])
        .reset_index(drop=True)
    )


def build_plan_crosswalk(
    output_quarters: list[str],
    time_shift: int,
    chunksize: int,
    spec: AnalysisSpec,
) -> pd.DataFrame:
    """Build all requested formulary mappings for the selected dimensions."""
    source_quarters = {shift_year_q(year_q, -time_shift) for year_q in output_quarters}
    pdp_crosswalk = (
        read_region_crosswalk(PDP_REGION_CROSSWALK_PATH, "PDP")
        if spec.includes_state
        else None
    )
    ma_crosswalk = (
        read_region_crosswalk(MA_REGION_CROSSWALK_PATH, "MA")
        if spec.includes_state
        else None
    )
    insurer_lookup = (
        load_parent_organization_lookup(source_quarters)
        if spec.includes_insurer
        else None
    )

    parts: list[pd.DataFrame] = []
    reader = pd.read_csv(
        PLAN_INFO_PATH,
        usecols=spec.plan_info_columns,
        dtype="string",
        keep_default_na=False,
        chunksize=chunksize,
    )
    progress = tqdm(
        reader,
        desc=f"Building {spec.label} crosswalk",
        unit="plan chunk",
    )
    for chunk in progress:
        part = prepare_plan_chunk(
            chunk,
            source_quarters,
            time_shift,
            spec,
            insurer_lookup,
            pdp_crosswalk,
            ma_crosswalk,
            PLAN_INFO_PATH.name,
        )
        if not part.empty:
            parts.append(part)
            progress.set_postfix_str(
                f"expanded rows={sum(len(item) for item in parts):,}"
            )
        del chunk, part
        gc.collect()

    if not parts:
        raise ValueError(
            "No merged plan-information rows matched the requested quarters."
        )
    combined = pd.concat(parts, ignore_index=True)
    result = collapse_plan_crosswalk(combined, spec)
    missing_quarters = sorted(set(output_quarters) - set(result["year_q"]))
    if missing_quarters:
        raise ValueError(
            f"Plan information produced no crosswalk for: {missing_quarters}"
        )
    del parts, combined
    gc.collect()
    return result


def save_quarter_crosswalks(
    crosswalk: pd.DataFrame,
    output_quarters: list[str],
    destination_dir: Path,
    spec: AnalysisSpec,
) -> dict[str, Path]:
    """Save one long-format selected-dimension crosswalk per quarter."""
    paths: dict[str, Path] = {}
    for year_q in output_quarters:
        path = destination_dir / f"formulary_{spec.slug}_crosswalk_{year_q}.csv"
        base.prepare_output_path(path, overwrite=True)
        quarter = crosswalk.loc[crosswalk["year_q"].eq(year_q)].copy()
        if quarter.empty:
            raise ValueError(f"Crosswalk is empty for {year_q}.")
        quarter.to_csv(path, index=False)
        paths[year_q] = path
    return paths


# ========================== CELL-QUARTER AGGREGATION ==========================


def read_quarter_crosswalk(
    path: Path,
    spec: AnalysisSpec,
) -> pd.DataFrame:
    """Read and validate one saved selected-dimension crosswalk."""
    crosswalk = pd.read_csv(
        path,
        dtype="string",
        keep_default_na=False,
    )
    required = {
        "formulary_id",
        *spec.dimensions,
        *spec.mapping_flags,
    }
    missing = sorted(required - set(crosswalk.columns))
    if missing:
        raise KeyError(f"{path.name} is missing columns: {missing}")
    crosswalk["formulary_id"] = normalize_formulary(crosswalk["formulary_id"])
    if spec.includes_state:
        crosswalk["state"] = base.normalize_string(
            crosswalk["state"],
            uppercase=True,
        )
    if spec.includes_insurer:
        crosswalk["parent_organization"] = base.normalize_string(
            crosswalk["parent_organization"]
        )
    for column in spec.mapping_flags:
        crosswalk[column] = pd.to_numeric(
            crosswalk[column],
            errors="raise",
        ).astype("int8")
    keys = ["formulary_id", *spec.dimensions]
    if crosswalk[keys].isna().any().any():
        raise ValueError(f"{path.name} contains missing cell identifiers.")
    if crosswalk.duplicated(keys).any():
        raise ValueError(f"{path.name} is not unique by formulary-{spec.label}.")
    return crosswalk[["formulary_id", *spec.dimensions, *spec.mapping_flags]]


def aggregate_chunk(
    chunk: pd.DataFrame,
    crosswalk: pd.DataFrame,
    source_name: str,
    spec: AnalysisSpec,
) -> tuple[pd.DataFrame, set[str]]:
    """Expand and aggregate one raw formulary chunk to additive cell statistics."""
    chunk["NDC"] = base.normalize_string(chunk["NDC"])
    chunk["BoardName"] = base.normalize_string(
        chunk["BoardName"],
        uppercase=True,
    )
    chunk["FORMULARY_ID"] = normalize_formulary(chunk["FORMULARY_ID"])
    identifiers = ["NDC", "BoardName", "FORMULARY_ID"]
    if chunk[identifiers].isna().any().any():
        raise ValueError(
            f"{source_name} contains missing NDC, BoardName, or FORMULARY_ID."
        )

    chunk["included"] = base.numeric_column(chunk, "included", source_name)
    if not chunk["included"].isin([0, 1]).all():
        raise ValueError(f"{source_name} contains included values outside 0/1.")
    chunk["tierA"] = base.numeric_column(chunk, "tierA", source_name)
    chunk["tier_raw"] = base.numeric_column(
        chunk,
        "tier_raw",
        source_name,
    )
    if chunk["tierA"].isna().any():
        raise ValueError(f"{source_name} contains missing tierA values.")

    for column in [*RAW_EVENT_COLUMNS, *RAW_SHARING_COLUMNS]:
        chunk[column] = base.numeric_column(chunk, column, source_name)
        if not chunk[column].isin([0, 1]).all():
            raise ValueError(f"{source_name} contains {column} values outside 0/1.")

    expanded = chunk.merge(
        crosswalk,
        how="left",
        left_on="FORMULARY_ID",
        right_on="formulary_id",
        validate="many_to_many",
        indicator="_cell_merge",
    )
    missing_formularies = set(
        expanded.loc[
            expanded["_cell_merge"].eq("left_only"),
            "FORMULARY_ID",
        ].astype(str)
    )
    fallback_values = {
        "state": UNMAPPED_STATE,
        "parent_organization": UNMAPPED_PARENT_ORGANIZATION,
    }
    for column in spec.dimensions:
        expanded[column] = expanded[column].fillna(fallback_values[column])
    for column in spec.mapping_flags:
        expanded[column] = (
            pd.to_numeric(
                expanded[column],
                errors="coerce",
            )
            .fillna(0)
            .astype("int8")
        )

    expanded["_tierA_sum"] = expanded["tierA"]
    expanded["_tierA_count"] = expanded["tierA"].notna().astype("int32")
    expanded["_tier_raw_sum"] = expanded["tier_raw"].fillna(0)
    expanded["_tier_raw_count"] = expanded["tier_raw"].notna().astype("int32")

    aggregation: dict[str, tuple[str, str]] = {
        "atc3": ("ATC3", "first"),
        "included_count": ("included", "sum"),
        "n_formularies_observed": ("FORMULARY_ID", "size"),
        "_tierA_sum": ("_tierA_sum", "sum"),
        "_tierA_count": ("_tierA_count", "sum"),
        "_tier_raw_sum": ("_tier_raw_sum", "sum"),
        "_tier_raw_count": ("_tier_raw_count", "sum"),
    }
    aggregation.update({column: (column, "max") for column in spec.mapping_flags})
    aggregation.update(
        {
            RAW_TO_OUTPUT_COLUMNS[column]: (column, "max")
            for column in [*RAW_EVENT_COLUMNS, *RAW_SHARING_COLUMNS]
        }
    )
    partial = (
        expanded.groupby(
            [
                "NDC",
                "BoardName",
                *spec.dimensions,
            ],
            as_index=False,
            sort=False,
        )
        .agg(**aggregation)
        .rename(columns={"NDC": "ndc", "BoardName": "boardname"})
    )
    return partial, missing_formularies


def combine_chunk_aggregates(
    partials: list[pd.DataFrame],
    year_q: str,
    year: int,
    quarter: int,
    spec: AnalysisSpec,
) -> pd.DataFrame:
    """Combine additive chunk statistics into one new-cell quarter panel."""
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
    aggregation.update({column: (column, "max") for column in spec.mapping_flags})
    aggregation.update(
        {column: (column, "max") for column in RAW_TO_OUTPUT_COLUMNS.values()}
    )
    result = combined.groupby(
        spec.cell_id_columns,
        as_index=False,
        sort=False,
    ).agg(**aggregation)
    result["included_count"] = result["included_count"].astype("int32")
    result["n_formularies_observed"] = result["n_formularies_observed"].astype("int32")
    result["included_share"] = (
        result["included_count"] / result["n_formularies_observed"]
    )
    result["mean_tiera"] = result["_tierA_sum"] / result["_tierA_count"]
    result["mean_tier_raw"] = result["_tier_raw_sum"] / result[
        "_tier_raw_count"
    ].replace(0, np.nan)

    if result["n_formularies_observed"].le(0).any():
        raise AssertionError(f"{year_q} contains a cell with no formulary rows.")
    if result["included_count"].gt(result["n_formularies_observed"]).any():
        raise AssertionError(
            f"{year_q} has included_count above the formulary denominator."
        )
    if not result["included_share"].between(0, 1).all():
        raise AssertionError(f"{year_q} has included_share outside [0, 1].")
    if result["mean_tiera"].isna().any():
        raise AssertionError(f"{year_q} has missing mean_tiera after aggregation.")

    result["year_q"] = year_q
    result["year"] = np.int16(year)
    result["quarter"] = np.int8(quarter)
    flag_columns = [
        *spec.mapping_flags,
        *RAW_TO_OUTPUT_COLUMNS.values(),
    ]
    result[flag_columns] = result[flag_columns].astype("int8")
    result = result.drop(
        columns=[
            "_tierA_sum",
            "_tierA_count",
            "_tier_raw_sum",
            "_tier_raw_count",
        ]
    )
    ordered = [
        *spec.cell_id_columns,
        "year_q",
        "year",
        "quarter",
        "atc3",
        *spec.mapping_flags,
        "included_count",
        "n_formularies_observed",
        "included_share",
        "mean_tiera",
        "mean_tier_raw",
        *[
            base.output_event_column(event, side)
            for event in EVENT_TYPES
            for side in TREATMENT_GROUPS
        ],
        *[
            base.output_sharing_column(event, side)
            for event in EVENT_TYPES
            for side in TREATMENT_GROUPS
        ],
    ]
    return (
        result[ordered]
        .sort_values([*reversed(spec.dimensions), "boardname", "ndc"])
        .reset_index(drop=True)
    )


def add_unmapped_formularies_to_crosswalk(
    crosswalk_path: Path,
    missing_formularies: set[str],
    year_q: str,
    time_shift: int,
    spec: AnalysisSpec,
) -> None:
    """Persist fallback rows for panel formularies absent from plan data."""
    if not missing_formularies:
        return
    crosswalk = pd.read_csv(
        crosswalk_path,
        dtype="string",
        keep_default_na=False,
    )
    source_year_q = shift_year_q(year_q, -time_shift)
    fallback_data: dict[str, object] = {
        "year_q": year_q,
        "source_year_q": source_year_q,
        "formulary_id": sorted(missing_formularies),
        "source_contract_count": np.int32(0),
    }
    if spec.includes_state:
        fallback_data.update(
            {
                "state": UNMAPPED_STATE,
                "state_mapped": np.int8(0),
                "state_source": "unmapped_formulary",
            }
        )
    if spec.includes_insurer:
        fallback_data.update(
            {
                "parent_organization": UNMAPPED_PARENT_ORGANIZATION,
                "parent_org_matched": np.int8(0),
                "representative_month": representative_month(source_year_q),
            }
        )
    fallback = pd.DataFrame(fallback_data)
    combined = pd.concat([crosswalk, fallback], ignore_index=True)
    keys = ["year_q", "formulary_id", *spec.dimensions]
    combined = combined.drop_duplicates(keys).sort_values(
        ["formulary_id", *spec.dimensions]
    )
    combined[spec.crosswalk_columns].to_csv(crosswalk_path, index=False)


def aggregate_one_quarter(
    source_path: Path,
    crosswalk_path: Path,
    output_path: Path,
    chunksize: int,
    time_shift: int,
    spec: AnalysisSpec,
) -> None:
    """Stream, expand, aggregate, and save one selected-dimension quarter."""
    year_q, year, quarter = base.parse_quarter_filename(source_path)
    base.prepare_output_path(output_path, overwrite=True)
    crosswalk = read_quarter_crosswalk(crosswalk_path, spec)
    partials: list[pd.DataFrame] = []
    missing_formularies: set[str] = set()

    reader = pd.read_csv(
        source_path,
        usecols=RAW_REQUIRED_COLUMNS,
        dtype="string",
        chunksize=chunksize,
    )
    progress = tqdm(
        reader,
        desc=f"  Aggregating {year_q}",
        unit="chunk",
        leave=False,
    )
    expected_raw_year_q = f"{year} Q{quarter}"
    for chunk in progress:
        observed = set(
            base.normalize_string(
                chunk["YEAR_Q"],
                uppercase=True,
            )
            .dropna()
            .unique()
        )
        if observed != {expected_raw_year_q}:
            raise ValueError(
                f"{source_path.name} must contain only "
                f"{expected_raw_year_q}; found {sorted(observed)}"
            )
        partial, missing = aggregate_chunk(
            chunk,
            crosswalk,
            source_path.name,
            spec,
        )
        partials.append(partial)
        missing_formularies.update(missing)
        progress.set_postfix_str(
            f"partial groups={sum(len(item) for item in partials):,}"
        )
        del chunk, partial
        gc.collect()

    result = combine_chunk_aggregates(
        partials,
        year_q,
        year,
        quarter,
        spec,
    )
    unique_columns = [*spec.cell_id_columns, "year_q"]
    if result.duplicated(unique_columns).any():
        raise AssertionError(
            f"{year_q} aggregation is not unique by new cell and quarter."
        )
    result.to_csv(output_path, index=False)
    add_unmapped_formularies_to_crosswalk(
        crosswalk_path,
        missing_formularies,
        year_q,
        time_shift,
        spec,
    )
    del crosswalk, partials, result
    gc.collect()


def build_drug_quarter_panels(
    quarter_tags: list[str],
    sources: dict[str, Path],
    crosswalk_paths: dict[str, Path],
    destination_dir: Path,
    chunksize: int,
    time_shift: int,
    spec: AnalysisSpec,
) -> dict[str, Path]:
    """Build every required selected-dimension drug-quarter panel once."""
    outputs = {
        tag: (destination_dir / f"formulary_drug_{spec.slug}_panel_{tag}.csv")
        for tag in quarter_tags
    }
    progress = tqdm(
        quarter_tags,
        desc=f"Building {spec.label} drug-quarter panels",
        unit="quarter",
    )
    for tag in progress:
        progress.set_postfix_str(tag)
        aggregate_one_quarter(
            sources[tag],
            crosswalk_paths[tag],
            outputs[tag],
            chunksize,
            time_shift,
            spec,
        )
    return outputs


# ========================== COHORT CONSTRUCTION ==========================


def read_cohort_window(
    paths: dict[str, Path],
    quarter_tags: list[str],
    spec: AnalysisSpec,
) -> pd.DataFrame:
    """Read and stack new-cell drug-quarter files for one cohort."""
    frames: list[pd.DataFrame] = []
    progress = tqdm(
        quarter_tags,
        desc=f"  Loading {spec.label} cohort quarters",
        unit="quarter",
        leave=False,
    )
    for tag in progress:
        progress.set_postfix_str(tag)
        frames.append(
            pd.read_csv(
                paths[tag],
                dtype={column: "string" for column in spec.cell_id_columns},
            )
        )
    cohort = pd.concat(frames, ignore_index=True)
    cohort["ndc"] = base.normalize_string(cohort["ndc"])
    cohort["boardname"] = base.normalize_string(
        cohort["boardname"],
        uppercase=True,
    )
    if spec.includes_state:
        cohort["state"] = base.normalize_string(
            cohort["state"],
            uppercase=True,
        )
    if spec.includes_insurer:
        cohort["parent_organization"] = base.normalize_string(
            cohort["parent_organization"]
        )
    unique_columns = [*spec.cell_id_columns, "year_q"]
    duplicate = cohort.duplicated(unique_columns, keep=False)
    if duplicate.any():
        examples = cohort.loc[duplicate, unique_columns].head(20)
        raise ValueError(
            f"{spec.label.title()} drug-quarter files are not unique by "
            "id and time. "
            f"Examples:\n{examples}"
        )
    return cohort


def keep_complete_cell_ids(
    cohort: pd.DataFrame,
    expected_quarters: int,
    spec: AnalysisSpec,
) -> pd.DataFrame:
    """Keep new-cell ids observed in every available cohort quarter."""
    counts = cohort.groupby(spec.cell_id_columns)["year_q"].nunique()
    complete_ids = counts[counts.eq(expected_quarters)].index
    id_index = pd.MultiIndex.from_frame(cohort[spec.cell_id_columns])
    return cohort.loc[id_index.isin(complete_ids)].copy()


def add_direction_flags(
    cohort: pd.DataFrame,
    movement: pd.DataFrame,
    candidate: pd.DataFrame,
    stay_column: str,
    event_type: str,
    cohort_year: int,
    spec: AnalysisSpec,
) -> pd.DataFrame:
    """Add direction-specific treated, sample, and Q1 sharing indicators."""
    result = cohort.copy()
    window_years = set(range(cohort_year - 1, cohort_year + 2))
    universe = set(result["boardname"].dropna().astype(str))

    for side in TREATMENT_GROUPS:
        side_lower = side.lower()
        treated = base.treated_firms(
            movement,
            event_type,
            side,
            cohort_year,
        )
        base.validate_panel_treatment_flags(
            result,
            event_type,
            side,
            cohort_year,
            treated,
        )
        pure_event = base.pure_event_firms_in_window(
            movement,
            event_type,
            side,
            window_years,
        )
        excluded_counterparts = base.counterpart_only_firms(
            candidate,
            stay_column,
            event_type,
            side,
            cohort_year,
        )
        controls = universe - pure_event - excluded_counterparts

        treated_column = f"treated_{side_lower}"
        sample_column = f"sample_{side_lower}"
        share_column = base.cohort_sharing_column(side)
        source_share_column = base.output_sharing_column(event_type, side)
        result[treated_column] = result["boardname"].isin(treated).astype("int8")
        result[sample_column] = (
            result[treated_column].eq(1) | result["boardname"].isin(controls)
        ).astype("int8")

        q1_share = result.loc[
            result["year"].eq(cohort_year) & result["quarter"].eq(1),
            [*DRUG_ID_COLUMNS, source_share_column],
        ]
        share_value_counts = q1_share.groupby(
            DRUG_ID_COLUMNS,
            sort=False,
        )[
            source_share_column
        ].nunique(dropna=False)
        inconsistent = share_value_counts[share_value_counts.gt(1)]
        if not inconsistent.empty:
            raise ValueError(
                f"Cohort-entry sharing varies across {spec.label} cells for "
                f"{event_type}, {side}, {cohort_year}. "
                f"Drug-firm examples: {inconsistent.index.tolist()[:10]}"
            )
        q1_share = q1_share.drop_duplicates(DRUG_ID_COLUMNS).rename(
            columns={source_share_column: share_column}
        )
        result = result.merge(
            q1_share,
            on=DRUG_ID_COLUMNS,
            how="left",
            validate="many_to_one",
        )
        if result[share_column].isna().any():
            examples = (
                result.loc[
                    result[share_column].isna(),
                    DRUG_ID_COLUMNS,
                ]
                .drop_duplicates()
                .head(10)
            )
            raise ValueError(
                "Cohort rows are missing the drug-firm Q1 sharing value. "
                f"Examples:\n{examples}"
            )
        result[share_column] = result[share_column].astype("int8")
        result.loc[
            result[treated_column].eq(0),
            share_column,
        ] = np.int8(0)

    return result


def cohort_output_columns(spec: AnalysisSpec) -> list[str]:
    """Return the intentionally lean selected-dimension cohort schema."""
    return [
        *spec.cell_id_columns,
        "year_q",
        "year",
        "quarter",
        "data_cohort",
        "atc3",
        *spec.mapping_flags,
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
        *[
            base.output_event_column(event, side)
            for event in EVENT_TYPES
            for side in TREATMENT_GROUPS
        ],
    ]


def build_one_cohort(
    drug_quarter_paths: dict[str, Path],
    quarter_tags: list[str],
    first_seen_qtime: dict[str, int],
    first_seen_year_offset: int,
    first_seen_quarter: int,
    movement: pd.DataFrame,
    candidate: pd.DataFrame,
    stay_column: str,
    event_type: str,
    cohort_year: int,
    spec: AnalysisSpec,
) -> pd.DataFrame:
    """Build one combined-direction, balanced selected-dimension cohort."""
    cohort = read_cohort_window(drug_quarter_paths, quarter_tags, spec)
    cohort = keep_complete_cell_ids(
        cohort,
        expected_quarters=len(quarter_tags),
        spec=spec,
    )
    cohort = base.keep_available_ndcs(
        cohort,
        first_seen_qtime,
        cohort_year,
        first_seen_year_offset,
        first_seen_quarter,
    )
    cohort["data_cohort"] = np.int16(cohort_year)
    cohort = add_direction_flags(
        cohort,
        movement,
        candidate,
        stay_column,
        event_type,
        cohort_year,
        spec,
    )
    cohort = cohort.loc[cohort["sample_a"].eq(1) | cohort["sample_b"].eq(1)].copy()
    return (
        cohort[cohort_output_columns(spec)]
        .sort_values(
            [
                *reversed(spec.dimensions),
                "boardname",
                "ndc",
                "year",
                "quarter",
            ]
        )
        .reset_index(drop=True)
    )


def build_cohort_outputs(
    drug_quarter_paths: dict[str, Path],
    cohort_windows: dict[int, list[str]],
    first_seen_qtime: dict[str, int],
    first_seen_year_offset: int,
    first_seen_quarter: int,
    movement: pd.DataFrame,
    candidate: pd.DataFrame,
    stay_column: str,
    destination_dir: Path,
    spec: AnalysisSpec,
) -> None:
    """Write every configured event-year selected-dimension cohort."""
    specifications = [
        (event_type, cohort_year)
        for event_type in EVENT_TYPES
        for cohort_year in COHORT_YEARS[event_type]
    ]
    progress = tqdm(
        specifications,
        desc=f"Building {spec.label} formulary cohorts",
        unit="cohort",
    )
    for event_type, cohort_year in progress:
        progress.set_postfix_str(f"{event_type}/{cohort_year}")
        output_path = destination_dir / f"{event_type}_quarter_cohort_{cohort_year}.csv"
        base.prepare_output_path(output_path, overwrite=True)
        cohort = build_one_cohort(
            drug_quarter_paths,
            cohort_windows[cohort_year],
            first_seen_qtime,
            first_seen_year_offset,
            first_seen_quarter,
            movement,
            candidate,
            stay_column,
            event_type,
            cohort_year,
            spec,
        )
        cohort.to_csv(output_path, index=False)
        del cohort
        gc.collect()


# ========================== OUTPUT DISPATCH ==========================


def main() -> None:
    """Build crosswalks, new-cell quarter panels, and balanced cohorts."""
    spec = analysis_spec(RUN_CONFIG)
    (
        chunksize,
        window_pre,
        window_post,
        time_shift,
        first_seen_year_offset,
        first_seen_quarter,
    ) = base.validate_config(RUN_CONFIG)
    source_dir = base.quarter_input_dir(time_shift)
    crosswalk_output_dir = shifted_output_dir(
        spec.crosswalk_output_dir,
        time_shift,
    )
    drug_output_dir = shifted_output_dir(
        spec.drug_output_dir,
        time_shift,
    )
    cohort_destination_dir = cohort_output_dir(
        spec,
        time_shift,
        first_seen_year_offset,
        first_seen_quarter,
    )

    sources = base.available_quarter_paths(source_dir)
    quarter_tags, cohort_windows = base.required_quarters(
        sources,
        window_pre,
        window_post,
    )
    crosswalk = build_plan_crosswalk(
        quarter_tags,
        time_shift,
        chunksize,
        spec,
    )
    crosswalk_paths = save_quarter_crosswalks(
        crosswalk,
        quarter_tags,
        crosswalk_output_dir,
        spec,
    )
    del crosswalk
    gc.collect()

    first_seen_qtime = base.load_first_seen_lookup(base.first_seen_path(time_shift))
    movement, candidate, stay_column = base.load_event_sources()
    drug_quarter_paths = build_drug_quarter_panels(
        quarter_tags,
        sources,
        crosswalk_paths,
        drug_output_dir,
        chunksize,
        time_shift,
        spec,
    )
    build_cohort_outputs(
        drug_quarter_paths,
        cohort_windows,
        first_seen_qtime,
        first_seen_year_offset,
        first_seen_quarter,
        movement,
        candidate,
        stay_column,
        cohort_destination_dir,
        spec,
    )
    print(f"Saved {spec.label} crosswalks to: {crosswalk_output_dir}")
    print(f"Saved {spec.label} drug-quarter panels to: {drug_output_dir}")
    print(
        f"Saved combined-direction {spec.label} cohorts to: "
        f"{cohort_destination_dir}"
    )


if __name__ == "__main__":
    main()
