# LaTeX Table Recipes

Use this reference when the user wants concrete LaTeX patterns instead of only general guidance.

## Core Packages

- `booktabs`: default for clean horizontal rules via `\toprule`, `\midrule`, and `\bottomrule`
- `threeparttable`: useful when the table needs structured notes below the main body
- `array` or `siunitx`: useful when numeric alignment needs more control

## Minimal Regression Table

```latex
\begin{table}[htbp]\centering
\caption{Effect of Treatment on Outcome}
\label{tab:main_results}
\begin{threeparttable}
\begin{tabular}{lccc}
\toprule
 & (1) & (2) & (3) \\
\midrule
Treatment & 0.125*** & 0.118*** & 0.102** \\
 & (0.041) & (0.039) & (0.046) \\
Controls & No & Yes & Yes \\
Fixed Effects & No & Yes & Yes \\
\midrule
Observations & 2,145 & 2,145 & 2,145 \\
R-squared & 0.18 & 0.24 & 0.31 \\
\bottomrule
\end{tabular}
\begin{tablenotes}
\small
\item Notes: Standard errors in parentheses. * p<0.10, ** p<0.05, *** p<0.01.
\end{tablenotes}
\end{threeparttable}
\end{table}
```

## Summary Statistics Pattern

Use a compact structure with a stub column plus a small set of descriptive columns such as mean, standard deviation, minimum, maximum, and observations.

```latex
\begin{table}[htbp]\centering
\caption{Summary Statistics}
\label{tab:summary_stats}
\begin{tabular}{lccccc}
\toprule
Variable & Mean & SD & Min & Max & N \\
\midrule
Outcome & 1.284 & 0.517 & 0.000 & 2.900 & 2145 \\
Treatment & 0.462 & 0.499 & 0.000 & 1.000 & 2145 \\
Age & 41.830 & 12.640 & 18.000 & 79.000 & 2145 \\
\bottomrule
\end{tabular}
\end{table}
```

## Balance Table Pattern

For treatment-control comparisons, include group means, the difference, and optionally a p-value.

```latex
\begin{table}[htbp]\centering
\caption{Baseline Balance}
\label{tab:balance}
\begin{tabular}{lcccc}
\toprule
Variable & Control Mean & Treated Mean & Difference & p-value \\
\midrule
Age & 41.2 & 42.0 & 0.8 & 0.214 \\
Income & 5.31 & 5.48 & 0.17 & 0.087 \\
Female & 0.51 & 0.49 & -0.02 & 0.462 \\
\bottomrule
\end{tabular}
\end{table}
```

## Formatting Guidance

- Keep decimal precision consistent within a row type.
- Avoid vertical rules unless the target journal explicitly requires them.
- Shorten verbose variable names in the table and explain full definitions in notes or the text.
- If a table is too wide, first shorten labels, then reduce reported statistics, then consider panel splits or landscape layout.
- For multi-model regression tables, place coefficient estimates and standard errors on separate rows unless the target style clearly prefers another convention.

## Journal-Oriented Checks

- Confirm whether the journal wants significance stars, confidence intervals, or only standard errors.
- Confirm whether table notes should state clustering, weights, and fixed effects explicitly.
- Check whether the style guide prefers title case or sentence case captions.
- Verify that labels follow the manuscript's naming convention, such as `tab:main_results` or `tab:appendix_balance`.
