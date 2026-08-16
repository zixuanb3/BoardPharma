# BoardPharma Code Reference

This document provides a script-level reference for the current BoardPharma codebase. The repository is organized around a common BoardEx event-construction layer and two outcome-data pipelines: SSR firm-product analyses and Formulary analyses.

## Directory Conventions

| Directory | Contents |
|---|---|
| `1_data_prep/` | Python programs that construct event tables, analysis panels, and cohort samples. |
| `2_stats/` | Python programs that create descriptive statistics, diagnostics, and randomization-inference analysis panels. |
| `3_event_study/` | Stata programs for event-study estimation, triple differences, and randomization inference. |
| `InterimData/` | Processed source data, including BoardEx, SSR, roster, plan, and cost-sharing inputs. |
| `data/` | Event tables, intermediate panels, cohort files, and analysis-ready datasets. |
| `csv/`, `figures/`, `logs/`, `tex/` | Tabular results, figures, Stata logs, and LaTeX output, respectively. |

# I. Common BoardEx Event Construction

Both the SSR and Formulary pipelines use BoardEx data to create the event-level inputs. The distinction between the pipelines arises only after event construction: SSR merges the event tables into firm-product outcomes, whereas Formulary merges formulary-specific event tables into formulary observations.

## I.1 Source Files

| Input | Use |
|---|---|
| `InterimData/boardex_pharma.dta` | Director-board affiliation histories used to identify director movements and construct annual firm-interlock histories. |
| `InterimData/boardex_interlock_direct_firmpair.dta` | Direct interlock firm-pair data. |
| `InterimData/boardex_interlock_indirect_firmpair.dta` | Indirect interlock firm-pair data. |
| `InterimData/ssr_company_roster.csv` | SSR company universe for large-sample event construction and personnel analyses. |
| `InterimData/formulary_company_roster.csv` | Formulary company universe used when producing formulary-specific movement-event inputs. |
| `InterimData/boardex_ssr_price_sample.csv` | SSR firm-product data; also used to restrict direct and indirect interlock pairs to firms in the SSR sample. |

## I.2 Event Definitions and Conventions

`RawEventTableMaker.py` derives the following event types:

| Event type | Definition |
|---|---|
| `to_B_not_in_A` | A director joins firm B after leaving firm A. |
| `to_B_still_in_A` | A director joins firm B while retaining an affiliation with firm A. |
| `interlock_dissolution` | A directional dissolution of a firm-pair interlocking relationship. |
| `direct_interlock` | A direct interlocking relationship between two firms. |
| `indirect_interlock` | An indirect interlocking relationship between two firms. |

For movement events, `treatment_group = A` treats the origin firm and `treatment_group = B` treats the destination firm. `req0`, `req1`, and `req2` represent nested event-eligibility definitions. The `stay_x_years` variable records the persistence requirement used in the corresponding event definition.

## I.3 Scripts

### `1_data_prep/RawEventTableMaker.py`

**Purpose.** Constructs the raw candidate tables for movement, direct-interlock, and indirect-interlock events.

**Processing.**

1. Reads BoardEx director-board affiliations and constructs director-year-board histories.
2. Identifies A-to-B director movements and creates the `to_B_not_in_A`, `to_B_still_in_A`, and `interlock_dissolution` candidate events.
3. Derives annual firm-interlock edges from director affiliations.
4. Calculates independent `stay`, `requirement1`, and `requirement2` measures for movement candidates.
5. Reads the direct and indirect BoardEx firm-pair files, restricts them to the SSR firm universe, and appends the corresponding event-requirement measures.
6. Produces separate large-sample and formulary-roster versions of movement candidates when those modes are selected.

**Outputs.**

- `data/event_tables/movement_event_candidates.csv`;
- `data/event_tables/movement_event_candidates_large_sample_{definition}.csv`;
- `data/event_tables/movement_event_candidates_formulary_large_sample_{definition}.csv`;
- `data/event_tables/interlock_event_candidates.csv`;
- `data/event_tables/firm_interlock_panel*.csv`.

### `1_data_prep/EventTableMaker.py`

**Purpose.** Standardizes raw event candidates into firm-year event-eligibility tables used by downstream panel builders.

**Processing.**

