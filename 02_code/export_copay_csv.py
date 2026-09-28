"""Output aggregated copay data (both COST_TYPE_PREF 0 and 1) to CSV"""
import pandas as pd

df = pd.read_csv(r'D:\pharma\merged_beneficiary_cost.csv')

# Keep only COST_TYPE_PREF 0 and 1 (exclude type 2)
df = df[df['COST_TYPE_PREF'].isin([0, 1])].copy()

# Aggregate: mean COST_AMT_PREF per FID x YQ x TIER x CONTRACT x PLAN x SEGMENT (cost types 0 and 1)
agg = df.groupby(['FORMULARY_ID','YEAR_Q','TIER','CONTRACT_ID','PLAN_ID','SEGMENT_ID']).agg(
    avg_cost=('COST_AMT_PREF','mean'),
    n_rows=('COST_AMT_PREF','count'),
).reset_index()

# max_tier
agg['max_tier'] = agg.groupby(['FORMULARY_ID','YEAR_Q'])['TIER'].transform('max')

# Include both cost types (0 and 1) in the aggregated output
cp = agg.copy()
print(f"Rows: {len(cp):,}")
print(f"Unique FQ: {cp.groupby(['FORMULARY_ID','YEAR_Q']).ngroups:,}")
print(f"max_tier distribution:")
print(cp['max_tier'].value_counts().sort_index().to_string())
print(f"\nTier cost summary:")
for t in sorted(cp['TIER'].unique()):
    vals = cp[cp['TIER']==t]['avg_cost']
    print(f"  T{int(t)}: median=${vals.median():.0f}, mean=${vals.mean():.0f}, n={len(vals):,}")

out = r'D:\pharma\aggregated_copay_by_tier.csv'
cp.to_csv(out, index=False)
print(f"\nSaved: {out}")
