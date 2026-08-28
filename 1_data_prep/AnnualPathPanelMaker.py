r"""
Purpose:
Build regression-ready annual upgrade and downgrade cohorts from complete
formulary paths.  The annual design keeps the current path-level req1/Not
sample, ATC3-sharing fields, and CPS-frequency weights while replacing four
quarterly observations with one calendar-year observation.

Process:
1. Read shifted plan information and quarterly formulary inputs for the 2021--
   2024 cohorts, where each cohort covers event years t-2 through t+1.
2. Keep CPS units with plan and formulary-source coverage in every observable
   cohort quarter, then collapse identical within-cohort formulary sequences
   to history_id paths and record their represented CPS count in n_path.
3. Stream the required formulary quarters to narrow, resumable path-NDC staging
   files.  The shifted 2021 cohort has 15 quarters because 2019Q1 is absent;
   the other cohorts have 16 quarters.
4. Keep a path-NDC only when included=1 and tier_raw is observed in every
   observable cohort quarter.  Set both t-2 outcomes to zero.
5. For t-1, t, and t+1, set tier_upgrade to one when any current-year quarter
   has a lower tier number than the preceding calendar year's Q4 tier; define
   tier_downgrade analogously using a higher tier number.  Both may equal one.
6. Apply the existing quarterly req1/Not treated-control rules for all three
   events and both directions, then write one row per path-NDC-cohort-year.

Input:
- InterimData/merged_plan_information.csv
- D:/task1_expanded_brand_panel/task1_expanded_brand_panel.csv
- data/formulary_panel_by_time/shift_q1/formulary_panel_YYYYQX.csv
- data/event_tables/movement_table_formulary_large_sample_narrow.csv
- data/event_tables/movement_event_candidates_formulary_large_sample_narrow.csv
- 1_data_prep/PlanPanelMaker.py

Output:
- D:/BoardPharma/data/formulary_path_year_cohort_data/event/req1/Not/
  shift_q1/state/{event}_path_year_cohort_{2021|2022|2023|2024}.csv
- D:/BoardPharma/data/formulary_path_year_cohort_data/event/req1/Not/
  shift_q1/state/_quarter_staging/cohort_{year}/YYYYQX.csv
- D:/BoardPharma/data/formulary_path_year_cohort_data/event/req1/Not/
  shift_q1/state/_annual_path_checkpoint.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from tqdm.auto import tqdm

import PlanPanelMaker as quarterly


COHORT_YEARS = (2021, 2022, 2023, 2024)
YEAR_OFFSETS = (-2, -1, 0, 1)
FIRST_ANNUAL_OFFSET = min(YEAR_OFFSETS)
EVENT_TYPES = quarterly.EVENT_TYPES


# ========================== USER CONFIG ==========================
# level:
# - Must be "state" to reproduce the current path-level regressions.
#
# formulary_time_shift_quarters:
# - Must be 1.  Plan information is shifted forward one quarter and the script
#   reads the already aligned shift_q1 formulary panels.
#
# chunksize:
# - Controls the number of raw formulary rows read at once.  It changes memory
#   use and runtime only; it does not change path definitions or annual values.
RUN_CONFIG: dict[str, object] = {
    "level": "state",
    "formulary_time_shift_quarters": 1,
    "chunksize": 2_000_000,
}
# ===============================================================

OUTPUT_ROOT = (
    Path(r"D:\BoardPharma\data")
    / "formulary_path_year_cohort_data"
    / "event"
    / "req1"
    / "Not"
)

ID_COLUMNS = ["history_id", "data_cohort", "ndc"]
QUARTER_ID_COLUMNS = [*ID_COLUMNS, "year", "quarter"]
ANNUAL_ID_COLUMNS = [*ID_COLUMNS, "year"]
STAGING_COLUMNS = [
    "history_id",
    "data_cohort",
    "n_path",
    "ndc",
    "boardname",
    "included",
    "tier_raw",
    *quarterly.EVENT_COLUMNS,
    *quarterly.SHARING_COLUMNS,
    "year",
    "quarter",
]


def annual_quarters(
    cohort_year: int,
    available: set[str],
    time_shift: int,
) -> tuple[str, ...]:
    """Return observable quarters from t-2 through t+1 for one cohort."""
    nominal = [
        f"{year}Q{quarter}"
        for year in range(cohort_year - 2, cohort_year + 2)
        for quarter in range(1, 5)
    ]
    missing = set(nominal) - available
    allowed_missing = {"2019Q1"} if time_shift == 1 and cohort_year == 2021 else set()
    unexpected = missing - allowed_missing
    if unexpected:
        raise FileNotFoundError(
            f"Cohort {cohort_year} is missing annual-path quarters: "
            f"{sorted(unexpected, key=quarterly.quarter_key)}"
        )
    quarters = tuple(tag for tag in nominal if tag in available)
    expected = 15 if allowed_missing else 16
    if len(quarters) != expected:
        raise ValueError(
            f"Cohort {cohort_year} has {len(quarters)} quarters; expected {expected}."
        )
    return quarters


def quarterly_sample_windows(
    available: set[str],
    time_shift: int,
) -> dict[int, tuple[str, ...]]:
    """Retain the current t-1 through t+1 req1/Not firm-sample definition."""
    return {
        cohort: tuple(quarterly.cohort_quarters(cohort, available, time_shift))
        for cohort in COHORT_YEARS
    }


def output_directory(level: str, time_shift: int) -> Path:
    """Return the annual path output directory."""
    return OUTPUT_ROOT / quarterly.shift_label(time_shift) / level


def output_path(directory: Path, event_type: str, cohort_year: int) -> Path:
    """Return one final event-by-cohort annual CSV path."""
    return directory / f"{event_type}_path_year_cohort_{cohort_year}.csv"


def staging_path(directory: Path, cohort_year: int, year_q: str) -> Path:
    """Return one narrow path-NDC cohort-quarter staging path."""
    return directory / "_quarter_staging" / f"cohort_{cohort_year}" / f"{year_q}.csv"


def checkpoint_path(directory: Path) -> Path:
    """Return the resumable quarterly-build checkpoint path."""
    return directory / "_annual_path_checkpoint.json"


def load_checkpoint(path: Path) -> list[str]:
    """Read and validate the list of fully committed staging quarters."""
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    completed = payload.get("completed_quarters")
    if not isinstance(completed, list) or not all(isinstance(tag, str) for tag in completed):
        raise ValueError(f"Invalid annual path checkpoint: {path}")
    return completed


def write_checkpoint(path: Path, completed_quarters: list[str]) -> None:
    """Atomically record committed staging quarters."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"completed_quarters": completed_quarters}, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def relevant_cohorts(
    year_q: str,
    cohort_windows: dict[int, tuple[str, ...]],
) -> list[int]:
    """Return cohorts whose four-year path contains one quarter."""
    return [cohort for cohort, quarters in cohort_windows.items() if year_q in quarters]


