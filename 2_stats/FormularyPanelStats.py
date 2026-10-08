"""
Purpose:
Summarize annual-event or quarterly-event formulary panel blocks without
combining them in memory. Produce coverage and event-by-ATC diagnostics.

Process:
1. Stream the required columns from each formulary_panel block in small chunks.
2. Deduplicate FORMULARY_ID-quarter and treated FORMULARY_ID-NDC observations.
   Annual events use event-year Q1; quarterly event-NDC statistics require the
   NDC to have been first seen by the prior-year same quarter (event t-4).
3. Validate that each ATC sharing split exhausts its corresponding event count.
4. Save concise CSV summaries and bar charts under the project-level csv and
   figures directories.

Input:
- data/formulary_panel[/shift_qX]/formulary_panel_*.csv (quarter=0)
- data/formulary_panel_quarter_by_time[/shift_qX]/formulary_panel_YYYYQX.csv
  (quarter=1)
- matching data/formulary_metadata/ndc_first_seen*.csv

Output:
- csv/formulary_panel_stats[/quarter/shift_qX]/{coverage,event,share}/*.csv
- figures/formulary_panel_stats[/quarter/shift_qX]/{coverage,event,share}/*.png
"""

from __future__ import annotations

from collections import Counter
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
CSV_ROOT = PROJECT_ROOT / "csv" / "formulary_panel_stats"
FIGURE_ROOT = PROJECT_ROOT / "figures" / "formulary_panel_stats"


# ========================== USER CONFIG ==========================
N_FORMULARY_BLOCKS = 30
CHUNKSIZE = 150_000
QUARTER = 1  # 0: annual events in Q1; 1: events in their actual quarter.
FORMULARY_TIME_SHIFT_QUARTERS = 1
ATC_LEVELS = (3,) if QUARTER else (1, 2, 3, 4)
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
    """Describe one event type and treatment direction stored in the panel."""

    event_type: str
    direction: str
    slug: str
    label: str

    @property
    def event_column(self) -> str:
        """Return the matching event-indicator column name."""
        return f"event_{self.event_type}_{self.direction}"

    def share_column(self, atc_level: int) -> str:
        """Return the matching event-specific ATC sharing column name."""
        return f"{self.event_column}_sharingATC{atc_level}"


def configured_events() -> tuple[EventSpec, ...]:
    """Build validated event specifications from the user configuration."""
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
    CSV_ROOT = PROJECT_ROOT / "csv" / "formulary_panel_stats" / output_suffix
    FIGURE_ROOT = PROJECT_ROOT / "figures" / "formulary_panel_stats" / output_suffix


def available_periods(paths: list[Path]) -> set[int]:
    """Read the time column once to locate the observed edge of cohort windows."""
    periods: set[int] = set()
    for path in tqdm(paths, desc="Checking observed quarters", unit="file"):
        for chunk in pd.read_csv(path, usecols=["YEAR_Q"], dtype="string", chunksize=CHUNKSIZE):
            parsed = parse_year_quarter(chunk, path)
            periods.update((parsed["_year"].astype(int) * 4 + parsed["_quarter"].astype(int)).unique())
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
    # Match FormularyCohortPanelMaker.keep_available_ndcs: request t-4, but
    # clip to the first quarter actually available in this cohort window.
    return max(period - 4, min(interior))


def quarter_tag(period: int) -> str:
    """Format an integer quarter index as YYYYQX."""
    year = (period - 1) // 4
    return f"{year}Q{period - year * 4}"


def panel_paths() -> list[Path]:
    """Return the configured panel files in deterministic time or block order."""
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

    if N_FORMULARY_BLOCKS < 1:
        raise ValueError("N_FORMULARY_BLOCKS must be at least 1.")

    paths = [PANEL_DIR / f"formulary_panel_{number}.csv" for number in range(1, N_FORMULARY_BLOCKS + 1)]
    missing = [path for path in paths if not path.exists()]
    if missing:
        raise FileNotFoundError(
            f"Expected {N_FORMULARY_BLOCKS} panel blocks in {PANEL_DIR}; missing: {missing[:5]}"
        )
    return paths


def required_columns(events: tuple[EventSpec, ...]) -> list[str]:
    """Return the minimal columns needed for all requested diagnostics."""
    columns = ["FORMULARY_ID", "NDC", "YEAR_Q"]
    for event in events:
        columns.append(event.event_column)
        columns.extend(event.share_column(level) for level in ATC_LEVELS)
    return columns


def validate_schema(paths: list[Path], columns: list[str]) -> None:
    """Check all blocks before a long streamed calculation begins."""
    for path in tqdm(paths, desc="Checking panel schemas", unit="file"):
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


