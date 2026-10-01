"""
Purpose:
Create representative formulary-level event diagnostics from large panel
blocks. Annual mode selects one Q1 formulary per year; quarterly mode selects
one formulary for each observed target quarter.

Process:
1. Open panel blocks in a dispersed, deterministic priority order and stream
   only the columns required for the event statistics.
2. For each uncovered event period, select the first encountered FORMULARY_ID,
   retain only its rows for that period, and stop opening event columns once
   every target period has one selected formulary.
3. Count unique event firms, firms with at least one ATC-sharing event NDC, and
   unique event NDC values split by ATC sharing status for all six event-
   direction indicators. Quarterly event-NDC statistics require first-seen by
   event t-4; only upstream ATC-sharing construction uses partner NDCs through
   event t+3. Annual mode retains the event-year Q1 cutoff.
4. Save the selection manifest, CSV summaries, and bar charts under concise
   project-level csv and figures folders.

Input:
- data/formulary_panel[/shift_qX]/formulary_panel_*.csv (quarter=0)
- data/formulary_panel_quarter_by_time[/shift_qX]/formulary_panel_YYYYQX.csv
  (quarter=1)
- matching data/formulary_metadata/ndc_first_seen*.csv

Output:
- csv/formulary_panel_event_stats[/quarter/shift_qX]/{selection,firm,ndc_share}/*.csv
- figures/formulary_panel_event_stats[/quarter/shift_qX]/{firm,ndc_share}/*.png
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd
from tqdm.auto import tqdm


# Configure project directory paths
CURRENT_PATH = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_PATH.parent.parent
PANEL_DIR = PROJECT_ROOT / "data" / "formulary_panel"
FIRST_SEEN_PATH = PROJECT_ROOT / "data" / "formulary_metadata" / "ndc_first_seen.csv"
CSV_ROOT = PROJECT_ROOT / "csv" / "formulary_panel_event_stats"
FIGURE_ROOT = PROJECT_ROOT / "figures" / "formulary_panel_event_stats"


# ========================== USER CONFIG ==========================
N_FORMULARY_BLOCKS = 30
CHUNKSIZE = 150_000
QUARTER = 1  # 0: annual events in Q1; 1: events in their actual quarter.
FORMULARY_TIME_SHIFT_QUARTERS = 1
TARGET_START_YEAR = 2019
TARGET_END_YEAR = 2025
ATC_LEVELS = (3,) if QUARTER else (1, 2, 3, 4)
FIRM_COLUMN = "id" if QUARTER else "BoardName"

# Start with early, late, and widely separated blocks. The script stops before
# reaching the end of this order as soon as every target period is covered.
BLOCK_ORDER = (
    1, 30, 15, 8, 23, 4, 19, 12, 27, 6,
    21, 10, 25, 3, 18, 13, 28, 5, 20, 11,
    26, 2, 17, 14, 29, 7, 22, 9, 24, 16,
)
EVENT_SPECS = (
    ("to_B_still_in_A", "A", "stay_a", "Move to B; still in A (A)"),
    ("to_B_still_in_A", "B", "stay_b", "Move to B; still in A (B)"),
    ("to_B_not_in_A", "A", "exit_a", "Move to B; not in A (A)"),
    ("to_B_not_in_A", "B", "exit_b", "Move to B; not in A (B)"),
    ("interlock_dissolution", "A", "dissolve_a", "Interlock dissolution (A)"),
    ("interlock_dissolution", "B", "dissolve_b", "Interlock dissolution (B)"),
)
# ================================================================


@dataclass(frozen=True)
class EventSpec:
    """Describe one stored event type and treatment direction."""

    event_type: str
    direction: str
    slug: str
    label: str

    @property
    def event_column(self) -> str:
        """Return the panel event-indicator column."""
        return f"event_{self.event_type}_{self.direction}"

    def share_column(self, atc_level: int) -> str:
        """Return the matching event-specific ATC sharing column."""
        return f"{self.event_column}_sharingATC{atc_level}"


def configured_events() -> tuple[EventSpec, ...]:
    """Build validated event specifications from the configuration."""
    events = tuple(EventSpec(*values) for values in EVENT_SPECS)
    if len({event.slug for event in events}) != len(events):
        raise ValueError("Each event specification must have a unique slug.")
    return events


def configure_paths() -> None:
    """Select matching panel, NDC metadata, and output paths for this run."""
    global PANEL_DIR, FIRST_SEEN_PATH, CSV_ROOT, FIGURE_ROOT
    if QUARTER not in (0, 1):
        raise ValueError("QUARTER must be 0 or 1.")
    shift = FORMULARY_TIME_SHIFT_QUARTERS
    if not isinstance(shift, int):
        raise ValueError("FORMULARY_TIME_SHIFT_QUARTERS must be an integer.")
    shift_suffix = f"shift_q{shift}" if shift else ""
    panel_name = "formulary_panel_quarter_by_time" if QUARTER else "formulary_panel"
    first_seen_name = "ndc_first_seen_quarter" if QUARTER else "ndc_first_seen"
    PANEL_DIR = PROJECT_ROOT / "data" / panel_name
    if shift_suffix:
        PANEL_DIR /= shift_suffix
        first_seen_name += f"_{shift_suffix}"
    FIRST_SEEN_PATH = PROJECT_ROOT / "data" / "formulary_metadata" / f"{first_seen_name}.csv"
    output_suffix = Path("quarter", shift_suffix or "shift_q0") if QUARTER else (Path("annual", shift_suffix) if shift_suffix else Path())
    CSV_ROOT = PROJECT_ROOT / "csv" / "formulary_panel_event_stats" / output_suffix
    FIGURE_ROOT = PROJECT_ROOT / "figures" / "formulary_panel_event_stats" / output_suffix


def available_periods(paths: list[Path]) -> set[int]:
    """Read panel time columns to locate the cohort-window data boundary."""
    periods: set[int] = set()
    for path in tqdm(paths, desc="Checking observed quarters", unit="file"):
        for chunk in pd.read_csv(path, usecols=["YEAR_Q"], dtype="string", chunksize=CHUNKSIZE):
            parsed = chunk["YEAR_Q"].str.extract(r"^\s*(\d{4})\s*Q([1-4])\s*$")
            if parsed.isna().any().any():
                raise ValueError(f"{path.name} contains invalid YEAR_Q values.")
            periods.update((parsed[0].astype(int) * 4 + parsed[1].astype(int)).unique())
    if not periods:
        raise ValueError("No formulary-quarter observations were found.")
    return periods


def cutoff_for_period(period: int, observed: set[int]) -> int:
    """Return the prior-year same-quarter event-NDC cutoff (event t-4)."""
    interior = set(range(max(period - 4, min(observed)), min(period + 7, max(observed)) + 1))
    missing = interior - observed
    if missing:
        raise FileNotFoundError(f"Cohort window for {period} is missing quarters: {sorted(missing)}")
    if period not in observed:
        raise ValueError(f"Event quarter {period} is absent from the panel.")
    return period - 4


def quarter_tag(period: int) -> str:
    """Format an integer quarter index as YYYYQX."""
    year = (period - 1) // 4
    return f"{year}Q{period - year * 4}"


def target_periods(observed: set[int] | None = None) -> list[int]:
    """Return target years or observed target quarters at the data edges."""
    if TARGET_START_YEAR > TARGET_END_YEAR:
        raise ValueError("TARGET_START_YEAR must be no later than TARGET_END_YEAR.")
    if QUARTER:
        if observed is None:
            raise ValueError("Quarterly targets require observed panel quarters.")
        return sorted(
            period for period in observed
            if TARGET_START_YEAR * 4 + 1 <= period <= TARGET_END_YEAR * 4 + 4
        )
    return list(range(TARGET_START_YEAR, TARGET_END_YEAR + 1))


def period_columns(periods: list[int]) -> dict[str, list[int] | list[str]]:
    """Return year and, in quarterly mode, quarter identifiers for output."""
    if QUARTER:
        years = [(period - 1) // 4 for period in periods]
        quarters = [period - year * 4 for period, year in zip(periods, years, strict=True)]
        return {
            "year": years,
            "quarter": quarters,
            "year_quarter": [f"{year}Q{quarter}" for year, quarter in zip(years, quarters, strict=True)],
        }
    return {"year": periods}


def validate_block_order() -> None:
    """Require a complete, nonrepeating priority order for all panel blocks."""
    expected = set(range(1, N_FORMULARY_BLOCKS + 1))
    if len(BLOCK_ORDER) != N_FORMULARY_BLOCKS or set(BLOCK_ORDER) != expected:
        raise ValueError("BLOCK_ORDER must contain each panel block exactly once.")


def panel_paths() -> list[Path]:
    """Return quarterly files chronologically or annual blocks by priority."""
    if QUARTER:
        paths = list(PANEL_DIR.glob("formulary_panel_????Q?.csv"))
        if not paths:
            raise FileNotFoundError(f"No quarterly panel files found in {PANEL_DIR}")

        def quarter_key(path: Path) -> tuple[int, int]:
            tag = path.stem.removeprefix("formulary_panel_")
            year_text, quarter_text = tag.split("Q", maxsplit=1)
            year, quarter = int(year_text), int(quarter_text)
            if quarter not in range(1, 5):
                raise ValueError(f"Invalid quarterly panel filename: {path.name}")
            return year, quarter

        return sorted(paths, key=quarter_key)

    validate_block_order()
    paths = [PANEL_DIR / f"formulary_panel_{number}.csv" for number in BLOCK_ORDER]
    missing = [path for path in paths if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Missing panel blocks: {missing[:5]}")
    return paths


def required_columns(events: tuple[EventSpec, ...]) -> list[str]:
    """Return the only columns streamed from each potentially useful block."""
    columns = ["FORMULARY_ID", "YEAR_Q", FIRM_COLUMN, "NDC"]
    for event in events:
        columns.append(event.event_column)
        columns.extend(event.share_column(level) for level in ATC_LEVELS)
    return columns


def validate_schema(path: Path, columns: list[str]) -> None:
    """Fail before a long stream if a candidate block lacks a needed field."""
    observed = set(pd.read_csv(path, nrows=0).columns)
    missing = sorted(set(columns) - observed)
    if missing:
        raise KeyError(f"{path.name} is missing required columns: {missing}")


def load_first_seen_lookup() -> dict[str, int]:
    """Load one earliest included quarter for every expanded NDC."""
    if not FIRST_SEEN_PATH.exists():
        raise FileNotFoundError(f"NDC first-seen lookup not found: {FIRST_SEEN_PATH}")
    first_seen = pd.read_csv(FIRST_SEEN_PATH, dtype={"NDC": "string"})
    required = {"NDC", "first_seen_qtime"}
    missing = sorted(required - set(first_seen.columns))
    if missing:
        raise KeyError(f"{FIRST_SEEN_PATH.name} is missing columns: {missing}")

    first_seen["NDC"] = first_seen["NDC"].astype("string").str.strip()
    if first_seen["NDC"].isna().any() or first_seen["NDC"].eq("").any():
        raise ValueError(f"{FIRST_SEEN_PATH.name} contains a missing NDC.")
    if first_seen["NDC"].duplicated().any():
        raise ValueError(f"{FIRST_SEEN_PATH.name} contains duplicate NDC values.")
    first_seen["first_seen_qtime"] = pd.to_numeric(
        first_seen["first_seen_qtime"], errors="raise"
    ).astype("int32")
    return dict(
        zip(
            first_seen["NDC"].astype(str),
            first_seen["first_seen_qtime"].astype(int),
        )
    )


def normalize_data(data: pd.DataFrame, path: Path) -> pd.DataFrame:
    """Normalize identifiers and parse YEAR_Q into integer year and quarter fields."""
    identifier_columns = ("FORMULARY_ID", FIRM_COLUMN, "NDC")
    for column in identifier_columns:
        data[column] = data[column].astype("string").str.strip()
        data.loc[data[column].eq(""), column] = pd.NA
    if data[list(identifier_columns)].isna().any().any():
        raise ValueError(f"{path.name} contains missing values in {identifier_columns}.")

    if QUARTER:
        numeric_id = pd.to_numeric(data[FIRM_COLUMN], errors="coerce")
        invalid_id = numeric_id.isna() | numeric_id.le(0) | numeric_id.ne(numeric_id.round())
        if invalid_id.any():
            examples = data.loc[invalid_id, FIRM_COLUMN].drop_duplicates().head(10).tolist()
            raise ValueError(f"{path.name}.id must contain positive integers: {examples}")
        data[FIRM_COLUMN] = numeric_id.astype("int32").astype("string")

    parsed = data["YEAR_Q"].astype("string").str.extract(r"^\s*(\d{4})\s*Q([1-4])\s*$")
    invalid = parsed[0].isna() | parsed[1].isna()
    if invalid.any():
        examples = data.loc[invalid, "YEAR_Q"].drop_duplicates().head(10).tolist()
        raise ValueError(f"{path.name} has invalid YEAR_Q values: {examples}")
    data["_year"] = parsed[0].astype("int16")
    data["_quarter"] = parsed[1].astype("int8")
    data["_period"] = (
        data["_year"].astype("int32") * 4 + data["_quarter"].astype("int32")
        if QUARTER else data["_year"].astype("int32")
    )
    return data


def available_by_event_cutoff(
    data: pd.DataFrame,
    first_seen_qtime: dict[str, int],
    path: Path,
    cutoff_by_period: dict[int, int],
) -> pd.Series:
    """Return whether each NDC was first included by the configured cutoff."""
    first_seen = data["NDC"].map(first_seen_qtime)
    if first_seen.isna().any():
        examples = data.loc[first_seen.isna(), "NDC"].drop_duplicates().head(10).tolist()
        raise KeyError(f"{path.name} has NDCs missing from the first-seen lookup: {examples}")
    if QUARTER:
        cutoff = data["_period"].map(cutoff_by_period)
        if cutoff.isna().any():
            raise ValueError(f"{path.name} contains a quarter without a cutoff.")
    else:
        cutoff = data["_year"].astype("int32") * 4 + 1
    return first_seen.le(cutoff)


def binary_indicator(data: pd.DataFrame, column: str, path: Path) -> pd.Series:
    """Return a strict binary indicator, treating blank cells as zero."""
    numeric = pd.to_numeric(data[column], errors="coerce")
    invalid = (data[column].notna() & numeric.isna()) | (numeric.notna() & ~numeric.isin([0, 1]))
    if invalid.any():
        examples = data.loc[invalid, column].drop_duplicates().head(10).tolist()
        raise ValueError(f"{path.name}.{column} must contain only 0, 1, or blank values: {examples}")
    return numeric.fillna(0).astype("int8")


def add_new_selections(
    data: pd.DataFrame,
    target_order: list[int],
    selected: dict[int, tuple[str, str]],
    source_file: str,
) -> None:
    """Select one deterministic formulary for each still-uncovered period."""
    missing = set(target_order) - set(selected)
    if not missing:
        return

    period_mask = data["_period"].isin(missing)
    if not QUARTER:
        period_mask &= data["_quarter"].eq(1)
    candidates = data.loc[period_mask, ["_period", "FORMULARY_ID"]].drop_duplicates()
    for period in target_order:
        if period in selected:
            continue
        values = candidates.loc[candidates["_period"].eq(period), "FORMULARY_ID"]
        if not values.empty:
            selected[period] = (str(values.iloc[0]), source_file)


def selected_rows(data: pd.DataFrame, selected: dict[int, tuple[str, str]]) -> pd.DataFrame:
    """Keep rows belonging to each period's selected formulary."""
    selection = pd.DataFrame(
        {
            "_period": list(selected),
            "FORMULARY_ID": [values[0] for values in selected.values()],
        }
    )
    candidate_rows = data if QUARTER else data.loc[data["_quarter"].eq(1)]
    return candidate_rows.merge(
        selection, on=["_period", "FORMULARY_ID"], how="inner", validate="many_to_one"
    )


