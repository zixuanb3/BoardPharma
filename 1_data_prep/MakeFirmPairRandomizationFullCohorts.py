"""
Purpose:
Build the complete include-event-pair cohort panels used as the fixed base for
the firm-pair randomization inference design.

Process:
1. Reuse CohortPanelMaker's existing cohort-selection logic unchanged.
2. Restrict the run to the SSR to_B_still_in_A, req1, Not-control design.
3. Set include_eventpair=1 so no counterpart-only controls are removed yet.
4. Save one balanced cohort panel for each side and cohort year.

Input:
- data/quarter-level_{A|B}/ssr_firm_panel_to_B_still_in_A_req1_large_sample_narrow.csv
- data/event_tables/movement_event_candidates_large_sample_narrow.csv
- data/event_tables/movement_table_large_sample_narrow.csv

Output:
- data/cohort_data/quarter-level_{A|B}_with_{B|A}_large_sample_narrow/
  event/req1/Not/to_B_still_in_A_quarter_cohort_YYYY_balanced_large_sample_narrow.csv
"""

from __future__ import annotations

from copy import deepcopy

from CohortPanelMaker import CohortPanelMaker, RUN_CONFIG as DEFAULT_RUN_CONFIG


# ========================== USER CONFIG ==========================
RUN_CONFIG = {
    "event_types": ["to_B_still_in_A"],
    "event_requirements": [1],
    "treat_types": ["event"],
    "control_types": ["not"],
    "control_variations": ["stable", "changing", "stable_interlock", "stable_no_interlock"],
    "window_pre": 1,
    "window_post": 1,
    "balanced_states": [1],
    "treatment_groups": ["A", "B"],
    "include_eventpair": [1],
    "plot_start_year": 2009,
    "plot_end_year": 2018,
    "large_sample": 1,
    "personnel_definition": "narrow",
}
# ================================================================


def build_config() -> dict[str, object]:
    """Return the targeted config while retaining any future maker defaults."""
    config = deepcopy(DEFAULT_RUN_CONFIG)
    config.update(RUN_CONFIG)
    return config


def main() -> None:
    """Create only the full cohort panels required by firm-pair RI."""
    CohortPanelMaker(build_config()).run()


if __name__ == "__main__":
    main()
