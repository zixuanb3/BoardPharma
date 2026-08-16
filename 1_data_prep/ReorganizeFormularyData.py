r"""
Purpose:
Reorganize complete-formulary panel blocks into calendar-quarter files without
loading any full input block or the combined panel into memory.

Process:
1. Stream only YEAR_Q from every configured formulary block to inventory the
   quarters present in each source file.
2. Stream every source block once more in chunks and append each row to the
   matching formulary_panel_YYYYQX.csv output.
3. Validate that every discovered quarter receives rows and print a concise
   output inventory.  Both passes expose file-level and chunk-level progress.

Input:
- data/formulary_panel/formulary_panel_1.csv through
  data/formulary_panel/formulary_panel_{n_input_blocks}.csv

Output:
- data/formulary_panel_by_time/formulary_panel_YYYYQX.csv
"""

from __future__ import annotations

import gc
import re
from contextlib import ExitStack
from pathlib import Path

import pandas as pd
from tqdm.auto import tqdm


# Configure project directory paths
CURRENT_PATH = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_PATH.parent.parent
INPUT_DIR = PROJECT_ROOT / "data" / "formulary_panel"
OUTPUT_DIR = PROJECT_ROOT / "data" / "formulary_panel_by_time"

QUARTER_PATTERN = re.compile(r"^(\d{4})\s*Q([1-4])$")


# ========================== USER CONFIG ==========================
# n_input_blocks:
# - Must match the number of blocks produced by FormularyPanelMaker.py.
#
# chunksize:
# - Controls only streaming memory.  No chunk is treated as a complete panel.
#
# formulary_time_shift_quarters:
# - Must match FormularyPanelMaker.py.  The default 0 preserves existing paths;
#   nonzero shifts read/write shift-specific subdirectories.
RUN_CONFIG = {
    "n_input_blocks": 30,
    "chunksize": 1_000_000,
    "formulary_time_shift_quarters": 1,
}
# ===============================================================


# ========================== VALIDATION HELPERS ==========================


def normalize_year_q(values: pd.Series, source_name: str) -> pd.Series:
    """Return canonical YYYY QX labels and reject malformed or missing values."""
    cleaned = values.astype("string").str.strip().str.upper()
    valid = cleaned.str.fullmatch(QUARTER_PATTERN, na=False)
    if not valid.all():
        examples = values.loc[~valid].head(10).tolist()
        raise ValueError(
            f"{source_name} contains invalid YEAR_Q values. Examples: {examples}"
        )
    return cleaned


def quarter_sort_key(year_q: str) -> tuple[int, int]:
    """Return the chronological sort key for one validated YEAR_Q label."""
    match = QUARTER_PATTERN.fullmatch(year_q)
    if match is None:
        raise ValueError(f"Invalid canonical YEAR_Q value: {year_q}")
    return int(match.group(1)), int(match.group(2))


def quarter_file_tag(year_q: str) -> str:
    """Convert '2020 Q1' to the output filename tag '2020Q1'."""
    return year_q.replace(" ", "")


def shift_label(shift_quarters: int) -> str:
    """Return the folder label for a formulary quarter shift."""
    return f"shift_q{shift_quarters:+d}".replace("+", "")


def input_dir(shift_quarters: int) -> Path:
    """Return the source panel directory for one timing specification."""
    return INPUT_DIR if shift_quarters == 0 else INPUT_DIR / shift_label(shift_quarters)


def output_dir(shift_quarters: int) -> Path:
    """Return the quarter-organized output directory for one timing specification."""
    return OUTPUT_DIR if shift_quarters == 0 else OUTPUT_DIR / shift_label(shift_quarters)


def validate_config(config: dict) -> tuple[int, int, int]:
    """Validate and normalize the small run configuration."""
    n_blocks = int(config["n_input_blocks"])
    chunksize = int(config["chunksize"])
    time_shift = int(config["formulary_time_shift_quarters"])
    if n_blocks < 1:
        raise ValueError("n_input_blocks must be at least 1.")
    if chunksize < 1:
        raise ValueError("chunksize must be at least 1.")
    return n_blocks, chunksize, time_shift


