# BoardPharma

This repository contains the code for the empirical analysis of director mobility, interfirm board connections, and pharmaceutical-product outcomes. It implements two linked analytical pipelines:

- **SSR:** annual and quarterly firm-product panels used to estimate the effects of director-movement and interlock events on product-market outcomes.
- **Formulary:** formulary, drug-firm, geographic, and plan-level panels used to estimate the effects of the same events on drug inclusion, tier placement, and cost sharing.

Both pipelines construct event-level data from BoardEx director-board affiliations. They differ in event timing, outcome data, and downstream analysis unit.

## Repository Structure

```text
BoardPharma/
├── codes/
│   ├── 02_code/          Upstream formulary, ATC, expansion, and copay preparation
│   ├── 1_data_prep/       Event, panel, and cohort construction
│   ├── 2_stats/           Descriptive statistics and diagnostics
│   └── 3_event_study/     Stata estimation and inference programs
├── crosswalks/            Labeler, BoardEx, company-name, and component-ID mappings
├── InterimData/           Processed source data
├── data/                  Intermediate data and analysis panels
├── csv/                   Estimation results and sample summaries
├── figures/               Descriptive and event-study figures
├── logs/                  Stata logs
└── tex/                   LaTeX tables
```

# SSR Pipeline

## Foundational Inputs

- `InterimData/boardex_pharma.dta`: BoardEx director-board affiliations.
- `InterimData/boardex_interlock_direct_firmpair.dta`: direct interlock pairs.
- `InterimData/boardex_interlock_indirect_firmpair.dta`: indirect interlock pairs.
- `InterimData/boardex_ssr_price_sample.csv`: SSR firm-product outcome data and firm universe.
- `InterimData/ssr_company_roster.csv`: roster for large-sample event construction and personnel analyses.

## Event Construction

The common event-construction sequence is:

```text
BoardEx affiliation records and firm-pair files
        ↓
RawEventTableMaker.py
        ↓
EventTableMaker.py
        ↓
Firm-year event eligibility tables
```

`RawEventTableMaker.py` derives director-movement, direct-interlock, and indirect-interlock candidates. `EventTableMaker.py` converts these candidates into standardized firm-year eligibility tables with nested `req0`, `req1`, and `req2` definitions.

The principal movement events are `to_B_not_in_A`, `to_B_still_in_A`, and `interlock_dissolution`. For movement events, treatment may be assigned to the origin firm (`A`) or destination firm (`B`). The SSR workflow also supports `direct_interlock` and `indirect_interlock`.

## Panel and Sample Construction

1. `1_data_prep/PanelMaker_FirmLevel.py` merges event eligibility into SSR firm-product data. It creates annual or quarterly event indicators, first-event timing, pure-event indicators, and balanced-window flags. Its principal outputs are the `data/year-level*/` and `data/quarter-level*/` SSR firm panels.

2. `1_data_prep/CohortPanelMaker.py` constructs balanced stacked cohorts around event timing or first-event timing. It supports `pure_control`, `not_yet`, and `not` comparison groups, both treatment directions, and alternative counterpart-firm inclusion rules.

3. `1_data_prep/StaggeredPanelMaker.py` constructs staggered-DID samples from first-event timing and corresponding balance conditions.

4. `1_data_prep/ATC3MappingMaker.py` constructs time-specific mappings of firms within ATC categories. `2_stats/ATC3DistributionPlotter.py` applies these mappings to cohort and staggered samples, adds ATC-sharing classifications, and generates diagnostics.

5. `1_data_prep/KappaFirmLevelMaker.py` constructs the firm-level kappa measures used in specifications with kappa controls.

## Estimation

- `3_event_study/StackedEventStudy_v5.do` estimates conventional stacked event studies, with an optional ATC-sharing split.
- `3_event_study/did_imputation_event_study.do` estimates dynamic stacked event studies using `did_imputation`, including ATC-sharing heterogeneity.
- `3_event_study/ddd_atc3sharing.do` estimates interaction and triple-difference specifications for ATC sharing.
- `3_event_study/ddd_atc3sharing_did_imputation.do` estimates ATC-sharing triple differences using `did_imputation` and optional extended controls.
- `3_event_study/StaggeredEventStudy.do` estimates staggered event studies using `csdid`, `did_imputation`, TWFE, and `eventstudyinteract`.
- `3_event_study/EventStudyFigureFormatter.py` reformats and combines event-study figures.

