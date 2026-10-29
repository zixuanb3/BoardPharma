# ATC3 overlap over the complete T2 event window

`CohortPanelMaker.py` accepts `atc3_overlap_mode` in its configuration,
`--atc3-overlap-mode` on the command line, or the shared environment variable
`BOARDPHARMA_ATC3_OVERLAP_MODE`. The command-line argument overrides the
environment. Python and both Stata scripts use the same mode names.

| Value | Definition | Label across the outcome window |
| --- | --- | --- |
| `window_all` | Matching counterpart products must be present in every quarter of the complete window; any uncovered quarter makes the label zero | Fixed |
| `window_any` | A matching counterpart product in any quarter of the complete window makes the label one; no covered quarters makes it zero | Fixed |
| `quarterly` | Independently compare actual same-quarter ATC3 portfolios in every quarter of the complete window, before and after the event | Varies by quarter |
| `event_quarter` | Historical T2 definition: compare only the event-quarter portfolios and copy that label across the window | Fixed; default |

The window is the exact interval used to construct the cohort, not the event
calendar year. Default T2 settings are relative quarters -4 through +7,
including the event quarter: twelve quarters in total. A 2012Q1 event therefore
uses 2011Q1 through 2013Q4 for all three full-window modes. Different configured
pre/post window lengths are honored automatically.

## One quarterly sequence, three full-window modes

For each treated company/product, let `g(q)` indicate a same-quarter ATC3 match
with at least one eligible event counterpart's products. The three modes use:

- `quarterly`: keep `g(q)` separately for each observation quarter.
- `window_all`: copy `min(g(q))` over all required window quarters to every
  outcome row for that company/product in this cohort.
- `window_any`: copy `max(g(q))` over all required window quarters to every
  outcome row for that company/product in this cohort.

This uses contemporaneous product classifications on both sides for each
quarterly comparison. It does not match a focal product classification observed
only in one quarter to a counterpart classification observed only in another.
An absent ATC3/product/partner market record produces a zero quarterly check;
required quarters with no match are not silently removed from the all-quarter
denominator. No outcome observation is filled or inserted for those checks.

Presence is evaluated at the ATC3-class level. Different counterpart products
may cover different quarters: the same product need not appear throughout the
window. When multiple counterpart companies are eligible for the focal event,
their ATC3 portfolios are pooled within each quarter.

## Real Plavix example

For the stored A-direction 2012Q1 not-event cohort, the twelve Plavix rows are:

| Outcome year | `quarterly` in Q1,Q2,Q3,Q4 | `window_all` | `window_any` |
| --- | --- | --- | --- |
| 2011 | 0, 0, 1, 0 | 0 in every quarter | 1 in every quarter |
| 2012 | 0, 1, 1, 1 | 0 in every quarter | 1 in every quarter |
| 2013 | 1, 1, 1, 1 | 0 in every quarter | 1 in every quarter |

Both fixed modes use all twelve quarters. Quarterly checks also cover all
twelve; they are not limited to 2012 or to post-event observations.

## Comparison universe and sample preservation

The ATC3 source is the complete
`InterimData/boardex_ssr_price_sample.csv`. Counterpart products are not filtered
to the balanced regression sample. Event counterpart identities are fixed by
the current cohort, event type, REQ level, and A/B direction. Pre/post quarter
comparisons use those same counterpart identities; their product portfolios and
the focal product's classifications are read independently in each quarter.

The overlap calculation follows cohort eligibility and balancing. It preserves
all outcome rows, values, and ordering. Controls retain zero sharing labels.
Each generated cohort records its definition in `atc3_overlap_mode`.

## Python and Stata selection

```powershell
$env:BOARDPHARMA_ATC3_OVERLAP_MODE = 'window_all'
& 'D:\ProgramData\anaconda3\python.exe' -B '.\CohortPanelMaker.py' --atc3-overlap-mode window_all
```

Alternatively, set `RUN_CONFIG['atc3_overlap_mode']` to `window_all`,
`window_any`, or `quarterly`. Rebuild the requested cohorts before regression.
Existing path variables and event/requirement/control/tier settings still apply.
For existing T2 firm panels, select REQ2, `not` controls, and `narrow` personnel:

```python
from CohortPanelMaker import CohortPanelMaker, CohortPlotter, RUN_CONFIG

config = {
    **RUN_CONFIG,
    'atc3_overlap_mode': 'window_all',
    'event_requirements': [2],
    'control_types': ['not'],
    'personnel_definitions': ['narrow'],
}
CohortPanelMaker(config).run()
CohortPlotter(config).run()
```

Set `SSR_ROSTER_ROOT` to the T2 rosters and
`BOARDPHARMA_VARIANT_OUTPUT_ROOT` to the T2 firm-panel parent before importing
the module. Set `BOARDPHARMA_OUTPUT_ROOT` for summary output as needed. Changing
the overlap rule alone does not require rebuilding raw events or firm panels.

The DID and DDD scripts read the same environment variable. Their optional
eleventh argument takes precedence:

```stata
do "ddd_atcsharing_req2_fe1_window_event_control.do" imputed_year narrow 2 1 price1 0 A to_B_not_in_A not none window_all
```

In DDD, `did_g = treat * post * atc_sharing` uses the selected fixed or quarterly
label. In DID, `hetby(atc_sharing_het)` groups treated observations by their
current label. With `quarterly`, a product may contribute to different groups
at different horizons; with `window_all`/`window_any`, its group is fixed.

## Output isolation and corrected names

Within each variant/tier directory:

- `event_quarter`: `cohort_data/` (historical default path).
- `window_all`: `cohort_data_atc3_window_all/`.
- `window_any`: `cohort_data_atc3_window_any/`.
- `quarterly`: `cohort_data_atc3_quarterly/`.

Distribution plots/tables use matching mode-specific directories. Alternative
regression result/log filenames end in `_atc3_<mode>`. Every mode has its own
versioned Stata cache, and mode metadata is checked before estimation. Cache
version `atc3v4treated` prevents reuse of older overlap stacks. Historical cohorts
without mode metadata remain readable only under `event_quarter`.

The earlier `event_year`, `event_year_all`, and `event_year_any` names are no
longer accepted. Use `window_all`/`window_any` and rebuild those cohorts; existing
event-year files are not reinterpreted as full-window data. Archived T2 code,
cohorts, and results under `D:/pharma/rb26` remain unchanged. The Stata templates
retain their existing Kappa configuration.


## Treated-positive cohort requirement

Every retained cohort must contain at least one treated firm-product unit after
all event, REQ, balance and other cohort eligibility filters. If no treated
observations remain, the generator writes a header-only CSV for that cohort;
its controls are excluded as well. This rule applies to all four overlap modes.

Both Stata templates independently recheck treated presence after applying the
window and after loading any cached stack, before pooled P95 winsorization.
The versioned cache name is `atc3v4treated`. Exported `n_cohorts` is recomputed
from distinct retained cohort labels, including when a stack is loaded from cache.
Reestimate the regressions after this selection change because pooled price
thresholds and estimation sample sizes can change.
