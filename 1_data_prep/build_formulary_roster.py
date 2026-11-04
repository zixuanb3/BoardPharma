"""
Purpose:
    Build two quarterly formulary board rosters from raw BoardEx BP, IE, and OC
    data. Match companies only through the standardized formulary mapping and
    use its integer ``id`` as the company identity throughout the pipeline.

Process:
    1. Normalize mapping and source company names by trimming outer whitespace,
       collapsing internal whitespace, and converting to uppercase. Match BP on
       BoardName and IE/OC on CompanyName; do not use BoardEx CompanyID.
    2. Apply the SSR date-flag rules to matched IE/OC rows. Build an imputed
       variant from id-level date bounds and a drop-sentinel variant that drops
       start/end flags 75, 80, 25, and 30.
    3. Consolidate overlapping or adjacent same-role intervals, classify
       leadership, expand to 1990Q1-2026Q3, and select one role per
       DirectorID-id-quarter.
    4. Add mapped BP director-year observations as up to four quarterly board
       records, recalculate board continuity, retain board or forward-continuity
       rows, validate the unique membership key, and write the two roster files.

Input:
    crosswalks/labeler_company_mapping_standardized_with_id.csv
    data/formulary/formulary_panel_with_company_id.csv
    InterimData/boardex_pharma.dta
    D:/individual_employment.csv
    D:/organization_composition.csv
    Command-line options can override the mapping, BP, raw, and output paths.

Output:
    data/formulary_roster/formulary_company_roster_imputed_yearquarter.csv
    data/formulary_roster/formulary_company_roster_drop_sentinel_yearquarter.csv
    Columns, in order: DirectorID, id, Year, Quarter, leader_tier,
    role_priority, board_continuity, source.
"""

from __future__ import annotations

import argparse
import io
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAPPING_PATH = (
    PROJECT_ROOT / "crosswalks" / "labeler_company_mapping_standardized_with_id.csv"
)
FORMULARY_PANEL_PATH = (
    PROJECT_ROOT / "data" / "formulary" / "formulary_panel_with_company_id.csv"
)
BP_PATH = PROJECT_ROOT / "InterimData" / "boardex_pharma.dta"
RAW_NA_DIR = Path("D:/")
OUTPUT_DIR = PROJECT_ROOT / "data" / "formulary_roster"

SAMPLE_START = 1990
CUTOFF_YEAR = 2026
CUTOFF_PERIOD = CUTOFF_YEAR * 4 + 2  # 2026Q3 on the zero-based quarter index.
IE_CHUNK_SIZE = 200_000
OC_CHUNK_SIZE = 300_000

UNKNOWN_FLAGS = {75, 80}
YEAR_ONLY_FLAGS = {25, 30}
KNOWN_FLAGS = {10, 15, 20, 25, 30, 40, 75, 80}
TIER_PRIORITY = {"excluded": 0, "vp_tech_hr": 1, "csuite": 2, "board": 3}
OUTPUT_COLUMNS = [
    "DirectorID",
    "id",
    "Year",
    "Quarter",
    "leader_tier",
    "role_priority",
    "board_continuity",
    "source",
]

NameToId = Mapping[str, int]


def normalize_series(series: pd.Series) -> pd.Series:
    """Normalize a company-name series for deterministic exact matching."""
    return (
        series.fillna("")
        .astype(str)
        .str.strip()
        .str.upper()
        .str.replace(r"\s+", " ", regex=True)
    )


def clean_text(series: pd.Series) -> pd.Series:
    """Trim display text while preserving its original case."""
    return series.fillna("").astype(str).str.strip()


def parse_year_series(series: pd.Series) -> pd.Series:
    """Keep a valid leading four-digit year from a raw date field."""
    raw = series.fillna("").astype(str).str.strip()
    year = pd.to_numeric(raw.str[:4], errors="coerce")
    return year.where(year.between(1800, 2200)).astype("Int64")


