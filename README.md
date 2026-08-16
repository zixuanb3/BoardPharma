# BoardPharma

This repository contains the code for the empirical analysis of director mobility, interfirm board connections, and pharmaceutical-product outcomes. The project comprises two analytical pipelines:

- **SSR** constructs annual and quarterly firm-product panels from SSR data and estimates the effects of director-movement and interlock events on product-market outcomes.
- **Formulary** combines the same BoardEx-derived event framework with expanded formulary data to estimate the effects of these events on drug inclusion, formulary tier, and cost-sharing outcomes.

The two pipelines use distinct outcome data, but both construct event-level data from BoardEx director-board affiliation records and BoardEx firm-pair interlock files.

## Repository Structure

```text
BoardPharma/
├── codes/
│   ├── 1_data_prep/          # Event, panel, and cohort construction
│   ├── 2_stats/              # Descriptive statistics and diagnostic analyses
│   └── 3_event_study/        # Stata estimation and inference programs
├── InterimData/              # Processed input data
├── data/                     # Intermediate data and analysis panels
├── csv/                      # Estimation results and sample summaries
├── figures/                  # Descriptive and event-study figures
├── logs/                     # Stata logs
└── tex/                      # LaTeX tables
```

# SSR Pipeline

## Foundational Input Data

The SSR pipeline uses the following data sources.

| Source | File | Role in the pipeline |
|---|---|---|
| BoardEx | `InterimData/boardex_pharma.dta` | Director-board affiliations used to construct director movement and annual firm-interlock histories. |
| BoardEx | `InterimData/boardex_interlock_direct_firmpair.dta` | Direct interlock firm-pair records. |
| BoardEx | `InterimData/boardex_interlock_indirect_firmpair.dta` | Indirect interlock firm-pair records. |
| SSR | `InterimData/boardex_ssr_price_sample.csv` | Firm-product outcomes and the SSR firm universe used in the main panel construction. |
| SSR | `InterimData/ssr_company_roster.csv` | Company roster used for large-sample event and personnel-panel construction. |

## Event Definitions

`RawEventTableMaker.py` derives event candidates from BoardEx data, and `EventTableMaker.py` converts them into firm-year event eligibility tables. The movement-event definitions are:

- `to_B_not_in_A`: a director joins firm B after leaving firm A;
- `to_B_still_in_A`: a director joins firm B while retaining an affiliation with firm A;
- `interlock_dissolution`: dissolution of a firm-pair interlocking relationship.

The SSR panel workflow also supports `direct_interlock` and `indirect_interlock`. Directional movement events may treat either the origin firm (`A`) or the destination firm (`B`). The standardized event tables contain nested eligibility definitions (`req0`, `req1`, and `req2`) and persistence indicators (`stay_x_years`).

## Construction Workflow

```text
BoardEx affiliation and firm-pair records       SSR firm-product data
                 │                                      │
                 ▼                                      │
      RawEventTableMaker.py                              │
                 │                                      │
                 ▼                                      │
        EventTableMaker.py ──────────────────────────────┤
                 │                                      ▼
                 └──────────────────────► PanelMaker_FirmLevel.py
                                                    │
                           ┌────────────────────────┴───────────────────────┐
                           ▼                                                ▼
                CohortPanelMaker.py                            StaggeredPanelMaker.py
                           │                                                │
                           ▼                                                ▼
             ATC3DistributionPlotter.py                   StaggeredEventStudy.do
                           │
                           ▼
                Event-study and DDD programs
```

| Order | Script | Function | Principal outputs |
|---:|---|---|---|
| 1 | `1_data_prep/RawEventTableMaker.py` | Constructs candidate director-movement, direct-interlock, and indirect-interlock events from BoardEx records; calculates retention and event-requirement indicators. | `data/event_tables/*_event_candidates*.csv`; `firm_interlock_panel*.csv` |
| 2 | `1_data_prep/EventTableMaker.py` | Aggregates candidate events to the firm-year level and produces `req0`, `req1`, and `req2` eligibility measures. | `data/event_tables/movement_table*.csv`; `interlock_table.csv` |
| 3 | `1_data_prep/PanelMaker_FirmLevel.py` | Merges the standardized event tables into annual or quarterly SSR firm-product panels; constructs event, first-event, pure-event, and balance-window variables. | `data/{year,quarter}-level_{A,B}/ssr_firm_panel_*.csv` |
| 4 | `1_data_prep/CohortPanelMaker.py` | Creates stacked cohorts around event timing or first-event timing. Supports `pure_control`, `not_yet`, and `not` controls, directional treatment definitions, and counterpart-firm inclusion rules. | `data/cohort_data/.../*.csv`; cohort-distribution figures |
| 5 | `1_data_prep/StaggeredPanelMaker.py` | Creates staggered-DID samples based on first-event timing. | `data/staggered_data/.../*.csv` |
| 6 | `1_data_prep/ATC3MappingMaker.py` | Builds time-specific ATC peer mappings among SSR firms. | `data/atc3mapping/*.csv` |
| 7 | `2_stats/ATC3DistributionPlotter.py` | Adds ATC-sharing classifications to cohort and staggered samples and produces diagnostic figures. | Enriched sample files and ATC-sharing figures |
| 8 | `1_data_prep/KappaFirmLevelMaker.py` | Constructs firm-level kappa controls for specifications requiring them. | Firm-level kappa files |

