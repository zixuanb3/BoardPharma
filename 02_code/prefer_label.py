r"""
Purpose: From copay_avg_by_plan_tier.csv, for plans with max_tier==5 or 6,
         classify preferred/non-preferred tiers based on max copay jump.
         Reference: pref_nonpref_analysis.py
Input:  D:\pharma\copay_avg_by_plan_tier.csv
Output: D:\pharma\copay_avg_with_prefer.csv
"""
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

# ============================================================
# Load
# ============================================================
df = pd.read_csv(r"D:\pharma\copay_avg_by_plan_tier.csv")
print(f"Input rows: {len(df):,}")

# Compute max_tier per plan-quarter (CONTRACT x PLAN x SEGMENT x YEAR_Q)
df['max_tier'] = df.groupby(['CONTRACT_ID','PLAN_ID','SEGMENT_ID','YEAR_Q'])['TIER'].transform('max')

# Stats on max_tier
mt_counts = df.drop_duplicates(['CONTRACT_ID','PLAN_ID','SEGMENT_ID','YEAR_Q'])['max_tier'].value_counts().sort_index()
print(f"Plan-quarters by max_tier:")
for mt, n in mt_counts.items():
    print(f"  max_tier={int(mt)}: {n:,} plan-quarters")

# Filter: max_tier == 5 or 6
df_sub = df[df['max_tier'].isin([5, 6])].copy()
plan_q_56 = df_sub.groupby(['CONTRACT_ID','PLAN_ID','SEGMENT_ID','YEAR_Q']).ngroups
print(f"\nFiltered to max_tier=5/6: {len(df_sub):,} rows, {plan_q_56:,} unique plan-quarters")

# ============================================================
# For each plan-quarter, find max copay jump → preferred/non-preferred boundary
# ============================================================
results = []

for (cid, pid, sid, yq), grp in df_sub.groupby(['CONTRACT_ID', 'PLAN_ID', 'SEGMENT_ID', 'YEAR_Q']):
    tiers = sorted(grp['TIER'].unique())
    if len(tiers) < 2:
        continue

    # AVG_COPAY_AMT per tier
    tier_costs = {}
    for t in tiers:
        vals = grp[grp['TIER'] == t]['AVG_COPAY_AMT']
        if len(vals) > 0:
            tier_costs[t] = vals.iloc[0]

    if len(tier_costs) < 2:
        continue

    # Find max jump between consecutive tiers
    max_jump_val = -np.inf
    max_jump_lower = None
    max_jump_upper = None
    sorted_tiers = sorted(tier_costs.keys())
    for i in range(len(sorted_tiers) - 1):
        a, b = sorted_tiers[i], sorted_tiers[i + 1]
        jump = tier_costs[b] - tier_costs[a]
        if jump > max_jump_val:
            max_jump_val = jump
            max_jump_lower = a
            max_jump_upper = b

    if max_jump_lower is None:
        continue

    mt = grp['max_tier'].iloc[0]

    # Classify: tiers <= lower_bound = preferred, tiers >= upper_bound = non-preferred
    pref_tiers = [t for t in sorted_tiers if t <= max_jump_lower]
    nonpref_tiers = [t for t in sorted_tiers if t >= max_jump_upper]

    results.append({
        'CONTRACT_ID': cid,
        'PLAN_ID': pid,
        'SEGMENT_ID': sid,
        'YEAR_Q': yq,
        'max_tier': int(mt),
        'max_jump': f'T{int(max_jump_lower)}->T{int(max_jump_upper)}',
        'max_jump_val': round(max_jump_val, 2),
        'lower_bound': int(max_jump_lower),
        'upper_bound': int(max_jump_upper),
        'pref_tiers': ','.join([f'T{int(t)}' for t in pref_tiers]),
        'nonpref_tiers': ','.join([f'T{int(t)}' for t in nonpref_tiers]),
    })

df_boundary = pd.DataFrame(results)
print(f"Plan-quarters with valid jump: {len(df_boundary):,}")

# ============================================================
# Merge boundary info back and generate prefer variable
# ============================================================
df_out = df_sub.merge(df_boundary[['CONTRACT_ID','PLAN_ID','SEGMENT_ID','YEAR_Q',
                                    'lower_bound','upper_bound','max_jump','max_jump_val',
                                    'pref_tiers','nonpref_tiers']],
                       on=['CONTRACT_ID','PLAN_ID','SEGMENT_ID','YEAR_Q'], how='left')

# Generate prefer variable: 1=preferred, 0=non-preferred
df_out['prefer'] = np.where(df_out['TIER'] <= df_out['lower_bound'], 1, 0)

# ============================================================
# Summary
# ============================================================
print(f"\n{'='*60}")
print(f"RESULT SUMMARY")
print(f"{'='*60}")
print(f"Total rows:           {len(df_out):>10,}")
print(f"prefer=1 (preferred): {(df_out['prefer']==1).sum():>10,} ({(df_out['prefer']==1).mean()*100:.0f}%)")
print(f"prefer=0 (non-pref):  {(df_out['prefer']==0).sum():>10,} ({(df_out['prefer']==0).mean()*100:.0f}%)")

print(f"\nMax jump distribution:")
vc = df_boundary['max_jump'].value_counts()
for k, v in vc.items():
    print(f"  {k}: {v:>6,} ({v/len(df_boundary)*100:.0f}%)")

print(f"\nprefer ratio by max_tier:")
for mt in [5, 6]:
    sub = df_out[df_out['max_tier'] == mt]
    if len(sub) > 0:
        n_pref = (sub['prefer'] == 1).sum()
        pct = (sub['prefer'] == 1).mean() * 100
        print(f"  max_tier={mt}: prefer=1 → {n_pref:,} / {len(sub):,} = {pct:.0f}%")

print(f"\nExample plan-quarters:")
for _, r in df_boundary.head(10).iterrows():
    print(f"  {r['CONTRACT_ID']}-{r['PLAN_ID']}-{r['SEGMENT_ID']} | {r['YEAR_Q']} | "
          f"max_tier={r['max_tier']} | jump={r['max_jump']} (${r['max_jump_val']}) | "
          f"pref={r['pref_tiers']} | nonpref={r['nonpref_tiers']}")

# ============================================================
# Save
# ============================================================
output_path = r"D:\pharma\copay_avg_with_prefer.csv"
df_out.to_csv(output_path, index=False)
print(f"\nSaved: {output_path}")
