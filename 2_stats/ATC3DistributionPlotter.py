"""
Purpose:
Generate ATC-sharing labels and diagnostics for movement event cohorts.

Process:
1. Read movement event candidates and build req-valid event partner tables.
2. Read ATC2/ATC3 product-firm mapping files.
3. Read balanced quarter-level movement cohort files from CohortPanelMaker.
4. Label treated BoardName-product rows with atc_sharing indicators.
5. Save enriched cohorts and Pure Control yearly sharing summaries.

Input:
- data/event_tables/movement_event_candidates.csv
- data/event_tables/movement_event_candidates_large_sample_{definition}.csv when LARGE_SAMPLE == 1
- data/atc3mapping/atc2mapping_year.csv
- data/atc3mapping/atc3mapping_year.csv
- data/cohort_data/quarter-level_{A|B}_{with|without}_{A|B}/event/req*/.../*_balanced.csv
- data/cohort_data/quarter-level_{A|B}_{with|without}_{A|B}_large_sample_{definition}/event/req*/.../*_balanced_large_sample_{definition}.csv when LARGE_SAMPLE == 1

Output:
- data/cohort_data_with_atcsharing_{atc2|atc3}/quarter-level*/event/req*/.../*.csv
- csv/cohort_sharing_atc_{atc2|atc3}/quarter-level*/event/req*/Pure Control/*.csv
- figures/cohort_sharing_atc_{atc2|atc3}/quarter-level*/event/req*/Pure Control/*.png
"""

from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd


PERSONNEL_DEFINITIONS = {"narrow", "medium", "broad"}

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COHORT_ROOT = PROJECT_ROOT / "data" / "cohort_data"
ATC_MAP_DIR = PROJECT_ROOT / "data" / "atc3mapping"
EVENT_TABLES_DIR = PROJECT_ROOT / "data" / "event_tables"

# ========================== USER CONFIG ==========================
ATCS = ["atc3","atc2"]
MOVEMENT_EVENTS = [
    "to_B_not_in_A",
    "to_B_still_in_A",
    "interlock_dissolution",
]
EVENT_REQUIREMENTS = [0, 1, 2]
COHORT_YEARS = list(range(2009, 2019))
TREATMENT_GROUPS = ["A", "B"]
INCLUDE_EVENTPAIR = [0]
CONTROL_FOLDERS = ["Not", "Not Yet", "Pure Control"]
PLOT_CONTROL_FOLDER = "Pure Control"
PANEL = "quarter"
EVENT_TYPE = "event"
LARGE_SAMPLE = 1
PERSONNEL_DEFINITION = "broad"
# ================================================================

def build_large_sample_suffix(large_sample: int, personnel_definition: str) -> str:
    """Return movement file suffix for the configured sample definition."""
    if large_sample not in {0, 1}:
        raise ValueError("large_sample must be 0 or 1")
    if large_sample == 0:
        return ""
    if personnel_definition not in PERSONNEL_DEFINITIONS:
        raise ValueError("personnel_definition must be one of: narrow, medium, broad")
    return f"_large_sample_{personnel_definition}"


def load_atc_mapping(atc: str) -> pd.DataFrame:
    """Load the requested ATC mapping table."""
    if atc not in {"atc2", "atc3"}:
        raise ValueError(f"ATCS only supports atc2 and atc3, got: {atc}")

    path = ATC_MAP_DIR / f"{atc}mapping_year.csv"
    if not path.exists():
        raise FileNotFoundError(f"Missing ATC mapping file: {path}")

    mapping = pd.read_csv(path)
    required = ["year", "product", "atc3", "BoardName", "BoardNamePair"]
    missing = [col for col in required if col not in mapping.columns]
    if missing:
        raise KeyError(f"{path} is missing required columns: {missing}")

    mapping = mapping[required].dropna(subset=required).drop_duplicates().copy()
    if atc == "atc2":
        mapping = mapping.rename(columns={"atc3": "atc2"})
    mapping["year"] = pd.to_numeric(mapping["year"], errors="raise").astype(int)
    mapping[atc] = mapping[atc].astype(str)
    return mapping