def accumulate_selected_rows(
    data: pd.DataFrame,
    events: tuple[EventSpec, ...],
    path: Path,
    first_seen_qtime: dict[str, int],
    cutoff_by_period: dict[int, int],
    firm_sets: dict[int, dict[str, set[str]]],
    sharing_firm_sets: dict[int, dict[str, set[str]]],
    ndc_sharing: dict[int, dict[tuple[str, int], dict[str, int]]],
) -> None:
    """Accumulate unique event firms and NDC share status from selected rows."""
    for period, quarter_data in data.groupby("_period", sort=False):
        available_mask = available_by_event_cutoff(
            quarter_data, first_seen_qtime, path, cutoff_by_period
        )
        for event in events:
            raw_event_mask = binary_indicator(quarter_data, event.event_column, path).eq(1)
            if not raw_event_mask.any():
                continue
            if not QUARTER and quarter_data.loc[raw_event_mask, "_quarter"].ne(1).any():
                raise ValueError(f"{path.name}.{event.event_column} contains an event outside Q1.")

            firm_sets[int(period)][event.slug].update(
                quarter_data.loc[raw_event_mask, FIRM_COLUMN].astype(str)
            )
            event_mask = raw_event_mask & available_mask
            for level in ATC_LEVELS:
                share_column = event.share_column(level)
                share = binary_indicator(quarter_data, share_column, path)
                if share.loc[~raw_event_mask].eq(1).any():
                    raise ValueError(f"{path.name}.{share_column} equals 1 outside matching event rows.")
                sharing_firm_sets[int(period)][event.slug].update(
                    quarter_data.loc[event_mask & share.eq(1), FIRM_COLUMN].astype(str)
                )
                event_share = pd.DataFrame(
                    {
                        "NDC": quarter_data.loc[event_mask, "NDC"].astype(str),
                        "share": share.loc[event_mask].to_numpy(),
                    }
                ).groupby("NDC", as_index=False)["share"].max()
                values = ndc_sharing[int(period)][(event.slug, level)]
                for ndc, share_value in event_share.itertuples(index=False, name=None):
                    values[str(ndc)] = max(values.get(str(ndc), 0), int(share_value))