def parse_period_series(series: pd.Series) -> pd.Series:
    """Convert a date to the zero-based quarter index year * 4 + quarter - 1."""
    raw = series.fillna("").astype(str).str.strip()
    year = pd.to_numeric(raw.str[:4], errors="coerce")
    month = pd.to_numeric(raw.str[5:7], errors="coerce")
    month = month.where(month.between(1, 12), 1)
    period = year * 4 + (month - 1) // 3
    return period.where(year.between(1800, 2200)).astype("Int64")


def build_name_to_id(mapping: pd.DataFrame, column: str) -> dict[str, int]:
    """Build one normalized-name-to-id map and reject ambiguous names."""
    names = mapping.loc[mapping[column].notna(), [column, "id"]].copy()
    names["name_key"] = normalize_series(names[column])
    names = names.loc[names["name_key"].ne("")].drop_duplicates(
        ["name_key", "id"]
    )
    ambiguous = names.groupby("name_key", sort=False)["id"].nunique()
    ambiguous = ambiguous.loc[ambiguous.gt(1)]
    if not ambiguous.empty:
        raise ValueError(
            f"Normalized {column} values map to multiple ids; "
            f"examples: {ambiguous.index.tolist()[:10]}"
        )
    return {
        str(name_key): int(company_id)
        for name_key, company_id in names[["name_key", "id"]].itertuples(
            index=False, name=None
        )
    }


def read_mapping(path: Path) -> tuple[dict[str, int], dict[str, int], set[int]]:
    """Read the standardized mapping used for BP and IE/OC name matches."""
    mapping = pd.read_csv(path, usecols=["BoardName", "CompanyName", "id"])
    if mapping["id"].isna().any():
        raise ValueError("The formulary mapping contains missing id values.")
    numeric_ids = pd.to_numeric(mapping["id"], errors="raise")
    if not numeric_ids.eq(numeric_ids.round()).all():
        raise ValueError("The formulary mapping contains noninteger id values.")
    mapping["id"] = numeric_ids.astype("int64")
    eligible_ids = set(
        pd.to_numeric(
            pd.read_csv(FORMULARY_PANEL_PATH, usecols=["id"])["id"],
            errors="raise",
        ).astype("int64")
    )
    mapping = mapping.loc[mapping["id"].isin(eligible_ids)].copy()
    return (
        build_name_to_id(mapping, "BoardName"),
        build_name_to_id(mapping, "CompanyName"),
        set(mapping["id"].tolist()),
    )


def choose_board_position(values: Iterable[str]) -> str:
    """Choose the strongest available IE board-position value."""
    cleaned = [
        str(value).strip()
        for value in values
        if pd.notna(value) and str(value).strip()
    ]
    if not cleaned:
        return ""
    upper_values = {value.upper(): value for value in cleaned}
    for preferred in ("YES", "INSIDE", "OUTSIDE", "NO"):
        if preferred in upper_values:
            return upper_values[preferred]
    return cleaned[0]


def join_source_values(values: Iterable[str]) -> str:
    """Join source labels without duplicate or missing-value tokens."""
    tokens: set[str] = set()
    for value in values:
        for token in str(value).split("+"):
            token = token.strip()
            if token and token.lower() not in {"nan", "none"}:
                tokens.add(token)
    return "+".join(sorted(tokens))