## Randomization Inference

- `1_data_prep/MakeFirmPairRandomizationFullCohorts.py` creates the complete balanced cohorts used as the fixed base for firm-pair randomization assignments.
- `2_stats/FirmPairRandomizationPanelMaker.py` draws conditional random partners while preserving focal firms, event years, and req0 partner counts; it recomputes req1 and ATC-sharing status.
- `1_data_prep/MakeTreatedFirmRandomizationBalancedPanel.py` builds the balanced base panels for joint treated-firm and firm-pair placebo assignments.
- `2_stats/TreatedFirmPairRandomizationPanelMaker.py` assigns pseudo treated firms and pseudo partners while preserving the observed side-year partner-count distribution.
- `3_event_study/random_inference.do`, `random_inference_firm_pair.do`, and `random_inference_treated_firm_pair.do` implement the corresponding inference procedures.

## Personnel-Cohort Extension

`1_data_prep/build_personnel_panels.py` creates personnel-based firm-pair-year movement and cohort panels. `PersonnelCohortQuarterPanelMaker.py` converts these to product-quarter regression panels. `PersonnelCohortIdGroupCounter.py` reports sample counts, and `personnel_did_imputation_event_study.do` estimates the associated event studies.

# Formulary Pipeline

The current quarterly Formulary workflow uses a standardized integer company `id` from the labeler-company crosswalk. Quarterly BoardEx events, formulary outcomes, ATC3 sharing, plan histories, and estimation all use that identifier. The older annual workflow remains available through the `quarter=0` branches and continues to use `BoardName`.

## Quarterly Workflow at a Glance

```text
CMS formulary + FDA NDC + standardized company crosswalk
        ↓
company-linked non-generic formulary panel
        ↓
RxNav ATC3/ATC4 enrichment
        ↓
complete formulary-quarter × NDC expansion
        ↓
quarterly company roster → quarterly movement events
        ↓
company-id × formulary × NDC quarterly event panel
        ↓
calendar-quarter panel files + NDC first-seen metadata
        ↓
balanced CPS formulary histories + tier copay
        ↓
event-quarter path-by-NDC cohorts
        ↓
quarterly DID and ATC3-sharing DDD estimation
```

The quarterly scripts currently use `formulary_time_shift_quarters=1`. `FormularyPanelMaker.py` shifts the raw formulary quarter forward once and writes the result under `shift_q1`. `PlanPanelMaker.py` applies the same shift to the beneficiary-cost source when it builds the CPS crosswalk and copay inputs. Event columns in the selected formulary panel are already aligned and are not shifted again.

## 1. Company-Linked Formulary and Copay Inputs

The upstream preparation scripts are in `02_code/` and should be run in the following order.

1. `02_code/task1_panel_with_flags.py` reads the CMS basic-drug formulary and FDA product file, converts each NDC to its FDA NDC9 lookup key, and joins `LabelerName` to `id` through `crosswalks/labeler_company_mapping_standardized_with_id.csv`. It keeps mapped non-generic rows, records `tier_raw` and the formulary-quarter `max_tier`, and writes `data/formulary/formulary_panel_with_company_id.csv`.

2. `02_code/atc_all_classes.py` enriches that panel in place. It normalizes NDC11 values, uses the RxNav cache under `D:/pharma`, queries retryable or missing NDCs, and adds `ATC3`, `ATC4`, `n_atc`, and `ATC_status`. The current quarterly analysis uses ATC3; ATC4 is retained in the upstream panel for other uses.

3. `02_code/expand_brand_ndc_panel.py` expands every observed formulary-quarter to the complete NDC set in the company-linked panel. Actual source rows keep their tier and receive `included=1`; added rows receive `included=0`. The script validates formulary-quarter maximum tiers and NDC metadata, writes in disk-backed batches, and produces `D:/pharma/formulary/task1_expanded_brand_panel.csv`.

