# Repository work rules

## Communication rule for BoardPharma data questions

### Start with the question the user needs answered

Identify the uncertainty behind the question and lead with a direct answer.
Use the conversation to determine the requested scope and depth. Resolve the
question completely, including the information needed to understand and verify
the answer. Keep the explanation focused; remove repetition and background that
does not help establish the answer. Use the user's language unless another
language is requested.

Answer only the question asked. Include supporting details only when they are
necessary to establish or verify the answer; omit adjacent background, optional
extensions, and unsolicited recommendations.

### Show the basis for the conclusion

For a claim about data, show the actual fields and values that support it.
For a claim about code, identify the inspected implementation and show the
operation that supports it. Include every field on which the displayed judgment
depends, even if the field's meaning seems obvious. The reader must be able to
verify the claim from the evidence provided without guessing what was omitted.

Identify where the evidence came from. Distinguish original source records,
saved extracts, reconstructed results, and illustrative examples. Identify the
unit represented by a row when interpreting records. Label original,
transformed, and output values clearly. Use readable tables or small code
excerpts when they make the evidence easier to inspect. Link to actual files
with absolute paths and verified line numbers where applicable.

### Explain the connection between evidence and result

Introduce a field, identifier, function, or technical term before relying on it
if its meaning is needed to follow the answer. Explain its source and concrete
meaning; an unexplained label or phrase such as "standardized" is insufficient.
For a derived value, show the transformation or mapping that produces it.

Connect the displayed input values to the actual code condition or operation,
and then to the resulting values. Explain what information the operation
preserves, changes, or discards when that determines the answer. When several
records are involved, make clear which records are processed together, why,
and how each relevant output value is chosen. Explain an order-dependent result
using the actual execution order. Cover all branches when the user requests the
complete rule; otherwise cover the branches needed to establish the answer.

### Keep claims within what has been established

Separate observed data, inspected code behavior, and inferred explanations.
Specify the scope of a claim about changed behavior rather than applying a local
change to the whole process. Identify the implementation and version being
explained. If a needed source or historical implementation is unavailable,
state the resulting limit instead of inventing its behavior.

Distinguish what the program produces from whether that output measures the
user's intended concept. Matching identifiers or successfully reproducing an
output is evidence about the processing rules, not automatic proof of the
underlying real-world interpretation.

For comparisons, define the compared inputs, units, preparation rules, and
counting basis before interpreting the results. Distinguish differences in
values, ordering, and format. Give cause categories descriptive names, explain
the conditions separating them, and reconcile counts. State whether categories
overlap and whether assignment precedence is an audit convention. Follow the
requested examples, periods, language, and output format.

### Repair an unclear explanation at the point of failure

When the user says they do not understand, re-examine the previous answer for
a missing field, undefined term, unsupported claim, or unexplained connection.
Supply that missing piece directly and correct any error. Do not repeat the same
abstraction or add more background in place of the missing evidence. A response
is adequate when it resolves the user's uncertainty, not when it is long.

Explanation requests authorize inspection. Change code, data, or existing
artifacts only when those changes are within the user's requested scope.

## Report language and format

- Write research reports and their tables in English unless the user explicitly requests another language.
- Before creating a report, inspect the existing reports in the repository and reuse the closest matching report's layout, table conventions, panel ordering, and typography. For cohort NDC summaries, use the existing academic LaTeX/booktabs report with event-by-direction panels, numeric columns, totals, and concise table notes. Do not introduce dashboard-style colored tables or a new visual design without a user request.
- Use the user's requested output directory. State any difference in statistical definitions from a reference report rather than silently changing the counting rules.

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
