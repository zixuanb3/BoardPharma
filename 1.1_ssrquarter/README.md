# Quarterly SSR Workflow

## Membership and regression-sample switches

Every Python entry point accepts `--ever-board 0|1` and `--ieoc-ref 0|1`.
`ever_board=1` expands all usable roles before forward board continuity.
`ieoc_ref=1` requires `ever_board=1`: within a
person-company-year with usable IE/OC coverage, BP labels covered quarters only;
years without usable IE/OC retain all valid BP quarters.

All Python outputs use only these membership policies, so event, balance,
treatment, cohort, and ATC3-sharing definitions do not depend on regression
outlier removal. The Stata entry point exposes `rule_outliers_out` and
`ruleout_threshold`: each listed threshold is run separately. With
`threshold_mode=price10`, each saved cohort uses
`(price1 max/min)/(price0 max/min)`; with `threshold_mode=price1`, it uses only
`price1 max/min`. Both scores are calculated by BoardName-product over that
cohort file's complete observed period, and every row of a pair whose score
exceeds the threshold is removed. A cohort with no treated observations after removal is
not stacked. Each threshold's ruleout results are written below a separate
`r1/threshold_<value>` results directory; cohort inputs always come from the
unfiltered membership path. Price1-mode results add a
`threshold_mode_price1` subdirectory, while the existing price10 paths remain
unchanged. When `outlier_treatment=none`, results are placed
one level lower under `outlier_none` so they cannot be mistaken for or skipped
because of existing winsorized outputs. For the
six-page reference comparison,
`1107/ssr_event_study/_support/run_ever_board.py` runs all downstream configurations,
prepares explicit treated-positive cohort lists, estimates the 96 quarterly
price1 specifications, and reuses the original report layout and annual panels.
Run `build_ssr_roster.py --ever-board 1` before that runner.

This directory contains the dedicated quarterly SSR robustness workflow. It starts from BoardEx board, individual-employment, and organization-composition records; builds two alternative quarterly company rosters; reconstructs quarterly director-movement events; merges those events into SSR firm-product outcomes; and produces balanced cohort files, diagnostics, and `did_imputation` event-study results.

The workflow is separate from the general annual/quarterly programs in `1_data_prep/`, `2_stats/`, and `3_event_study/`. All generated data and results are stored under an `ssrquarter` subtree so that the specification variants remain separate from the main SSR outputs.

## 1. Workflow at a Glance

```text
InterimData/boardex_pharma.dta
InterimData/cro_bname_boardex_*.dta
D:/individual_employment.csv
D:/organization_composition.csv
                    ↓
build_ssr_roster.py
                    ↓
data/ssrquarter/ssr_company_roster_{variant}.csv
                    ↓
RawEventTableMaker.py
                    ↓
movement_event_candidates.csv + firm_interlock_panel.csv
                    ↓
EventTableMaker.py
                    ↓
movement_table.csv with req0, req1, and req2
                    ↓
PanelMaker_FirmLevel.py
                    ↓
quarterly SSR firm-product event panels
                    ↓
CohortPanelMaker.py
                    ↓
balanced req2/Not cohorts with ATC3-sharing indicators
             ┌──────┴──────┐
             ↓             ↓
CohortDistributionPlotter.py   did_imputation_event_study.do
             ↓             ↓
diagnostic CSV/figures      coefficients, figures, and logs
```

## 2. Scripts and Responsibilities

| Script | Role | Principal output |
|---|---|---|
| `build_ssr_roster.py` | Matches the SSR company universe to BoardEx BP, IE, and OC records; applies date-flag rules; consolidates roles; classifies leadership; expands memberships to quarters; and builds the two roster variants. | `data/ssrquarter/ssr_company_roster_imputed_yearquarter.csv` and `ssr_company_roster_drop_sentinel_yearquarter.csv` |
| `RawEventTableMaker.py` | Reads each quarterly roster, completes director membership histories, constructs firm interlocks, and derives `to_B_not_in_A`, `to_B_still_in_A`, and `interlock_dissolution` candidates. | `movement_event_candidates.csv` and `firm_interlock_panel.csv` under the selected specification path |
| `EventTableMaker.py` | Expands each candidate to its A- and B-side company records and constructs firm-quarter `req0`, `req1`, and `req2` eligibility. | `movement_table.csv` |
| `PanelMaker_FirmLevel.py` | Aggregates the SSR source to `BoardName × year × quarter × product × atc3`, merges req2-qualified events, and creates event-quarter, first-event, and T-4 through T+7 balance fields. | `data/ssrquarter/firm_panels/.../quarter-level_{A|B}/ssr_firm_panel_*_req2.csv` |
| `CohortPanelMaker.py` | Builds counterpart-excluded req2/Not cohorts, retains complete firm-product event windows, constructs other-event controls, and classifies ATC3 sharing using eligible event partners. | `data/ssrquarter/cohort_data/.../*_quarter_cohort_YYYYqQ_balanced.csv` |
| `CohortDistributionPlotter.py` | Reads the saved cohorts without rebuilding their sample or sharing definitions and reports firm counts, firm-product counts, and treated ATC3-sharing counts by cohort quarter. | `csv/ssrquarter/cohort_distribution_*`, `csv/ssrquarter/cohort_sharing_atc3`, and matching figure directories |
| `did_imputation_event_study.do` | Stacks the manually listed cohorts, applies the selected outcome transformation and controls, estimates ATC-sharing heterogeneity with `did_imputation`, and exports autosample-adjusted results. | `csv/ssrquarter/`, `figures/ssrquarter/`, and `logs/ssrquarter/` under the full specification tag |

