r"""
Purpose:
Build cohort panels for treated and control firm-product units from pre-built
firm-level event panels. The script supports first-event and event-year cohorts,
multiple event requirements, balanced-window filtering, and req2 control
variation splits for movement events.

Process:
- Load a selected firm-level panel by event_type, panel_level, and requirement.
- For each cohort year t, keep observations in [t-window_pre, t+window_post].
- Build treated units from first_event_year == t or event_t == 1.
- Build controls from units with no event inside the window, then apply
  control_type rules (pure_control, not_yet, not).
- When include_eventpair == 0 for movement events, drop counterpart-only firms
  using the pre-built movement candidate table.
- For movement req2 cohorts, split controls into stable/changing variation
  groups using the firm interlock panel.
- Add other-event control columns (e.g., event_still_pulse, event_not_history) 
  relevant to the current event_type and requirement, filtered strictly within 
  the cohort window.
- Save cohort CSV files and plot treated/control counts by cohort year for
  both product-level and firm-level distributions.

Input:
- data/year-level_{A|B}/ssr_firm_panel_*.csv
- data/quarter-level_{A|B}/ssr_firm_panel_*.csv
- data/movement_tables/movement_event_candidates.csv
- data/movement_tables/firm_interlock_panel.csv

Output:
- data/cohort_data/{panel_level}-level_{A|B}_{with|without}_{B|A}/{treat_type}/req{n}/{control_folder}/*.csv
- figures/cohort_distribution_product/{panel_level}-level_{A|B}_{with|without}_{B|A}/{treat_type}/req{n}/{control_folder}/*.png
- figures/cohort_distribution_firm/{panel_level}-level_{A|B}_{with|without}_{B|A}/{treat_type}/req{n}/{control_folder}/*.png
"""

from __future__ import annotations
from functools import lru_cache
from pathlib import Path
import re
import matplotlib.pyplot as plt
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = PROJECT_ROOT / "data"
COHORT_DATA_ROOT = DATA_ROOT / "cohort_data"
MOVEMENT_TABLES_PATH = DATA_ROOT / "movement_tables"
MOVEMENT_CANDIDATES_PATH = MOVEMENT_TABLES_PATH / "movement_event_candidates.csv"
FIRM_INTERLOCK_PANEL_PATH = MOVEMENT_TABLES_PATH / "firm_interlock_panel.csv"

# Event key to panel-filename mapping.
EVENT_CONFIGS = {
    "to_B_not_in_A": {
        "kind": "movement",
        "panel_stem": "ssr_firm_panel_to_B_not_in_A",
        "candidate_event_type": "to_B_not_in_A",
    },
    "to_B_still_in_A": {
        "kind": "movement",
        "panel_stem": "ssr_firm_panel_to_B_still_in_A",
        "candidate_event_type": "to_B_still_in_A",
    },
    "interlock_dissolution": {
        "kind": "movement",
        "panel_stem": "ssr_firm_panel_interlock_dissolution_leave_B",
        "candidate_event_type": "interlock_dissolution",
    },
    "indirect_interlock": {
        "kind": "indirect_interlock",
        "panel_stem": "ssr_firm_panel_indirect_interlock",
    },
}
"""
    "direct_interlock": {
        "kind": "interlock",
        "filename": "ssr_firm_panel_direct_interlock.csv",
    },
    "indirect_interlock": {
        "kind": "interlock",
        "filename": "ssr_firm_panel_indirect_interlock.csv",
    },
"""
CONTROL_FOLDER_MAP = {
    "pure_control": "Pure Control",
    "not_yet": "Not Yet",
    "not": "Not",
}

REQ2_CONTROL_VARIATIONS = (
    "stable",
    "changing",
    "stable_interlock",
    "stable_no_interlock",
)

REQ2_STACK_SEGMENTS = (
    "changing",
    "stable_interlock",
    "stable_no_interlock",
)

REQ2_STACK_COLORS = {
    "changing": "#C65D4A",
    "stable_interlock": "#4B8F8C",
    "stable_no_interlock": "#C7B299",
}

DISTRIBUTION_CONFIGS = {
    "product": {
        "id_cols": ["BoardName", "product"],
        "ylabel": "Number of BoardName-Product Pairs",
        "figure_root": "cohort_distribution_product",
        "title_label": "product distribution",
    },
    "firm": {
        "id_cols": ["BoardName"],
        "ylabel": "Number of Unique BoardName",
        "figure_root": "cohort_distribution_firm",
        "title_label": "firm distribution",
    },
}


