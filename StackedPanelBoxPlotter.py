"""
Purpose:
Create stacked panel box plots for treated groups in cohort samples across event type, outcome variable,
treatment group, and ATC3 sharing status.

Process:
- Load balanced cohort stacks from the shared cohort data directory.
- Normalize each outcome relative to the cohort baseline, then optionally trim
    extreme high-ratio groups.
- Split the stack by ATC3 sharing value and save the resulting figures.

Input:
- data/cohort_data_with_atc3sharing/quarter-level_*/{event_type}/Pure Control/*.csv

Output:
- figures/stacked_panel_box_plot/{event_type}/{treatment_group}/{outcome_variable}/*.png

Some Observations:
- For the setting of to_B_still_in_A, B_with_A, price, hetby and separate have different pre-trends. The box plot shows that the atc3_sharing = 1 group has a increasing pre-trends, while the atc3_sharing = 0 group has a flat pre-trend. This suggests that the increasing pre-trend in the main event stack is driven by the atc3_sharing = 1 group.
- For the setting of to_B_still_in_A, A_with_B, price, normalized and standardized results are different. The box plot shows that there are still extreme values after trimming top 5% of normalized value. Under a top 10% trimming, the results show similar trends.
"""




from pathlib import Path
import re
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# =========================
# paths
# =========================
project_root = Path(r"C:\Users\LENOVO\Desktop\BoardPharma")
figures_root = project_root / "figures" / "stacked_panel_box_plot"
event_names = ["to_B_still_in_A"]
event_types = ["event", "first_event"]
outcome_variables = ["price"]
sharing_values = [1, 0]
treatment_groups = ["A", "B"]
include_eventpair = 1
apply_group_ratio_trim = 1
group_ratio_trim_quantile = 0.95


# =========================
# load and stack cohorts
# =========================
required_periods = set(range(-4, 8))
periods = list(range(-4, 8))


def cohort_group_label(treatment_group: str, include_eventpair: int) -> str:
    treatment_group = str(treatment_group).upper()
    include_eventpair = int(include_eventpair)

    if treatment_group not in {"A", "B"}:
        raise ValueError("treatment_group must be one of: A, B")
    if include_eventpair not in {0, 1}:
        raise ValueError("include_eventpair must be one of: 0, 1")

    counterpart = "B" if treatment_group == "A" else "A"
    relation = "with" if include_eventpair == 1 else "without"
    return f"{treatment_group}_{relation}_{counterpart}"


