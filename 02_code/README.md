# Formulary Drug Panel and ATC Classification Data Package

## 1. Purpose

This folder organizes the CMS Formulary drug records, FDA NDC product data, standardized labeler-to-company mapping, panel construction scripts, and the existing panel outputs with ATC classifications. The intended workflow is:

```text
merged_basic_drugs_formulary.csv
        + fda_ndc_product.csv
        + labeler_company_mapping_standardized.csv
        + CMS quarterly plan and beneficiary cost files
        + BoardEx company data
                    |
                    v
task1_panel_with_flags.py
                    |
                    v
task1_final_panel.csv
        + WHO ATC-DDD crosswalk
        + RxNav NLM API / local cache
                    |
                    v
atc_all_classes.py
                    |
                    v
task1_final_panel_with_atc_all.csv
```

## 2. Folder Structure

| Folder | Contents | Purpose |
|---|---|---|
| `01_input_data` | Raw Formulary data, FDA product data, labeler-company mapping, and the base panel | Input data and the panel used by the ATC script |
| `02_code` | Five Python scripts for panel construction, ATC classification, copay aggregation, preferred-tier labels, and brand-panel expansion | Data-processing and analysis code |
| `03_output_data` | `task1_final_panel_with_atc_all.csv` | Existing final panel with ATC classifications |
| `04_reference_data` | `WHO ATC-DDD 2024-07-31.csv` | Reference table for ATC codes and names |
| `05_cache` | NDC-to-ATC lookup caches | Avoid repeated API lookups and support resumed runs |

## 3. File Descriptions

### Input Data

- `01_input_data/merged_basic_drugs_formulary.csv`: CMS Formulary drug-level records. It includes quarter, Formulary, NDC, tier, and utilization-management fields such as PA, ST, and QL. File size: approximately 2.48 GB.
- `01_input_data/fda_ndc_product.csv`: FDA NDC product data used to add labeler, marketing category, and drug names. File size: approximately 39.1 MB.
- `01_input_data/labeler_company_mapping_standardized.csv`: Standardized labeler-to-company mapping. File size: approximately 89 KB.
- `01_input_data/task1_final_panel.csv`: Existing base panel with drug-type flags, tier, and company-matching fields. This is also the panel input for `atc_all_classes.py`. File size: approximately 5.36 GB.

### Code

- `02_code/task1_panel_with_flags.py`: Builds a Formulary-quarter-drug panel from the raw Formulary file, FDA data, labeler-company mapping, and quarterly CMS plan/cost data. BoardEx data is used for company-name matching. The script writes the base panel and a Specialty consistency audit file.
- `02_code/atc_all_classes.py`: Queries RxNav for all ATC classifications associated with each NDC in the base panel, then merges the results into the panel. Lookup results are saved to local caches for reuse in later runs.
- `02_code/export_copay_csv.py`: Reads `D:\pharma\merged_beneficiary_cost.csv`, keeps `COST_TYPE_PREF` values 0 and 1, and calculates mean cost and row counts by Formulary, quarter, tier, contract, plan, and segment. It writes `D:\pharma\aggregated_copay_by_tier.csv`. **This script does not generate `copay_avg_by_plan_tier.csv`.**
- `02_code/prefer_label.py`: Reads `D:\pharma\copay_avg_by_plan_tier.csv`. For plan-quarters with maximum tier 5 or 6, it finds the largest cost increase between adjacent observed tiers and adds a `prefer` indicator. It writes `D:\pharma\copay_avg_with_prefer.csv`. The input `copay_avg_by_plan_tier.csv` is not included in this package, and its generating script was not found in the project files examined.
- `02_code/expand_brand_ndc_panel.py`: Reads `D:\pharma\task1_final_panel_with_atc_all.csv` and expands each Formulary-quarter to include every NDC selected by its brand-NDC rule. The output has an `included` indicator for whether an NDC appeared in the original Formulary-quarter and is written to `D:\pharma\task1_expanded_brand_panel.csv`. The script defines candidate brand NDCs as `is_generic == 0` with a nonmissing `BoardName`; it sets `is_generic` to 0 in the expanded data. The expanded output is not included in this package.

### Output Data

- `03_output_data/task1_final_panel_with_atc_all.csv`: Existing panel snapshot with ATC classifications added. File size: approximately 13.96 GB. If an NDC maps to multiple ATC classes, the `ATC1`–`ATC4` codes and corresponding name fields contain semicolon-separated values. `n_atc` is the number of ATC classifications found for that NDC. Records without an ATC match have `n_atc=0`.

