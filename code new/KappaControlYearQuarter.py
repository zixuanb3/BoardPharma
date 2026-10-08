"""
Purpose:
    Build quarterly firm-level and pair-level Kappa for the current SSR roster
    universe and create movement-aligned common-ownership exposure controls.

Input:
    - Variant-specific SSR rosters: D:/pharma/ssr_company_roster_*.csv
    - 13F holdings: D:/Dropbox/BoardPharma/RawData/common_ownership/processed_13f_data.csv
    - CIK names: D:/Dropbox/BoardPharma/RawData/common_ownership/cikmap.csv
    - Variant-specific movement candidates under the reproduction output root
    - BoardEx employment dates: D:/Dropbox/BoardPharma/RawData/boardex/boardex_na/individual_employment.csv
      (used only as a fallback for quarterly events with no usable quarter field)
    - Curated CUSIP map: D:/pharma/compute_kappa_ssr.py (read only), unless a
      local CSV map is supplied with --cusip-map.

Output:
    - data/roster_variants/<variant>/leader_tier_<tier>/kappa_controls/*.csv
      including firm-level Kappa, pair-level Kappa, and firm-cohort exposure.

Method:
    For each 13F reporting quarter, gamma_ij is the institutional ownership
    share of firm j held by investor i. The raw Backus-Conlon-Sinkinson
    asymmetric Kappa is sum_i(gamma_ij * gamma_ik) / sum_i(gamma_ij^2).
    The reported firm-level control is the mean across other SSR firms. The
    pair-level normalized Kappa is retained to construct the total potential
    exposure to all possible counterpart firms. Annual movement uses the prior
    calendar year's quarterly average; year-quarter movement uses the mean of
    the four quarters immediately before the movement quarter. The p95
    sensitivity measures cap only the largest institution's gamma within each
    CUSIP-quarter at the global gamma p95, then renormalize gamma within that
    CUSIP-quarter before recomputing Kappa.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

import numpy as np
import pandas as pd

from pipeline_variant_config import (
    DATA_PROJECT_ROOT,
    OUTPUT_PROJECT_ROOT,
    PERSONNEL_DEFINITIONS,
    PERSONNEL_TIER_RULES,
    ROSTER_VARIANTS,
    get_variant,
)


DEFAULT_OUTPUT_ROOT = OUTPUT_PROJECT_ROOT
DEFAULT_DATA_ROOT = DATA_PROJECT_ROOT
DEFAULT_MAP_SOURCE = Path(r"D:/pharma/compute_kappa_ssr.py")


def clean_firm_key(value: object) -> str:
    """Create a stable firm key while preserving the project's alias rule."""
    if pd.isna(value):
        return ""
    text = str(value).upper().strip()
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def canonicalize_cusip(value: object) -> str:
    """Return the eight-character CUSIP base used in the ownership file."""
    if pd.isna(value):
        return ""
    text = re.sub(r"[^0-9A-Z]", "", str(value).upper().strip())
    return text[:8]


def normalize_id(value: object) -> str:
    """Normalize numeric-looking identifiers without changing string IDs."""
    if pd.isna(value):
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null", ""}:
        return ""
    return re.sub(r"\.0$", "", text)


def load_variant_roster(roster_variant: str) -> pd.DataFrame:
    """Load the minimum roster columns used to define the Kappa universe."""
    metadata = get_variant(roster_variant)
    roster_path = Path(metadata["path"])
    columns = ["BoardName", "CompanyID", "inSSR", "leader_tier"]
    roster = pd.read_csv(roster_path, usecols=columns, low_memory=False)
    roster["CompanyID"] = roster["CompanyID"].map(normalize_id)
    roster["inSSR"] = pd.to_numeric(roster["inSSR"], errors="raise").astype("int8")
    roster["leader_tier"] = (
        roster["leader_tier"].astype("string").str.strip().str.lower()
    )
    roster["BoardName"] = roster["BoardName"].astype("string").str.strip()
    return roster


def filter_roster_tier(roster: pd.DataFrame, personnel_definition: str) -> pd.DataFrame:
    """Keep SSR roster records allowed by one leader-tier definition."""
    if personnel_definition not in PERSONNEL_TIER_RULES:
        allowed = ", ".join(PERSONNEL_DEFINITIONS)
        raise ValueError(
            f"Unknown personnel_definition={personnel_definition}; expected one of: {allowed}"
        )
    return roster.loc[
        roster["inSSR"].eq(1)
        & roster["leader_tier"].isin(PERSONNEL_TIER_RULES[personnel_definition])
    ].copy()


def parse_date_series(series: pd.Series) -> pd.Series:
    """Parse ISO and YYYYMMDD dates into pandas timestamps."""
    raw = series.astype("string").str.strip()
    parsed_numeric = pd.to_datetime(raw, format="%Y%m%d", errors="coerce")
    parsed_general = pd.to_datetime(raw, errors="coerce")
    return parsed_numeric.fillna(parsed_general)


def load_curated_cusip_map(map_source: Path) -> Dict[str, str]:
    """Load a local CSV map or read PHARMA_CUSIP_MAP without executing source."""
    if map_source.suffix.lower() == ".csv":
        mapping = pd.read_csv(map_source, dtype=str)
        lower = {column.lower(): column for column in mapping.columns}
        name_column = lower.get("company") or lower.get("matchedkey") or lower.get("firm")
        cusip_column = lower.get("cusip")
        if not name_column or not cusip_column:
            raise ValueError("CUSIP CSV must contain a company/firm and CUSIP column.")
        result = dict(
            zip(
                mapping[name_column].map(clean_firm_key),
                mapping[cusip_column].map(canonicalize_cusip),
            )
        )
        return {key: value for key, value in result.items() if key and value}

    if not map_source.exists():
        raise FileNotFoundError(
            f"No CUSIP map found at {map_source}. Supply --cusip-map with a local CSV."
        )
    source = map_source.read_text(encoding="utf-8", errors="ignore")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if "PHARMA_CUSIP_MAP" in targets:
                raw_mapping = ast.literal_eval(node.value)
                return {
                    clean_firm_key(name): canonicalize_cusip(cusip)
                    for name, cusip in raw_mapping.items()
                    if clean_firm_key(name) and canonicalize_cusip(cusip)
                }
    raise ValueError(f"PHARMA_CUSIP_MAP was not found in {map_source}.")


def fuzzy_match(name: str, candidates: Iterable[str], threshold: float = 0.70) -> Tuple[Optional[str], float]:
    """Match a BoardEx name to the curated map using exact then fuzzy matching."""
    cleaned = clean_firm_key(name)
    if not cleaned:
        return None, 0.0
    best_name = None
    best_score = 0.0
    name_tokens = set(cleaned.split())
    for candidate in candidates:
        candidate_tokens = set(candidate.split())
        score = SequenceMatcher(None, cleaned, candidate).ratio()
        if name_tokens and candidate_tokens:
            score = max(score, len(name_tokens & candidate_tokens) / len(name_tokens | candidate_tokens))
        if score > best_score:
            best_name = candidate
            best_score = score
    if best_score >= threshold:
        return best_name, best_score
    return None, best_score


