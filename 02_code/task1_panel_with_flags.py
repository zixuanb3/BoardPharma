# -*- coding: utf-8 -*-
"""Purpose:
    Build a drug-formulary-quarter panel with FDA drug information, company
    component IDs, formulary tiers, and utilization-management flags.

Process:
    1. Build an FDA NDC9 lookup for labeler, marketing category, and drug names.
    2. Build a unique LabelerName-to-id mapping from the company crosswalk.
    3. Compute the maximum tier within each formulary-quarter.
    4. Process the formulary in chunks, retain rows with a company id, and
       derive generic and brand indicators.

Input:
    D:\\pharma\\merged_basic_drugs_formulary.csv
    D:\\pharma\\full_list_of_ndc_codes\\fda_ndc_product.csv
    crosswalks/labeler_company_mapping_standardized_with_id.csv

Output:
    data/formulary/formulary_panel_with_company_id.csv
"""

import csv
import os
from pathlib import Path
import time

import pandas as pd

FORMULARY = r"D:\pharma\merged_basic_drugs_formulary.csv"
FDA_PROD = r"D:\pharma\full_list_of_ndc_codes\fda_ndc_product.csv"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAPPING = PROJECT_ROOT / "crosswalks" / "labeler_company_mapping_standardized_with_id.csv"
OUTPUT_DIR = PROJECT_ROOT / "data" / "formulary"
OUTPUT = OUTPUT_DIR / "formulary_panel_with_company_id.csv"
FORMULARY_COLUMNS = [
    'YEAR_Q', 'FORMULARY_ID', 'FORMULARY_VERSION', 'CONTRACT_YEAR', 'RXCUI',
    'NDC', 'TIER_LEVEL_VALUE', 'QUANTITY_LIMIT_YN', 'QUANTITY_LIMIT_AMOUNT',
    'QUANTITY_LIMIT_DAYS', 'PRIOR_AUTHORIZATION_YN', 'STEP_THERAPY_YN',
    'SELECTED_DRUG_YN',
]
FORMULARY_USECOLS = [
    'YEAR_Q', 'FORMULARY_ID', 'NDC', 'TIER_LEVEL_VALUE',
]


def iter_formulary_chunks(usecols, chunk_size):
    """Read mixed 12/13-field CMS rows and yield only requested columns."""
    column_positions = {name: FORMULARY_COLUMNS.index(name) for name in usecols}
    buffers = {name: [] for name in usecols}

    with open(FORMULARY, 'r', encoding='utf-8-sig', newline='') as source:
        reader = csv.reader(source)
        header = next(reader, None)
        accepted_headers = [FORMULARY_COLUMNS[:-1], FORMULARY_COLUMNS]
        if header not in accepted_headers:
            raise ValueError(f"Unexpected formulary header at line 1: {header}")

        for row in reader:
            if len(row) == len(FORMULARY_COLUMNS) - 1:
                row.append('')
            elif len(row) != len(FORMULARY_COLUMNS):
                raise ValueError(
                    f"Unexpected field count at physical line {reader.line_num}: "
                    f"expected 12 or 13 fields, found {len(row)}"
                )

            for name, position in column_positions.items():
                buffers[name].append(row[position])

            if len(next(iter(buffers.values()))) >= chunk_size:
                chunk = pd.DataFrame(buffers)
                buffers = {name: [] for name in usecols}
                yield chunk

    if buffers and len(next(iter(buffers.values()))) > 0:
        yield pd.DataFrame(buffers)

t0 = time.time()
print("=" * 65)
print("  Final Panel: Generic / Brand Flags with Company IDs")
print("=" * 65)

# ══════════════════════════════════════════════════════════════
# STEP 1: FDA NDC → Labeler + MARKETINGCATEGORYNAME
# ══════════════════════════════════════════════════════════════
print("\n[1] Building FDA NDC lookup...")
fda = pd.read_csv(FDA_PROD, dtype=str, encoding='utf-8',
                  usecols=['PRODUCTNDC', 'LABELERNAME', 'MARKETINGCATEGORYNAME',
                           ])
