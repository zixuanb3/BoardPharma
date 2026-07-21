"""
Purpose:
Create representative formulary-level annual event diagnostics from the
large formulary panel blocks. Each year is represented by exactly one
formulary observed in Q1, which already contains the full NDC universe.

Process:
1. Open panel blocks in a dispersed, deterministic priority order and stream
   only the columns required for the event statistics.
2. For each uncovered year, select the first encountered Q1 FORMULARY_ID,
   retain only that FORMULARY_ID-year-Q1's rows, and stop opening new blocks
   once every target year has one selected formulary.
3. Count unique event BoardName values and unique event NDC values split by
   ATC1--ATC4 sharing status for all six event-direction indicators. Event NDC
   counts include only products first included by the event year's Q1.
4. Save the selection manifest, CSV summaries, and bar charts under concise
   project-level csv and figures folders.

Input:
- data/formulary_panel/formulary_panel_1.csv through formulary_panel_30.csv
- data/formulary_metadata/ndc_first_seen.csv

Output:
- csv/formulary_panel_event_stats/{selection,firm,ndc_share}/*.csv
- figures/formulary_panel_event_stats/{firm,ndc_share}/*.png
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
TARGET_START_YEAR = 2019
TARGET_END_YEAR = 2025
ATC_LEVELS = (1, 2, 3, 4)

# Start with early, late, and widely separated blocks. The script stops before
# reaching the end of this order as soon as every target year is covered.
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


def target_years() -> list[int]:
    """Return every event year represented by a Q1 formulary observation."""
    if TARGET_START_YEAR > TARGET_END_YEAR:
        raise ValueError("TARGET_START_YEAR must be no later than TARGET_END_YEAR.")
    return list(range(TARGET_START_YEAR, TARGET_END_YEAR + 1))


def validate_block_order() -> None:
    """Require a complete, nonrepeating priority order for all panel blocks."""
    expected = set(range(1, N_FORMULARY_BLOCKS + 1))
    if len(BLOCK_ORDER) != N_FORMULARY_BLOCKS or set(BLOCK_ORDER) != expected:
        raise ValueError("BLOCK_ORDER must contain each panel block exactly once.")


def panel_path(block_number: int) -> Path:
    """Return and validate one numbered panel block path."""
    path = PANEL_DIR / f"formulary_panel_{block_number}.csv"
    if not path.exists():
        raise FileNotFoundError(f"Missing panel block: {path}")
    return path


def required_columns(events: tuple[EventSpec, ...]) -> list[str]:
    """Return the only columns streamed from each potentially useful block."""
    columns = ["FORMULARY_ID", "YEAR_Q", "BoardName", "NDC"]
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
    for column in ("FORMULARY_ID", "BoardName", "NDC"):
        data[column] = data[column].astype("string").str.strip()
        data.loc[data[column].eq(""), column] = pd.NA
    if data[["FORMULARY_ID", "BoardName", "NDC"]].isna().any().any():
        raise ValueError(f"{path.name} contains missing FORMULARY_ID, BoardName, or NDC values.")

    parsed = data["YEAR_Q"].astype("string").str.extract(r"^\s*(\d{4})\s*Q([1-4])\s*$")
    invalid = parsed[0].isna() | parsed[1].isna()
    if invalid.any():
        examples = data.loc[invalid, "YEAR_Q"].drop_duplicates().head(10).tolist()
        raise ValueError(f"{path.name} has invalid YEAR_Q values: {examples}")
    data["_year"] = parsed[0].astype("int16")
    data["_quarter"] = parsed[1].astype("int8")
    return data


def available_by_event_q1(
    data: pd.DataFrame,
    first_seen_qtime: dict[str, int],
    path: Path,
) -> pd.Series:
    """Return whether each NDC was first included by its row year's Q1."""
    first_seen = data["NDC"].map(first_seen_qtime)
    if first_seen.isna().any():
        examples = data.loc[first_seen.isna(), "NDC"].drop_duplicates().head(10).tolist()
        raise KeyError(f"{path.name} has NDCs missing from the first-seen lookup: {examples}")
    return first_seen.le(data["_year"].astype("int32") * 4 + 1)


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
    selected: dict[int, tuple[str, int]],
    block_number: int,
) -> None:
    """Select one deterministic Q1 formulary for each still-uncovered year."""
    missing = set(target_order) - set(selected)
    if not missing:
        return

    candidates = data.loc[
        data["_quarter"].eq(1) & data["_year"].isin(missing),
        ["_year", "FORMULARY_ID"],
    ].drop_duplicates()
    for year in target_order:
        if year in selected:
            continue
        values = candidates.loc[candidates["_year"].eq(year), "FORMULARY_ID"]
        if not values.empty:
            selected[year] = (str(values.iloc[0]), block_number)


