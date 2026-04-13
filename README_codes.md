# BoardPharma Codes

## Pipeline

1. Build event table: `EventTableMaker.py`
2. Build firm-level panels: `PanelMaker_FirmLevel.py`
3. Build cohort samples (stacked branch): `CohortPanelMaker.py`
4. Build staggered samples (staggered branch): `StaggeredPanelMaker.py`
5. Build ATC3 peer mapping: `ATC3MappingMaker.py`
6. Add ATC3 sharing labels: `ATC3DistributionPlotter.py`
7. Run cohort estimations: `StackedEventStudy_v5.do`, `did_imputation_event_study.do`, `ddd_atc3sharing.do`
8. Run staggered estimations: `StaggeredEventStudy.do`

## Script Reference

## 1) EventTableMaker.py

Inputs
- `InterimData/boardex_ssr_price_sample.csv`
- `InterimData/boardex_interlock_direct_firmpair.dta`
- `InterimData/boardex_interlock_indirect_firmpair.dta`
- `InterimData/boardex_pharma.dta`

Processing
- Builds board-product-year base records from SSR.
- Identifies direct interlock, indirect interlock, and director-mobility events.
- Applies stay rules and direction handling for event links.
- Consolidates all event types into one event table.

Outputs
- `data/event.xlsx`

## 2) PanelMaker_FirmLevel.py

Inputs
- `InterimData/boardex_ssr_price_sample.csv`
- `InterimData/boardex_interlock_direct_firmpair.dta`
- `InterimData/boardex_interlock_indirect_firmpair.dta`
- `InterimData/boardex_pharma.dta`

Processing
- Creates year-level and quarter-level firm-product panels.
- Merges event information into panel data.
- Creates event indicators such as `first_event_year` and `event_YYYY`.
- Creates balance-related flags used downstream.

Outputs
- `data/year-level/ssr_firm_panel_*.csv`
- `data/quarter-level/ssr_firm_panel_*.csv`

## 3) CohortPanelMaker.py

Inputs
- `data/year-level/ssr_firm_panel_*.csv`
- `data/quarter-level/ssr_firm_panel_*.csv`

Processing
- Builds cohort-based treated/control samples around cohort years.
- Supports both `first_event` and `event` treatment definitions.
- Supports control rules: `not`, `notyet`, `purecontrol`.
- Applies balanced-window filtering.

Outputs
- `data/cohort_data/{year|quarter}/{event_type}/{control_folder}/*.csv`
- `figures/cohort_distribution/{year|quarter}/{event_type}/{control_folder}/*.png`

## 4) StaggeredPanelMaker.py

Inputs
- `data/year-level/ssr_firm_panel_*.csv`
- `data/quarter-level/ssr_firm_panel_*.csv`

Processing
- Builds staggered DID/event-study samples from firm-level panels.
- Selects treated windows and combines control groups by rule.
- Applies balanced sample constraints.

Outputs
- `data/staggered_data/{year-level|quarter-level}/staggered_firm_level_panel_*.csv`

## 5) ATC3MappingMaker.py

Inputs
- `InterimData/boardex_ssr_price_sample.csv`

Processing
- Builds ATC3 peer-company mappings by time granularity.
- Creates `BoardName` to `BoardNamePair` mappings within ATC groups.
- Drops self-links and duplicates.

Outputs
- `data/atc3mapping/atc3mapping_year_level_level2.csv`

## 6) ATC3DistributionPlotter.py

Inputs
- `data/atc3mapping/atc3mapping_year_level_level2.csv`
- `data/cohort_data/quarter/...`
- `data/staggered_data/quarter-level/...`
- `data/event.xlsx`

Processing
- Merges ATC3 mapping and event links into cohort/staggered samples.
- Adds `atc3_sharing` labels.
- Produces sharing distribution diagnostics by event and control setting.

Outputs
- `data/cohort_data_with_atc3sharing/quarter/...`
- `data/staggered_data_with_atc3sharing/quarter/...`
- `figures/cohort_sharing_atc3/quarter/...`
- `figures/staggered_sharing_atc3/quarter/...`

## 7) StackedEventStudy_v5.do

Inputs
- `data/cohort_data_with_atc3sharing/quarter/...` when `atc3sharing=1`
- `data/cohort_data/quarter/...` when `atc3sharing=0`

Processing
- Runs stacked event-study estimations over events/outcomes/control definitions.
- Builds relative-time dummies and estimates dynamic coefficients.
- Supports both sharing-split and non-split modes.

Outputs
- `logs/stacked_event_study_sharingatc3/quarter/...` or `logs/stacked_event_study/quarter/...`
- `figures/stacked_event_study_sharingatc3/quarter/...` or `figures/stacked_event_study/quarter/...`

## 8) did_imputation_event_study.do

Inputs
- `data/cohort_data_with_atc3sharing/quarter/{event_type}/{control_folder}/*.csv`

Processing
- Runs dynamic treatment-effect estimation with `did_imputation`.
- Compares `atc3_sharing=0/1` under hetby or separate-sample modes.
- Optionally trims extreme normalized groups in trail mode.
- Produces estimator-comparison event-study plots.

Outputs
- `logs/did_imputation_event_study_sharingatc3*/quarter/{event}/*.log`
- `figures/did_imputation_event_study_sharingatc3*/quarter/*.png`

## 9) ddd_atc3sharing.do

Inputs
- `data/cohort_data_with_atc3sharing/quarter/{event_type}/{control_folder}/*.csv`

Processing
- Runs DDD-style regressions with sharing interaction terms.
- Supports standardize/normalize preprocessing and normalized outlier trimming.
- Computes table statistics including pre-means, percent effects, and grouped sample counts.
- Collects `did_imputation` contrast metrics for table reporting.

Outputs
- `logs/ddd_atc3sharing/quarter/{event}/*.log`
- `tex/ddd_atc3sharing/quarter/{event}/*.tex`

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

## Directory Map

- Inputs are read from `InterimData/` and `data/`.
- Processed datasets are written to subfolders under `data/`.
- Regression logs are written to `logs/`.
- Figures are written to `figures/`.
- LaTeX tables are written to `tex/`.