1. Reads movement and interlock candidate tables from `data/event_tables/`.
2. Converts candidate observations into firm-year event records.
3. Maps the raw event conditions to nested `req0`, `req1`, and `req2` indicators.
4. Collapses duplicate firm-year-event rows using the maximum eligibility value.

**Outputs.**

- `data/event_tables/movement_table.csv`;
- `data/event_tables/movement_table_large_sample_{definition}.csv`;
- `data/event_tables/movement_table_formulary_large_sample_{definition}.csv`;
- `data/event_tables/interlock_table.csv`.

# II. SSR Pipeline

The SSR pipeline merges the BoardEx-derived event tables with SSR firm-product outcomes, constructs stacked or staggered samples, and estimates event-study and triple-difference specifications.

## II.1 Primary Inputs

| Input | Use |
|---|---|
| `InterimData/boardex_ssr_price_sample.csv` | Annual and quarterly SSR firm-product outcomes, including the firm and product identifiers used to define the analysis unit. |
| `data/event_tables/movement_table*.csv` | Directional firm-year movement-event eligibility measures. |
| `data/event_tables/interlock_table.csv` | Firm-year direct- and indirect-interlock eligibility measures. |
| `data/event_tables/movement_event_candidates*.csv` | Candidate partner information used when cohort construction excludes counterpart-only observations. |
| `InterimData/ssr_kappa_pairwise_v5.csv` and `InterimData/ssr_kappa_firm_level_v5.csv` | Pairwise and firm-level kappa inputs for kappa-control specifications. |

## II.2 Panel Construction

### `1_data_prep/PanelMaker_FirmLevel.py`

**Purpose.** Constructs annual or quarterly SSR firm-product event-study panels.

**Processing.**

1. Reads the SSR firm-product source panel and the standardized movement/interlock event tables.
2. Builds one panel for each selected event type and treatment direction.
3. Places annual events and persistence indicators in Q1 when the panel is quarterly.
4. Preserves `pure_event` from the unfiltered event table and separately applies the selected requirement level to the analysis event indicator.
5. Creates `event_YYYY`, `first_event_year`, first-event indicators, and event-specific balanced-window indicators.
6. Writes movement panels separately for A- and B-side treatment definitions; interlock panels are written to the non-directional directory.

**Outputs.**

- `data/year-level_{A,B}/ssr_firm_panel_*.csv`;
- `data/quarter-level_{A,B}/ssr_firm_panel_*.csv`;
- `data/year-level/ssr_firm_panel_*.csv` and `data/quarter-level/ssr_firm_panel_*.csv` for interlock events.

### `1_data_prep/KappaFirmLevelMaker.py`

**Purpose.** Extends the normalized firm-level kappa file with raw pairwise kappa moments.

**Processing.** Aggregates raw pairwise `kappa` by reporting date and firm, validates the one-to-one merge with the normalized firm-level file, derives year and quarter, and appends mean, median, and standard-deviation measures.

**Output.** `data/kappa/ssr_kappa_firm_level_v5.csv`.

### `1_data_prep/ATC3MappingMaker.py`

**Purpose.** Creates time-specific firm-pair mappings within ATC categories.

**Processing.** Forms `BoardName`–`BoardNamePair` relationships within each ATC group and period, excludes self-pairs, and removes duplicate mappings.

**Output.** `data/atc3mapping/atc3mapping_{time_level}*.csv`.

## II.3 Cohort and Staggered Sample Construction

### `1_data_prep/CohortPanelMaker.py`

**Purpose.** Constructs balanced, cohort-stacked SSR samples for event-study designs.

**Processing.**

1. Reads event-specific annual or quarterly SSR panels.
2. Defines cohorts using either the event-year indicator or first-event timing.
3. Applies event-specific balanced-window indicators created upstream.
4. Constructs the requested control sample: `pure_control`, `not_yet`, or `not`.
5. Applies the selected A/B treatment definition and, for movement events, controls whether counterpart-firm-only observations remain in the cohort traversal.
6. Produces cohort-count diagnostics for each configured specification.

**Outputs.**

- `data/cohort_data/{year|quarter}-level_.../{event_type}/{control_folder}/*.csv`;
- `figures/cohort_distribution/{year|quarter}-level_.../{event_type}/{control_folder}/*.png`.

### `1_data_prep/StaggeredPanelMaker.py`

**Purpose.** Constructs staggered-DID SSR samples based on first-event timing.

