"""
Purpose:
Generate ATC-sharing labels for cohort files, then plot treated sharing vs
non-sharing distributions across cohort years.

Process:
- Load req-valid movement partners and ATC mapping tables.
- For each configured cohort design, label treated firm-product units as
    atc3_sharing when their ATC peers overlap with movement partners.
- Save enriched cohort files under *_with_atc3sharing folders.
- Build diagnostic plots for configured periods.

Input:
- data/atc3mapping/atc3mapping_year_level[_level2].csv
- data/cohort_data/{panel_level}-level_{A|B}_{with|without}_{B|A}/{event|first_event}/req{n}/...
- data/movement_tables/movement_event_candidates.csv

Output:
- data/cohort_data_with_atc3sharing/{panel_level}-level_{A|B}_{with|without}_{B|A}/{event|first_event}/req{n}/...
- figures/cohort_sharing_atc3/{panel_level}-level_{A|B}_{with|without}_{B|A}/{event|first_event}/req{n}/...
"""

import ast
from functools import lru_cache
from pathlib import Path
import matplotlib.pyplot as plt
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ATC3_MAP_DIR = PROJECT_ROOT / "data" / "atc3mapping"
COHORT_ROOT = PROJECT_ROOT / "data" / "cohort_data"
STAGGERED_ROOT = PROJECT_ROOT / "data" / "staggered_data"
EVENT_XLSX_B = PROJECT_ROOT / "data" / "event_B.xlsx"
EVENT_XLSX_A = PROJECT_ROOT / "data" / "event_A.xlsx"
MOVEMENT_CANDIDATES_PATH = PROJECT_ROOT / "data" / "movement_tables" / "movement_event_candidates.csv"
INDIRECT_INTERLOCK_CANDIDATES_PATH = (
    PROJECT_ROOT / "data" / "movement_tables" / "indirect_interlock_event_candidates.csv"
)

# ========================== USER CONFIG ==========================
# EVENTS:
# - Four director-board shocks used to define event-partner networks.
# - Changing this changes which partner network is used for ATC overlap checks.
#
# EVENT_TYPES:
# - "event" vs "first_event".
# - Changes whether overlap checks are anchored to every event year or only first event year.
#
# PANEL_LEVELS:
# - "quarter" and/or "year".
#
# CONTROL_FOLDERS:
# - "Not", "Not Yet", "Pure Control".
# - Controls which cohort-design variants are traversed for enrichment and diagnostics.
#
# EVENT_REQUIREMENTS:
# - 0 / 1 / 2 correspond to req0 / req1 / req2 cohort folders.
# - These are used only by the cohort movement workflow below.
# - Pair filtering always requires the movement stay_{x}_years flag.
# - req1 and req2 additionally require requirement1 == 1.
# - req2 treated filtering itself is already encoded upstream in the req2 cohort files,
#   so this script does not apply a second requirement2 filter at the pair level.
#
# ATC3_SHARING_PERIODS:
# - Relative periods used to define saved atc3_sharing labels.
# - [0] means labels are defined from event-year observations only.
#
# PERIODS:
# - Relative periods used only for plotted diagnostics.
# - Does not change exported cohort files.
#
# atc_level:
# - 1 keeps original ATC3.
# - 2 truncates the last character and coarsens categories.
# - 3 keeps "Device" if it starts with "Device", otherwise keeps only the first character.
# - Coarser ATC usually increases the chance of being tagged as sharing.
#
# treatment_groups / include_eventpair:
# - Only applied to cohort data traversal.
# - Staggered traversal remains unchanged.
RUN_CONFIG = {
    "EVENTS": [
        "indirect_interlock"
    ],
    "EVENT_TYPES": ["event"], #"first_event"
    "PANEL_LEVELS": ["quarter"],
    "CONTROL_FOLDERS": ["Not", "Not Yet", "Pure Control"],
    "EVENT_REQUIREMENTS": [0, 1, 2],
    "ATC3_SHARING_PERIODS": [0],
    "PERIODS": [0],
    "atc_level": 2,
    "COHORT_YEARS": list(range(2009, 2019)),
    "treatment_groups": ["B","A"],
    "include_eventpair": [0], # 1
}
"""
        "to_B_not_in_A",
        "to_B_still_in_A",
        "interlock_dissolution",
        "direct_interlock",
        "indirect_interlock",
"""
# ===============================================================
EVENTS = RUN_CONFIG["EVENTS"]
EVENT_TYPES = RUN_CONFIG["EVENT_TYPES"]
PANEL_LEVELS = RUN_CONFIG["PANEL_LEVELS"]
CONTROL_FOLDERS = RUN_CONFIG["CONTROL_FOLDERS"]
EVENT_REQUIREMENTS = [int(x) for x in RUN_CONFIG["EVENT_REQUIREMENTS"]]
ATC3_SHARING_PERIODS = RUN_CONFIG["ATC3_SHARING_PERIODS"]
PERIODS = RUN_CONFIG["PERIODS"]
COHORT_YEARS = RUN_CONFIG["COHORT_YEARS"]
ATC_LEVEL = int(RUN_CONFIG["atc_level"])
TREATMENT_GROUPS = [str(x).upper() for x in RUN_CONFIG.get("treatment_groups", ["B"])]
INCLUDE_EVENTPAIR_VALUES = [int(x) for x in RUN_CONFIG.get("include_eventpair", [1])]