## Estimation

| Script | Function | Principal outputs |
|---|---|---|
| `3_event_study/did_imputation_event_study.do` | Estimates dynamic stacked event studies using `did_imputation`, including heterogeneity by ATC3 sharing. | Coefficient files, event-study figures, and logs |
| `3_event_study/ddd_atc3sharing.do` | Estimates interaction and triple-difference specifications for ATC3 sharing. | CSV summaries, LaTeX tables, and logs |
| `3_event_study/ddd_atc3sharing_did_imputation.do` | Estimates ATC3-sharing triple differences using `did_imputation`; incorporates extended controls where specified. | Estimation results and logs |
| `3_event_study/StackedEventStudy_v5.do` | Estimates conventional stacked event studies, with an optional ATC3-sharing split. | Event-study figures and logs |
| `3_event_study/StaggeredEventStudy.do` | Estimates staggered event studies using `csdid`, `did_imputation`, TWFE, and `eventstudyinteract`. | Dynamic-effect figures and logs |
| `3_event_study/EventStudyFigureFormatter.py` | Formats and combines event-study figures generated by the estimation programs. | Formatted figures |

## Randomization Inference

| Script | Function | Principal outputs |
|---|---|---|
| `1_data_prep/MakeFirmPairRandomizationFullCohorts.py` | Constructs complete cohorts for firm-pair randomization assignments. | Firm-pair randomization cohort files |
| `1_data_prep/MakeTreatedFirmRandomizationBalancedPanel.py` | Constructs balanced panels for treated-firm randomization assignments. | Treated-firm randomization panel files |
| `2_stats/FirmPairRandomizationPanelMaker.py` | Produces regression panels for firm-pair randomization inference. | Firm-pair analysis panels |
| `2_stats/TreatedFirmPairRandomizationPanelMaker.py` | Produces regression panels for treated-firm randomization inference. | Treated-firm analysis panels |
| `3_event_study/random_inference.do` | Implements the baseline randomization-inference procedure. | Randomization results and logs |
| `3_event_study/random_inference_firm_pair.do` | Implements firm-pair randomization inference. | Randomization results and logs |
| `3_event_study/random_inference_treated_firm_pair.do` | Implements treated-firm randomization inference. | Randomization results and logs |

## Personnel-Cohort Extension

| Script | Function |
|---|---|
| `1_data_prep/build_personnel_panels.py` | Constructs firm-pair-year movement panels and cohort regression panels from the configured company roster. |
| `1_data_prep/PersonnelCohortQuarterPanelMaker.py` | Constructs personnel-level quarterly cohort panels. |
| `2_stats/PersonnelCohortIdGroupCounter.py` | Reports counts of units and treatment/control groups in personnel cohorts. |
| `3_event_study/personnel_did_imputation_event_study.do` | Estimates personnel-cohort event studies using `did_imputation`. |

# Formulary Pipeline

## Foundational Input Data

The Formulary pipeline combines BoardEx-based event data with expanded formulary, plan, and cost-sharing data.

| Source | File | Role in the pipeline |
|---|---|---|
| BoardEx | `InterimData/boardex_pharma.dta` | Director-board affiliations used to construct movement and firm-interlock histories. |
| BoardEx | `InterimData/boardex_interlock_direct_firmpair.dta` | Direct interlock firm-pair records. |
| BoardEx | `InterimData/boardex_interlock_indirect_firmpair.dta` | Indirect interlock firm-pair records. |
| Company universe | `InterimData/formulary_company_roster.csv` | Formulary-company roster used to restrict BoardEx-derived movement events to the formulary sample. |
| Formulary | `D:/task1_expanded_brand_panel/task1_expanded_brand_panel.csv` | Expanded branded-drug formulary data used to construct the base formulary panel. |
| Plan information | `InterimData/merged_plan_information.csv` | Contract-plan-segment and geographic attributes used in state, insurer, and plan-level panels. |
| Cost sharing | `InterimData/copay_avg_by_plan_tier.csv`; `InterimData/copay_avg_with_prefer.csv` | Plan-level copay and preferred-tier outcomes. |
| CMS directory | `data/directory/Monthly_Report_By_Contract_YYYY_MM.csv` | Parent-organization and geographic mapping inputs. |
| Geographic crosswalks | `crosswalks/pdp_region_state_crosswalk.csv`; `crosswalks/ma_region_state_crosswalk.csv` | State assignment for regional PDP and MA observations. |