def classify_leader(rolename: str, brdposition: str, seniority: str) -> str:
    """Classify one consolidated employment spell using the SSR tier rules."""
    role_up = str(rolename).strip().upper()
    brd_up = str(brdposition).strip().upper()
    seniority_up = str(seniority).strip().upper()
    if brd_up in {"YES", "INSIDE", "OUTSIDE"}:
        return "board"
    board_keywords = [
        "DIRECTOR - SD",
        "SUPERVISORY DIRECTOR",
        "INDEPENDENT DIRECTOR",
        "EXECUTIVE DIRECTOR",
        "CHAIRMAN",
        "CHAIRWOMAN",
        "VICE CHAIR",
        "NON-EXECUTIVE DIRECTOR",
        "LEAD INDEPENDENT DIRECTOR",
        "BOARD MEMBER",
        "INDEPENDENT NED",
    ]
    if seniority_up in {"SUPERVISORY DIRECTOR", "EXECUTIVE DIRECTOR"}:
        return "board"
    if any(keyword in role_up for keyword in board_keywords):
        return "board"

    csuite_patterns = [
        "CHIEF EXECUTIVE",
        "CEO",
        "CHIEF FINANCIAL",
        "CFO",
        "CHIEF OPERAT",
        "COO",
        "CHIEF TECHNOLOGY",
        "CTO",
        "CHIEF INFORMATION",
        "CIO",
        "CHIEF MARKETING",
        "CMO",
        "CHIEF SCIENTIFIC",
        "CSO",
        "CHIEF HUMAN RESOURCES",
        "CHRO",
        "CHIEF LEGAL",
        "CHIEF COMPLIANCE",
        "CHIEF MEDICAL",
        "CHIEF STRATEGY",
        "CHIEF BUSINESS",
        "CHIEF COMMERCIAL",
        "CHIEF DATA",
        "CHIEF REVENUE",
        "CHIEF DIGITAL",
        "CHIEF ACCOUNTING",
        "CHIEF RISK",
        "CHIEF INVESTMENT",
        "GENERAL COUNSEL",
    ]
    for pattern in csuite_patterns:
        if (
            pattern in role_up
            and "ASSISTANT" not in role_up
            and "DEPUTY" not in role_up
        ):
            return "csuite"
    if (
        "PRESIDENT" in role_up
        and "VICE PRESIDENT" not in role_up
        and "ASSISTANT" not in role_up
    ):
        return "csuite"

    vp_patterns = [
        "SENIOR VP",
        "SVP",
        "EXECUTIVE VP",
        "EVP",
        "GROUP VP",
        "CORPORATE VP",
    ]
    if not any(pattern in role_up for pattern in vp_patterns) and (
        "VICE PRESIDENT" not in role_up
    ):
        return "excluded"
    technical_keywords = [
        "TECHNOLOGY",
        "TECHNICAL",
        "ENGINEER",
        "R&D",
        "RESEARCH",
        "DEVELOPMENT",
        "INFORMATION",
        "DATA",
        "DIGITAL",
        "HUMAN RESOURCES",
        "PERSONNEL",
        "PEOPLE",
        "TALENT",
        "HR",
        "SCIENTIF",
        "LAB",
        "CLINICAL",
        "MEDICAL",
        "REGULATORY",
        "QUALITY",
        "MANUFACTURING",
        "OPERATION",
        "INNOVATION",
        "INTELLECTUAL PROPERTY",
    ]
    return (
        "vp_tech_hr"
        if any(keyword in role_up for keyword in technical_keywords)
        else "excluded"
    )


def endpoint_period(frame: pd.DataFrame, side: str) -> pd.Series:
    """Apply the SSR endpoint rules and return a zero-based quarter index."""
    date = frame[f"date{side}role"]
    flag = pd.to_numeric(frame[f"date{side}roleflag"], errors="coerce")
    year = parse_year_series(date)
    period = parse_period_series(date)
    period = period.where(~flag.isin(YEAR_ONLY_FLAGS), year * 4)
    if side == "end":
        period = period.where(~flag.eq(25), year * 4 + 3)
        period = period.where(~flag.eq(40), CUTOFF_PERIOD)
    valid = flag.isin(KNOWN_FLAGS) & ~flag.isin(UNKNOWN_FLAGS)
    upper = CUTOFF_YEAR * 4 + 3 if side == "end" else CUTOFF_PERIOD
    valid &= period.notna() & period.between(1800 * 4, upper)
    return period.where(valid).astype("Int64")