LEVEL_SUFFIX = "" if ATC_LEVEL == 1 else f"_level{ATC_LEVEL}"
COHORT_OUT_ROOT = PROJECT_ROOT / "data" / f"cohort_data_with_atc3sharing{LEVEL_SUFFIX}"
STAGGERED_OUT_ROOT = PROJECT_ROOT / "data" / f"staggered_data_with_atc3sharing{LEVEL_SUFFIX}"
FIG_ROOT = PROJECT_ROOT / "figures" / f"cohort_sharing_atc3{LEVEL_SUFFIX}"
STAGGERED_FIG_ROOT = PROJECT_ROOT / "figures" / f"staggered_sharing_atc3{LEVEL_SUFFIX}"

STAGGERED_CONTROL_FOLDER_MAP = {
    "not_yet": "Not Yet",
    "pure_control": "Pure Control",
}


def staggered_level_folder(panel_level: str) -> str:
    return "year-level" if panel_level == "year" else "quarter-level"


def cohort_group_label(treatment_group: str, include_eventpair: int) -> str:
    """Return cohort group suffix like B_with_A / A_without_B."""
    tg = str(treatment_group).upper()
    ie = int(include_eventpair)
    if tg not in {"A", "B"}:
        raise ValueError("treatment_group must be one of: A, B")
    if ie not in {0, 1}:
        raise ValueError("include_eventpair must be one of: 0, 1")
    counterpart = "B" if tg == "A" else "A"
    relation = "with" if ie == 1 else "without"
    return f"{tg}_{relation}_{counterpart}"


def cohort_level_folder(panel_level: str, treatment_group: str, include_eventpair: int) -> str:
    """Return cohort level folder name with group suffix."""
    return f"{panel_level}-level_{cohort_group_label(treatment_group, include_eventpair)}"


def requirement_folder(event_requirement: int) -> str:
    """Return req{n} folder label."""
    req = int(event_requirement)
    if req not in {0, 1, 2}:
        raise ValueError("event_requirement must be one of: 0, 1, 2")
    return f"req{req}"


def parse_staggered_file_name(file_name: str) -> dict[str, str] | None:
    """Parse staggered filename metadata.

    Expected pattern:
    staggered_firm_level_panel_{panel}_{event}_{control}_balanced.csv
    """
    stem = Path(file_name).stem
    prefix = "staggered_firm_level_panel_"
    suffix = "_balanced"

    if not stem.startswith(prefix) or not stem.endswith(suffix):
        return None

    body = stem[len(prefix):-len(suffix)]

    panel_level = None
    rest = None
    for p in ["year", "quarter"]:
        marker = f"{p}_"
        if body.startswith(marker):
            panel_level = p
            rest = body[len(marker):]
            break
    if panel_level is None or rest is None:
        return None

    control_type = None
    event = None
    for c in STAGGERED_CONTROL_FOLDER_MAP:
        marker = f"_{c}"
        if rest.endswith(marker):
            control_type = c
            event = rest[:-len(marker)]
            break
    if control_type is None or event is None:
        return None
    # Skip interlock_dissolution for staggered processing as requested
    if event not in [e for e in EVENTS if e != "interlock_dissolution"]:
        return None

    return {
        "panel_level": panel_level,
        "event": event,
        "control_type": control_type,
    }


def apply_atc_level(df: pd.DataFrame, atc_level: int) -> pd.DataFrame:
    """Return a copy transformed for requested ATC level."""
    out = df.copy()
    if atc_level == 2:
        out["atc3"] = out["atc3"].astype(str).str[:-1]
    elif atc_level == 3:
        out["atc3"] = out["atc3"].astype(str).apply(lambda x: 'Device' if x.startswith('Device') else x[0])
    return out


def parse_list_cell(value) -> list[str]:
    """Parse list-like cell from Excel into python list."""
    if pd.isna(value):
        return []
    if isinstance(value, list):
        return [str(x) for x in value if pd.notna(x)]
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return []
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, list):
                return [str(x) for x in parsed if pd.notna(x)]
        except (ValueError, SyntaxError):
            pass

        # Fallback for plain list-like text such as "['a','b']".
        if text.startswith("[") and text.endswith("]"):
            inner = text[1:-1].strip()
            if inner == "":
                return []
            parts = [p.strip().strip("'\"") for p in inner.split(",")]
            return [p for p in parts if p != ""]

        return [text]

    return [str(value)]


