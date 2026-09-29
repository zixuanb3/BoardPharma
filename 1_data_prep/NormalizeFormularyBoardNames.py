r"""Rewrite expanded-formulary BoardName from the audited expanded mapping.

Only ``BoardName`` changes. Accepted mapping rows are restricted to
``audit_keep == 1`` and joined through the same normalized LabelerName key used
by ``scripts/LabelerCompanyMappingMaker.py``. The 14 GB panel is replaced only
after a complete readback verifies every rewritten value.

Run from ``codes``:
    python 1_data_prep/NormalizeFormularyBoardNames.py
"""

from __future__ import annotations

import os
import re
import shutil
import unicodedata
from collections import Counter
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAPPING_PATH = PROJECT_ROOT / "crosswalks" / "labeler_board_name_mapping_expanded.csv"
PANEL_PATH = Path(r"D:\task1_expanded_brand_panel\task1_expanded_brand_panel.csv")
READ_OPTIONS = {"dtype": "string", "keep_default_na": False, "encoding": "utf-8-sig"}
CHUNKSIZE = 250_000

# These rules intentionally match scripts/LabelerCompanyMappingMaker.py.
LEGAL_EQ = {"INCORPORATED": "INC", "CORPORATION": "CORP", "COMPANY": "CO", "LIMITED": "LTD"}
LEGAL_SUFFIXES = {"INC", "LLC", "LTD", "CO", "CORP", "PLC", "LP", "LLP"}
ORGANIZATION_SUFFIXES = {"GROUP", "HOLDING", "HOLDINGS"}
TYPO = {
    "PHRAMACEUTICALS": "PHARMACEUTICALS", "INCOPORATED": "INCORPORATED",
    "PHARMACEUTIALS": "PHARMACEUTICALS", "PHARMACUTICALS": "PHARMACEUTICALS",
    "PHARMACUETICALS": "PHARMACEUTICALS", "PHARAMACEUTICALS": "PHARMACEUTICALS",
    "LABRATORIES": "LABORATORIES", "INDUSTIRES": "INDUSTRIES",
    "THERAPUETICS": "THERAPEUTICS",
}
INDUSTRY_EQ = {
    "PHARMACEUTICALS": "PHARMA", "PHARMACEUTICAL": "PHARMA",
    "LABORATORIES": "LAB", "LABORATORY": "LAB", "LABS": "LAB",
    "THERAPEUTICS": "THERAPEUTIC", "BIOPHARMACEUTICALS": "BIOPHARMA",
    "BIOPHARMACEUTICAL": "BIOPHARMA",
}
SPACE = re.compile(r"\s+")
PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
STATUS = re.compile(r"\([^()]*(?:PRIOR TO|FORMERLY|DE-LISTED|DELISTED|DISSOLVED|INACTIVE)[^()]*\)")
BRACKETS = re.compile(r"\([^()]*\)|\[[^\[\]]*\]|\{[^{}]*\}")
RELATION_TAIL = re.compile(r"\b(?:D/B/A|DBA|A SUBSIDIARY OF|A DIVISION OF|DIVISION OF|DIV)\b")
PHRASE_EQ = re.compile(r"\b(?:DESIGNATED ACTIVITY CO|L L C|L L P|L P|P L C)\b")
PHRASE_REPLACEMENTS = {"DESIGNATED ACTIVITY CO": "DAC", "L L C": "LLC", "L L P": "LLP", "L P": "LP", "P L C": "PLC"}


def normalize_name(value: str) -> str:
    """Return a case/punctuation-insensitive temporary join key."""
    base = SPACE.sub(" ", unicodedata.normalize("NFKC", value.strip().upper())).strip()
    base = "".join(
        character
        for character in unicodedata.normalize("NFKD", base.replace("\x92", "'").replace("\x91", "'"))
        if not unicodedata.combining(character)
    )
    base = STATUS.sub(" ", base)
    while BRACKETS.search(base):
        base = BRACKETS.sub(" ", base)
    base = RELATION_TAIL.split(base, maxsplit=1)[0]
    base = base.replace("&", " AND ").replace(".", "").replace("'", "").replace("’", "")
    base = SPACE.sub(" ", PUNCTUATION.sub(" ", base).replace("_", " ")).strip()
    tokens = [LEGAL_EQ.get(TYPO.get(token, token), TYPO.get(token, token)) for token in base.split()]
    base = PHRASE_EQ.sub(lambda match: PHRASE_REPLACEMENTS[match.group()], " ".join(tokens))
    tokens = [INDUSTRY_EQ.get(token, token) for token in base.split()]
    while tokens and tokens[-1] in LEGAL_SUFFIXES | ORGANIZATION_SUFFIXES:
        tokens.pop()
    if tokens and tokens[0] == "THE":
        tokens.pop(0)
    if tokens and tokens[-1] == "AND":
        tokens.pop()
    return "".join(tokens)