def selected_rows(data: pd.DataFrame, selected: dict[int, tuple[str, int]]) -> pd.DataFrame:
    """Keep only Q1 rows belonging to the one selected formulary for each year."""
    selection = pd.DataFrame(
        {
            "_year": list(selected),
            "FORMULARY_ID": [values[0] for values in selected.values()],
        }
    )
    q1 = data.loc[data["_quarter"].eq(1)]
    return q1.merge(selection, on=["_year", "FORMULARY_ID"], how="inner", validate="many_to_one")


def accumulate_selected_rows(
    data: pd.DataFrame,
    events: tuple[EventSpec, ...],
    path: Path,
    first_seen_qtime: dict[str, int],
    firm_sets: dict[int, dict[str, set[str]]],
    ndc_sharing: dict[int, dict[tuple[str, int], dict[str, int]]],
) -> None:
    """Accumulate unique event firms and NDC share status from selected rows."""
    for year, quarter_data in data.groupby("_year", sort=False):
        available_mask = available_by_event_q1(quarter_data, first_seen_qtime, path)
        for event in events:
            raw_event_mask = binary_indicator(quarter_data, event.event_column, path).eq(1)
            if not raw_event_mask.any():
                continue
            if quarter_data.loc[raw_event_mask, "_quarter"].ne(1).any():
                raise ValueError(f"{path.name}.{event.event_column} contains an event outside Q1.")

            firm_sets[int(year)][event.slug].update(
                quarter_data.loc[raw_event_mask, "BoardName"].astype(str)
            )
            event_mask = raw_event_mask & available_mask
            for level in ATC_LEVELS:
                share_column = event.share_column(level)
                share = binary_indicator(quarter_data, share_column, path)
                if share.loc[~raw_event_mask].eq(1).any():
                    raise ValueError(f"{path.name}.{share_column} equals 1 outside matching event rows.")
                event_share = pd.DataFrame(
                    {
                        "NDC": quarter_data.loc[event_mask, "NDC"].astype(str),
                        "share": share.loc[event_mask].to_numpy(),
                    }
                ).groupby("NDC", as_index=False)["share"].max()
                values = ndc_sharing[int(year)][(event.slug, level)]
                for ndc, share_value in event_share.itertuples(index=False, name=None):
                    values[str(ndc)] = max(values.get(str(ndc), 0), int(share_value))


def initialize_accumulators(
    years: list[int],
    events: tuple[EventSpec, ...],
) -> tuple[dict[int, dict[str, set[str]]], dict[int, dict[tuple[str, int], dict[str, int]]]]:
    """Create empty compact accumulators for all target year-event cells."""
    firm_sets = {
        year: {event.slug: set() for event in events}
        for year in years
    }
    ndc_sharing = {
        year: {(event.slug, level): {} for event in events for level in ATC_LEVELS}
        for year in years
    }
    return firm_sets, ndc_sharing


def selection_manifest(selected: dict[int, tuple[str, int]], years: list[int]) -> pd.DataFrame:
    """Return the reproducibility record for all representative formularies."""
    missing = [year for year in years if year not in selected]
    if missing:
        raise RuntimeError(f"No Q1 formulary was selected for target years: {missing}")
    return pd.DataFrame(
        {
            "year": years,
            "formulary_id": [selected[year][0] for year in years],
            "source_block": [selected[year][1] for year in years],
        }
    )


def firm_summary(years: list[int], event: EventSpec, firm_sets: dict[int, dict[str, set[str]]]) -> pd.DataFrame:
    """Return annual unique event-firm counts for one event-direction pair."""
    return pd.DataFrame(
        {
            "year": years,
            "event_type": event.event_type,
            "direction": event.direction,
            "event_boardnames": [len(firm_sets[year][event.slug]) for year in years],
        }
    )


def ndc_summary(
    years: list[int],
    event: EventSpec,
    atc_level: int,
    ndc_sharing: dict[int, dict[tuple[str, int], dict[str, int]]],
) -> pd.DataFrame:
    """Return annual unique event NDC counts split by ATC sharing status."""
    sharing = []
    nonsharing = []
    for year in years:
        values = ndc_sharing[year][(event.slug, atc_level)].values()
        sharing.append(sum(value == 1 for value in values))
        nonsharing.append(sum(value == 0 for value in values))
    return pd.DataFrame(
        {
            "year": years,
            "event_type": event.event_type,
            "direction": event.direction,
            "atc_level": atc_level,
            "sharing_ndcs": sharing,
            "nonsharing_ndcs": nonsharing,
            "event_ndcs": [share + nonshare for share, nonshare in zip(sharing, nonsharing, strict=True)],
        }
    )