def initialize_quarter_outputs(
    directory: Path,
    year_q: str,
    cohorts: list[int],
) -> tuple[dict[int, Path], dict[int, bool], dict[int, int]]:
    """Remove partial files and initialize per-cohort staging state."""
    paths = {cohort: staging_path(directory, cohort, year_q) for cohort in cohorts}
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
    return paths, {cohort: False for cohort in cohorts}, {cohort: 0 for cohort in cohorts}


def build_quarter_staging(
    source_path: Path,
    path_cohorts: dict[int, quarterly.PathCohort],
    cohort_windows: dict[int, tuple[str, ...]],
    directory: Path,
    chunksize: int,
) -> None:
    """Write one raw formulary quarter at narrow path-NDC level."""
    year_q = source_path.stem.removeprefix("formulary_panel_")
    cohorts = relevant_cohorts(year_q, cohort_windows)
    if not cohorts:
        return

    histories = pd.concat(
        [
            path_cohorts[cohort].history_quarters.loc[
                path_cohorts[cohort].history_quarters["year_q"].eq(year_q),
                ["history_id", "data_cohort", "n_path", "year_q", "formulary_id"],
            ]
            for cohort in cohorts
        ],
        ignore_index=True,
    )
    if histories.empty:
        raise ValueError(f"No path histories require {year_q}.")
    used_formularies = set(histories["formulary_id"].dropna().astype(str))
    paths, headers_written, row_counts = initialize_quarter_outputs(
        directory,
        year_q,
        cohorts,
    )

    reader = pd.read_csv(
        source_path,
        usecols=quarterly.RAW_FORMULARY_COLUMNS,
        dtype="string",
        chunksize=chunksize,
    )
    progress = tqdm(reader, desc=f"Annual path staging {year_q}", unit="chunk", leave=False)
    for raw_chunk in progress:
        raw = quarterly.normalize_raw_chunk(raw_chunk, source_path.name)
        raw = raw.loc[raw["formulary_id"].isin(used_formularies)].copy()
        if raw.empty:
            continue
        expanded = raw.merge(
            histories,
            on=["year_q", "formulary_id"],
            how="inner",
            validate="many_to_many",
        )
        if expanded.empty:
            continue
        expanded["year"] = np.int16(int(year_q[:4]))
        expanded["quarter"] = np.int8(int(year_q[-1]))
        if expanded[["history_id", "ndc", "boardname"]].isna().any().any():
            raise ValueError(f"{source_path.name} produced missing annual path identifiers.")

        for cohort, cohort_data in expanded.groupby("data_cohort", sort=False):
            cohort_int = int(cohort)
            if cohort_int not in paths:
                raise ValueError(f"Unexpected cohort {cohort_int} while staging {year_q}.")
            cohort_data[STAGING_COLUMNS].to_csv(
                paths[cohort_int],
                mode="a",
                index=False,
                header=not headers_written[cohort_int],
            )
            headers_written[cohort_int] = True
            row_counts[cohort_int] += len(cohort_data)
        progress.set_postfix_str(f"rows={sum(row_counts.values()):,}")

    empty = [cohort for cohort, wrote in headers_written.items() if not wrote]
    if empty:
        raise ValueError(f"No annual path staging rows for {year_q}, cohorts {empty}.")


