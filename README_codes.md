# BoardPharma Codes

## Pipeline

1. Build event tables for both treated-side definitions: `EventTableMaker.py`
2. Build firm-level panels by panel level and treated side: `PanelMaker_FirmLevel.py`
3. Build cohort samples by treated side and event-pair setting: `CohortPanelMaker.py`
4. Build staggered samples on the legacy staggered branch: `StaggeredPanelMaker.py`
5. Build ATC peer mappings: `ATC3MappingMaker.py`
6. Add `atc3_sharing` labels and diagnostics: `ATC3DistributionPlotter.py`
7. Run stacked event studies: `StackedEventStudy_v5.do`
8. Run `did_imputation` event studies: `did_imputation_event_study.do`
9. Run DDD specifications: `ddd_atc3sharing.do`
10. Run staggered estimators: `StaggeredEventStudy.do`
11. Plot stacked treated-group boxplots: `StackedPanelBoxPlotter.py`

## Script Reference

## 1) EventTableMaker.py

Inputs
- `InterimData/boardex_ssr_price_sample.csv`
- `InterimData/boardex_interlock_direct_firmpair.dta`
- `InterimData/boardex_interlock_indirect_firmpair.dta`
- `InterimData/boardex_pharma.dta`

Processing
- Builds the full firm-year product timeline from SSR.
- Constructs direct and indirect interlock events with `stay_x_years` persistence filtering.
- Constructs directional movement events `to_B_still_in_A` and `to_B_not_in_A`.
- Exports separate event tables for the two movement treated-side definitions.
- `treatment_group = B`: treat the destination firm `B` as the treated firm.
- `treatment_group = A`: treat the origin firm `A` as the treated firm.

Outputs
- `data/event_B.xlsx`
- `data/event_A.xlsx`

## 2) PanelMaker_FirmLevel.py

Inputs
- `InterimData/boardex_ssr_price_sample.csv`
- `InterimData/boardex_interlock_direct_firmpair.dta`
- `InterimData/boardex_interlock_indirect_firmpair.dta`
- `InterimData/boardex_pharma.dta`

Processing
- Builds yearly or quarterly SSR firm-product panels.
- Supports four event definitions: `direct_interlock`, `indirect_interlock`, `to_B_not_in_A`, `to_B_still_in_A`.
- Supports both treated-side definitions through `treatment_group = A/B`.
- Applies `stay_x_years` and balance-window rules.
- Creates `first_event_year`, `event_YYYY`, `balance_panel_first`, and `balance_panel_YYYY`.
- Saves movement metadata used later when `include_eventpair = 0`.

Outputs
- `data/year-level_A/ssr_firm_panel_*.csv`
- `data/year-level_B/ssr_firm_panel_*.csv`
- `data/quarter-level_A/ssr_firm_panel_*.csv`
- `data/quarter-level_B/ssr_firm_panel_*.csv`
- `data/movement_list/*.csv`

## 3) CohortPanelMaker.py

Inputs
- `data/year-level_A/ssr_firm_panel_*.csv`
- `data/year-level_B/ssr_firm_panel_*.csv`
- `data/quarter-level_A/ssr_firm_panel_*.csv`
- `data/quarter-level_B/ssr_firm_panel_*.csv`
- `data/movement_list/*.csv` when `include_eventpair = 0` for movement events

Processing
- Builds cohort stacks around `first_event_year == t` or `event_t == 1`.
- Supports `pure_control`, `not_yet`, and `not` control definitions.
- Supports `treatment_group = A/B` and `include_eventpair = 0/1`.
    - `include_eventpair = 1`: keep counterpart-firm observations in cohort traversal, shown as `with`.
    - `include_eventpair = 0`: drop counterpart-only observations in cohort traversal, shown as `without`.
- Applies balanced-window filtering using upstream `balance_panel_*` columns.
- Produces cohort-count diagnostic plots by cohort year.

Outputs
- `data/cohort_data/{year|quarter}-level_{A|B}_{with|without}_{B|A}/{event_type}/{control_folder}/*.csv`
- `figures/cohort_distribution/{year|quarter}-level_{A|B}_{with|without}_{B|A}/{event_type}/{control_folder}/*.png`

## 4) StaggeredPanelMaker.py

Inputs
- `data/year-level/ssr_firm_panel_*.csv`
- `data/quarter-level/ssr_firm_panel_*.csv`

Processing
- Builds staggered DID/event-study samples around `first_event_year`.
- Supports `not_yet` and `pure_control`.
- Applies `balance_panel_first` and complete-window filtering when `balanced_panel = 1`.
- Keeps the staggered branch on the legacy folder layout.

Outputs
- `data/staggered_data/year-level/staggered_firm_level_panel_*.csv`
- `data/staggered_data/quarter-level/staggered_firm_level_panel_*.csv`

## 5) ATC3MappingMaker.py

Inputs
- `InterimData/boardex_ssr_price_sample.csv`

Processing
- Builds ATC peer-company mappings by time granularity.
- Creates `BoardName` to `BoardNamePair` peer mappings within ATC groups.
- Drops self-links and duplicate pairs.

Outputs
- `data/atc3mapping/atc3mapping_year_level_level2.csv`

## 6) ATC3DistributionPlotter.py

Inputs
- `data/atc3mapping/atc3mapping_year_level[_level2].csv`
- `data/cohort_data/{year|quarter}-level_{A|B}_{with|without}_{B|A}/...`
- `data/staggered_data/...`
- `data/event_B.xlsx`
- `data/event_A.xlsx`