# ========================== USER CONFIG ==========================
# panel_levels:
# - "year" or "quarter"
# - Changes how many rows each firm-product contributes inside a cohort window.
# - Year mode requires one row per retained year; quarter mode requires four quarters per retained year.
#
# event_types:
# - Chooses the upstream treatment definition and timing source.
# - Changing event_type changes which units are treated in cohort year t and the treatment interpretation.
#
# event_requirements:
# - 0, 1, or 2 for movement events.
# - indirect interlock uses req0/req1/req2 firm panels without treatment_group suffix.
#
# treat_types:
# - "first_event": unit enters treated sample at most once, when first_event_year == t.
# - "event": unit enters treated sample whenever event_t == 1, so one unit can appear in multiple cohort years.
#
# window_pre, window_post:
# - Retained calendar window is [t - window_pre, t + window_post].
#
# control_types:
# - Base "not" controls: no event anywhere inside the current window and full window support.
# - "pure_control": subset of "not" with never-treated units only.
# - "not_yet": subset of "not" with never-treated or first treatment after window end.
#
# control_variations:
# - Only used for movement req2 cohorts.
# - "stable" is the union of "stable_interlock" and "stable_no_interlock".
#
# balanced_states:
# - 0 or 1.
# - When 1, treated units must have full window support and satisfy upstream balance_panel_t.
# - Controls are always required to be complete in-window by design.
#
# treatment_groups:
# - "B": destination firm as treated group
# - "A": origin firm as treated group
#
# include_eventpair:
# - 1: keep counterpart-firm observations in source panel
# - 0: drop counterpart-only firms using movement_event_candidates.csv
RUN_CONFIG = {
    "panel_levels": ["quarter"],
    "event_types": [
        "indirect_interlock",
    ],
    "event_requirements": [0, 1, 2],
    "treat_types": ["first_event", "event"],
    "control_types": ["pure_control", "not_yet", "not"],
    "control_variations": ["stable", "changing", "stable_interlock", "stable_no_interlock"],
    "window_pre": 1,
    "window_post": 1,
    "balanced_states": [1],
    "treatment_groups": ["B", "A"],
    "include_eventpair": [0], # 1
    "plot_start_year": 2009,
    "plot_end_year": 2018,
}
"""
        "to_B_not_in_A",
        "to_B_still_in_A",
        "interlock_dissolution",
        "direct_interlock",
        "indirect_interlock",
"""
# ===============================================================


def is_movement_event(event_type: str) -> bool:
    """Return whether the current event type uses movement panels."""
    if event_type not in EVENT_CONFIGS:
        raise ValueError(f"Unsupported event_type: {event_type}")
    return EVENT_CONFIGS[event_type]["kind"] == "movement"


def is_indirect_interlock_event(event_type: str) -> bool:
    """Return whether the current event type uses indirect interlock requirement panels."""
    if event_type not in EVENT_CONFIGS:
        raise ValueError(f"Unsupported event_type: {event_type}")
    return EVENT_CONFIGS[event_type]["kind"] == "indirect_interlock"


def get_panel_group_label(treatment_group: str) -> str:
    """Build the panel-group suffix used by PanelMaker_FirmLevel.py."""
    treatment_group = str(treatment_group).upper()
    if treatment_group not in {"A", "B"}:
        raise ValueError("treatment_group must be one of: A, B")
    return treatment_group


def get_output_group_label(treatment_group: str, include_eventpair: int) -> str:
    """Build the cohort and figure folder suffix."""
    counterpart = "B" if str(treatment_group).upper() == "A" else "A"
    relation = "with" if int(include_eventpair) == 1 else "without"
    return f"{str(treatment_group).upper()}_{relation}_{counterpart}"


def get_event_output_group_label(event_type: str, treatment_group: str, include_eventpair: int) -> str:
    """Build the cohort and figure folder suffix for the current event."""
    if is_indirect_interlock_event(event_type):
        return ""
    return get_output_group_label(treatment_group, include_eventpair)


def get_level_folder_name(panel_level: str, output_group_label: str) -> str:
    """Build the level folder, allowing no suffix for indirect interlock."""
    if output_group_label == "":
        return f"{panel_level}-level"
    return f"{panel_level}-level_{output_group_label}"


def get_requirement_folder(event_requirement: int) -> str:
    """Convert requirement index to the req{n} folder label."""
    if int(event_requirement) not in {0, 1, 2}:
        raise ValueError("event_requirement must be one of: 0, 1, 2")
    return f"req{int(event_requirement)}"


def get_control_folder_name(
    control_type: str,
) -> str:
    """Return the output folder name for the current control definition."""
    if control_type not in CONTROL_FOLDER_MAP:
        raise ValueError("control_type must be one of: pure_control, not_yet, not")
    return CONTROL_FOLDER_MAP[control_type]


def get_data_path(
    event_type: str,
    panel_level: str,
    treatment_group: str = "B",
    event_requirement: int = 0,
) -> Path:
    """Return the source panel path for a given event type and requirement."""
    if event_type not in EVENT_CONFIGS:
        raise ValueError(
            "event_type must be one of: direct_interlock, indirect_interlock, "
            "to_B_not_in_A, to_B_still_in_A, interlock_dissolution"
        )
    if panel_level not in {"year", "quarter"}:
        raise ValueError("panel_level must be one of: year, quarter")

    config = EVENT_CONFIGS[event_type]

    if config["kind"] == "movement":
        group_label = get_panel_group_label(treatment_group)
        level_folder = f"{panel_level}-level_{group_label}"
        filename = f"{config['panel_stem']}_{get_requirement_folder(event_requirement)}.csv"
    elif config["kind"] == "indirect_interlock":
        level_folder = f"{panel_level}-level"
        filename = f"{config['panel_stem']}_{get_requirement_folder(event_requirement)}.csv"
    else:
        group_label = get_panel_group_label(treatment_group)
        level_folder = f"{panel_level}-level_{group_label}"
        filename = config["filename"]

    return DATA_ROOT / level_folder / filename


