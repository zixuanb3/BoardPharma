"""
Purpose: Expand the formulary-year panel to include ALL brand NDCs for each formulary-year.
         For each formulary-year combination, include every unique brand NDC (is_generic=0,
         BoardName not null). Add a dummy variable 'included' = 1 if the NDC was actually
         present in that formulary-year in the original data, else 0.
         Uses chunked reading + itertuples for memory efficiency.

Input:  D:\pharma\task1_final_panel_with_atc_all.csv
Output: D:\pharma\task1_expanded_brand_panel.csv
"""

import pandas as pd
import numpy as np

INPUT = r'D:\pharma\task1_final_panel_with_atc_all.csv'
OUTPUT = r'D:\pharma\task1_expanded_brand_panel.csv'
CHUNKSIZE = 500000

# ============================================================
# Step 1: Chunked read to collect unique keys and brand metadata
# ============================================================
print("Pass 1: Chunked reading to extract keys and brand metadata...")

form_year_set = set()           # (YEAR_Q, FORMULARY_ID)
actual_inclusion_set = set()    # (YEAR_Q, FORMULARY_ID, NDC) for brand drugs
brand_ndc_meta_dict = {}        # NDC -> {metadata fields}
form_specific_rows = []         # list of dicts
ndc_flags_dict = {}             # NDC -> (is_specialty, is_brand)

metadata_cols = ['BoardName', 'LabelerName', 'ProprietaryName',
                 'NonProprietaryName', 'MARKETINGCATEGORYNAME',
                 'ATC1', 'ATC1_name', 'ATC2', 'ATC2_name',
                 'ATC3', 'ATC3_name', 'ATC4', 'ATC4_name', 'n_atc']

chunk_count = 0
for chunk in pd.read_csv(INPUT, chunksize=CHUNKSIZE):
    chunk_count += 1
    
    # Collect unique formulary-year combos (vectorized)
    for tup in chunk[['YEAR_Q', 'FORMULARY_ID']].drop_duplicates().itertuples(index=False, name=None):
        form_year_set.add(tup)
    
    # Brand mask: is_generic=0 AND BoardName not null
    brand_mask = (chunk['is_generic'] == 0) & (chunk['BoardName'].notna())
    brand_chunk = chunk[brand_mask]
    
    if len(brand_chunk) > 0:
        # Collect actual inclusion tuples (dedup first)
        inc_tuples = brand_chunk[['YEAR_Q', 'FORMULARY_ID', 'NDC']].drop_duplicates()
        for tup in inc_tuples.itertuples(index=False, name=None):
            actual_inclusion_set.add(tup)
        
        # Collect brand NDC metadata (first occurrence per NDC per chunk)
        ndc_meta_sub = brand_chunk[['NDC'] + metadata_cols].drop_duplicates(subset='NDC')
        for row in ndc_meta_sub.itertuples(index=False):
            ndc = row[0]
            if ndc not in brand_ndc_meta_dict:
                brand_ndc_meta_dict[ndc] = dict(zip(metadata_cols, row[1:]))
        
        # Collect formulary-specific fields
        fs_sub = brand_chunk[['YEAR_Q', 'FORMULARY_ID', 'NDC',
                               'tier_raw', 'PA', 'ST', 'QL', 'max_tier']].drop_duplicates()
        for row in fs_sub.itertuples(index=False):
            form_specific_rows.append({
                'YEAR_Q': row[0], 'FORMULARY_ID': row[1], 'NDC': row[2],
                'tier_raw': row[3], 'PA': row[4], 'ST': row[5],
                'QL': row[6], 'max_tier': row[7]
            })
        
        # Collect NDC-level flags (first occurrence)
        ndc_flag_sub = brand_chunk[['NDC', 'is_specialty', 'is_brand']].drop_duplicates(subset='NDC')
        for row in ndc_flag_sub.itertuples(index=False):
            ndc = row[0]
            if ndc not in ndc_flags_dict:
                ndc_flags_dict[ndc] = (row[1], row[2])
    
    if chunk_count % 20 == 0:
        print(f"  Processed chunk {chunk_count} (brand NDCs so far: {len(brand_ndc_meta_dict):,})...")

print(f"Pass 1 complete. Processed {chunk_count} chunks.")
print(f"  Unique formulary-year combos: {len(form_year_set):,}")
print(f"  Unique brand NDCs: {len(brand_ndc_meta_dict):,}")
print(f"  Actual brand inclusion entries: {len(actual_inclusion_set):,}")
print(f"  Formulary-specific rows collected: {len(form_specific_rows):,}")