def initialize_accumulators(
    years: list[int],
    events: tuple[EventSpec, ...],
) -> tuple[
    dict[int, dict[str, set[str]]],
    dict[int, dict[str, set[str]]],
    dict[int, dict[tuple[str, int], dict[str, int]]],
]:
    """Create empty compact accumulators for all target year-event cells."""
    firm_sets = {
        year: {event.slug: set() for event in events}
        for year in years
    }
    sharing_firm_sets = {
        year: {event.slug: set() for event in events}
        for year in years
    }
    ndc_sharing = {
        year: {(event.slug, level): {} for event in events for level in ATC_LEVELS}
        for year in years
    }
    return firm_sets, sharing_firm_sets, ndc_sharing


def selection_manifest(
    selected: dict[int, tuple[str, str]],
    periods: list[int],
    cutoff_by_period: dict[int, int],
) -> pd.DataFrame:
    """Return the reproducibility record for all representative formularies."""
    missing = [period for period in periods if period not in selected]
    if missing:
        raise RuntimeError(f"No formulary was selected for target periods: {missing}")
    columns = {
        **period_columns(periods),
        "formulary_id": [selected[period][0] for period in periods],
        "source_file": [selected[period][1] for period in periods],
    }
    if QUARTER:
        columns["first_seen_cutoff_qtime"] = [cutoff_by_period[period] for period in periods]
        columns["first_seen_cutoff_year_quarter"] = [
            quarter_tag(cutoff_by_period[period]) for period in periods
        ]
    return pd.DataFrame(columns)


