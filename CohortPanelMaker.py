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
- Save cohort CSV files and optionally plot treated/control counts by cohort year.

Input:
- data/year-level/ssr_firm_panel_*.csv
- data/quarter-level/ssr_firm_panel_*.csv

Output:
- data/cohort_data/{panel_level}/{treat_type}/{control_folder}/*.csv
- figures/cohort_distribution/{panel_level}/{treat_type}/{control_folder}/*.png
"""

from pathlib import Path
import pandas as pd
import matplotlib.pyplot as plt
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Event key to panel filename mapping.
EVENT_CONFIGS = {
    "direct_interlock": "ssr_firm_panel_direct_interlock.csv",
    "indirect_interlock": "ssr_firm_panel_indirect_interlock.csv",
    "to_B_not_in_A": "ssr_firm_panel_to_B_not_in_A.csv",
    "to_B_still_in_A": "ssr_firm_panel_to_B_still_in_A.csv",
}

CONTROL_FOLDER_MAP = {
    "pure_control": "Pure Control",
    "not_yet": "Not Yet",
    "not": "Not",
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
RUN_CONFIG = {
    "panel_levels": ["quarter"],
    "event_types": [
        "direct_interlock",
        "indirect_interlock",
        "to_B_not_in_A",
        "to_B_still_in_A",
    ],
    "treat_types": ["first_event", "event"],
    "control_types": ["pure_control", "not_yet", "not"],
    "window_pre": 1,
    "window_post": 1,
    "balanced_states": [1],
    "plot_start_year": 2007,
    "plot_end_year": 2018,
}
# ===============================================================


def get_data_path(event_type, panel_level):
    """
    Return the source panel path for a given event_type and panel_level.
    """
    if event_type not in EVENT_CONFIGS:
        raise ValueError(
            "event_type must be one of: direct_interlock, indirect_interlock, "
            "to_B_not_in_A, to_B_still_in_A"
        )
    if panel_level not in ["year", "quarter"]:
        raise ValueError("panel_level must be one of: year, quarter")

    level_folder = "year-level" if panel_level == "year" else "quarter-level"
    return PROJECT_ROOT / "data" / level_folder / EVENT_CONFIGS[event_type]


def expected_period_count(window_years, panel_level):
    """
    Compute required observation count for one unit in the target window.
    """
    # Quarter panels expand each year into four periods.
    return len(window_years) if panel_level == "year" else len(window_years) * 4


def build_control_groups(
    event_type,
    panel_level="year",
    treat_type="first_event",
    window_pre=1,
    window_post=1,
    control_type="pure_control",
    balanced=1,
):
    """
    Build cohort-level treated and control samples around each cohort year.
    """

    if treat_type not in ["first_event", "event"]:
        raise ValueError("treat_type must be one of: first_event, event")
    if control_type not in CONTROL_FOLDER_MAP:
        raise ValueError("control_type must be one of: pure_control, not_yet, not")

    # Source panel already contains event indicators and balance_panel_* tags.
    data_path = get_data_path(event_type, panel_level)
    df = pd.read_csv(data_path)

    # Unit of analysis used for deduplication and balancing.
    id_cols = ["BoardName", "product"]

    output_dir = (
        PROJECT_ROOT
        / "data"
        / "cohort_data"
        / panel_level
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
        event_cols = [c for c in df.columns if c.startswith("event_")]
        cohorts = sorted(int(c.split("_")[1]) for c in event_cols)

    for t in sorted(cohorts):
        # Event-time window for this cohort year t.
        start, end = t - window_pre, t + window_post
        window_years = set(range(start, end + 1))
        expected_n = expected_period_count(window_years, panel_level)

        df_window = df[df["year"].between(start, end)].copy()

        if treat_type == "first_event":
            treated = df_window[df_window["first_event_year"] == t]
        else:
            # event_t treatment: treated units are those with event_t == 1.
            event_col = f"event_{t}"
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
            balance_col = f"balance_panel_{t}"
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

        # Final cohort sample for year t.
        cohort_df = pd.concat([treated, controls], axis=0)
        cohort_df = cohort_df.sort_values(id_cols + ["year"])

        balanced_suffix = "_balanced" if balanced == 1 else ""
        treat_type_suffix = "_first_event" if treat_type == "first_event" else ""

        filename = (
            f"{event_type}_{panel_level}_cohort_{t}"
            f"{treat_type_suffix}"
            f"{balanced_suffix}.csv"
        )

        out_path = output_dir / filename
        cohort_df.to_csv(out_path, index=False)

    print(f"Finished. Files saved to: {output_dir}")


def plot_treated_control_counts(panel_level="year", start_year=2007, end_year=2018):
    """
    Plot treated/control unit counts by cohort year and configuration.
    """
    treat_types = ["first_event", "event"]
    control_types = ["pure_control", "not_yet", "not"]
    balanced_states = {1: "balanced"}

    id_cols = ["BoardName", "product"]
    years = list(range(start_year, end_year + 1))

    for event_type in EVENT_CONFIGS:
        for treat_type in treat_types:
            treat_type_suffix = "_first_event" if treat_type == "first_event" else ""

            for control_type in control_types:
                folder_name = CONTROL_FOLDER_MAP[control_type]

                for balanced, bal_name in balanced_states.items():
                    # Collect yearly treated/control counts for one configuration.
                    treated_counts = []
                    control_counts = []

                    for y in years:
                        balanced_suffix = "_balanced" if balanced == 1 else ""
                        filename = (
                            f"{event_type}_{panel_level}_cohort_{y}"
                            f"{treat_type_suffix}"
                            f"{balanced_suffix}.csv"
                        )

                        file_path = (
                            PROJECT_ROOT
                            / "data"
                            / "cohort_data"
                            / panel_level
                            / treat_type
                            / folder_name
                            / filename
                        )

                        if not file_path.exists():
                            raise FileNotFoundError(f"Missing cohort file: {file_path}")

                        df = pd.read_csv(file_path)
                        event_col = f"event_{y}"

                        if event_col not in df.columns:
                            raise KeyError(
                                f"Missing event column: {event_col}\\nFile: {file_path}"
                            )

                        treated = df[df[event_col] == 1]
                        control = df[df[event_col] == 0]

                        # Count unique firm-product units.
                        treated_n = treated[id_cols].drop_duplicates().shape[0]
                        control_n = control[id_cols].drop_duplicates().shape[0]

                        treated_counts.append(treated_n)
                        control_counts.append(control_n)

                    # Build grouped bars for treated vs control.
                    x = range(len(years))
                    width = 0.35
                    plt.figure(figsize=(14, 6))

                    bars_treated = plt.bar(
                        [i - width / 2 for i in x],
                        treated_counts,
                        width=width,
                        label="Treated",
                    )
                    bars_control = plt.bar(
                        [i + width / 2 for i in x],
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

                    # Apply labels and save figure.
                    plt.xticks(x, years, rotation=45)
                    plt.ylabel("Number of BoardName-Product Pairs")
                    plt.title(
                        f"{panel_level} | {event_type} | {treat_type} | {control_type} | {bal_name}"
                    )
                    plt.legend()
                    plt.tight_layout()

                    figure_dir = (
                        PROJECT_ROOT
                        / "figures"
                        / "cohort_distribution"
                        / panel_level
                        / treat_type
                        / folder_name
                    )
                    figure_dir.mkdir(parents=True, exist_ok=True)

                    out_path = (
                        figure_dir
                        / f"{event_type}_{panel_level}_{treat_type}_{control_type}_{bal_name}.png"
                    )
                    plt.savefig(out_path)
                    plt.close()

    print("All figures generated and saved to ./figures/cohort_distribution/")


if __name__ == "__main__":
    def ensure_list(v):
        if isinstance(v, str):
            return [v]
        return list(v)

    panel_levels = ensure_list(RUN_CONFIG["panel_levels"])
    event_types = ensure_list(RUN_CONFIG["event_types"])
    treat_types = ensure_list(RUN_CONFIG["treat_types"])
    control_types = ensure_list(RUN_CONFIG["control_types"])
    balanced_states = ensure_list(RUN_CONFIG["balanced_states"])
    window_pre = int(RUN_CONFIG["window_pre"])
    window_post = int(RUN_CONFIG["window_post"])
    plot_start_year = int(RUN_CONFIG["plot_start_year"])
    plot_end_year = int(RUN_CONFIG["plot_end_year"])

    for panel_level in panel_levels:
        for event_type in event_types:
            for treat_type in treat_types:
                for control_type in control_types:
                    for balanced in balanced_states:
                        print(
                            "Running: "
                            f"panel_level={panel_level}, "
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
                        )

        plot_treated_control_counts(
            panel_level=panel_level,
            start_year=plot_start_year,
            end_year=plot_end_year,
        )