def read_staging_year(
    directory: Path,
    cohort_year: int,
    calendar_year: int,
    quarter_tags: tuple[str, ...],
) -> pd.DataFrame:
    """Read and validate all observable staging quarters in one calendar year."""
    tags = tuple(tag for tag in quarter_tags if int(tag[:4]) == calendar_year)
    if not tags:
        raise ValueError(f"No staged quarters for cohort {cohort_year}, year {calendar_year}.")
    frames = [
        pd.read_csv(
            staging_path(directory, cohort_year, tag),
            dtype={
                "history_id": "string",
                "ndc": "string",
                "boardname": "string",
            },
        )
        for tag in tags
    ]
    data = pd.concat(frames, ignore_index=True)
    duplicate = data.duplicated(QUARTER_ID_COLUMNS, keep=False)
    if duplicate.any():
        examples = data.loc[duplicate, QUARTER_ID_COLUMNS].head(10)
        raise ValueError(
            f"Duplicate annual path quarter rows for cohort {cohort_year}/{calendar_year}:\n"
            f"{examples}"
        )
    observed_tags = set(
        data["year"].astype(int).astype(str) + "Q" + data["quarter"].astype(int).astype(str)
    )
    if observed_tags != set(tags):
        raise ValueError(
            f"Unexpected staging coverage for cohort {cohort_year}/{calendar_year}: "
            f"{sorted(observed_tags)}"
        )
    return data


def assert_group_constant(data: pd.DataFrame, column: str) -> None:
    """Require a column to be constant within path-NDC-cohort."""
    counts = data.groupby(ID_COLUMNS, dropna=False)[column].nunique(dropna=False)
    if counts.gt(1).any():
        examples = counts[counts.gt(1)].head(10).reset_index()
        raise ValueError(f"{column} varies within annual path-NDC units:\n{examples}")