def load_event_stack(
    event_name: str,
    event_type: str,
    treatment_group: str,
    include_eventpair: int,
    outcome_variable: str,
) -> pd.DataFrame:
    if event_name not in event_names:
        raise ValueError("event_name must be one of: direct_interlock, indirect_interlock, to_B_not_in_A, to_B_still_in_A")

    group_label = cohort_group_label(treatment_group, include_eventpair)
    event_stem = event_name
    event_dir = (
        project_root
        / "data"
        / "cohort_data_with_atc3sharing"
        / f"quarter-level_{group_label}"
        / event_type
        / "Pure Control"
    )
    suffix = "_first_event" if event_type == "first_event" else ""
    pattern = re.compile(rf"{re.escape(event_stem)}_quarter_cohort_(\d{{4}}){suffix}_balanced\.csv$")
    files = sorted(event_dir.glob(f"{event_stem}_quarter_cohort_*{suffix}_balanced.csv"))

    frames = []

    for f in files:
        m = pattern.match(f.name)
        if not m:
            continue
        cohort = int(m.group(1))
        df = pd.read_csv(f)
        balance_col = f"balance_panel_{cohort}"
        if balance_col not in df.columns:
            raise KeyError(f"Missing column {balance_col} in {f.name}")
        
        # Keep only treated units with a valid balanced panel flag for this cohort.
        df = df.loc[df[balance_col] == 1].copy()
        if df.empty:
            continue
        df["cohort"] = cohort
        df["group_id"] = (
            df["BoardName"].astype(str)
            + " || "
            + df["product"].astype(str)
            + " || "
            + df["cohort"].astype(str)
        )

        # Relative time: cohort-year Q1 is the zero period.
        df["rel_period"] = (df["year"] - df["cohort"]) * 4 + (df["quarter"] - 1)
        invalid_groups = []
        for group_id, group_df in df.groupby("group_id"):
            periods_in_group = set(group_df["rel_period"])
            if periods_in_group != required_periods:
                invalid_groups.append(
                    f"{group_id}: periods={sorted(periods_in_group)}, rows={len(group_df)}"
                )
                continue
            if (group_df[outcome_variable] <= 0).any():
                invalid_groups.append(f"{group_id}: non-positive {outcome_variable}")
        if invalid_groups:
            raise ValueError(
                f"Invalid cohort data in {f.name}: " + "; ".join(invalid_groups)
            )
        frames.append(
            df[
                [
                    "BoardName",
                    "product",
                    "year",
                    "quarter",
                    "cohort",
                    "group_id",
                    "atc3_sharing",
                    outcome_variable,
                    "rel_period",
                ]
            ].copy()
        )

    if not frames:
        raise ValueError(f"No cohort files found in {event_dir}")
    stack = pd.concat(frames, ignore_index=True)

    # Normalize each outcome against the cohort-quarter baseline.
    baseline = (
        stack.loc[
            (stack["year"] == stack["cohort"]) & (stack["quarter"] == 1),
            ["group_id", outcome_variable],
        ].rename(columns={outcome_variable: f"baseline_{outcome_variable}"})
    )
    stack = stack.merge(baseline, on="group_id", how="left")
    norm_column = f"{outcome_variable}_norm"
    stack[norm_column] = stack[outcome_variable] / stack[f"baseline_{outcome_variable}"]

    # Restrict the working sample to the plotting window used downstream.
    stack = stack.loc[stack["rel_period"].between(-4, 7)].copy()

    # Store a log-normalized version for the companion figure.
    stack[f"log_{norm_column}"] = np.log(stack[norm_column])
    return stack


def validate_stack(stack: pd.DataFrame, outcome_variable: str, context: str) -> None:
    invalid_groups = []
    for group_id, group_df in stack.groupby("group_id"):
        periods_in_group = set(group_df["rel_period"])
        if periods_in_group != required_periods:
            invalid_groups.append(
                f"{group_id}: periods={sorted(periods_in_group)}, rows={len(group_df)}"
            )
            continue
        if (group_df[outcome_variable] <= 0).any():
            invalid_groups.append(f"{group_id}: non-positive {outcome_variable}")

    if invalid_groups:
        raise ValueError(f"Invalid {context} data: " + "; ".join(invalid_groups))


def trim_high_ratio_groups(
    stack: pd.DataFrame,
    norm_column: str,
    trim_quantile: float,
) -> pd.DataFrame:
    if not 0 < trim_quantile < 1:
        raise ValueError("trim_quantile must be between 0 and 1")

    group_ratio = (
        stack.groupby("group_id")[norm_column]
        .agg(group_max_norm="max", group_min_norm="min")
        .reset_index()
    )
    group_ratio["group_ratio_norm"] = (
        group_ratio["group_max_norm"] / group_ratio["group_min_norm"]
    )

    valid_ratios = group_ratio["group_ratio_norm"].replace([np.inf, -np.inf], np.nan).dropna()
    if valid_ratios.empty:
        raise ValueError("Cannot compute group ratio trim threshold: no valid groups found")

    # Drop groups above the requested upper-tail ratio threshold.
    threshold = valid_ratios.quantile(trim_quantile)
    keep_groups = group_ratio.loc[group_ratio["group_ratio_norm"] <= threshold, "group_id"]

    trimmed_stack = stack.loc[stack["group_id"].isin(keep_groups)].copy()
    dropped_groups = stack["group_id"].nunique() - trimmed_stack["group_id"].nunique()
    print(
        f"Trimmed groups above {trim_quantile:.0%} ratio threshold: dropped {dropped_groups} groups"
    )
    return trimmed_stack