def build_partner_table(
    movement: pd.DataFrame,
    treatment_group: str,
    event_requirement: int,
) -> pd.DataFrame:
    """Build req-valid BoardName to BoardNamePair movement partners."""
    treatment_group = str(treatment_group).upper()
    if treatment_group not in {"A", "B"}:
        raise ValueError(f"treatment_group must be A or B, got: {treatment_group}")
    if event_requirement not in {0, 1, 2}:
        raise ValueError(f"event_requirement must be 0, 1, or 2, got: {event_requirement}")

    required = [
        "event_type",
        "event_year",
        "FirmA",
        "FirmB",
        "stay_2_years",
        "requirement1",
        "requirement2_A",
        "requirement2_B",
    ]
    missing = [col for col in required if col not in movement.columns]
    if missing:
        raise KeyError(f"movement candidates are missing required columns: {missing}")

    data = movement.copy()
    int_cols = ["event_year", "stay_2_years", "requirement1", "requirement2_A", "requirement2_B"]
    for col in int_cols:
        data[col] = pd.to_numeric(data[col], errors="raise").astype(int)

    data = data[data["event_type"].isin(MOVEMENT_EVENTS)].copy()
    data = data[data["stay_2_years"].eq(1)].copy()
    if event_requirement >= 1:
        data = data[data["requirement1"].eq(1)].copy()
    if event_requirement == 2:
        side_col = f"requirement2_{treatment_group}"
        data = data[data[side_col].eq(1)].copy()

    treated_col = "FirmA" if treatment_group == "A" else "FirmB"
    partner_col = "FirmB" if treatment_group == "A" else "FirmA"
    partners = (
        data.rename(
            columns={
                "event_type": "event",
                "event_year": "year",
                treated_col: "BoardName",
                partner_col: "BoardNamePair",
            }
        )[["event", "year", "BoardName", "BoardNamePair"]]
        .dropna(subset=["event", "year", "BoardName", "BoardNamePair"])
        .drop_duplicates()
        .reset_index(drop=True)
    )
    partners["year"] = partners["year"].astype(int)
    return partners


def plot_summary(summary: pd.DataFrame, out_png: Path, title: str) -> None:
    """Save a stacked yearly treated sharing diagnostic plot."""
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar(summary["Year"], summary["sharing_atc"], label="sharing_atc")
    ax.bar(
        summary["Year"],
        summary["not_sharing_atc"],
        bottom=summary["sharing_atc"],
        label="not_sharing_atc",
    )
    ax.set_xlabel("Year")
    ax.set_ylabel("Treated BoardName-product count")
    ax.set_title(title)
    ax.legend()
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(out_png, dpi=300)
    plt.close(fig)


