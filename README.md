# BoardPharma Codes

This repository contains the Python and Stata scripts used in the BoardPharma project. It covers the full workflow from event-table construction and panel building to stacked / staggered event-study estimation and plotting.

## What Is Included

### Python

- `EventTableMaker.py`: builds the consolidated event table `data/event.xlsx` from SSR and BoardEx intermediate data.
- `PanelMaker_FirmLevel.py`: constructs firm-product event-study panels at the year or quarter level under different event definitions.
- `CohortPanelMaker.py`: builds cohort-based samples from the firm-level panels, including `first_event`, `event`, and multiple control-group definitions.
- `StaggeredPanelMaker.py`: prepares staggered DID / event-study panel datasets.
- `ATC3MappingMaker.py`: creates ATC-based peer mapping tables by time granularity.
- `ATC3DistributionPlotter.py`: adds `atc3_sharing` labels to cohort / staggered samples and exports related diagnostic figures.

### Stata

- `StaggeredEventStudy.do`: runs dynamic staggered event-study regressions and compares estimators such as `csdid`, `did_imputation`, `TWFE`, and `eventstudyinteract`.
- `StackedEventStudy_v5.do`: runs stacked event-study estimation on cohort samples with `atc3_sharing` splits and reports results separately for sharing vs. non-sharing groups.

## Workflow

If you are starting from the intermediate data, a typical workflow is:

1. `python EventTableMaker.py`
2. `python PanelMaker_FirmLevel.py`
3. `python CohortPanelMaker.py` or `python StaggeredPanelMaker.py`
4. `python ATC3MappingMaker.py`
5. `python ATC3DistributionPlotter.py`
6. Run `StaggeredEventStudy.do` or `StackedEventStudy_v5.do` in Stata

Notes:

- `CohortPanelMaker.py` is used for the stacked / cohort design.
- `StaggeredPanelMaker.py` is used for the staggered DID design.
- If you do not need the `atc3_sharing` split, you can skip `ATC3MappingMaker.py` and `ATC3DistributionPlotter.py`.

## Expected Directory Layout

In practice, it expects a structure similar to:

```text
BoardPharma/
├─ codes/
├─ InterimData/
├─ data/
├─ figures/
└─ logs/
```

In other words:

- input data is read from `../InterimData/` or `../data/`
- outputs are written to `../data/`, `../figures/`, and `../logs/`