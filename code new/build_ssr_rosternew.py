"""
Purpose:
    Build annual and quarterly leadership rosters for SSR companies from
    BoardEx data.
    The script preserves employment spells, combines complementary IE and OC
    fields, matches fixed initial names or current SSR CompanyIDs without
    learning historical IDs or aliases, and keeps one
    highest-priority leadership record per person-company-year.

Input:
    boardex_pharma.dta, cro_bname_boardex_citeline.dta,
    cro_bname_boardex_within.dta, organization_composition.csv,
    and individual_employment.csv.

Output:
    <output-root>/ssr_company_roster_imputed_year.csv/.dta
    <output-root>/ssr_company_roster_imputed_yearquarter.csv/.dta
    <output-root>/ssr_company_roster_drop_sentinel_year.csv/.dta
    <output-root>/ssr_company_roster_drop_sentinel_yearquarter.csv/.dta
    <output-root>/ssr_company_current_companyid_audit.csv
    <output-root>/ssr_company_initial_name_audit.csv
    <output-root>/ssr_company_ambiguous_name_audit.csv
"""

import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import pandas as pd


INTERIM_DIR = Path(r"D:\Dropbox\BoardPharma\InterimData")
RAW_NA_DIR = Path(r"D:\Dropbox\BoardPharma\RawData\boardex\boardex_na")
# Keep generated roster files separate from raw and regression outputs.
OUTPUT_DIR = Path(
    os.environ.get("BOARDPHARMA_ROSTER_OUTPUT_ROOT", r"F:\BoardPharma\data")
)
INTERMEDIATE_DIR = OUTPUT_DIR / "ssr_ie_oc_intermediate"

PHARMA_DTA = INTERIM_DIR / "boardex_pharma.dta"
CROSSWALK_DTA = INTERIM_DIR / "cro_bname_boardex_citeline.dta"
WITHIN_DTA = INTERIM_DIR / "cro_bname_boardex_within.dta"
ORG_COMP = RAW_NA_DIR / "organization_composition.csv"
INDIV_EMPLOY = RAW_NA_DIR / "individual_employment.csv"

SAMPLE_START = 1990
SAMPLE_END = 2025
MAX_VALID_DATE_YEAR = 2026
IE_CHUNK_SIZE = 200_000
OC_CHUNK_SIZE = 300_000
DATE_MODE_IMPUTE = "impute"
DATE_MODE_DROP = "drop_sentinel"
DELIST_DATES: Dict[str, Tuple[int, int]] = {}

DELIST_PATTERN = re.compile(
    r"\bDE[- ]?LISTED\s+(?P<month>0?[1-9]|1[0-2])/(?P<year>\d{4})\b",
    flags=re.IGNORECASE,
)

GENERIC_ALIASES = {
    "THE",
    "INC",
    "LTD",
    "LLC",
    "PLC",
    "CORP",
    "AG",
    "SA",
    "NV",
    "LISTED",
    "DE-LISTED",
    "DE LISTED",
    "REDOMICILED",
}

TIER_PRIORITY = {
    "excluded": 0,
    "vp_tech_hr": 1,
    "csuite": 2,
    "board": 3,
}


def normalize_scalar(value) -> str:
    """Normalize one text value for matching while preserving missing as empty."""
    if pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value).strip().upper())


def normalize_series(series: pd.Series) -> pd.Series:
    """Normalize a pandas text series for matching."""
    return (
        series.fillna("")
        .astype(str)
        .str.strip()
        .str.upper()
        .str.replace(r"\s+", " ", regex=True)
    )


def clean_display_series(series: pd.Series) -> pd.Series:
    """Clean a pandas text series without upper-casing display values."""
    return series.fillna("").astype(str).str.strip()


def parse_year_series(series: pd.Series) -> pd.Series:
    """Keep only the leading four-digit year from a raw date field."""
    raw = series.fillna("").astype(str).str.strip()
    year = pd.to_numeric(raw.str[:4], errors="coerce")
    return year.where(year.between(1800, 2200)).astype("Int64")


def parse_period_series(series: pd.Series) -> pd.Series:
    """Convert a date field to a quarter index equal to year * 4 + quarter - 1."""
    raw = series.fillna("").astype(str).str.strip()
    year = pd.to_numeric(raw.str[:4], errors="coerce")
    month = pd.to_numeric(raw.str[5:7], errors="coerce")
    month = month.where(month.between(1, 12), 1)
    valid_year = year.between(1800, 2200)
    period = year * 4 + ((month - 1) // 3)
    return period.where(valid_year).astype("Int64")


def extract_delist_period(value: object) -> Tuple[Optional[int], Optional[int]]:
    """Extract an explicit De-listed month/year as year and quarter index."""
    if pd.isna(value):
        return None, None
    match = DELIST_PATTERN.search(str(value))
    if match is None:
        return None, None
    year = int(match.group("year"))
    month = int(match.group("month"))
    return year, year * 4 + (month - 1) // 3


def period_to_year(period: int) -> int:
    """Convert a quarter index back to its calendar year."""
    return int(period) // 4


def period_to_quarter(period: int) -> int:
    """Convert a quarter index back to quarter number one through four."""
    return int(period) % 4 + 1


def add_candidate(
    candidates: Dict[str, Dict[str, Set[str]]],
    source: str,
    alias: str,
    canonical: str,
) -> None:
    """Add a non-generic alias candidate to the mapping audit structure."""
    alias_key = normalize_scalar(alias)
    canonical_key = normalize_scalar(canonical)
    if len(alias_key) <= 3 or not canonical_key or alias_key in GENERIC_ALIASES:
        return
    candidates[source].setdefault(alias_key, set()).add(canonical_key)


def extract_parenthetical_aliases(
    canonical: str,
    canonical_names: Set[str],
    candidates: Dict[str, Dict[str, Set[str]]],
) -> None:
    """Extract only explicit historical names and ignore status descriptors."""
    for match in re.finditer(r"\(([^()]*)\)", canonical):
        text = match.group(1).strip().upper()
        if re.match(r"^(19|20)\d{2}", text):
            continue
        history_match = re.search(
            r"^(.*?)\s+(?:PRIOR TO|FORMERLY|F/K/A|FKA|FORMERLY KNOWN AS|"
            r"PREVIOUSLY|ACQUIRED|MERGED)\s+\d{2}/\d{4}\b",
            text,
        )
        if history_match:
            alias = history_match.group(1).strip()
            if alias not in canonical_names:
                add_candidate(candidates, "parenthetical", alias, canonical)




def build_name_mapping(
    canonical_names: Set[str],
    within: pd.DataFrame,
    crosswalk: pd.DataFrame,
) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, List[str]]]:
    """Build a deterministic mapping and an explicit ambiguity report."""
    candidates: Dict[str, Dict[str, Set[str]]] = defaultdict(dict)

    for canonical in canonical_names:
        add_candidate(candidates, "canonical", canonical, canonical)
        core = normalize_scalar(canonical).split("(", 1)[0].strip()
        if core and core != normalize_scalar(canonical):
            add_candidate(candidates, "core", core, canonical)
        extract_parenthetical_aliases(canonical, canonical_names, candidates)

    for row in within.itertuples(index=False):
        original = normalize_scalar(row.BoardName)
        new_name = normalize_scalar(row.BoardNameNew)
        target_candidates = set()
        if new_name in canonical_names:
            target_candidates.add(new_name)
        for source in ("canonical", "parenthetical", "within"):
            target_candidates.update(candidates[source].get(new_name, set()))
        for target in target_candidates:
            add_candidate(candidates, "within", original, target)

    for row in crosswalk.itertuples(index=False):
        company = normalize_scalar(row.Company)
        board_name = normalize_scalar(row.BoardName)
        if board_name in canonical_names:
            add_candidate(candidates, "crosswalk", company, board_name)

    all_candidates: Dict[str, Set[str]] = defaultdict(set)
    for source_map in candidates.values():
        for alias, targets in source_map.items():
            all_candidates[alias].update(targets)

    ambiguous = {
        alias: sorted(targets)
        for alias, targets in all_candidates.items()
        if len(targets) > 1
    }

    source_priority = ["canonical", "crosswalk", "within", "parenthetical", "core"]
    name_map: Dict[str, str] = {}
    source_map: Dict[str, str] = {}
    for alias, targets in all_candidates.items():
        if len(targets) != 1:
            continue
        target = next(iter(targets))
        name_map[alias] = target
        source_map[alias] = next(
            source for source in source_priority if alias in candidates[source]
        )

    return name_map, source_map, ambiguous


def build_company_info(bp: pd.DataFrame) -> Tuple[Set[str], Dict[str, str], Dict[int, str], Dict[str, Tuple[str, int]]]:
    """Build the SSR universe, canonical display names, and CompanyID maps."""
    bp = bp.copy()
    bp["BoardName"] = clean_display_series(bp["BoardName"])
    bp["company_key"] = normalize_series(bp["BoardName"])
    ssr = bp[bp["inSSR"].eq(1)].copy()
    canonical_names = set(ssr["company_key"])

    company_info: Dict[str, Tuple[str, int]] = {}
    for row in ssr[["company_key", "BoardName", "HOCountryName", "CompanyID"]].itertuples(index=False):
        company_id = int(row.CompanyID) if pd.notna(row.CompanyID) else -1
        candidate = (str(row.HOCountryName), company_id)
        if row.company_key not in company_info:
            company_info[row.company_key] = candidate
        elif company_info[row.company_key] != candidate:
            raise ValueError(f"Conflicting SSR company information for {row.company_key}")

    display_names = {
        key: str(group.iloc[0]["BoardName"])
        for key, group in ssr.groupby("company_key", sort=False)
    }
    id_to_key: Dict[int, str] = {}
    for key, (_, company_id) in company_info.items():
        if company_id >= 0:
            if company_id in id_to_key and id_to_key[company_id] != key:
                raise ValueError(f"CompanyID maps to multiple SSR companies: {company_id}")
            id_to_key[company_id] = key

    return canonical_names, display_names, id_to_key, company_info


