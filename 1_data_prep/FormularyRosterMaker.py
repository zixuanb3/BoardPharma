"""
Purpose:
    Build a quarterly formulary roster by combining BoardEx personnel (BP),
    individual employment (IE), and organization composition (OC) records.
    The standardized mapping uses mapping id as the common company identity;
    the expanded mapping retains the legacy CompanyID-to-BoardName rule.

Process:
    1. Read the selected mapping and filter BP, IE, and OC records to the
         requested inclusive year range and eligible companies.
    2. Match BP by BoardName and IE/OC by CompanyName in the standardized
         stream; the expanded stream matches IE/OC through CompanyID.
    3. Expand each observed BP director/company/year record to quarters 1-4,
         retain observed IE/OC quarters, and never create unobserved years.
    4. Combine identical standardized rows, record contributing sources in the
         fixed ie-oc-bp order, and write an audit summary. Use --audit-only to
         inspect the joins without writing the roster.

Input:
    data/boardex/individual_employment_record.csv
    data/boardex/organization_composition_record.csv
    InterimData/boardex_pharma.dta
    crosswalks/labeler_company_mapping_standardized_with_id.csv by default for
    --mapping-source standardized, or
    crosswalks/labeler_board_name_mapping_expanded.csv for expanded.
    Command-line paths can override the mapping and source files.

Output:
    data/formulary_roster/formulary_{start_year}_{end_year}.csv
    data/formulary_roster/formulary_{start_year}_{end_year}_audit.json
    Standardized output contains DirectorID, Year, Quarter, HOCountryName,
    id, and source. Expanded output retains the legacy source columns and
    reports unmatched BP CompanyName values in the audit.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
EXPANDED_COLUMNS = [
    "BoardName", "DirectorName", "DirectorID", "CompanyID", "Year",
    "Quarter", "HOCountryName", "LabelerName", "CompanyName",
]
STANDARDIZED_COLUMNS = [
    # "BoardName",  # Used to match BP, but not needed in standardized output.
    # "DirectorName",  # Not needed when standardized id identifies the company.
    "DirectorID",
    # "CompanyID",  # BoardEx CompanyID is not used in the standardized stream.
    "Year", "Quarter", "HOCountryName",
    # "LabelerName",  # Used by the mapping, but id identifies the company.
    # "CompanyName",  # Used to match IE/OC, but not needed in output.
    "id",
]
RENAME = {name.lower(): name for name in EXPANDED_COLUMNS}
SOURCE_ORDER = ("ie", "oc", "bp")
MAPPING_PATHS = {
    "standardized": ROOT / "crosswalks/labeler_company_mapping_standardized_with_id.csv",
    "expanded": ROOT / "crosswalks/labeler_board_name_mapping_expanded.csv",
}


def merge_sources(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Deduplicate on all output data columns and combine contributing sources."""
    return (
        frame.groupby(columns, dropna=False, sort=False)["source"]
        .agg(lambda values: "-".join(
            source for source in SOURCE_ORDER if source in set(values)
        ))
        .reset_index()
    )


def read_records(path: Path, start: int, end: int, ids: set[int],
                 country: bool) -> pd.DataFrame:
    """Filter the large record files in chunks before retaining them in memory."""
    columns = ["directorid", "directorname", "companyid", "companyname",
               "year", "quarter"]
    if country:
        columns.append("hocountryname")
    parts = []
    for chunk in pd.read_csv(path, usecols=columns, chunksize=250_000,
                             low_memory=False):
        selected = chunk.loc[
            chunk.year.between(start, end) & chunk.companyid.isin(ids)
        ]
        parts.append(selected.rename(columns=RENAME))
    return pd.concat(parts, ignore_index=True)


def read_standardized_records(
    path: Path,
    start: int,
    end: int,
    company_names: set[str],
    country: bool,
) -> pd.DataFrame:
    """Read period rows whose exact CompanyName appears in the standardized map."""
    columns = [
        "directorid",
        # "directorname",  # Not needed in standardized roster output.
        # "companyid",  # CompanyName, not CompanyID, determines eligibility.
        "companyname", "year", "quarter",
    ]
    if country:
        columns.append("hocountryname")
    parts = []
    for chunk in pd.read_csv(
        path,
        usecols=columns,
        chunksize=250_000,
        low_memory=False,
    ):
        selected = chunk.loc[
            chunk.year.between(start, end)
            & chunk.companyname.isin(company_names)
        ]
        parts.append(selected.rename(columns=RENAME))
    return pd.concat(parts, ignore_index=True)


