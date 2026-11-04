# Event-specific date evidence for T2

`event_specific` retains usable information from either endpoint of a role.
It does not substitute company-wide earliest/latest dates and does not remove
an entire role solely because one endpoint is uncertain.

## Dates and membership

- Existing ordinary flags 10/15/20 retain their original quarter interpretation.
- Year-only flags 25/30 define Q1--Q4 bounds, rather than an assigned quarter.
- Unknown flags 75/80 leave the endpoint unbounded, subject to start <= end.
- Current flag 40 confirms membership through 2026Q3 and is right-censored;
  it does not establish an actual departure after the cutoff.
- Invalid dates, missing person identifiers and empty roles retain the existing
  eligibility exclusions. Independent role gaps are never filled.

For each possible leadership membership, the roster stores separate certain
and possible indicators. Certain membership requires presence under every
allowed endpoint placement; absence requires no possible membership. Other
states remain unknown. Role-specific and narrow/medium/broad indicators are
separate, so a possible board role cannot silently become a certain board role.
Narrow board continuity is propagated separately for certain and possible
leadership spells, retaining T2's forward continuity definition.

The existing T2 annual-board labeling assumption is retained for pairs with
dated IE/OC coverage: an annual board label applies to covered employment
periods. For annual-only pairs, the year is observed but its individual quarters
are possible, not certain. These conditional labels are not exact appointment dates.

## Event evidence

Events require confirmed adjacent membership changes and their existing
stay/REQ2 conditions. Possible changes also need a feasible underlying start
or end boundary; marginal unknown statuses alone do not establish a possible
entry or exit. In particular, a known future end cannot generate possible
departures in every earlier quarter. Continuous same-company leadership
coverage prevents an earlier role-record end from becoming a membership exit.

- Dissolution: confirmed prior joint membership and a confirmed departure;
  REQ2 requires no possible firm-pair interlock in the event/next period.
- Still in A: confirmed B entry and continued A membership; the two-year B stay
  and absence of prior firm-pair interlock must be confirmed for REQ2.
- Left A: confirmed A exit and first B entry in the original allowed delay;
  A/B retain their own event dates and side-specific REQ2 checks.

`movement_event_candidates.csv` contains confirmed base candidates only.
`firm_event_evidence.csv` separately retains confirmed/possible base and REQ2
flags. Its states are 1 (confirmed), -1 (uncertain), and 0 (ruled out).
`firm_interlock_evidence.csv` retains certain/possible interlocks. Possible
classifications are conservative when several unresolved records interact.

Controls must have no confirmed or possible focal base event anywhere in the
entire cohort window. This exclusion is firm-window-specific. Uncertainty is
not converted to a zero treatment. Every retained cohort must contain treated
units after all eligibility and product-balance filters.

## Variants and execution

The added variants are `event_specific_year` and `event_specific_yearquarter`.
The annual variant uses annual evidence and the existing Q1 regression anchor;
the quarterly variant preserves actual event quarters. Both regress quarterly
SSR price outcomes. Original imputed/drop variants remain available.

```powershell
& 'D:\ProgramData\anaconda3\python.exe' -B build_ssr_roster_t2.py `
  --date-policy event_specific --output-dir 'D:\pharma\my_run\rosters'
```

`--date-policy all` builds all six roster variants. Existing output-root
environment variables and pipeline settings apply. Stata cache version
`atc3v6eventbounds` separates these stacks from previous date policies.
No Kappa dependency is added. All four ATC3 overlap definitions remain unchanged.