def resolve_company_chunk(
    chunk: pd.DataFrame,
    name_map: Mapping[str, str],
    mapping_source: Mapping[str, str],
    id_to_key: Mapping[int, str],
) -> pd.DataFrame:
    """Match fixed initial names and current IDs; reject conflicting targets.

    Full names take precedence over core-name fallback. Current IDs take
    precedence over name matches only when the two targets do not conflict.
    A name match never adds its raw ID or other source names to either map.
    """
    raw_company_id = pd.to_numeric(chunk["companyid"], errors="coerce").round().astype("Int64")
    id_candidate = raw_company_id.map(id_to_key)
    raw_name = normalize_series(chunk["companyname"])
    core_name = raw_name.str.split("(", n=1).str[0].str.strip()
    full_candidate = raw_name.map(name_map)
    name_candidate = full_candidate.where(full_candidate.notna(), core_name.map(name_map))
    name_source = raw_name.map(mapping_source).where(
        full_candidate.notna(), core_name.map(mapping_source)
    )
    conflict = id_candidate.notna() & name_candidate.notna() & id_candidate.ne(name_candidate)
    resolved = id_candidate.where(id_candidate.notna(), name_candidate).where(~conflict)

    source = pd.Series("unmatched", index=chunk.index, dtype="object")
    by_name = resolved.notna() & id_candidate.isna()
    source.loc[by_name] = "name:" + name_source.loc[by_name].fillna("initial")
    source.loc[resolved.notna() & id_candidate.notna()] = "current_companyid"
    source.loc[conflict] = "id_name_conflict"

    confidence = pd.Series("unmatched", index=chunk.index, dtype="object")
    confidence.loc[by_name] = "medium"
    confidence.loc[resolved.notna() & id_candidate.notna()] = "high"

    return pd.DataFrame(
        {
            "raw_companyname": chunk["companyname"].fillna("").astype(str).str.strip(),
            "RawCompanyID": raw_company_id,
            "company_key": resolved,
            "mapping_source": source,
            "mapping_confidence": confidence,
            "mapping_conflict": conflict.astype(int),
        },
        index=chunk.index,
    )




def collect_earliest_years(
    paths_and_columns: Sequence[Tuple[Path, int]],
    name_map: Mapping[str, str],
    mapping_source: Mapping[str, str],
    id_to_key: Mapping[int, str],
) -> Tuple[Dict[str, int], Dict[str, int]]:
    """Find the earliest valid observed start year and quarter by company."""
    earliest: Dict[str, int] = {}
    earliest_period: Dict[str, int] = {}
    for path, chunk_size in paths_and_columns:
        for chunk in pd.read_csv(
            path,
            usecols=["companyid", "companyname", "datestartrole"],
            chunksize=chunk_size,
            low_memory=False,
        ):
            resolved = resolve_company_chunk(chunk, name_map, mapping_source, id_to_key)
            start_year = parse_year_series(chunk["datestartrole"])
            start_period = parse_period_series(chunk["datestartrole"])
            valid = (
                resolved["company_key"].notna()
                & resolved["mapping_conflict"].eq(0)
                & start_year.notna()
                & start_year.ne(1900)
                & start_year.between(SAMPLE_START, SAMPLE_END)
                & start_period.notna()
            )
            if not valid.any():
                continue
            current = pd.DataFrame(
                {
                    "company_key": resolved.loc[valid, "company_key"],
                    "start_year": start_year.loc[valid].astype(int),
                    "start_period": start_period.loc[valid].astype(int),
                }
            ).groupby("company_key").agg(
                start_year=("start_year", "min"),
                start_period=("start_period", "min"),
            )
            for company_key, row in current.iterrows():
                year = int(row["start_year"])
                period = int(row["start_period"])
                if company_key not in earliest or (year, period) < (
                    earliest[company_key], earliest_period[company_key]
                ):
                    earliest[company_key] = year
                    earliest_period[company_key] = period
    return earliest, earliest_period


def collect_latest_years(
    paths_and_columns: Sequence[Tuple[Path, int]],
    name_map: Mapping[str, str],
    mapping_source: Mapping[str, str],
    id_to_key: Mapping[int, str],
) -> Tuple[Dict[str, int], Dict[str, int]]:
    """Find the latest valid observed end year and quarter by company."""
    latest: Dict[str, int] = {}
    latest_period: Dict[str, int] = {}
    for path, chunk_size in paths_and_columns:
        for chunk in pd.read_csv(
            path,
            usecols=["companyid", "companyname", "dateendrole"],
            chunksize=chunk_size,
            low_memory=False,
        ):
            resolved = resolve_company_chunk(chunk, name_map, mapping_source, id_to_key)
            end_year = parse_year_series(chunk["dateendrole"])
            end_period = parse_period_series(chunk["dateendrole"])
            valid = (
                resolved["company_key"].notna()
                & resolved["mapping_conflict"].eq(0)
                & end_year.notna()
                & end_year.between(SAMPLE_START, MAX_VALID_DATE_YEAR)
                & end_period.notna()
            )
            if not valid.any():
                continue
            current = pd.DataFrame(
                {
                    "company_key": resolved.loc[valid, "company_key"],
                    "end_year": end_year.loc[valid].astype(int),
                    "end_period": end_period.loc[valid].astype(int),
                }
            ).groupby("company_key").agg(
                end_year=("end_year", "max"),
                end_period=("end_period", "max"),
            )
            for company_key, row in current.iterrows():
                year = int(row["end_year"])
                period = int(row["end_period"])
                if company_key not in latest or (year, period) > (
                    latest[company_key], latest_period[company_key]
                ):
                    latest[company_key] = year
                    latest_period[company_key] = period
    return latest, latest_period


def collect_delist_dates(paths_and_columns, name_map, mapping_source, id_to_key):
    """Collect company delisting year and quarter from parenthesized source names."""
    pattern = re.compile(
        r"\(\s*(?:de[\s-]?listed|delist(?:ed)?)\s*[:=,-]?\s*"
        r"(?:(?P<month_first>0?[1-9]|1[0-2])[/.-](?P<year_first>(?:19|20)\d{2})"
        r"|(?P<year_last>(?:19|20)\d{2})(?:[-/] (?P<month_last>0?[1-9]|1[0-2]))?)",
        re.I | re.X,
    )
    delist_dates: Dict[str, Tuple[int, int]] = {}
    for path, chunk_size in paths_and_columns:
        for chunk in pd.read_csv(path, usecols=["companyid", "companyname"], chunksize=chunk_size, low_memory=False):
            resolved = resolve_company_chunk(chunk, name_map, mapping_source, id_to_key)
            for idx, name in chunk["companyname"].items():
                key = resolved.at[idx, "company_key"]
                if pd.isna(key):
                    continue
                match = pattern.search(str(name))
                if not match:
                    continue
                year = int(match.group("year_first") or match.group("year_last"))
                month_text = match.group("month_first") or match.group("month_last")
                month = int(month_text or 12)
                if 1 <= month <= 12:
                    quarter = (month - 1) // 3 + 1
                    candidate = (year, quarter)
                    if key not in delist_dates or candidate > delist_dates[key]:
                        delist_dates[key] = candidate
    return delist_dates


def materialize_ssr_ie_oc(paths_and_columns, name_map, mapping_source, id_to_key):
    """Filter IE and OC source files to validated SSR companies once for reuse."""
    INTERMEDIATE_DIR.mkdir(parents=True, exist_ok=True)
    filtered_paths = []
    for source_path, chunk_size in paths_and_columns:
        output_path = INTERMEDIATE_DIR / f"{source_path.stem}_ssr.csv"
        if output_path.exists():
            output_path.unlink()
        wrote_header = False
        rows = 0
        for chunk in pd.read_csv(source_path, chunksize=chunk_size, low_memory=False):
            resolved = resolve_company_chunk(chunk, name_map, mapping_source, id_to_key)
            keep = resolved["company_key"].notna() & resolved["mapping_conflict"].eq(0)
            if not keep.any():
                continue
            selected = chunk.loc[keep].copy()
            selected["company_key"] = resolved.loc[keep, "company_key"]
            selected["RawCompanyID"] = resolved.loc[keep, "RawCompanyID"]
            selected["mapping_source"] = resolved.loc[keep, "mapping_source"]
            selected["mapping_confidence"] = resolved.loc[keep, "mapping_confidence"]
            selected["mapping_conflict"] = resolved.loc[keep, "mapping_conflict"]
            selected.to_csv(output_path, mode="w" if not wrote_header else "a", index=False, header=not wrote_header, encoding="utf-8-sig" if not wrote_header else "utf-8")
            rows += len(selected)
            wrote_header = True
        if not wrote_header:
            raise ValueError(f"No SSR-linked records found in {source_path}")
        print(f"  SSR-filtered intermediate: {output_path} ({rows:,} rows)")
        filtered_paths.append((output_path, chunk_size))
    return filtered_paths


