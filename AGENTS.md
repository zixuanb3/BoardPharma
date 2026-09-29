# Repository work rules

- Do not create or leave test directories, `__pycache__`, `.pyc` files, `.pytest_cache`, `pytest-cache-files-*`, `.mypy_cache`, or other tool-generated artifacts in this repository.
- Do not add or commit test files unless the user explicitly requests tests. Do not run tests as an extra step when the user has asked only for documentation or commits.
- If a tool needs temporary files or caches, place them outside this repository and disable in-repository cache or bytecode creation. If that cannot be done, skip the tool and report the limitation.
- Before committing, check both Git status and the relevant workspace directories for generated artifacts. Remove artifacts created by the current work. Remove older artifacts only when the user explicitly requests it.

## BoardPharma event-control discussion note

When discussing BoardPharma `first_event` cohort files, state the concrete finding: some existing `data/cohort_data/.../first_event/req2/...` files do not match the currently generated `quarter-level_A/B` req2 panel files. For example, the B `to_B_not_in_A` first-event cohort for 2013 contains ASSERTIO with `first_event_year=2013` and `event_2013=1`, while the current `quarter-level_B/ssr_firm_panel_to_B_not_in_A_req2.csv` has ASSERTIO's not-event in 2017 and `event_2013=0`. Before using `first_event` cohort outputs for this project, rebuild them from the current panels or explicitly verify file-panel consistency.