## 4. Key Classification Rules

The packaged `task1_panel_with_flags.py` documents and implements these rules:

- `is_generic`: 1 when the FDA `MARKETINGCATEGORYNAME` contains `ANDA`.
- `is_specialty`: 1 when any matched plan for the same quarter, Formulary, and tier is flagged as Specialty.
- `is_brand`: 1 when FDA classification is observed and the record is neither generic nor specialty. A missing FDA classification is not treated as evidence that a drug is a brand drug.

The CMS Formulary file may contain both 12-field and 13-field data rows. The additional 13th field is `SELECTED_DRUG_YN`. The packaged panel script recognizes both row widths and supplies a blank value for this field on older 12-field rows.

## 5. How to Reproduce the Outputs

### 5.1 Runtime Requirements

- Python: the local installation is `D:\ProgramData\anaconda3\python.exe`.
- Python packages: `pandas` and `requests`.
- ATC lookup: `atc_all_classes.py` calls the RxNav/NLM API for NDCs that are not in the cache, so an internet connection is required for uncached lookups.
- Base-panel construction also requires quarterly CMS Plan/Beneficiary Cost files and the BoardEx `organization_composition.csv` and `company_details.csv` files. These additional large inputs are not included in this package.

### 5.2 Script Paths

The packaged `atc_all_classes.py` uses paths relative to the package folder. It reads the base panel from `01_input_data`, the WHO crosswalk from `04_reference_data`, and its caches from `05_cache`; it writes the completed output to `03_output_data`. The other four scripts retain the absolute project paths used in the original project. Their required source files and output locations are described above.

- `task1_panel_with_flags.py` reads the Formulary, FDA, and mapping files under `D:\pharma`, plus quarterly CMS files and BoardEx data from their project directories. It writes `D:\pharma\task1_final_panel.csv`.

To run the packaged ATC script from another computer, keep the package folder structure intact and install `pandas` and `requests`. The script writes to a temporary file and replaces the existing ATC output only after a complete successful run. CSV parsing is strict: malformed rows stop processing instead of being silently skipped. Example command:

```powershell
& 'D:\ProgramData\anaconda3\python.exe' 'D:\pharma\formulary_atc_package\02_code\atc_all_classes.py'
```

Run `task1_panel_with_flags.py` to rebuild the base panel only after the quarterly CMS plan/cost data and BoardEx files are available and the script paths point to those files. To use the rebuilt panel with the packaged ATC script, place it at `01_input_data/task1_final_panel.csv`.

The additional copay scripts need `merged_beneficiary_cost.csv` or `copay_avg_by_plan_tier.csv`, depending on the script. Neither input is included in this package. The brand-panel expansion script needs the ATC-enriched panel and may create a large output; review its hardcoded input/output paths before running it from this package.

## 6. Output Versions and Reproduction Limitations

This package contains both code and existing data snapshots. The outputs were not freshly regenerated from the current packaged panel script. The base panel and ATC output timestamps predate the latest update to `task1_panel_with_flags.py`. Therefore, treat the packaged base panel and ATC panel as existing output snapshots; their presence in this package does not establish that they incorporate the latest panel rules.

To reproduce outputs under the latest rules, provide the quarterly CMS Plan/Beneficiary Cost files and BoardEx inputs that are not included here, run `task1_panel_with_flags.py` to create a new base panel, place that panel at `01_input_data/task1_final_panel.csv`, then run `atc_all_classes.py` to generate a new ATC output. Exact agreement with the existing ATC output also depends on the cache contents and the data returned by the RxNav API at the time of the run. The updated ATC script adds an `ATC_status` field so successful matches, valid no-match results, API errors, legacy fallbacks, and invalid NDC values can be distinguished.

## 7. File Integrity

The copied files were checked by comparing their byte sizes with the source files. Key file sizes are:

| File | Size in bytes |
|---|---:|
| `merged_basic_drugs_formulary.csv` | 2,477,599,224 |
| `fda_ndc_product.csv` | 39,092,300 |
| `labeler_company_mapping_standardized.csv` | 89,012 |
| `task1_final_panel.csv` | 5,356,062,420 |
| `task1_final_panel_with_atc_all.csv` | 13,961,812,399 |
| `atc_all_classes.py` | 11,433 |
| `task1_panel_with_flags.py` | 18,792 |

Matching byte sizes confirm that the copy operation preserved file lengths. Cryptographic hashes were not calculated for the data files, which together exceed 20 GB.