def add_resolved_dates(
    frame: pd.DataFrame,
    earliest: Mapping[str, int],
    latest: Mapping[str, int],
    earliest_period: Mapping[str, int],
    latest_period: Mapping[str, int],
    date_mode: str,
) -> pd.DataFrame:
    """Apply either company-level date imputation or sentinel-row deletion."""
    output = frame.copy()
    raw_start = parse_year_series(output["datestartrole"]).fillna(1900).astype(int)
    raw_end = parse_year_series(output["dateendrole"]).fillna(9999).astype(int)
    raw_start_period = parse_period_series(output["datestartrole"]).fillna(
        SAMPLE_START * 4
    ).astype(int)
    raw_end_period = parse_period_series(output["dateendrole"]).fillna(
        (MAX_VALID_DATE_YEAR + 1) * 4
    ).astype(int)

    start_sentinel = raw_start.eq(1900)
    end_sentinel = raw_end.gt(MAX_VALID_DATE_YEAR)
    if date_mode == DATE_MODE_DROP:
        keep = ~start_sentinel & ~end_sentinel
        output = output.loc[keep].copy()
        raw_start = raw_start.loc[keep]
        raw_end = raw_end.loc[keep]
        output["raw_start_year"] = raw_start
        output["raw_end_year"] = raw_end
        output["raw_start_period"] = raw_start_period
        output["raw_end_period"] = raw_end_period
        output["start_year"] = raw_start.astype(int)
        output["end_year"] = raw_end.astype(int)
        output["start_period"] = raw_start_period.astype(int)
        output["end_period"] = raw_end_period.astype(int)
        output["start_imputed"] = 0
        output["end_censored"] = 0
        return output
    if date_mode != DATE_MODE_IMPUTE:
        raise ValueError(f"Unknown date mode: {date_mode}")

    company_earliest = output["company_key"].map(earliest).fillna(1900).astype(int)
    company_earliest_period = output["company_key"].map(earliest_period).fillna(
        SAMPLE_START * 4
    ).astype(int)
    start_year = raw_start.where(~start_sentinel, company_earliest)
    start_period = raw_start_period.where(~start_sentinel, company_earliest_period)

    end_open = end_sentinel
    company_latest = output["company_key"].map(latest).fillna(SAMPLE_END).astype(int)
    company_latest_period = output["company_key"].map(latest_period).fillna(
        SAMPLE_END * 4 + 3
    ).astype(int)
    # Open-ended end dates use the company's latest valid observed end date.
    # Delisting text is intentionally not used for this imputation rule.
    resolved_end_year = company_latest
    resolved_end_period = company_latest_period
    end_year = raw_end.where(~end_open, resolved_end_year)
    end_period = raw_end_period.where(~end_open, resolved_end_period)

    output["raw_start_year"] = raw_start
    output["raw_end_year"] = raw_end
    output["raw_start_period"] = raw_start_period
    output["raw_end_period"] = raw_end_period
    output["start_year"] = start_year.astype(int)
    output["end_year"] = end_year.astype(int)
    output["start_period"] = start_period.astype(int)
    output["end_period"] = end_period.astype(int)
    output["start_imputed"] = start_sentinel.astype(int)
    output["end_censored"] = end_open.astype(int)
    return output


def load_source_spells_fixed(
    path: Path,
    source: str,
    chunk_size: int,
    usecols: Sequence[str],
    name_map: Mapping[str, str],
    mapping_source: Mapping[str, str],
    id_to_key: Mapping[int, str],
    earliest: Mapping[str, int],
    latest: Mapping[str, int],
    earliest_period: Mapping[str, int],
    latest_period: Mapping[str, int],
    date_mode: str,
) -> pd.DataFrame:
    """Load source rows with the correct IE and OC-specific fields."""
    matched_chunks: List[pd.DataFrame] = []
    for chunk in pd.read_csv(path, usecols=usecols, chunksize=chunk_size, low_memory=False):
        resolved = resolve_company_chunk(chunk, name_map, mapping_source, id_to_key)
        director_id = pd.to_numeric(chunk["directorid"], errors="coerce")
        keep = (
            director_id.notna()
            & resolved["company_key"].notna()
            & resolved["mapping_conflict"].eq(0)
        )
        if not keep.any():
            continue

        selected = chunk.loc[keep].copy()
        selected_resolved = resolved.loc[keep].copy()
        selected["DirectorID"] = director_id.loc[keep].astype(int)
        selected["DirectorName"] = clean_display_series(selected["directorname"])
        selected["rolename"] = clean_display_series(selected["rolename"])
        selected["role_key"] = normalize_series(selected["rolename"])
        selected["company_key"] = selected_resolved["company_key"]
        selected["RawCompanyID"] = selected_resolved["RawCompanyID"]
        selected["mapping_source"] = selected_resolved["mapping_source"]
        selected["mapping_confidence"] = selected_resolved["mapping_confidence"]
        selected["mapping_conflict"] = selected_resolved["mapping_conflict"]
        if source == "individual_employment":
            selected["brdposition_raw"] = clean_display_series(selected["brdposition"])
        else:
            selected["seniority_raw"] = clean_display_series(selected["seniority"])
        selected["brdposition"] = ""
        selected["seniority"] = ""
        selected["source_ie"] = int(source == "individual_employment")
        selected["source_oc"] = int(source == "organization_composition")
        if source == "individual_employment":
            selected["brdposition"] = clean_display_series(selected["brdposition_raw"])
        else:
            selected["seniority"] = clean_display_series(selected["seniority_raw"])

        selected = add_resolved_dates(
            selected, earliest, latest, earliest_period, latest_period, date_mode
        )
        if selected.empty:
            continue
        matched_chunks.append(
            selected[
                [
                    "DirectorID", "DirectorName", "company_key", "RawCompanyID",
                    "rolename", "role_key", "brdposition", "seniority",
                    "datestartrole", "dateendrole", "start_year", "end_year",
                    "start_period", "end_period", "raw_start_year", "raw_end_year",
                    "raw_start_period", "raw_end_period", "start_imputed", "end_censored",
                    "mapping_source", "mapping_confidence", "mapping_conflict",
                    "source_ie", "source_oc",
                ]
            ]
        )

    if not matched_chunks:
        return pd.DataFrame()
    return pd.concat(matched_chunks, ignore_index=True)


def choose_board_position(values: Iterable[str]) -> str:
    """Choose the strongest available IE board-position value."""
    # Missing OC board fields must remain empty, rather than becoming "<NA>".
    cleaned = [str(value).strip() for value in values if pd.notna(value) and str(value).strip()]
    if not cleaned:
        return ""
    upper_values = {value.upper(): value for value in cleaned}
    for preferred in ("YES", "INSIDE", "OUTSIDE", "NO"):
        if preferred in upper_values:
            return upper_values[preferred]
    return cleaned[0]


def join_source_values(values: Iterable[str]) -> str:
    """Join source labels without duplicate tokens or missing-value text."""
    tokens = set()
    for value in values:
        for token in str(value).split("+"):
            token = token.strip()
            if token and token.lower() not in {"nan", "none"}:
                tokens.add(token)
    return "+".join(sorted(tokens))


def join_mapping_values(values: Iterable[str]) -> str:
    """Join mapping labels while avoiding redundant CompanyID labels."""
    labels = set()
    for value in values:
        label = str(value).strip()
        if label and label.lower() not in {"nan", "none"}:
            labels.add(label)
    if "companyid+name" in labels:
        labels.discard("companyid")
    return "+".join(sorted(labels))


