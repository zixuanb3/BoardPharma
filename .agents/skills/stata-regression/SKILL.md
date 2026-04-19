---
name: stata-regression
description: Design and write reproducible Stata regression workflows with diagnostics and publication-ready output. Use when Codex needs to specify OLS, fixed-effects, panel, logit/probit, or related regressions in Stata, choose clustering and controls, export tables with esttab or outreg2, or suggest diagnostics and robustness checks for empirical research.
---

# Stata Regression

## Overview

Produce clear, reproducible Stata regression workflows for empirical research. Build estimation code, table-export code, and concise explanation of identification choices, diagnostics, and robustness checks.

## Workflow

1. Gather the specification before drafting code: dependent variable, main regressors, controls, fixed effects, panel structure, clustering level, sample restrictions, weights, and desired export format.
2. Choose the estimator that matches the design. Use `regress` for baseline OLS, `reghdfe` for multiple high-dimensional fixed effects, `xtreg` for panel models, and nonlinear estimators such as `logit`, `probit`, or `poisson` when the outcome requires them.
3. Write a reproducible do-file skeleton with `version`, data loading, quick validation checks, transformations, estimation blocks, stored models, and exported tables.
4. Match standard errors to the research design. Explain why the chosen clustering or robust variance option fits the treatment variation and sampling structure.
5. Add diagnostics and robustness suggestions after the main specification: alternative fixed effects, alternative samples, functional-form checks, balance or pre-trend checks when relevant, and table notes that document the design.
6. Explain what each model estimates, what assumptions matter, and what the user should verify in the data before trusting the coefficients.

## Output Expectations

- Write Stata code that can be pasted into a do-file with minimal cleanup.
- Keep model labels, variable labels, and export filenames explicit.
- Prefer publication-ready table workflows with `esttab` or `outreg2`.
- State package dependencies when using community commands such as `reghdfe`, `esttab`, or `outreg2`.
- Surface design risks such as bad clustering choices, omitted fixed effects, or unclear variable definitions.

## References

Read [references/stata-regression-recipes.md](references/stata-regression-recipes.md) when choosing commands, table-export patterns, diagnostics, or robustness checks.