4. `02_code/export_copay_csv.py` reads the beneficiary-cost source, keeps candidate formularies with `COVERAGE_LEVEL=1`, and calculates daily nonpreferred copay from `COST_TYPE_NONPREF`, the reported amount, or the available minimum-maximum range. It writes `D:/pharma/formulary/beneficiary_cost_with_copay.csv`, which is required by the quarterly plan workflow.

## 2. Quarterly Company Roster and Movement Events

`1_data_prep/BoardexRecordMaker.py` produces the quarterly individual-employment and organization-composition extracts used by the roster builder.

`1_data_prep/FormularyRosterMaker.py` combines three BoardEx sources for 2019-2025. In the standardized mapping mode, BP rows match the crosswalk on exact `BoardName`, while IE and OC rows match on exact `CompanyName`. The crosswalk's integer `id` is the common company identifier. BP observations are expanded from each observed director-company-year to quarters 1-4; IE and OC retain their observed quarters. The script audits ambiguous matches, combines `ie`, `oc`, and `bp` provenance, and writes `data/formulary_roster/formulary_roster_2019_2025.csv` with director, company-id, quarter, country, and source fields.

The quarterly event sequence is:

```text
data/formulary_roster/formulary_roster_2019_2025.csv
        ↓ RawEventTableMaker.py
movement_event_candidates_formulary_quarter_narrow.csv
firm_interlock_panel_formulary_quarter_narrow.csv
        ↓ EventTableMaker.py
movement_table_formulary_quarter_narrow.csv
```

`1_data_prep/RawEventTableMaker.py` runs quarterly mode with `quarter=1`, `formulary=1`, and the narrow personnel definition. It compares adjacent director-quarter memberships, constructs the `to_B_not_in_A`, `to_B_still_in_A`, and `interlock_dissolution` candidates, uses `idA` and `idB` for the directional firms, and evaluates the default two-year persistence rule as `stay_8_quarters`.

`1_data_prep/EventTableMaker.py` converts the candidate file to company-quarter event eligibility. Quarterly mode outputs `id`, `year`, `quarter`, event type, A/B treatment side, `req0`, and `req1`. Quarterly mode does not calculate `req2`; that requirement remains part of the annual workflow.

## 3. Quarterly Formulary Event Panel

`1_data_prep/FormularyPanelMaker.py` combines the expanded formulary with the quarterly event tables. The current configuration uses quarterly events, both A and B treatment directions, `req1`, ATC3 sharing, 30 complete-formulary blocks, and a one-quarter formulary timing shift.

For each block, the script:

1. validates `FORMULARY_ID`, NDC, company `id`, quarter, `max_tier`, and the supplied event fields;
2. records the first shifted quarter in which each NDC has `included=1`;
3. merges events on company `id`, year, and quarter;
4. marks formulary-event-quarter balance over event time -4 through +7;
5. computes direction-specific ATC3-sharing indicators using the event counterpart's eligible NDCs;
6. validates that observed `tier_raw` never exceeds `max_tier` and assigns uncovered rows to `max_tier+1` when the constructed `tierA` outcome is needed; and
7. writes complete-formulary block files without loading the full expanded panel into memory.

With the current settings, the main outputs are:

- `data/formulary_panel_quarter/shift_q1/formulary_panel_1.csv` through `formulary_panel_30.csv`;
- `data/formulary_metadata/ndc_first_seen_quarter_shift_q1.csv`.

`1_data_prep/ReorganizeFormularyData.py` must use the same quarter, block-count, and timing-shift settings. It streams the 30 block files and rewrites them as chronological files under `data/formulary_panel_quarter_by_time/shift_q1/formulary_panel_YYYYQX.csv`. Downstream quarterly statistics and plan cohorts read these time-split files.

## 4. Quarterly Diagnostics

`2_stats/FormularyPanelStats.py` reads the time-split quarterly panel in calendar order. In quarterly mode it reports coverage, event incidence, and ATC3-sharing counts for all three event types and both treatment directions. An event NDC enters the quarterly diagnostic only if its first included quarter is no later than event time -4.

`2_stats/FormularyPanelEventStats.py` selects one representative formulary in each observed target quarter and reports unique event firms, firms with at least one ATC3-sharing event NDC, and sharing/non-sharing event-NDC counts. It saves the selected formulary and source-file manifests together with CSV summaries and figures.