**Processing.** Selects `not_yet` or `pure_control` comparison observations, applies first-event balance conditions, and retains complete event windows when the balanced-panel option is active.

**Outputs.**

- `data/staggered_data/year-level/staggered_firm_level_panel_*.csv`;
- `data/staggered_data/quarter-level/staggered_firm_level_panel_*.csv`.

### `2_stats/ATC3DistributionPlotter.py`

**Purpose.** Adds ATC-sharing classifications to SSR cohort and staggered samples and reports their distribution over relative event time.

**Processing.** Matches treated firms to event partners using directional event information and ATC mappings, assigns `atc3_sharing` (or the configured ATC level), writes enriched samples, and creates diagnostic figures by event time and treatment direction.

**Outputs.** Enriched cohort/staggered data under `data/*_with_atc3sharing/` and diagnostic figures under `figures/`.

## II.4 Estimation

### `3_event_study/StackedEventStudy_v5.do`

**Purpose.** Estimates conventional stacked event-study specifications.

**Processing.** Runs the configured estimators over stacked SSR cohorts and switches between pooled and ATC-sharing-split inputs through the `atc3sharing` option. Output paths are separated by estimation mode.

**Outputs.** Event-study figures and logs under `figures/stacked_event_study*/` and `logs/stacked_event_study*/`.

### `3_event_study/did_imputation_event_study.do`

**Purpose.** Estimates dynamic stacked event studies using `did_imputation`.

**Processing.** Stacks ATC-enriched cohort files over event years; applies the selected outcome transformation, fixed effects, controls, and clustering; estimates ATC-sharing heterogeneity either with `hetby()` or separate sharing/non-sharing runs; and writes autosample-based coefficient and sample statistics.

**Outputs.** Figures, coefficient CSV files, and logs under `figures/didimp_es_*/`, `csv/didimp_es_*/`, and `logs/didimp_es_*/`.

### `3_event_study/ddd_atc3sharing.do`

**Purpose.** Estimates stacked triple-difference specifications for ATC sharing.

**Processing.** Estimates post-event treatment effects for non-sharing products and the incremental post-event effect for sharing products. The program also produces an associated `did_imputation` heterogeneity contrast and reports pre-treatment means, percent effects, and sample counts.

**Outputs.** Regression tables, CSV results, and logs under `tex/ddd_atcsharing_*/`, `csv/ddd_atcsharing_*/`, and `logs/ddd_atcsharing_*/`.

### `3_event_study/ddd_atc3sharing_did_imputation.do`

**Purpose.** Estimates ATC-sharing triple differences using `did_imputation`.

**Processing.** Uses `hetby(atc_sharing)` when no exposure term is specified and the `project()` interface when exposure variables are included. It constructs reported sharing and non-sharing effects from autosample-adjusted estimates.

**Outputs.** Estimation tables, CSV files, and logs under the `ddd_atcsh_didimp_*` output hierarchy.

### `3_event_study/StaggeredEventStudy.do`

**Purpose.** Estimates staggered event studies and compares alternative estimators.

**Processing.** Uses staggered SSR samples to run `csdid`, `did_imputation`, TWFE, and `eventstudyinteract`, then aligns the dynamic coefficients for reporting.

**Outputs.** Figures and logs under the staggered-event-study output hierarchy.

### `3_event_study/EventStudyFigureFormatter.py`

**Purpose.** Reformats and combines event-study figures produced by the estimation scripts.

## II.5 Randomization Inference

### `1_data_prep/MakeFirmPairRandomizationFullCohorts.py`

**Purpose.** Creates the full, balanced, `include_eventpair=1` cohorts used as the fixed starting point for firm-pair randomization inference.

**Design.** Reuses the cohort-selection logic for the SSR `to_B_still_in_A`, req1, Not-control design and writes one balanced cohort for each treatment side and cohort year.

### `2_stats/FirmPairRandomizationPanelMaker.py`

**Purpose.** Creates side-specific Stata panels for conditional firm-pair randomization.

**Design.** Holds focal firms, years, and req0 partner counts fixed; draws random partners from the SSR roster; recomputes req1, ATC3 sharing, and counterpart-only-control status; and preserves observed and simulated indicators in a common Stata panel.