def parse_year_quarter(data: pd.DataFrame, path: Path) -> pd.DataFrame:
    """Add integer year and quarter fields from YEAR_Q values such as 2020Q1."""
    parsed = data["YEAR_Q"].astype("string").str.extract(r"^\s*(\d{4})\s*Q([1-4])\s*$")
    invalid = parsed[0].isna() | parsed[1].isna()
    if invalid.any():
        examples = data.loc[invalid, "YEAR_Q"].drop_duplicates().head(10).tolist()
        raise ValueError(f"{path.name} has invalid YEAR_Q values: {examples}")
    data["_year"] = parsed[0].astype("int16")
    data["_quarter"] = parsed[1].astype("int8")
    data["_year_quarter"] = data["_year"].astype("string") + "Q" + data["_quarter"].astype("string")
    return data


def binary_indicator(data: pd.DataFrame, column: str, path: Path) -> pd.Series:
    """Return a strict 0/1 indicator, treating blank values as zero."""
    numeric = pd.to_numeric(data[column], errors="coerce")
    invalid = (data[column].notna() & numeric.isna()) | (numeric.notna() & ~numeric.isin([0, 1]))
    if invalid.any():
        examples = data.loc[invalid, column].drop_duplicates().head(10).tolist()
        raise ValueError(f"{path.name}.{column} must contain only 0/1/blank values; found {examples}")
    return numeric.fillna(0).astype("int8")


def normalize_identifiers(data: pd.DataFrame, path: Path) -> pd.DataFrame:
    """Strip key identifiers and reject missing formulary or drug identifiers."""
    for column in ("FORMULARY_ID", "NDC"):
        data[column] = data[column].astype("string").str.strip()
        data.loc[data[column].eq(""), column] = pd.NA
    if data[["FORMULARY_ID", "NDC"]].isna().any().any():
        raise ValueError(f"{path.name} has missing FORMULARY_ID or NDC values.")
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
        period = data["_year"].astype("int32") * 4 + data["_quarter"].astype("int32")
        cutoff = period.map(cutoff_by_period)
        if cutoff.isna().any():
            raise ValueError(f"{path.name} contains a quarter without a cutoff.")
    else:
        cutoff = data["_year"].astype("int32") * 4 + 1
    return first_seen.le(cutoff)


def add_block_coverage(
    pairs: set[tuple[str, str]],
    quarter_counts: Counter[str],
    span_counts: Counter[int],
) -> None:
    """Accumulate coverage counts from one complete-formulary block."""
    quarter_counts.update(year_quarter for _, year_quarter in pairs)
    periods_by_formulary = Counter(formulary_id for formulary_id, _ in pairs)
    span_counts.update(periods_by_formulary.values())


def accumulate_block(
    path: Path,
    events: tuple[EventSpec, ...],
    columns: list[str],
    first_seen_qtime: dict[str, int],
    cutoff_by_period: dict[int, int],
    quarter_counts: Counter[str],
    span_counts: Counter[int],
    event_counts: dict[str, Counter[int]],
    sharing_counts: dict[tuple[str, int], Counter[tuple[int, int]]],
) -> None:
    """Stream one block and add its exact, within-block-deduplicated counts."""
    coverage_pairs: set[tuple[str, str]] = set()
    event_ids = {event.slug: set() for event in events}
    share_by_event = {
        (event.slug, level): {} for event in events for level in ATC_LEVELS
    }

    reader = pd.read_csv(path, usecols=columns, dtype="string", chunksize=CHUNKSIZE)
    for data in tqdm(reader, desc=f"Reading {path.stem}", unit="chunk", leave=False):
        data = parse_year_quarter(normalize_identifiers(data, path), path)
        available_mask = available_by_event_cutoff(data, first_seen_qtime, path, cutoff_by_period)
        coverage_pairs.update(
            zip(data["FORMULARY_ID"].astype(str), data["_year_quarter"].astype(str), strict=True)
        )

        for event in events:
            raw_event_mask = binary_indicator(data, event.event_column, path).eq(1)
            if not raw_event_mask.any():
                continue
            if not QUARTER and data.loc[raw_event_mask, "_quarter"].ne(1).any():
                raise ValueError(f"{path.name}.{event.event_column} contains event rows outside Q1.")

            event_mask = raw_event_mask & available_mask
            data["_period"] = (
                data["_year"].astype("int32") * 4 + data["_quarter"].astype("int32")
                if QUARTER else data["_year"].astype("int32")
            )
            event_rows = data.loc[event_mask, ["_period", "FORMULARY_ID", "NDC"]]
            event_id_rows = list(
                zip(
                    event_rows["_period"].astype(int),
                    event_rows["FORMULARY_ID"].astype(str),
                    event_rows["NDC"].astype(str),
                    strict=True,
                )
            )
            event_ids[event.slug].update(event_id_rows)

            for level in ATC_LEVELS:
                share_column = event.share_column(level)
                share = binary_indicator(data, share_column, path)
                if share.loc[~raw_event_mask].eq(1).any():
                    raise ValueError(
                        f"{path.name}.{share_column} equals 1 outside {event.event_column} rows."
                    )
                share_values = share.loc[event_mask].tolist()
                values = share_by_event[(event.slug, level)]
                for event_id, value in zip(event_id_rows, share_values, strict=True):
                    values[event_id] = max(values.get(event_id, 0), int(value))

        del data

    add_block_coverage(coverage_pairs, quarter_counts, span_counts)
    for event in events:
        event_counts[event.slug].update(event_id[0] for event_id in event_ids[event.slug])
        for level in ATC_LEVELS:
            values = share_by_event[(event.slug, level)]
            sharing_counts[(event.slug, level)].update(
                (event_id[0], share_value) for event_id, share_value in values.items()
            )