Processing
- Labels treated firm-product observations with `atc3_sharing` using event-partner links and ATC peer mappings.
- Traverses cohort data over `treatment_group = A/B` and `include_eventpair = 0/1`.
- Traverses staggered data on the unchanged legacy branch.
- Supports alternative ATC aggregation levels through `atc_level`.
- Saves enriched data and plots sharing diagnostics for configured relative periods.

Outputs
- `data/cohort_data_with_atc3sharing/{year|quarter}-level_{A|B}_{with|without}_{B|A}/...`
- `data/staggered_data_with_atc3sharing/{year|quarter}-level/...`
- `figures/cohort_sharing_atc3/{year|quarter}-level_{A|B}_{with|without}_{B|A}/...`
- `figures/staggered_sharing_atc3/{year|quarter}-level/...`

## 7) StackedEventStudy_v5.do

Inputs
- `data/cohort_data_with_atc3sharing/quarter-level_{group_label}/...` when `atc3sharing = 1`
- `data/cohort_data/quarter-level_{group_label}/...` when `atc3sharing = 0`

Processing
- Runs stacked event-study regressions over events, outcomes, and control definitions.
- Supports both ATC-sharing-split and non-split modes.
- Uses the cohort-style grouped folder layout for movement events.

Outputs
- `logs/stacked_event_study_sharingatc3/quarter-level_{group_label}/...`
- `logs/stacked_event_study/quarter-level_{group_label}/...`
- `figures/stacked_event_study_sharingatc3/quarter-level_{group_label}/...`
- `figures/stacked_event_study/quarter-level_{group_label}/...`

## 8) did_imputation_event_study.do

Inputs
- `data/cohort_data_with_atc3sharing/quarter-level_{group_label}/{event_type}/{control_folder}/*.csv`

Processing
- Loops over `treatment_group = A/B`, `include_eventpair = 0/1`, `event_type`, controls, outcomes, scaling rules, and FE levels.
- Supports two modes:
- `hetby`: one `did_imputation` run with `hetby(atc3_sharing)`.
- `separate`: two separate treated-subgroup runs with a common control group.
- Supports both `standardize` and `normalize`; the normalize branch trims the top 5% of groups by within-group max/min ratio.
- Keeps one canonical interlock run only: `B_with_A`.
- Exports coefficient CSVs used for plotting and overlays the two sharing groups in each figure.

Outputs
- `logs/did_imputation_event_study_sharingatc3_fe{1|2}/quarter-level_{group_label}/{event}/{target}/*.log`
- `figures/did_imputation_event_study_sharingatc3_fe{1|2}/quarter-level_{group_label}/{event}/{target}/*.png`
- `csv/did_imputation_event_study_sharingatc3_fe{1|2}/quarter-level_{group_label}/{event}/{target}/*.csv`

## 9) ddd_atc3sharing.do

Inputs
- `data/cohort_data_with_atc3sharing/quarter-level_{group_label}/{event_type}/{control_folder}/*.csv`

Processing
- Loops over `treatment_group = A/B`, `include_eventpair = 0/1`, controls, outcomes, scaling rules, and FE levels.
- Estimates stacked DDD specifications with `Treat x Post` and `Treat x Post x ATC3-sharing`.
- Reports both baseline non-sharing effects and incremental sharing effects.
- Computes pre-means, percent effects, sample counts, and `did_imputation` heterogeneity contrasts for reporting.
- Keeps one canonical interlock run only: `B_with_A`.

Outputs
- `logs/ddd_atc3sharing_fe{1|2}/quarter-level_{group_label}/{event}/*.log`
- `tex/ddd_atc3sharing_fe{1|2}/quarter-level_{group_label}/{event}/*.tex`
- `csv/ddd_atc3sharing_fe{1|2}/quarter-level_{group_label}/{event}/*.csv`

## 10) StaggeredEventStudy.do

Inputs
- `data/staggered_data_with_atc3sharing/quarter/first_event/{control_folder}/*.csv`

Processing
- Runs staggered event-study estimators.
- Compares `csdid`, `did_imputation`, `TWFE`, and `eventstudyinteract`.
- Extracts and aligns dynamic coefficients by sharing group.

Outputs
- `logs/staggered_event_study_sharingatc3/quarter/{event}/*.log`
- `figures/staggered_event_study_sharingatc3/quarter/{event}/*.png`

## 11) StackedPanelBoxPlotter.py

Inputs
- `data/cohort_data_with_atc3sharing/quarter-level_{A|B}_{with|without}_{B|A}/{event_type}/Pure Control/*.csv`

Processing
- Loads balanced cohort stacks for treated units only.
- Normalizes each outcome by the cohort-baseline quarter.
- Optionally trims the top tail of groups by within-group max/min ratio before plotting.
- Splits the sample by `atc3_sharing = 0/1`.
- Exports both level-normalized and log-normalized boxplots.

Outputs
- `figures/stacked_panel_box_plot/{event}/{event_type}/{treatment_group}/{outcome_variable}/*_norm_boxplot.png`
- `figures/stacked_panel_box_plot/{event}/{event_type}/{treatment_group}/{outcome_variable}/*_log_norm_boxplot.png`

## Directory Map

- Raw inputs are read from `InterimData/`.
- Intermediate and analysis datasets are written under `data/`.
- Regression logs are written under `logs/`.
- Figures are written under `figures/`.
- LaTeX tables are written under `tex/`.
- CSV regression summaries are written under `csv/`.
