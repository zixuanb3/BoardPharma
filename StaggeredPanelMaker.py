"""
Purpose:
Build staggered event-study panels from firm-level event panels for staggered DID
estimators. The script keeps treated units in event-time windows around first
treatment and appends controls under selected control definitions.

Process:
- Load a firm-level panel chosen by panel_level and event_type.
- Define treated units as first-treated firm-product pairs and keep rows in
    [first_event_year - pre_periods, first_event_year + post_periods].
- If balanced_panel=1, enforce upstream balance_panel_first and complete
    in-window support for each treated unit.
- Apply control_type to decide whether to append never-treated controls.
- Save staggered panel files to data/staggered_data/{panel_level}-level.

Input:
- data/year-level/ssr_firm_panel_*.csv
- data/quarter-level/ssr_firm_panel_*.csv

Output:
- data/staggered_data/year-level/staggered_firm_level_panel_*.csv
- data/staggered_data/quarter-level/staggered_firm_level_panel_*.csv
"""

from pathlib import Path

import pandas as pd


EVENT_FILE_MAP = {
    "direct_interlock": "ssr_firm_panel_direct_interlock.csv",
    "indirect_interlock": "ssr_firm_panel_indirect_interlock.csv",
    "to_B_not_in_A": "ssr_firm_panel_to_B_not_in_A.csv",
    "to_B_still_in_A": "ssr_firm_panel_to_B_still_in_A.csv",
}


# ========================== USER CONFIG ==========================
# panel_levels:
# - "year" or "quarter".
# - Changes time granularity and therefore required row counts inside event windows.
#
# event_types:
# - Chooses which upstream treatment definition supplies first_event_year and event timing.
# - Changing event_type changes treated units and treatment interpretation.
#
# pre_periods, post_periods:
# - Keep rows in [t - pre_periods, t + post_periods], where t is first_event_year.
# - Wider windows increase rows and raise completeness thresholds under balanced_panel=1.
#
# control_types:
# - "not_yet": keep treated sample only.
# - "pure_control": append never-treated controls over treated calendar years.
#
# balanced_panels:
# - 0 or 1.
# - When 1, treated units must satisfy balance_panel_first and full in-window support.
RUN_CONFIG = {
    "panel_levels": ["quarter"],
    "event_types": [
        "direct_interlock",
        "indirect_interlock",
        "to_B_not_in_A",
        "to_B_still_in_A",
    ],
    "control_types": ["not_yet", "pure_control"],
    "pre_periods": 1,
    "post_periods": 1,
    "balanced_panels": [1],
}
# ===============================================================


def build_staggered_panel(
    panel_level,
    event_type,
    pre_periods=4,
    post_periods=4,
    balanced_panel=0,
    control_type="not_yet",
):
    """Build one staggered panel under a specific parameter combination."""
    if panel_level not in {"year", "quarter"}:
        raise ValueError("panel_level must be one of: year, quarter")

    if event_type not in EVENT_FILE_MAP:
        raise ValueError(
            "event_type must be one of: direct_interlock, indirect_interlock, "
            "to_B_not_in_A, to_B_still_in_A"
        )

    if control_type not in {"not_yet", "pure_control"}:
        raise ValueError("control_type must be one of: not_yet, pure_control")

    if balanced_panel not in {0, 1}:
        raise ValueError("balanced_panel must be 0 or 1")

    level_folder = "year-level" if panel_level == "year" else "quarter-level"

    project_root = Path(__file__).resolve().parent.parent
    input_path = project_root / "data" / level_folder / EVENT_FILE_MAP[event_type]

    # Upstream firm-level panel contains event flags and first_event_year.
    df = pd.read_csv(input_path)

    required_cols = {"BoardName", "product", "year", "first_event_year"}
    missing_cols = sorted(required_cols - set(df.columns))
    if missing_cols:
        raise KeyError(f"Missing required columns: {missing_cols}")

    id_cols = ["BoardName", "product"]

    # First-event split: treated units have observed first_event_year.
    treated_all = df[df["first_event_year"].notna()].copy()
    control_all = df[df["first_event_year"].isna()].copy()

    treated_all["first_event_year"] = treated_all["first_event_year"].astype(int)
    treated = treated_all[
        (treated_all["year"] >= treated_all["first_event_year"] - pre_periods)
        & (treated_all["year"] <= treated_all["first_event_year"] + post_periods)
    ].copy()

    if balanced_panel == 1:
        # Balanced treated sample requires both balance_panel_first and full window support.
        expected_years = pre_periods + post_periods + 1

        base_treated = treated[treated["balance_panel_first"] == 1].copy()
        expected_rows = expected_years if panel_level == "year" else expected_years * 4
        complete_ids = (
            base_treated.groupby(id_cols)
            .size()
            .loc[lambda s: s == expected_rows]
            .index
        )

        treated = base_treated[
            base_treated.set_index(id_cols).index.isin(complete_ids)
        ].copy()

    if control_type == "not_yet":
        # Keep treated sample only under the not_yet setting in this script.
        final_df = treated.copy()
    else:
        # pure_control appends never-treated units aligned to treated calendar years.
        treated_years = sorted(treated["year"].dropna().unique().tolist())
        controls = control_all[control_all["year"].isin(treated_years)].copy()
        final_df = pd.concat([treated, controls], ignore_index=True)

    final_df = final_df.sort_values(id_cols + ["year"]).reset_index(drop=True)

    output_dir = project_root / "data" / "staggered_data" / level_folder
    output_dir.mkdir(parents=True, exist_ok=True)

    balanced_suffix = "_balanced" if balanced_panel == 1 else ""
    output_name = (
        f"staggered_firm_level_panel_"
        f"{panel_level}_{event_type}_{control_type}{balanced_suffix}.csv"
    )
    output_path = output_dir / output_name

    final_df.to_csv(output_path, index=False)
    print(f"Saved: {output_path}")


if __name__ == "__main__":
    def ensure_list(v):
        if isinstance(v, str):
            return [v]
        return list(v)

    panel_levels = ensure_list(RUN_CONFIG["panel_levels"])
    event_types = ensure_list(RUN_CONFIG["event_types"])
    control_types = ensure_list(RUN_CONFIG["control_types"])
    balanced_panels = ensure_list(RUN_CONFIG["balanced_panels"])
    pre_periods = int(RUN_CONFIG["pre_periods"])
    post_periods = int(RUN_CONFIG["post_periods"])

    for panel_level in panel_levels:
        for event_type in event_types:
            for control_type in control_types:
                for balanced_panel in balanced_panels:
                    print(
                        "Running:",
                        f"panel_level={panel_level},",
                        f"event_type={event_type},",
                        f"control_type={control_type},",
                        f"balanced_panel={balanced_panel}",
                    )
                    build_staggered_panel(
                        panel_level=panel_level,
                        event_type=event_type,
                        pre_periods=pre_periods,
                        post_periods=post_periods,
                        balanced_panel=balanced_panel,
                        control_type=control_type,
                    )