def firm_summary(
    periods: list[int],
    event: EventSpec,
    firm_sets: dict[int, dict[str, set[str]]],
    sharing_firm_sets: dict[int, dict[str, set[str]]],
) -> pd.DataFrame:
    """Return event-firm and ATC-sharing event-firm counts."""
    return pd.DataFrame(
        {
            **period_columns(periods),
            "event_type": event.event_type,
            "direction": event.direction,
            "event_firms": [len(firm_sets[period][event.slug]) for period in periods],
            "sharing_event_firms": [
                len(sharing_firm_sets[period][event.slug]) for period in periods
            ],
        }
    )


def ndc_summary(
    periods: list[int],
    event: EventSpec,
    atc_level: int,
    ndc_sharing: dict[int, dict[tuple[str, int], dict[str, int]]],
) -> pd.DataFrame:
    """Return unique event NDC counts split by ATC sharing status."""
    sharing = []
    nonsharing = []
    for period in periods:
        values = ndc_sharing[period][(event.slug, atc_level)].values()
        sharing.append(sum(value == 1 for value in values))
        nonsharing.append(sum(value == 0 for value in values))
    return pd.DataFrame(
        {
            **period_columns(periods),
            "event_type": event.event_type,
            "direction": event.direction,
            "atc_level": atc_level,
            "sharing_ndcs": sharing,
            "nonsharing_ndcs": nonsharing,
            "event_ndcs": [share + nonshare for share, nonshare in zip(sharing, nonsharing, strict=True)],
        }
    )