def scan_sources(
    paths: Sequence[tuple[str, Path, int]], company_names: NameToId
) -> pd.DataFrame:
    """Read raw IE/OC chunks and keep exact normalized CompanyName matches."""
    matched_sources: list[pd.DataFrame] = []
    for label, path, chunk_size in paths:
        columns = [
            "companyname",
            "directorid",
            "rolename",
            "datestartrole",
            "dateendrole",
            "datestartroleflag",
            "dateendroleflag",
            "brdposition" if label == "IE" else "seniority",
        ]
        parts: list[pd.DataFrame] = []
        print(f"Reading {label}: {path}", flush=True)
        for chunk in pd.read_csv(
            path, usecols=columns, chunksize=chunk_size, low_memory=False
        ):
            matched_ids = normalize_series(chunk["companyname"]).map(company_names)
            keep = matched_ids.notna()
            if not keep.any():
                continue
            selected = chunk.loc[keep].copy()
            selected["id"] = matched_ids.loc[keep].astype("int64")
            selected["source_dataset"] = label
            parts.append(selected)
        if not parts:
            raise ValueError(f"No formulary CompanyName matches were found in {label}.")
        source_frame = pd.concat(parts, ignore_index=True)
        matched_sources.append(source_frame)
        print(f"Matched {label} rows: {len(source_frame):,}", flush=True)
    return pd.concat(matched_sources, ignore_index=True)


def company_bounds(raw: pd.DataFrame) -> pd.DataFrame:
    """Calculate id-level endpoint bounds from all matched IE/OC records."""
    endpoints = pd.concat(
        [
            pd.DataFrame({"id": raw["id"], "period": endpoint_period(raw, side)})
            for side in ("start", "end")
        ],
        ignore_index=True,
    ).dropna(subset=["period"])
    observed = endpoints.groupby("id")["period"].agg(
        earliest_period="min", latest_observed_period="max"
    )
    current = (
        raw.assign(
            _current=pd.to_numeric(raw["dateendroleflag"], errors="coerce").eq(40)
        )
        .groupby("id")["_current"]
        .any()
    )
    bounds = (
        pd.DataFrame({"id": sorted(raw["id"].unique())})
        .set_index("id")
        .join(observed)
    )
    bounds["has_current_flag40"] = (
        bounds.index.to_series().map(current).fillna(False).astype("int8")
    )
    bounds["latest_period"] = bounds["latest_observed_period"].where(
        bounds["has_current_flag40"].eq(0), CUTOFF_PERIOD
    )
    for column in ("earliest_period", "latest_observed_period", "latest_period"):
        bounds[column] = bounds[column].astype("Int64")
    return bounds.reset_index()


def resolve_dates(raw: pd.DataFrame, bounds: pd.DataFrame, mode: str) -> pd.DataFrame:
    """Apply row-level SSR date rules before same-role consolidation."""
    frame = raw.copy()
    start_flag = pd.to_numeric(
        frame["datestartroleflag"], errors="coerce"
    ).astype("Int64")
    end_flag = pd.to_numeric(frame["dateendroleflag"], errors="coerce").astype(
        "Int64"
    )
    frame["datestartroleflag"] = start_flag
    frame["dateendroleflag"] = end_flag
    start = endpoint_period(frame, "start")
    end = endpoint_period(frame, "end")
    start_unknown = start_flag.isin(UNKNOWN_FLAGS)
    end_unknown = end_flag.isin(UNKNOWN_FLAGS)
    start_year_only = start_flag.isin(YEAR_ONLY_FLAGS)
    end_year_only = end_flag.isin(YEAR_ONLY_FLAGS)
    indexed_bounds = bounds.set_index("id")

    if mode == "imputed":
        start = start.where(
            ~start_unknown, frame["id"].map(indexed_bounds["earliest_period"])
        )
        end = end.where(
            ~end_unknown, frame["id"].map(indexed_bounds["latest_period"])
        )
        flag_keep = pd.Series(True, index=frame.index)
    elif mode == "drop":
        flag_keep = ~(
            start_unknown | end_unknown | start_year_only | end_year_only
        )
    else:
        raise ValueError(f"Unknown date mode: {mode}")

    frame["start_date_rule"] = np.select(
        [
            start_unknown.to_numpy(dtype=bool, na_value=False),
            start_year_only.to_numpy(dtype=bool, na_value=False),
        ],
        ["company_earliest", "year_only_q1"],
        default="observed_quarter",
    )
    frame["start_period"] = start
    frame["end_period"] = end
    frame["start_year"] = start // 4
    frame["end_year"] = end // 4

    person_valid = pd.to_numeric(frame["directorid"], errors="coerce").notna()
    dates_valid = start.notna() & end.notna() & (start // 4).le(end // 4).fillna(
        False
    )
    frame["quarter_interval_valid"] = start.le(end).fillna(False).astype("int8")
    frame["date_rule_keep"] = (flag_keep & dates_valid & person_valid).astype("int8")
    return frame