def expected_period_count(window_years: set[int], panel_level: str) -> int:
    """Compute required observation count for one unit in the target window."""
    return len(window_years) if panel_level == "year" else len(window_years) * 4


def get_cohort_filename(
    event_type: str,
    panel_level: str,
    cohort_year: int,
    treat_type: str,
    balanced: int,
) -> str:
    """Build a cohort CSV filename from the current configuration."""
    balanced_suffix = "_balanced" if int(balanced) == 1 else ""
    treat_type_suffix = "_first_event" if treat_type == "first_event" else ""
    return (
        f"{event_type}_{panel_level}_cohort_{int(cohort_year)}"
        f"{treat_type_suffix}"
        f"{balanced_suffix}.csv"
    )


def get_valid_event_requirements(
    event_type: str,
    requested_requirements: list[int],
) -> list[int]:
    """Return the applicable requirement levels for the current event type."""
    # Movement and indirect interlock events run req0/req1/req2.
    if is_movement_event(event_type) or is_indirect_interlock_event(event_type):
        return [int(requirement) for requirement in requested_requirements]
    return [0]


@lru_cache(maxsize=1)
def load_movement_candidates_table() -> pd.DataFrame:
    """Load the pre-built movement candidate table once for reuse."""
    if not MOVEMENT_CANDIDATES_PATH.exists():
        raise FileNotFoundError(
            f"Missing movement candidate table: {MOVEMENT_CANDIDATES_PATH}. "
            "Run MovementTableMaker.py first."
        )
    return pd.read_csv(MOVEMENT_CANDIDATES_PATH)


@lru_cache(maxsize=1)
def load_firm_interlock_panel() -> pd.DataFrame:
    """Load the yearly firm interlock panel once for reuse."""
    if not FIRM_INTERLOCK_PANEL_PATH.exists():
        raise FileNotFoundError(
            f"Missing firm interlock panel: {FIRM_INTERLOCK_PANEL_PATH}. "
            "Run MovementTableMaker.py first."
        )
    return pd.read_csv(FIRM_INTERLOCK_PANEL_PATH)


def detect_stay_column(columns: list[str] | pd.Index) -> str:
    """Detect the unique stay_{x}_years column in a panel or candidate table."""
    stay_cols = [
        str(column)
        for column in columns
        if re.fullmatch(r"stay_\d+_years", str(column))
    ]
    if len(stay_cols) != 1:
        raise ValueError(
            "Expected exactly one stay_{x}_years column, found: "
            f"{stay_cols}"
        )
    return stay_cols[0]


def get_cohort_root(
    panel_level: str,
    output_group_label: str,
    treat_type: str,
    event_requirement: int,
) -> Path:
    """Build the shared cohort output root for one configuration."""
    return (
        COHORT_DATA_ROOT
        / get_level_folder_name(panel_level, output_group_label)
        / treat_type
        / get_requirement_folder(event_requirement)
    )


def read_cohort_file(file_path: Path, event_col: str) -> pd.DataFrame:
    """Read one cohort file and verify the cohort event column exists."""
    if not file_path.exists():
        raise FileNotFoundError(f"Missing cohort file: {file_path}")

    cohort_df = pd.read_csv(file_path)
    if event_col not in cohort_df.columns:
        raise KeyError(
            f"Missing event column: {event_col}\nFile: {file_path}"
    )
    return cohort_df


def get_allowed_cohort_years(plot_start_year: int, plot_end_year: int) -> list[int]:
    """Use the configured plotting range as the cohort-year output range as well."""
    start_year = int(plot_start_year)
    end_year = int(plot_end_year)
    if start_year > end_year:
        raise ValueError("plot_start_year must be <= plot_end_year")
    return list(range(start_year, end_year + 1))


def get_req2_plot_segments(control_variations: list[str]) -> list[str]:
    """Return the req2 control segments that should appear in stacked control bars."""
    requested = [variation for variation in control_variations if variation != "stable"]
    return [segment for segment in REQ2_STACK_SEGMENTS if segment in requested]


def build_firm_interlock_lookup() -> dict[tuple[str, int], set[str]]:
    """Map BoardName-year to the set of SSR counterpart firms in that year."""
    firm_interlock_panel = load_firm_interlock_panel()
    grouped = (
        firm_interlock_panel.groupby(["BoardName", "year"])["CounterpartBoard"]
        .agg(lambda values: set(values.tolist()))
        .reset_index()
    )
    return {
        (str(board_name), int(year)): set(counterparts)
        for board_name, year, counterparts in grouped.itertuples(index=False, name=None)
    }