@lru_cache(maxsize=1)
def load_movement_candidates() -> pd.DataFrame:
    """Load movement candidates once and detect the unique stay column."""
    movement = pd.read_csv(MOVEMENT_CANDIDATES_PATH)
    stay_cols = [
        column
        for column in movement.columns
        if str(column).startswith("stay_") and str(column).endswith("_years")
    ]
    if len(stay_cols) != 1:
        raise ValueError(
            "Expected exactly one stay_{x}_years column in movement candidates, "
            f"found: {stay_cols}"
        )
    stay_col = stay_cols[0]
    movement["event_year"] = pd.to_numeric(movement["event_year"], errors="raise").astype(int)
    movement["requirement1"] = pd.to_numeric(movement["requirement1"], errors="raise").astype(int)
    movement[stay_col] = pd.to_numeric(movement[stay_col], errors="raise").astype(int)
    movement.attrs["stay_col"] = stay_col
    return movement


def filter_movement_pairs(event_requirement: int) -> pd.DataFrame:
    """Return req-valid movement rows for ATC-sharing matching."""
    base_movement = load_movement_candidates()
    stay_col = base_movement.attrs["stay_col"]
    movement = base_movement.copy()
    movement = movement.loc[movement[stay_col].eq(1)].copy()
    if int(event_requirement) in {1, 2}:
        movement = movement.loc[movement["requirement1"].eq(1)].copy()
    return movement


@lru_cache(maxsize=1)
def load_indirect_interlock_candidates() -> pd.DataFrame:
    """Load indirect interlock candidates once and detect the unique stay column."""
    indirect = pd.read_csv(INDIRECT_INTERLOCK_CANDIDATES_PATH)
    stay_cols = [
        column
        for column in indirect.columns
        if str(column).startswith("stay_") and str(column).endswith("_years")
    ]
    if len(stay_cols) != 1:
        raise ValueError(
            "Expected exactly one stay_{x}_years column in indirect interlock candidates, "
            f"found: {stay_cols}"
        )
    stay_col = stay_cols[0]
    indirect["event_year"] = pd.to_numeric(indirect["event_year"], errors="raise").astype(int)
    indirect["requirement1"] = pd.to_numeric(indirect["requirement1"], errors="raise").astype(int)
    indirect["requirement2"] = pd.to_numeric(indirect["requirement2"], errors="raise").astype(int)
    indirect[stay_col] = pd.to_numeric(indirect[stay_col], errors="raise").astype(int)
    indirect.attrs["stay_col"] = stay_col
    return indirect


def filter_indirect_interlock_pairs(event_requirement: int) -> pd.DataFrame:
    """Return req-valid indirect interlock rows for ATC-sharing matching."""
    base_indirect = load_indirect_interlock_candidates()
    stay_col = base_indirect.attrs["stay_col"]
    indirect = base_indirect.copy()
    indirect = indirect.loc[indirect[stay_col].eq(1)].copy()
    if int(event_requirement) in {1, 2}:
        indirect = indirect.loc[indirect["requirement1"].eq(1)].copy()
    if int(event_requirement) == 2:
        indirect = indirect.loc[indirect["requirement2"].eq(1)].copy()
    return indirect


def cohort_file_path(
    panel_level: str,
    event_type: str,
    event: str,
    cohort_year: int,
    treatment_group: str,
    include_eventpair: int,
    control_folder: str,
    event_requirement: int,
) -> Path:
    """Build cohort file path following CohortPanelMaker naming."""
    treat_suffix = "_first_event" if event_type == "first_event" else ""
    file_name = f"{event}_{panel_level}_cohort_{cohort_year}{treat_suffix}_balanced.csv"
    if event == "indirect_interlock":
        level_folder = f"{panel_level}-level"
    else:
        level_folder = cohort_level_folder(panel_level, treatment_group, include_eventpair)
    return (
        COHORT_ROOT
        / level_folder
        / event_type
        / requirement_folder(event_requirement)
        / control_folder
        / file_name
    )


def load_atc3_mapping(panel_level: str, atc_level: int = 1) -> pd.DataFrame:
    """Load year-level ATC3 mapping.
    
    Parameters:
    -----------
    panel_level : str
        Kept for compatibility; mapping is always year-level.
    atc_level : int
        1 for standard level, 2 for level2 version, 3 for level3 version
    """
    level_suffix = "" if atc_level == 1 else f"_level{atc_level}"
    path = ATC3_MAP_DIR / f"atc3mapping_year_level{level_suffix}.csv"
    df = pd.read_csv(path)
    return df


def load_movement_event_pairs(
    treatment_group: str = "B",
    event_requirement: int = 0,
) -> pd.DataFrame:
    """Build cohort event-pair rows directly from req-valid movement candidates."""
    movement = filter_movement_pairs(event_requirement=event_requirement)
    tg = str(treatment_group).upper()
    if tg not in {"A", "B"}:
        raise ValueError("treatment_group must be one of: A, B")

    treated_col = "FirmB" if tg == "B" else "FirmA"
    counterpart_col = "FirmA" if tg == "B" else "FirmB"

    event_long = (
        movement.rename(
            columns={
                "event_type": "event",
                "event_year": "year",
                treated_col: "BoardName",
                counterpart_col: "BoardNamePair",
            }
        )[["BoardName", "year", "event", "BoardNamePair"]]
        .dropna(subset=["BoardName", "year", "event", "BoardNamePair"])
        .drop_duplicates()
        .reset_index(drop=True)
    )
    event_long["year"] = event_long["year"].astype(int)
    return event_long