## 3. Specification Structure

Every output path records the choices that determine its sample and event definition.

| Dimension | Values | Meaning |
|---|---|---|
| Roster variant | `imputed_yearquarter`, `drop_sentinel_yearquarter` | Retain the imputed quarterly date treatment or exclude records with the configured sentinel/year-only date flags. |
| Late-entry rule | `late_0`, `late_3` | Allow a `to_B_not_in_A` entry only in the departure quarter or up to three quarters later. |
| req1 rule | `req1_new`, `req1_origin` | Select the current or origin-side upstream requirement-1 construction. |
| Stay rule | `stay_new`, `stay_origin`, `stay_pre2` | For dissolution, `new` waives the stay check. `origin` and `pre2` require the triggering director to serve at both companies in each of the four or eight prior quarters, respectively. Both joining events use eight quarters at the destination, starting in the joining quarter. |
| Sharing-time rule | `share_time_new`, `share_time_origin` | Compare ATC3 portfolios at the event quarter (`new`) or over T-2 through T+1 (`origin`). |
| Treatment side | `A`, `B` | Assign treatment to the origin or destination side of the directional movement event. |
| Event | `to_B_not_in_A`, `to_B_still_in_A`, `interlock_dissolution` | Select the quarterly director-movement event. |

The event table defines `req0=1` for every candidate, `req1=1` when the selected stay condition passes, and `req2=1` when req1 and the treated side's requirement-1 condition both pass. Cohorts use `event/req2/Not`, exclude counterpart-only controls, and require complete T-4 through T+7 firm-product windows.

## 4. Data and Output Layout

```text
data/ssrquarter/
├── ssr_company_roster_imputed_yearquarter.csv
├── ssr_company_roster_drop_sentinel_yearquarter.csv
├── event_tables/
│   └── <variant>/late_<late>/req1_<rule>/stay_<rule>/
│       ├── movement_event_candidates.csv
│       ├── firm_interlock_panel.csv
│       └── movement_table.csv
├── firm_panels/
│   └── <variant>/late_<late>/req1_<rule>/stay_<rule>/
│       └── quarter-level_<A|B>/ssr_firm_panel_<event>_req2.csv
└── cohort_data/
    └── <variant>/late_<late>/req1_<rule>/stay_<rule>/share_time_<rule>/
        └── quarter-level_<side>_without_<other>/event/req2/Not/
            └── <event>_quarter_cohort_<YYYYqQ>_balanced.csv

csv/ssrquarter/       Cohort diagnostics and estimation results
figures/ssrquarter/   Cohort and event-study figures
logs/ssrquarter/      Stata run logs
```

The cohort builders cover 2009Q1 through 2018Q4. A cohort file is omitted when the corresponding upstream event and balance columns do not exist. This workflow does not generate `first_event` cohort files.

## 5. Run Order

Run these scripts in order from VS Code using the settings in each `USER CONFIG` block. The current settings reuse the existing drop-sentinel roster under `ever_board_1/ieoc1` and select `late_3 / req1_new / stay_pre2 / share_time_new`. `-B` prevents Python bytecode and `__pycache__` creation when running from the terminal.

```powershell
python -B 1.1_ssrquarter/RawEventTableMaker.py
python -B 1.1_ssrquarter/EventTableMaker.py
python -B 1.1_ssrquarter/PanelMaker_FirmLevel.py
python -B 1.1_ssrquarter/CohortPanelMaker.py
python -B 1.1_ssrquarter/CohortDistributionPlotter.py
```

`build_ssr_roster.py` requires the BP and SSR name-mapping files under `InterimData/` and, by default, the IE and OC CSV files under `D:/`. Its command-line options can point to alternative input and output roots.

The four middle Python programs process one default configuration unless `--all-configurations` is supplied. To generate `pre2` only, pass `--stay-spec pre2` to each of the four programs in order. The distribution plotter accepts the same option and selects the remaining settings from `RUN_CONFIG`. To select `pre2` in Stata, set `local stay_specs pre2` or pass `pre2` as the fourth do-file argument.

Run `did_imputation_event_study.do` only after the selected cohort files exist and the required Stata packages are installed.

## 6. Reproduction and Change-Control Rules

- The scripts preserve the selected parameter values in their directory paths; do not mix files from different specification paths.
- The Python builders stop when a destination already exists. Remove or relocate an earlier output only when intentionally rebuilding that exact specification.
- `PanelMaker_FirmLevel.py` uses the arithmetic means of the source `price1` and `price0` fields; it does not reconstruct prices from revenue and quantity.
- `CohortPanelMaker.py` reads the current firm panels and movement-event files directly. It does not substitute previously saved cohort files.
- `CohortDistributionPlotter.py` reports the definitions already stored in the cohorts. It does not recalculate treatment, balance, or ATC3-sharing status.
- Stata uses the recorded cohort list when one is provided. For `pre2` without a manual list, it uses saved 2009Q1–2018Q4 files in the selected `stay_pre2` directory and skips cohorts with no treated observations.