def load_effective_movement_pairs(
    event_type: str,
    event_requirement: int,
    stay_col: str,
) -> pd.DataFrame:
    """Load requirement-valid movement pairs for include_eventpair filtering."""
    movement = load_movement_candidates_table().copy()
    required_cols = {
        "event_type",
        "event_year",
        "FirmA",
        "FirmB",
        "requirement1",
        stay_col,
    }
    missing_cols = sorted(required_cols - set(movement.columns))
    if missing_cols:
        raise ValueError(
            "Movement candidate table is missing required columns for include_eventpair=0: "
            f"{missing_cols}"
        )

    candidate_event_type = EVENT_CONFIGS[event_type]["candidate_event_type"]
    movement = movement.loc[movement["event_type"].eq(candidate_event_type)].copy()
    movement["event_year"] = pd.to_numeric(movement["event_year"], errors="raise").astype(int)
    movement["requirement1"] = pd.to_numeric(movement["requirement1"], errors="raise").astype(int)
    movement[stay_col] = pd.to_numeric(movement[stay_col], errors="raise").astype(int)

    movement = movement.loc[movement[stay_col].eq(1)].copy()
    if int(event_requirement) in {1, 2}:
        movement = movement.loc[movement["requirement1"].eq(1)].copy()

    return (
        movement[["event_year", "FirmA", "FirmB"]]
        .dropna(subset=["event_year", "FirmA", "FirmB"])
        .drop_duplicates()
        .reset_index(drop=True)
    )


def classify_control_variation(
    board_name: str,
    window_years: list[int],
    interlock_lookup: dict[tuple[str, int], set[str]],
) -> str:
    """Classify one control board's interlock evolution inside the cohort window."""
    interlock_history = [
        interlock_lookup.get((board_name, year), set())
        for year in window_years
    ]
    is_stable = all(
        current_set == previous_set
        for previous_set, current_set in zip(interlock_history, interlock_history[1:])
    )
    if not is_stable:
        return "changing"
    if all(current_set for current_set in interlock_history):
        return "stable_interlock"
    return "stable_no_interlock"


def add_req2_control_variation_columns(
    cohort_df: pd.DataFrame,
    cohort_year: int,
    board_variations: dict[str, str],
    control_variations: list[str],
) -> pd.DataFrame:
    """Attach req2 control-variation marker columns to one cohort file."""
    event_col = f"event_{int(cohort_year)}"
    cohort_df = cohort_df.copy()
    control_mask = cohort_df[event_col].eq(0)

    for control_variation in control_variations:
        column_name = f"control_{control_variation}"
        cohort_df[column_name] = 0

    for board_name, classification in board_variations.items():
        board_mask = control_mask & cohort_df["BoardName"].eq(board_name)
        if "stable" in control_variations and classification in {"stable_interlock", "stable_no_interlock"}:
            cohort_df.loc[board_mask, "control_stable"] = 1
        if classification in control_variations:
            cohort_df.loc[board_mask, f"control_{classification}"] = 1

    control_columns = [f"control_{variation}" for variation in control_variations]
    cohort_df[control_columns] = cohort_df[control_columns].astype("int8")
    return cohort_df


def get_relevant_other_events(current_event_type: str, req: int) -> list[str]:
    events = []
    if current_event_type == "to_B_not_in_A":
        events = ["to_B_still_in_A", "interlock_dissolution"]
    elif current_event_type == "to_B_still_in_A":
        if req in {0, 1}:
            events = ["to_B_not_in_A", "interlock_dissolution"]
        elif req == 2:
            events = ["to_B_not_in_A"]
    elif current_event_type == "interlock_dissolution":
        if req in {0, 1}:
            events = ["to_B_not_in_A", "to_B_still_in_A"]
        elif req == 2:
            events = ["to_B_not_in_A"]
    return events


def get_other_event_prefix(other_event: str) -> str:
    mapping = {
        "to_B_not_in_A": "event_not",
        "to_B_still_in_A": "event_still",
        "interlock_dissolution": "event_dissolution"
    }
    return mapping[other_event]


@lru_cache(maxsize=16)
def get_other_event_timing(
    other_event_type: str,
    panel_level: str,
    treatment_group: str,
    req: int
) -> pd.DataFrame:
    """Return a DataFrame of unique (BoardName, year) where event == 1 for the other event."""
    data_path = get_data_path(
        event_type=other_event_type,
        panel_level=panel_level,
        treatment_group=treatment_group,
        event_requirement=req,
    )
    if not data_path.exists():
        raise FileNotFoundError(f"Missing required other-event panel: {data_path}")
    
    try:
        df = pd.read_csv(data_path, usecols=["BoardName", "year", "event"])
    except ValueError:
        df = pd.read_csv(data_path)
    
    df_event = df[df["event"] == 1][["BoardName", "year"]].drop_duplicates()
    return df_event


