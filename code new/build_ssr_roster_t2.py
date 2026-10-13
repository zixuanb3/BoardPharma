"""Purpose:
    Build the dedicated T2 SSR leadership roster directly from original data.
    T2 retains the current IE/OC matching, role, flag-date and continuity rules,
    then appends observed boardex_pharma SSR director-year baseline membership.

Input:
    boardex_pharma.dta, cro_bname_boardex_within.dta,
    cro_bname_boardex_citeline.dta, individual_employment.csv,
    and organization_composition.csv. No previously generated roster or local
    builder module is required. Python requires pandas, numpy and pyarrow.

Output:
    Four CSV/Stata 118 rosters: imputed_year, imputed_yearquarter,
    drop_sentinel_year and drop_sentinel_yearquarter. Audits include fixed name
    mappings, raw matched records, company date bounds, row-level flag decisions,
    build counts and an optional full-column comparison with existing T2 files.
    Default output: D:/pharma/t2_standalone_20261008/rosters.

Usage:
    D:/ProgramData/anaconda3/python.exe build_ssr_roster_t2.py
    D:/ProgramData/anaconda3/python.exe build_ssr_roster_t2.py --output-dir D:/pharma/my_t2/rosters

Policy:
    Cutoff is fixed at 2026Q3. No historical CompanyID/alias discovery is run.
    IE board flags Yes/Inside/Outside, OC board seniority, and current role
    keywords remain active. This script implements T2 only, without T1 or T3.
    Baseline years expand to all four quarters; they do not create employment
    start/end dates or connect different observed baseline years by themselves.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import numpy as np
import pandas as pd

INTERIM_DIR = Path(r"D:/Dropbox/BoardPharma/InterimData")
RAW_NA_DIR = Path(r"D:/Dropbox/BoardPharma/RawData/boardex/boardex_na")
OUTPUT_DIR = Path(r"D:/pharma/t2_standalone_20261008/rosters")
SAMPLE_START = 1990
SAMPLE_END = CUTOFF_YEAR = 2026
CUTOFF_PERIOD = 2026 * 4 + 2
IE_CHUNK_SIZE = 200_000
OC_CHUNK_SIZE = 300_000
UNKNOWN_FLAGS = {75, 80}
YEAR_ONLY_FLAGS = {25, 30}
KNOWN_FLAGS = {10, 15, 20, 25, 30, 40, 75, 80}
GENERIC_ALIASES = {
    "THE", "INC", "LTD", "LLC", "PLC", "CORP", "AG", "SA", "NV",
    "LISTED", "DE-LISTED", "DE LISTED", "REDOMICILED",
}
TIER_PRIORITY = {"excluded": 0, "vp_tech_hr": 1, "csuite": 2, "board": 3}
TRACE = ["start_flag_codes", "end_flag_codes", "start_date_rules", "end_date_rules"]

# Company matching is fixed before either large source is scanned.
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
        board["yearquarter"] = board["year"].astype(str) + "q" + board["quarter"].astype(str)
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


# Resolve original date flags before consolidating each independent role.
def endpoint_period(frame: pd.DataFrame, side: str) -> pd.Series:
    """Use Q1 for year-only starts, Q4 for end flag 25, and Q3 for current roles."""
    date = frame[f"date{side}role"]
    flag = pd.to_numeric(frame[f"date{side}roleflag"], errors="coerce")
    year = parse_year_series(date)
    period = parse_period_series(date)
    period = period.where(~flag.isin(YEAR_ONLY_FLAGS), year * 4)
    if side == "end":
        period = period.where(~flag.eq(25), year * 4 + 3)
        period = period.where(~flag.eq(40), CUTOFF_PERIOD)
    valid = flag.isin(KNOWN_FLAGS) & ~flag.isin(UNKNOWN_FLAGS)
    # A known 2026 end year may resolve to Q4. Preserve that endpoint while
    # expansion caps observable membership at the analysis cutoff of Q3.
    upper = CUTOFF_YEAR * 4 + 3 if side == "end" else CUTOFF_PERIOD
    valid &= period.notna() & period.between(1800 * 4, upper)
    return period.where(valid).astype("Int64")


def company_bounds(raw: pd.DataFrame, display: dict, info: dict) -> pd.DataFrame:
    """Use all valid matched IE/OC endpoint quarters for company-level bounds."""
    endpoints = pd.concat([
        pd.DataFrame({"company_key": raw.company_key, "period": endpoint_period(raw, side)})
        for side in ["start", "end"]
    ], ignore_index=True).dropna(subset=["period"])
    observed = endpoints.groupby("company_key").period.agg(earliest_period="min", latest_observed_period="max")
    current = raw.assign(_current=pd.to_numeric(raw.dateendroleflag, errors="coerce").eq(40)).groupby("company_key")._current.any()
    bounds = pd.DataFrame({"company_key": list(display)}).set_index("company_key").join(observed)
    bounds["has_current_flag40"] = bounds.index.to_series().map(current).fillna(False).astype(int)
    bounds["latest_period"] = bounds.latest_observed_period.where(bounds.has_current_flag40.eq(0), CUTOFF_PERIOD)
    for column in ["earliest_period", "latest_observed_period", "latest_period"]:
        bounds[column] = bounds[column].astype("Int64")
        bounds[column.replace("period", "yearquarter")] = bounds[column].map(
            lambda value: "" if pd.isna(value) else f"{int(value)//4}q{int(value)%4+1}")
    bounds["CompanyID"] = bounds.index.map(lambda key: info[key][1])
    bounds["BoardName"] = bounds.index.map(display)
    return bounds.reset_index()


def resolve_dates(raw: pd.DataFrame, bounds: pd.DataFrame, mode: str) -> pd.DataFrame:
    """Apply row-level flag rules before any same-role consolidation."""
    frame = raw.copy()
    start_flag = pd.to_numeric(frame.datestartroleflag, errors="coerce").astype("Int64")
    end_flag = pd.to_numeric(frame.dateendroleflag, errors="coerce").astype("Int64")
    frame["datestartroleflag"], frame["dateendroleflag"] = start_flag, end_flag
    start, end = endpoint_period(frame, "start"), endpoint_period(frame, "end")
    start_unknown, end_unknown = start_flag.isin(UNKNOWN_FLAGS), end_flag.isin(UNKNOWN_FLAGS)
    start_year_only, end_year_only = start_flag.isin(YEAR_ONLY_FLAGS), end_flag.isin(YEAR_ONLY_FLAGS)
    indexed = bounds.set_index("company_key")
    if mode == "imputed":
        start = start.where(~start_unknown, frame.company_key.map(indexed.earliest_period))
        end = end.where(~end_unknown, frame.company_key.map(indexed.latest_period))
        flag_keep = pd.Series(True, index=frame.index)
    elif mode == "drop":
        flag_keep = ~(start_unknown | end_unknown | start_year_only | end_year_only)
    else:
        raise ValueError(mode)
    frame["start_date_rule"] = np.select([mask.to_numpy(dtype=bool, na_value=False) for mask in [start_unknown, start_year_only]], ["company_earliest", "year_only_q1"], default="observed_quarter")
    frame["end_date_rule"] = np.select([mask.to_numpy(dtype=bool, na_value=False) for mask in [end_unknown, end_flag.eq(40), end_flag.eq(25), end_year_only]], ["company_latest", "current_2026q3", "year_only_q4", "year_only_q1"], default="observed_quarter")
    frame["start_period"], frame["end_period"] = start, end
    frame["start_year"], frame["end_year"] = start // 4, end // 4
    frame["start_imputed"] = (start_unknown | start_year_only).astype(int)
    frame["end_censored"] = (end_unknown | end_year_only | end_flag.eq(40)).astype(int)
    frame["raw_start_year"] = parse_year_series(frame.datestartrole)
    frame["raw_end_year"] = parse_year_series(frame.dateendrole)
    frame["raw_start_period"] = parse_period_series(frame.datestartrole)
    frame["raw_end_period"] = parse_period_series(frame.dateendrole)
    person_valid = pd.to_numeric(frame.directorid, errors="coerce").notna()
    # Validate annual and quarterly intervals separately. Never move a
    # precise source start backward to repair an inconsistent end date.
    dates_valid = start.notna() & end.notna() & (start // 4).le(end // 4).fillna(False)
    frame["quarter_interval_valid"] = start.le(end).fillna(False).astype(int)
    frame["quarter_rule_exclusion"] = np.where(frame.quarter_interval_valid.eq(0), "quarter_end_before_start_or_unresolved", "")
    frame["date_rule_keep"] = (flag_keep & dates_valid & person_valid).astype(int)
    frame["date_rule_exclusion"] = np.select([mask.to_numpy(dtype=bool, na_value=False) for mask in [~person_valid, ~flag_keep, start.isna() | end.isna(), (start // 4).gt(end // 4)]],
                                               ["missing_person_id", "drop_flag_75_80_25_30", "unresolved_or_invalid_endpoint", "end_before_start"], default="")
    return frame


def codes(values: pd.Series) -> str:
    """Retain all contributing source flag codes as a compact sorted string."""
    return "|".join(str(int(value)) for value in sorted(set(values.dropna())))


def consolidate(frame: pd.DataFrame, frequency: str) -> pd.DataFrame:
    """Keep the previous same-role interval policy and retain flag provenance."""
    frame = frame[frame.role_key.ne("")].copy()
    start_column = "start_year" if frequency == "year" else "start_period"
    end_column = "end_year" if frequency == "year" else "end_period"
    group = ["DirectorID", "company_key", "role_key"]
    frame = frame.sort_values(group + [start_column, end_column, "source_ie"], kind="stable").reset_index(drop=True)
    running_end = frame.groupby(group, sort=False)[end_column].cummax()
    previous_end = running_end.groupby([frame[column] for column in group], sort=False).shift()
    new_cluster = previous_end.isna() | frame[start_column].gt(previous_end + 1)
    frame["_cluster"] = new_cluster.groupby([frame[column] for column in group], sort=False).cumsum()
    for column in ["DirectorName", "raw_companyname", "rolename", "seniority", "brdposition", "mapping_source"]:
        frame[column] = frame[column].fillna("").astype(str).str.strip().replace({"": pd.NA, "nan": pd.NA, "None": pd.NA})
    # A known year with Q1 imputation remains a known-year start. Only fully
    # unknown company-bound starts yield to a more precise same-role source.
    frame["_observed_start"] = frame[start_column].where(frame.start_date_rule.ne("company_earliest"))
    frame["_oc_seniority"] = frame.seniority.where(frame.source_oc.eq(1))
    summary = frame.groupby(group + ["_cluster"], sort=False, as_index=False).agg(
        DirectorID=("DirectorID", "first"), DirectorName=("DirectorName", "first"),
        company_key=("company_key", "first"), RawCompanyID=("RawCompanyID", "first"),
        raw_companyname=("raw_companyname", "first"), rolename=("rolename", "first"), role_key=("role_key", "first"),
        brdposition=("brdposition", choose_board_position), seniority=("_oc_seniority", "first"),
        all_start=(start_column, "min"), observed_start=("_observed_start", "min"), all_end=(end_column, "max"),
        start_imputed=("start_imputed", "max"), end_censored=("end_censored", "max"),
        start_flag_codes=("datestartroleflag", codes), end_flag_codes=("dateendroleflag", codes),
        start_date_rules=("start_date_rule", join_mapping_values), end_date_rules=("end_date_rule", join_mapping_values),
        mapping_source=("mapping_source", join_mapping_values), source_ie=("source_ie", "max"), source_oc=("source_oc", "max"))
    summary["interval_start"] = summary.observed_start.fillna(summary.all_start).astype(int)
    summary["interval_end"] = summary.all_end.astype(int)
    if frequency == "year":
        summary["start_year"], summary["end_year"] = summary.interval_start, summary.interval_end
        summary["start_period"], summary["end_period"] = summary.start_year * 4, summary.end_year * 4 + 3
    else:
        summary["start_period"], summary["end_period"] = summary.interval_start, summary.interval_end
        summary["start_year"], summary["end_year"] = summary.start_period // 4, summary.end_period // 4
    summary["delist_year"], summary["delist_period"] = np.nan, np.nan
    summary["mapping_conflict"] = 0
    summary["mapping_confidence"] = np.where(summary.mapping_source.str.contains("companyid", na=False), "high", "medium")
    summary["source"] = np.select([summary.source_ie.eq(1) & summary.source_oc.eq(1), summary.source_ie.eq(1)], ["ie_oc", "individual_employment"], default="organization_composition")
    return summary.drop(columns=["_cluster", "all_start", "observed_start", "all_end", "interval_start", "interval_end"])


def expand(frame: pd.DataFrame, frequency: str) -> pd.DataFrame:
    """Expand with vectorized repeats and a hard upper cutoff of 2026Q3."""
    frame = frame.copy()
    frame["leader_tier"] = [classify_leader(role, board, seniority) for role, board, seniority in frame[["rolename", "brdposition", "seniority"]].itertuples(index=False, name=None)]
    frame = frame[frame.leader_tier.ne("excluded") & frame.rolename.ne("")].copy()
    frame["role_priority"] = frame.leader_tier.map(TIER_PRIORITY).astype(int)
    starts = np.maximum(frame.start_year if frequency == "year" else frame.start_period, SAMPLE_START if frequency == "year" else SAMPLE_START * 4)
    ends = np.minimum(frame.end_year if frequency == "year" else frame.end_period, CUTOFF_YEAR if frequency == "year" else CUTOFF_PERIOD)
    lengths = np.maximum(ends - starts + 1, 0).astype(int).to_numpy()
    repeated = frame.loc[frame.index.repeat(lengths)].copy().reset_index(drop=True)
    offsets = np.arange(lengths.sum()) - np.repeat(np.cumsum(lengths) - lengths, lengths)
    periods = np.repeat(starts.to_numpy(), lengths) + offsets
    repeated["year"] = periods if frequency == "year" else periods // 4
    if frequency == "quarter":
        repeated["quarter"] = periods % 4 + 1
        repeated["yearquarter"] = repeated.year.astype(str) + "q" + repeated.quarter.astype(str)
    else:
        repeated["quarter"], repeated["yearquarter"] = np.nan, ""
    repeated["role_missing"] = 0
    times = ["year"] if frequency == "year" else ["year", "quarter"]
    repeated = repeated.sort_values(["DirectorID", "company_key"] + times + ["role_priority", "role_missing", "start_year"],
                                    ascending=[True, True] + [True] * len(times) + [False, True, False], kind="stable")
    return repeated.drop_duplicates(["DirectorID", "company_key"] + times)


# The only T2 intervention is the observed BoardEx Pharma board baseline.
def time_key(frequency: str) -> list[str]:
    """Return the unique membership key at the requested frequency."""
    return ["DirectorID", "company_key", "year"] + (["quarter"] if frequency == "quarter" else [])


def finalize(candidates: pd.DataFrame, bp, display, info, frequency: str) -> pd.DataFrame:
    """Use the unchanged time-level selection and board continuity calculation."""
    chosen = combine_annual_candidates(candidates, frequency)
    trace_columns = TRACE + (["baseline_present"] if "baseline_present" in chosen else [])
    trace = chosen[time_key(frequency) + trace_columns].copy()
    roster = build_final_roster(bp, chosen, display, info, frequency)
    for column in ["start_year", "end_year", "start_period", "end_period"]:
        if column in roster:
            roster[column] = pd.to_numeric(roster[column], errors="coerce")
    return roster.merge(trace, on=time_key(frequency), how="left", validate="one_to_one")


def append_baseline(current: pd.DataFrame, bp, display, info, frequency: str) -> tuple[pd.DataFrame, int]:
    """Append observed director-year membership without changing spell profiles."""
    board = build_board_candidates(bp, current, display, info, frequency)
    board = board[board.year.between(SAMPLE_START, CUTOFF_YEAR)].copy()
    if frequency == "quarter":
        board = board[(board.year * 4 + board.quarter - 1).le(CUTOFF_PERIOD)].copy()
    key = time_key(frequency)
    board = board.drop_duplicates(key)
    # Baseline role enrichment uses the current same-period record only.
    # It never attaches a first role from another year or another quarter.
    enrich = current[key + TRACE + ["RawCompanyID"]]
    board = board.merge(enrich, on=key, how="left", validate="one_to_one")
    board["RawCompanyID"] = board.RawCompanyID.fillna(board.CompanyID).astype(int)
    for column in TRACE:
        fallback = "boardex_pharma_annual_baseline" if "rules" in column else ""
        board[column] = board[column].fillna(fallback)
    for column in ["delist_year", "delist_period"]:
        board[column] = np.nan
    current = current.copy()
    baseline_keys = pd.MultiIndex.from_frame(board[key])
    current["baseline_present"] = pd.MultiIndex.from_frame(current[key]).isin(baseline_keys).astype(int)
    board["baseline_present"] = 1
    combined = pd.concat([current, board], ignore_index=True)
    roster = finalize(combined, bp, display, info, frequency)
    # Every baseline observation must be recognized in narrow membership.
    narrow = roster.leader_tier.eq("board") | roster.board_continuity.eq(1)
    assert baseline_keys.isin(pd.MultiIndex.from_frame(roster.loc[narrow, key])).all()
    return roster, len(board)


def write_audit(frame: pd.DataFrame, name: str) -> None:
    """Write an English-named derived audit in the selected output directory."""
    frame.to_csv(OUTPUT_DIR / name, index=False, encoding="utf-8-sig")


def source_fingerprint(path: Path, include_hash: bool = False) -> dict:
    """Record source identity without repeatedly hashing multi-gigabyte CSVs."""
    stat = path.stat()
    record = {"path": str(path.resolve()), "bytes": stat.st_size,
              "mtime_ns": stat.st_mtime_ns}
    if include_hash:
        record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return record


def scan_sources(paths, names, sources, ids) -> tuple[pd.DataFrame, list[dict]]:
    """Scan IE and OC once, retaining original board fields and source order."""
    matched, audit = [], []
    for label, path, chunk_size in paths:
        started = time.perf_counter()
        columns = ["companyid", "companyname", "directorid", "directorname",
                   "rolename", "datestartrole", "dateendrole",
                   "datestartroleflag", "dateendroleflag",
                   "brdposition" if label == "IE" else "seniority"]
        parts, offset, conflict_count = [], 0, 0
        print(f"Scanning original {label}: {path}", flush=True)
        for chunk in pd.read_csv(path, usecols=columns, chunksize=chunk_size,
                                 low_memory=False):
            resolved = resolve_company_chunk(chunk, names, sources, ids)
            conflict_count += int(resolved.mapping_conflict.sum())
            keep = resolved.company_key.notna() & resolved.mapping_conflict.eq(0)
            if keep.any():
                selected = chunk.loc[keep].copy()
                selected["input_row_index"] = np.arange(offset, offset + len(chunk))[keep.to_numpy()]
                for column in resolved:
                    selected[column] = resolved.loc[keep, column]
                selected["source_dataset"] = label
                parts.append(selected)
            offset += len(chunk)
        if not parts:
            raise ValueError(f"No SSR matches were found in {label}")
        frame = pd.concat(parts, ignore_index=True)
        frame.to_parquet(OUTPUT_DIR / f"{label.lower()}_matched_raw_flags.parquet", index=False)
        matched.append(frame)
        audit.append({"source": label, "scanned_rows": offset,
                      "matched_rows": len(frame), "id_name_conflicts": conflict_count,
                      "invalid_person_ids": int(pd.to_numeric(frame.directorid, errors="coerce").isna().sum()),
                      "seconds": round(time.perf_counter() - started, 2)})
        print(audit[-1], flush=True)
    write_audit(pd.DataFrame(audit), "raw_scan_audit.csv")
    return pd.concat(matched, ignore_index=True), audit


def current_roster(candidates, bp, display, info, frequency):
    """Recreate the current IE/OC roster before the sole T2 intervention."""
    key = time_key(frequency)
    trace = candidates[key + TRACE]
    roster = build_final_roster(bp, candidates, display, info, frequency)
    roster = roster.merge(trace, on=key, how="left", validate="one_to_one")
    # Historical T2 read the current roster through CSV. Reproduce that
    # serialization in memory, including empty strings and numeric inference,
    # without relying on any pre-existing roster or writing a second large DTA.
    output = roster.copy()
    for column in output.select_dtypes(include="object").columns:
        output[column] = output[column].fillna("").astype(str)
    return pd.read_csv(io.StringIO(output.to_csv(index=False)), low_memory=False)


def compare_saved_roster(stem: str, reference: Path) -> dict:
    """Verify every output field and row against an existing T2 CSV and DTA."""
    actual = pd.read_csv(OUTPUT_DIR / f"{stem}.csv", low_memory=False)
    expected = pd.read_csv(reference / f"{stem}.csv", low_memory=False)
    pd.testing.assert_frame_equal(actual, expected, check_dtype=False,
                                  check_exact=True, check_like=False)
    stata = pd.read_stata(OUTPUT_DIR / f"{stem}.dta", convert_categoricals=False)
    normalized_csv = actual.copy()
    for column in stata.select_dtypes(include="object").columns:
        normalized_csv[column] = normalized_csv[column].fillna("")
    pd.testing.assert_frame_equal(normalized_csv, stata, check_dtype=False,
                                  check_exact=True, check_like=False)
    return {"variant": stem.removeprefix("ssr_company_roster_"),
            "rows": len(actual), "columns": len(actual.columns),
            "reference_csv_all_fields_equal": True,
            "generated_csv_dta_all_fields_equal": True}


def build(args) -> None:
    """Run the complete original-data-to-T2 pipeline with auditable outputs."""
    started = time.perf_counter()
    inputs = {
        "pharma": args.interim_dir / "boardex_pharma.dta",
        "within": args.interim_dir / "cro_bname_boardex_within.dta",
        "crosswalk": args.interim_dir / "cro_bname_boardex_citeline.dta",
        "ie": args.raw_na_dir / "individual_employment.csv",
        "oc": args.raw_na_dir / "organization_composition.csv",
    }
    fingerprints = {name: source_fingerprint(path, name not in {"ie", "oc"})
                    for name, path in inputs.items()}
    bp = pd.read_stata(inputs["pharma"])
    canonical, display, ids, info = build_company_info(bp)
    names, sources, ambiguous = build_name_mapping(
        canonical, pd.read_stata(inputs["within"]), pd.read_stata(inputs["crosswalk"]))
    # Ambiguous aliases are already excluded by build_name_mapping.
    write_audit(pd.DataFrame([{"alias": alias, "company_key": target,
                              "mapping_source": sources[alias]}
                             for alias, target in sorted(names.items())]), "initial_name_mapping.csv")
    write_audit(pd.DataFrame([{"CompanyID": company_id, "company_key": key}
                             for company_id, key in sorted(ids.items())]), "current_company_ids.csv")
    write_audit(pd.DataFrame([{"alias": alias, "targets": "|".join(targets)}
                             for alias, targets in sorted(ambiguous.items())],
                            columns=["alias", "targets"]), "ambiguous_initial_names.csv")
    raw, scan_audit = scan_sources([
        ("IE", inputs["ie"], IE_CHUNK_SIZE), ("OC", inputs["oc"], OC_CHUNK_SIZE)],
        names, sources, ids)
    for column in ["datestartroleflag", "dateendroleflag"]:
        flags = pd.to_numeric(raw[column], errors="coerce")
        unexpected = sorted(set(flags.dropna()) - KNOWN_FLAGS)
        if unexpected:
            raise ValueError(f"Unexpected {column}: {unexpected}")
    bounds = company_bounds(raw, display, info)
    write_audit(bounds, "company_flag_date_bounds.csv")
    raw["DirectorName"] = clean_display_series(raw.directorname)
    raw["rolename"] = clean_display_series(raw.rolename)
    raw["role_key"] = normalize_series(raw.rolename)
    raw["raw_companyname"] = clean_display_series(raw.companyname)
    for column in ["brdposition", "seniority"]:
        raw[column] = clean_display_series(raw[column])
    raw["source_ie"] = raw.source_dataset.eq("IE").astype(int)
    raw["source_oc"] = raw.source_dataset.eq("OC").astype(int)
    audit, verification = [], []
    for mode in ["imputed", "drop"]:
        resolved = resolve_dates(raw, bounds, mode)
        resolved.to_parquet(OUTPUT_DIR / f"{mode}_date_rule_records.parquet", index=False)
        selected = resolved[resolved.date_rule_keep.eq(1)].copy()
        selected["DirectorID"] = pd.to_numeric(selected.directorid).astype(int)
        selected["RawCompanyID"] = pd.to_numeric(selected.RawCompanyID).fillna(-1).astype(int)
        for column in ["start_year", "end_year", "start_period", "end_period"]:
            selected[column] = selected[column].astype(int)
        assert selected.loc[selected.dateendroleflag.eq(40), "end_period"].eq(CUTOFF_PERIOD).all()
        if mode == "drop":
            assert not selected.datestartroleflag.isin(UNKNOWN_FLAGS | YEAR_ONLY_FLAGS).any()
            assert not selected.dateendroleflag.isin(UNKNOWN_FLAGS | YEAR_ONLY_FLAGS).any()
        for frequency, suffix in [("year", "year"), ("quarter", "yearquarter")]:
            variant = f"{'imputed' if mode == 'imputed' else 'drop_sentinel'}_{suffix}"
            print(f"Building T2 {variant}", flush=True)
            frequency_rows = selected if frequency == "year" else selected[selected.quarter_interval_valid.eq(1)]
            spells = consolidate(frequency_rows, frequency)
            current = current_roster(expand(spells, frequency), bp, display, info, frequency)
            roster, baseline_rows = append_baseline(current, bp, display, info, frequency)
            validate_roster(roster, variant, len(canonical), frequency)
            assert roster.company_key.isin(canonical).all()
            key = time_key(frequency)
            assert pd.MultiIndex.from_frame(current[key]).isin(pd.MultiIndex.from_frame(roster[key])).all()
            narrow = roster.leader_tier.eq("board") | roster.board_continuity.eq(1)
            current_narrow = current.leader_tier.eq("board") | current.board_continuity.eq(1)
            assert pd.MultiIndex.from_frame(current.loc[current_narrow, key]).isin(
                pd.MultiIndex.from_frame(roster.loc[narrow, key])).all()
            assert roster.baseline_present.sum() == baseline_rows
            if frequency == "quarter":
                assert (roster.year * 4 + roster.quarter - 1).le(CUTOFF_PERIOD).all()
            stem = f"ssr_company_roster_{variant}"
            atomic_write_outputs(roster, stem)
            record = {"variant": variant, "rows": len(roster), "narrow_rows": int(narrow.sum()),
                      "companies": int(roster.CompanyID.nunique()),
                      "directors": int(roster.DirectorID.nunique()), "baseline_rows": baseline_rows,
                      "current_ie_oc_rows": len(current), "added_membership_rows": len(roster) - len(current),
                      "added_narrow_rows": int(narrow.sum() - current_narrow.sum()),
                      "kept_raw_rows": len(frequency_rows)}
            audit.append(record)
            print(record, flush=True)
            if args.compare_rosters:
                verification.append(compare_saved_roster(stem, args.compare_rosters))
                print(f"Full-column CSV comparison and CSV/DTA validation passed: {variant}", flush=True)
    # Ensure the raw inputs were not replaced or changed during the build.
    for name, path in inputs.items():
        assert source_fingerprint(path, name not in {"ie", "oc"}) == fingerprints[name]
    write_audit(pd.DataFrame(audit), "t2_roster_build_audit.csv")
    if verification:
        write_audit(pd.DataFrame(verification), "t2_reference_verification.csv")
    manifest = {"experiment": "T2", "cutoff": "2026q3",
                "initial_names": len(names), "current_company_ids": len(ids),
                "historical_id_or_alias_discovery": False,
                "baseline_policy": "Observed director-year SSR membership; all four quarters per baseline year",
                "date_policy": {"75_80_imputed": "company earliest/latest valid endpoint quarter; flag40 company ends at cutoff",
                                "75_80_drop": "exclude raw row", "start_30_imputed": "known year Q1",
                                "end_25_imputed": "known year Q4", "25_30_drop": "exclude raw row",
                                "end_40": "2026Q3 in both modes", "delist_override": False},
                "role_policy": "Independent same-role spans; IE Yes/Inside/Outside, OC board seniority and role keywords",
                "narrow_policy": "board role or forward board continuity within continuous person-company membership",
                "source_files": fingerprints, "raw_scan": scan_audit, "rosters": audit,
                "verification": verification,
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "runtime_seconds": round(time.perf_counter() - started, 2)}
    (OUTPUT_DIR / "t2_build_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"All four T2 rosters complete in {manifest['runtime_seconds']} seconds: {OUTPUT_DIR}", flush=True)


def main() -> None:
    """Parse explicit input/output locations; build only the dedicated T2 version."""
    global OUTPUT_DIR
    parser = argparse.ArgumentParser(description="Build standalone T2 SSR rosters from original inputs.")
    parser.add_argument("--interim-dir", type=Path, default=INTERIM_DIR)
    parser.add_argument("--raw-na-dir", type=Path, default=RAW_NA_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--compare-rosters", type=Path, default=None,
                        help="Optional existing T2 folder for exact all-column CSV comparison.")
    args = parser.parse_args()
    OUTPUT_DIR = args.output_dir.resolve()
    protected = [Path(r"F:/BoardPharma/data"),
                 Path(r"D:/pharma/flag_dates_20261007/rosters"),
                 Path(r"D:/pharma/rb26/t1/rosters"), Path(r"D:/pharma/rb26/t2/rosters"),
                 Path(r"D:/pharma/rb26/t3/rosters"), args.interim_dir, args.raw_na_dir]
    if OUTPUT_DIR in [path.resolve() for path in protected]:
        parser.error("Choose a separate output directory to preserve original data and previous results.")
    if args.compare_rosters:
        args.compare_rosters = args.compare_rosters.resolve()
        if OUTPUT_DIR == args.compare_rosters:
            parser.error("The output and comparison directories must differ.")
        for variant in ["imputed_year", "imputed_yearquarter", "drop_sentinel_year", "drop_sentinel_yearquarter"]:
            if not (args.compare_rosters / f"ssr_company_roster_{variant}.csv").is_file():
                parser.error(f"Missing reference CSV for {variant}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    build(args)


if __name__ == "__main__":
    main()
