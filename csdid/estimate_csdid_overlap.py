"""Estimate pooled and ATC3-overlap CSDID effects from the archived T2 stacks.

Input: r1110 job stacks and the original pooled csdid_event_study.csv.
Output: csdid_overlap_event_study.csv with firm-clustered standard errors.
"""

import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd
from scipy.stats import norm

from run_event_study import JOB_KEYS, MODES, PERIODS, ROOT, STATA


def stack_data(stack):
    """Arrange each balanced firm-product panel in relative-quarter order."""
    stack = stack.sort_values(['unit_id', 'relative_quarter'])
    quarters = np.arange(-4, 8)
    assert np.array_equal(stack.relative_quarter.to_numpy().reshape(-1, 12),
                          np.tile(quarters, (len(stack) // 12, 1)))
    units = stack.iloc[::12]
    groups = units[['cohort', 'firm', 'treated']].reset_index(drop=True)
    flags = {mode: stack[f'share_{mode}'].to_numpy().reshape(-1, 12).astype(bool)
             for mode in MODES}
    for mode in MODES[:-1]:
        assert (flags[mode] == flags[mode][:, [0]]).all()
    prices = {}
    for outcome in ('price1', 'price0'):
        values = stack[outcome].to_numpy()
        assert np.isfinite(values).all() and (values > 0).all()
        # Match Stata summarize, detail at the 95th percentile.
        ordered = np.sort(values)
        index, remainder = divmod(len(values) * 95, 100)
        cutoff = ordered[index] if remainder else ordered[index - 1:index + 1].mean()
        prices[outcome] = np.log(np.minimum(values, cutoff)).reshape(-1, 12)
    return groups, flags, prices


def contrast(groups, changes, treated):
    """Fit the intercept-only outcome-regression ATT and pool cohort scores."""
    cohort, firm = groups.cohort.to_numpy(), groups.firm.to_numpy()
    controls = groups.treated.eq(0).to_numpy()
    included = treated | controls
    # Every retained cohort must have at least one overlapping treated product.
    supported = np.unique(cohort[treated])
    if not len(supported):
        return 0, 0, 0, 0, np.nan, np.nan
    use = included & np.isin(cohort, supported)
    data = pd.DataFrame({'cohort': cohort[use], 'firm': firm[use],
                         'treated': treated[use], 'change': changes[use]})
    by_group = data.groupby(['cohort', 'treated']).change
    stats = by_group.agg(['mean', 'size']).unstack()
    assert stats.notna().all().all()
    nt = stats['size'][True]
    nc = stats['size'][False]
    total = nt.sum()
    att = ((stats['mean'][True] - stats['mean'][False]) * nt).sum() / total
    residual = data.change - by_group.transform('mean')
    # Within-cohort ATT influence scores multiplied by the cohort's treated count.
    # This matches the original pool's fixed cohort weights and firm correction.
    weight = np.where(data.treated, 1, -data.cohort.map(nt / nc))
    firm_score = (residual * weight).groupby(data.firm).sum() / total
    clusters = len(firm_score)
    assert clusters > 1
    se = np.sqrt(firm_score.pow(2).sum() * clusters / (clusters - 1))
    return len(stats), total, nc.sum(), clusters, att, se


def estimate(directory):
    """Use the same units at each target quarter and at reference quarter -1."""
    metadata = json.loads((directory / 'job.json').read_text())
    keys = {key: metadata[key] for key in JOB_KEYS}
    stack = pd.read_stata(directory / 'stack.dta', convert_categoricals=False)
    groups, flags, prices = stack_data(stack)
    treated = groups.treated.eq(1).to_numpy()
    records = []
    for outcome, price in prices.items():
        for mode in ('pooled', *MODES):
            for q in range(-4, 8):
                reference = q == -1
                # For quarterly, choose the treated subgroup using the target
                # quarter's observed label and hold it fixed for this 2x2 contrast.
                selected = treated if mode == 'pooled' else treated & flags[mode][:, q + 4]
                ncohort, nt, nc, nfirm, estimate, se = contrast(
                    groups, price[:, q + 4] - price[:, 3], selected)
                status = 'estimated' if ncohort else 'unsupported'
                if reference:
                    estimate = se = 0.
                    status = 'reference'
                records.append({**keys, 'outcome': outcome, 'overlap_mode': mode,
                                'effect': 'overall' if mode == 'pooled' else 'share',
                                'relative_quarter': q, 'reference_quarter': -1,
                                'estimate': estimate, 'std_error': se,
                                'p_value': 2 * norm.sf(abs(estimate / se)) if se > 0 else np.nan,
                                'n_cohorts': ncohort, 'n_treated_drugs': nt,
                                'n_control_drugs': nc, 'n_firm_clusters': nfirm, 'status': status})
    return pd.DataFrame(records)


def verify_stata(directory):
    """Check actual Stata CSDID estimates and RIFs for fixed and changing labels."""
    stack = pd.read_stata(directory / 'stack.dta', convert_categoricals=False)
    groups, flags, prices = stack_data(stack)
    # Use a cohort with support for all four modes and with a changing label.
    treated = groups.treated.eq(1).to_numpy()
    candidates = groups.cohort[treated & np.any(flags['quarterly'] != flags['quarterly'][:, [3]], axis=1)]
    for cohort in candidates.unique():
        in_cohort = groups.cohort.eq(cohort).to_numpy()
        if all((in_cohort & treated & flags[mode][:, 3]).any() for mode in MODES):
            break
    else:
        raise ValueError('No verification cohort with support across modes.')
    sample = stack[stack.cohort.eq(cohort)].copy()
    sample['study_time'] = sample.relative_quarter + 5
    sample['adoption'] = 5 * sample.treated
    for outcome, price in prices.items():
        sample[f'log_{outcome}'] = price[in_cohort].ravel()
    checks = []
    commands = ['version 18', 'set more off', 'set processors 1', 'use validation.dta, clear']
    for outcome in prices:
        for mode in MODES:
            for q in ((-4, -2, 0, 3, 7) if mode == 'quarterly' else (3,)):
                selected = treated & flags[mode][:, q + 4] & in_cohort
                if not selected.any():
                    continue
                number = len(checks)
                checks.append((number, outcome, q, selected))
                commands.extend([
                    'preserve',
                    f'bysort unit_id: egen byte overlap = max(share_{mode} * (relative_quarter == {q}))',
                    'keep if !treated | overlap',
                    f'csdid log_{outcome}, ivar(unit_id) time(study_time) gvar(adoption) method(reg) long2 cluster(firm) saverif(rif_{number}) replace',
                    'restore',
                ])
    commands.extend(['file open marker using done.txt, write replace', 'file close marker'])
    with TemporaryDirectory(dir=ROOT / 'tmp') as scratch:
        scratch = Path(scratch)
        sample.to_stata(scratch / 'validation.dta', write_index=False, version=118)
        dofile = scratch / 'validate_overlap.do'
        dofile.write_text('\n'.join(commands) + '\n', encoding='ascii')
        subprocess.run([str(STATA), '-b', '-e', 'do', dofile.name], cwd=scratch,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=120, check=True)
        if not (scratch / 'done.txt').exists():
            raise RuntimeError((scratch / 'validate_overlap.log').read_text(errors='replace')[-7000:])
        for number, outcome, q, selected in checks:
            rif = pd.read_stata(scratch / f'rif_{number}.dta', convert_categoricals=False)
            name = f'_g5_{min(q + 5, 4)}_{max(q + 5, 4)}'
            direct_att = rif[name].mean()
            score = (rif[name] - direct_att).groupby(rif.firm).sum() / len(rif)
            direct_se = np.sqrt(score.pow(2).sum() * len(score) / (len(score) - 1))
            change = prices[outcome][:, q + 4] - prices[outcome][:, 3]
            *_, att, se = contrast(groups[in_cohort].reset_index(drop=True),
                                   change[in_cohort], selected[in_cohort])
            np.testing.assert_allclose([att, se], [direct_att, direct_se], rtol=1e-7, atol=1e-9)
    print(f'Validated {len(checks)} overlap ATT and standard errors against Stata csdid.', flush=True)


def main():
    jobs = sorted((ROOT / 'jobs').glob('*/stack.dta'))
    assert len(jobs) == 24
    result = pd.concat([estimate(path.parent) for path in jobs], ignore_index=True)
    # Reproduce every original pooled estimate, standard error and sample count.
    original = pd.read_csv(ROOT / 'csdid_event_study.csv')
    keys = [*JOB_KEYS, 'outcome', 'relative_quarter']
    columns = ['estimate', 'std_error', 'n_cohorts', 'n_treated_drugs',
               'n_control_drugs', 'n_firm_clusters']
    pooled = result[result.effect.eq('overall')].set_index(keys).sort_index()
    expected = original.set_index(keys).sort_index()
    assert pooled.index.equals(expected.index)
    np.testing.assert_allclose(pooled[columns], expected[columns], rtol=1e-7, atol=1e-9)
    print(f'Reproduced all {len(expected)} pooled estimates and standard errors.', flush=True)
    verify_stata(jobs[0].parent)
    overlap = result[result.effect.eq('share')]
    target = ROOT / 'csdid_overlap_event_study.csv'
    staged = target.with_suffix('.tmp')
    overlap.to_csv(staged, index=False)
    staged.replace(target)
    print(overlap.groupby(['overlap_mode', 'status']).size().to_string(), flush=True)


if __name__ == '__main__':
    main()