def add_other_event_columns(
    cohort_df: pd.DataFrame,
    start_year: int,
    end_year: int,
    current_event_type: str,
    req: int,
    panel_level: str,
    treatment_group: str
) -> pd.DataFrame:
    """Attach configured event_pulse and event_history columns for other event types."""
    other_events = get_relevant_other_events(current_event_type, req)
    
    for other_evt in other_events:
        prefix = get_other_event_prefix(other_evt)
        pulse_col = f"{prefix}_pulse"
        history_col = f"{prefix}_history"
        
        cohort_df[pulse_col] = 0
        cohort_df[history_col] = 0
        
        timing_df = get_other_event_timing(other_evt, panel_level, treatment_group, req)
        timing_window = timing_df[
            timing_df["year"].between(start_year, end_year)
        ].copy()
        
        if not timing_window.empty:
            timing_window = timing_window.rename(columns={"year": "other_event_year"})
            
            # Pulse
            merged = cohort_df[["BoardName", "year"]].merge(
                timing_window,
                left_on=["BoardName", "year"],
                right_on=["BoardName", "other_event_year"],
                how="left"
            )
            # The pulse condition is 1 if there was a match, keeping original order using .values
            cohort_df[pulse_col] = merged["other_event_year"].notna().values.astype("int8")
            
            # History
            first_event_in_window = (
                timing_window.groupby("BoardName")["other_event_year"]
                .min()
            )
            min_years = cohort_df["BoardName"].map(first_event_in_window)
            cohort_df[history_col] = (
                min_years.notna() & 
                (cohort_df["year"] >= min_years)
            ).values.astype("int8")
        else:
            cohort_df[pulse_col] = cohort_df[pulse_col].astype("int8")
            cohort_df[history_col] = cohort_df[history_col].astype("int8")
            
    return cohort_df


def build_control_groups(
    event_type: str,
    panel_level: str = "year",
    event_requirement: int = 0,
    treat_type: str = "first_event",
    window_pre: int = 2,
    window_post: int = 1,
    control_type: str = "pure_control",
    control_variations: list[str] | None = None,
    balanced: int = 1,
    treatment_group: str = "B",
    include_eventpair: int = 1,
) -> None:
    """Build cohort-level treated and control samples around each cohort year."""
    if treat_type not in {"first_event", "event"}:
        raise ValueError("treat_type must be one of: first_event, event")
    if control_type not in CONTROL_FOLDER_MAP:
        raise ValueError("control_type must be one of: pure_control, not_yet, not")

    if control_variations is None:
        control_variations = list(REQ2_CONTROL_VARIATIONS)
    invalid_variations = sorted(set(control_variations) - set(REQ2_CONTROL_VARIATIONS))
    if invalid_variations:
        raise ValueError(
            "control_variations contains unsupported values: "
            f"{invalid_variations}"
        )
    is_req2_movement = int(event_requirement) == 2 and is_movement_event(event_type)
    has_stay_column = is_movement_event(event_type) or is_indirect_interlock_event(event_type)

    # Load the firm-level panel that already matches the requested requirement.
    data_path = get_data_path(
        event_type=event_type,
        panel_level=panel_level,
        treatment_group=treatment_group,
        event_requirement=event_requirement,
    )
    df = pd.read_csv(data_path)

    stay_col = detect_stay_column(df.columns) if has_stay_column else None
    movement_pairs = None
    if int(include_eventpair) == 0 and is_movement_event(event_type):
        movement_pairs = load_effective_movement_pairs(
            event_type=event_type,
            event_requirement=event_requirement,
            stay_col=stay_col,
        )

    # req2 control variation needs yearly SSR interlock neighborhoods.
    interlock_lookup = build_firm_interlock_lookup() if is_req2_movement else None

    id_cols = ["BoardName", "product"]
    output_group_label = get_event_output_group_label(event_type, treatment_group, include_eventpair)
    output_root = get_cohort_root(
        panel_level=panel_level,
        output_group_label=output_group_label,
        treat_type=treat_type,
        event_requirement=event_requirement,
    )
    allowed_cohort_years = set(get_allowed_cohort_years(plot_start_year, plot_end_year))

    # Cohort year source depends on the treat_type definition.
    if treat_type == "first_event":
        cohorts = (
            df.loc[df["first_event_year"].notna(), "first_event_year"]
            .astype(int)
            .unique()
        )
    else:
        event_cols = [column for column in df.columns if column.startswith("event_")]
        cohorts = sorted(int(column.split("_")[1]) for column in event_cols)

    # Respect the configured year range for both generated cohort files and plots.
    cohorts = sorted(int(year) for year in cohorts if int(year) in allowed_cohort_years)

    for cohort_year in sorted(cohorts):
        # Event-time window for this cohort year t.
        start = int(cohort_year) - int(window_pre)
        end = int(cohort_year) + int(window_post)
        window_years = list(range(start, end + 1))
        expected_n = expected_period_count(set(window_years), panel_level)

        df_window = df[df["year"].between(start, end)].copy()

        if treat_type == "first_event":
            treated = df_window[df_window["first_event_year"] == cohort_year].copy()
        else:
            # event_t treatment: treated units are those with event_t == 1.
            event_col = f"event_{int(cohort_year)}"
            treated_ids = df.loc[df[event_col] == 1, id_cols].drop_duplicates()
            treated = df_window.merge(treated_ids, on=id_cols, how="inner")

        # Balanced cohorts require complete window coverage and balance_panel_t == 1.
        if int(balanced) == 1:
            treated_obs = treated.groupby(id_cols).size()
            treated = treated[
                treated.set_index(id_cols).index.isin(
                    treated_obs[treated_obs == expected_n].index
                )
            ]
            balance_col = f"balance_panel_{int(cohort_year)}"
            treated = treated[treated[balance_col] == 1]

        # Candidate controls must have no event inside the cohort window.
        treated_in_window = df_window.groupby(id_cols)["event"].max()
        valid_controls = treated_in_window[treated_in_window == 0].index
        controls = df_window[df_window.set_index(id_cols).index.isin(valid_controls)].copy()

        # Keep control units with full window observations.
        controls_obs = controls.groupby(id_cols).size()
        controls = controls[
            controls.set_index(id_cols).index.isin(
                controls_obs[controls_obs == expected_n].index
            )
        ].copy()

        # control_type narrows controls by future treatment timing in the current requirement panel.
        if control_type == "pure_control":
            controls = controls[controls["first_event_year"].isna()].copy()
        elif control_type == "not_yet":
            controls = controls[
                (controls["first_event_year"].isna())
                | (controls["first_event_year"] > end)
            ].copy()

        if movement_pairs is not None:
            # Drop firms that only appear on the counterpart side in cohort year t.
            year_moves = movement_pairs[movement_pairs["event_year"] == cohort_year]
            if not year_moves.empty:
                if str(treatment_group).upper() == "B":
                    drop_list = set(year_moves["FirmA"]) - set(year_moves["FirmB"])
                else:
                    drop_list = set(year_moves["FirmB"]) - set(year_moves["FirmA"])
                controls = controls[~controls["BoardName"].isin(drop_list)].copy()

        sort_cols = id_cols + ["year"] + (["quarter"] if "quarter" in df_window.columns else [])
        filename = get_cohort_filename(
            event_type=event_type,
            panel_level=panel_level,
            cohort_year=int(cohort_year),
            treat_type=treat_type,
            balanced=int(balanced),
        )

        output_dir = output_root / get_control_folder_name(control_type=control_type)
        output_dir.mkdir(parents=True, exist_ok=True)

        # req2 movement cohorts keep one file and mark control variation with four columns.
        if is_req2_movement:
            board_variations = {
                board_name: classify_control_variation(
                    board_name=board_name,
                    window_years=window_years,
                    interlock_lookup=interlock_lookup,
                )
                for board_name in sorted(controls["BoardName"].dropna().unique())
            }
            cohort_df = pd.concat([treated, controls], axis=0)
            cohort_df = cohort_df.sort_values(sort_cols).reset_index(drop=True)
            cohort_df = add_req2_control_variation_columns(
                cohort_df=cohort_df,
                cohort_year=int(cohort_year),
                board_variations=board_variations,
                control_variations=control_variations,
            )
            cohort_df = add_other_event_columns(
                cohort_df=cohort_df,
                start_year=start,
                end_year=end,
                current_event_type=event_type,
                req=event_requirement,
                panel_level=panel_level,
                treatment_group=treatment_group,
            )
            cohort_df.to_csv(output_dir / filename, index=False)
        else:
            cohort_df = pd.concat([treated, controls], axis=0)
            cohort_df = cohort_df.sort_values(sort_cols).reset_index(drop=True)
            cohort_df = add_other_event_columns(
                cohort_df=cohort_df,
                start_year=start,
                end_year=end,
                current_event_type=event_type,
                req=event_requirement,
                panel_level=panel_level,
                treatment_group=treatment_group,
            )
            cohort_df.to_csv(output_dir / filename, index=False)

    print(f"Finished. Files saved to: {output_root}")


