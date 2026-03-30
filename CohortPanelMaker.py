r"""
OVERVIEW:
Builds stacked cohort panels for treated and control firms based on
pre‑computed event‑study panels.  The cohorts are defined around either the
first event year or every event year, and the user can select "pure-control",
"not yet" or "not" comparisons.  Output files can be visualised with the
plotting utility provided.

INPUT:
- Pre-generated firm‑level SSR panels located under data/{year,quarter}-level
  (produced by PanelMaker_FirmLevel.py).

OUTPUT:
- Cohort CSV files saved to data/cohort_data/{frequency}/{treatment}/{control}/
- Distribution bar charts saved under figures/cohort_distribution/...

FILE STRUCTURE:
project_root/
│
├── codes/
│   └── CohortPanelMaker.py
│
├── data/
│   ├── year-level/
│   ├── quarter-level/
│
└── figures/
    ├── cohort_distribution/
    └──── year/
    └──── quarter/

ENVIRONMENT:
- Python 3.12.8 ~\anaconda3\python.exe
"""

from pathlib import Path
import pandas as pd
import matplotlib.pyplot as plt
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# mapping event keys to their panel filenames
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


def get_data_path(event_type, panel_level):
    """
    Return filepath for the specified event panel.
    """
    if event_type not in EVENT_CONFIGS:
        raise ValueError(
            "event_type must be one of: direct_interlock, indirect_interlock, "
            "to_B_not_in_A, to_B_still_in_A"
        )
    if panel_level not in ["year", "quarter"]:
        raise ValueError("panel_level must be one of: year, quarter")

    # level_folder = "year-level" if panel_level == "year" else "quarter-level"
    level_folder = "year-level" if panel_level == "year" else "quarter-level"
    return PROJECT_ROOT / "data" / level_folder / EVENT_CONFIGS[event_type]


def expected_period_count(window_years, panel_level):
    """
    Compute number of periods in a balanced window.
    """
    # yearly panels count years directly; quarterly expand each year to 4
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
    Generate treated/control cohorts within a time window around events.
    """

    if treat_type not in ["first_event", "event"]:
        raise ValueError("treat_type must be one of: first_event, event")
    if control_type not in CONTROL_FOLDER_MAP:
        raise ValueError("control_type must be one of: pure_control, not_yet, not")

    # load the relevant event study panel
    data_path = get_data_path(event_type, panel_level)
    df = pd.read_csv(data_path)


    # identifier columns for firms/products
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

    # determine cohort years depending on treatment definition
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
        # define window boundaries and expected observation count
        start, end = t - window_pre, t + window_post
        window_years = set(range(start, end + 1))
        expected_n = expected_period_count(window_years, panel_level)

        df_window = df[df["year"].between(start, end)].copy()

        if treat_type == "first_event":
            treated = df_window[df_window["first_event_year"] == t]
        else:
            event_col = f"event_{t}"
            treated_ids = df.loc[df[event_col] == 1, id_cols].drop_duplicates()
            treated = df_window.merge(treated_ids, on=id_cols, how="inner")

        # drop treated firms lacking a full set of window observations if balanced is requested
        if balanced == 1:
            treated_obs = treated.groupby(id_cols).size()
            treated = treated[
                treated.set_index(id_cols).index.isin(
                    treated_obs[treated_obs == expected_n].index
                )
            ]
            balance_col = f"balance_panel_{t}"
            treated = treated[treated[balance_col] == 1]

        # identify units without any event in the window (candidates for control)
        treated_in_window = df_window.groupby(id_cols)["event"].max()
        valid_controls = treated_in_window[treated_in_window == 0].index
        controls = df_window[df_window.set_index(id_cols).index.isin(valid_controls)]

        # enforce balanced panel on control group
        controls_obs = controls.groupby(id_cols).size()
        controls = controls[
            controls.set_index(id_cols).index.isin(
                controls_obs[controls_obs == expected_n].index
            )
        ]

        # apply selection rules per control_type
        if control_type == "pure_control":
            controls = controls[controls["first_event_year"].isna()]
        elif control_type == "not_yet":
            # include firms that are untreated or whose first event occurs after window
            controls = controls[
                (controls["first_event_year"].isna())
                | (controls["first_event_year"] > end)
            ]

        # combine treated and control observations and sort for convenience
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
    Draw bar charts showing treated vs. control counts by cohort year.
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
                    # accumulate counts for each year in this configuration
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

                        # count unique firm-product pairs in each group
                        treated_n = treated[id_cols].drop_duplicates().shape[0]
                        control_n = control[id_cols].drop_duplicates().shape[0]

                        treated_counts.append(treated_n)
                        control_counts.append(control_n)

                    # build bar chart positions and set figure size
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

                    # finalize axis labels and title before saving
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
    # combinations to iterate when running as script
    panel_levels = ["quarter"] # "year"
    event_types = [
        "direct_interlock",
        "indirect_interlock",
        "to_B_not_in_A",
        "to_B_still_in_A",
    ]
    treat_types = ["first_event", "event"]
    control_types = ["pure_control", "not_yet", "not"]
    balanced_states = [1]

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
                            window_pre=1,
                            window_post=1,
                            control_type=control_type,
                            balanced=balanced,
                        )

        plot_treated_control_counts(panel_level=panel_level)