def aggregate_calendar_year(
    data: pd.DataFrame,
    calendar_year: int,
    first_year: bool,
    previous_q4: pd.DataFrame | None,
    expected_quarters: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Collapse path-NDC quarters to annual any-upgrade/any-downgrade rows."""
    for column in ("n_path", "boardname"):
        assert_group_constant(data, column)

    aggregation: dict[str, tuple[str, str]] = {
        "n_path": ("n_path", "first"),
        "boardname": ("boardname", "first"),
        "n_quarters": ("quarter", "nunique"),
        "included_all": ("included", "min"),
        "tier_nonmissing_count": ("tier_raw", "count"),
    }
    for column in quarterly.EVENT_COLUMNS:
        aggregation[column] = (column, "max")
    annual = data.groupby(ID_COLUMNS, as_index=False, dropna=False).agg(**aggregation)
    annual["year"] = np.int16(calendar_year)
    annual["expected_quarters"] = np.int8(expected_quarters)

    q4 = data.loc[data["quarter"].eq(4), [*ID_COLUMNS, "tier_raw"]].rename(
        columns={"tier_raw": "q4_tier_raw"}
    )
    if q4.duplicated(ID_COLUMNS).any():
        raise ValueError(f"Q4 tier is not unique for calendar year {calendar_year}.")
    annual = annual.merge(q4, on=ID_COLUMNS, how="left", validate="one_to_one")

    if first_year:
        annual["prior_year_q4_tier"] = np.nan
        annual["tier_upgrade"] = np.int8(0)
        annual["tier_downgrade"] = np.int8(0)
    else:
        if previous_q4 is None:
            raise ValueError(f"Missing previous-Q4 lookup for calendar year {calendar_year}.")
        current = data.merge(previous_q4, on=ID_COLUMNS, how="left", validate="many_to_one")
        current["upgrade_vs_prior_q4"] = (
            current["tier_raw"].notna()
            & current["prior_year_q4_tier"].notna()
            & current["tier_raw"].lt(current["prior_year_q4_tier"])
        )
        current["downgrade_vs_prior_q4"] = (
            current["tier_raw"].notna()
            & current["prior_year_q4_tier"].notna()
            & current["tier_raw"].gt(current["prior_year_q4_tier"])
        )
        movements = current.groupby(ID_COLUMNS, as_index=False, dropna=False).agg(
            tier_upgrade=("upgrade_vs_prior_q4", "max"),
            tier_downgrade=("downgrade_vs_prior_q4", "max"),
            prior_year_q4_tier=("prior_year_q4_tier", "first"),
        )
        annual = annual.merge(movements, on=ID_COLUMNS, how="left", validate="one_to_one")
        annual["tier_upgrade"] = annual["tier_upgrade"].astype("int8")
        annual["tier_downgrade"] = annual["tier_downgrade"].astype("int8")

    next_q4 = q4.rename(columns={"q4_tier_raw": "prior_year_q4_tier"})
    return annual, next_q4


def complete_annual_ids(annual: pd.DataFrame) -> pd.MultiIndex:
    """Return path-NDCs satisfying the quarterly intensive-margin candidate rule."""
    candidates = annual[ID_COLUMNS].copy()
    candidates["row_complete"] = (
        annual["n_quarters"].eq(annual["expected_quarters"])
        & annual["included_all"].eq(1)
        & annual["tier_nonmissing_count"].eq(annual["expected_quarters"])
    )
    counts = candidates.groupby(ID_COLUMNS, dropna=False)["row_complete"].sum()
    complete = counts[counts.eq(len(YEAR_OFFSETS))].reset_index()[ID_COLUMNS]
    return pd.MultiIndex.from_frame(complete.astype("string").fillna("<MISSING>"))


def id_index(data: pd.DataFrame) -> pd.MultiIndex:
    """Return a missing-safe path-NDC-cohort index."""
    return pd.MultiIndex.from_frame(data[ID_COLUMNS].astype("string").fillna("<MISSING>"))


def cohort_q1_sharing(
    directory: Path,
    cohort_year: int,
) -> pd.DataFrame:
    """Return event-year Q1 sharing classifications for every path-NDC."""
    source = staging_path(directory, cohort_year, f"{cohort_year}Q1")
    columns = [*ID_COLUMNS, *quarterly.SHARING_COLUMNS]
    sharing = pd.read_csv(
        source,
        usecols=columns,
        dtype={"history_id": "string", "ndc": "string"},
    )
    if sharing.duplicated(ID_COLUMNS).any():
        examples = sharing.loc[sharing.duplicated(ID_COLUMNS, keep=False), ID_COLUMNS].head(10)
        raise ValueError(f"Cohort-Q1 sharing is not unique for {cohort_year}:\n{examples}")
    return sharing


def add_event_sample(
    annual: pd.DataFrame,
    sample: quarterly.CohortSample,
) -> pd.DataFrame:
    """Apply the current req1/Not A- and B-side firm sample definitions."""
    boardname = annual["boardname"]
    treated_a = boardname.isin(sample.treated_a).astype("int8")
    treated_b = boardname.isin(sample.treated_b).astype("int8")
    sample_a = (treated_a.eq(1) | ~boardname.isin(sample.excluded_controls_a)).astype("int8")
    sample_b = (treated_b.eq(1) | ~boardname.isin(sample.excluded_controls_b)).astype("int8")
    keep = sample_a.eq(1) | sample_b.eq(1)
    result = annual.loc[keep].copy()
    result["treated_a"] = treated_a.loc[keep]
    result["treated_b"] = treated_b.loc[keep]
    result["sample_a"] = sample_a.loc[keep]
    result["sample_b"] = sample_b.loc[keep]
    return result


def write_event_outputs(
    annual: pd.DataFrame,
    sharing: pd.DataFrame,
    samples: dict[tuple[str, int], quarterly.CohortSample],
    directory: Path,
    cohort_year: int,
) -> None:
    """Write all three event-specific annual cohort files."""
    for event_type in EVENT_TYPES:
        own_sharing = {
            f"event_{event_type}_A_sharingATC3": "cohort_sharing_a",
            f"event_{event_type}_B_sharingATC3": "cohort_sharing_b",
        }
        selected_sharing = sharing[[*ID_COLUMNS, *own_sharing]].rename(columns=own_sharing)
        output = annual.merge(selected_sharing, on=ID_COLUMNS, how="left", validate="many_to_one")
        output = add_event_sample(output, samples[(event_type, cohort_year)])
        if output[["cohort_sharing_a", "cohort_sharing_b"]].isna().any().any():
            raise ValueError(f"Missing cohort-Q1 sharing for {event_type}/{cohort_year}.")
        for side in ("a", "b"):
            output[f"cohort_sharing_{side}"] = output[f"cohort_sharing_{side}"].astype("int8")
            output.loc[
                output[f"treated_{side}"].eq(0),
                f"cohort_sharing_{side}",
            ] = np.int8(0)
        output["rel_year"] = (output["year"] - cohort_year).astype("int8")
        output["included"] = np.int8(1)
        output = output[
            [
                "history_id",
                "data_cohort",
                "n_path",
                "ndc",
                "boardname",
                "year",
                "rel_year",
                "included",
                "tier_upgrade",
                "tier_downgrade",
                "treated_a",
                "treated_b",
                "sample_a",
                "sample_b",
                "cohort_sharing_a",
                "cohort_sharing_b",
                "n_quarters",
                "expected_quarters",
                "prior_year_q4_tier",
                *quarterly.EVENT_COLUMNS,
            ]
        ].sort_values(["history_id", "ndc", "year"])
        if output.duplicated(ANNUAL_ID_COLUMNS).any():
            raise ValueError(f"Duplicate annual rows for {event_type}/{cohort_year}.")
        counts = output.groupby(ID_COLUMNS, dropna=False)["year"].nunique()
        if not counts.eq(len(YEAR_OFFSETS)).all():
            raise ValueError(f"Unbalanced four-year output for {event_type}/{cohort_year}.")
        first_year = output["year"].eq(cohort_year + FIRST_ANNUAL_OFFSET)
        if not (
            output.loc[first_year, "tier_upgrade"].eq(0).all()
            and output.loc[first_year, "tier_downgrade"].eq(0).all()
        ):
            raise ValueError(f"First annual outcomes are not zero for {event_type}/{cohort_year}.")
        destination = output_path(directory, event_type, cohort_year)
        destination.parent.mkdir(parents=True, exist_ok=True)
        output.to_csv(destination, index=False)
        print(f"Wrote {len(output):,} annual rows: {destination}", flush=True)


def aggregate_cohort(
    directory: Path,
    cohort_year: int,
    quarter_tags: tuple[str, ...],
    samples: dict[tuple[str, int], quarterly.CohortSample],
) -> None:
    """Aggregate one four-year path cohort and write its three event files."""
    annual_frames: list[pd.DataFrame] = []
    previous_q4: pd.DataFrame | None = None
    for offset in YEAR_OFFSETS:
        calendar_year = cohort_year + offset
        data = read_staging_year(directory, cohort_year, calendar_year, quarter_tags)
        expected_quarters = sum(int(tag[:4]) == calendar_year for tag in quarter_tags)
        annual, previous_q4 = aggregate_calendar_year(
            data,
            calendar_year,
            first_year=offset == FIRST_ANNUAL_OFFSET,
            previous_q4=previous_q4,
            expected_quarters=expected_quarters,
        )
        annual_frames.append(annual)

    annual = pd.concat(annual_frames, ignore_index=True)
    complete_ids = complete_annual_ids(annual)
    annual = annual.loc[id_index(annual).isin(complete_ids)].copy()
    if annual.empty:
        raise ValueError(f"No complete annual path-NDC candidates for cohort {cohort_year}.")
    counts = annual.groupby(ID_COLUMNS, dropna=False)["year"].nunique()
    if not counts.eq(len(YEAR_OFFSETS)).all():
        raise ValueError(f"Complete annual candidate screen failed for cohort {cohort_year}.")
    if annual.loc[annual["included_all"].eq(1), "tier_nonmissing_count"].lt(
        annual.loc[annual["included_all"].eq(1), "expected_quarters"]
    ).any():
        raise ValueError(f"Included path-NDC has missing tier_raw in cohort {cohort_year}.")
    sharing = cohort_q1_sharing(directory, cohort_year)
    write_event_outputs(annual, sharing, samples, directory, cohort_year)


def parse_arguments() -> argparse.Namespace:
    """Parse resumable-build command-line options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Keep committed quarter staging files and continue from the checkpoint.",
    )
    return parser.parse_args()