def plot_treated_control_counts(
    event_type: str,
    event_requirement: int,
    panel_level: str = "year",
    start_year: int = 2007,
    end_year: int = 2018,
    treatment_group: str = "B",
    include_eventpair: int = 1,
    unit_level: str = "product",
    treat_types: list[str] | None = None,
    control_types: list[str] | None = None,
    control_variations: list[str] | None = None,
    balanced_states: list[int] | None = None,
) -> None:
    """Plot treated/control counts by cohort year under one event and requirement."""
    if unit_level not in DISTRIBUTION_CONFIGS:
        valid_levels = ", ".join(sorted(DISTRIBUTION_CONFIGS))
        raise ValueError(f"unit_level must be one of: {valid_levels}")

    if treat_types is None:
        treat_types = ["first_event", "event"]
    if control_types is None:
        control_types = ["pure_control", "not_yet", "not"]
    if control_variations is None:
        control_variations = list(REQ2_CONTROL_VARIATIONS)
    if balanced_states is None:
        balanced_states = [1]

    config = DISTRIBUTION_CONFIGS[unit_level]
    id_cols = config["id_cols"]
    output_group_label = get_event_output_group_label(event_type, treatment_group, include_eventpair)
    level_folder_name = get_level_folder_name(panel_level, output_group_label)
    output_group_suffix = f"_{output_group_label}" if output_group_label else ""
    years = list(range(int(start_year), int(end_year) + 1))
    is_req2_movement = int(event_requirement) == 2 and is_movement_event(event_type)

    for treat_type in treat_types:
        treat_root = get_cohort_root(
            panel_level=panel_level,
            output_group_label=output_group_label,
            treat_type=treat_type,
            event_requirement=event_requirement,
        )
        for control_type in control_types:
            for balanced in balanced_states:
                balanced_label = "balanced" if int(balanced) == 1 else "unbalanced"
                filename_by_year = {
                    year: get_cohort_filename(
                        event_type=event_type,
                        panel_level=panel_level,
                        cohort_year=year,
                        treat_type=treat_type,
                        balanced=int(balanced),
                    )
                    for year in years
                }

                treated_counts: list[int] = []
                folder_name = get_control_folder_name(control_type=control_type)

                # req2 movement figures keep one base control chart and split the control bar by variation.
                if is_req2_movement:
                    plot_segments = get_req2_plot_segments(control_variations)
                    control_segment_counts = {
                        segment: []
                        for segment in plot_segments
                    }

                    for year in years:
                        event_col = f"event_{year}"
                        file_path = treat_root / folder_name / filename_by_year[year]
                        if not file_path.exists():
                            treated_counts.append(0)
                            for segment in plot_segments:
                                control_segment_counts[segment].append(0)
                            continue

                        df = read_cohort_file(file_path, event_col)

                        # event_YYYY is a board-level constant, so it safely identifies treated rows here.
                        treated = df[df[event_col] == 1]
                        controls = df[df[event_col] == 0]
                        treated_counts.append(treated[id_cols].drop_duplicates().shape[0])

                        # Split the control bar into mutually exclusive req2 segments.
                        for segment in plot_segments:
                            segment_col = f"control_{segment}"
                            if segment_col not in df.columns:
                                raise KeyError(
                                    f"Missing req2 control column: {segment_col}\nFile: {file_path}"
                                )
                            segment_controls = controls[controls[segment_col] == 1]
                            control_segment_counts[segment].append(
                                segment_controls[id_cols].drop_duplicates().shape[0]
                            )

                    x = list(range(len(years)))
                    width = 0.35
                    plt.figure(figsize=(14, 6))

                    bars_treated = plt.bar(
                        [index - width / 2 for index in x],
                        treated_counts,
                        width=width,
                        label="Treated",
                    )

                    control_bottom = [0] * len(years)
                    for segment in plot_segments:
                        values = control_segment_counts[segment]
                        plt.bar(
                            [index + width / 2 for index in x],
                            values,
                            width=width,
                            bottom=control_bottom,
                            label=f"Control: {segment}",
                            color=REQ2_STACK_COLORS[segment],
                        )
                        control_bottom = [
                            bottom + value
                            for bottom, value in zip(control_bottom, values)
                        ]

                    for bar in bars_treated:
                        height = bar.get_height()
                        plt.text(
                            bar.get_x() + bar.get_width() / 2,
                            height + 1,
                            f"{int(height)}",
                            ha="center",
                            va="bottom",
                            fontsize=10,
                        )

                    for xpos, total in zip([index + width / 2 for index in x], control_bottom):
                        plt.text(
                            xpos,
                            total + 1,
                            f"{int(total)}",
                            ha="center",
                            va="bottom",
                            fontsize=10,
                        )

                else:
                    control_counts: list[int] = []

                    for year in years:
                        event_col = f"event_{year}"
                        file_path = treat_root / folder_name / filename_by_year[year]
                        if not file_path.exists():
                            treated_counts.append(0)
                            control_counts.append(0)
                            continue

                        df = read_cohort_file(file_path, event_col)

                        # req0 and req1 keep the original treated-vs-control two-bar display.
                        treated = df[df[event_col] == 1]
                        controls = df[df[event_col] == 0]

                        treated_counts.append(treated[id_cols].drop_duplicates().shape[0])
                        control_counts.append(controls[id_cols].drop_duplicates().shape[0])

                    x = list(range(len(years)))
                    width = 0.35
                    plt.figure(figsize=(14, 6))

                    bars_treated = plt.bar(
                        [index - width / 2 for index in x],
                        treated_counts,
                        width=width,
                        label="Treated",
                    )
                    bars_control = plt.bar(
                        [index + width / 2 for index in x],
                        control_counts,
                        width=width,
                        label="Control",
                    )

                    for bar in bars_treated:
                        height = bar.get_height()
                        plt.text(
                            bar.get_x() + bar.get_width() / 2,
                            height + 1,
                            f"{int(height)}",
                            ha="center",
                            va="bottom",
                            fontsize=10,
                        )

                    for bar in bars_control:
                        height = bar.get_height()
                        plt.text(
                            bar.get_x() + bar.get_width() / 2,
                            height + 1,
                            f"{int(height)}",
                            ha="center",
                            va="bottom",
                            fontsize=10,
                        )

                plt.xticks(x, years, rotation=45)
                plt.ylabel(config["ylabel"])
                plt.title(
                    f"{level_folder_name} | "
                    f"{config['title_label']} | {event_type} | {treat_type} | "
                    f"req{int(event_requirement)} | {control_type} | {balanced_label}"
                )
                plt.legend()
                plt.tight_layout()

                figure_dir = (
                    PROJECT_ROOT
                    / "figures"
                    / config["figure_root"]
                    / level_folder_name
                    / treat_type
                    / get_requirement_folder(event_requirement)
                    / folder_name
                )
                figure_dir.mkdir(parents=True, exist_ok=True)

                out_path = (
                    figure_dir
                    / (
                        f"{event_type}_{panel_level}-level{output_group_suffix}_"
                        f"{treat_type}_{control_type}_{balanced_label}.png"
                    )
                )
                plt.savefig(out_path)
                plt.close()

                # Save CSV output
                csv_dir = (
                    PROJECT_ROOT
                    / "csv"
                    / config["figure_root"]
                    / level_folder_name
                    / treat_type
                    / get_requirement_folder(event_requirement)
                    / folder_name
                )
                csv_dir.mkdir(parents=True, exist_ok=True)
                
                csv_filename = (
                    f"{event_type}_{panel_level}-level{output_group_suffix}_"
                    f"{treat_type}_{control_type}_{balanced_label}.csv"
                )
                
                if is_req2_movement:
                    csv_data = {"Year": years, "Treated": treated_counts}
                    for segment in plot_segments:
                        csv_data[f"Control: {segment}"] = control_segment_counts[segment]
                    pd.DataFrame(csv_data).to_csv(csv_dir / csv_filename, index=False)
                else:
                    pd.DataFrame({
                        "Year": years, 
                        "Treated": treated_counts, 
                        "Control": control_counts
                    }).to_csv(csv_dir / csv_filename, index=False)


    print(
        "Figures and CSV tables generated and saved to:\n"
        f"  {PROJECT_ROOT}/figures/{config['figure_root']}/\n"
        f"  {PROJECT_ROOT}/csv/{config['figure_root']}/"
    )