def consolidate(frame: pd.DataFrame) -> pd.DataFrame:
    """Merge overlapping or adjacent same-role intervals within DirectorID-id."""
    frame = frame.loc[frame["role_key"].ne("")].copy()
    group_columns = ["DirectorID", "id", "role_key"]
    frame = frame.sort_values(
        group_columns + ["start_period", "end_period", "source_ie"],
        kind="stable",
    ).reset_index(drop=True)
    running_end = frame.groupby(group_columns, sort=False)["end_period"].cummax()
    previous_end = running_end.groupby(
        [frame[column] for column in group_columns], sort=False
    ).shift()
    new_cluster = previous_end.isna() | frame["start_period"].gt(previous_end + 1)
    frame["_cluster"] = new_cluster.groupby(
        [frame[column] for column in group_columns], sort=False
    ).cumsum()

    for column in ("rolename", "seniority", "brdposition"):
        frame[column] = (
            frame[column]
            .fillna("")
            .astype(str)
            .str.strip()
            .replace({"": pd.NA, "nan": pd.NA, "None": pd.NA})
        )
    frame["_observed_start"] = frame["start_period"].where(
        frame["start_date_rule"].ne("company_earliest")
    )
    frame["_oc_seniority"] = frame["seniority"].where(frame["source_oc"].eq(1))
    summary = frame.groupby(
        group_columns + ["_cluster"], sort=False, as_index=False
    ).agg(
        rolename=("rolename", "first"),
        brdposition=("brdposition", choose_board_position),
        seniority=("_oc_seniority", "first"),
        all_start=("start_period", "min"),
        observed_start=("_observed_start", "min"),
        all_end=("end_period", "max"),
        source_ie=("source_ie", "max"),
        source_oc=("source_oc", "max"),
    )
    summary["start_period"] = summary["observed_start"].fillna(
        summary["all_start"]
    ).astype(int)
    summary["end_period"] = summary["all_end"].astype(int)
    summary["start_year"] = summary["start_period"] // 4
    summary["end_year"] = summary["end_period"] // 4
    summary["source"] = np.select(
        [
            summary["source_ie"].eq(1) & summary["source_oc"].eq(1),
            summary["source_ie"].eq(1),
        ],
        ["ie_oc", "individual_employment"],
        default="organization_composition",
    )
    return summary.drop(
        columns=["_cluster", "all_start", "observed_start", "all_end"]
    )


def time_key() -> list[str]:
    """Return the unique quarterly formulary membership key."""
    return ["DirectorID", "id", "Year", "Quarter"]