def process_one_config(
    atc: str,
    mapping: pd.DataFrame,
    partners: pd.DataFrame,
    treatment_group: str,
    include_eventpair: int,
    event_requirement: int,
    event: str,
    movement_suffix: str,
) -> None:
    """Label one treatment-side/event/req cohort set and save diagnostics."""
    counterpart = "B" if treatment_group == "A" else "A"
    relation = "with" if int(include_eventpair) == 1 else "without"
    cohort_folder = f"{PANEL}-level_{treatment_group}_{relation}_{counterpart}{movement_suffix}"
    req_folder = f"req{event_requirement}"
    data_out_root = PROJECT_ROOT / "data" / f"cohort_data_with_atcsharing_{atc}"
    fig_root = PROJECT_ROOT / "figures" / f"cohort_sharing_atc_{atc}"
    csv_root = PROJECT_ROOT / "csv" / f"cohort_sharing_atc_{atc}"

    rows = []
    for control_folder in CONTROL_FOLDERS:
        src_dir = COHORT_ROOT / cohort_folder / EVENT_TYPE / req_folder / control_folder
        dst_dir = data_out_root / cohort_folder / EVENT_TYPE / req_folder / control_folder

        for cohort_year in COHORT_YEARS:
            src_path = src_dir / f"{event}_{PANEL}_cohort_{cohort_year}_balanced{movement_suffix}.csv"
            if not src_path.exists():
                if control_folder == PLOT_CONTROL_FOLDER:
                    rows.append(
                        {
                            "Year": cohort_year,
                            "sharing_atc": 0,
                            "not_sharing_atc": 0,
                            "total": 0,
                        }
                    )
                continue

            cohort = pd.read_csv(src_path)
            original_rows = len(cohort)
            required_cols = ["BoardName", "year", "quarter", "product", "atc3", f"event_{cohort_year}"]
            missing = [col for col in required_cols if col not in cohort.columns]
            if missing:
                raise KeyError(f"{src_path} is missing required columns: {missing}")

            event_col = f"event_{cohort_year}"
            if atc == "atc2":
                atc2 = cohort["atc3"].astype(str).str[:-1]
                if "atc2" in cohort.columns:
                    cohort["atc2"] = atc2
                else:
                    insert_at = cohort.columns.get_loc("atc3") + 1
                    cohort.insert(insert_at, "atc2", atc2)
                cohort = cohort.drop(columns=["atc3"])

            cohort["_row_order"] = range(len(cohort))
            work = cohort[["BoardName", "year", "quarter", "product", atc, event_col]].copy()
            work["year"] = pd.to_numeric(work["year"], errors="raise").astype(int)
            work["quarter"] = pd.to_numeric(work["quarter"], errors="raise").astype(int)
            work[event_col] = pd.to_numeric(work[event_col], errors="coerce").fillna(0).astype(int)
            work[atc] = work[atc].astype(str)

            treated = work[
                work[event_col].eq(1)
                & work["year"].eq(cohort_year)
                & work["quarter"].eq(1)
            ][["BoardName", "year", "product", atc]].drop_duplicates()

            sharing_pairs: set[tuple[str, str]] = set()
            if not treated.empty:
                candidates = treated.merge(
                    mapping,
                    on=["BoardName", "year", "product", atc],
                    how="left",
                ).dropna(subset=["BoardNamePair"])

                valid_partners = partners[
                    partners["event"].eq(event) & partners["year"].eq(cohort_year)
                ][["BoardName", "BoardNamePair"]].drop_duplicates()

                matched = candidates.merge(
                    valid_partners,
                    on=["BoardName", "BoardNamePair"],
                    how="inner",
                )
                if not matched.empty:
                    sharing_pairs = set(
                        matched[["BoardName", "product"]]
                        .drop_duplicates()
                        .itertuples(index=False, name=None)
                    )

            cohort["atc_sharing"] = 0
            if sharing_pairs:
                pair_index = pd.MultiIndex.from_tuples(sharing_pairs, names=["BoardName", "product"])
                cohort_pairs = pd.MultiIndex.from_frame(cohort[["BoardName", "product"]])
                cohort.loc[cohort_pairs.isin(pair_index), "atc_sharing"] = 1

            control_mask = pd.to_numeric(cohort[event_col], errors="coerce").fillna(0).astype(int).ne(1)
            cohort.loc[control_mask, "atc_sharing"] = 0
            cohort = cohort.sort_values("_row_order").drop(columns=["_row_order"])

            if len(cohort) != original_rows:
                raise AssertionError(f"Row count changed for {src_path}")
            if cohort.loc[control_mask, "atc_sharing"].sum() != 0:
                raise AssertionError(f"Control rows have atc_sharing == 1 in {src_path}")

            dst_dir.mkdir(parents=True, exist_ok=True)
            out_path = dst_dir / f"{src_path.stem}_{atc}.csv"
            cohort.to_csv(out_path, index=False)

            if control_folder != PLOT_CONTROL_FOLDER:
                continue

            treated_units = (
                cohort[
                    cohort[event_col].eq(1)
                    & cohort["year"].eq(cohort_year)
                    & cohort["quarter"].eq(1)
                ][["BoardName", "product", "atc_sharing"]]
                .drop_duplicates(subset=["BoardName", "product"])
                .copy()
            )
            sharing_count = int(treated_units["atc_sharing"].sum())
            total = int(len(treated_units))
            not_sharing_count = total - sharing_count
            rows.append(
                {
                    "Year": cohort_year,
                    "sharing_atc": sharing_count,
                    "not_sharing_atc": not_sharing_count,
                    "total": total,
                }
            )

    summary = pd.DataFrame(rows, columns=["Year", "sharing_atc", "not_sharing_atc", "total"])
    if not summary["total"].eq(summary["sharing_atc"] + summary["not_sharing_atc"]).all():
        raise AssertionError(
            f"Summary total mismatch for {atc}/{cohort_folder}/{req_folder}/{event}"
        )

    fig_dir = fig_root / cohort_folder / EVENT_TYPE / req_folder / PLOT_CONTROL_FOLDER
    csv_dir = csv_root / cohort_folder / EVENT_TYPE / req_folder / PLOT_CONTROL_FOLDER
    csv_dir.mkdir(parents=True, exist_ok=True)
    stem = f"sharing_{atc}_{event}_{PANEL}_{cohort_folder}_{req_folder}"
    out_csv = csv_dir / f"{stem}.csv"
    summary.to_csv(out_csv, index=False)

    plot_summary(
        summary=summary,
        out_png=fig_dir / f"{stem}.png",
        title=f"{event} | {cohort_folder} | {req_folder} | {atc}",
    )
    print(f"Saved: {out_csv}")


def main() -> None:
    invalid_atcs = [atc for atc in ATCS if atc not in {"atc2", "atc3"}]
    if invalid_atcs:
        raise ValueError(f"ATCS only supports atc2 and atc3, got: {invalid_atcs}")

    movement_suffix = build_large_sample_suffix(int(LARGE_SAMPLE), str(PERSONNEL_DEFINITION))
    movement_candidates_path = EVENT_TABLES_DIR / f"movement_event_candidates{movement_suffix}.csv"
    movement = pd.read_csv(movement_candidates_path)
    for atc in ATCS:
        mapping = load_atc_mapping(atc)
        print(f"Processing {atc}")

        for treatment_group in TREATMENT_GROUPS:
            treatment_group = treatment_group.upper()
            for include_eventpair in INCLUDE_EVENTPAIR:
                if int(include_eventpair) not in {0, 1}:
                    raise ValueError(f"include_eventpair must be 0 or 1, got: {include_eventpair}")
                for event_requirement in EVENT_REQUIREMENTS:
                    partners = build_partner_table(
                        movement=movement,
                        treatment_group=treatment_group,
                        event_requirement=int(event_requirement),
                    )
                    for event in MOVEMENT_EVENTS:
                        process_one_config(
                            atc=atc,
                            mapping=mapping,
                            partners=partners,
                            treatment_group=treatment_group,
                            include_eventpair=int(include_eventpair),
                            event_requirement=int(event_requirement),
                            event=event,
                            movement_suffix=movement_suffix,
                        )


if __name__ == "__main__":
    main()