if __name__ == "__main__":
    def ensure_list(value):
        if isinstance(value, str):
            return [value]
        return list(value)

    panel_levels = ensure_list(RUN_CONFIG["panel_levels"])
    event_types = ensure_list(RUN_CONFIG["event_types"])
    event_requirements = [int(value) for value in ensure_list(RUN_CONFIG["event_requirements"])]
    treat_types = ensure_list(RUN_CONFIG["treat_types"])
    control_types = ensure_list(RUN_CONFIG["control_types"])
    control_variations = ensure_list(RUN_CONFIG["control_variations"])
    balanced_states = [int(value) for value in ensure_list(RUN_CONFIG["balanced_states"])]
    treatment_groups = [
        str(value).upper()
        for value in ensure_list(RUN_CONFIG.get("treatment_groups", ["B"]))
    ]
    include_eventpair_values = [
        int(value)
        for value in ensure_list(RUN_CONFIG.get("include_eventpair", [1]))
    ]
    window_pre = int(RUN_CONFIG["window_pre"])
    window_post = int(RUN_CONFIG["window_post"])
    plot_start_year = int(RUN_CONFIG["plot_start_year"])
    plot_end_year = int(RUN_CONFIG["plot_end_year"])

    for panel_level in panel_levels:
        for treatment_group in treatment_groups:
            for include_eventpair in include_eventpair_values:
                for event_type in event_types:
                    if (
                        is_indirect_interlock_event(event_type)
                        and (
                            treatment_group != treatment_groups[0]
                            or include_eventpair != include_eventpair_values[0]
                        )
                    ):
                        continue
                    valid_requirements = get_valid_event_requirements(
                        event_type=event_type,
                        requested_requirements=event_requirements,
                    )

                    for event_requirement in valid_requirements:
                        for treat_type in treat_types:
                            for control_type in control_types:
                                for balanced in balanced_states:
                                    print(
                                        "Running: "
                                        f"panel_level={panel_level}, "
                                        f"treatment_group={treatment_group}, "
                                        f"include_eventpair={include_eventpair}, "
                                        f"event_type={event_type}, "
                                        f"event_requirement=req{event_requirement}, "
                                        f"treat_type={treat_type}, "
                                        f"control_type={control_type}, "
                                        f"balanced={balanced}"
                                    )
                                    build_control_groups(
                                        event_type=event_type,
                                        panel_level=panel_level,
                                        event_requirement=event_requirement,
                                        treat_type=treat_type,
                                        window_pre=window_pre,
                                        window_post=window_post,
                                        control_type=control_type,
                                        control_variations=control_variations,
                                        balanced=balanced,
                                        treatment_group=treatment_group,
                                        include_eventpair=include_eventpair,
                                    )

                        for unit_level in DISTRIBUTION_CONFIGS:
                            plot_treated_control_counts(
                                event_type=event_type,
                                event_requirement=event_requirement,
                                panel_level=panel_level,
                                start_year=plot_start_year,
                                end_year=plot_end_year,
                                treatment_group=treatment_group,
                                include_eventpair=include_eventpair,
                                unit_level=unit_level,
                                treat_types=treat_types,
                                control_types=control_types,
                                control_variations=control_variations,
                                balanced_states=balanced_states,
                            )