def build_coverage_summary(quarter_counts: Counter[str]) -> pd.DataFrame:
    """Return quarterly formulary coverage in chronological order."""
    rows = []
    for year_quarter, count in quarter_counts.items():
        year_text, quarter_text = year_quarter.split("Q")
        rows.append(
            {
                "year": int(year_text),
                "quarter": int(quarter_text),
                "year_quarter": year_quarter,
                "n_formularies": int(count),
            }
        )
    return pd.DataFrame(rows).sort_values(["year", "quarter"]).reset_index(drop=True)


def build_span_summary(span_counts: Counter[int]) -> pd.DataFrame:
    """Return the distribution of observed-quarter counts per formulary."""
    return pd.DataFrame(
        {
            "n_quarters": sorted(span_counts),
            "n_formularies": [int(span_counts[value]) for value in sorted(span_counts)],
        }
    )


def observed_event_periods(coverage: pd.DataFrame) -> list[int]:
    """Return all observed calendar periods, including zero-event periods."""
    if coverage.empty:
        raise ValueError("No formulary-quarter observations were found.")
    if QUARTER:
        return (coverage["year"].astype(int) * 4 + coverage["quarter"].astype(int)).tolist()
    return list(range(int(coverage["year"].min()), int(coverage["year"].max()) + 1))


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


def build_event_summary(periods: list[int], counts: Counter[int], event: EventSpec) -> pd.DataFrame:
    """Return unique FORMULARY_ID-NDC incidence by configured event period."""
    return pd.DataFrame(
        {
            **period_columns(periods),
            "event_type": event.event_type,
            "direction": event.direction,
            "event_formulary_ndc": [int(counts[period]) for period in periods],
        }
    )


def build_sharing_summary(
    periods: list[int],
    event_counts: Counter[int],
    counts: Counter[tuple[int, int]],
    event: EventSpec,
    level: int,
) -> pd.DataFrame:
    """Return treated counts split into ATC-sharing and non-sharing groups."""
    sharing = [int(counts[(period, 1)]) for period in periods]
    nonsharing = [int(counts[(period, 0)]) for period in periods]
    total = [share + nonshare for share, nonshare in zip(sharing, nonsharing, strict=True)]
    expected = [int(event_counts[period]) for period in periods]
    if total != expected:
        raise AssertionError(
            f"{event.slug}, ATC{level}: sharing plus non-sharing does not equal total event incidence."
        )
    return pd.DataFrame(
        {
            **period_columns(periods),
            "event_type": event.event_type,
            "direction": event.direction,
            "atc_level": level,
            "sharing_formulary_ndc": sharing,
            "nonsharing_formulary_ndc": nonsharing,
            "event_formulary_ndc": total,
        }
    )


def save_single_bar(summary: pd.DataFrame, x: str, y: str, title: str, path: Path, rotate_labels: bool = False) -> None:
    """Save a single-series bar chart with a consistent publication-ready style."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, axis = plt.subplots(figsize=(11, 5))
    axis.bar(summary[x].astype(str), summary[y], color="#4C78A8")
    axis.set_xlabel(x.replace("_", " ").title())
    axis.set_ylabel(y.replace("_", " ").title())
    axis.set_title(title)
    axis.grid(axis="y", alpha=0.25)
    if rotate_labels:
        axis.tick_params(axis="x", rotation=60)
    fig.tight_layout()
    fig.savefig(path, dpi=300)
    plt.close(fig)


def save_sharing_bar(summary: pd.DataFrame, title: str, path: Path) -> None:
    """Save stacked sharing and non-sharing event counts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, axis = plt.subplots(figsize=(10, 5))
    time_column = "year_quarter" if QUARTER else "year"
    years = summary[time_column].astype(str)
    sharing = summary["sharing_formulary_ndc"]
    nonsharing = summary["nonsharing_formulary_ndc"]
    axis.bar(years, sharing, label="Sharing", color="#59A14F")
    axis.bar(years, nonsharing, bottom=sharing, label="Not sharing", color="#E15759")
    axis.set_xlabel("Year quarter" if QUARTER else "Year")
    axis.set_ylabel("Event FORMULARY_ID-NDC count")
    axis.set_title(title)
    axis.legend()
    axis.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=300)
    plt.close(fig)