def load_indirect_interlock_event_pairs(event_requirement: int = 0) -> pd.DataFrame:
    """Build indirect interlock event-pair rows from req-valid directed candidates."""
    indirect = filter_indirect_interlock_pairs(event_requirement=event_requirement)
    event_long = (
        indirect.rename(
            columns={
                "event_type": "event",
                "event_year": "year",
            }
        )[["BoardName", "year", "event", "BoardNamePair"]]
        .dropna(subset=["BoardName", "year", "event", "BoardNamePair"])
        .drop_duplicates()
        .reset_index(drop=True)
    )
    event_long["year"] = event_long["year"].astype(int)
    return event_long


def event_table_path(treatment_group: str) -> Path:
    """Return event table path by treated-group direction."""
    tg = str(treatment_group).upper()
    if tg not in {"A", "B"}:
        raise ValueError("treatment_group must be one of: A, B")
    return EVENT_XLSX_A if tg == "A" else EVENT_XLSX_B


def load_event_pairs(treatment_group: str = "B") -> pd.DataFrame:
    """Load event_{A|B}.xlsx and explode BoardNamePair list into long form."""
    event_path = event_table_path(treatment_group)
    if not event_path.exists():
        raise FileNotFoundError(
            f"Missing event table for treatment_group={str(treatment_group).upper()}: {event_path}"
        )

    event_df = pd.read_excel(event_path)
    needed = ["BoardName", "year", "event", "BoardNamePair"]
    event_df = event_df[needed].copy()
    event_df["BoardNamePair"] = event_df["BoardNamePair"].apply(parse_list_cell)

    event_long = event_df.explode("BoardNamePair", ignore_index=True)
    event_long = event_long.dropna(subset=["BoardNamePair"])
    # Keep unique board-year-event-pair links.
    event_long = event_long.drop_duplicates(
        subset=["BoardName", "year", "event", "BoardNamePair"]
    ).reset_index(drop=True)
    return event_long


def filter_event_rows(df: pd.DataFrame, event_type: str, cohort_year: int, panel_level: str, period: int = 0) -> pd.DataFrame:
    """Return treated rows in the period-shifted year.

    Uses first_event_year or event_{cohort_year} to keep treated rows,
    then keeps year == cohort_year + period.
    Quarter panels are evaluated at quarter == 1.
    """
    data = df.copy()
    if event_type == "first_event":
        if "first_event_year" not in data.columns:
            raise KeyError("first_event cohort file must contain first_event_year")
        data = data[data["first_event_year"].fillna(-1) == cohort_year]
    else:
        event_col = f"event_{cohort_year}"
        if event_col not in data.columns:
            raise KeyError(f"Missing column {event_col} in cohort file")
        data = data[data[event_col] == 1]

    target_year = cohort_year + period
    data = data[data["year"] == target_year]

    if panel_level == "quarter":
        if "quarter" not in data.columns:
            raise KeyError("Quarter-level cohort file must contain 'quarter' column")
        data = data[data["quarter"] == 1]

    return data


def count_sharing_for_cohort(
    cohort_df: pd.DataFrame,
    event_pairs_long: pd.DataFrame,
    atc3_mapping: pd.DataFrame,
    panel_level: str,
    event: str,
    cohort_year: int = None,
    period: int = 0,
) -> tuple[int, int, int]:
    """Return sharing_count, not_sharing_count, total_event_rows for one cohort."""
    if cohort_df.empty:
        return 0, 0, 0

    required_cols = ["BoardName", "year", "product", "atc3"]
    obs = cohort_df[required_cols].copy().reset_index(drop=True)
    obs["obs_id"] = obs.index
    key_cols = ["year", "product", "atc3", "BoardName"]

    # Candidate ATC3 peers for each observation from mapping table.
    cand = obs.merge(atc3_mapping[key_cols + ["BoardNamePair"]], on=key_cols, how="left")
    cand = cand.dropna(subset=["BoardNamePair"]).copy()
    if cand.empty:
        return 0, len(obs), len(obs)

    # True event partners: use the original event year (cohort_year).
    evt = event_pairs_long[
        (event_pairs_long["event"] == event)
        & (event_pairs_long["year"] == cohort_year)
    ][["BoardName", "BoardNamePair"]].copy()

    matched = cand.merge(
        evt,
        on=["BoardName", "BoardNamePair"],
        how="inner",
    )

    sharing_obs_ids = set(matched["obs_id"].unique().tolist())
    sharing_count = len(sharing_obs_ids)
    total_count = len(obs)
    not_sharing_count = total_count - sharing_count
    return sharing_count, not_sharing_count, total_count