def save_firm_plot(summary: pd.DataFrame, event: EventSpec, path: Path) -> None:
    """Save one unique-event-firm bar chart."""
    path.parent.mkdir(parents=True, exist_ok=True)
    figure, axis = plt.subplots(figsize=(11, 5))
    time_column = "year_quarter" if QUARTER else "year"
    axis.bar(summary[time_column].astype(str), summary["event_firms"], color="#4C78A8")
    axis.set_xlabel("Year quarter" if QUARTER else "Year")
    axis.set_ylabel("Unique firm count")
    axis.set_title(f"{event.label}: firms with an event")
    axis.grid(axis="y", alpha=0.25)
    axis.tick_params(axis="x", rotation=60)
    figure.tight_layout()
    figure.savefig(path, dpi=300)
    plt.close(figure)


def save_ndc_plot(summary: pd.DataFrame, event: EventSpec, atc_level: int, path: Path) -> None:
    """Save one stacked event-NDC sharing bar chart."""
    path.parent.mkdir(parents=True, exist_ok=True)
    figure, axis = plt.subplots(figsize=(11, 5))
    time_column = "year_quarter" if QUARTER else "year"
    years = summary[time_column].astype(str)
    sharing = summary["sharing_ndcs"]
    nonsharing = summary["nonsharing_ndcs"]
    axis.bar(years, sharing, label="Sharing", color="#59A14F")
    axis.bar(years, nonsharing, bottom=sharing, label="Not sharing", color="#E15759")
    axis.set_xlabel("Year quarter" if QUARTER else "Year")
    axis.set_ylabel("Unique NDC count")
    axis.set_title(f"{event.label}: event NDCs, ATC{atc_level}")
    axis.legend()
    axis.grid(axis="y", alpha=0.25)
    axis.tick_params(axis="x", rotation=60)
    figure.tight_layout()
    figure.savefig(path, dpi=300)
    plt.close(figure)