fda = fda.drop_duplicates(subset='PRODUCTNDC')
fda['PRODUCTNDC'] = fda['PRODUCTNDC'].str.strip()
fda['parts'] = fda['PRODUCTNDC'].str.split('-')
fda['NDC9'] = fda['parts'].str[0].str.zfill(5) + fda['parts'].str[1].str.zfill(4)
ndc9_map = fda.set_index('NDC9')
print(f"  FDA unique NDCs: {len(ndc9_map):,}")

# ══════════════════════════════════════════════════════════════
# STEP 2: LabelerName → connected-component id
# ══════════════════════════════════════════════════════════════
print("\n[2] Building LabelerName → id mapping...")
mapping = pd.read_csv(MAPPING, usecols=['LabelerName', 'id'], dtype=str)
mapping.columns = mapping.columns.str.strip()
required_mapping_cols = {'LabelerName', 'id'}
missing_mapping_cols = required_mapping_cols.difference(mapping.columns)
if missing_mapping_cols:
    raise ValueError(f"Mapping file is missing required columns: {sorted(missing_mapping_cols)}")

mapping['LabelerName'] = mapping['LabelerName'].fillna('').str.strip()
mapping['id'] = pd.to_numeric(mapping['id'], errors='raise').astype('Int64')
mapping = mapping[(mapping['LabelerName'] != '') & mapping['id'].notna()].copy()
mapping['labeler_key'] = mapping['LabelerName'].str.upper()
labeler_id = mapping[['labeler_key', 'id']].drop_duplicates()
id_counts = labeler_id.groupby('labeler_key')['id'].nunique()
conflicting_labelers = id_counts[id_counts.gt(1)]
if not conflicting_labelers.empty:
    raise ValueError(
        "Each normalized LabelerName must map to one id; conflicting labelers: "
        f"{conflicting_labelers.index.tolist()[:10]}"
    )

# Multiple mapping rows may carry different BN/CN names for the same labeler
# and id. Keeping one row per key makes the later join many-to-one.
labeler_id = labeler_id.drop_duplicates('labeler_key')
print(f"  Unique LabelerName keys: {len(labeler_id):,}")

# ══════════════════════════════════════════════════════════════
# STEP 3: Precompute global tier maxima using a narrow first pass
# ══════════════════════════════════════════════════════════════
chunk_size = 250000
print("\n[3] Scanning tier keys for global formulary-quarter maxima...")
max_tier_parts = []
for tier_chunk in iter_formulary_chunks(
    ['YEAR_Q', 'FORMULARY_ID', 'TIER_LEVEL_VALUE'], chunk_size,
):
    tier_chunk['TIER_LEVEL_VALUE'] = pd.to_numeric(
        tier_chunk['TIER_LEVEL_VALUE'], errors='coerce'
    )
    max_tier_parts.append(
        tier_chunk.groupby(['FORMULARY_ID', 'YEAR_Q'], as_index=False)['TIER_LEVEL_VALUE'].max()
    )

max_tier = pd.concat(max_tier_parts, ignore_index=True).groupby(
    ['FORMULARY_ID', 'YEAR_Q'], as_index=False
)['TIER_LEVEL_VALUE'].max().rename(columns={'TIER_LEVEL_VALUE': 'max_tier'})
print(f"  Formulary-quarter keys: {len(max_tier):,}")

# ══════════════════════════════════════════════════════════════
# STEP 4: Process chunks and stream directly to a temporary output
# ══════════════════════════════════════════════════════════════
print("\n[4] Processing formulary panel in chunks...")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
tmp_output = OUTPUT.with_suffix(OUTPUT.suffix + '.tmp')
if os.path.exists(tmp_output):
    os.remove(tmp_output)

first_chunk = True
source_rows = 0
output_rows = 0
labeler_rows = 0
flag_totals = {'is_generic': 0, 'is_brand': 0}
flag_combinations = {}