def combine_spell_rows(group: pd.DataFrame, frequency: str = "year") -> List[dict]:
    """Merge overlapping IE and OC rows without bridging employment gaps."""
    if frequency not in {"year", "quarter"}:
        raise ValueError(f"Unknown frequency: {frequency}")
    start_column = "start_year" if frequency == "year" else "start_period"
    end_column = "end_year" if frequency == "year" else "end_period"
    group = group.sort_values(
        [start_column, end_column, "source_ie"], kind="stable"
    )
    clusters: List[List[pd.Series]] = []
    current: List[pd.Series] = []
    current_end: Optional[int] = None

    for row in group.itertuples(index=False):
        row_start = int(getattr(row, start_column))
        row_end = int(getattr(row, end_column))
        if current and current_end is not None and row_start > current_end + 1:
            clusters.append(current)
            current = []
            current_end = None
        current.append(pd.Series(row._asdict()))
        current_end = row_end if current_end is None else max(current_end, row_end)
    if current:
        clusters.append(current)

    output: List[dict] = []
    for cluster in clusters:
        frame = pd.DataFrame(cluster)
        observed_start = frame.loc[frame["raw_start_year"].ne(1900), start_column]
        interval_start = (
            int(observed_start.min())
            if not observed_start.empty
            else int(frame[start_column].min())
        )
        interval_end = int(frame[end_column].max())
        if frequency == "year":
            start_year = interval_start
            end_year = interval_end
            start_period = int(start_year) * 4
            end_period = int(end_year) * 4 + 3
        else:
            start_period = interval_start
            end_period = interval_end
            start_year = period_to_year(start_period)
            end_year = period_to_year(end_period)

        ie_rows = frame[frame["source_ie"].eq(1)]
        oc_rows = frame[frame["source_oc"].eq(1)]
        role_values = list(ie_rows["rolename"]) + list(oc_rows["rolename"])
        role_values = [value for value in role_values if str(value).strip()]
        role = role_values[0] if role_values else ""
        name_values = list(ie_rows["DirectorName"]) + list(oc_rows["DirectorName"])
        name_values = [value for value in name_values if str(value).strip()]
        name = name_values[0] if name_values else ""
        seniority_values = [value for value in oc_rows["seniority"] if str(value).strip()]
        seniority = seniority_values[0] if seniority_values else ""
        brdposition = choose_board_position(frame["brdposition"])
        source_ie = int(frame["source_ie"].max())
        source_oc = int(frame["source_oc"].max())
        source = "ie_oc" if source_ie and source_oc else "individual_employment" if source_ie else "organization_composition"
        mapping_sources = join_mapping_values(frame["mapping_source"])
        confidence = "high" if any("companyid" in str(value) for value in frame["mapping_source"]) else "medium"

        output.append(
            {
                "DirectorID": int(frame.iloc[0]["DirectorID"]),
                "DirectorName": name,
                "company_key": frame.iloc[0]["company_key"],
                "RawCompanyID": int(frame.iloc[0]["RawCompanyID"]) if pd.notna(frame.iloc[0]["RawCompanyID"]) else -1,
                "rolename": role,
                "role_key": frame.iloc[0]["role_key"],
                "brdposition": brdposition,
                "seniority": seniority,
                "start_year": start_year,
                "end_year": end_year,
                "start_period": start_period,
                "end_period": end_period,
                "start_imputed": int(observed_start.empty),
                "end_censored": int(frame["end_censored"].max()),
                "mapping_source": mapping_sources,
                "mapping_confidence": confidence,
                "mapping_conflict": 0,
                "source_ie": source_ie,
                "source_oc": source_oc,
                "source": source,
            }
        )
    return output


def consolidate_spells(spells: pd.DataFrame, frequency: str = "year") -> pd.DataFrame:
    """Consolidate only overlapping same-role spells and preserve gaps."""
    if spells.empty:
        return spells
    spells = spells[spells["role_key"].ne("")].copy()
    spells = spells.drop_duplicates(
        subset=[
            "DirectorID", "company_key", "role_key", "start_year", "end_year",
            "start_period", "end_period",
            "brdposition", "seniority", "source_ie", "source_oc",
        ]
    )
    rows: List[dict] = []
    group_cols = ["DirectorID", "company_key", "role_key"]
    for _, group in spells.groupby(group_cols, sort=False):
        rows.extend(combine_spell_rows(group, frequency=frequency))
    return pd.DataFrame(rows)


def classify_leader(rolename: str, brdposition: str, seniority: str) -> str:
    """Classify one employment spell into the agreed leadership tiers."""
    role_up = str(rolename).strip().upper()
    brd_up = str(brdposition).strip().upper()
    seniority_up = str(seniority).strip().upper()

    if brd_up in {"YES", "INSIDE", "OUTSIDE"}:
        return "board"

    board_keywords = [
        "DIRECTOR - SD", "SUPERVISORY DIRECTOR", "INDEPENDENT DIRECTOR",
        "EXECUTIVE DIRECTOR", "CHAIRMAN", "CHAIRWOMAN", "VICE CHAIR",
        "NON-EXECUTIVE DIRECTOR", "LEAD INDEPENDENT DIRECTOR",
        "BOARD MEMBER", "INDEPENDENT NED",
    ]
    if seniority_up in {"SUPERVISORY DIRECTOR", "EXECUTIVE DIRECTOR"}:
        return "board"
    if any(keyword in role_up for keyword in board_keywords):
        return "board"

    csuite_patterns = [
        "CHIEF EXECUTIVE", "CEO", "CHIEF FINANCIAL", "CFO",
        "CHIEF OPERAT", "COO", "CHIEF TECHNOLOGY", "CTO",
        "CHIEF INFORMATION", "CIO", "CHIEF MARKETING", "CMO",
        "CHIEF SCIENTIFIC", "CSO", "CHIEF HUMAN RESOURCES", "CHRO",
        "CHIEF LEGAL", "CHIEF COMPLIANCE", "CHIEF MEDICAL",
        "CHIEF STRATEGY", "CHIEF BUSINESS", "CHIEF COMMERCIAL",
        "CHIEF DATA", "CHIEF REVENUE", "CHIEF DIGITAL",
        "CHIEF ACCOUNTING", "CHIEF RISK", "CHIEF INVESTMENT",
        "GENERAL COUNSEL",
    ]
    for pattern in csuite_patterns:
        if pattern in role_up and "ASSISTANT" not in role_up and "DEPUTY" not in role_up:
            return "csuite"

    if "PRESIDENT" in role_up and "VICE PRESIDENT" not in role_up and "ASSISTANT" not in role_up:
        return "csuite"

    vp_patterns = ["SENIOR VP", "SVP", "EXECUTIVE VP", "EVP", "GROUP VP", "CORPORATE VP"]
    if not any(pattern in role_up for pattern in vp_patterns) and "VICE PRESIDENT" not in role_up:
        return "excluded"

    technical_keywords = [
        "TECHNOLOGY", "TECHNICAL", "ENGINEER", "R&D", "RESEARCH", "DEVELOPMENT",
        "INFORMATION", "DATA", "DIGITAL", "HUMAN RESOURCES", "PERSONNEL",
        "PEOPLE", "TALENT", "HR", "SCIENTIF", "LAB", "CLINICAL", "MEDICAL",
        "REGULATORY", "QUALITY", "MANUFACTURING", "OPERATION", "INNOVATION",
        "INTELLECTUAL PROPERTY",
    ]
    return "vp_tech_hr" if any(keyword in role_up for keyword in technical_keywords) else "excluded"


def expand_spells(spells: pd.DataFrame, frequency: str = "year") -> pd.DataFrame:
    """Expand eligible spells into annual or quarterly observations."""
    if frequency not in {"year", "quarter"}:
        raise ValueError(f"Unknown frequency: {frequency}")
    if spells.empty:
        return pd.DataFrame()
    spells = spells.copy()
    spells["leader_tier"] = spells.apply(
        lambda row: classify_leader(row["rolename"], row["brdposition"], row["seniority"]),
        axis=1,
    )
    spells["role_priority"] = spells["leader_tier"].map(TIER_PRIORITY).astype(int)
    eligible = spells[(spells["leader_tier"] != "excluded") & spells["rolename"].ne("")].copy()

    rows: List[dict] = []
    for row in eligible.itertuples(index=False):
        if frequency == "year":
            start_index = max(int(row.start_year), SAMPLE_START)
            end_index = min(int(row.end_year), SAMPLE_END)
        else:
            start_index = max(int(row.start_period), SAMPLE_START * 4)
            end_index = min(int(row.end_period), SAMPLE_END * 4 + 3)
        if start_index > end_index:
            continue
        for index in range(start_index, end_index + 1):
            year = index if frequency == "year" else period_to_year(index)
            quarter = None if frequency == "year" else period_to_quarter(index)
            rows.append(
                {
                    "DirectorID": int(row.DirectorID),
                    "DirectorName": row.DirectorName,
                    "company_key": row.company_key,
                    "RawCompanyID": row.RawCompanyID,
                    "year": year,
                    "quarter": quarter,
                    "yearquarter": (
                        "" if quarter is None else f"{year}q{quarter}"
                    ),
                    "rolename": row.rolename,
                    "seniority": row.seniority,
                    "brdposition": row.brdposition,
                    "leader_tier": row.leader_tier,
                    "role_priority": row.role_priority,
                    "start_year": row.start_year,
                    "end_year": row.end_year,
                    "start_period": row.start_period,
                    "end_period": row.end_period,
                    "delist_year": getattr(row, "delist_year", pd.NA),
                    "delist_period": getattr(row, "delist_period", pd.NA),
                    "start_imputed": row.start_imputed,
                    "end_censored": row.end_censored,
                    "source": row.source,
                    "source_ie": row.source_ie,
                    "source_oc": row.source_oc,
                    "mapping_source": row.mapping_source,
                    "mapping_confidence": row.mapping_confidence,
                    "mapping_conflict": row.mapping_conflict,
                }
            )

    expanded = pd.DataFrame(rows)
    if expanded.empty:
        return expanded
    expanded["role_missing"] = expanded["rolename"].eq("").astype(int)
    time_columns = ["year"] if frequency == "year" else ["year", "quarter"]
    expanded = expanded.sort_values(
        ["DirectorID", "company_key"] + time_columns
        + ["role_priority", "role_missing", "start_year"],
        ascending=[True, True] + [True] * len(time_columns) + [False, True, False],
        kind="stable",
    )
    return expanded.drop_duplicates(
        ["DirectorID", "company_key"] + time_columns, keep="first"
    )


