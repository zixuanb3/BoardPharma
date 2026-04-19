# Stata Regression Recipes

Use this reference after the model specification is known.

## Estimator Selection

- Use `regress` for baseline OLS and simple robust or clustered standard errors.
- Use `reghdfe` when absorbing multiple fixed effects or large-dimensional effects.
- Use `xtreg, fe` or `xtreg, re` for classic panel-data workflows when that structure is explicit.
- Use `logit`, `probit`, or `poisson` when the outcome is binary or count-based.

## Reproducible Do-File Skeleton

```stata
version 17.0
clear all
set more off

use "data.dta", clear
describe
summarize y x1 x2

* Main specification
regress y x1 x2 x3, vce(cluster firm_id)
eststo main

* Alternative specification
reghdfe y x1 x2 x3, absorb(firm_id year) vce(cluster firm_id)
eststo fe

* Export
esttab main fe using "results/regression_table.tex", replace se label
```

## Table Export Notes

- Use `esttab` from `estout` for LaTeX-friendly tables and stored-model workflows.
- Use `outreg2` when the user wants Word or Excel-oriented outputs.
- Keep table titles, column labels, notes, and filenames explicit.
- Report fixed effects, clustered standard errors, sample size, and fit statistics consistently across specifications.

## Diagnostics and Robustness

- Check missingness, variable types, and outliers before estimating the model.
- Match clustering to the level of treatment or error correlation.
- Suggest alternative samples, alternative fixed effects, or transformed outcomes when identification is sensitive.
- For panel or event-study settings, consider pre-trend and balance checks when appropriate.

## Common Pitfalls

- Clustering at the wrong level.
- Omitting necessary fixed effects implied by the design.
- Exporting tables without labels, notes, or a clear specification map.
- Mixing sample filters across columns without stating it in the table.

## Common Packages

- `ssc install estout`
- `ssc install reghdfe`
- `ssc install outreg2`