def variants(frame: pd.DataFrame, column: str, key: str = "CompanyID") -> dict:
    """Report keys with multiple nonmissing attribute values."""
    grouped = frame.groupby(key)[column].agg(
        lambda values: sorted(set(values.dropna()))
    )
    return {str(key): values for key, values in grouped.items() if len(values) > 1}


def expand_bp(bp: pd.DataFrame) -> pd.DataFrame:
    """Expand observed annual records only; e.g. 2019/2021 never creates 2020."""
    keys = ["DirectorID", "CompanyID", "Year"]
    # Keep the existing deterministic choice for duplicate annual keys.
    observed = bp.sort_values(keys + ["DirectorName"], kind="stable")
    observed = observed.drop_duplicates(keys, keep="first")
    return observed.merge(
        pd.DataFrame({"Quarter": [1, 2, 3, 4]}), how="cross"
    ).sort_values(keys + ["Quarter"])


def expand_standardized_bp(bp: pd.DataFrame) -> pd.DataFrame:
    """Expand each observed DirectorID-BoardName-Year record to four quarters."""
    keys = ["DirectorID", "BoardName", "Year"]
    observed = bp.sort_values(keys, kind="stable").drop_duplicates(keys, keep="first")
    return observed.merge(
        pd.DataFrame({"Quarter": [1, 2, 3, 4]}), how="cross"
    ).sort_values(keys + ["Quarter"])


def read_mapping(path: Path, source: str) -> pd.DataFrame:
    """Read the selected mapping and retain eligible BoardName-labeler pairs."""
    if source == "expanded":
        mapping = pd.read_csv(
            path, usecols=["BoardName", "LabelerName", "audit_keep"]
        )
        keep = pd.to_numeric(mapping["audit_keep"], errors="coerce").eq(1)
        mapping = mapping.loc[keep, ["BoardName", "LabelerName"]]
    else:
        mapping = pd.read_csv(
            path,
            usecols=["LabelerName", "BoardName", "CompanyName", "id"],
        )
        mapping = mapping.dropna(subset=["id"]).copy()
        mapping["id"] = pd.to_numeric(mapping["id"], errors="raise").astype("int64")
        return mapping.drop_duplicates()
    return mapping.dropna(subset=["BoardName"]).drop_duplicates()


def validate_standardized_match_keys(mapping: pd.DataFrame) -> None:
    """Require each exact source name to identify only one company id."""
    for column in ["BoardName", "CompanyName"]:
        matched = mapping.dropna(subset=[column])
        ambiguous = matched.groupby(column)["id"].nunique()
        ambiguous = ambiguous[ambiguous.gt(1)]
        if not ambiguous.empty:
            examples = ambiguous.index.tolist()[:10]
            raise ValueError(
                f"Exact {column} values map to multiple ids; examples: {examples}"
            )