# ============================================================
# Step 2: Build lookup DataFrames from collected data
# ============================================================
print("\nBuilding lookup DataFrames...")

form_year = pd.DataFrame(list(form_year_set), columns=['YEAR_Q', 'FORMULARY_ID'])
print(f"Formulary-year DataFrame: {len(form_year)} rows")

brand_ndc_meta = pd.DataFrame([
    {'NDC': ndc, **meta} for ndc, meta in brand_ndc_meta_dict.items()
])
print(f"Brand NDC metadata DataFrame: {len(brand_ndc_meta)} rows")

actual_df = pd.DataFrame(list(actual_inclusion_set),
                          columns=['YEAR_Q', 'FORMULARY_ID', 'NDC'])
actual_df['included'] = 1
print(f"Actual inclusion DataFrame: {len(actual_df)} rows")

form_specific = pd.DataFrame(form_specific_rows)
form_specific = form_specific.drop_duplicates(subset=['YEAR_Q', 'FORMULARY_ID', 'NDC'])
print(f"Formulary-specific DataFrame (deduped): {len(form_specific)} rows")

ndc_flags = pd.DataFrame([
    {'NDC': ndc, 'is_specialty': v[0], 'is_brand': v[1]}
    for ndc, v in ndc_flags_dict.items()
])

# Free memory
del form_year_set, actual_inclusion_set, brand_ndc_meta_dict, form_specific_rows, ndc_flags_dict

# ============================================================
# Step 3: Cross-join expansion + save incrementally
# ============================================================
print("\nPass 2: Cross-join expansion and saving...")

total_combos = len(form_year)
chunk_size = 500
first_chunk = True

# Columns order for consistency
col_order = [
    'YEAR_Q', 'FORMULARY_ID',
    'NDC', 'BoardName', 'LabelerName', 'ProprietaryName', 'NonProprietaryName',
    'MARKETINGCATEGORYNAME',
    'ATC1', 'ATC1_name', 'ATC2', 'ATC2_name', 'ATC3', 'ATC3_name',
    'ATC4', 'ATC4_name', 'n_atc',
    'included', 'tier_raw', 'PA', 'ST', 'QL', 'max_tier',
    'is_generic', 'is_specialty', 'is_brand'
]

for i in range(0, total_combos, chunk_size):
    fy_chunk = form_year.iloc[i:i+chunk_size].copy()

    # Cross join
    fy_chunk['_key'] = 1
    meta_copy = brand_ndc_meta.copy()
    meta_copy['_key'] = 1
    expanded = fy_chunk.merge(meta_copy, on='_key').drop(columns='_key')
    del fy_chunk

    # Merge included flag
    expanded = expanded.merge(actual_df, on=['YEAR_Q', 'FORMULARY_ID', 'NDC'], how='left')
    expanded['included'] = expanded['included'].fillna(0).astype(int)

    # Merge formulary-specific fields
    expanded = expanded.merge(form_specific, on=['YEAR_Q', 'FORMULARY_ID', 'NDC'], how='left')

    # Merge NDC flags
    expanded = expanded.merge(ndc_flags, on='NDC', how='left')
    expanded['is_generic'] = 0

    # Reorder columns for consistency
    expanded = expanded[col_order]

    # Write
    expanded.to_csv(OUTPUT, mode='a', index=False, header=first_chunk)
    first_chunk = False

    if (i + chunk_size) % 2000 == 0 or (i + chunk_size) >= total_combos:
        print(f"  Processed {min(i+chunk_size, total_combos):,} / {total_combos:,} combos...")

print("Expansion complete.")

# ============================================================
# Step 4: Quick verification
# ============================================================
print("\n=== Quick Verification ===")
verify = pd.read_csv(OUTPUT, nrows=5)
print("First 5 rows:")
print(verify.to_string())
print(f"\nColumns: {verify.columns.tolist()}")

# Count rows
import subprocess
result = subprocess.run(['powershell', '-Command',
    f"(Get-Content '{OUTPUT}' | Measure-Object -Line).Lines"],
    capture_output=True, text=True)
total_lines = int(result.stdout.strip()) - 1
print(f"\nTotal data rows in output: {total_lines:,} (expected ~30.5M)")

# Verify included distribution on a sample
print("\nSampling for included distribution...")
sample = pd.read_csv(OUTPUT, usecols=['included'], nrows=500000)
print(f"Sample included=1: {(sample['included']==1).sum():,} / {len(sample):,}")

print("\nDone!")