def build_board_candidates(
    bp: pd.DataFrame,
    spells: pd.DataFrame,
    display_names: Mapping[str, str],
    company_info: Mapping[str, Tuple[str, int]],
    frequency: str = "year",
) -> pd.DataFrame:
    """Build the boardex_pharma baseline candidates and enrich available fields."""
    if frequency not in {"year", "quarter"}:
        raise ValueError(f"Unknown frequency: {frequency}")
    board = bp[bp["inSSR"].eq(1)].copy()
    board["DirectorID"] = pd.to_numeric(board["DirectorID"], errors="coerce").astype(int)
    board["company_key"] = normalize_series(board["BoardName"])
    board["BoardName"] = board["company_key"].map(display_names)
    board["year"] = pd.to_numeric(board["year"], errors="coerce").astype(int)
    board["DirectorName"] = clean_display_series(board["DirectorName"])
    board["HOCountryName"] = board["company_key"].map(lambda key: company_info[key][0])
    board["CompanyID"] = board["company_key"].map(lambda key: company_info[key][1])
    if frequency == "quarter":
        board = board.loc[board.index.repeat(4)].copy()
        board["quarter"] = board.groupby(level=0).cumcount() + 1
        board["yearquarter"] = board.apply(
            lambda row: f"{row['year']}q{int(row['quarter'])}", axis=1
        )
        board["start_period"] = pd.NA
        board["end_period"] = pd.NA
    board["rolename"] = ""
    board["seniority"] = ""
    board["brdposition"] = "BOARD_BASELINE"
    board["leader_tier"] = "board"
    board["role_priority"] = TIER_PRIORITY["board"]
    board["start_year"] = pd.NA
    board["end_year"] = pd.NA
    board["start_imputed"] = 0
    board["end_censored"] = 0
    board["source"] = "boardex_pharma"
    board["source_ie"] = 0
    board["source_oc"] = 0
    board["mapping_source"] = "boardex_pharma"
    board["mapping_confidence"] = "high"
    board["mapping_conflict"] = 0

    if not spells.empty:
        time_columns = ["year"] if frequency == "year" else ["year", "quarter"]
        enrich_cols = [
            "DirectorID", "company_key"] + time_columns + ["rolename", "seniority",
            "source", "source_ie", "source_oc", "mapping_source", "mapping_confidence",
        ]
        enrichment = spells[enrich_cols].copy()
        enrichment = enrichment.rename(
            columns={
                "rolename": "spell_rolename",
                "seniority": "spell_seniority",
                "source": "spell_source",
                "source_ie": "spell_source_ie",
                "source_oc": "spell_source_oc",
                "mapping_source": "spell_mapping_source",
                "mapping_confidence": "spell_mapping_confidence",
            }
        )
        board = board.merge(
            enrichment,
            on=["DirectorID", "company_key"] + time_columns,
            how="left",
        )
        board["rolename"] = board["spell_rolename"].fillna("")
        board["seniority"] = board["spell_seniority"].fillna("")
        board["source"] = board.apply(
            lambda row: join_source_values(["boardex_pharma", row["spell_source"]]),
            axis=1,
        )
        board["source_ie"] = board[["source_ie", "spell_source_ie"]].max(axis=1).astype(int)
        board["source_oc"] = board[["source_oc", "spell_source_oc"]].max(axis=1).astype(int)
        board["mapping_source"] = board.apply(
            lambda row: join_mapping_values(["boardex_pharma", row["spell_mapping_source"]]),
            axis=1,
        )
        board["mapping_confidence"] = board["spell_mapping_confidence"].fillna("high")
        board = board.drop(
            columns=[
                "spell_rolename", "spell_seniority", "spell_source", "spell_source_ie",
                "spell_source_oc", "spell_mapping_source", "spell_mapping_confidence",
            ]
        )

    board["role_missing"] = board["rolename"].eq("").astype(int)
    board["inSSR"] = 1
    return board


def combine_annual_candidates(
    candidates: pd.DataFrame, frequency: str = "year"
) -> pd.DataFrame:
    """Choose one highest-priority record per person-company time period."""
    if frequency not in {"year", "quarter"}:
        raise ValueError(f"Unknown frequency: {frequency}")
    key = ["DirectorID", "company_key", "year"]
    if frequency == "quarter":
        key.append("quarter")
    candidates = candidates.copy()
    candidates["role_missing"] = candidates["rolename"].fillna("").eq("").astype(int)
    candidates = candidates.sort_values(
        key + ["role_priority", "role_missing", "start_year"],
        ascending=[True] * len(key) + [False, True, False],
        kind="stable",
    )
    provenance = candidates.groupby(key, sort=False).agg(
        all_sources=("source", join_source_values),
        source_ie=("source_ie", "max"),
        source_oc=("source_oc", "max"),
    ).reset_index()
    chosen = candidates.drop_duplicates(key, keep="first").drop(
        columns=["source", "source_ie", "source_oc"]
    )
    chosen = chosen.merge(provenance, on=key, how="left")
    chosen["source"] = chosen["all_sources"]
    chosen = chosen.drop(columns=["all_sources"])
    chosen["inSSR"] = 1
    return chosen


def add_board_continuity_flag(roster: pd.DataFrame, frequency: str) -> pd.DataFrame:
    """Carry board status forward within a continuous person-company spell."""
    if frequency not in {"year", "quarter"}:
        raise ValueError(f"Unknown frequency: {frequency}")
    if roster.empty:
        roster["board_continuity"] = pd.Series(dtype="int8")
        return roster

    frame = roster.copy()
    if frequency == "year":
        frame["_continuity_time"] = frame["year"].astype(int)
    else:
        frame["_continuity_time"] = (
            frame["year"].astype(int) * 4 + frame["quarter"].astype(int) - 1
        )

    group_columns = ["DirectorID", "company_key"]
    frame = frame.sort_values(
        group_columns + ["_continuity_time"], kind="stable"
    )
    gap = frame.groupby(group_columns, sort=False)["_continuity_time"].diff().fillna(1).gt(1)
    frame["_employment_spell"] = gap.groupby(
        [frame[column] for column in group_columns], sort=False
    ).cumsum()
    frame["_is_board_role"] = frame["leader_tier"].eq("board")

    # Pipeline3 propagates board status forward from the first board role.
    # A prior CEO spell therefore remains a CEO-only observation until the
    # first observed board role, while a later board-to-CEO transition remains
    # a continuous membership in the narrow event definition.
    frame["board_continuity"] = (
        frame.groupby(group_columns + ["_employment_spell"], sort=False)[
            "_is_board_role"
        ].cummax()
        .astype("int8")
    )
    frame = frame.drop(
        columns=["_continuity_time", "_employment_spell", "_is_board_role"]
    )
    return frame


def build_final_roster(
    bp: pd.DataFrame,
    spells: pd.DataFrame,
    display_names: Mapping[str, str],
    company_info: Mapping[str, Tuple[str, int]],
    frequency: str = "year",
) -> pd.DataFrame:
    """Build personnel membership from validated IE and OC employment records."""
    roster = combine_annual_candidates(spells, frequency=frequency)
    roster = add_board_continuity_flag(roster, frequency=frequency)
    roster["BoardName"] = roster["company_key"].map(display_names)
    roster["HOCountryName"] = roster["company_key"].map(lambda key: company_info[key][0])
    roster["CompanyID"] = roster["company_key"].map(lambda key: company_info[key][1])
    roster["role_missing"] = roster["rolename"].fillna("").eq("").astype(int)
    roster["seniority_missing"] = roster["seniority"].fillna("").eq("").astype(int)

    output_columns = [
        "DirectorID", "DirectorName", "BoardName", "company_key", "HOCountryName",
        "CompanyID", "year",
    ]
    if frequency == "quarter":
        output_columns.extend(["quarter", "yearquarter"])
    output_columns.extend([
        "rolename", "leader_tier", "role_priority",
        "board_continuity",
        "seniority", "seniority_missing", "brdposition", "role_missing",
        "start_year", "end_year",
    ])
    if frequency == "quarter":
        output_columns.extend(["start_period", "end_period"])
    output_columns.extend([
        "delist_year", "delist_period", "start_imputed", "end_censored", "source",
        "source_ie", "source_oc", "mapping_source", "mapping_confidence",
        "mapping_conflict", "RawCompanyID", "inSSR",
    ])
    roster = roster[output_columns].copy()
    sort_columns = ["BoardName", "year"]
    if frequency == "quarter":
        sort_columns.append("quarter")
    sort_columns.append("DirectorID")
    return roster.sort_values(sort_columns, kind="stable")


def validate_roster(
    roster: pd.DataFrame,
    label: str,
    canonical_company_count: int,
    frequency: str = "year",
) -> None:
    """Validate one output roster before writing it."""
    if "inCiteline" in roster.columns:
        raise AssertionError(f"inCiteline must not be present in {label}")
    key = ["DirectorID", "company_key", "year"]
    if frequency == "quarter":
        key.append("quarter")
        if not roster["quarter"].between(1, 4).all():
            raise AssertionError(f"{label} contains invalid quarters")
        if roster["yearquarter"].eq("").any() or roster["yearquarter"].isna().any():
            raise AssertionError(f"{label} contains missing year-quarter labels")
    if roster.duplicated(key).any():
        raise AssertionError(f"{label} has duplicate person-company-time records")
    if not roster["year"].between(SAMPLE_START, SAMPLE_END).all():
        raise AssertionError(f"{label} contains years outside the analysis window")
    if roster["company_key"].nunique() != canonical_company_count:
        raise AssertionError(f"{label} does not contain all SSR companies")