def build_distribution_for_config(
    panel_level: str,
    event_type: str,
    event: str,
    event_requirement: int,
    control_folder: str,
    treatment_group: str,
    include_eventpair: int,
    event_pairs_long: pd.DataFrame,
    atc3_mapping: pd.DataFrame,
    atc_level: int = 1,
    period: int = 0,
) -> pd.DataFrame:
    """Aggregate sharing vs not-sharing counts over cohorts for one configuration.
    
    Parameters:
    -----------
    atc_level : int
        1 for standard level, 2 for level2 version (atc3 with last char removed)
    period : int
        Relative period to examine (-4 to 3). 0 = event year.
    """
    rows = []
    for cohort in COHORT_YEARS:
        file_path = cohort_file_path(
            panel_level,
            event_type,
            event,
            cohort,
            treatment_group,
            include_eventpair,
            control_folder,
            event_requirement,
        )

        if not file_path.exists():
            rows.append(
                {
                    "cohort": cohort,
                    "sharing_atc3": 0,
                    "not_sharing_atc3": 0,
                    "total": 0,
                    "file_exists": 0,
                }
            )
            continue

        df = pd.read_csv(file_path)
        
        # Process atc3 based on atc_level
        if atc_level == 2:
            df['atc3'] = df['atc3'].astype(str).str[:-1]
        elif atc_level == 3:
            df['atc3'] = df['atc3'].astype(str).apply(lambda x: 'Device' if x.startswith('Device') else x[0])
        
        event_rows = filter_event_rows(df, event_type=event_type, cohort_year=cohort, panel_level=panel_level, period=period)

        sharing, not_sharing, total = count_sharing_for_cohort(
            cohort_df=event_rows,
            event_pairs_long=event_pairs_long,
            atc3_mapping=atc3_mapping,
            panel_level=panel_level,
            event=event,
            cohort_year=cohort,
            period=period,
        )

        rows.append(
            {
                "cohort": cohort,
                "sharing_atc3": sharing,
                "not_sharing_atc3": not_sharing,
                "total": total,
                "file_exists": 1,
            }
        )

    return pd.DataFrame(rows).sort_values("cohort").reset_index(drop=True)


def plot_distribution(summary_df: pd.DataFrame, panel_level: str, event_type: str, event: str, out_png: Path, period: int = 0) -> None:
    """Plot sharing vs not-sharing distribution by cohort."""
    x = summary_df["cohort"].tolist()
    sharing = summary_df["sharing_atc3"].tolist()
    not_sharing = summary_df["not_sharing_atc3"].tolist()

    plt.figure(figsize=(12, 6))
    plt.bar(x, sharing, label="sharing_atc3")
    plt.bar(x, not_sharing, bottom=sharing, label="not_sharing_atc3")

    # Add text labels on bars
    for i, cohort in enumerate(x):
        # Label for sharing_atc3 (bottom bar)
        if sharing[i] > 0:
            plt.text(cohort, sharing[i] / 2, str(sharing[i]), 
                    ha='center', va='center', fontsize=8, fontweight='bold')
        
        # Label for not_sharing_atc3 (top bar)
        if not_sharing[i] > 0:
            plt.text(cohort, sharing[i] + not_sharing[i] / 2, str(not_sharing[i]), 
                    ha='center', va='center', fontsize=8, fontweight='bold')

    plt.xlabel("Cohort Year")
    plt.ylabel("Number of Event Observations")
    plt.title(
        f"ATC3 Sharing Distribution | event={event} | event_type={event_type} | "
        f"level={panel_level} | period={period}"
    )
    plt.legend()
    plt.xticks(x, rotation=45)
    plt.tight_layout()

    out_png.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_png, dpi=200)
    plt.close()


# ============================================================================
#  Generate cohort CSVs with atc3_sharing column
# ============================================================================

def compute_sharing_set(
    cohort_df: pd.DataFrame,
    event_pairs_long: pd.DataFrame,
    atc3_mapping: pd.DataFrame,
    panel_level: str,
    event: str,
    event_type: str,
    cohort_year: int,
    periods: list[int] | None = None,
) -> set[tuple[str, str]]:
    """Return the set of (BoardName, product) pairs that are sharing ATC3.

    A treated (BoardName, product) is sharing_atc3 if, in *any* of the
    specified ``periods`` relative to the cohort year, its ATC3 peers
    (from atc3_mapping) include at least one of its interlock partners
    (from event_pairs_long).

    Control units (non-treated) are never tagged as sharing.

    Parameters
    ----------
    periods : list[int]
        Relative periods to check (e.g. [0] = event year only).
    """
    if periods is None:
        periods = ATC3_SHARING_PERIODS

    sharing_pairs: set[tuple[str, str]] = set()

    for period in periods:
        # Get treated rows in this period
        event_rows = filter_event_rows(
            cohort_df, event_type=event_type,
            cohort_year=cohort_year, panel_level=panel_level,
            period=period,
        )
        if event_rows.empty:
            continue

        required_cols = ["BoardName", "year", "product", "atc3"]

        obs = event_rows[required_cols].copy().reset_index(drop=True)
        obs["obs_id"] = obs.index
        key_cols = ["year", "product", "atc3", "BoardName"]

        # ATC3 peers
        cand = obs.merge(
            atc3_mapping[key_cols + ["BoardNamePair"]], on=key_cols, how="left"
        )
        cand = cand.dropna(subset=["BoardNamePair"]).copy()
        if cand.empty:
            continue

        # Event partners at the cohort year
        evt = event_pairs_long[
            (event_pairs_long["event"] == event)
            & (event_pairs_long["year"] == cohort_year)
        ][["BoardName", "BoardNamePair"]].copy()

        matched = cand.merge(evt, on=["BoardName", "BoardNamePair"], how="inner")

        for _, row in matched[["BoardName", "product"]].drop_duplicates().iterrows():
            sharing_pairs.add((row["BoardName"], row["product"]))

    return sharing_pairs