**Outputs.** `data/random_inference_firm_pair/.../firm_pair_randomization_{A|B}.dta` and randomization diagnostics.

### `1_data_prep/MakeTreatedFirmRandomizationBalancedPanel.py`

**Purpose.** Creates side-specific balanced stacked base panels for joint treated-firm and firm-pair randomization inference.

**Design.** Retains every complete firm-product event window that could enter a cohort before pseudo-event assignment, then attaches the other-event timing controls used by the observed specification.

**Outputs.** `data/random_inference_treated_firm_pair/.../balanced_base_{A|B}.dta` and cohort diagnostics.

### `2_stats/TreatedFirmPairRandomizationPanelMaker.py`

**Purpose.** Creates wide Stata panels for joint placebo assignment of treated firms and partners.

**Design.** Assigns pseudo pure events over the full SSR firm roster, preserves the observed side-year distribution of req0 and req1 partner counts, draws req1-valid and req1-invalid partners, and writes replication-specific sample-state and sharing indicators.

**Outputs.** `data/random_inference_treated_firm_pair/.../treated_firm_pair_randomization_{A|B}.dta`, pseudo-event schedules, partner assignments, and diagnostics.

### `3_event_study/random_inference.do`

**Purpose.** Runs baseline randomization inference for stacked ATC3 `did_imputation` DDD specifications by reassigning treatment states within cohort-specific stacked identifiers.

### `3_event_study/random_inference_firm_pair.do`

**Purpose.** Runs randomization inference using the simulated firm-pair panels.

### `3_event_study/random_inference_treated_firm_pair.do`

**Purpose.** Runs randomization inference using joint treated-firm and firm-pair placebo assignments.

## II.6 Personnel-Cohort Extension

### `1_data_prep/build_personnel_panels.py`

**Purpose.** Constructs personnel-based firm-pair-year movement and cohort panels from the selected company roster.

**Processing.** Filters the roster by personnel definition, builds director-year-board assignments, derives movement events and retention counters, constructs control sets, and writes cohort regression panels.

**Outputs.** `data/personnel_panels/{definition}/...`; formulary-specific outputs are written under `data/personnel_panels/formulary/{definition}/...`.

### `1_data_prep/FormularyPanelCacheMaker.py`

**Purpose.** Creates a reusable Parquet cache of the large raw formulary panel for personnel-cohort analyses.

**Processing.** Streams the raw formulary CSV, retains observations with a mapped `BoardName`, derives time and product keys, and writes a compact Parquet file.

**Output.** `InterimData/task1_final_panel_with_atc_boardname.parquet`.

### `1_data_prep/PersonnelCohortQuarterPanelMaker.py`

**Purpose.** Expands personnel cohort pair-year panels to firm-pair-product-quarter regression panels.

**Processing.** Expands annual cohort records to quarters, merges SSR outcomes or cached formulary outcomes, attaches directional ATC-sharing measures, kappa, and pair-balance indicators, and writes one regression panel per cohort-year specification.

### `2_stats/PersonnelCohortIdGroupCounter.py`

**Purpose.** Counts distinct regression-panel identifiers by treatment/confounding and ATC-sharing group for personnel cohorts.

**Outputs.** `csv/personnel_cohort_id_counts/personnel_cohort_id_group_counts.csv`, with a separate formulary output hierarchy when applicable.

### `3_event_study/personnel_did_imputation_event_study.do`

**Purpose.** Estimates personnel-cohort `did_imputation` event studies on firm-pair-product-quarter panels.

**Processing.** Stacks available cohort years, constructs treatment/confounding group indicators, estimates heterogeneous effects with `hetby(group4)`, and exports group-by-event-time coefficients and figures.

# III. Formulary Pipeline

The Formulary pipeline uses BoardEx-derived movement-event tables for the formulary company universe, then merges these event inputs with expanded branded-drug formulary data.

## III.1 Primary Inputs