def build_expanded(args: argparse.Namespace) -> None:
    """Build the legacy expanded-mapping roster."""
    mapping = read_mapping(args.mapping, args.mapping_source)
    bp_all = pd.read_stata(args.bp, convert_categoricals=False).rename(columns={"year": "Year"})
    # Build the IE/OC crosswalk BEFORE applying any BP year filter.
    boards_all = bp_all[["CompanyID", "BoardName"]].dropna().drop_duplicates()
    boards = boards_all.loc[boards_all.BoardName.isin(mapping.BoardName)].copy()
    if boards.empty:
        raise ValueError(
            f"No full-history BP companies match {args.mapping_source} BoardNames."
        )
    if boards.CompanyID.duplicated().any():
        raise ValueError(
            f"Full-history {args.mapping_source} BP maps a CompanyID to multiple "
            "BoardNames."
        )
    ids = set(boards.CompanyID)
    bp = bp_all.loc[bp_all.Year.between(args.start_year, args.end_year)
                & bp_all.BoardName.isin(mapping.BoardName),
                ["BoardName", "DirectorName", "DirectorID", "CompanyID", "Year", "HOCountryName"]]
    if bp[["DirectorID", "CompanyID", "Year"]].isna().any().any():
        raise ValueError("BP has missing director/company/year keys.")
    bp_ids = set(bp.CompanyID)
    print("Reading and filtering IE...", flush=True)
    ie = read_records(args.ie, args.start_year, args.end_year, ids, True)
    print("Reading and filtering OC...", flush=True)
    oc = read_records(args.oc, args.start_year, args.end_year, ids, False)
    for name, frame in [("IE", ie), ("OC", oc)]:
        if frame[["DirectorID", "CompanyID", "Year", "Quarter"]].isna().any().any():
            raise ValueError(f"{name} has missing director/company/time keys.")
        if not frame.Quarter.isin([1, 2, 3, 4]).all():
            raise ValueError(f"{name} contains an invalid quarter.")
    names = pd.concat([ie, oc], ignore_index=True)[["CompanyID", "CompanyName"]]
    # Stable choice: the alphabetically first nonmissing IE/OC company name.
    chosen_names = names.dropna().sort_values(["CompanyID", "CompanyName"])
    chosen_names = chosen_names.drop_duplicates("CompanyID")
    country_conflicts = variants(ie, "HOCountryName")
    countries = ie[["CompanyID", "HOCountryName"]].dropna().drop_duplicates()
    missing_bp = bp_ids - set(chosen_names.CompanyID)
    missing_oc = set(oc.CompanyID) - set(ie.CompanyID)
    audit = {
        "start_year": args.start_year, "end_year": args.end_year,
        "mapping_source": args.mapping_source,
        "input_paths": {key: str(getattr(args, key)) for key in ["mapping", "bp", "ie", "oc"]},
        "filtered_rows": {"BP": len(bp), "IE": len(ie), "OC": len(oc)},
        "filtered_companies": {"BP": len(bp_ids), "IE": ie.CompanyID.nunique(), "OC": oc.CompanyID.nunique()},
        "ieoc_crosswalk_rule": f"CompanyID -> BoardName from full BP history, then a nonmissing BoardName in the {args.mapping_source} formulary mapping; year bounds apply only to roster rows.",
        "full_history_mapping_companies": len(ids),
        "ie_companies_absent_from_period_bp": sorted(set(ie.CompanyID) - bp_ids),
        "oc_companies_absent_from_period_bp": sorted(set(oc.CompanyID) - bp_ids),
        "bp_observed_years": sorted(bp.Year.unique().tolist()),
        "bp_expansion_rule": "Expand each observed DirectorID-CompanyID-Year into quarters 1-4; do not create missing years or fill attributes across years.",
        "bp_missing_ieoc_companyname": boards.loc[boards.CompanyID.isin(missing_bp)].to_dict("records"),
        "oc_missing_ie_companyid": sorted(missing_oc),
        "oc_missing_ie_country": sorted(set(oc.CompanyID) - set(countries.CompanyID)),
        "ie_multiple_countries": country_conflicts,
        "ie_missing_country_rows": int(ie.HOCountryName.isna().sum()),
        "ieoc_multiple_companynames": variants(names, "CompanyName"),
        "mapping_boardnames_with_multiple_labelers": int((mapping.groupby("BoardName").LabelerName.nunique() > 1).sum()),
        "deduplication_columns": EXPANDED_COLUMNS,
        "source_order": list(SOURCE_ORDER),
        "source_definition": "Roster row provenance; attribute lookups do not add sources; BP-filled quarters count as bp.",
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"formulary_roster_{args.start_year}_{args.end_year}"
    audit_path = args.output_dir / f"{stem}_audit.json"
    def save_audit() -> None:
        audit_path.write_text(json.dumps(audit, indent=2, ensure_ascii=False, default=int), encoding="utf-8")
    save_audit()
    print(json.dumps(audit, indent=2, ensure_ascii=False, default=int), flush=True)
    if args.audit_only:
        return
    if country_conflicts:
        raise ValueError(f"IE country mapping is ambiguous; inspect {audit_path}")
    bp_q = expand_bp(bp).merge(chosen_names, on="CompanyID", how="left", validate="many_to_one")
    ie = ie.merge(boards, on="CompanyID", how="left", validate="many_to_one")
    oc = oc.merge(boards, on="CompanyID", how="left", validate="many_to_one")
    oc = oc.merge(countries, on="CompanyID", how="left", validate="many_to_one")
    combined = pd.concat(
        [bp_q.assign(source="bp"), ie.assign(source="ie"), oc.assign(source="oc")],
        ignore_index=True,
    )
    combined = combined.merge(mapping, on="BoardName", how="left", validate="many_to_many")
    combined = combined[EXPANDED_COLUMNS + ["source"]]
    for column in ["DirectorID", "CompanyID", "Year", "Quarter"]:
        if not combined[column].eq(combined[column].round()).all():
            raise ValueError(f"Noninteger values in {column}")
        combined[column] = combined[column].astype("int64")
    before = len(combined)
    combined = merge_sources(combined, EXPANDED_COLUMNS).sort_values(
        ["CompanyID", "DirectorID", "Year", "Quarter", "LabelerName", "CompanyName"],
        kind="stable",
    )
    output_path = args.output_dir / f"{stem}.csv"
    combined.to_csv(output_path, index=False, encoding="utf-8-sig")
    audit.update(bp_expanded_rows=len(bp_q), rows_before_deduplication=before,
                 duplicates_removed=before-len(combined), output_rows=len(combined),
                 source_counts=combined.source.value_counts().sort_index().to_dict(),
                 output_path=str(output_path))
    save_audit()
    print(f"Wrote {len(combined):,} rows to {output_path}", flush=True)


def build_standardized(args: argparse.Namespace) -> None:
    """Build a roster using mapping id as the cross-source company identity."""
    mapping = read_mapping(args.mapping, args.mapping_source)
    validate_standardized_match_keys(mapping)
    board_mapping = mapping.dropna(subset=["BoardName"]).drop_duplicates()
    company_mapping = mapping.dropna(subset=["CompanyName"]).drop_duplicates()
    board_names = set(board_mapping["BoardName"])
    company_names = set(company_mapping["CompanyName"])

    bp_columns = [
        "BoardName",
        # "DirectorName",  # Not needed in standardized roster output.
        "DirectorID",
        # "CompanyID",  # BoardName, not CompanyID, determines eligibility.
        "year", "HOCountryName",
    ]
    bp_all = pd.read_stata(
        args.bp,
        columns=bp_columns,
        convert_categoricals=False,
    ).rename(columns={"year": "Year"})
    bp = bp_all.loc[
        bp_all.Year.between(args.start_year, args.end_year)
        & bp_all.BoardName.isin(board_names),
        ["BoardName", "DirectorID", "Year", "HOCountryName"],
    ]
    if bp[["BoardName", "DirectorID", "Year"]].isna().any().any():
        raise ValueError("BP has missing board/director/year keys.")

    print("Reading and filtering IE by CompanyName...", flush=True)
    ie = read_standardized_records(
        args.ie, args.start_year, args.end_year, company_names, True
    )
    print("Reading and filtering OC by CompanyName...", flush=True)
    oc = read_standardized_records(
        args.oc, args.start_year, args.end_year, company_names, False
    )
    for name, frame in [("IE", ie), ("OC", oc)]:
        keys = ["CompanyName", "DirectorID", "Year", "Quarter"]
        if frame[keys].isna().any().any():
            raise ValueError(f"{name} has missing company/director/time keys.")
        if not frame.Quarter.isin([1, 2, 3, 4]).all():
            raise ValueError(f"{name} contains an invalid quarter.")

    country_conflicts = variants(
        ie, "HOCountryName", key="CompanyName"
    )
    countries = ie[["CompanyName", "HOCountryName"]].dropna().drop_duplicates()
    missing_oc_country = sorted(set(oc.CompanyName) - set(countries.CompanyName))

    audit = {
        "start_year": args.start_year,
        "end_year": args.end_year,
        "mapping_source": args.mapping_source,
        "input_paths": {
            key: str(getattr(args, key)) for key in ["mapping", "bp", "ie", "oc"]
        },
        "matching_rules": {
            "BP": "Exact BoardName match to a nonmissing mapping BoardName.",
            "IE": "Exact CompanyName match to a nonmissing mapping CompanyName.",
            "OC": "Exact CompanyName match to a nonmissing mapping CompanyName.",
            "company_identity": "Mapping id; CompanyID is not used.",
        },
        "filtered_rows": {"BP": len(bp), "IE": len(ie), "OC": len(oc)},
        "filtered_names": {
            "BP_BoardName": bp.BoardName.nunique(),
            "IE_CompanyName": ie.CompanyName.nunique(),
            "OC_CompanyName": oc.CompanyName.nunique(),
        },
        "mapping_ids": mapping.id.nunique(),
        "mapping_boardnames": len(board_names),
        "mapping_companynames": len(company_names),
        "bp_observed_years": sorted(bp.Year.unique().tolist()),
        "bp_expansion_rule": "Expand each observed DirectorID-BoardName-Year into quarters 1-4; do not create missing years.",
        "oc_companynames_missing_ie_country": missing_oc_country,
        "ie_companynames_with_multiple_countries": country_conflicts,
        "ie_missing_country_rows": int(ie.HOCountryName.isna().sum()),
        "deduplication_columns": STANDARDIZED_COLUMNS,
        "source_order": list(SOURCE_ORDER),
        "source_definition": "Roster row provenance combined in fixed ie-oc-bp order for identical standardized data columns.",
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"formulary_roster_{args.start_year}_{args.end_year}"
    audit_path = args.output_dir / f"{stem}_audit.json"

    def save_audit() -> None:
        audit_path.write_text(
            json.dumps(audit, indent=2, ensure_ascii=False, default=int),
            encoding="utf-8",
        )

    save_audit()
    print(json.dumps(audit, indent=2, ensure_ascii=False, default=int), flush=True)
    if args.audit_only:
        return
    if country_conflicts:
        raise ValueError(f"IE country mapping is ambiguous; inspect {audit_path}")

    bp_q = expand_standardized_bp(bp).merge(
        board_mapping,
        on="BoardName",
        how="inner",
        validate="many_to_many",
    )
    ie = ie.merge(
        company_mapping,
        on="CompanyName",
        how="inner",
        validate="many_to_many",
    )
    oc = oc.merge(
        countries,
        on="CompanyName",
        how="left",
        validate="many_to_one",
    ).merge(
        company_mapping,
        on="CompanyName",
        how="inner",
        validate="many_to_many",
    )
    combined = pd.concat(
        [bp_q.assign(source="bp"), ie.assign(source="ie"), oc.assign(source="oc")],
        ignore_index=True,
    )
    combined = combined.dropna(subset=["id"])
    combined["id"] = pd.to_numeric(combined["id"], errors="raise").astype("int64")
    combined = combined[STANDARDIZED_COLUMNS + ["source"]]
    for column in ["DirectorID", "Year", "Quarter"]:
        if not combined[column].eq(combined[column].round()).all():
            raise ValueError(f"Noninteger values in {column}")
        combined[column] = combined[column].astype("int64")

    before = len(combined)
    combined = merge_sources(combined, STANDARDIZED_COLUMNS).sort_values(
        ["id", "DirectorID", "Year", "Quarter", "HOCountryName"],
        kind="stable",
        na_position="last",
    )
    output_path = args.output_dir / f"{stem}.csv"
    combined.to_csv(output_path, index=False, encoding="utf-8-sig")
    membership_columns = ["DirectorID", "id", "Year", "Quarter"]
    membership_summary = (
        combined.groupby(membership_columns, dropna=False)
        .agg(
            rows=("source", "size"),
            countries=("HOCountryName", lambda values: values.nunique(dropna=False)),
        )
        .reset_index()
    )
    duplicate_memberships = membership_summary.loc[membership_summary.rows.gt(1)]
    unique_memberships = len(membership_summary)
    audit.update(
        bp_expanded_rows=len(bp_q),
        rows_before_deduplication=before,
        duplicates_removed=before - len(combined),
        output_rows=len(combined),
        output_ids=combined.id.nunique(),
        unique_director_id_quarters=unique_memberships,
        nonunique_director_id_quarter_keys=len(duplicate_memberships),
        additional_rows_from_country_variants=len(combined) - unique_memberships,
        nonunique_keys_with_multiple_countries=int(
            duplicate_memberships.countries.gt(1).sum()
        ),
        source_counts=combined.source.value_counts().sort_index().to_dict(),
        output_path=str(output_path),
    )
    save_audit()
    print(f"Wrote {len(combined):,} rows to {output_path}", flush=True)


def build(args: argparse.Namespace) -> None:
    """Dispatch to the selected mapping stream."""
    if args.mapping_source == "standardized":
        build_standardized(args)
    else:
        build_expanded(args)


def main() -> None:
    """Parse inclusive year bounds and optional input/output path overrides."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=2018)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument(
        "--mapping-source",
        choices=sorted(MAPPING_PATHS),
        default="standardized",
        help="Mapping rules to use (default: expanded).",
    )
    parser.add_argument(
        "--mapping",
        type=Path,
        help="Override the default file for --mapping-source.",
    )
    parser.add_argument("--bp", type=Path, default=ROOT / "InterimData/boardex_pharma.dta")
    parser.add_argument("--ie", type=Path, default=ROOT / "data/boardex/individual_employment_record.csv")
    parser.add_argument("--oc", type=Path, default=ROOT / "data/boardex/organization_composition_record.csv")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/formulary_roster")
    parser.add_argument("--audit-only", action="store_true")
    args = parser.parse_args()
    if args.start_year > args.end_year:
        parser.error("--start-year must be <= --end-year")
    if args.mapping is None:
        args.mapping = MAPPING_PATHS[args.mapping_source]
    build(args)


if __name__ == "__main__":
    main()