def match_ssr_companies(boardex: pd.DataFrame, curated_map: Dict[str, str]) -> pd.DataFrame:
    """Match every SSR BoardEx company to the curated CUSIP map."""
    ssr = boardex.loc[boardex["inSSR"].eq(1), ["BoardName", "CompanyID"]].drop_duplicates().copy()
    ssr["company_id"] = ssr["CompanyID"].map(normalize_id)
    keys = list(curated_map)
    exact = {clean_firm_key(key): key for key in keys}
    rows = []
    for row in ssr.itertuples(index=False):
        board_name = str(row.BoardName)
        cleaned = clean_firm_key(board_name)
        if cleaned in exact:
            matched_key = exact[cleaned]
            score = 1.0
            method = "exact"
        else:
            matched_key, score = fuzzy_match(cleaned, keys)
            method = "fuzzy" if matched_key else "unmatched"
        rows.append(
            {
                "BoardName": board_name,
                "CompanyID": row.CompanyID,
                "company_id": row.company_id,
                "MatchedKey": matched_key or "",
                "CUSIP": curated_map.get(matched_key, "") if matched_key else "",
                "MatchScore": round(score, 4),
                "Method": method,
            }
        )
    mapping = pd.DataFrame(rows)
    return mapping


def build_company_firm_map(mapping: pd.DataFrame) -> Dict[str, str]:
    """Map BoardEx company IDs to one stable firm key."""
    usable = mapping.loc[mapping["MatchedKey"].ne("")].copy()
    usable["firm_key"] = usable["MatchedKey"].map(clean_firm_key)
    counts = usable.groupby(["company_id", "firm_key"]).size().reset_index(name="n")
    counts = counts.sort_values(["company_id", "n", "firm_key"], ascending=[True, False, True])
    return counts.drop_duplicates("company_id").set_index("company_id")["firm_key"].to_dict()


def build_event_firm_matcher(curated_map: Dict[str, str]):
    """Return a function that maps movement names to curated firm keys."""
    keys = list(curated_map)
    exact = {clean_firm_key(key): key for key in keys}

    def match(value: object) -> str:
        cleaned = clean_firm_key(value)
        if cleaned in exact:
            return exact[cleaned]
        matched, _ = fuzzy_match(cleaned, keys)
        return matched or cleaned

    return match


def build_cik_consolidation(cikmap_path: Path) -> Dict[str, str]:
    """Consolidate CIKs with the same latest normalized institution name."""
    cik_map = pd.read_csv(
        cikmap_path,
        usecols=["cik", "rdate", "cikname"],
        dtype={"cik": str, "cikname": str},
    )
    cik_map["cik"] = cik_map["cik"].map(normalize_id)
    cik_map["rdate"] = pd.to_numeric(cik_map["rdate"], errors="coerce")
    cik_map = cik_map.dropna(subset=["cik", "rdate", "cikname"]).copy()
    cik_map["name_clean"] = (
        cik_map["cikname"]
        .str.upper()
        .str.strip()
        .str.replace(r"\b(LLC|INC|CORP|LTD|PLC|LP|L\.P\.?)\b", "", regex=True)
        .str.replace(r"\s+", " ", regex=True)
        .str.strip()
    )
    latest = (
        cik_map.sort_values(["cik", "rdate"])
        .groupby("cik", as_index=False)
        .tail(1)[["cik", "name_clean"]]
    )
    latest["canonical_cik"] = latest.groupby("name_clean")["cik"].transform("min")
    return dict(zip(latest["cik"], latest["canonical_cik"]))


def load_ssr_holdings(
    ownership_path: Path,
    cikmap_path: Path,
    ssr_cusips: set[str],
    chunk_size: int = 5_000_000,
) -> pd.DataFrame:
    """Read 13F data in chunks and retain deduplicated SSR holdings."""
    cik_to_canonical = build_cik_consolidation(cikmap_path)
    selected = []
    total_rows = 0
    matched_rows = 0
    usecols = ["cik", "cusip", "shares", "rdate", "fdate", "filetype"]
    for chunk_number, chunk in enumerate(
        pd.read_csv(
            ownership_path,
            usecols=usecols,
            dtype={"cik": str, "cusip": str, "rdate": str, "fdate": str},
            chunksize=chunk_size,
        ),
        start=1,
    ):
        total_rows += len(chunk)
        chunk = chunk.loc[chunk["filetype"].isin(["13F-HR", "13F-HR/A"])].copy()
        chunk["cusip"] = chunk["cusip"].map(canonicalize_cusip)
        chunk = chunk.loc[chunk["cusip"].isin(ssr_cusips)].copy()
        chunk["shares"] = pd.to_numeric(chunk["shares"], errors="coerce")
        chunk = chunk.loc[chunk["shares"].gt(0)].copy()
        if chunk.empty:
            continue
        chunk["cik"] = chunk["cik"].map(normalize_id)
        chunk["rdate"] = parse_date_series(chunk["rdate"])
        chunk["fdate"] = parse_date_series(chunk["fdate"])
        selected.append(chunk)
        matched_rows += len(chunk)
        if chunk_number % 3 == 0:
            print(f"  Scanned {total_rows / 1e6:.1f}M rows; retained {matched_rows:,} SSR rows.")

    if not selected:
        raise ValueError("No SSR holdings matched the CUSIP map.")
    holdings = pd.concat(selected, ignore_index=True)
    holdings["filetype_priority"] = holdings["filetype"].eq("13F-HR/A").astype("int8")
    holdings = (
        holdings.sort_values(["cik", "rdate", "cusip", "fdate", "filetype_priority"])
        .drop_duplicates(["cik", "rdate", "cusip"], keep="last")
        .drop(columns=["fdate", "filetype", "filetype_priority"])
    )
    holdings["cik"] = holdings["cik"].map(cik_to_canonical).fillna(holdings["cik"])
    holdings = holdings.groupby(["cik", "cusip", "rdate"], as_index=False, sort=False)["shares"].sum()
    holdings = holdings.dropna(subset=["rdate"])
    holdings["year"] = holdings["rdate"].dt.year.astype("int16")
    holdings["quarter"] = holdings["rdate"].dt.quarter.astype("int8")
    print(f"  Scanned {total_rows / 1e6:.1f}M rows; retained {len(holdings):,} final holdings.")
    return holdings


