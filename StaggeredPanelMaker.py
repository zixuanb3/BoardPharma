from pathlib import Path

import pandas as pd


EVENT_FILE_MAP = {
    "direct_interlock": "ssr_firm_panel_direct_interlock.csv",
    "indirect_interlock": "ssr_firm_panel_indirect_interlock.csv",
    "to_B_not_in_A": "ssr_firm_panel_to_B_not_in_A.csv",
    "to_B_still_in_A": "ssr_firm_panel_to_B_still_in_A.csv",
}


def build_staggered_panel(
    panel_level,
    event_type,
    pre_periods=4,
    post_periods=4,
    balanced_panel=0,
    control_type="not_yet",
):
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

    df = pd.read_csv(input_path)

    required_cols = {"BoardName", "product", "year", "first_event_year"}
    missing_cols = sorted(required_cols - set(df.columns))
    if missing_cols:
        raise KeyError(f"Missing required columns: {missing_cols}")

    id_cols = ["BoardName", "product"]

    treated_all = df[df["first_event_year"].notna()].copy()
    control_all = df[df["first_event_year"].isna()].copy()

    treated_all["first_event_year"] = treated_all["first_event_year"].astype(int)
    treated = treated_all[
        (treated_all["year"] >= treated_all["first_event_year"] - pre_periods)
        & (treated_all["year"] <= treated_all["first_event_year"] + post_periods)
    ].copy()

    if balanced_panel == 1:
        # Modified in Mar.27, balanced_panel also requires full coverage of windows, not just balance_panel_first. 
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
        final_df = treated.copy()
    else:
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
    panel_levels = ["quarter"]
    event_types = [
        "direct_interlock",
        "indirect_interlock",
        "to_B_not_in_A",
        "to_B_still_in_A",
    ]
    control_types = ["not_yet", "pure_control"]
    balanced_panels = [1]

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
                        pre_periods=1,
                        post_periods=1,
                        balanced_panel=balanced_panel,
                        control_type=control_type,
                    )