| Input | Use |
|---|---|
| `InterimData/boardex_pharma.dta` | BoardEx affiliation histories used to derive formulary-sample movement and interlock events. |
| `InterimData/boardex_interlock_direct_firmpair.dta` and `boardex_interlock_indirect_firmpair.dta` | BoardEx direct and indirect interlock records. |
| `InterimData/formulary_company_roster.csv` | Firm universe used to construct formulary-specific event candidates and movement tables. |
| `D:/task1_expanded_brand_panel/task1_expanded_brand_panel.csv` | Expanded branded-drug formulary source used in the principal formulary panel construction. |
| `InterimData/merged_plan_information.csv` | Contract, plan, segment, and geographic information for state, insurer, and plan-level analyses. |
| `InterimData/copay_avg_by_plan_tier.csv` and `InterimData/copay_avg_with_prefer.csv` | Plan-level cost-sharing and preferred-tier outcomes. |
| `data/directory/Monthly_Report_By_Contract_YYYY_MM.csv` | CMS contract-directory inputs for insurer and geography assignment. |
| `crosswalks/pdp_region_state_crosswalk.csv` and `crosswalks/ma_region_state_crosswalk.csv` | PDP and MA region-to-state mappings. |

## III.2 Event Inputs for Formulary Analyses

Formulary event construction uses the common `RawEventTableMaker.py` and `EventTableMaker.py` sequence with the formulary company roster enabled. The resulting inputs are:

- `data/event_tables/movement_event_candidates_formulary_large_sample_{definition}.csv`;
- `data/event_tables/movement_table_formulary_large_sample_{definition}.csv`.

These files retain the BoardEx-derived event timing, event requirements, and firm-pair information used by all downstream formulary cohort builders.

## III.3 Formulary Panel Construction

### `1_data_prep/FormularyPanelMaker.py`

**Purpose.** Constructs event-enriched formulary panels at the `FORMULARY_ID × BoardName × NDC × quarter` level without loading the complete expanded source into memory.

**Processing.**

1. Splits complete formularies into disk-backed blocks and records each NDC's first quarter with `included=1` during the routing pass.
2. Merges formulary-specific movement events into each block.
3. Creates quarterly event and balance indicators, annual-event placement in Q1, tier outcomes, and direction-specific ATC1-ATC4-sharing measures.
4. Writes each completed block and removes its temporary staging file.

**Outputs.**

- `data/formulary_panel/formulary_panel_*.csv`;
- `data/formulary_metadata/ndc_first_seen*.csv`.

### `1_data_prep/ReorganizeFormularyData.py`

**Purpose.** Reorganizes formulary panel blocks into quarter-specific CSV files.

**Processing.** Reads the block-level output, groups observations by `YEAR_Q`, and writes one file per quarter. The script supports separate output directories for alternative quarterly timing alignments.

**Outputs.** `data/formulary_panel_by_time[/shift_q*]/formulary_panel_YYYYQX.csv`.

## III.4 Formulary Cohort Construction

### `1_data_prep/FormularyCohortPanelMaker.py`

**Purpose.** Builds NDC-firm-quarter cohorts for formulary event-study analyses.

**Processing.**

1. Streams the quarter-organized formulary files and aggregates to `NDC × BoardName × YEAR_Q`.
2. Constructs `included_count`, `included_share`, `mean_tiera`, and `mean_tier_raw`.
3. Retains req1 movement-event and direction-specific ATC3-sharing information.
4. Applies the NDC first-seen eligibility criterion and complete-quarter balance requirement.
5. Reproduces the SSR Not-control design and the directional counterpart-pair exclusion rule.
6. Writes a combined A/B cohort file for each event type and cohort year, with direction-specific treatment, sample, and sharing flags.

**Outputs.**

- `data/formulary_drug_panel_by_time[/shift_q*]/formulary_drug_panel_YYYYQX.csv`;
- `data/formulary_cohort_data/event/req1/Not/.../{event}_quarter_cohort_{year}.csv`.

### `1_data_prep/FormularyStateInsurerCohortPanelMaker.py`

**Purpose.** Extends formulary cohort construction to NDC-firm-state, NDC-firm-insurer, and NDC-firm-state-insurer cells.

**Processing.**

1. Maps formulary observations to state and/or CMS Parent Organization using plan information, CMS directory files, and region-to-state crosswalks.
2. Expands blank-state PDP and MA regional observations where the selected dimension requires a state assignment.
3. Aggregates outcomes within the selected analysis cell and quarter.
4. Retains global NDC first-seen timing, direction-specific treatment status, and the precomputed ATC3-sharing measure while constructing balanced cohorts.

**Outputs.**