def main() -> None:
    """Build annual path-level upgrade/downgrade panels for all event cohorts."""
    arguments = parse_arguments()
    spec = quarterly.analysis_spec(RUN_CONFIG)
    time_shift = int(str(RUN_CONFIG["formulary_time_shift_quarters"]))
    chunksize = int(str(RUN_CONFIG["chunksize"]))
    if time_shift != 1 or spec.level != "state":
        raise ValueError("The annual path design requires shift_q1 state-level inputs.")

    all_source_paths = quarterly.quarter_panel_paths(quarterly.formulary_input_dir(time_shift))
    available = set(all_source_paths)
    cohort_windows = {
        cohort: annual_quarters(cohort, available, time_shift) for cohort in COHORT_YEARS
    }
    target_quarters = set().union(*cohort_windows.values())
    source_paths = {tag: all_source_paths[tag] for tag in target_quarters}
    directory = output_directory(spec.level, time_shift)
    directory.mkdir(parents=True, exist_ok=True)

    print("[annual path] Loading complete CPS-quarter-formulary crosswalk...", flush=True)
    crosswalks = quarterly.build_plan_crosswalks(spec, time_shift, target_quarters)
    availability = quarterly.load_formulary_quarter_availability(
        quarterly.EXPANDED_FORMULARY_PATH,
        time_shift,
        target_quarters,
        chunksize,
    )
    balanced_units = quarterly.balanced_plan_units(
        crosswalks,
        cohort_windows,
        spec,
        formulary_availability=availability,
    )
    path_cohorts = quarterly.build_path_cohorts(
        crosswalks,
        balanced_units,
        cohort_windows,
        spec,
    )
    for cohort, path_cohort in sorted(path_cohorts.items()):
        histories = path_cohort.history_quarters["history_id"].nunique()
        represented_cps = int(
            path_cohort.history_quarters["n_path"].sum()
            / len(path_cohort.required_quarters)
        )
        print(
            f"[annual path] Cohort {cohort}: {represented_cps:,} CPS in "
            f"{histories:,} four-year paths.",
            flush=True,
        )

    # Firm treatment/control definitions stay identical to the current
    # quarterly regressions even though the annual outcome adds t-2 data.
    samples = quarterly.build_samples(quarterly_sample_windows(available, time_shift))

    checkpoint = checkpoint_path(directory)
    completed = load_checkpoint(checkpoint) if arguments.resume else []
    if not arguments.resume:
        checkpoint.unlink(missing_ok=True)
    ordered_quarters = sorted(target_quarters, key=quarterly.quarter_key)
    for position, year_q in enumerate(ordered_quarters, start=1):
        if year_q in completed:
            expected_files = [
                staging_path(directory, cohort, year_q)
                for cohort in relevant_cohorts(year_q, cohort_windows)
            ]
            if not all(path.exists() and path.stat().st_size > 0 for path in expected_files):
                raise FileNotFoundError(
                    f"Checkpoint marks {year_q} complete but staging files are missing."
                )
            print(
                f"[annual path] Reusing quarter {position}/{len(ordered_quarters)}: {year_q}",
                flush=True,
            )
            continue
        build_quarter_staging(
            source_paths[year_q],
            path_cohorts,
            cohort_windows,
            directory,
            chunksize,
        )
        completed.append(year_q)
        completed.sort(key=quarterly.quarter_key)
        write_checkpoint(checkpoint, completed)
        print(
            f"[annual path] Committed quarter {position}/{len(ordered_quarters)}: {year_q}",
            flush=True,
        )

    for cohort in COHORT_YEARS:
        aggregate_cohort(directory, cohort, cohort_windows[cohort], samples)
    print(f"Completed annual path panels under: {directory}", flush=True)


if __name__ == "__main__":
    main()
