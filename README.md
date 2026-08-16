# BoardPharma

This repository contains the code for the empirical analysis of director mobility, interfirm board connections, and pharmaceutical-product outcomes. It implements two linked analytical pipelines:

- **SSR:** annual and quarterly firm-product panels used to estimate the effects of director-movement and interlock events on product-market outcomes.
- **Formulary:** formulary, drug-firm, geographic, and plan-level panels used to estimate the effects of the same events on drug inclusion, tier placement, and cost sharing.

Both pipelines construct event-level data from BoardEx director-board affiliations and BoardEx firm-pair interlock records. They differ only in the outcome data and downstream analysis unit.

## Repository Structure

```text
BoardPharma/
├── codes/
│   ├── 1_data_prep/       Event, panel, and cohort construction
│   ├── 2_stats/           Descriptive statistics and diagnostics
│   └── 3_event_study/     Stata estimation and inference programs
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

## Foundational Inputs

### BoardEx Event Inputs

- `InterimData/boardex_pharma.dta`: BoardEx director-board affiliations.
- `InterimData/boardex_interlock_direct_firmpair.dta`: direct interlock pairs.
- `InterimData/boardex_interlock_indirect_firmpair.dta`: indirect interlock pairs.
- `InterimData/formulary_company_roster.csv`: company universe used to restrict BoardEx-derived event construction to the formulary sample.

### Formulary, Plan, and Geographic Inputs

- `D:/task1_expanded_brand_panel/task1_expanded_brand_panel.csv`: expanded branded-drug formulary source.
- `InterimData/merged_plan_information.csv`: contract, plan, segment, and geographic characteristics.
- `InterimData/copay_avg_by_plan_tier.csv` and `InterimData/copay_avg_with_prefer.csv`: plan-level cost-sharing outcomes.
- `data/directory/Monthly_Report_By_Contract_YYYY_MM.csv`: CMS contract-directory inputs.
- `crosswalks/pdp_region_state_crosswalk.csv` and `crosswalks/ma_region_state_crosswalk.csv`: region-to-state mappings.

## Event Construction and Formulary Panel

The Formulary pipeline uses `RawEventTableMaker.py` and `EventTableMaker.py` with the formulary company roster. The resulting `movement_event_candidates_formulary_*` and `movement_table_formulary_*` files retain BoardEx-derived event timing, event requirements, and directional firm-pair information.

The core construction sequence is:

```text
BoardEx event tables + expanded formulary data
        ↓
FormularyPanelMaker.py
        ↓
ReorganizeFormularyData.py
        ↓
Drug-firm, geographic, and plan-level cohort builders
```

1. `1_data_prep/FormularyPanelMaker.py` processes the expanded formulary data in complete-formulary blocks. It merges event and balance variables, constructs tier and ATC1-ATC4-sharing measures, and records the first quarter in which each NDC is included. It writes `data/formulary_panel/` block files and `data/formulary_metadata/ndc_first_seen*.csv`.

2. `1_data_prep/ReorganizeFormularyData.py` rewrites the block-level panel into quarter-specific `formulary_panel_YYYYQX.csv` files. It supports separate paths for alternative timing-alignment specifications.

## Cohort Construction

1. `1_data_prep/FormularyCohortPanelMaker.py` aggregates quarterly observations to NDC-firm outcomes. It constructs inclusion counts and shares, mean tier measures, balanced event cohorts, and direction-specific treatment, sample, and ATC3-sharing indicators.

2. `1_data_prep/FormularyStateInsurerCohortPanelMaker.py` extends the NDC-firm cohort design to state, CMS Parent Organization, and joint state-insurer cells. It uses plan information, CMS directory data, and regional crosswalks for geographic assignment.

3. `1_data_prep/PlanPanelMaker.py` constructs balanced and reproducibly sampled contract-plan-segment-drug cohorts at plan, state, and county level. It combines formulary outcomes with tier transitions and cost-sharing measures.

NDC eligibility is defined by the global first quarter in which `included=1`. Eligible NDCs retain their complete cohort histories under the selected timing-alignment specification.

## Descriptive Statistics

- `2_stats/FormularyPanelStats.py` produces block-level coverage, event-incidence, and event-by-ATC-sharing summaries.
- `2_stats/FormularyPanelEventStats.py` constructs annual Q1 diagnostics for event firms and NDCs by ATC1-ATC4-sharing status.

## Estimation

- `3_event_study/formulary_did_imputation_event_study.do` estimates dynamic `did_imputation` models for NDC-firm, state, insurer, and state-insurer panels.
- `3_event_study/formulary_ddd_atc3sharing_did_imputation.do` estimates ATC3-sharing triple differences for those panels.
- `3_event_study/formulary_plan_did_imputation_event_study.do` estimates dynamic models for contract-plan-drug cohorts at plan, state, and county level.
- `3_event_study/formulary_plan_ddd_atc3sharing_did_imputation.do` estimates plan-level ATC3-sharing triple differences.

The Formulary estimation programs stack event-year cohorts, implement direction-specific treatment definitions, classify ATC3 sharing at cohort-year Q1, apply the relevant NDC first-seen eligibility rule, and estimate `did_imputation` models with firm-level clustering. Dynamic programs export coefficients, autosample statistics, figures, and logs. Triple-difference programs additionally export result tables and sample summaries.

## Detailed Script Reference

For script-level descriptions of inputs, transformations, and outputs, see [README_codes.md](README_codes.md).
