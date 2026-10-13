"""
Purpose:
    Centralize project paths and the four SSR roster variants used by the
    event-study data-preparation pipeline.

Input:
    Four roster CSV files under F:/BoardPharma/data and the original BoardPharma
    InterimData directory.

Output:
    Validated variant metadata and variant-specific output directories for the
    five data-preparation scripts.
"""

from __future__ import annotations

import os
from pathlib import Path


# The reproduction code is stored directly under <project>/_repro/code.
CODE_PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_PROJECT_ROOT = Path(
    os.environ.get("BOARDPHARMA_DATA_ROOT", r"D:\Dropbox\BoardPharma")
)
OUTPUT_PROJECT_ROOT = Path(
    os.environ.get("BOARDPHARMA_OUTPUT_ROOT", str(CODE_PROJECT_ROOT))
)
INTERIM_DATA_PATH = DATA_PROJECT_ROOT / "InterimData"
ROSTER_ROOT = Path(
    os.environ.get("SSR_ROSTER_ROOT", str(OUTPUT_PROJECT_ROOT / "data"))
)
VARIANT_OUTPUT_ROOT = Path(
    os.environ.get(
        "BOARDPHARMA_VARIANT_OUTPUT_ROOT",
        str(ROSTER_ROOT / "roster_variants"),
    )
)

# Leader-tier sample definitions used to construct separate datasets.
PERSONNEL_TIER_RULES = {
    "narrow": frozenset({"board"}),
    "medium": frozenset({"board", "csuite"}),
    "broad": frozenset({"board", "csuite", "vp_tech_hr"}),
}
PERSONNEL_DEFINITIONS = tuple(PERSONNEL_TIER_RULES)


def configured_variants() -> list[str]:
    """Read the optional roster-variant run filter from the environment."""
    values = os.environ.get("BOARDPHARMA_ROSTER_VARIANTS", "").strip()
    selected = [value.strip() for value in values.split(",") if value.strip()]
    return selected or list(ROSTER_VARIANTS)


def configured_personnel_definitions() -> list[str]:
    """Read the optional personnel-tier run filter from the environment."""
    values = os.environ.get("BOARDPHARMA_PERSONNEL_DEFINITIONS", "").strip()
    selected = [value.strip() for value in values.split(",") if value.strip()]
    return selected or list(PERSONNEL_DEFINITIONS)

ROSTER_VARIANTS = {
    "imputed_year": {
        "path": ROSTER_ROOT / "ssr_company_roster_imputed_year.csv",
        "panel_level": "year",
        "regression_panel_level": "quarter",
        "date_rule": "imputed",
    },
    "imputed_yearquarter": {
        "path": ROSTER_ROOT / "ssr_company_roster_imputed_yearquarter.csv",
        "panel_level": "quarter",
        "regression_panel_level": "quarter",
        "date_rule": "imputed",
    },
    "drop_sentinel_year": {
        "path": ROSTER_ROOT / "ssr_company_roster_drop_sentinel_year.csv",
        "panel_level": "year",
        "regression_panel_level": "quarter",
        "date_rule": "drop_sentinel",
    },
    "drop_sentinel_yearquarter": {
        "path": ROSTER_ROOT / "ssr_company_roster_drop_sentinel_yearquarter.csv",
        "panel_level": "quarter",
        "regression_panel_level": "quarter",
        "date_rule": "drop_sentinel",
    },
}


def get_variant(variant: str) -> dict[str, object]:
    """Return validated metadata for one roster variant."""
    if variant not in ROSTER_VARIANTS:
        allowed = ", ".join(sorted(ROSTER_VARIANTS))
        raise ValueError(f"Unknown roster_variant={variant}; expected one of: {allowed}")
    metadata = dict(ROSTER_VARIANTS[variant])
    path = Path(metadata["path"])
    if not path.exists():
        raise FileNotFoundError(f"Roster input does not exist: {path}")
    return metadata


def variant_output_dir(variant: str) -> Path:
    """Return the isolated output directory for one roster variant."""
    get_variant(variant)
    return VARIANT_OUTPUT_ROOT / variant


def personnel_output_dir(variant: str, personnel_definition: str) -> Path:
    """Return the isolated output directory for one roster variant and tier."""
    get_variant(variant)
    if personnel_definition not in PERSONNEL_TIER_RULES:
        allowed = ", ".join(PERSONNEL_DEFINITIONS)
        raise ValueError(
            f"Unknown personnel_definition={personnel_definition}; expected one of: {allowed}"
        )
    return VARIANT_OUTPUT_ROOT / variant / f"leader_tier_{personnel_definition}"


def ensure_variant_columns(frame, variant: str):
    """Validate and normalize the minimum roster schema used downstream."""
    metadata = get_variant(variant)
    required = {
        "DirectorID", "BoardName", "year", "inSSR", "leader_tier",
    }
    if metadata["panel_level"] == "quarter":
        required.update({"quarter", "yearquarter"})
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{variant} roster is missing required columns: {missing}")

    frame = frame.copy()
    frame["DirectorID"] = frame["DirectorID"].astype("int64")
    frame["year"] = frame["year"].astype("int64")
    frame["inSSR"] = frame["inSSR"].astype("int8")
    frame["BoardName"] = frame["BoardName"].astype(str)
    frame["leader_tier"] = frame["leader_tier"].astype("string").str.strip().str.lower()
    if metadata["panel_level"] == "quarter":
        frame["quarter"] = frame["quarter"].astype("int8")
        if not frame["quarter"].between(1, 4).all():
            raise ValueError(f"{variant} contains invalid quarter values")
    return frame