- `data/formulary_{dimension}_crosswalk_by_time/`;
- `data/formulary_drug_{dimension}_panel_by_time/`;
- `data/formulary_{dimension}_cohort_data/event/req1/Not/...`.

### `1_data_prep/PlanPanelMaker.py`

**Purpose.** Builds regression-ready contract-plan-segment-drug cohort panels.

**Processing.**

1. Normalizes and deduplicates plan information at the selected plan, state, or county geography level.
2. Identifies plan units observed in every quarter of each cohort window and draws reproducible cohort-specific samples of balanced plans.
3. Merges the sampled plan units with quarterly formulary outcomes, tier transitions, copay measures, NDC first-seen metadata, and event measures.
4. Applies req1 Not-control sample rules and writes one file per event-year cohort.

**Outputs.** `data/formulary_plan_cohort_data/event/req1/Not/{shift}/{level}/{event}_plan_quarter_cohort_{year}.csv`.

## III.5 Formulary Descriptive Statistics

### `2_stats/FormularyPanelStats.py`

**Purpose.** Produces coverage, event-incidence, and event-by-ATC-sharing summaries from the block-level formulary panels.

**Processing.** Streams only required variables from each block, deduplicates formulary-quarter and treated formulary-NDC observations, restricts event NDCs by first-seen eligibility, validates sharing partitions, and produces tables and bar charts.

**Outputs.** `csv/formulary_panel_stats/{coverage,event,share}/` and `figures/formulary_panel_stats/{coverage,event,share}/`.

### `2_stats/FormularyPanelEventStats.py`

**Purpose.** Creates annual Q1 event diagnostics using a representative formulary for each year.

**Processing.** Selects one Q1 formulary per year, counts unique event firms and NDCs by ATC1-ATC4-sharing status, and restricts NDC event counts using first-seen eligibility.

**Outputs.** `csv/formulary_panel_event_stats/` and `figures/formulary_panel_event_stats/`.

## III.6 Formulary Estimation

### `3_event_study/formulary_did_imputation_event_study.do`

**Purpose.** Estimates dynamic Formulary event studies using `did_imputation` for NDC-firm, state, insurer, and state-insurer panels.

**Processing.** Stacks cohort files by event year, selects the A- or B-side sample, applies the NDC first-seen cutoff, fixes ATC3 sharing at cohort-year Q1, constructs the event-time design, estimates heterogeneous effects with `hetby(atc_sharing)`, and exports dynamic coefficients and autosample statistics.

**Outputs.** `csv/formulary/es/.../dynamic.csv`, `samples.csv`, event-study figures, and run logs.

### `3_event_study/formulary_ddd_atc3sharing_did_imputation.do`

**Purpose.** Estimates Formulary ATC3-sharing triple differences for NDC-firm, state, insurer, and state-insurer analyses.

**Processing.** Applies the same cohort stacking, first-seen eligibility, and Q1 sharing classification as the dynamic program, then reports non-sharing effects, sharing effects, their contrast, pre-treatment means, and autosample statistics.

**Outputs.** Result CSV files, pre-/post-autosample summaries, LaTeX tables, and logs under `csv/formulary/ddd/`, `tex/formulary/ddd/`, and `logs/formulary/ddd/`.

### `3_event_study/formulary_plan_did_imputation_event_study.do`

**Purpose.** Estimates dynamic `did_imputation` event studies for contract-plan-drug cohorts.

**Processing.** Reads plan-, plan-state-, or plan-county-level cohorts; constructs level-specific unit identifiers and fixed effects; fixes ATC3 sharing at the cohort-year Q1; winsorizes average copay at the specified upper percentile; and exports dynamic coefficients, figures, and autosample statistics.

**Outputs.** `csv/formulary_plan/es/.../dynamic.csv`, `samples.csv`, figures, and logs.

### `3_event_study/formulary_plan_ddd_atc3sharing_did_imputation.do`

**Purpose.** Estimates ATC3-sharing triple differences for contract-plan-drug cohorts.

**Processing.** Uses the plan-level cohort files and the same event-time, first-seen, sharing, outcome-treatment, and clustering logic as the plan-level dynamic program; reports non-sharing and sharing effects and their difference.

**Outputs.** Result CSV files, sample summaries, LaTeX tables, and logs under `csv/formulary_plan/ddd/`, `tex/formulary_plan/ddd/`, and `logs/formulary_plan/ddd/`.