def write_outputs(
    coverage: pd.DataFrame,
    spans: pd.DataFrame,
    periods: list[int],
    events: tuple[EventSpec, ...],
    event_counts: dict[str, Counter[int]],
    sharing_counts: dict[tuple[str, int], Counter[tuple[int, int]]],
) -> None:
    """Write all requested CSVs and their matching bar charts."""
    for directory in (
        CSV_ROOT / "coverage",
        CSV_ROOT / "event",
        CSV_ROOT / "share",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    coverage_csv = CSV_ROOT / "coverage" / "by_quarter.csv"
    span_csv = CSV_ROOT / "coverage" / "span_quarters.csv"
    coverage.to_csv(coverage_csv, index=False)
    spans.to_csv(span_csv, index=False)
    save_single_bar(
        coverage,
        "year_quarter",
        "n_formularies",
        "Observed formularies by quarter",
        FIGURE_ROOT / "coverage" / "by_quarter.png",
        rotate_labels=True,
    )
    save_single_bar(
        spans,
        "n_quarters",
        "n_formularies",
        "Observed-quarter span per formulary",
        FIGURE_ROOT / "coverage" / "span_quarters.png",
    )

    for event in events:
        event_summary = build_event_summary(periods, event_counts[event.slug], event)
        event_summary.to_csv(CSV_ROOT / "event" / f"{event.slug}.csv", index=False)
        save_single_bar(
            event_summary,
            "year_quarter" if QUARTER else "year",
            "event_formulary_ndc",
            f"{event.label}: {'quarterly' if QUARTER else 'yearly'} event incidence",
            FIGURE_ROOT / "event" / f"{event.slug}.png",
        )

        for level in ATC_LEVELS:
            sharing_summary = build_sharing_summary(
                periods,
                event_counts[event.slug],
                sharing_counts[(event.slug, level)],
                event,
                level,
            )
            sharing_summary.to_csv(
                CSV_ROOT / "share" / f"{event.slug}_atc{level}.csv",
                index=False,
            )
            save_sharing_bar(
                sharing_summary,
                f"{event.label}: ATC{level} sharing split",
                FIGURE_ROOT / "share" / f"{event.slug}_atc{level}.png",
            )


def main() -> None:
    """Stream all panel blocks, validate the statistics, and save diagnostics."""
    configure_paths()
    events = configured_events()
    paths = panel_paths()
    columns = required_columns(events)
    validate_schema(paths, columns)
    first_seen_qtime = load_first_seen_lookup()
    cutoff_by_period: dict[int, int] = {}
    if QUARTER:
        observed = available_periods(paths)
        cutoff_by_period = {period: cutoff_for_period(period, observed) for period in observed}

    quarter_counts: Counter[str] = Counter()
    span_counts: Counter[int] = Counter()
    event_counts = {event.slug: Counter() for event in events}
    sharing_counts = {
        (event.slug, level): Counter() for event in events for level in ATC_LEVELS
    }

    for path in tqdm(paths, desc="Processing panel blocks", unit="block"):
        accumulate_block(
            path,
            events,
            columns,
            first_seen_qtime,
            cutoff_by_period,
            quarter_counts,
            span_counts,
            event_counts,
            sharing_counts,
        )

    coverage = build_coverage_summary(quarter_counts)
    spans = build_span_summary(span_counts)
    if QUARTER:
        cutoff_summary = coverage[["year", "quarter", "year_quarter"]].copy()
        cutoff_summary["first_seen_cutoff_qtime"] = [
            cutoff_by_period[year * 4 + quarter]
            for year, quarter in zip(cutoff_summary["year"], cutoff_summary["quarter"], strict=True)
        ]
        cutoff_summary["first_seen_cutoff_year_quarter"] = cutoff_summary[
            "first_seen_cutoff_qtime"
        ].map(quarter_tag)
        (CSV_ROOT / "coverage").mkdir(parents=True, exist_ok=True)
        cutoff_summary.to_csv(CSV_ROOT / "coverage" / "first_seen_cutoffs.csv", index=False)
    write_outputs(
        coverage,
        spans,
        observed_event_periods(coverage),
        events,
        event_counts,
        sharing_counts,
    )
    print(f"Saved CSV summaries under: {CSV_ROOT}")
    print(f"Saved figures under: {FIGURE_ROOT}")


if __name__ == "__main__":
    main()