def write_outputs(
    periods: list[int],
    selected: dict[int, tuple[str, str]],
    opened_files: list[str],
    events: tuple[EventSpec, ...],
    firm_sets: dict[int, dict[str, set[str]]],
    sharing_firm_sets: dict[int, dict[str, set[str]]],
    ndc_sharing: dict[int, dict[tuple[str, int], dict[str, int]]],
    cutoff_by_period: dict[int, int],
) -> None:
    """Write the manifest plus all requested event-firm and event-NDC diagnostics."""
    for directory in (CSV_ROOT / "selection", CSV_ROOT / "firm", CSV_ROOT / "ndc_share"):
        directory.mkdir(parents=True, exist_ok=True)
    selection_manifest(selected, periods, cutoff_by_period).to_csv(
        CSV_ROOT / "selection" / "formularies.csv", index=False
    )
    pd.DataFrame({"opened_file": opened_files}).to_csv(
        CSV_ROOT / "selection" / "files.csv", index=False
    )

    for event in events:
        firms = firm_summary(periods, event, firm_sets, sharing_firm_sets)
        firms.to_csv(CSV_ROOT / "firm" / f"{event.slug}.csv", index=False)
        save_firm_plot(firms, event, FIGURE_ROOT / "firm" / f"{event.slug}.png")
        for level in ATC_LEVELS:
            ndcs = ndc_summary(periods, event, level, ndc_sharing)
            ndcs.to_csv(CSV_ROOT / "ndc_share" / f"{event.slug}_atc{level}.csv", index=False)
            save_ndc_plot(ndcs, event, level, FIGURE_ROOT / "ndc_share" / f"{event.slug}_atc{level}.png")