def expand_spells(frame: pd.DataFrame) -> pd.DataFrame:
    """Classify and expand consolidated spells over 1990Q1-2026Q3."""
    frame = frame.copy()
    frame["leader_tier"] = [
        classify_leader(role, board, seniority)
        for role, board, seniority in frame[
            ["rolename", "brdposition", "seniority"]
        ].itertuples(index=False, name=None)
    ]
    frame = frame.loc[
        frame["leader_tier"].ne("excluded") & frame["rolename"].ne("")
    ].copy()
    frame["role_priority"] = frame["leader_tier"].map(TIER_PRIORITY).astype(int)
    starts = np.maximum(frame["start_period"], SAMPLE_START * 4)
    ends = np.minimum(frame["end_period"], CUTOFF_PERIOD)
    lengths = np.maximum(ends - starts + 1, 0).astype(int).to_numpy()
    repeated = frame.loc[frame.index.repeat(lengths)].copy().reset_index(drop=True)
    offsets = np.arange(lengths.sum()) - np.repeat(
        np.cumsum(lengths) - lengths, lengths
    )
    periods = np.repeat(starts.to_numpy(), lengths) + offsets
    repeated["Year"] = periods // 4
    repeated["Quarter"] = periods % 4 + 1
    repeated["role_missing"] = 0
    repeated = repeated.sort_values(
        time_key() + ["role_priority", "role_missing", "start_year"],
        ascending=[True] * len(time_key()) + [False, True, False],
        kind="stable",
    )
    return repeated.drop_duplicates(time_key(), keep="first")


def combine_quarter_candidates(candidates: pd.DataFrame) -> pd.DataFrame:
    """Select one highest-priority role and combine sources for each membership."""
    key = time_key()
    candidates = candidates.copy()
    candidates["role_missing"] = (
        candidates["rolename"].fillna("").eq("").astype("int8")
    )
    candidates = candidates.sort_values(
        key + ["role_priority", "role_missing", "start_year"],
        ascending=[True] * len(key) + [False, True, False],
        kind="stable",
    )
    provenance = (
        candidates.groupby(key, sort=False)
        .agg(
            all_sources=("source", join_source_values),
            source_ie=("source_ie", "max"),
            source_oc=("source_oc", "max"),
        )
        .reset_index()
    )
    chosen = candidates.drop_duplicates(key, keep="first").drop(
        columns=["source", "source_ie", "source_oc"]
    )
    chosen = chosen.merge(provenance, on=key, how="left", validate="one_to_one")
    return chosen.rename(columns={"all_sources": "source"})


def add_board_continuity_flag(roster: pd.DataFrame) -> pd.DataFrame:
    """Carry board status forward within each continuous DirectorID-id spell."""
    if roster.empty:
        roster["board_continuity"] = pd.Series(dtype="int8")
        return roster
    frame = roster.copy()
    frame["_continuity_time"] = (
        frame["Year"].astype(int) * 4 + frame["Quarter"].astype(int) - 1
    )
    group_columns = ["DirectorID", "id"]
    frame = frame.sort_values(group_columns + ["_continuity_time"], kind="stable")
    gap = (
        frame.groupby(group_columns, sort=False)["_continuity_time"]
        .diff()
        .fillna(1)
        .gt(1)
    )
    frame["_employment_spell"] = gap.groupby(
        [frame[column] for column in group_columns], sort=False
    ).cumsum()
    frame["_is_board_role"] = frame["leader_tier"].eq("board")
    frame["board_continuity"] = (
        frame.groupby(group_columns + ["_employment_spell"], sort=False)[
            "_is_board_role"
        ]
        .cummax()
        .astype("int8")
    )
    return frame.drop(
        columns=["_continuity_time", "_employment_spell", "_is_board_role"]
    )


def finalize_candidates(candidates: pd.DataFrame) -> pd.DataFrame:
    """Select quarterly roles and recalculate id-level board continuity."""
    return add_board_continuity_flag(combine_quarter_candidates(candidates))