The quarterly diagnostic outputs are written under:

- `csv/formulary_panel_stats/quarter/shift_q1/` and `figures/formulary_panel_stats/quarter/shift_q1/`;
- `csv/formulary_panel_event_stats/quarter/shift_q1/` and `figures/formulary_panel_event_stats/quarter/shift_q1/`.

## 5. Quarterly Plan-Path Cohorts

`1_data_prep/PlanPanelMaker.py` is the active quarterly cohort builder. Quarterly mode is fixed at the CPS level, where one analysis unit is a `CONTRACT_ID × PLAN_ID × SEGMENT_ID`. State and county repetitions in the beneficiary-cost source do not create additional path weight.

The current configuration uses `quarter=1`, `level="plan"`, `formulary_time_shift_quarters=1`, `path_weighted_mode=1`, and `prefer=0`. Quarterly copay is always read from `D:/pharma/formulary/beneficiary_cost_with_copay.csv`; `prefer=1` is optional and uses `D:/pharma/copay_avg_with_prefer.csv`.

For each observed req1 event quarter from 2020 through 2024, the script:

1. builds the event window from quarter -4 through quarter +7 and rejects missing internal quarter files;
2. creates a shifted CPS-quarter-formulary crosswalk and averages daily copay to a unique CPS-quarter-tier value;
3. keeps CPS units with plan and formulary coverage in every required quarter;
4. collapses identical complete formulary histories into `history_id` and records the number of represented plans in `n_path`;
5. calculates outcome-specific `n_path_copay` and, when enabled, `n_path_prefer` weights;
6. expands each history across eligible NDCs, merges inclusion, tier, ATC3-sharing, and copay outcomes, and applies the req1/Not treated-control rules for both A and B; and
7. writes one path-by-NDC file for each event-quarter cohort, with a checkpoint that permits an interrupted build to resume from the last completed quarter.

Quarterly path cohorts are written to:

`data/formulary_path_cohort_data_quarter/event/req1/Not/shift_q1/plan/{event}_path_quarter_cohort_YYYYQX.csv`

The NDC first-seen field is carried into these files. The first row of a cohort history has no within-cohort predecessor; the builder does not import a pre-cohort formulary merely to fill that value.

## 6. Quarterly Estimation

`3_event_study/formulary_path_did_imputation_event_study.do` estimates dynamic path-weighted effects. `3_event_study/formulary_path_ddd_atc3sharing_did_imputation.do` estimates the corresponding ATC3-sharing triple differences.

The current quarterly specifications:

- estimate `included`, `tier_raw`, and `avg_copay_amt`;
- run both A- and B-side treatment definitions;
- use plan-level quarterly path cohorts with `shift_q1`;
- sample 10 percent of distinct `history_id` paths within each cohort using the configured seed;
- use company `id` for event classification, firm counts, other-event histories, and clustering; and
- retain only event quarters with at least one ATC3-sharing event NDC.

The retained quarterly cohorts are `2020Q1`, `2022Q1`, and `2024Q1` for `to_B_still_in_A`; `2024Q1` for `to_B_not_in_A`; and `2021Q3`, `2022Q3`, `2024Q3`, and `2024Q4` for `interlock_dissolution`.

The dynamic program exports event-time coefficients, sample counts, figures, and logs. The DDD program exports sharing and non-sharing effects, comparison statistics, tables, sample summaries, and logs.

## Legacy Annual Formulary Workflow

The annual branch remains available for older analyses. It uses `BoardName`, supports `req0`, `req1`, and `req2`, can calculate ATC1-ATC4 sharing, and writes block files under `data/formulary_panel/`. `FormularyCohortPanelMaker.py` and `FormularyStateInsurerCohortPanelMaker.py` continue to build NDC-firm, state, insurer, and state-insurer cohorts. Annual `PlanPanelMaker.py` retains plan, state, and county options and the older plan-information and copay inputs. The annual Stata programs remain separate from the quarterly path specifications described above.

## Detailed Script Reference

For script-level descriptions of inputs, transformations, and outputs, see [README_codes.md](README_codes.md).