def add_atc3_sharing_column_for_years(
    cohort_df: pd.DataFrame,
    event_pairs_long: pd.DataFrame,
    atc3_mapping: pd.DataFrame,
    panel_level: str,
    event: str,
    event_type: str,
    cohort_years: list[int],
    periods: list[int] | None = None,
) -> pd.DataFrame:
    """Add atc3_sharing using a shared merge pipeline over multiple cohort years."""
    sharing_pairs: set[tuple[str, str]] = set()
    for cohort_year in cohort_years:
        sharing_pairs |= compute_sharing_set(
            cohort_df=cohort_df,
            event_pairs_long=event_pairs_long,
            atc3_mapping=atc3_mapping,
            panel_level=panel_level,
            event=event,
            event_type=event_type,
            cohort_year=cohort_year,
            periods=periods,
        )

    df = cohort_df.copy()
    if sharing_pairs:
        sharing_idx = df.set_index(["BoardName", "product"]).index.isin(sharing_pairs)
        df["atc3_sharing"] = sharing_idx.astype(int)
    else:
        df["atc3_sharing"] = 0
    return df


def generate_all_cohort_data_with_atc3sharing(
    treatment_group: str,
    include_eventpair: int,
    event_requirement: int,
    atc_level: int = 1,
    periods: list[int] | None = None,
) -> None:
    """Add atc3_sharing to all balanced cohort files and mirror folder structure."""
    movement_event_pairs = None
    if any(event != "indirect_interlock" for event in EVENTS):
        movement_event_pairs = load_movement_event_pairs(
            treatment_group=treatment_group,
            event_requirement=event_requirement,
        )
    indirect_event_pairs = None
    if "indirect_interlock" in EVENTS:
        indirect_event_pairs = load_indirect_interlock_event_pairs(
            event_requirement=event_requirement
        )

    for panel_level in PANEL_LEVELS:
        atc3_mapping = load_atc3_mapping(panel_level, atc_level=atc_level)

        for event_type in EVENT_TYPES:
            for control_folder in CONTROL_FOLDERS:
                for event in EVENTS:
                    if event == "indirect_interlock":
                        if (
                            treatment_group != TREATMENT_GROUPS[0]
                            or include_eventpair != INCLUDE_EVENTPAIR_VALUES[0]
                        ):
                            continue
                        level_folder = f"{panel_level}-level"
                        event_pairs_long = indirect_event_pairs
                    else:
                        level_folder = cohort_level_folder(
                            panel_level,
                            treatment_group,
                            include_eventpair,
                        )
                        event_pairs_long = movement_event_pairs
                    src_dir = (
                        COHORT_ROOT
                        / level_folder
                        / event_type
                        / requirement_folder(event_requirement)
                        / control_folder
                    )
                    if not src_dir.exists():
                        continue

                    dst_dir = (
                        COHORT_OUT_ROOT
                        / level_folder
                        / event_type
                        / requirement_folder(event_requirement)
                        / control_folder
                    )
                    dst_dir.mkdir(parents=True, exist_ok=True)

                    for f in sorted(src_dir.glob(f"{event}_{panel_level}_cohort_*_balanced.csv")):
                        # Extract cohort year from standard cohort filename patterns.
                        stem = f.stem
                        parts = stem.split("_cohort_")
                        if len(parts) != 2:
                            continue
                        year_part = parts[1].replace("_first_event_balanced", "").replace("_balanced", "")
                        try:
                            cohort_year = int(year_part)
                        except ValueError:
                            continue

                        df = pd.read_csv(f)

                        df_proc = apply_atc_level(df, atc_level=atc_level)
                        df_out = add_atc3_sharing_column_for_years(
                            cohort_df=df_proc,
                            event_pairs_long=event_pairs_long,
                            atc3_mapping=atc3_mapping,
                            panel_level=panel_level,
                            event=event,
                            event_type=event_type,
                            cohort_years=[cohort_year],
                            periods=periods,
                        )

                        out_path = dst_dir / f.name
                        df_out.to_csv(out_path, index=False)

                    print(
                        f"  [{panel_level}/{event_type}/req{event_requirement}/{control_folder}] "
                        f"{event} done"
                    )

    label = cohort_group_label(treatment_group, include_eventpair)
    print(
        f"\nAll cohort data with atc3_sharing saved for {label} "
        f"(req{event_requirement}) to: {COHORT_OUT_ROOT}"
    )