def save_firm_plot(summary: pd.DataFrame, event: EventSpec, path: Path) -> None:
    """Save one annual unique-event-firm bar chart."""
    path.parent.mkdir(parents=True, exist_ok=True)
    figure, axis = plt.subplots(figsize=(11, 5))
    axis.bar(summary["year"].astype(str), summary["event_boardnames"], color="#4C78A8")
    axis.set_xlabel("Year")
    axis.set_ylabel("Unique BoardName count")
    axis.set_title(f"{event.label}: annual firms with an event")
    axis.grid(axis="y", alpha=0.25)
    axis.tick_params(axis="x", rotation=60)
    figure.tight_layout()
    figure.savefig(path, dpi=300)
    plt.close(figure)


def save_ndc_plot(summary: pd.DataFrame, event: EventSpec, atc_level: int, path: Path) -> None:
    """Save one stacked annual event-NDC sharing bar chart."""
    path.parent.mkdir(parents=True, exist_ok=True)
    figure, axis = plt.subplots(figsize=(11, 5))
    years = summary["year"].astype(str)
    sharing = summary["sharing_ndcs"]
    nonsharing = summary["nonsharing_ndcs"]
    axis.bar(years, sharing, label="Sharing", color="#59A14F")
    axis.bar(years, nonsharing, bottom=sharing, label="Not sharing", color="#E15759")
    axis.set_xlabel("Year")
    axis.set_ylabel("Unique NDC count")
    axis.set_title(f"{event.label}: annual event NDCs, ATC{atc_level}")
    axis.legend()
    axis.grid(axis="y", alpha=0.25)
    axis.tick_params(axis="x", rotation=60)
    figure.tight_layout()
    figure.savefig(path, dpi=300)
    plt.close(figure)


def write_outputs(
    years: list[int],
    selected: dict[int, tuple[str, int]],
    opened_blocks: list[int],
    events: tuple[EventSpec, ...],
    firm_sets: dict[int, dict[str, set[str]]],
    ndc_sharing: dict[int, dict[tuple[str, int], dict[str, int]]],
) -> None:
    """Write the manifest plus all requested event-firm and event-NDC diagnostics."""
    for directory in (CSV_ROOT / "selection", CSV_ROOT / "firm", CSV_ROOT / "ndc_share"):
        directory.mkdir(parents=True, exist_ok=True)
    selection_manifest(selected, years).to_csv(CSV_ROOT / "selection" / "formularies.csv", index=False)
    pd.DataFrame({"opened_block": opened_blocks}).to_csv(CSV_ROOT / "selection" / "blocks.csv", index=False)

    for event in events:
        firms = firm_summary(years, event, firm_sets)
        firms.to_csv(CSV_ROOT / "firm" / f"{event.slug}.csv", index=False)
        save_firm_plot(firms, event, FIGURE_ROOT / "firm" / f"{event.slug}.png")
        for level in ATC_LEVELS:
            ndcs = ndc_summary(years, event, level, ndc_sharing)
            ndcs.to_csv(CSV_ROOT / "ndc_share" / f"{event.slug}_atc{level}.csv", index=False)
            save_ndc_plot(ndcs, event, level, FIGURE_ROOT / "ndc_share" / f"{event.slug}_atc{level}.png")


def main() -> None:
    """Select one Q1 formulary per year, compute diagnostics, and save results."""
    validate_block_order()
    events = configured_events()
    years = target_years()
    columns = required_columns(events)
    first_seen_qtime = load_first_seen_lookup()
    selected: dict[int, tuple[str, int]] = {}
    opened_blocks: list[int] = []
    firm_sets, ndc_sharing = initialize_accumulators(years, events)

    for block_number in tqdm(BLOCK_ORDER, desc="Opening prioritized panel blocks", unit="block"):
        if len(selected) == len(years):
            break
        path = panel_path(block_number)
        validate_schema(path, columns)
        opened_blocks.append(block_number)
        reader = pd.read_csv(path, usecols=columns, dtype="string", chunksize=CHUNKSIZE)
        for data in tqdm(reader, desc=f"Reading block {block_number}", unit="chunk", leave=False):
            data = normalize_data(data, path)
            add_new_selections(data, years, selected, block_number)
            retained = selected_rows(data, selected)
            if not retained.empty:
                accumulate_selected_rows(
                    retained,
                    events,
                    path,
                    first_seen_qtime,
                    firm_sets,
                    ndc_sharing,
                )
            del data, retained

    if len(selected) != len(years):
        missing = [year for year in years if year not in selected]
        raise RuntimeError(f"Stopped after all blocks but target years remain uncovered: {missing}")

    write_outputs(years, selected, opened_blocks, events, firm_sets, ndc_sharing)
    print(f"Opened {len(opened_blocks)} of {N_FORMULARY_BLOCKS} panel blocks: {opened_blocks}")
    print(f"Saved CSV summaries under: {CSV_ROOT}")
    print(f"Saved figures under: {FIGURE_ROOT}")


if __name__ == "__main__":
    main()