def load_board_mapping(path: Path = MAPPING_PATH) -> dict[str, str]:
    """Return accepted mappings, requiring one BoardName per normalized key."""
    raw = pd.read_csv(path, usecols=["LabelerName", "BoardName", "audit_keep"], **READ_OPTIONS)
    accepted = raw.loc[
        pd.to_numeric(raw["audit_keep"], errors="coerce").eq(1),
        ["LabelerName", "BoardName"],
    ].copy()
    accepted = accepted.apply(lambda column: column.str.strip())
    if accepted.empty or accepted.eq("").any().any():
        raise ValueError("Every audit_keep == 1 row must have LabelerName and BoardName.")
    accepted["match_key"] = accepted["LabelerName"].map(normalize_name)
    if accepted["match_key"].eq("").any():
        raise ValueError("An accepted LabelerName normalizes to an empty key.")
    names = accepted.groupby("match_key")["BoardName"].agg(
        lambda values: tuple(dict.fromkeys(values))
    )
    ambiguous = names[names.map(len).gt(1)]
    if not ambiguous.empty:
        raise ValueError(
            "Normalized LabelerName maps to multiple accepted BoardNames: "
            f"{ambiguous.to_dict()!r}"
        )
    return {key: values[0] for key, values in names.items()}


def rewrite_panel(mapping: dict[str, str]) -> tuple[int, Counter[str], int]:
    """Write, fully verify, and atomically install the remapped panel."""
    required_bytes = int(PANEL_PATH.stat().st_size * 1.10)
    free_bytes = shutil.disk_usage(PANEL_PATH.parent).free
    if free_bytes < required_bytes:
        raise OSError(
            f"Need {required_bytes / 1e9:.1f} GB free beside the panel; "
            f"only {free_bytes / 1e9:.1f} GB available."
        )
    temporary = PANEL_PATH.with_name(".task1_expanded_brand_panel.expanded_mapping.tmp.csv")
    if temporary.exists():
        raise FileExistsError(f"Temporary panel already exists: {temporary}")
    source_stat = PANEL_PATH.stat()
    rows = unmatched_rows = 0
    replacements: Counter[str] = Counter()
    labeler_lookup: dict[str, str] = {}
    columns: list[str] | None = None
    with temporary.open("x", encoding="utf-8-sig", newline="") as target:
        for chunk in pd.read_csv(PANEL_PATH, chunksize=CHUNKSIZE, **READ_OPTIONS):
            if columns is None:
                columns = list(chunk.columns)
                missing = {"BoardName", "LabelerName"} - set(columns)
                if missing:
                    raise KeyError(f"Panel is missing columns: {sorted(missing)}")
            elif list(chunk.columns) != columns:
                raise ValueError("Panel column order changed between chunks.")

            for labeler in chunk["LabelerName"].unique():
                if labeler not in labeler_lookup:
                    labeler_lookup[labeler] = mapping.get(normalize_name(labeler), "")
            board_name = chunk["LabelerName"].map(labeler_lookup)
            changed = chunk["BoardName"].ne(board_name)
            replacements.update(chunk.loc[changed, "LabelerName"].value_counts().to_dict())
            chunk["BoardName"] = board_name
            unmatched_rows += int(board_name.eq("").sum())
            chunk.to_csv(target, index=False, header=rows == 0, lineterminator="\n")
            rows += len(chunk)
        target.flush()
        os.fsync(target.fileno())

    verified_rows = verified_unmatched = 0
    for chunk in pd.read_csv(temporary, chunksize=CHUNKSIZE, **READ_OPTIONS):
        expected = chunk["LabelerName"].map(labeler_lookup)
        incorrect = chunk["BoardName"].ne(expected)
        if incorrect.any():
            labeler = chunk.loc[incorrect, "LabelerName"].iloc[0]
            raise ValueError(f"Incorrect BoardName for {labeler!r} in temporary panel.")
        verified_unmatched += int(chunk["BoardName"].eq("").sum())
        verified_rows += len(chunk)
    if (verified_rows, verified_unmatched) != (rows, unmatched_rows):
        raise ValueError("Temporary panel readback counts do not match the write pass.")
    current_stat = PANEL_PATH.stat()
    if (current_stat.st_size, current_stat.st_mtime_ns) != (source_stat.st_size, source_stat.st_mtime_ns):
        raise RuntimeError("Source panel changed during rewrite; it was not replaced.")

    backup = PANEL_PATH.with_name("task1_expanded_brand_panel.before_expanded_mapping.csv")
    if backup.exists():
        raise FileExistsError(f"Backup path already exists: {backup}")
    PANEL_PATH.replace(backup)
    try:
        temporary.replace(PANEL_PATH)
    except OSError:
        backup.replace(PANEL_PATH)
        raise
    backup.unlink()
    return rows, replacements, unmatched_rows


def main() -> None:
    """Install accepted expanded-mapping BoardNames in the expanded panel."""
    rows, replacements, unmatched = rewrite_panel(load_board_mapping())
    print(
        f"Installed expanded mapping for {rows:,} rows; "
        f"changed rows: {sum(replacements.values()):,}; unmatched rows: {unmatched:,}"
    )


if __name__ == "__main__":
    main()
