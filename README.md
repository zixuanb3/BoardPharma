# BoardPharma Codes

This repository contains the Python and Stata scripts used in the BoardPharma project. It covers the full workflow from event-table construction and panel building to stacked / staggered event-study estimation and plotting. The current version also supports directional treated-side definitions (`A` / `B`) and cohort samples with or without counterpart-firm traversal.

## What Is Included

### Python

- `EventTableMaker.py`: builds directional event tables `data/event_B.xlsx` and `data/event_A.xlsx` from SSR and BoardEx intermediate data.
- `PanelMaker_FirmLevel.py`: constructs firm-product event-study panels at the year or quarter level under different event definitions, for both treated-side definitions (`A` / `B`), and saves movement metadata used by downstream cohort filtering.
- `CohortPanelMaker.py`: builds cohort-based samples from the firm-level panels for both `first_event` and `event`, supports multiple control-group definitions, and generates samples under different treated-side (A/B) and event-pair inclusion settings.
- `StaggeredPanelMaker.py`: prepares staggered DID / event-study panel datasets.
- `ATC3MappingMaker.py`: creates ATC-based peer mapping tables by time granularity.
- `ATC3DistributionPlotter.py`: adds `atc3_sharing` labels to cohort / staggered samples, based on either `event_A.xlsx` or `event_B.xlsx`, and exports grouped diagnostic figures.
- `StackedPanelBoxPlotter.py`: plots treated-group stacked boxplots from cohort samples with `atc3_sharing`, including normalized and log-normalized versions.

### Stata

- `StaggeredEventStudy.do`: runs dynamic staggered event-study regressions and compares estimators such as `csdid`, `did_imputation`, `TWFE`, and `eventstudyinteract`.
- `StackedEventStudy_v5.do`: runs stacked event-study estimation on cohort samples with `atc3_sharing` splits and reports results separately for sharing vs. non-sharing groups.
- `did_imputation_event_study.do`: runs `did_imputation`-based event-study estimation on grouped cohort samples (including `atc3_sharing` splits), supports both `hetby` and separate-run modes, and exports comparison plots / logs / coefficient CSVs.
- `ddd_atc3sharing.do`: runs DDD-style regressions with sharing interactions on grouped cohort samples and exports table outputs to both `tex/` and `csv/`.

## Workflow

If you are starting from the intermediate data, a typical workflow is:

1. `python EventTableMaker.py` to build `event_B.xlsx` and `event_A.xlsx`
2. `python PanelMaker_FirmLevel.py` to build year / quarter panels for treated side `B` and `A`
3. `python CohortPanelMaker.py` and/or `python StaggeredPanelMaker.py`
4. `python ATC3MappingMaker.py`
5. `python ATC3DistributionPlotter.py`
6. Run one or more Stata scripts: `StaggeredEventStudy.do`, `StackedEventStudy_v5.do`, `did_imputation_event_study.do`, `ddd_atc3sharing.do`
7. Optionally run `python StackedPanelBoxPlotter.py` for treated-group boxplot diagnostics

Notes:

- `CohortPanelMaker.py` is used for the stacked / cohort design.
- `StaggeredPanelMaker.py` is used for the staggered DID design.
- `treatment_group = B` means the destination firm is treated; `treatment_group = A` means the origin firm is treated.
- Cohort folder names such as `quarter-level_B_with_A` and `quarter-level_A_without_B` indicate treated side plus whether counterpart-firm observations are kept during cohort traversal.
- If you do not need the `atc3_sharing` split, you can skip `ATC3MappingMaker.py` and `ATC3DistributionPlotter.py`.
- The staggered branch keeps the legacy folder layout (`year-level` / `quarter-level`) even after the cohort branch was expanded.
- `README_codes.md` contains a more detailed script-by-script reference.


## Expected Directory Layout

In practice, it expects a structure similar to:

```text
BoardPharma/
├─ codes/
├─ InterimData/
├─ data/
├─ figures/
├─ logs/
├─ tex/
└─ csv/
```