def build_bp_candidates(
    bp: pd.DataFrame, board_names: NameToId
) -> pd.DataFrame:
    """Map BP BoardName rows to id and expand observed director-years to quarters."""
    board = bp.copy()
    board["id"] = normalize_series(board["BoardName"]).map(board_names)
    board = board.loc[board["id"].notna()].copy()
    if board.empty:
        raise ValueError("No BP BoardName values matched the formulary mapping.")
    for column in ("DirectorID", "Year"):
        numeric = pd.to_numeric(board[column], errors="coerce")
        if numeric.isna().any() or not numeric.eq(numeric.round()).all():
            raise ValueError(f"Mapped BP rows contain invalid {column} values.")
        board[column] = numeric.astype("int64")
    board["id"] = board["id"].astype("int64")
    board = board.loc[board["Year"].between(SAMPLE_START, CUTOFF_YEAR)].copy()
    if board.empty:
        raise ValueError("Mapped BP rows do not overlap the 1990Q1-2026Q3 timeline.")
    board = board.loc[board.index.repeat(4)].copy()
    board["Quarter"] = board.groupby(level=0).cumcount() + 1
    board = board.loc[
        (board["Year"] * 4 + board["Quarter"] - 1).le(CUTOFF_PERIOD)
    ].copy()
    board["rolename"] = ""
    board["leader_tier"] = "board"
    board["role_priority"] = TIER_PRIORITY["board"]
    board["start_year"] = pd.NA
    board["source"] = "boardex_pharma"
    board["source_ie"] = 0
    board["source_oc"] = 0
    return board.drop_duplicates(time_key(), keep="first")


def validate_roster(
    roster: pd.DataFrame, label: str, mapping_ids: set[int]
) -> None:
    """Validate identity, time bounds, and uniqueness without requiring empty ids."""
    key = time_key()
    if roster.empty:
        raise AssertionError(f"{label} produced an empty roster")
    if roster.duplicated(key).any():
        raise AssertionError(f"{label} has duplicate DirectorID-id-quarter rows")
    if not roster["Quarter"].between(1, 4).all():
        raise AssertionError(f"{label} contains invalid quarters")
    periods = roster["Year"] * 4 + roster["Quarter"] - 1
    if not periods.between(SAMPLE_START * 4, CUTOFF_PERIOD).all():
        raise AssertionError(f"{label} contains periods outside the SSR timeline")
    output_ids = set(pd.to_numeric(roster["id"], errors="raise").astype(int))
    if not output_ids.issubset(mapping_ids):
        raise AssertionError(f"{label} contains ids absent from the mapping")


def write_roster(roster: pd.DataFrame, stem: str, output_dir: Path) -> None:
    """Write current board roles and forward board-continuity memberships."""
    keep = roster["leader_tier"].eq("board") | roster["board_continuity"].eq(1)
    output = roster.loc[keep, OUTPUT_COLUMNS].copy()
    if output.duplicated(time_key()).any():
        raise AssertionError(f"{stem} contains duplicate output memberships")
    for column in output.select_dtypes(include="object").columns:
        output[column] = output[column].fillna("").astype(str)
    output = output.sort_values(
        ["id", "Year", "Quarter", "DirectorID"], kind="stable"
    )
    path = output_dir / f"{stem}.csv"
    output.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"Saved {len(output):,} board-or-continuity rows: {path}", flush=True)