The BoardEx inputs are first processed by `RawEventTableMaker.py` and `EventTableMaker.py` with the formulary company roster. The resulting formulary-specific movement event tables are then merged into the expanded formulary data.

## Construction Workflow

```text
BoardEx affiliation and firm-pair records        Expanded formulary data
                 │                                        │
                 ▼                                        │
      RawEventTableMaker.py                                │
                 │                                        │
                 ▼                                        │
        EventTableMaker.py ────────────────────────────────┤
                 │                                        ▼
                 └──────────────────────► FormularyPanelMaker.py
                                                    │
                                                    ▼
                                      ReorganizeFormularyData.py
                                                    │
                    ┌──────────────────────────────┼─────────────────────────────┐
                    ▼                              ▼                             ▼
     FormularyCohortPanelMaker.py  FormularyStateInsurerCohortPanelMaker.py  PlanPanelMaker.py
                    │                              │                             │
                    └──────────────────────────────┴───────────────┬─────────────┘
                                                                    ▼
                                                 Formulary event-study and DDD programs
```

| Order | Script | Function | Principal outputs |
|---:|---|---|---|
| 1 | `1_data_prep/RawEventTableMaker.py` | Constructs BoardEx-derived movement and interlock candidates for firms in the formulary company roster. | Formulary-specific event candidate files in `data/event_tables/` |
| 2 | `1_data_prep/EventTableMaker.py` | Converts formulary-sample candidates into firm-year movement event tables with requirement indicators. | `movement_table_formulary_large_sample_*.csv` |
| 3 | `1_data_prep/FormularyPanelMaker.py` | Processes the expanded formulary data in complete-formulary blocks; merges events, balance flags, tier variables, and ATC1-ATC4-sharing measures; records each NDC's first observed inclusion quarter. | `data/formulary_panel/*.csv`; `data/formulary_metadata/ndc_first_seen*.csv` |
| 4 | `1_data_prep/ReorganizeFormularyData.py` | Reorganizes panel blocks into quarter-specific files for cohort construction and timing-alignment analyses. | `data/formulary_panel_by_time[/shift_q*]/formulary_panel_YYYYQX.csv` |
| 5a | `1_data_prep/FormularyCohortPanelMaker.py` | Aggregates quarterly formulary observations to NDC-firm outcomes and constructs direction-specific event cohorts. | `data/formulary_drug_panel_by_time/...`; `data/formulary_cohort_data/...` |
| 5b | `1_data_prep/FormularyStateInsurerCohortPanelMaker.py` | Aggregates the NDC-firm panel to state, insurer, or state-insurer cells; uses plan and regional crosswalk information for geographic assignment. | `data/formulary_{state,insurer,state_insurer}_cohort_data/...` |
| 5c | `1_data_prep/PlanPanelMaker.py` | Constructs balanced and sampled contract-plan-segment-drug cohorts at plan, state, or county level; combines formulary, tier, and cost-sharing outcomes. | `data/formulary_plan_cohort_data/...` |

## Descriptive Statistics

| Script | Function | Principal outputs |
|---|---|---|
| `2_stats/FormularyPanelStats.py` | Produces descriptive summaries of block-level formulary-panel outcomes. | Summary CSV files and figures |
| `2_stats/FormularyPanelEventStats.py` | Constructs annual Q1 event diagnostics for firms and NDCs by ATC1-ATC4-sharing status. | `csv/formulary_panel_event_stats/`; `figures/formulary_panel_event_stats/` |

## Estimation

| Analysis panel | Dynamic event study | ATC3-sharing triple difference |
|---|---|---|
| NDC-firm, NDC-firm-state, NDC-firm-insurer, or NDC-firm-state-insurer | `3_event_study/formulary_did_imputation_event_study.do` | `3_event_study/formulary_ddd_atc3sharing_did_imputation.do` |
| Contract-plan-drug at plan, state, or county level | `3_event_study/formulary_plan_did_imputation_event_study.do` | `3_event_study/formulary_plan_ddd_atc3sharing_did_imputation.do` |

The Formulary estimation programs stack cohort files across event years, implement direction-specific treatment definitions, classify ATC3 sharing at the cohort-year Q1 observation, and estimate `did_imputation` models with firm-level clustering. Dynamic programs export coefficient files, autosample statistics, figures, and logs. Triple-difference programs export regression results, sample summaries, LaTeX tables, and logs.