def plot_boxplot(
    stack: pd.DataFrame,
    value_column: str,
    ylabel: str,
    fig_path: Path,
    title: str,
) -> Path:
    box_data = [
        stack.loc[stack["rel_period"] == p, value_column].dropna().values
        for p in periods
    ]

    fig, ax = plt.subplots(figsize=(14, 7))

    ax.boxplot(
        box_data,
        positions=periods,
        widths=0.6,
        patch_artist=True,
        boxprops=dict(facecolor="#9ecae1", edgecolor="black"),
        medianprops=dict(color="darkred", linewidth=1.5),
        whiskerprops=dict(color="black"),
        capprops=dict(color="black"),
        flierprops=dict(
            marker="o",
            markerfacecolor="gray",
            markeredgecolor="gray",
            markersize=3,
            alpha=0.35,
        ),
    )

    ax.axvline(0, color="red", linestyle="--", linewidth=1)
    ax.set_xlabel("Relative period (cohort year Q1 = 0)")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.set_xticks(periods)
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    plt.tight_layout()

    fig_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(fig_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return fig_path


# Iterate over the three user-facing dimensions that define each figure family.
for event_name in event_names:
    for treatment_group in treatment_groups:
        group_label = cohort_group_label(treatment_group, include_eventpair)

        for event_type in event_types:
            for outcome_variable in outcome_variables:
                full_stack = load_event_stack(
                    event_name,
                    event_type,
                    treatment_group,
                    include_eventpair,
                    outcome_variable,
                )
                norm_column = f"{outcome_variable}_norm"
                log_norm_column = f"log_{norm_column}"

                if apply_group_ratio_trim:
                    # Apply the shared group-level trim before splitting by sharing status.
                    full_stack = trim_high_ratio_groups(
                        full_stack,
                        norm_column,
                        group_ratio_trim_quantile,
                    )

                # Split the same prepared stack into sharing=1 and sharing=0 figures.
                for sharing_value in sharing_values:
                    stack = full_stack.loc[full_stack["atc3_sharing"] == sharing_value].copy()
                    if stack.empty:
                        raise ValueError(
                            f"No data left after filtering atc3_sharing == {sharing_value} for {event_name} / {group_label} / {event_type} / {outcome_variable}"
                        )

                    # Re-check the filtered sample so plotting fails early on malformed cohorts.
                    validate_stack(
                        stack,
                        outcome_variable,
                        f"{event_name} / {group_label} / {event_type} / {outcome_variable} / atc3_sharing={sharing_value}",
                    )

                    display_event_name = event_name.replace("_", " ")
                    display_event_type = event_type.replace("_", " ")
                    display_group_label = group_label.replace("_", " ")
                    display_outcome_variable = outcome_variable.replace("_", " ")
                    title = (
                        f"{display_event_name} | {display_group_label} | {display_event_type} | Pure Control | "
                        f"{display_outcome_variable} | atc3_sharing = {sharing_value}"
                    )

                    # Keep output names stable and descriptive across the full parameter grid.
                    figure_dir = figures_root / event_name / event_type / treatment_group / outcome_variable
                    base_name = f"{event_name}_{event_type}_{treatment_group}_{outcome_variable}_sharing{sharing_value}"

                    raw_fig_path = plot_boxplot(
                        stack,
                        norm_column,
                        f"{outcome_variable} / baseline",
                        figure_dir / f"{base_name}_norm_boxplot.png",
                        title,
                    )
                    log_fig_path = plot_boxplot(
                        stack,
                        log_norm_column,
                        f"log({outcome_variable} / baseline)",
                        figure_dir / f"{base_name}_log_norm_boxplot.png",
                        title,
                    )

                    print(f"Figure saved to: {raw_fig_path}")
                    print(f"Figure saved to: {log_fig_path}")
                    print(
                        f"Number of complete groups used ({event_name}, {group_label}, {event_type}, {outcome_variable}, sharing={sharing_value}): "
                        f"{stack['group_id'].nunique()}"
                    )
                    print(
                        f"Number of observations used ({event_name}, {group_label}, {event_type}, {outcome_variable}, sharing={sharing_value}): "
                        f"{len(stack)}"
                    )