def input_paths(source_dir: Path, n_blocks: int) -> list[Path]:
    """Return and validate the ordered complete-formulary source paths."""
    paths = [source_dir / f"formulary_panel_{block}.csv" for block in range(1, n_blocks + 1)]
    missing = [path for path in paths if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Missing formulary panel blocks: {missing[:10]}")
    return paths


# ========================== QUARTER INVENTORY ==========================


def scan_quarter_inventory(paths: list[Path], chunksize: int) -> dict[Path, set[str]]:
    """Stream only YEAR_Q and record the quarters present in every source block."""
    inventory: dict[Path, set[str]] = {}
    file_progress = tqdm(paths, desc="Pass 1/2: inventorying block quarters", unit="file")
    for path in file_progress:
        file_progress.set_postfix_str(path.name)
        quarters: set[str] = set()
        reader = pd.read_csv(
            path,
            usecols=["YEAR_Q"],
            dtype="string",
            chunksize=chunksize,
        )
        chunk_progress = tqdm(
            reader,
            desc=f"  {path.stem}: YEAR_Q chunks",
            unit="chunk",
            leave=False,
        )
        for chunk in chunk_progress:
            clean_quarters = normalize_year_q(chunk["YEAR_Q"], path.name)
            quarters.update(clean_quarters.unique().tolist())
            chunk_progress.set_postfix_str(f"quarters={len(quarters)}")
            del chunk, clean_quarters
            gc.collect()
        if not quarters:
            raise ValueError(f"{path.name} contains no data rows.")
        inventory[path] = quarters
    return inventory


def all_discovered_quarters(inventory: dict[Path, set[str]]) -> list[str]:
    """Return every discovered quarter in chronological order."""
    quarters = set().union(*inventory.values()) if inventory else set()
    if not quarters:
        raise ValueError("No YEAR_Q values were discovered in the input blocks.")
    return sorted(quarters, key=quarter_sort_key)


# ========================== QUARTER ROUTING ==========================


def output_paths(destination_dir: Path, quarters: list[str]) -> dict[str, Path]:
    """Map canonical YEAR_Q labels to their quarter-organized output paths."""
    return {
        year_q: destination_dir / f"formulary_panel_{quarter_file_tag(year_q)}.csv"
        for year_q in quarters
    }


def prepare_output_directory(destination_dir: Path, paths: dict[str, Path]) -> None:
    """Create the output directory and replace prior quarter outputs."""
    destination_dir.mkdir(parents=True, exist_ok=True)
    for path in paths.values():
        if path.exists():
            path.unlink()


def route_blocks_by_quarter(
    inventory: dict[Path, set[str]],
    paths: dict[str, Path],
    chunksize: int,
) -> dict[str, int]:
    """Stream full chunks once and append rows to their calendar-quarter files."""
    row_counts = {year_q: 0 for year_q in paths}
    headers_written = {year_q: False for year_q in paths}

    with ExitStack() as stack:
        handles = {
            year_q: stack.enter_context(path.open("w", encoding="utf-8", newline=""))
            for year_q, path in paths.items()
        }
        file_progress = tqdm(
            list(inventory),
            desc="Pass 2/2: routing blocks by quarter",
            unit="file",
        )
        for source_path in file_progress:
            file_progress.set_postfix_str(source_path.name)
            reader = pd.read_csv(source_path, dtype="string", chunksize=chunksize)
            chunk_progress = tqdm(
                reader,
                desc=f"  {source_path.stem}: full-data chunks",
                unit="chunk",
                leave=False,
            )
            for chunk in chunk_progress:
                chunk["YEAR_Q"] = normalize_year_q(chunk["YEAR_Q"], source_path.name)
                unexpected = set(chunk["YEAR_Q"].unique()) - inventory[source_path]
                if unexpected:
                    raise RuntimeError(
                        f"Pass 2 found quarters absent from pass 1 in {source_path.name}: "
                        f"{sorted(unexpected, key=quarter_sort_key)}"
                    )

                for year_q, subset in chunk.groupby("YEAR_Q", sort=False):
                    subset.to_csv(
                        handles[str(year_q)],
                        index=False,
                        header=not headers_written[str(year_q)],
                    )
                    headers_written[str(year_q)] = True
                    row_counts[str(year_q)] += len(subset)
                    del subset
                chunk_progress.set_postfix_str(
                    f"rows routed={sum(row_counts.values()):,}"
                )
                del chunk
                gc.collect()

    empty_outputs = [year_q for year_q, count in row_counts.items() if count == 0]
    if empty_outputs:
        raise RuntimeError(f"No rows were written for discovered quarters: {empty_outputs}")
    return row_counts


# ========================== OUTPUT DISPATCH ==========================


def main() -> None:
    """Run both streaming passes and write one complete file per quarter."""
    n_blocks, chunksize, time_shift = validate_config(RUN_CONFIG)
    source_dir = input_dir(time_shift)
    destination_dir = output_dir(time_shift)
    sources = input_paths(source_dir, n_blocks)
    inventory = scan_quarter_inventory(sources, chunksize)
    quarters = all_discovered_quarters(inventory)
    targets = output_paths(destination_dir, quarters)
    prepare_output_directory(destination_dir, targets)
    row_counts = route_blocks_by_quarter(inventory, targets, chunksize)

    print(f"Saved {len(quarters)} quarter files to: {destination_dir}")
    for year_q in quarters:
        print(f"  {quarter_file_tag(year_q)}: {row_counts[year_q]:,} rows")


if __name__ == "__main__":
    main()