def cap_largest_gamma_p95(holdings: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    """Cap the largest gamma in each CUSIP-quarter and renormalize gamma.

    The cap is defined globally over the uncapped institution-CUSIP-quarter
    gamma observations. Only the largest institution in each CUSIP-quarter is
    changed, as requested for the p95 sensitivity specification. The capped
    values are renormalized so that gamma still sums to one within every
    CUSIP-quarter.
    """
    output = holdings.copy()
    gamma_cap = float(output["gamma"].quantile(0.95))
    output["gamma_p95"] = output["gamma"]
    largest_index = output.groupby(["rdate", "cusip"])["gamma"].idxmax()
    if len(largest_index):
        largest_values = output.loc[largest_index, "gamma"].to_numpy(dtype=float)
        output.loc[largest_index, "gamma_p95"] = np.minimum(
            largest_values, gamma_cap
        )
    capped_totals = output.groupby(["rdate", "cusip"])["gamma_p95"].transform("sum")
    output["gamma_p95"] = np.divide(
        output["gamma_p95"],
        capped_totals,
        out=np.zeros(len(output), dtype=float),
        where=capped_totals.to_numpy(dtype=float) > 0,
    )
    return output, gamma_cap


def compute_kappa_tables(
    holdings: pd.DataFrame,
    cusip_to_firm: Dict[str, str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Compute firm-quarter summaries and pair-quarter Kappa in one matrix pass."""
    holdings = holdings.copy()
    totals = holdings.groupby(["rdate", "cusip"], as_index=False)["shares"].sum()
    totals = totals.rename(columns={"shares": "total_shares"})
    holdings = holdings.merge(
        totals,
        on=["rdate", "cusip"],
        how="left",
        validate="many_to_one",
    )
    holdings["gamma"] = holdings["shares"] / holdings["total_shares"]
    holdings, gamma_p95_cap = cap_largest_gamma_p95(holdings)

    firm_results = []
    pair_results = []
    for quarter_date, quarter_data in holdings.groupby("rdate", sort=True):
        pivot = quarter_data.pivot_table(
            index="cik", columns="cusip", values="gamma", aggfunc="sum", fill_value=0.0
        )
        if pivot.shape[1] < 2:
            continue

        matrix = pivot.to_numpy(dtype=float)
        pivot_p95 = quarter_data.pivot_table(
            index="cik", columns="cusip", values="gamma_p95", aggfunc="sum", fill_value=0.0
        ).reindex(columns=pivot.columns, fill_value=0.0)
        matrix_p95 = pivot_p95.to_numpy(dtype=float)
        cross = matrix.T @ matrix
        hhi = (matrix * matrix).sum(axis=0)
        cross_p95 = matrix_p95.T @ matrix_p95
        hhi_p95 = (matrix_p95 * matrix_p95).sum(axis=0)
        cusips = list(pivot.columns)
        firm_keys = [cusip_to_firm.get(cusip, cusip) for cusip in cusips]

        # Preserve the existing firm-level summary used by legacy controls.
        for firm_index, cusip in enumerate(cusips):
            denominator_raw = hhi[firm_index]
            if denominator_raw <= 0:
                continue
            raw_values = cross[firm_index, :] / denominator_raw
            normalized_denominator = hhi[firm_index] + hhi - cross[firm_index, :]
            normalized_values = np.divide(
                cross[firm_index, :],
                normalized_denominator,
                out=np.full_like(cross[firm_index, :], np.nan),
                where=normalized_denominator > 0,
            )
            keep = np.ones(len(cusips), dtype=bool)
            keep[firm_index] = False
            raw_values = raw_values[keep]
            normalized_values = normalized_values[keep]
            raw_values = raw_values[np.isfinite(raw_values)]
            normalized_values = normalized_values[np.isfinite(normalized_values)]
            if len(raw_values) == 0 or len(normalized_values) == 0:
                continue

            denominator_raw_p95 = hhi_p95[firm_index]
            raw_values_p95 = np.divide(
                cross_p95[firm_index, :],
                denominator_raw_p95,
                out=np.full_like(cross_p95[firm_index, :], np.nan),
                where=denominator_raw_p95 > 0,
            )
            normalized_denominator_p95 = (
                hhi_p95[firm_index] + hhi_p95 - cross_p95[firm_index, :]
            )
            normalized_values_p95 = np.divide(
                cross_p95[firm_index, :],
                normalized_denominator_p95,
                out=np.full_like(cross_p95[firm_index, :], np.nan),
                where=normalized_denominator_p95 > 0,
            )
            raw_values_p95 = raw_values_p95[keep]
            normalized_values_p95 = normalized_values_p95[keep]
            raw_values_p95 = raw_values_p95[np.isfinite(raw_values_p95)]
            normalized_values_p95 = normalized_values_p95[
                np.isfinite(normalized_values_p95)
            ]
            firm_results.append(
                {
                    "rdate": quarter_date,
                    "year": quarter_date.year,
                    "quarter": quarter_date.quarter,
                    "cusip": cusip,
                    "firm_key": firm_keys[firm_index],
                    "kappa_raw_mean": float(np.mean(raw_values)),
                    "kappa_raw_median": float(np.median(raw_values)),
                    "kappa_raw_std": float(np.std(raw_values, ddof=1))
                    if len(raw_values) > 1
                    else np.nan,
                    "kappa_norm_mean": float(np.mean(normalized_values)),
                    "kappa_norm_median": float(np.median(normalized_values)),
                    "kappa_norm_std": float(np.std(normalized_values, ddof=1))
                    if len(normalized_values) > 1
                    else np.nan,
                    "kappa_raw_p95_mean": float(np.mean(raw_values_p95))
                    if len(raw_values_p95)
                    else np.nan,
                    "kappa_raw_p95_median": float(np.median(raw_values_p95))
                    if len(raw_values_p95)
                    else np.nan,
                    "kappa_norm_p95_mean": float(np.mean(normalized_values_p95))
                    if len(normalized_values_p95)
                    else np.nan,
                    "kappa_norm_p95_median": float(np.median(normalized_values_p95))
                    if len(normalized_values_p95)
                    else np.nan,
                    "gamma_p95_cap": gamma_p95_cap,
                    "n_pairs": int(len(normalized_values)),
                }
            )

        # Retain one normalized and both directional raw values for each pair.
        # Pair keys are sorted so the normalized measure is uniquely identified.
        for left_index in range(len(cusips) - 1):
            for right_index in range(left_index + 1, len(cusips)):
                left_key = firm_keys[left_index]
                right_key = firm_keys[right_index]
                if not left_key or not right_key or left_key == right_key:
                    continue

                overlap = float(cross[left_index, right_index])
                norm_denominator = (
                    hhi[left_index] + hhi[right_index] - overlap
                )
                if norm_denominator <= 0:
                    continue
                normalized_pair = overlap / norm_denominator
                if not np.isfinite(normalized_pair):
                    continue

                overlap_p95 = float(cross_p95[left_index, right_index])
                norm_denominator_p95 = (
                    hhi_p95[left_index] + hhi_p95[right_index] - overlap_p95
                )
                if norm_denominator_p95 <= 0:
                    continue
                normalized_pair_p95 = overlap_p95 / norm_denominator_p95
                if not np.isfinite(normalized_pair_p95):
                    continue

                if left_key <= right_key:
                    firm_i_key, firm_j_key = left_key, right_key
                    raw_i_to_j = overlap / hhi[left_index] if hhi[left_index] > 0 else np.nan
                    raw_j_to_i = overlap / hhi[right_index] if hhi[right_index] > 0 else np.nan
                    raw_p95_i_to_j = (
                        overlap_p95 / hhi_p95[left_index]
                        if hhi_p95[left_index] > 0
                        else np.nan
                    )
                    raw_p95_j_to_i = (
                        overlap_p95 / hhi_p95[right_index]
                        if hhi_p95[right_index] > 0
                        else np.nan
                    )
                else:
                    firm_i_key, firm_j_key = right_key, left_key
                    raw_i_to_j = overlap / hhi[right_index] if hhi[right_index] > 0 else np.nan
                    raw_j_to_i = overlap / hhi[left_index] if hhi[left_index] > 0 else np.nan
                    raw_p95_i_to_j = (
                        overlap_p95 / hhi_p95[right_index]
                        if hhi_p95[right_index] > 0
                        else np.nan
                    )
                    raw_p95_j_to_i = (
                        overlap_p95 / hhi_p95[left_index]
                        if hhi_p95[left_index] > 0
                        else np.nan
                    )

                pair_results.append(
                    {
                        "rdate": quarter_date,
                        "year": quarter_date.year,
                        "quarter": quarter_date.quarter,
                        "firm_i_key": firm_i_key,
                        "firm_j_key": firm_j_key,
                        "kappa_norm_pair": float(normalized_pair),
                        "kappa_norm_p95_pair": float(normalized_pair_p95),
                        "kappa_raw_i_to_j": float(raw_i_to_j)
                        if np.isfinite(raw_i_to_j)
                        else np.nan,
                        "kappa_raw_j_to_i": float(raw_j_to_i)
                        if np.isfinite(raw_j_to_i)
                        else np.nan,
                        "kappa_raw_p95_i_to_j": float(raw_p95_i_to_j)
                        if np.isfinite(raw_p95_i_to_j)
                        else np.nan,
                        "kappa_raw_p95_j_to_i": float(raw_p95_j_to_i)
                        if np.isfinite(raw_p95_j_to_i)
                        else np.nan,
                    }
                )

    firm_result = pd.DataFrame(firm_results)
    pair_result = pd.DataFrame(pair_results)
    if firm_result.empty:
        raise ValueError("Kappa computation produced no firm-quarter observations.")
    if pair_result.empty:
        raise ValueError("Kappa computation produced no pair-quarter observations.")

    # If a firm is represented by multiple securities, collapse security pairs
    # to one firm pair so a counterpart is counted only once in the exposure sum.
    pair_result = (
        pair_result.groupby(
            ["rdate", "year", "quarter", "firm_i_key", "firm_j_key"],
            as_index=False,
        )
        .agg(
            kappa_norm_pair=("kappa_norm_pair", "mean"),
            kappa_norm_p95_pair=("kappa_norm_p95_pair", "mean"),
            kappa_raw_i_to_j=("kappa_raw_i_to_j", "mean"),
            kappa_raw_j_to_i=("kappa_raw_j_to_i", "mean"),
            kappa_raw_p95_i_to_j=("kappa_raw_p95_i_to_j", "mean"),
            kappa_raw_p95_j_to_i=("kappa_raw_p95_j_to_i", "mean"),
            n_security_pairs=("kappa_norm_pair", "size"),
        )
    )
    firm_result = firm_result.sort_values(["rdate", "firm_key"]).reset_index(drop=True)
    pair_result = pair_result.sort_values(
        ["rdate", "firm_i_key", "firm_j_key"]
    ).reset_index(drop=True)
    return firm_result, pair_result


def compute_firm_level_kappa(
    holdings: pd.DataFrame,
    cusip_to_firm: Dict[str, str],
) -> pd.DataFrame:
    """Compute raw and normalized Kappa summaries for every firm-quarter."""
    firm_result, _ = compute_kappa_tables(holdings, cusip_to_firm)
    return firm_result


def aggregate_annual_kappa(kappa_quarter: pd.DataFrame) -> pd.DataFrame:
    """Aggregate quarterly firm-level Kappa to the annual movement frequency."""
    annual = (
        kappa_quarter.groupby(["year", "firm_key", "cusip"], as_index=False)
        .agg(
            kappa_raw_mean_year=("kappa_raw_mean", "mean"),
            kappa_raw_median_year=("kappa_raw_median", "mean"),
            kappa_norm_mean_year=("kappa_norm_mean", "mean"),
            kappa_norm_median_year=("kappa_norm_median", "mean"),
            kappa_raw_p95_mean_year=("kappa_raw_p95_mean", "mean"),
            kappa_raw_p95_median_year=("kappa_raw_p95_median", "mean"),
            kappa_norm_p95_mean_year=("kappa_norm_p95_mean", "mean"),
            kappa_norm_p95_median_year=("kappa_norm_p95_median", "mean"),
            n_quarters=("quarter", "nunique"),
            n_pairs_mean=("n_pairs", "mean"),
        )
    )
    return annual.sort_values(["year", "firm_key"]).reset_index(drop=True)


def load_movement_candidates(
    path: Path,
    event_matcher,
    panel_level: str,
) -> pd.DataFrame:
    """Load movement candidates and standardize annual and quarterly fields.

    Annual event candidates are defined at the year frequency.  The project's
    convention is that an annual movement occurs in Q1, so annual rows are
    assigned Q1 directly and never use employment dates to infer a quarter.
    Quarterly rows retain an input quarter/date/time_id when available.
    """
    movement = pd.read_csv(path, low_memory=False)
    required = {"event_type", "FirmA", "FirmB"}
    missing = required - set(movement.columns)
    if missing:
        raise ValueError(f"Movement input is missing columns: {sorted(missing)}")
    movement = movement.copy()
    movement.insert(0, "movement_row_id", np.arange(len(movement), dtype=np.int64))
    year_column = next((column for column in ["event_year", "Year", "year"] if column in movement), None)
    if year_column is None:
        raise ValueError("Movement input must contain event_year, Year, or year.")
    movement["event_year"] = pd.to_numeric(movement[year_column], errors="coerce").astype("Int64")
    movement["firm_a_key"] = movement["FirmA"].map(event_matcher)
    movement["firm_b_key"] = movement["FirmB"].map(event_matcher)

    quarter_column = next(
        (
            column
            for column in ["event_quarter", "movement_quarter", "quarter"]
            if column in movement
        ),
        None,
    )
    date_column = next(
        (column for column in ["event_date", "movement_date", "event_rdate"] if column in movement), None
    )
    movement["event_quarter"] = pd.Series(pd.array([pd.NA] * len(movement), dtype="Int64"))
    movement["event_date"] = pd.NaT
    movement["quarter_source"] = "unresolved"
    if quarter_column:
        given_quarter = pd.to_numeric(movement[quarter_column], errors="coerce").astype("Int64")
        valid = given_quarter.between(1, 4)
        movement.loc[valid, "event_quarter"] = given_quarter.loc[valid]
        movement.loc[valid, "quarter_source"] = "provided"
    if date_column:
        event_dates = parse_date_series(movement[date_column])
        valid_dates = event_dates.notna()
        movement.loc[valid_dates, "event_date"] = event_dates.loc[valid_dates]
        movement.loc[valid_dates & movement["event_quarter"].isna(), "event_year"] = event_dates.loc[
            valid_dates & movement["event_quarter"].isna()
        ].dt.year.astype("Int64")
        missing_quarter = valid_dates & movement["event_quarter"].isna()
        movement.loc[missing_quarter, "event_quarter"] = event_dates.loc[missing_quarter].dt.quarter.astype("Int64")
        movement.loc[missing_quarter, "quarter_source"] = "provided_date"

    # Annual events have no observed event quarter by construction.  Under the
    # annual-panel convention, place every annual event at Q1 rather than
    # inferring a quarter from employment spell dates.  This also overwrites
    # any accidental quarter/date column in an annual input file so that the
    # annual definition is deterministic across all roster variants.
    if panel_level == "year":
        movement["event_quarter"] = pd.Series(
            pd.array([1] * len(movement), dtype="Int64"), index=movement.index
        )
        movement["quarter_source"] = "annual_default_q1"

    if panel_level == "quarter" and movement["event_quarter"].isna().any():
        time_column = next(
            (column for column in ["time_id", "event_time_id"] if column in movement),
            None,
        )
        if time_column:
            time_id = pd.to_numeric(movement[time_column], errors="coerce")
            valid_time = (
                movement["event_quarter"].isna()
                & time_id.notna()
                & movement["event_year"].notna()
            )
            derived_year = (time_id // 4).astype("Int64")
            derived_quarter = (time_id % 4 + 1).astype("Int64")
            movement.loc[valid_time, "event_year"] = derived_year.loc[valid_time]
            movement.loc[valid_time, "event_quarter"] = derived_quarter.loc[valid_time]
            movement.loc[valid_time, "quarter_source"] = "time_id"
    movement["event_year"] = movement["event_year"].astype("Int64")
    return movement


def load_relevant_employment_dates(
    employment_path: Path,
    director_ids: set[str],
    company_ids: set[str],
    min_year: int,
    max_year: int,
    company_to_firm: Dict[str, str],
    chunk_size: int = 750_000,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Read only relevant BoardEx employment rows and return role starts/ends."""
    starts = []
    ends = []
    usecols = ["companyid", "directorid", "datestartrole", "dateendrole"]
    for chunk_number, chunk in enumerate(
        pd.read_csv(
            employment_path,
            usecols=usecols,
            dtype={column: str for column in usecols},
            chunksize=chunk_size,
        ),
        start=1,
    ):
        chunk["company_id"] = chunk["companyid"].map(normalize_id)
        chunk["director_id"] = chunk["directorid"].map(normalize_id)
        chunk = chunk.loc[
            chunk["company_id"].isin(company_ids) & chunk["director_id"].isin(director_ids)
        ].copy()
        if chunk.empty:
            continue
        chunk["firm_key"] = chunk["company_id"].map(company_to_firm).fillna("")
        chunk["start_date"] = parse_date_series(chunk["datestartrole"])
        chunk["end_date"] = parse_date_series(chunk["dateendrole"])
        valid_start = chunk["start_date"].dt.year.between(min_year, max_year)
        valid_end = chunk["end_date"].dt.year.between(min_year, max_year)
        valid_start &= chunk["start_date"].ne(pd.Timestamp("1900-01-01"))
        valid_end &= ~chunk["end_date"].isin([pd.Timestamp("9999-12-31"), pd.Timestamp("9000-01-01")])
        if valid_start.any():
            starts.append(chunk.loc[valid_start, ["director_id", "firm_key", "start_date"]])
        if valid_end.any():
            ends.append(chunk.loc[valid_end, ["director_id", "firm_key", "end_date"]])
        if chunk_number % 10 == 0:
            print(f"  Scanned {chunk_number * chunk_size / 1e6:.1f}M BoardEx employment rows.")
    start_data = pd.concat(starts, ignore_index=True) if starts else pd.DataFrame()
    end_data = pd.concat(ends, ignore_index=True) if ends else pd.DataFrame()
    return start_data, end_data


def derive_movement_quarters(
    movement: pd.DataFrame,
    employment_path: Path,
    company_to_firm: Dict[str, str],
    chunk_size: int = 750_000,
) -> pd.DataFrame:
    """Use exact BoardEx start/end dates to assign unresolved movement quarters."""
    movement = movement.copy()
    needs_date = movement["event_quarter"].isna() & movement["event_year"].notna()
    if not needs_date.any():
        return movement
    director_ids = set(movement.loc[needs_date, "DirectorID"].map(normalize_id)) if "DirectorID" in movement else set()
    director_ids.discard("")
    firm_names = set(movement.loc[needs_date, "firm_b_key"].dropna()) | set(
        movement.loc[needs_date, "firm_a_key"].dropna()
    )
    company_ids = {company_id for company_id, firm in company_to_firm.items() if firm in firm_names}
    if not director_ids or not company_ids:
        return movement
    min_year = int(movement.loc[needs_date, "event_year"].min())
    max_year = int(movement.loc[needs_date, "event_year"].max())
    starts, ends = load_relevant_employment_dates(
        employment_path,
        director_ids,
        company_ids,
        min_year,
        max_year,
        company_to_firm,
        chunk_size=chunk_size,
    )
    if starts.empty and ends.empty:
        return movement
    if not starts.empty:
        starts["event_year"] = starts["start_date"].dt.year.astype("int64")
        start_lookup = starts.groupby(["director_id", "firm_key", "event_year"], as_index=False)["start_date"].min()
    else:
        start_lookup = pd.DataFrame(columns=["director_id", "firm_key", "event_year", "start_date"])
    if not ends.empty:
        ends["event_year"] = ends["end_date"].dt.year.astype("int64")
        end_lookup = ends.groupby(["director_id", "firm_key", "event_year"], as_index=False)["end_date"].min()
    else:
        end_lookup = pd.DataFrame(columns=["director_id", "firm_key", "event_year", "end_date"])

    movement["director_id"] = movement["DirectorID"].map(normalize_id) if "DirectorID" in movement else ""
    start_keys = pd.MultiIndex.from_frame(
        start_lookup[["director_id", "firm_key", "event_year"]]
    ) if not start_lookup.empty else pd.MultiIndex.from_arrays([[], [], []])
    end_keys = pd.MultiIndex.from_frame(
        end_lookup[["director_id", "firm_key", "event_year"]]
    ) if not end_lookup.empty else pd.MultiIndex.from_arrays([[], [], []])
    start_values = dict(zip(start_keys, start_lookup["start_date"]))
    end_values = dict(zip(end_keys, end_lookup["end_date"]))

    for index in movement.index[needs_date]:
        director_id = movement.at[index, "director_id"]
        year = movement.at[index, "event_year"]
        firm_b = movement.at[index, "firm_b_key"]
        event_type = str(movement.at[index, "event_type"]).lower()
        key = (director_id, firm_b, int(year))
        if "dissolution" in event_type or "dissolve" in event_type:
            date = end_values.get(key)
            source = "employment_end"
        else:
            date = start_values.get(key)
            source = "employment_start"
        if date is not None and not pd.isna(date):
            movement.at[index, "event_date"] = date
            movement.at[index, "event_quarter"] = int(date.quarter)
            movement.at[index, "quarter_source"] = source
    return movement


def add_partner_counts(movement: pd.DataFrame) -> pd.DataFrame:
    """Count distinct origin firms per destination firm and movement timing."""
    movement = movement.copy()
    movement["n_origin_partners_year"] = np.nan
    movement["n_origin_partners_yearquarter"] = np.nan
    valid_year = movement["event_year"].notna() & movement["firm_b_key"].ne("")
    year_counts = (
        movement.loc[valid_year]
        .groupby(["event_type", "event_year", "firm_b_key"])["firm_a_key"]
        .nunique()
        .rename("n_origin_partners_year")
        .reset_index()
    )
    movement = movement.merge(year_counts, on=["event_type", "event_year", "firm_b_key"], how="left", suffixes=("", "_new"))
    movement["n_origin_partners_year"] = movement["n_origin_partners_year_new"].fillna(
        movement["n_origin_partners_year"]
    )
    movement = movement.drop(columns=["n_origin_partners_year_new"])
    valid_quarter = valid_year & movement["event_quarter"].notna()
    quarter_counts = (
        movement.loc[valid_quarter]
        .groupby(["event_type", "event_year", "event_quarter", "firm_b_key"])["firm_a_key"]
        .nunique()
        .rename("n_origin_partners_yearquarter")
        .reset_index()
    )
    movement = movement.merge(
        quarter_counts,
        on=["event_type", "event_year", "event_quarter", "firm_b_key"],
        how="left",
        suffixes=("", "_new"),
    )
    movement["n_origin_partners_yearquarter"] = movement["n_origin_partners_yearquarter_new"].fillna(
        movement["n_origin_partners_yearquarter"]
    )
    movement = movement.drop(columns=["n_origin_partners_yearquarter_new"])
    return movement


def build_annual_controls(kappa_year: pd.DataFrame, movement: pd.DataFrame, firm_universe: list[str]) -> pd.DataFrame:
    """Create one annual pre-Kappa row for every firm and movement year cohort."""
    years = sorted(pd.to_numeric(movement["event_year"], errors="coerce").dropna().astype(int).unique())
    cohort = pd.MultiIndex.from_product([firm_universe, years], names=["firm_key", "event_year"]).to_frame(index=False)
    cohort["pre_year"] = cohort["event_year"] - 1
    pre = kappa_year.rename(
        columns={
            "year": "pre_year",
            "kappa_raw_mean_year": "kappa_raw_pre",
            "kappa_norm_mean_year": "kappa_norm_pre",
            "kappa_raw_p95_mean_year": "kappa_raw_p95_pre",
            "kappa_norm_p95_mean_year": "kappa_norm_p95_pre",
            "n_quarters": "n_pre_quarters",
        }
    )[
        [
            "firm_key",
            "pre_year",
            "kappa_raw_pre",
            "kappa_norm_pre",
            "kappa_raw_p95_pre",
            "kappa_norm_p95_pre",
            "n_pre_quarters",
        ]
    ]
    controls = cohort.merge(pre, on=["firm_key", "pre_year"], how="left", validate="one_to_one")
    controls["kappa_pre_complete"] = controls["n_pre_quarters"].eq(4)
    controls["kappa_pre_valid"] = controls["n_pre_quarters"].ge(2)
    controls["data_cohort"] = controls["event_year"].map(lambda year: f"Y{int(year)}")
    return controls.drop(columns=["pre_year"]).sort_values(["event_year", "firm_key"])


def build_quarter_controls(
    kappa_quarter: pd.DataFrame,
    movement: pd.DataFrame,
    firm_universe: list[str],
) -> pd.DataFrame:
    """Create four-quarter pre-Kappa controls for every firm and quarter cohort."""
    valid = movement["event_year"].notna() & movement["event_quarter"].notna()
    cohort_values = (
        movement.loc[valid, ["event_year", "event_quarter"]]
        .drop_duplicates()
        .astype({"event_year": int, "event_quarter": int})
    )
    if cohort_values.empty:
        return pd.DataFrame(
            columns=[
                "firm_key", "event_year", "event_quarter", "kappa_raw_pre", "kappa_norm_pre",
                "kappa_raw_p95_pre", "kappa_norm_p95_pre",
                "n_pre_quarters", "kappa_pre_complete", "kappa_pre_valid", "data_cohort",
            ]
        )
    cohort = cohort_values.assign(_join=1).merge(
        pd.DataFrame({"firm_key": firm_universe, "_join": 1}), on="_join", how="inner"
    ).drop(columns="_join")
    cohort["event_index"] = cohort["event_year"] * 4 + cohort["event_quarter"] - 1
    observed = kappa_quarter[
        [
            "firm_key",
            "year",
            "quarter",
            "kappa_raw_mean",
            "kappa_norm_mean",
            "kappa_raw_p95_mean",
            "kappa_norm_p95_mean",
        ]
    ].copy()
    observed["q_index"] = observed["year"] * 4 + observed["quarter"] - 1
    merged = cohort.merge(observed, on="firm_key", how="left")
    pre = merged.loc[
        merged["q_index"].notna()
        & merged["q_index"].ge(merged["event_index"] - 4)
        & merged["q_index"].lt(merged["event_index"])
    ].copy()
    controls = (
        pre.groupby(["firm_key", "event_year", "event_quarter"], as_index=False)
        .agg(
            kappa_raw_pre=("kappa_raw_mean", "mean"),
            kappa_norm_pre=("kappa_norm_mean", "mean"),
            kappa_raw_p95_pre=("kappa_raw_p95_mean", "mean"),
            kappa_norm_p95_pre=("kappa_norm_p95_mean", "mean"),
            n_pre_quarters=("q_index", "nunique"),
        )
    )
    controls = cohort[["firm_key", "event_year", "event_quarter"]].merge(
        controls, on=["firm_key", "event_year", "event_quarter"], how="left", validate="one_to_one"
    )
    controls["kappa_pre_complete"] = controls["n_pre_quarters"].eq(4)
    controls["kappa_pre_valid"] = controls["n_pre_quarters"].ge(2)
    controls["data_cohort"] = controls.apply(
        lambda row: f"Y{int(row.event_year)}Q{int(row.event_quarter)}", axis=1
    )
    return controls.sort_values(["event_year", "event_quarter", "firm_key"])


def build_potential_exposure_controls(
    kappa_pair_quarter: pd.DataFrame,
    movement: pd.DataFrame,
    firm_universe: list[str],
    frequency: str,
) -> pd.DataFrame:
    """Build pre-event total and mean pair exposure for every firm-cohort."""
    if frequency not in {"year", "yearquarter"}:
        raise ValueError("frequency must be either 'year' or 'yearquarter'.")

    if frequency == "year":
        valid = movement["event_year"].notna()
        cohort_values = (
            movement.loc[valid, ["event_year"]]
            .drop_duplicates()
            .astype({"event_year": int})
        )
    else:
        valid = movement["event_year"].notna() & movement["event_quarter"].notna()
        cohort_values = (
            movement.loc[valid, ["event_year", "event_quarter"]]
            .drop_duplicates()
            .astype({"event_year": int, "event_quarter": int})
        )

    output_columns = [
        "firm_key",
        "event_year",
        *(["event_quarter"] if frequency == "yearquarter" else []),
        "co_potential_sum_pre_norm",
        "co_potential_mean_pre_norm",
        "n_potential_partners",
        "n_risk_set_partners",
        "n_potential_pre_quarters",
        "n_pair_pre_observations",
        "co_potential_pre_complete",
        "co_potential_pre_valid",
        "data_cohort",
    ]
    if cohort_values.empty or len(firm_universe) < 2:
        return pd.DataFrame(columns=output_columns)

    # Convert the undirected pair table into focal-firm observations. The
    # normalized Kappa is symmetric, so either orientation has the same value.
    pair = kappa_pair_quarter.loc[
        kappa_pair_quarter["firm_i_key"].isin(firm_universe)
        & kappa_pair_quarter["firm_j_key"].isin(firm_universe)
    ].copy()
    if pair.empty:
        return pd.DataFrame(columns=output_columns)

    left = pair.rename(
        columns={"firm_i_key": "firm_key", "firm_j_key": "partner_key"}
    )
    right = pair.rename(
        columns={"firm_i_key": "partner_key", "firm_j_key": "firm_key"}
    )
    pair_long = pd.concat([left, right], ignore_index=True)
    pair_long = pair_long[
        ["firm_key", "partner_key", "year", "quarter", "kappa_norm_pair"]
    ].copy()

    if frequency == "year":
        # Annual events use the preceding calendar year's quarterly average.
        pair_long["event_year"] = pair_long["year"] + 1
        pre = pair_long.merge(cohort_values, on="event_year", how="inner")
        group_keys = ["firm_key", "partner_key", "event_year"]
    else:
        # A pre-event quarter contributes to each of the next four quarter
        # cohorts. This avoids a large firm-by-cohort Cartesian product.
        pair_long["q_index"] = pair_long["year"] * 4 + pair_long["quarter"] - 1
        shifted = []
        for lag in range(1, 5):
            part = pair_long.copy()
            part["event_index"] = part["q_index"] + lag
            part["event_year"] = part["event_index"] // 4
            part["event_quarter"] = part["event_index"] % 4 + 1
            shifted.append(part)
        pre = pd.concat(shifted, ignore_index=True).merge(
            cohort_values,
            on=["event_year", "event_quarter"],
            how="inner",
        )
        group_keys = ["firm_key", "partner_key", "event_year", "event_quarter"]

    if pre.empty:
        return pd.DataFrame(columns=output_columns)

    # First average each pair over the pre-event window. Then sum across
    # potential partners so partner count is not multiplied by four quarters.
    pair_pre = (
        pre.groupby(group_keys, as_index=False)
        .agg(
            pair_pre_norm=("kappa_norm_pair", "mean"),
            pair_pre_quarters=("kappa_norm_pair", "size"),
        )
    )
    exposure_keys = ["firm_key", "event_year"]
    if frequency == "yearquarter":
        exposure_keys.append("event_quarter")
    exposure = (
        pair_pre.groupby(exposure_keys, as_index=False)
        .agg(
            co_potential_sum_pre_norm=("pair_pre_norm", "sum"),
            co_potential_mean_pre_norm=("pair_pre_norm", "mean"),
            n_potential_partners=("partner_key", "nunique"),
            n_potential_pre_quarters=("pair_pre_quarters", "max"),
            n_pair_pre_observations=("pair_pre_norm", "size"),
        )
    )

    firm_cohort = cohort_values.assign(_join=1).merge(
        pd.DataFrame({"firm_key": sorted(set(firm_universe)), "_join": 1}),
        on="_join",
        how="inner",
    ).drop(columns="_join")
    exposure = firm_cohort.merge(
        exposure,
        on=exposure_keys,
        how="left",
        validate="one_to_one",
    )
    exposure["n_risk_set_partners"] = max(len(set(firm_universe)) - 1, 0)
    exposure["co_potential_pre_complete"] = exposure["n_potential_pre_quarters"].eq(4)
    exposure["co_potential_pre_valid"] = (
        exposure["n_potential_partners"].gt(0)
        & exposure["n_potential_pre_quarters"].ge(2)
    )
    if frequency == "year":
        exposure["data_cohort"] = exposure["event_year"].map(
            lambda year: f"Y{int(year)}"
        )
    else:
        exposure["data_cohort"] = exposure.apply(
            lambda row: f"Y{int(row.event_year)}Q{int(row.event_quarter)}",
            axis=1,
        )
    return exposure[output_columns].sort_values(
        ["event_year", *(["event_quarter"] if frequency == "yearquarter" else []), "firm_key"]
    )


def attach_event_kappa(movement: pd.DataFrame, controls: pd.DataFrame, frequency: str) -> pd.DataFrame:
    """Attach pre-Kappa to both origin (A) and destination (B) sides of movement."""
    movement = movement.copy()
    if frequency == "year":
        keys = ["event_year"]
    else:
        keys = ["event_year", "event_quarter"]
    base_columns = [
        "kappa_raw_pre",
        "kappa_norm_pre",
        "kappa_raw_p95_pre",
        "kappa_norm_p95_pre",
        "n_pre_quarters",
        "kappa_pre_valid",
    ]
    exposure_columns = [
        "co_potential_sum_pre_norm",
        "co_potential_mean_pre_norm",
        "n_potential_partners",
        "n_risk_set_partners",
        "co_potential_pre_complete",
        "co_potential_pre_valid",
    ]
    columns = keys + ["firm_key"] + base_columns + [
        column for column in exposure_columns if column in controls.columns
    ]
    lookup = controls[columns].copy()
    a_lookup = lookup.rename(
        columns={column: f"{column}_A" for column in columns if column not in keys}
    )
    b_lookup = lookup.rename(
        columns={column: f"{column}_B" for column in columns if column not in keys}
    )
    output = movement.merge(
        a_lookup,
        left_on=keys + ["firm_a_key"],
        right_on=keys + ["firm_key_A"],
        how="left",
    ).drop(columns=["firm_key_A"])
    output = output.merge(
        b_lookup,
        left_on=keys + ["firm_b_key"],
        right_on=keys + ["firm_key_B"],
        how="left",
    ).drop(columns=["firm_key_B"])
    output = output.rename(
        columns={
            "kappa_raw_pre_A": "kappa_raw_pre_origin",
            "kappa_norm_pre_A": "kappa_norm_pre_origin",
            "kappa_raw_p95_pre_A": "kappa_raw_p95_pre_origin",
            "kappa_norm_p95_pre_A": "kappa_norm_p95_pre_origin",
            "n_pre_quarters_A": "n_pre_quarters_origin",
            "kappa_pre_valid_A": "kappa_pre_valid_origin",
            "kappa_raw_pre_B": "kappa_raw_pre_destination",
            "kappa_norm_pre_B": "kappa_norm_pre_destination",
            "kappa_raw_p95_pre_B": "kappa_raw_p95_pre_destination",
            "kappa_norm_p95_pre_B": "kappa_norm_p95_pre_destination",
            "n_pre_quarters_B": "n_pre_quarters_destination",
            "kappa_pre_valid_B": "kappa_pre_valid_destination",
            "co_potential_sum_pre_norm_A": "co_potential_sum_pre_norm_origin",
            "co_potential_mean_pre_norm_A": "co_potential_mean_pre_norm_origin",
            "n_potential_partners_A": "n_potential_partners_origin",
            "n_risk_set_partners_A": "n_risk_set_partners_origin",
            "co_potential_pre_complete_A": "co_potential_pre_complete_origin",
            "co_potential_pre_valid_A": "co_potential_pre_valid_origin",
            "co_potential_sum_pre_norm_B": "co_potential_sum_pre_norm_destination",
            "co_potential_mean_pre_norm_B": "co_potential_mean_pre_norm_destination",
            "n_potential_partners_B": "n_potential_partners_destination",
            "n_risk_set_partners_B": "n_risk_set_partners_destination",
            "co_potential_pre_complete_B": "co_potential_pre_complete_destination",
            "co_potential_pre_valid_B": "co_potential_pre_valid_destination",
        }
    )
    return output


def parse_args() -> argparse.Namespace:
    """Parse command-line paths while keeping the project defaults explicit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Backward-compatible alias for --output-root.",
    )
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--cusip-map", type=Path, default=DEFAULT_MAP_SOURCE)
    parser.add_argument("--chunk-size", type=int, default=5_000_000)
    parser.add_argument("--employment-chunk-size", type=int, default=750_000)
    parser.add_argument("--movement-input", type=Path, default=None)
    parser.add_argument(
        "--roster-variants",
        nargs="+",
        choices=sorted(ROSTER_VARIANTS),
        default=list(ROSTER_VARIANTS),
    )
    parser.add_argument(
        "--personnel-definitions",
        nargs="+",
        choices=list(PERSONNEL_DEFINITIONS),
        default=list(PERSONNEL_DEFINITIONS),
    )
    return parser.parse_args()


def main() -> None:
    """Run the complete raw-data to regression-control pipeline."""
    args = parse_args()
    output_root = args.root or args.output_root
    data_root = args.data_root
    raw_common = data_root / "RawData" / "common_ownership"
    ownership_path = raw_common / "processed_13f_data.csv"
    cikmap_path = raw_common / "cikmap.csv"
    employment_path = (
        data_root / "RawData" / "boardex" / "boardex_na" / "individual_employment.csv"
    )

    selected_rosters: dict[tuple[str, str], pd.DataFrame] = {}
    combined_roster = []
    for roster_variant in args.roster_variants:
        roster = load_variant_roster(str(roster_variant))
        for personnel_definition in args.personnel_definitions:
            selected = filter_roster_tier(roster, str(personnel_definition))
            if selected.empty:
                raise ValueError(
                    f"No SSR roster rows for {roster_variant} / {personnel_definition}."
                )
            selected_rosters[(str(roster_variant), str(personnel_definition))] = selected
            combined_roster.append(selected[["BoardName", "CompanyID", "inSSR"]])

    curated_map = load_curated_cusip_map(args.cusip_map)
    roster_for_mapping = pd.concat(combined_roster, ignore_index=True).drop_duplicates()
    mapping = match_ssr_companies(roster_for_mapping, curated_map)
    company_to_firm = build_company_firm_map(mapping)
    cusip_to_firm = (
        mapping.loc[mapping["CUSIP"].ne("")]
        .drop_duplicates("CUSIP")
        .set_index("CUSIP")["MatchedKey"]
        .map(clean_firm_key)
        .to_dict()
    )
    if not cusip_to_firm:
        raise ValueError("No selected SSR roster company matched the curated CUSIP map.")
    print(
        f"Matched selected SSR roster records: {len(mapping):,}; "
        f"CUSIPs: {len(cusip_to_firm):,}."
    )

    holdings = load_ssr_holdings(ownership_path, cikmap_path, set(cusip_to_firm), args.chunk_size)
    kappa_quarter, kappa_pair_quarter = compute_kappa_tables(holdings, cusip_to_firm)
    kappa_year = aggregate_annual_kappa(kappa_quarter)

    event_matcher = build_event_firm_matcher(curated_map)
    variant_output_root = Path(
        os.environ.get(
            "BOARDPHARMA_VARIANT_OUTPUT_ROOT",
            str(output_root / "data" / "roster_variants"),
        )
    )
    for (roster_variant, personnel_definition), selected in selected_rosters.items():
        output_dir = (
            variant_output_root
            / roster_variant
            / f"leader_tier_{personnel_definition}"
            / "kappa_controls"
        )
        output_dir.mkdir(parents=True, exist_ok=True)

        selected_company_ids = set(selected["CompanyID"].loc[lambda values: values.ne("")])
        selected_mapping = mapping.loc[mapping["company_id"].isin(selected_company_ids)].copy()
        firm_universe = sorted(
            set(
                selected_mapping.loc[selected_mapping["MatchedKey"].ne(""), "MatchedKey"]
                .map(clean_firm_key)
            )
        )
        if not firm_universe:
            raise ValueError(
                f"No mapped firm universe for {roster_variant} / {personnel_definition}."
            )

        panel_level = str(get_variant(roster_variant)["panel_level"])
        movement_path = args.movement_input or (
            variant_output_root
            / roster_variant
            / f"leader_tier_{personnel_definition}"
            / "event_tables"
            / "movement_event_candidates.csv"
        )
        if not movement_path.exists():
            raise FileNotFoundError(f"Movement candidates not found: {movement_path}")

        movement = load_movement_candidates(movement_path, event_matcher, panel_level)
        unresolved_before = int(movement["event_quarter"].isna().sum())
        # Employment dates are a fallback only for quarterly events.  Annual
        # events are already assigned Q1 by load_movement_candidates and must
        # not be reconstructed from employment spells.
        if panel_level == "quarter" and unresolved_before:
            if not employment_path.exists():
                raise FileNotFoundError(f"BoardEx employment file not found: {employment_path}")
            movement = derive_movement_quarters(
                movement,
                employment_path,
                company_to_firm,
                chunk_size=args.employment_chunk_size,
            )
        movement = add_partner_counts(movement)

        selected_kappa_quarter = kappa_quarter.loc[
            kappa_quarter["firm_key"].isin(firm_universe)
        ].copy()
        selected_kappa_year = kappa_year.loc[
            kappa_year["firm_key"].isin(firm_universe)
        ].copy()
        selected_kappa_pair_quarter = kappa_pair_quarter.loc[
            kappa_pair_quarter["firm_i_key"].isin(firm_universe)
            & kappa_pair_quarter["firm_j_key"].isin(firm_universe)
        ].copy()
        selected_mapping.to_csv(output_dir / "ssr_cusip_mapping.csv", index=False)
        selected_kappa_quarter.to_csv(
            output_dir / "ssr_kappa_firm_level_quarter.csv", index=False
        )
        selected_kappa_year.to_csv(output_dir / "ssr_kappa_firm_level_year.csv", index=False)
        selected_kappa_pair_quarter.to_csv(
            output_dir / "ssr_kappa_pair_level_quarter.csv", index=False
        )
        movement.to_csv(output_dir / "movement_candidates_yearquarter.csv", index=False)

        controls_year = build_annual_controls(selected_kappa_year, movement, firm_universe)
        potential_year = build_potential_exposure_controls(
            selected_kappa_pair_quarter,
            movement,
            firm_universe,
            "year",
        )
        controls_year = controls_year.merge(
            potential_year,
            on=["firm_key", "event_year", "data_cohort"],
            how="left",
            validate="one_to_one",
        )
        controls_year.to_csv(output_dir / "kappa_controls_movement_year.csv", index=False)
        controls_quarter = build_quarter_controls(
            selected_kappa_quarter, movement, firm_universe
        )
        potential_quarter = build_potential_exposure_controls(
            selected_kappa_pair_quarter,
            movement,
            firm_universe,
            "yearquarter",
        )
        controls_quarter = controls_quarter.merge(
            potential_quarter,
            on=["firm_key", "event_year", "event_quarter", "data_cohort"],
            how="left",
            validate="one_to_one",
        )
        controls_quarter.to_csv(
            output_dir / "kappa_controls_movement_yearquarter.csv", index=False
        )

        movement_year = attach_event_kappa(movement, controls_year, "year")
        movement_year.to_csv(output_dir / "movement_kappa_year.csv", index=False)
        movement_yearquarter = attach_event_kappa(movement, controls_quarter, "yearquarter")
        movement_yearquarter.to_csv(
            output_dir / "movement_kappa_yearquarter.csv", index=False
        )

        summary = pd.DataFrame(
            {
                "metric": [
                    "roster_variant",
                    "personnel_definition",
                    "panel_level",
                    "mapping_rows",
                    "mapped_cusips",
                    "selected_firm_universe",
                    "holding_rows",
                    "firm_quarter_rows",
                    "firm_year_rows",
                    "pair_quarter_rows",
                    "movement_rows",
                    "movement_quarter_rows",
                    "movement_quarter_unresolved_rows",
                    "annual_control_rows",
                    "quarter_control_rows",
                ],
                "value": [
                    roster_variant,
                    personnel_definition,
                    panel_level,
                    len(selected_mapping),
                    int(selected_mapping["CUSIP"].ne("").sum()),
                    len(firm_universe),
                    len(holdings),
                    len(selected_kappa_quarter),
                    len(selected_kappa_year),
                    len(selected_kappa_pair_quarter),
                    len(movement),
                    int(movement["event_quarter"].notna().sum()),
                    int(movement["event_quarter"].isna().sum()),
                    len(controls_year),
                    len(controls_quarter),
                ],
            }
        )
        summary.to_csv(output_dir / "run_summary.csv", index=False)
        source_counts = movement["quarter_source"].value_counts(dropna=False).to_dict()
        print(
            f"[{roster_variant} | {personnel_definition}] "
            f"movement={len(movement):,}; firms={len(firm_universe):,}; "
            f"unresolved_quarters={int(movement['event_quarter'].isna().sum()):,}; "
            f"quarter_sources={source_counts}"
        )
        print(f"Outputs written to: {output_dir}")


if __name__ == "__main__":
    main()