def atomic_write_outputs(roster: pd.DataFrame, stem: str) -> None:
    """Write validated CSV and Unicode-capable Stata outputs atomically."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUTPUT_DIR / f"{stem}.csv"
    stata_path = OUTPUT_DIR / f"{stem}.dta"
    csv_tmp = OUTPUT_DIR / f"{stem}.csv.tmp"
    stata_tmp = OUTPUT_DIR / f"{stem}.dta.tmp"

    output = roster.copy()
    string_columns = output.select_dtypes(include="object").columns
    for column in string_columns:
        output[column] = output[column].fillna("").astype(str)
    output.to_csv(csv_tmp, index=False, encoding="utf-8-sig")
    output.to_stata(stata_tmp, write_index=False, version=118)

    if not csv_tmp.exists() or not stata_tmp.exists():
        raise RuntimeError("Output validation failed: temporary output is missing")
    os.replace(csv_tmp, csv_path)
    os.replace(stata_tmp, stata_path)
    print(f"  CSV saved: {csv_path}")
    print(f"  Stata saved: {stata_path}")


def read_exact_source(
    path: Path,
    source: str,
    usecols: Sequence[str],
    id_to_key: Mapping[int, str],
    chunk_size: int,
    name_map: Optional[Mapping[str, str]] = None,
    mapping_source: Optional[Mapping[str, str]] = None,
) -> pd.DataFrame:
    """Read fixed-name/current-ID matches while preserving source role fields.

    The optional maps retain compatibility with callers that request only
    current-ID matches. The main build always supplies the initial name maps.
    """
    chunks: List[pd.DataFrame] = []
    for chunk in pd.read_csv(path, usecols=usecols, chunksize=chunk_size, low_memory=False):
        resolved = resolve_company_chunk(
            chunk, name_map if name_map is not None else {},
            mapping_source if mapping_source is not None else {}, id_to_key,
        )
        keep = resolved["company_key"].notna() & resolved["mapping_conflict"].eq(0)
        keep &= pd.to_numeric(chunk["directorid"], errors="coerce").notna()
        if not keep.any():
            continue
        selected = chunk.loc[keep].copy()
        selected["RawCompanyID"] = resolved.loc[keep, "RawCompanyID"].fillna(-1).astype(int)
        for column in ["company_key", "mapping_source", "mapping_confidence", "mapping_conflict"]:
            selected[column] = resolved.loc[keep, column]
        chunks.append(selected)
    if not chunks:
        return pd.DataFrame()

    raw = pd.concat(chunks, ignore_index=True)
    raw["DirectorID"] = pd.to_numeric(raw["directorid"], errors="coerce").astype(int)
    raw["DirectorName"] = clean_display_series(raw["directorname"])
    raw["rolename"] = clean_display_series(raw["rolename"])
    raw["role_key"] = normalize_series(raw["rolename"])
    raw["raw_companyname"] = clean_display_series(raw["companyname"])
    # Clean existing values before supplying empty fields missing in a source.
    # IE provides brdposition; OC provides seniority. Never overwrite either.
    for column in ["brdposition", "seniority"]:
        if column in raw.columns:
            raw[column] = clean_display_series(raw[column])
        else:
            raw[column] = ""
    raw["source_ie"] = int(source == "individual_employment")
    raw["source_oc"] = int(source == "organization_composition")
    return raw


def valid_company_dates(
    frames: Sequence[pd.DataFrame],
) -> Tuple[pd.DataFrame, Dict[str, int], Dict[str, int], Dict[str, int], Dict[str, int]]:
    """Compute valid earliest and latest observed company dates from IE/OC."""
    all_rows = pd.concat(frames, ignore_index=True, sort=False)
    start_year = parse_year_series(all_rows["datestartrole"])
    start_period = parse_period_series(all_rows["datestartrole"])
    end_year = parse_year_series(all_rows["dateendrole"])
    end_period = parse_period_series(all_rows["dateendrole"])

    starts = pd.DataFrame({"company_key": all_rows["company_key"], "year": start_year, "period": start_period})
    starts = starts[
        starts["year"].notna()
        & starts["period"].notna()
        & starts["year"].ne(1900)
        & starts["year"].between(SAMPLE_START, SAMPLE_END)
    ].sort_values(["company_key", "year", "period"])
    starts = starts.drop_duplicates("company_key", keep="first")
    earliest = starts.set_index("company_key")["year"].astype(int).to_dict()
    earliest_period = starts.set_index("company_key")["period"].astype(int).to_dict()

    ends = pd.DataFrame({"company_key": all_rows["company_key"], "year": end_year, "period": end_period})
    ends = ends[
        ends["year"].notna()
        & ends["period"].notna()
        & ends["year"].between(SAMPLE_START, MAX_VALID_DATE_YEAR)
    ].sort_values(["company_key", "year", "period"])
    ends = ends.drop_duplicates("company_key", keep="last")
    latest = ends.set_index("company_key")["year"].astype(int).to_dict()
    latest_period = ends.set_index("company_key")["period"].astype(int).to_dict()
    return all_rows, earliest, earliest_period, latest, latest_period


def add_resolved_dates_p3(
    frame: pd.DataFrame,
    earliest: Mapping[str, int],
    latest: Mapping[str, int],
    earliest_period: Mapping[str, int],
    latest_period: Mapping[str, int],
    delist_year: Mapping[str, int],
    delist_period: Mapping[str, int],
    date_mode: str,
) -> pd.DataFrame:
    """Apply pipeline3 date rules, including canonical-name delisting dates."""
    output = frame.copy()
    raw_start = parse_year_series(output["datestartrole"]).fillna(1900).astype(int)
    raw_end = parse_year_series(output["dateendrole"]).fillna(9999).astype(int)
    raw_start_period = parse_period_series(output["datestartrole"]).fillna(SAMPLE_START * 4).astype(int)
    raw_end_period = parse_period_series(output["dateendrole"]).fillna((MAX_VALID_DATE_YEAR + 1) * 4).astype(int)

    start_sentinel = raw_start.eq(1900)
    end_sentinel = raw_end.gt(MAX_VALID_DATE_YEAR)
    if date_mode == DATE_MODE_DROP:
        keep = ~start_sentinel & ~end_sentinel
        output = output.loc[keep].copy()
        output["raw_start_year"] = raw_start.loc[keep]
        output["raw_end_year"] = raw_end.loc[keep]
        output["raw_start_period"] = raw_start_period.loc[keep]
        output["raw_end_period"] = raw_end_period.loc[keep]
        output["start_year"] = raw_start.loc[keep].astype(int)
        output["end_year"] = raw_end.loc[keep].astype(int)
        output["start_period"] = raw_start_period.loc[keep].astype(int)
        output["end_period"] = raw_end_period.loc[keep].astype(int)
        output["delist_year"] = output["company_key"].map(delist_year).astype("Int64")
        output["delist_period"] = output["company_key"].map(delist_period).astype("Int64")
        output["start_imputed"] = 0
        output["end_censored"] = 0
        return output
    if date_mode != DATE_MODE_IMPUTE:
        raise ValueError(f"Unknown date mode: {date_mode}")

    company_earliest = output["company_key"].map(earliest).fillna(1900).astype(int)
    company_earliest_period = output["company_key"].map(earliest_period).fillna(SAMPLE_START * 4).astype(int)
    start_year = raw_start.where(~start_sentinel, company_earliest)
    start_period = raw_start_period.where(~start_sentinel, company_earliest_period)

    company_latest = output["company_key"].map(latest).fillna(SAMPLE_END).astype(int)
    company_latest_period = output["company_key"].map(latest_period).fillna(SAMPLE_END * 4 + 3).astype(int)
    company_delist_year = output["company_key"].map(delist_year).astype("Int64")
    company_delist_period = output["company_key"].map(delist_period).astype("Int64")
    has_delist = end_sentinel & company_delist_year.notna() & company_delist_period.notna()
    resolved_end_year = company_latest.astype("Int64").where(~has_delist, company_delist_year).clip(upper=SAMPLE_END)
    resolved_end_period = company_latest_period.astype("Int64").where(~has_delist, company_delist_period).clip(upper=SAMPLE_END * 4 + 3)

    output["raw_start_year"] = raw_start
    output["raw_end_year"] = raw_end
    output["raw_start_period"] = raw_start_period
    output["raw_end_period"] = raw_end_period
    output["start_year"] = start_year.astype(int)
    output["end_year"] = raw_end.where(~end_sentinel, resolved_end_year).astype(int)
    output["start_period"] = start_period.astype(int)
    output["end_period"] = raw_end_period.where(~end_sentinel, resolved_end_period).astype(int)
    output["delist_year"] = company_delist_year
    output["delist_period"] = company_delist_period
    output["start_imputed"] = start_sentinel.astype(int)
    output["end_censored"] = end_sentinel.astype(int)
    return output


def add_dates_and_select(
    frame: pd.DataFrame,
    earliest: Mapping[str, int],
    earliest_period: Mapping[str, int],
    latest: Mapping[str, int],
    latest_period: Mapping[str, int],
    delist_year: Mapping[str, int],
    delist_period: Mapping[str, int],
) -> pd.DataFrame:
    """Apply pipeline3 imputation and retain the standardized spell schema."""
    frame = add_resolved_dates_p3(
        frame, earliest, latest, earliest_period, latest_period,
        delist_year, delist_period, DATE_MODE_IMPUTE,
    )
    if frame.empty:
        return frame
    return frame[
        [
            "DirectorID", "DirectorName", "company_key", "RawCompanyID", "raw_companyname",
            "rolename", "role_key", "brdposition", "seniority", "datestartrole", "dateendrole",
            "start_year", "end_year", "start_period", "end_period", "raw_start_year", "raw_end_year",
            "raw_start_period", "raw_end_period", "start_imputed", "end_censored", "delist_year",
            "delist_period", "mapping_source", "mapping_confidence", "mapping_conflict", "source_ie", "source_oc",
        ]
    ]


def consolidate_spells_vectorized(spells: pd.DataFrame, frequency: str = "year") -> pd.DataFrame:
    """Consolidate same-role intervals with grouped vectorized interval labels."""
    if frequency not in {"year", "quarter"}:
        raise ValueError(f"Unknown frequency: {frequency}")
    if spells.empty:
        return spells
    frame = spells[spells["role_key"].ne("")].copy()
    frame = frame.drop_duplicates(
        subset=["DirectorID", "company_key", "role_key", "start_year", "end_year",
                "start_period", "end_period", "brdposition", "seniority", "source_ie", "source_oc"]
    )
    group_cols = ["DirectorID", "company_key", "role_key"]
    start_column = "start_year" if frequency == "year" else "start_period"
    end_column = "end_year" if frequency == "year" else "end_period"
    frame = frame.sort_values(group_cols + [start_column, end_column, "source_ie"], kind="stable").reset_index(drop=True)
    grouped = frame.groupby(group_cols, sort=False)
    frame["_running_end"] = grouped[end_column].cummax()
    frame["_previous_running_end"] = frame.groupby(group_cols, sort=False)["_running_end"].shift()
    frame["_new_cluster"] = frame["_previous_running_end"].isna() | frame[start_column].gt(frame["_previous_running_end"] + 1)
    frame["_employment_cluster"] = frame.groupby(group_cols, sort=False)["_new_cluster"].cumsum()
    cluster_cols = group_cols + ["_employment_cluster"]

    for column in ["DirectorName", "raw_companyname", "rolename", "seniority", "brdposition", "mapping_source"]:
        frame[column] = frame[column].fillna("").astype(str).str.strip().replace({"": pd.NA, "nan": pd.NA, "None": pd.NA})
    frame["_observed_start"] = frame[start_column].where(frame["raw_start_year"].ne(1900))
    frame["_oc_seniority"] = frame["seniority"].where(frame["source_oc"].eq(1))
    summary = frame.groupby(cluster_cols, sort=False, as_index=False).agg(
        DirectorID=("DirectorID", "first"), DirectorName=("DirectorName", "first"),
        company_key=("company_key", "first"), RawCompanyID=("RawCompanyID", "first"),
        raw_companyname=("raw_companyname", "first"), rolename=("rolename", "first"),
        role_key=("role_key", "first"), brdposition=("brdposition", choose_board_position),
        seniority=("_oc_seniority", "first"), all_start=(start_column, "min"),
        observed_start=("_observed_start", "min"), all_end=(end_column, "max"),
        end_censored=("end_censored", "max"), delist_year=("delist_year", "min"),
        delist_period=("delist_period", "min"), mapping_source=("mapping_source", join_mapping_values),
        source_ie=("source_ie", "max"), source_oc=("source_oc", "max"),
    )
    summary["interval_start"] = summary["observed_start"].fillna(summary["all_start"]).astype(int)
    summary["interval_end"] = summary["all_end"].astype(int)
    if frequency == "year":
        summary["start_year"] = summary["interval_start"]
        summary["end_year"] = summary["interval_end"]
        summary["start_period"] = summary["start_year"] * 4
        summary["end_period"] = summary["end_year"] * 4 + 3
    else:
        summary["start_period"] = summary["interval_start"]
        summary["end_period"] = summary["interval_end"]
        summary["start_year"] = summary["start_period"].map(period_to_year)
        summary["end_year"] = summary["end_period"].map(period_to_year)
    summary["start_imputed"] = summary["observed_start"].isna().astype(int)
    summary["mapping_conflict"] = 0
    summary["mapping_confidence"] = summary["mapping_source"].fillna("").map(
        lambda value: "high" if "companyid" in str(value) else "medium"
    )
    summary["source"] = "organization_composition"
    summary.loc[summary["source_ie"].eq(1) & summary["source_oc"].eq(0), "source"] = "individual_employment"
    summary.loc[summary["source_ie"].eq(1) & summary["source_oc"].eq(1), "source"] = "ie_oc"
    return summary.drop(columns=["_employment_cluster", "all_start", "observed_start", "all_end", "interval_start", "interval_end"])


def write_ssr_ie_oc_intermediate(ie: pd.DataFrame, oc: pd.DataFrame) -> pd.DataFrame:
    """Write fixed-name/current-ID SSR IE/OC records used by all variants."""
    intermediate = pd.concat(
        [ie.assign(source_dataset="individual_employment"), oc.assign(source_dataset="organization_composition")],
        ignore_index=True, sort=False,
    )
    if intermediate.empty:
        raise RuntimeError("The SSR-filtered IE/OC intermediate dataset is empty")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for source_name, frame in intermediate.groupby("source_dataset", sort=False):
        frame.to_csv(OUTPUT_DIR / f"ssr_{source_name}_intermediate.csv", index=False, encoding="utf-8-sig")
    intermediate.to_csv(OUTPUT_DIR / "ssr_ie_oc_intermediate.csv", index=False, encoding="utf-8-sig")
    return intermediate


def main_legacy() -> None:
    """Run the superseded code-new roster build for audit purposes."""
    print("=== Step 1: Loading boardex_pharma baseline ===")
    bp = pd.read_stata(PHARMA_DTA)
    bp["year"] = pd.to_numeric(bp["year"], errors="coerce").astype(int)
    canonical_names, display_names, id_to_key, company_info = build_company_info(bp)
    if len(canonical_names) != 134:
        raise ValueError(f"Expected 134 SSR companies, found {len(canonical_names)}")
    print(f"  SSR canonical companies: {len(canonical_names)}")
    print(f"  Board baseline rows: {int(bp['inSSR'].eq(1).sum())}")

    # The new pipeline intentionally does not load name crosswalks and does
    # not discover historical CompanyIDs.  IE/OC rows are matched directly to
    # the current SSR CompanyID map created from boardex_pharma.dta.
    name_map: Dict[str, str] = {}
    mapping_source: Dict[str, str] = {}
    print("=== Step 2: Matching current SSR CompanyIDs only ===")
    print(f"  Current SSR CompanyIDs used for IE/OC matching: {len(id_to_key):,}")

    source_paths = [(ORG_COMP, OC_CHUNK_SIZE), (INDIV_EMPLOY, IE_CHUNK_SIZE)]
    print("=== Step 3: Materializing IE and OC records for current SSR CompanyIDs ===")
    source_paths = materialize_ssr_ie_oc(source_paths, name_map, mapping_source, id_to_key)
    # Delisting dates are not used when imputing open-ended end dates.
    global DELIST_DATES
    DELIST_DATES = {}

    print("=== Step 3b: Finding earliest valid company years and quarters ===")
    earliest, earliest_period = collect_earliest_years(
        source_paths,
        name_map,
        mapping_source,
        id_to_key,
    )
    print(f"  Companies with valid earliest year: {len(earliest)}")

    print("=== Step 3c: Finding latest valid company years and quarters ===")
    latest, latest_period = collect_latest_years(
        source_paths,
        name_map,
        mapping_source,
        id_to_key,
    )
    print(f"  Companies with valid latest year: {len(latest)}")

    print("=== Step 4: Loading employment spells for both date rules ===")
    ie_usecols = [
        "companyid", "companyname", "directorid", "directorname", "rolename",
        "brdposition", "datestartrole", "dateendrole",
    ]
    oc_usecols = [
        "companyid", "directorid", "directorname", "companyname", "rolename",
        "seniority", "datestartrole", "dateendrole",
    ]
    ie = load_source_spells_fixed(
        source_paths[1][0], "individual_employment", IE_CHUNK_SIZE, ie_usecols,
        name_map, mapping_source, id_to_key, earliest, latest,
        earliest_period, latest_period, DATE_MODE_IMPUTE,
    )
    oc = load_source_spells_fixed(
        source_paths[0][0], "organization_composition", OC_CHUNK_SIZE, oc_usecols,
        name_map, mapping_source, id_to_key, earliest, latest,
        earliest_period, latest_period, DATE_MODE_IMPUTE,
    )
    print(f"  Matched IE rows: {len(ie):,}")
    print(f"  Matched OC rows: {len(oc):,}")
    all_spells = pd.concat([ie, oc], ignore_index=True)
    drop_all_spells = all_spells[
        all_spells["start_imputed"].eq(0) & all_spells["end_censored"].eq(0)
    ].copy()
    imputed_year_spells = consolidate_spells(all_spells, frequency="year")
    imputed_quarter_spells = consolidate_spells(all_spells, frequency="quarter")
    drop_year_spells = consolidate_spells(drop_all_spells, frequency="year")
    drop_quarter_spells = consolidate_spells(drop_all_spells, frequency="quarter")
    print(f"  Imputed year consolidated spells: {len(imputed_year_spells):,}")
    print(f"  Imputed quarter consolidated spells: {len(imputed_quarter_spells):,}")
    print(f"  Sentinel-dropped year consolidated spells: {len(drop_year_spells):,}")
    print(f"  Sentinel-dropped quarter consolidated spells: {len(drop_quarter_spells):,}")

    print("=== Step 5: Expanding four spell datasets ===")
    imputed_year = expand_spells(imputed_year_spells, frequency="year")
    imputed_yearquarter = expand_spells(
        imputed_quarter_spells, frequency="quarter"
    )
    drop_year = expand_spells(drop_year_spells, frequency="year")
    drop_yearquarter = expand_spells(drop_quarter_spells, frequency="quarter")
    print(f"  Imputed annual leadership rows: {len(imputed_year):,}")
    print(f"  Imputed quarterly leadership rows: {len(imputed_yearquarter):,}")
    print(f"  Sentinel-dropped annual leadership rows: {len(drop_year):,}")
    print(f"  Sentinel-dropped quarterly leadership rows: {len(drop_yearquarter):,}")

    print("=== Step 6: Combining boardex_pharma with four employment datasets ===")
    imputed_year_roster = build_final_roster(
        bp, imputed_year, display_names, company_info, frequency="year"
    )
    imputed_yearquarter_roster = build_final_roster(
        bp, imputed_yearquarter, display_names, company_info, frequency="quarter"
    )
    drop_year_roster = build_final_roster(
        bp, drop_year, display_names, company_info, frequency="year"
    )
    drop_yearquarter_roster = build_final_roster(
        bp, drop_yearquarter, display_names, company_info, frequency="quarter"
    )
    roster_outputs = [
        ("Imputed-year", imputed_year_roster, "year"),
        ("Imputed-yearquarter", imputed_yearquarter_roster, "quarter"),
        ("Drop-sentinel-year", drop_year_roster, "year"),
        ("Drop-sentinel-yearquarter", drop_yearquarter_roster, "quarter"),
    ]
    for label, roster, frequency in roster_outputs:
        validate_roster(roster, label.lower(), len(canonical_names), frequency)
        print(f"  {label} roster rows: {len(roster):,}")
        print(f"  {label} unique individuals: {roster['DirectorID'].nunique():,}")
        print(f"  {label} source breakdown:\n{roster['source'].value_counts().to_string()}")

    print("=== Step 7: Saving four outputs ===")
    atomic_write_outputs(imputed_year_roster, "ssr_company_roster_imputed_year")
    atomic_write_outputs(
        imputed_yearquarter_roster, "ssr_company_roster_imputed_yearquarter"
    )
    atomic_write_outputs(drop_year_roster, "ssr_company_roster_drop_sentinel_year")
    atomic_write_outputs(
        drop_yearquarter_roster, "ssr_company_roster_drop_sentinel_yearquarter"
    )
    current_audit = pd.DataFrame(
        [
            {
                "CompanyID": company_id,
                "BoardName": display_names[company_key],
                "company_key": company_key,
                "MappingRule": "current_boardex_pharma_companyid_only",
            }
            for company_id, company_key in sorted(id_to_key.items())
        ]
    )
    audit_path = OUTPUT_DIR / "ssr_company_current_companyid_audit.csv"
    current_audit.to_csv(audit_path, index=False, encoding="utf-8-sig")
    print(f"  Current CompanyID audit saved: {audit_path}")
    print("DONE.")


def main() -> None:
    """Build rosters with fixed initial names and current SSR IDs only."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print("=== Pipeline3 roster port: loading boardex_pharma baseline ===")
    bp = pd.read_stata(PHARMA_DTA)
    bp["year"] = pd.to_numeric(bp["year"], errors="coerce").astype(int)
    canonical_names, display_names, id_to_key, company_info = build_company_info(bp)
    if len(canonical_names) != 134 or len(id_to_key) != 134:
        raise ValueError("The SSR universe must contain 134 unique current CompanyIDs.")

    # Build the name maps once from reference data. IE/OC records cannot add
    # aliases or historical IDs, even when a name identifies a historical row.
    name_map, mapping_source, ambiguous = build_name_mapping(
        canonical_names, pd.read_stata(WITHIN_DTA), pd.read_stata(CROSSWALK_DTA)
    )
    print(f"  Current SSR CompanyIDs: {len(id_to_key):,}")
    print(f"  Fixed initial aliases: {len(name_map):,}; ambiguous aliases: {len(ambiguous):,}")
    pd.DataFrame(
        [{"alias": alias, "company_key": target, "mapping_source": mapping_source[alias]}
         for alias, target in sorted(name_map.items())],
        columns=["alias", "company_key", "mapping_source"],
    ).to_csv(OUTPUT_DIR / "ssr_company_initial_name_audit.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(
        [{"alias": alias, "candidate_companies": " | ".join(targets)}
         for alias, targets in sorted(ambiguous.items())],
        columns=["alias", "candidate_companies"],
    ).to_csv(OUTPUT_DIR / "ssr_company_ambiguous_name_audit.csv", index=False, encoding="utf-8-sig")
    delist_year: Dict[str, int] = {}
    delist_period: Dict[str, int] = {}
    for company_key, display_name in display_names.items():
        year, period = extract_delist_period(display_name)
        if year is not None and period is not None:
            delist_year[company_key] = year
            delist_period[company_key] = period

    ie_cols = [
        "companyid", "companyname", "directorid", "directorname", "rolename",
        "brdposition", "datestartrole", "dateendrole",
    ]
    oc_cols = [
        "companyid", "directorid", "directorname", "companyname", "rolename",
        "seniority", "datestartrole", "dateendrole",
    ]
    print("=== Reading fixed-name/current-ID IE/OC records without propagation ===")
    ie_raw = read_exact_source(
        INDIV_EMPLOY, "individual_employment", ie_cols, id_to_key, IE_CHUNK_SIZE,
        name_map=name_map, mapping_source=mapping_source,
    )
    oc_raw = read_exact_source(
        ORG_COMP, "organization_composition", oc_cols, id_to_key, OC_CHUNK_SIZE,
        name_map=name_map, mapping_source=mapping_source,
    )
    all_raw, earliest, earliest_period, latest, latest_period = valid_company_dates([ie_raw, oc_raw])
    ie = add_dates_and_select(
        ie_raw, earliest, earliest_period, latest, latest_period, delist_year, delist_period
    )
    oc = add_dates_and_select(
        oc_raw, earliest, earliest_period, latest, latest_period, delist_year, delist_period
    )
    intermediate = write_ssr_ie_oc_intermediate(ie, oc)
    all_spells = pd.read_csv(OUTPUT_DIR / "ssr_ie_oc_intermediate.csv", low_memory=False)
    drop_spells = all_spells[
        all_spells["start_imputed"].eq(0) & all_spells["end_censored"].eq(0)
    ].copy()

    variants = {
        "imputed_year": (all_spells, "year"),
        "imputed_yearquarter": (all_spells, "quarter"),
        "drop_sentinel_year": (drop_spells, "year"),
        "drop_sentinel_yearquarter": (drop_spells, "quarter"),
    }
    print(
        f"  Matched raw rows: IE={len(ie_raw):,}; OC={len(oc_raw):,}; "
        f"intermediate={len(intermediate):,}; delist names={len(delist_year):,}"
    )
    for variant, (spell_input, frequency) in variants.items():
        spells = consolidate_spells_vectorized(spell_input, frequency)
        roster = expand_spells(spells, frequency)
        roster = build_final_roster(bp, roster, display_names, company_info, frequency)
        validate_roster(roster, variant, len(canonical_names), frequency)
        atomic_write_outputs(roster, f"ssr_company_roster_{variant}")
        print(
            f"{variant}: rows={len(roster):,}; directors={roster['DirectorID'].nunique():,}; "
            f"companies={roster['CompanyID'].nunique():,}",
            flush=True,
        )

    audit = pd.DataFrame(
        {
            "rule": ["initial_names_plus_current_ids_no_history_propagation"],
            "historical_name_tracing": [0],
            "historical_id_discovery": [0],
            "ie_oc_alias_learning": [0],
            "initial_name_aliases": [len(name_map)],
            "raw_matched_rows": [len(all_raw)],
            "raw_exact_id_rows": [int(all_raw["mapping_source"].eq("current_companyid").sum())],
            "intermediate_rows": [len(intermediate)],
            "ssr_companies": [len(canonical_names)],
            "delist_name_count": [len(delist_year)],
        }
    )
    audit.to_csv(OUTPUT_DIR / "latest_name_build_audit.csv", index=False, encoding="utf-8-sig")
    current_audit = pd.DataFrame(
        [
            {
                "CompanyID": company_id,
                "BoardName": display_names[company_key],
                "company_key": company_key,
                "MappingRule": "current_companyids_plus_initial_names_no_history_propagation",
            }
            for company_id, company_key in sorted(id_to_key.items())
        ]
    )
    current_audit.to_csv(OUTPUT_DIR / "ssr_company_current_companyid_audit.csv", index=False, encoding="utf-8-sig")
    print("Fixed-name/current-ID roster build complete; historical propagation disabled.", flush=True)


if __name__ == "__main__":
    main()
