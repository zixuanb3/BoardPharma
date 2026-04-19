---
name: latex-tables
description: Generate publication-ready LaTeX tables for regression results, summary statistics, balance tables, and similar empirical outputs using standard academic formatting.
---

# LaTeX Tables

## Overview

Produce clean, publication-ready LaTeX tables for empirical research. Optimize for compact layout, consistent notation, journal-friendly formatting, and output that can be pasted directly into a paper or appendix with minimal cleanup.

This skill is especially useful for regression tables, summary-statistics tables, balance tables, and other economics-style result tables.

## Workflow

1. Gather the table context before drafting code: table type, source software, number of columns or models, desired statistics, significance convention, notes, and any journal or style constraints.
2. Match the LaTeX structure to the task. Use `table` plus `tabular` for standard tables, `threeparttable` when notes need clearer structure, and a compact column layout that fits the page.
3. Prefer academic table conventions: `booktabs` rules, explicit captions and labels, aligned numeric columns, and concise row labels that mirror the paper text.
4. Include the metadata readers expect: observations, fit statistics, fixed-effects indicators, summary-stat columns, sample notes, and significance or standard-error notes when relevant.
5. After generating the table, explain any formatting assumptions, mention required packages, and point out likely refinements for submission-quality output.
6. When tables risk becoming too wide or dense, proactively suggest alternatives such as abbreviating labels, splitting panels, moving columns to an appendix, or using landscape formatting.

## Output Expectations

- Return valid LaTeX that can be pasted into a manuscript with minimal edits.
- Use `booktabs` by default for horizontal rules.
- Include `\caption{}` and `\label{}` unless the user explicitly wants only the tabular body.
- Add table notes for standard errors, significance stars, samples, or other conventions when needed.
- Keep notation and styling consistent across columns and tables.
- Explain compile requirements briefly when extra packages such as `threeparttable` are used.

## Best Practices

1. Keep tables compact and readable rather than maximizing the number of reported statistics.
2. Use consistent notation for stars, parentheses, brackets, and sample descriptions.
3. Make captions informative and labels stable so they can be referenced cleanly in the text.
4. Prefer clearly aligned numeric columns and avoid visual clutter from excessive vertical rules.
5. Flag journal-style mismatches early when the requested format conflicts with common economics conventions.

## Common Pitfalls

- Tables that are too wide for the page because of long variable labels or too many model columns.
- Missing notes about standard errors, clustering, weights, or significance thresholds.
- Inconsistent labels, decimal precision, or panel structure across multiple tables in the same paper.
- Raw software exports that compile but do not match publication norms.

## References

Read [references/latex-table-recipes.md](references/latex-table-recipes.md) when you need ready-to-adapt templates, package guidance, or quick formatting patterns for common econ tables.

Source inspiration: [Awesome Econ AI Stuff - LaTeX Tables](https://meleantonio.github.io/awesome-econ-ai-stuff/skills/writing/latex-tables/index/), plus linked references to [booktabs](https://ctan.org/pkg/booktabs) and the [AEA author guidelines](https://www.aeaweb.org/journals/policies/author-instructions).
