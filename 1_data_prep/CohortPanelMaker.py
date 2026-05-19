r"""
Purpose:
Build cohort panels for treated and control firm-product units from pre-built
firm-level event panels. The script supports first-event and event-year cohorts,
multiple control definitions, and balanced-window filtering.

Process:
- Load a selected firm-level panel by event_type and panel_level.
- For each cohort year t, keep observations in [t-window_pre, t+window_post].
- Build treated units from first_event_year == t or event_t == 1.
- Build controls from units with no event inside the window, then apply
    control_type rules (pure_control, not_yet, not).
- When balanced=1, keep only units with complete window observations and
    pass through balance_panel_t.
- Save cohort CSV files and plot treated/control counts by cohort year for
    both product-level and firm-level distributions.

Input:
- data/year-level_{A|B}_{with|without}_{B|A}/ssr_firm_panel_*.csv
- data/quarter-level_{A|B}_{with|without}_{B|A}/ssr_firm_panel_*.csv

Output:
- data/cohort_data/{panel_level}-level_{A|B}_{with|without}_{B|A}/{treat_type}/{control_folder}/*.csv
- figures/cohort_distribution_product/{panel_level}-level_{A|B}_{with|without}_{B|A}/{treat_type}/{control_folder}/*.png
- figures/cohort_distribution_firm/{panel_level}-level_{A|B}_{with|without}_{B|A}/{treat_type}/{control_folder}/*.png
"""

from pathlib import Path
import pandas as pd
import matplotlib.pyplot as plt


PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Event key to panel filename mapping.
EVENT_CONFIGS = {
    "direct_interlock": "ssr_firm_panel_direct_interlock.csv",
    "indirect_interlock": "ssr_firm_panel_indirect_interlock.csv",
    "to_B_not_in_A": "ssr_firm_panel_to_B_not_in_A.csv",
    "to_B_still_in_A": "ssr_firm_panel_to_B_still_in_A.csv",
    "interlock_dissolution": "ssr_firm_panel_interlock_dissolution_leave_B.csv",
}