def generate_all_staggered_data_with_atc3sharing(
    atc_level: int = 1,
    periods: list[int] | None = None,
) -> None:
    """Process all staggered balanced files and mirror output folder structure.

    Staggered files are not split by treatment_group, so use B-oriented event table.
    """
    event_pairs_long = load_event_pairs(treatment_group="B")

    for panel_level in PANEL_LEVELS:
        atc3_mapping = load_atc3_mapping(panel_level, atc_level=atc_level)
        src_dir = STAGGERED_ROOT / staggered_level_folder(panel_level)
        if not src_dir.exists():
            continue

        for f in sorted(src_dir.glob("staggered_firm_level_panel_*_balanced.csv")):
            parsed = parse_staggered_file_name(f.name)
            if parsed is None:
                continue
            if parsed["panel_level"] != panel_level:
                continue

            event = parsed["event"]
            control_type = parsed["control_type"]
            control_folder = STAGGERED_CONTROL_FOLDER_MAP[control_type]
            event_type = "first_event"

            df = pd.read_csv(f)
            df_proc = apply_atc_level(df, atc_level=atc_level)
            cohort_years = (
                df_proc["first_event_year"].dropna().astype(int).sort_values().unique().tolist()
            )

            df_out = add_atc3_sharing_column_for_years(
                cohort_df=df_proc,
                event_pairs_long=event_pairs_long,
                atc3_mapping=atc3_mapping,
                panel_level=panel_level,
                event=event,
                event_type=event_type,
                cohort_years=cohort_years,
                periods=periods,
            )

            out_dir = STAGGERED_OUT_ROOT / panel_level / event_type / control_folder
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f.name
            df_out.to_csv(out_path, index=False)
            print(f"Saved: {out_path}")

    print(f"\nAll staggered data with atc3_sharing saved to: {STAGGERED_OUT_ROOT}")


def build_distribution_for_staggered_file(
    file_path: Path,
    panel_level: str,
    event: str,
    event_pairs_long: pd.DataFrame,
    atc3_mapping: pd.DataFrame,
    atc_level: int = 1,
    period: int = 0,
) -> pd.DataFrame:
    """Aggregate sharing counts by cohort year from one staggered file."""
    df = pd.read_csv(file_path)
    df_proc = apply_atc_level(df, atc_level=atc_level)

    rows = []
    for cohort in COHORT_YEARS:
        event_rows = filter_event_rows(
            df=df_proc,
            event_type="first_event",
            cohort_year=cohort,
            panel_level=panel_level,
            period=period,
        )

        sharing, not_sharing, total = count_sharing_for_cohort(
            cohort_df=event_rows,
            event_pairs_long=event_pairs_long,
            atc3_mapping=atc3_mapping,
            panel_level=panel_level,
            event=event,
            cohort_year=cohort,
            period=period,
        )

        rows.append(
            {
                "cohort": cohort,
                "sharing_atc3": sharing,
                "not_sharing_atc3": not_sharing,
                "total": total,
                "file_exists": 1,
            }
        )

    return pd.DataFrame(rows).sort_values("cohort").reset_index(drop=True)


def period_suffix(period: int) -> str:
    """Return filename suffix for given period."""
    if period == 0:
        return ""
    elif period < 0:
        return f"_pre_{abs(period)}"
    else:
        return f"_post_{period}"