for chunk_num, chunk in enumerate(iter_formulary_chunks(FORMULARY_USECOLS, chunk_size)):
    source_rows += len(chunk)
    chunk = chunk.rename(columns={
        'TIER_LEVEL_VALUE': 'tier_raw',
    })

    for col in ['NDC', 'YEAR_Q', 'FORMULARY_ID']:
        chunk[col] = chunk[col].astype(str).str.strip()
    chunk['tier_raw'] = pd.to_numeric(chunk['tier_raw'], errors='coerce').astype('Int64')
    # FDA product records use the 9-digit labeler-plus-product portion of NDC.
    ndc9 = chunk['NDC'].str.zfill(11).str[:9]
    chunk['LabelerName'] = ndc9.map(ndc9_map['LABELERNAME'])
    chunk['MARKETINGCATEGORYNAME'] = ndc9.map(ndc9_map['MARKETINGCATEGORYNAME'])

    chunk['is_generic'] = (
        chunk['MARKETINGCATEGORYNAME'].str.upper().fillna('').str.contains('ANDA').astype('int8')
    )
    has_fda_category = chunk['MARKETINGCATEGORYNAME'].notna()
    chunk['is_brand'] = (
        has_fda_category & (chunk['is_generic'] == 0)
    ).astype('int8')
    chunk['labeler_key'] = chunk['LabelerName'].fillna('').str.strip().str.upper()
    chunk = chunk.merge(
        labeler_id,
        on='labeler_key',
        how='left',
        validate='many_to_one',
    ).drop(columns='labeler_key')
    chunk = chunk.dropna(subset=['id'])
    if chunk.empty:
        continue

    chunk = chunk.merge(max_tier, on=['FORMULARY_ID', 'YEAR_Q'], how='left')

    keep = [
        'YEAR_Q', 'FORMULARY_ID', 'NDC', 'tier_raw', 'max_tier',
        'LabelerName', 'id',
        'is_generic', 'is_brand',
    ]
    chunk = chunk[keep]
    chunk = chunk[chunk['is_generic'].eq(0)]
    if chunk.empty:
        continue

    output_rows += len(chunk)
    labeler_rows += int(chunk['LabelerName'].notna().sum())
    for flag in flag_totals:
        flag_totals[flag] += int(chunk[flag].sum())
    combo_counts = chunk.groupby(['is_generic', 'is_brand']).size()
    for combo, count in combo_counts.items():
        flag_combinations[combo] = flag_combinations.get(combo, 0) + int(count)

    chunk.to_csv(
        tmp_output, mode='w' if first_chunk else 'a',
        header=first_chunk, index=False,
    )
    first_chunk = False
    if (chunk_num + 1) % 5 == 0:
        print(f"  Processed {source_rows:,} source rows; retained {output_rows:,} rows...")

if first_chunk:
    raise ValueError("No formulary rows matched a mapping id.")
os.replace(tmp_output, OUTPUT)

print(f"\n{'=' * 65}")
print("  FINAL PANEL SUMMARY")
print(f"{'=' * 65}")
print(f"  Source rows:                {source_rows:>12,}")
print(f"  Dropped without id:         {source_rows - output_rows:>12,}")
print(f"  Output rows:                {output_rows:>12,}")
print(f"  With Labeler:               {labeler_rows:>12,} ({labeler_rows/output_rows*100:.1f}%)")
for flag, label in [('is_generic', 'Generic'), ('is_brand', 'Brand')]:
    print(f"  {label} rows: {flag_totals[flag]:>12,} ({flag_totals[flag]/output_rows*100:.1f}%)")
print("  Flag combinations (generic, brand):")
for combo, count in sorted(flag_combinations.items()):
    print(f"    {combo}: {count:,}")
sz = os.path.getsize(OUTPUT) / 1e9
print(f"  {OUTPUT}: {output_rows:,} rows, {sz:.2f} GB, {time.time() - t0:.0f}s")
print(f"{'=' * 65}")