def build(
    mapping_path: Path,
    bp_path: Path,
    raw_dir: Path,
    output_dir: Path,
) -> None:
    """Build the two SSR-rule formulary roster variants."""
    board_names, company_names, mapping_ids = read_mapping(mapping_path)
    bp = pd.read_stata(
        bp_path,
        columns=["BoardName", "DirectorID", "year"],
        convert_categoricals=False,
    ).rename(columns={"year": "Year"})
    raw = scan_sources(
        [
            ("IE", raw_dir / "individual_employment.csv", IE_CHUNK_SIZE),
            ("OC", raw_dir / "organization_composition.csv", OC_CHUNK_SIZE),
        ],
        company_names,
    )
    for column in ("datestartroleflag", "dateendroleflag"):
        flags = pd.to_numeric(raw[column], errors="coerce")
        unexpected = sorted(set(flags.dropna()) - KNOWN_FLAGS)
        if unexpected:
            raise ValueError(f"Unexpected {column}: {unexpected}")

    bounds = company_bounds(raw)
    raw["rolename"] = clean_text(raw["rolename"])
    raw["role_key"] = normalize_series(raw["rolename"])
    for column in ("brdposition", "seniority"):
        raw[column] = clean_text(raw[column])
    raw["source_ie"] = raw["source_dataset"].eq("IE").astype("int8")
    raw["source_oc"] = raw["source_dataset"].eq("OC").astype("int8")
    bp_candidates = build_bp_candidates(bp, board_names)

    output_dir.mkdir(parents=True, exist_ok=True)
    for mode in ("imputed", "drop"):
        variant = (
            "imputed_yearquarter"
            if mode == "imputed"
            else "drop_sentinel_yearquarter"
        )
        print(f"Building {variant}", flush=True)
        resolved = resolve_dates(raw, bounds, mode)
        selected = resolved.loc[resolved["date_rule_keep"].eq(1)].copy()
        director_ids = pd.to_numeric(
            selected["directorid"], errors="raise"
        )
        if not director_ids.eq(director_ids.round()).all():
            raise ValueError("Matched IE/OC rows contain noninteger directorid values")
        selected["DirectorID"] = director_ids.astype("int64")
        for column in ("start_year", "end_year", "start_period", "end_period"):
            selected[column] = selected[column].astype(int)
        current_end = selected.loc[
            selected["dateendroleflag"].eq(40), "end_period"
        ]
        if not current_end.eq(CUTOFF_PERIOD).all():
            raise AssertionError("Current roles must end at 2026Q3")
        if mode == "drop":
            sentinel_flags = UNKNOWN_FLAGS | YEAR_ONLY_FLAGS
            if selected["datestartroleflag"].isin(sentinel_flags).any() or selected[
                "dateendroleflag"
            ].isin(sentinel_flags).any():
                raise AssertionError("Drop-sentinel rows still contain sentinel flags")

        valid_intervals = selected.loc[
            selected["quarter_interval_valid"].eq(1)
        ].copy()
        current = finalize_candidates(expand_spells(consolidate(valid_intervals)))

        # Preserve the SSR pipeline's in-memory CSV round trip before BP is added.
        for column in current.select_dtypes(include="object").columns:
            current[column] = current[column].fillna("").astype(str)
        current = pd.read_csv(
            io.StringIO(current.to_csv(index=False)), low_memory=False
        )

        baseline_keys = pd.MultiIndex.from_frame(bp_candidates[time_key()])
        combined = pd.concat([current, bp_candidates], ignore_index=True)
        roster = finalize_candidates(combined)
        validate_roster(roster, variant, mapping_ids)
        narrow = roster["leader_tier"].eq("board") | roster[
            "board_continuity"
        ].eq(1)
        narrow_keys = pd.MultiIndex.from_frame(roster.loc[narrow, time_key()])
        if not baseline_keys.isin(narrow_keys).all():
            raise AssertionError(f"{variant} dropped mapped BP baseline memberships")
        write_roster(
            roster,
            f"formulary_company_roster_{variant}",
            output_dir,
        )


def main() -> None:
    """Resolve inputs and write the two quarterly formulary roster files."""
    parser = argparse.ArgumentParser(
        description="Build SSR-rule quarterly formulary company rosters."
    )
    parser.add_argument("--mapping", type=Path, default=MAPPING_PATH)
    parser.add_argument("--bp", type=Path, default=BP_PATH)
    parser.add_argument("--raw-na-dir", type=Path, default=RAW_NA_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()

    mapping_path = args.mapping.resolve()
    bp_path = args.bp.resolve()
    raw_dir = args.raw_na_dir.resolve()
    output_dir = args.output_dir.resolve()
    inputs = [
        mapping_path,
        FORMULARY_PANEL_PATH,
        bp_path,
        raw_dir / "individual_employment.csv",
        raw_dir / "organization_composition.csv",
    ]
    for path in inputs:
        if not path.is_file():
            parser.error(f"Missing input: {path}")
    if output_dir in {mapping_path.parent, bp_path.parent, raw_dir}:
        parser.error("The output directory must differ from all input directories.")
    build(mapping_path, bp_path, raw_dir, output_dir)


if __name__ == "__main__":
    main()