def main() -> None:
    sharing_periods = list(ATC3_SHARING_PERIODS)

    # Generate enriched cohort files.
    print("=" * 60)
    print(
        "Generating cohort data with atc3_sharing "
        f"(atc_level={ATC_LEVEL}, periods={sharing_periods}) ..."
    )
    print("=" * 60)
    for treatment_group in TREATMENT_GROUPS:
        for include_eventpair in INCLUDE_EVENTPAIR_VALUES:
            for event_requirement in EVENT_REQUIREMENTS:
                label = cohort_group_label(treatment_group, include_eventpair)
                print(f"Cohort pass: {label} | req{event_requirement}")
                generate_all_cohort_data_with_atc3sharing(
                    treatment_group=treatment_group,
                    include_eventpair=include_eventpair,
                    event_requirement=event_requirement,
                    atc_level=ATC_LEVEL,
                    periods=sharing_periods,
                )
    """
    # Staggered processing intentionally unchanged by cohort group parameters.
    generate_all_staggered_data_with_atc3sharing(atc_level=ATC_LEVEL, periods=sharing_periods)
    """

    # Plot diagnostics for configured periods.
    print("\n" + "=" * 60)
    print("Plotting cohort sharing distributions ...")
    print("=" * 60)
    """
    staggered_event_pairs_long = load_event_pairs(treatment_group="B")
    """

    for period in PERIODS:
        p_suffix = period_suffix(period)

        for atc_level in [ATC_LEVEL]:
            level_suffix = "" if atc_level == 1 else f"_level{atc_level}"
            print(f"\n  ATC Level {atc_level}")

            for panel_level in PANEL_LEVELS:
                atc3_mapping = load_atc3_mapping(panel_level, atc_level=atc_level)

                for treatment_group in TREATMENT_GROUPS:
                    for include_eventpair in INCLUDE_EVENTPAIR_VALUES:
                        for event_requirement in EVENT_REQUIREMENTS:
                            movement_event_pairs = None
                            if any(event != "indirect_interlock" for event in EVENTS):
                                movement_event_pairs = load_movement_event_pairs(
                                    treatment_group=treatment_group,
                                    event_requirement=event_requirement,
                                )
                            indirect_event_pairs = None
                            if "indirect_interlock" in EVENTS:
                                indirect_event_pairs = load_indirect_interlock_event_pairs(
                                    event_requirement=event_requirement
                                )
                            for event_type in EVENT_TYPES:
                                for control_folder in ["Pure Control"]:
                                    for event in EVENTS:
                                        if event == "indirect_interlock":
                                            if (
                                                treatment_group != TREATMENT_GROUPS[0]
                                                or include_eventpair != INCLUDE_EVENTPAIR_VALUES[0]
                                            ):
                                                continue
                                            level_folder = f"{panel_level}-level"
                                            label_suffix = ""
                                            event_pairs_long = indirect_event_pairs
                                        else:
                                            label = cohort_group_label(treatment_group, include_eventpair)
                                            level_folder = cohort_level_folder(
                                                panel_level,
                                                treatment_group,
                                                include_eventpair,
                                            )
                                            label_suffix = f"_{label}"
                                            event_pairs_long = movement_event_pairs
                                        summary = build_distribution_for_config(
                                            panel_level=panel_level,
                                            event_type=event_type,
                                            event=event,
                                            event_requirement=event_requirement,
                                            control_folder=control_folder,
                                            treatment_group=treatment_group,
                                            include_eventpair=include_eventpair,
                                            event_pairs_long=event_pairs_long,
                                            atc3_mapping=atc3_mapping,
                                            atc_level=atc_level,
                                            period=period,
                                        )

                                        out_dir = (
                                            FIG_ROOT
                                            / level_folder
                                            / event_type
                                            / requirement_folder(event_requirement)
                                        )
                                        stem = (
                                            f"sharing_atc3_{event}_{event_type}_{panel_level}-level{label_suffix}"
                                            f"{level_suffix}{p_suffix}"
                                        )
                                        out_png = out_dir / f"{stem}.png"

                                        plot_distribution(
                                            summary_df=summary,
                                            panel_level=panel_level,
                                            event_type=event_type,
                                            event=event,
                                            out_png=out_png,
                                            period=period,
                                        )

                                        csv_dir = (
                                            PROJECT_ROOT
                                            / "csv"
                                            / f"cohort_sharing_atc3{level_suffix}"
                                            / level_folder
                                            / event_type
                                            / requirement_folder(event_requirement)
                                        )
                                        csv_dir.mkdir(parents=True, exist_ok=True)
                                        out_csv = csv_dir / f"{stem}.csv"
                                        
                                        # Only save relevant plotting columns to make it clean
                                        csv_df = summary[["cohort", "sharing_atc3", "not_sharing_atc3", "total"]].copy()
                                        csv_df.rename(columns={"cohort": "Year"}, inplace=True)
                                        csv_df.to_csv(out_csv, index=False)

                                        print(f"Saved: {out_png}")
                                        print(f"Saved CSV: {out_csv}")
                """
                # staggered_data is always first_event by construction
                src_dir = STAGGERED_ROOT / staggered_level_folder(panel_level)
                if not src_dir.exists():
                    continue

                for f in sorted(src_dir.glob("staggered_firm_level_panel_*_balanced.csv")):
                    parsed = parse_staggered_file_name(f.name)
                    if parsed is None:
                        continue
                    if parsed["panel_level"] != panel_level:
                        continue

                    event = parsed["event"]
                    control_type = parsed["control_type"]
                    control_folder = STAGGERED_CONTROL_FOLDER_MAP[control_type]

                    summary = build_distribution_for_staggered_file(
                        file_path=f,
                        panel_level=panel_level,
                        event=event,
                        event_pairs_long=staggered_event_pairs_long,
                        atc3_mapping=atc3_mapping,
                        atc_level=atc_level,
                        period=period,
                    )

                    out_dir = STAGGERED_FIG_ROOT / panel_level / "first_event" / control_folder
                    stem = (
                        f"sharing_atc3_{event}_first_event_"
                        f"{panel_level}_{control_type}{level_suffix}{p_suffix}"
                    )
                    out_png = out_dir / f"{stem}.png"

                    plot_distribution(
                        summary_df=summary,
                        panel_level=panel_level,
                        event_type="first_event",
                        event=event,
                        out_png=out_png,
                        period=period,
                    )

                    csv_dir = (
                        PROJECT_ROOT
                        / "csv"
                        / f"staggered_sharing_atc3{level_suffix}"
                        / panel_level 
                        / "first_event" 
                        / control_folder
                    )
                    csv_dir.mkdir(parents=True, exist_ok=True)
                    out_csv = csv_dir / f"{stem}.csv"
                    
                    csv_df = summary[["cohort", "sharing_atc3", "not_sharing_atc3", "total"]].copy()
                    csv_df.rename(columns={"cohort": "Year"}, inplace=True)
                    csv_df.to_csv(out_csv, index=False)

                    print(f"Saved: {out_png}")
                    print(f"Saved CSV: {out_csv}")
                """


if __name__ == "__main__":
    main()
