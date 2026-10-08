# Repository work rules

## Communication rule for BoardPharma data questions

Explain from the user's actual point of uncertainty. Never assume that a file,
table, column, script, abbreviation, historical decision, or technical term is
already understood merely because it appeared earlier in the conversation.
Before relying on any such thing, say what it is in plain language, where it
came from, what it contains or does, and why it matters to the question.

Answer the question asked before adding context. For data and code questions,
show the concrete input and output rows, values, or code action that establish
the answer. For comparisons, distinguish data-value changes, row-order changes,
and file-format changes. Do not make the user infer the missing connection
between steps. When historical code is relevant, identify the exact code and
state whether the claim comes from its text or from an inference about its use.

## Fidelity and change-control rule

Do not silently substitute an unavailable input, add a rule, omit a requested
step, change output ordering or format, or otherwise alter the user's specified
process or result. Before making any change that cannot exactly satisfy the
user's stated requirements, identify the exact mismatch, its effect on the
result, and the available alternatives. Wait for the user's explicit decision
before proceeding with that non-identical implementation. A generated result
must state whether it was reproduced from the original inputs and process, or
whether any substitute input or reconstructed step was used.

- Do not create or leave test directories, `__pycache__`, `.pyc` files, `.pytest_cache`, `pytest-cache-files-*`, `.mypy_cache`, or other tool-generated artifacts in this repository.
- Do not add or commit test files unless the user explicitly requests tests. Do not run tests as an extra step when the user has asked only for documentation or commits.
- If a tool needs temporary files or caches, place them outside this repository and disable in-repository cache or bytecode creation. If that cannot be done, skip the tool and report the limitation.
- Before committing, check both Git status and the relevant workspace directories for generated artifacts. Remove artifacts created by the current work. Remove older artifacts only when the user explicitly requests it.

## BoardPharma event-control discussion note

When discussing BoardPharma `first_event` cohort files, state the concrete finding: some existing `data/cohort_data/.../first_event/req2/...` files do not match the currently generated `quarter-level_A/B` req2 panel files. For example, the B `to_B_not_in_A` first-event cohort for 2013 contains ASSERTIO with `first_event_year=2013` and `event_2013=1`, while the current `quarter-level_B/ssr_firm_panel_to_B_not_in_A_req2.csv` has ASSERTIO's not-event in 2017 and `event_2013=0`. Before using `first_event` cohort outputs for this project, rebuild them from the current panels or explicitly verify file-panel consistency.