CONTROL_FOLDER_MAP = {
    "pure_control": "Pure Control",
    "not_yet": "Not Yet",
    "not": "Not",
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
# treat_types:
# - "first_event": unit enters treated sample at most once, when first_event_year == t.
# - "event": unit enters treated sample whenever event_t == 1, so one unit can appear in multiple cohort years.
#
# window_pre, window_post:
# - Retained calendar window is [t - window_pre, t + window_post].
# - Event year is the first post period; larger window_post extends post support.
# - Wider windows increase row counts and raise balanced-completeness thresholds.
#
# control_types:
# - Base "not" controls: no event anywhere inside the current window and full window support.
# - "pure_control": subset of "not" with never-treated units only.
# - "not_yet": subset of "not" with never-treated or first treatment after window end.
#
# balanced_states:
# - 0 or 1. This filter changes treated selection only.
# - When 1, treated units must have full window support and satisfy upstream balance_panel_t.
# - Controls are always required to be complete in-window by design.
#
# treatment_groups:
# - "B": destination firm as treated group (legacy behavior)
# - "A": origin firm as treated group
#
# include_eventpair:
# - 1: keep counterpart-firm observations in source panel
# - 0: source panel already drops counterpart-only firms
RUN_CONFIG = {
    "panel_levels": ["quarter"],
    "event_types": [
        "direct_interlock",
        "indirect_interlock",
        "to_B_not_in_A",
        "to_B_still_in_A",
        "interlock_dissolution",
    ],
    "treat_types": ["first_event", "event"],
    "control_types": ["pure_control", "not_yet", "not"],
    "window_pre": 1,
    "window_post": 1,
    "balanced_states": [1],
    "treatment_groups": ["B", "A"],
    "include_eventpair": [1, 0],
    "plot_start_year": 2007,
    "plot_end_year": 2018,
}
# ===============================================================


def get_panel_group_label(treatment_group):
    """
    Build panel-group suffix in the same style as PanelMaker_FirmLevel.py.
    """
    treatment_group = str(treatment_group).upper()

    if treatment_group not in ["A", "B"]:
        raise ValueError("treatment_group must be one of: A, B")

    return f"{treatment_group}"


def get_output_group_label(treatment_group, include_eventpair):
    """
    Build the folder label used by both cohort CSVs and distribution figures.
    """
    counterpart = "B" if str(treatment_group).upper() == "A" else "A"
    relation = "with" if int(include_eventpair) == 1 else "without"
    return f"{str(treatment_group).upper()}_{relation}_{counterpart}"


def get_data_path(event_type, panel_level, treatment_group="B"):
    """
    Return the source panel path for a given event_type and panel_level.
    """
    if event_type not in EVENT_CONFIGS:
        raise ValueError(
            "event_type must be one of: direct_interlock, indirect_interlock, "
            "to_B_not_in_A, to_B_still_in_A, interlock_dissolution"
        )
    if panel_level not in ["year", "quarter"]:
        raise ValueError("panel_level must be one of: year, quarter")

    group_label = get_panel_group_label(treatment_group)
    level_folder = f"{panel_level}-level_{group_label}"
    return PROJECT_ROOT / "data" / level_folder / EVENT_CONFIGS[event_type]


def expected_period_count(window_years, panel_level):
    """
    Compute required observation count for one unit in the target window.
    """
    return len(window_years) if panel_level == "year" else len(window_years) * 4


def get_cohort_filename(event_type, panel_level, cohort_year, treat_type, balanced):
    """
    Build a cohort CSV filename from the current configuration.
    """
    balanced_suffix = "_balanced" if balanced == 1 else ""
    treat_type_suffix = "_first_event" if treat_type == "first_event" else ""
    return (
        f"{event_type}_{panel_level}_cohort_{cohort_year}"
        f"{treat_type_suffix}"
        f"{balanced_suffix}.csv"
    )


def build_control_groups(
    event_type,
    panel_level="year",
    treat_type="first_event",
    window_pre=1,
    window_post=1,
    control_type="pure_control",
    balanced=1,
    treatment_group="B",
    include_eventpair=1,
):
    """
    Build cohort-level treated and control samples around each cohort year.
    """
    if treat_type not in ["first_event", "event"]:
        raise ValueError("treat_type must be one of: first_event, event")
    if control_type not in CONTROL_FOLDER_MAP:
        raise ValueError("control_type must be one of: pure_control, not_yet, not")

    data_path = get_data_path(
        event_type,
        panel_level,
        treatment_group=treatment_group,
    )
    df = pd.read_csv(data_path)

    # Load movement list if include_eventpair is 0
    movement_df = None
    if include_eventpair == 0 and event_type in ["to_B_not_in_A", "to_B_still_in_A", "interlock_dissolution"]:
        movement_dir = PROJECT_ROOT / "data" / "movement_list"
        
        if event_type == "interlock_dissolution":
            # For interlock_dissolution, we read the specific leave_B_movement.csv
            dissolution_file = movement_dir / "leave_B_movement.csv"
            if dissolution_file.exists():
                movement_df = pd.read_csv(dissolution_file)
        else:
            csv_files = list(movement_dir.glob("*.csv"))
            if csv_files:
                movement_df = pd.concat(
                    [pd.read_csv(file_path) for file_path in csv_files],
                    ignore_index=True,
                )
                mode_val = "still" if event_type == "to_B_still_in_A" else "not"
                if "event_type" in movement_df.columns:
                    movement_df = movement_df[movement_df["event_type"] == mode_val]

    id_cols = ["BoardName", "product"]
    output_group_label = get_output_group_label(treatment_group, include_eventpair)

    output_dir = (
        PROJECT_ROOT
        / "data"
        / "cohort_data"
        / f"{panel_level}-level_{output_group_label}"
        / treat_type
        / CONTROL_FOLDER_MAP[control_type]
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    # Cohort year source depends on treat_type definition.
    if treat_type == "first_event":
        cohorts = (
            df.loc[df["first_event_year"].notna(), "first_event_year"]
            .astype(int)
            .unique()
        )
    else:
        event_cols = [col for col in df.columns if col.startswith("event_")]
        cohorts = sorted(int(col.split("_")[1]) for col in event_cols)

    for cohort_year in sorted(cohorts):
        # Event-time window for this cohort year t.
        start = cohort_year - window_pre
        end = cohort_year + window_post
        window_years = set(range(start, end + 1))
        expected_n = expected_period_count(window_years, panel_level)

        df_window = df[df["year"].between(start, end)].copy()

        if treat_type == "first_event":
            treated = df_window[df_window["first_event_year"] == cohort_year]
        else:
            # event_t treatment: treated units are those with event_t == 1.
            event_col = f"event_{cohort_year}"
            treated_ids = df.loc[df[event_col] == 1, id_cols].drop_duplicates()
            treated = df_window.merge(treated_ids, on=id_cols, how="inner")

        # Balanced cohorts require complete window coverage and balance_panel_t == 1.
        if balanced == 1:
            treated_obs = treated.groupby(id_cols).size()
            treated = treated[
                treated.set_index(id_cols).index.isin(
                    treated_obs[treated_obs == expected_n].index
                )
            ]
            balance_col = f"balance_panel_{cohort_year}"
            treated = treated[treated[balance_col] == 1]

        # Candidate controls must have no event inside the cohort window.
        treated_in_window = df_window.groupby(id_cols)["event"].max()
        valid_controls = treated_in_window[treated_in_window == 0].index
        controls = df_window[df_window.set_index(id_cols).index.isin(valid_controls)]

        # Keep control units with full window observations.
        controls_obs = controls.groupby(id_cols).size()
        controls = controls[
            controls.set_index(id_cols).index.isin(
                controls_obs[controls_obs == expected_n].index
            )
        ]

        # control_type narrows controls by future treatment timing.
        if control_type == "pure_control":
            controls = controls[controls["first_event_year"].isna()]
        elif control_type == "not_yet":
            # Allow never-treated and future-treated units after this window.
            controls = controls[
                (controls["first_event_year"].isna())
                | (controls["first_event_year"] > end)
            ]

        if movement_df is not None:
            # find year t movements
            year_moves = movement_df[movement_df["movement_year"] == cohort_year]
            if not year_moves.empty:
                if str(treatment_group).upper() == "B":
                    drop_list = set(year_moves["FirmA"]) - set(year_moves["FirmB"])
                else:
                    drop_list = set(year_moves["FirmB"]) - set(year_moves["FirmA"])

                controls = controls[~controls["BoardName"].isin(drop_list)]

        # Final cohort sample for year t.
        cohort_df = pd.concat([treated, controls], axis=0)
        cohort_df = cohort_df.sort_values(id_cols + ["year"])

        filename = get_cohort_filename(
            event_type=event_type,
            panel_level=panel_level,
            cohort_year=cohort_year,
            treat_type=treat_type,
            balanced=balanced,
        )
        cohort_df.to_csv(output_dir / filename, index=False)

    print(f"Finished. Files saved to: {output_dir}")


def plot_treated_control_counts(
    panel_level="year",
    start_year=2007,
    end_year=2018,
    treatment_group="B",
    include_eventpair=1,
    unit_level="product",
):
    """
    Plot treated/control unit counts by cohort year and configuration.

    Notes:
        - product: counts unique (BoardName, product) pairs
        - firm: counts unique BoardName values within treated/control rows
    """
    if unit_level not in DISTRIBUTION_CONFIGS:
        valid_levels = ", ".join(sorted(DISTRIBUTION_CONFIGS))
        raise ValueError(f"unit_level must be one of: {valid_levels}")

    treat_types = ["first_event", "event"]
    control_types = ["pure_control", "not_yet", "not"]
    balanced_states = {1: "balanced"}
    years = list(range(start_year, end_year + 1))

    config = DISTRIBUTION_CONFIGS[unit_level]
    id_cols = config["id_cols"]
    output_group_label = get_output_group_label(treatment_group, include_eventpair)
    cohort_root = (
        PROJECT_ROOT
        / "data"
        / "cohort_data"
        / f"{panel_level}-level_{output_group_label}"
    )

    for event_type in EVENT_CONFIGS:
        for treat_type in treat_types:
            for control_type in control_types:
                folder_name = CONTROL_FOLDER_MAP[control_type]

                for balanced, bal_name in balanced_states.items():
                    treated_counts = []
                    control_counts = []

                    for year in years:
                        filename = get_cohort_filename(
                            event_type=event_type,
                            panel_level=panel_level,
                            cohort_year=year,
                            treat_type=treat_type,
                            balanced=balanced,
                        )
                        file_path = cohort_root / treat_type / folder_name / filename

                        if not file_path.exists():
                            raise FileNotFoundError(f"Missing cohort file: {file_path}")

                        df = pd.read_csv(file_path)
                        event_col = f"event_{year}"

                        if event_col not in df.columns:
                            raise KeyError(
                                f"Missing event column: {event_col}\nFile: {file_path}"
                            )

                        treated = df[df[event_col] == 1]
                        control = df[df[event_col] == 0]

                        treated_n = treated[id_cols].drop_duplicates().shape[0]
                        control_n = control[id_cols].drop_duplicates().shape[0]

                        treated_counts.append(treated_n)
                        control_counts.append(control_n)

                    x = range(len(years))
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
                        f"{panel_level}-level_{output_group_label} | "
                        f"{config['title_label']} | {event_type} | "
                        f"{treat_type} | {control_type} | {bal_name}"
                    )
                    plt.legend()
                    plt.tight_layout()

                    figure_dir = (
                        PROJECT_ROOT
                        / "figures"
                        / config["figure_root"]
                        / f"{panel_level}-level_{output_group_label}"
                        / treat_type
                        / folder_name
                    )
                    figure_dir.mkdir(parents=True, exist_ok=True)

                    out_path = (
                        figure_dir
                        / (
                            f"{event_type}_{panel_level}-level_{output_group_label}_"
                            f"{treat_type}_{control_type}_{bal_name}.png"
                        )
                    )
                    plt.savefig(out_path)
                    plt.close()

    print(
        "All figures generated and saved to: "
        f"./figures/{config['figure_root']}/"
    )


if __name__ == "__main__":
    def ensure_list(value):
        if isinstance(value, str):
            return [value]
        return list(value)

    panel_levels = ensure_list(RUN_CONFIG["panel_levels"])
    event_types = ensure_list(RUN_CONFIG["event_types"])
    treat_types = ensure_list(RUN_CONFIG["treat_types"])
    control_types = ensure_list(RUN_CONFIG["control_types"])
    balanced_states = ensure_list(RUN_CONFIG["balanced_states"])
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
                    for treat_type in treat_types:
                        for control_type in control_types:
                            for balanced in balanced_states:
                                print(
                                    "Running: "
                                    f"panel_level={panel_level}, "
                                    f"treatment_group={treatment_group}, "
                                    f"include_eventpair={include_eventpair}, "
                                    f"event_type={event_type}, "
                                    f"treat_type={treat_type}, "
                                    f"control_type={control_type}, "
                                    f"balanced={balanced}"
                                )
                                build_control_groups(
                                    event_type=event_type,
                                    panel_level=panel_level,
                                    treat_type=treat_type,
                                    window_pre=window_pre,
                                    window_post=window_post,
                                    control_type=control_type,
                                    balanced=balanced,
                                    treatment_group=treatment_group,
                                    include_eventpair=include_eventpair,
                                )

                for unit_level in DISTRIBUTION_CONFIGS:
                    plot_treated_control_counts(
                        panel_level=panel_level,
                        start_year=plot_start_year,
                        end_year=plot_end_year,
                        treatment_group=treatment_group,
                        include_eventpair=include_eventpair,
                        unit_level=unit_level,
                    )
