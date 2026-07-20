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
# overwrite_outputs:
# - Keep at 0 for a safe first run.  Set to 1 only when intentionally replacing
#   every existing quarter file in OUTPUT_DIR.
RUN_CONFIG = {
    "n_input_blocks": 30,
    "chunksize": 1_000_000,
    "overwrite_outputs": 0,
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


def validate_config(config: dict) -> tuple[int, int, bool]:
    """Validate and normalize the small run configuration."""
    n_blocks = int(config["n_input_blocks"])
    chunksize = int(config["chunksize"])
    overwrite = int(config["overwrite_outputs"])
    if n_blocks < 1:
        raise ValueError("n_input_blocks must be at least 1.")
    if chunksize < 1:
        raise ValueError("chunksize must be at least 1.")
    if overwrite not in {0, 1}:
        raise ValueError("overwrite_outputs must be 0 or 1.")
    return n_blocks, chunksize, bool(overwrite)


def input_paths(n_blocks: int) -> list[Path]:
    """Return and validate the ordered complete-formulary source paths."""
    paths = [INPUT_DIR / f"formulary_panel_{block}.csv" for block in range(1, n_blocks + 1)]
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


def output_paths(quarters: list[str]) -> dict[str, Path]:
    """Map canonical YEAR_Q labels to their quarter-organized output paths."""
    return {
        year_q: OUTPUT_DIR / f"formulary_panel_{quarter_file_tag(year_q)}.csv"
        for year_q in quarters
    }


def prepare_output_directory(paths: dict[str, Path], overwrite: bool) -> None:
    """Create the output directory and enforce the configured overwrite policy."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    existing = [path for path in paths.values() if path.exists()]
    if existing and not overwrite:
        raise FileExistsError(
            "Quarter outputs already exist. Move/delete them or set overwrite_outputs=1. "
            f"Examples: {existing[:5]}"
        )
    if overwrite:
        for path in existing:
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
    n_blocks, chunksize, overwrite = validate_config(RUN_CONFIG)
    sources = input_paths(n_blocks)
    inventory = scan_quarter_inventory(sources, chunksize)
    quarters = all_discovered_quarters(inventory)
    targets = output_paths(quarters)
    prepare_output_directory(targets, overwrite)
    row_counts = route_blocks_by_quarter(inventory, targets, chunksize)

    print(f"Saved {len(quarters)} quarter files to: {OUTPUT_DIR}")
    for year_q in quarters:
        print(f"  {quarter_file_tag(year_q)}: {row_counts[year_q]:,} rows")


if __name__ == "__main__":
    main()
