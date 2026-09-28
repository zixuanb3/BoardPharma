# -*- coding: utf-8 -*-
"""Purpose:
    Build a drug-formulary-quarter panel with drug-type flags and BoardEx matches.

Input:
    D:\\pharma\\merged_basic_drugs_formulary.csv
    D:\\pharma\\full_list_of_ndc_codes\\fda_ndc_product.csv
    Quarterly Beneficiary Cost and Plan Information files under COST_ROOT
    D:\\pharma\\labeler_company_mapping_standardized.csv

Output:
    D:\\pharma\\task1_final_panel.csv
    D:\\pharma\\specialty_tier_plan_consistency_audit.csv

Classification:
    Generic means FDA marketing category contains ANDA.
    Specialty means the formulary tier is specialty for any matched plan.
    Brand means FDA category is observed and neither other flag is set.
"""

import pandas as pd
import csv
import os, re, time

FORMULARY = r"D:\pharma\merged_basic_drugs_formulary.csv"
FDA_PROD = r"D:\pharma\full_list_of_ndc_codes\fda_ndc_product.csv"
MAPPING = r"D:\pharma\labeler_company_mapping_standardized.csv"
BOARDEX_ORG = r"D:\Dropbox\BoardPharma\RawData\boardex\boardex_na\organization_composition.csv"
BOARDEX_COMPANY = r"D:\Dropbox\BoardPharma\RawData\boardex\boardex_na\company_details.csv"
COST_ROOT = r"D:\pharma\批量下载-formulary等142个文件\formulary"
OUTPUT = r"D:\pharma\task1_final_panel.csv"
SPECIALTY_AUDIT_OUTPUT = r"D:\pharma\specialty_tier_plan_consistency_audit.csv"
FORMULARY_COLUMNS = [
    'YEAR_Q', 'FORMULARY_ID', 'FORMULARY_VERSION', 'CONTRACT_YEAR', 'RXCUI',
    'NDC', 'TIER_LEVEL_VALUE', 'QUANTITY_LIMIT_YN', 'QUANTITY_LIMIT_AMOUNT',
    'QUANTITY_LIMIT_DAYS', 'PRIOR_AUTHORIZATION_YN', 'STEP_THERAPY_YN',
    'SELECTED_DRUG_YN',
]
FORMULARY_USECOLS = [
    'YEAR_Q', 'FORMULARY_ID', 'NDC', 'TIER_LEVEL_VALUE',
    'PRIOR_AUTHORIZATION_YN', 'STEP_THERAPY_YN', 'QUANTITY_LIMIT_YN',
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
print("  Final Panel: Generic / Specialty / Brand Flags")
print("=" * 65)

# ══════════════════════════════════════════════════════════════
# STEP 1: FDA NDC → Labeler + MARKETINGCATEGORYNAME
# ══════════════════════════════════════════════════════════════
print("\n[1] Building FDA NDC lookup...")
fda = pd.read_csv(FDA_PROD, dtype=str, encoding='utf-8',
                  usecols=['PRODUCTNDC', 'LABELERNAME', 'MARKETINGCATEGORYNAME',
                           'PROPRIETARYNAME', 'NONPROPRIETARYNAME'])
fda = fda.drop_duplicates(subset='PRODUCTNDC')
fda['PRODUCTNDC'] = fda['PRODUCTNDC'].str.strip()
fda['parts'] = fda['PRODUCTNDC'].str.split('-')
fda['NDC9'] = fda['parts'].str[0].str.zfill(5) + fda['parts'].str[1].str.zfill(4)
ndc9_map = fda.set_index('NDC9')
print(f"  FDA unique NDCs: {len(ndc9_map):,}")

# ══════════════════════════════════════════════════════════════
# STEP 2: Labeler → BoardName
# ══════════════════════════════════════════════════════════════
print("\n[2] Building Labeler → BoardName mapping...")
mapping = pd.read_csv(MAPPING, dtype=str)
mapping.columns = mapping.columns.str.strip()
required_mapping_cols = {'LabelerName', 'BoardName', 'CompanyName'}
missing_mapping_cols = required_mapping_cols.difference(mapping.columns)
if missing_mapping_cols:
    raise ValueError(f"Mapping file is missing required columns: {sorted(missing_mapping_cols)}")

mapping['LabelerName'] = mapping['LabelerName'].fillna('').str.strip()
mapping['BoardName'] = mapping['BoardName'].fillna('').str.strip()
mapping['CompanyName'] = mapping['CompanyName'].fillna('').str.strip()
if 'from_mapping' in mapping.columns:
    mapping['from_mapping'] = mapping['from_mapping'].fillna('0').str.strip()
else:
    mapping['from_mapping'] = '0'

def normalize_company_name(value):
    """Build an alphanumeric key for exact company-name joins."""
    return re.sub(r'[^A-Z0-9]+', ' ', str(value).upper()).strip()


def build_companyname_boardname_lookup(company_names):
    """Join mapping CompanyName to BoardEx company and organization names."""
    wanted = {normalize_company_name(name) for name in company_names if name}
    company_details = pd.read_csv(
        BOARDEX_COMPANY, usecols=['boardid', 'boardname'], dtype=str,
        low_memory=False,
    ).dropna(subset=['boardid', 'boardname']).drop_duplicates('boardid')
    id_to_boardname = dict(zip(company_details['boardid'], company_details['boardname'].str.strip()))

    # Match normalized CompanyName values directly to BoardEx canonical boardname values.
    candidates = {}
    for name in company_details['boardname'].dropna().astype(str):
        key = normalize_company_name(name)
        if key in wanted and key:
            candidates.setdefault(key, set()).add(name.strip())
    lookup = {key: next(iter(names)) for key, names in candidates.items() if len(names) == 1}

    # Resolve remaining names through organization_composition.companyname -> companyid.
    remaining = wanted.difference(lookup)
    org_candidates = {}
    if remaining:
        for org_chunk in pd.read_csv(
            BOARDEX_ORG, usecols=['companyid', 'companyname'], dtype=str,
            chunksize=250_000, low_memory=False,
        ):
            org_chunk = org_chunk.dropna(subset=['companyid', 'companyname'])
            org_chunk['_company_key'] = org_chunk['companyname'].map(normalize_company_name)
            org_chunk = org_chunk[org_chunk['_company_key'].isin(remaining)].copy()
            if org_chunk.empty:
                continue
            org_chunk['BoardName'] = org_chunk['companyid'].map(id_to_boardname)
            org_chunk = org_chunk.dropna(subset=['BoardName'])
            for key, names in org_chunk.groupby('_company_key')['BoardName']:
                org_candidates.setdefault(key, set()).update(names.str.strip())

    lookup.update({key: next(iter(names)) for key, names in org_candidates.items()
                   if len(names) == 1})
    return lookup


company_lookup = build_companyname_boardname_lookup(mapping['CompanyName'])
mapping['company_key'] = mapping['CompanyName'].map(normalize_company_name)
company_matches = mapping['company_key'].map(company_lookup).fillna('')
missing_boardname = mapping['BoardName'].eq('')
mapping.loc[missing_boardname, 'BoardName'] = company_matches[missing_boardname]
print(
    f"  CompanyName → BoardEx matched {int((missing_boardname & company_matches.ne('')).sum()):,} "
    f"previously blank rows across "
    f"{mapping.loc[missing_boardname & company_matches.ne(''), 'LabelerName'].nunique():,} labelers"
)

mapping = mapping[mapping['LabelerName'] != ''].copy()
mapping['labeler_key'] = mapping['LabelerName'].str.upper()
mapping['is_curated'] = mapping['from_mapping'].eq('1')

# Prefer a single curated BoardEx match; otherwise accept only unambiguous matches.
# Ambiguous labelers are left unmatched instead of depending on CSV row order.
lb_to_bx = {}
ambiguous_labelers = 0
unmatched_labelers = 0
for key, group in mapping.groupby('labeler_key', sort=False):
    curated_names = group.loc[group['is_curated'], 'BoardName'].drop_duplicates().tolist()
    all_names = group['BoardName'].drop_duplicates().tolist()
    if len(curated_names) == 1:
        lb_to_bx[key] = curated_names[0]
    elif len(curated_names) > 1:
        ambiguous_labelers += 1
    elif len(all_names) == 1:
        lb_to_bx[key] = all_names[0]
    elif not all_names:
        unmatched_labelers += 1
    else:
        ambiguous_labelers += 1

lb_to_bx_lower = {k.lower(): v for k, v in lb_to_bx.items()}
print(
    f"  {len(lb_to_bx)} labelers mapped; {ambiguous_labelers} ambiguous and "
    f"{unmatched_labelers} unmatched"
)


def get_boardex(labeler):
    if pd.isna(labeler): return ''
    key = str(labeler).strip().upper()
    if key in lb_to_bx: return lb_to_bx[key]
    kl = key.lower()
    if kl in lb_to_bx_lower: return lb_to_bx_lower[kl]
    return ''


# ══════════════════════════════════════════════════════════════
# STEP 3: Specialty-tier map from Beneficiary Cost + Plan Info
# ══════════════════════════════════════════════════════════════
print("\n[3] Building specialty-tier mapping...")
spec_map_parts = []
specialty_consistency_parts = []
for qdir in sorted(os.listdir(COST_ROOT)):
    dpath = os.path.join(COST_ROOT, qdir)
    if not os.path.isdir(dpath): continue
    m = re.match(r'(\d{4})_Q(\d)', qdir)
    if not m: continue
    yq = f"{m.group(1)} Q{m.group(2)}"
    files = os.listdir(dpath)
    pi_file = next((os.path.join(dpath, f) for f in files
                    if f.lower().startswith('plan information') and f.endswith('.txt')), None)
    bc_file = next((os.path.join(dpath, f) for f in files
                    if 'beneficiary cost' in f.lower() and f.endswith('.txt')), None)
    if pi_file and bc_file:
        pi = pd.read_csv(pi_file, sep='|', dtype=str, encoding='latin-1',
                         usecols=['CONTRACT_ID', 'PLAN_ID', 'SEGMENT_ID', 'FORMULARY_ID'])
        pi = pi.drop_duplicates()
        bc = pd.read_csv(bc_file, sep='|', dtype=str, encoding='latin-1',
                         usecols=['CONTRACT_ID', 'PLAN_ID', 'SEGMENT_ID', 'TIER',
                                  'TIER_SPECIALTY_YN', 'COVERAGE_LEVEL'])
        bc = bc[bc['COVERAGE_LEVEL'].eq('1')].copy()
        bc['TIER'] = pd.to_numeric(bc['TIER'], errors='coerce')
        bc['is_specialty'] = bc['TIER_SPECIALTY_YN'].fillna('N').str.upper().eq('Y').astype('int8')
        quarter_map = bc.merge(
            pi, on=['CONTRACT_ID', 'PLAN_ID', 'SEGMENT_ID'], how='inner',
        )
        quarter_map = quarter_map.dropna(subset=['FORMULARY_ID', 'TIER'])
        quarter_map['YEAR_Q'] = yq
        plan_tier_map = quarter_map.groupby(
            ['YEAR_Q', 'FORMULARY_ID', 'TIER', 'CONTRACT_ID', 'PLAN_ID', 'SEGMENT_ID'],
            as_index=False,
        ).agg(is_specialty=('is_specialty', 'max'))
        consistency = plan_tier_map.groupby(
            ['YEAR_Q', 'FORMULARY_ID', 'TIER'], as_index=False,
        ).agg(
            n_matched_plans=('is_specialty', 'size'),
            n_specialty_plans=('is_specialty', 'sum'),
            n_flag_values=('is_specialty', 'nunique'),
        )
        consistency['n_non_specialty_plans'] = (
            consistency['n_matched_plans'] - consistency['n_specialty_plans']
        )
        specialty_consistency_parts.append(consistency)
        spec_map_parts.append(
            plan_tier_map[['YEAR_Q', 'FORMULARY_ID', 'TIER', 'is_specialty']]
        )

if not spec_map_parts:
    raise FileNotFoundError(
        f"No matching plan information and beneficiary cost files were found under {COST_ROOT}"
    )

spec_map = pd.concat(spec_map_parts, ignore_index=True).rename(columns={'TIER': 'tier_raw'})
# Resolve plan-level disagreements before merging to prevent row multiplication.
spec_map = spec_map.groupby(
    ['YEAR_Q', 'FORMULARY_ID', 'tier_raw'], as_index=False
).agg(is_specialty=('is_specialty', 'max'))
print(f"  Specialty-tier map: {len(spec_map):,} entries, {(spec_map['is_specialty'] == 1).sum():,} specialty tiers")

specialty_audit = pd.concat(specialty_consistency_parts, ignore_index=True)
specialty_audit = specialty_audit[specialty_audit['n_flag_values'].gt(1)].copy()
specialty_audit.to_csv(SPECIALTY_AUDIT_OUTPUT, index=False)
print(
    f"  Plan disagreements: {len(specialty_audit):,} formulary-quarter-tier keys; "
    f"saved {SPECIALTY_AUDIT_OUTPUT}"
)

# ══════════════════════════════════════════════════════════════
# STEP 4: Precompute global tier maxima using a narrow first pass
# ══════════════════════════════════════════════════════════════
chunk_size = 250000
print("\n[4] Scanning tier keys for global formulary-quarter maxima...")
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
# STEP 5: Process chunks and stream directly to a temporary output
# ══════════════════════════════════════════════════════════════
print("\n[5] Processing formulary panel in chunks...")
tmp_output = OUTPUT + '.tmp'
if os.path.exists(tmp_output):
    os.remove(tmp_output)

first_chunk = True
total_rows = 0
labeler_rows = 0
boardex_rows = 0
flag_totals = {'is_generic': 0, 'is_specialty': 0, 'is_brand': 0}
flag_combinations = {}

for chunk_num, chunk in enumerate(iter_formulary_chunks(FORMULARY_USECOLS, chunk_size)):
    total_rows += len(chunk)
    chunk = chunk.rename(columns={
        'TIER_LEVEL_VALUE': 'tier_raw',
        'PRIOR_AUTHORIZATION_YN': 'PA',
        'STEP_THERAPY_YN': 'ST',
        'QUANTITY_LIMIT_YN': 'QL',
    })

    for col in ['NDC', 'YEAR_Q', 'FORMULARY_ID']:
        chunk[col] = chunk[col].astype(str).str.strip()
    chunk['tier_raw'] = pd.to_numeric(chunk['tier_raw'], errors='coerce').astype('Int64')
    for col in ['PA', 'ST', 'QL']:
        chunk[col] = (
            chunk[col].astype(str).str.strip().str.upper()
            .map({'Y': 1, 'N': 0}).fillna(0).astype('int8')
        )

    # FDA product records use the 9-digit labeler-plus-product portion of NDC.
    ndc9 = chunk['NDC'].str.zfill(11).str[:9]
    chunk['LabelerName'] = ndc9.map(ndc9_map['LABELERNAME'])
    chunk['MARKETINGCATEGORYNAME'] = ndc9.map(ndc9_map['MARKETINGCATEGORYNAME'])
    chunk['ProprietaryName'] = ndc9.map(ndc9_map['PROPRIETARYNAME'])
    chunk['NonProprietaryName'] = ndc9.map(ndc9_map['NONPROPRIETARYNAME'])

    chunk['is_generic'] = (
        chunk['MARKETINGCATEGORYNAME'].str.upper().fillna('').str.contains('ANDA').astype('int8')
    )
    chunk = chunk.merge(spec_map, on=['YEAR_Q', 'FORMULARY_ID', 'tier_raw'], how='left')
    chunk['is_specialty'] = chunk['is_specialty'].fillna(0).astype('int8')
    has_fda_category = chunk['MARKETINGCATEGORYNAME'].notna()
    chunk['is_brand'] = (
        has_fda_category & (chunk['is_generic'] == 0) & (chunk['is_specialty'] == 0)
    ).astype('int8')
    chunk['BoardName'] = chunk['LabelerName'].map(get_boardex)
    chunk = chunk.merge(max_tier, on=['FORMULARY_ID', 'YEAR_Q'], how='left')

    keep = [
        'YEAR_Q', 'FORMULARY_ID', 'NDC', 'tier_raw', 'max_tier',
        'LabelerName', 'BoardName', 'MARKETINGCATEGORYNAME',
        'ProprietaryName', 'NonProprietaryName', 'is_generic',
        'is_specialty', 'is_brand', 'PA', 'ST', 'QL',
    ]
    chunk = chunk[keep]
    labeler_rows += int(chunk['LabelerName'].notna().sum())
    boardex_rows += int(chunk['BoardName'].fillna('').ne('').sum())
    for flag in flag_totals:
        flag_totals[flag] += int(chunk[flag].sum())
    combo_counts = chunk.groupby(['is_generic', 'is_specialty', 'is_brand']).size()
    for combo, count in combo_counts.items():
        flag_combinations[combo] = flag_combinations.get(combo, 0) + int(count)

    chunk.to_csv(
        tmp_output, mode='w' if first_chunk else 'a',
        header=first_chunk, index=False,
    )
    first_chunk = False
    if (chunk_num + 1) % 5 == 0:
        print(f"  Processed {total_rows:,} rows...")

if first_chunk:
    raise ValueError("The formulary input contains no data rows.")
os.replace(tmp_output, OUTPUT)

print(f"\n{'=' * 65}")
print("  FINAL PANEL SUMMARY")
print(f"{'=' * 65}")
print(f"  Total rows:                 {total_rows:>12,}")
print(f"  With Labeler:               {labeler_rows:>12,} ({labeler_rows/total_rows*100:.1f}%)")
print(f"  With BoardEx:               {boardex_rows:>12,} ({boardex_rows/total_rows*100:.1f}%)")
for flag, label in [('is_generic', 'Generic'), ('is_specialty', 'Specialty'), ('is_brand', 'Brand')]:
    print(f"  {label} rows: {flag_totals[flag]:>12,} ({flag_totals[flag]/total_rows*100:.1f}%)")
print("  Flag combinations (generic, specialty, brand):")
for combo, count in sorted(flag_combinations.items()):
    print(f"    {combo}: {count:,}")
sz = os.path.getsize(OUTPUT) / 1e9
print(f"  {OUTPUT}: {total_rows:,} rows, {sz:.2f} GB, {time.time() - t0:.0f}s")
print(f"{'=' * 65}")