def main() -> None:
    """Select one formulary per event period and save diagnostics."""
    configure_paths()
    paths = panel_paths()
    events = configured_events()
    observed = available_periods(paths) if QUARTER else None
    periods = target_periods(observed)
    if not periods:
        raise ValueError("No observed formulary quarters fall in the target year range.")
    cutoff_by_period = (
        {period: cutoff_for_period(period, observed) for period in observed}
        if observed is not None else {}
    )
    columns = required_columns(events)
    first_seen_qtime = load_first_seen_lookup()
    selected: dict[int, tuple[str, str]] = {}
    opened_files: list[str] = []
    firm_sets, sharing_firm_sets, ndc_sharing = initialize_accumulators(periods, events)

    for path in tqdm(paths, desc="Opening panel files", unit="file"):
        if len(selected) == len(periods):
            break
        validate_schema(path, columns)
        opened_files.append(path.name)
        reader = pd.read_csv(path, usecols=columns, dtype="string", chunksize=CHUNKSIZE)
        for data in tqdm(reader, desc=f"Reading {path.stem}", unit="chunk", leave=False):
            data = normalize_data(data, path)
            add_new_selections(data, periods, selected, path.name)
            retained = selected_rows(data, selected)
            if not retained.empty:
                accumulate_selected_rows(
                    retained,
                    events,
                    path,
                    first_seen_qtime,
                    cutoff_by_period,
                    firm_sets,
                    sharing_firm_sets,
                    ndc_sharing,
                )
            del data, retained

    if len(selected) != len(periods):
        missing = [period for period in periods if period not in selected]
        raise RuntimeError(f"Stopped after all blocks but target periods remain uncovered: {missing}")

    write_outputs(
        periods,
        selected,
        opened_files,
        events,
        firm_sets,
        sharing_firm_sets,
        ndc_sharing,
        cutoff_by_period,
    )
    print(f"Opened {len(opened_files)} of {len(paths)} panel files.")
    print(f"Saved CSV summaries under: {CSV_ROOT}")
    print(f"Saved figures under: {FIGURE_ROOT}")


if __name__ == "__main__":
    main()
