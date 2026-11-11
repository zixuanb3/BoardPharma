"""Run the new Imputed/Drop event studies on the original REQ2 cohort stacks.

Inputs: r1104a cohort manifest, cohort CSV files and estimation sample audit.
Outputs: Stata job files, relative-quarter estimates and a specification audit.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import subprocess

import numpy as np
import pandas as pd
from scipy.stats import norm


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parent / 'r1104a'
STATA = Path('C:/Program Files/Stata18/StataMP-64.exe')
MODES = ('event_quarter', 'window_all', 'window_any', 'quarterly')
JOB_KEYS = ['roster_variant', 'event', 'direction']
PERIODS = tuple(q for q in range(-4, 8) if q != -1)


def quarter_label(period):
    period = int(period)
    return f'{period // 4}q{period % 4 + 1}'


def job_sources():
    manifest = pd.read_csv(SOURCE / 'cohort_manifest.csv')
    manifest = manifest.rename(columns={'variant': 'roster_variant'})
    parsed = manifest.relative_path.str.extract(
        r'quarter-level_([AB])_without_[AB].*[\\/]([^\\/]+)_quarter_cohort_(\d{4}q[1-4])_balanced\.csv$')
    parsed.columns = ['direction', 'event', 'cohort_label']
    assert parsed.notna().all().all()
    manifest = pd.concat([manifest, parsed], axis=1)
    assert len(manifest) == 1760
    return list(manifest.groupby(JOB_KEYS, sort=True))


def prepare_stack(records, directory):
    """Read the verified source windows and align their four sharing labels."""
    frames = []
    expected_rows = 0
    keys = ['BoardName', 'product', 'year', 'quarter']
    for label, rows in records.groupby('cohort_label', sort=True):
        assert set(rows.overlap_mode) == set(MODES) and len(rows) == len(MODES)
        rows = rows.set_index('overlap_mode')
        indicator = f'event_{label}'
        for mode in MODES:
            record = rows.loc[mode]
            folder = 'cohort_data' if mode == 'event_quarter' else f'cohort_data_atc3_{mode}'
            path = SOURCE / 'p' / record.roster_variant / 'leader_tier_narrow' / folder / record.relative_path
            columns = [*keys, 'price1', 'price0', indicator, 'atc3_sharing']
            frame = pd.read_csv(path, usecols=columns, dtype={'BoardName': str, 'product': str})
            assert len(frame) == record.rows
            assert frame.atc3_sharing.isin([0, 1]).all()
            assert frame[indicator].isin([0, 1]).all()
            assert frame.loc[frame[indicator].eq(0), 'atc3_sharing'].eq(0).all()
            if mode == 'event_quarter':
                cohort = int(label[:4]) * 4 + int(label[-1]) - 1
                stack = frame.drop(columns='atc3_sharing').rename(columns={indicator: 'treated'})
                stack['cohort'] = cohort
                stack['relative_quarter'] = stack.year * 4 + stack.quarter - 1 - cohort
                expected_rows += int(record.rows)
                assert stack.treated.nunique() == 2, label
            else:
                pd.testing.assert_frame_equal(stack[keys + ['price1', 'price0', 'treated']],
                    frame.rename(columns={indicator: 'treated'})[keys + ['price1', 'price0', 'treated']])
            stack[f'share_{mode}'] = frame.atc3_sharing.astype('int8')
        frames.append(stack)
    data = pd.concat(frames, ignore_index=True)
    assert len(data) == expected_rows
    assert np.isfinite(data[['price1', 'price0']]).all().all()
    assert data[['price1', 'price0']].gt(0).all().all()
    data['firm'] = pd.factorize(data.BoardName, sort=True)[0].astype('int32') + 1
    data['unit_id'] = data.groupby(['firm', 'product', 'cohort'], sort=False).ngroup().astype('int32') + 1
    assert data.groupby('unit_id').treated.nunique().eq(1).all()
    data['product'] = pd.factorize(data['product'], sort=True)[0].astype('int32') + 1
    data[['firm', 'BoardName']].drop_duplicates().sort_values('firm').to_csv(directory / 'firm_ids.csv', index=False)
    data.drop(columns=['BoardName', 'year', 'quarter']).to_stata(directory / 'stack.dta', write_index=False, version=118)
    return {'rows': len(data), 'treated_rows': int(data.treated.sum()), 'cohorts': data.cohort.nunique()}


def execute(job_index, key, records, rerun=False):
    directory = ROOT / 'jobs' / f'{job_index:02d}'
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / 'done.txt'
    code_hash = hashlib.sha256((ROOT / 'event_study.do').read_bytes()).hexdigest()
    metadata_file = directory / 'job.json'
    metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
    if not rerun and marker.exists() and metadata.get('code_sha256') == code_hash:
        print(f'Reuse job {job_index:02d} {key}', flush=True)
        return directory
    marker.unlink(missing_ok=True)
    metadata = {**dict(zip(JOB_KEYS, key)), **prepare_stack(records, directory), 'code_sha256': code_hash}
    metadata_file.write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    temp = directory / 'tmp'
    temp.mkdir(exist_ok=True)
    env = {**os.environ, 'STATATMP': str(temp)}
    print(f'Stata job {job_index:02d} {key} ({metadata["rows"]:,} rows)', flush=True)
    with (directory / 'launcher.log').open('w', encoding='utf-8') as log:
        command = [str(STATA), '-b', '-e', 'do', str(ROOT / 'event_study.do')]
        subprocess.run(command, cwd=directory, env=env, stdout=log, stderr=subprocess.STDOUT,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=1800, check=True)
    if not marker.exists():
        raise RuntimeError(f'Stata did not complete. Inspect {directory / "event_study.log"}')
    print(f'Finished job {job_index:02d}', flush=True)
    return directory


def aggregate_csdid(directory, cohort_effects):
    """Pool ATT by treated drug count and cluster the influence functions by firm."""
    estimates = []
    for outcome, target in cohort_effects.groupby('outcome'):
        scores = []
        for cohort, rows in target.groupby('cohort'):
            rows = rows.set_index('relative_quarter').loc[list(PERIODS)]
            assert rows.index.is_unique and len(rows) == len(PERIODS)
            names = [f'_g5_{min(q + 5, 4)}_{max(q + 5, 4)}' for q in PERIODS]
            rif = pd.read_stata(directory / f'rif_{outcome}_{cohort}.dta', columns=['firm', *names], convert_categoricals=False)
            assert len(rif) == int(rows.n_treated.iloc[0] + rows.n_control.iloc[0])
            assert rif[names].notna().all().all()
            np.testing.assert_allclose(rif[names].mean().to_numpy(), rows.estimate.to_numpy(), rtol=1e-7, atol=1e-9)
            # Weight fixed sample shares. Center within each cohort, then sum
            # across cohorts by firm to account for repeated firms in the stacks.
            centered = (rif[names] - rows.estimate.to_numpy()) * rows.n_treated.to_numpy() / len(rif)
            centered.columns = PERIODS
            scores.append(centered.groupby(rif.firm).sum())
        totals = target.groupby('relative_quarter')[['n_treated', 'n_control']].sum()
        weighted = (target.estimate * target.n_treated).groupby(target.relative_quarter).sum() / totals.n_treated
        cluster_scores = pd.concat(scores).groupby(level=0).sum()
        clusters = len(cluster_scores)
        assert clusters > 1
        se = ((cluster_scores / totals.n_treated).pow(2).sum() * clusters / (clusters - 1)).pow(.5)
        for q in range(-4, 8):
            reference = q == -1
            estimate = 0. if reference else weighted[q]
            error = 0. if reference else se[q]
            counts = totals.loc[0 if reference else q]
            estimates.append({'outcome': outcome, 'overlap_mode': 'pooled', 'effect': 'overall',
                'relative_quarter': q, 'estimate': estimate, 'std_error': error,
                'p_value': 2 * norm.sf(abs(estimate / error)) if error > 0 else np.nan,
                'n_cohorts': target.cohort.nunique(), 'n_treated_drugs': int(counts.n_treated),
                'n_control_drugs': int(counts.n_control), 'n_firm_clusters': clusters,
                'status': 'reference' if reference else 'estimated'})
    return pd.DataFrame(estimates)


def publish(directories):
    hd_results, cs_results, cohort_results = [], [], []
    for directory in directories:
        meta = json.loads((directory / 'job.json').read_text())
        key = {name: meta[name] for name in JOB_KEYS}
        hd = pd.read_stata(directory / 'reghdfe.dta')
        cs = pd.read_stata(directory / 'csdid_cohort.dta')
        assert len(hd) == 2 * 13 * 12
        assert len(cs) == 2 * 11 * meta['cohorts']
        assert hd.n_obs.eq(meta['rows']).all()
        for _, block in cs.groupby('outcome'):
            assert (block.n_treated + block.n_control).sum() * 12 == meta['rows'] * 11
            assert block.n_treated.sum() * 12 == meta['treated_rows'] * 11
        cs_results.append(aggregate_csdid(directory, cs).assign(**key))
        hd_results.append(hd.assign(**key, n_cohorts=meta['cohorts']))
        cohort_results.append(cs.assign(**key, cohort_label=cs.cohort.map(quarter_label)))
    hdf = pd.concat(hd_results, ignore_index=True)
    csf = pd.concat(cs_results, ignore_index=True)
    for frame in (hdf, csf):
        assert frame.loc[frame.relative_quarter.eq(-1), 'status'].eq('reference').all()
        assert frame.loc[frame.relative_quarter.eq(-1), 'estimate'].eq(0).all()
        frame['reference_quarter'] = -1
    hdf.to_csv(ROOT / 'reghdfe_event_study.csv', index=False)
    csf.to_csv(ROOT / 'csdid_event_study.csv', index=False)
    pd.concat(cohort_results, ignore_index=True).to_csv(ROOT / 'csdid_cohort_estimates.csv', index=False)
    print(hdf.groupby(['effect', 'status']).size().to_string(), flush=True)
    print(f'Published {len(directories)} jobs, {len(hdf)} reghdfe rows and {len(csf)} pooled CSDID rows.', flush=True)


def verify():
    """Reconcile the original Stata sample and check all CSDID reference differences."""
    prior = pd.read_csv(SOURCE / 'estimation_sample_cohort_audit.csv')
    prior['direction'] = prior.panel_group.str.extract(r'quarter-level_([AB])_')[0]
    prior = prior[prior.model.eq('did') & prior.overlap_mode.eq('event_quarter')]
    prior = prior.set_index([*JOB_KEYS, 'outcome'])
    assert len(prior) == 48 and prior.index.is_unique
    checks = []
    for directory in sorted((ROOT / 'jobs').iterdir()):
        meta = json.loads((directory / 'job.json').read_text())
        key = tuple(meta[name] for name in JOB_KEYS)
        sample = pd.read_stata(directory / 'stack.dta')
        hd = pd.read_stata(directory / 'reghdfe.dta')
        cs = pd.read_stata(directory / 'csdid_cohort.dta')
        for outcome in ('price1', 'price0'):
            old = prior.loc[(*key, outcome)]
            assert int(old.treated_rows) == meta['treated_rows']
            assert int(old.control_rows) + int(old.treated_rows) == meta['rows']
            assert int(old.cohorts) == meta['cohorts']
            prices = np.sort(sample[outcome].to_numpy())
            index, remainder = divmod(len(prices) * 95, 100)
            cutoff = prices[index] if remainder else (prices[index - 1] + prices[index]) / 2
            values = np.log(sample[outcome].clip(upper=cutoff))
            means = sample.assign(log_price=values).groupby(['cohort', 'relative_quarter', 'treated']).log_price.mean().unstack()
            gap = means[1] - means[0]
            reference = gap.xs(-1, level='relative_quarter')
            reference_gap = gap - gap.index.get_level_values('cohort').map(reference)
            cohort_att = cs[cs.outcome.eq(outcome)].set_index(['cohort', 'relative_quarter']).estimate
            np.testing.assert_allclose(reference_gap.reindex(cohort_att.index), cohort_att, rtol=1e-7, atol=1e-9)
            models = hd[hd.outcome.eq(outcome)]
            assert models.n_obs.eq(meta['rows']).all()
            assert models[models.effect.eq('overall') & models.relative_quarter.ne(-1)].status.eq('estimated').all()
            checks.append({**dict(zip(JOB_KEYS, key)), 'outcome': outcome,
                'old_sample_matches': True, 'observations': meta['rows'], 'cohorts': meta['cohorts'],
                'max_csdid_reference_check_error': (reference_gap.reindex(cohort_att.index) - cohort_att).abs().max(),
                'unsupported_reghdfe_coefficients': int(models.status.eq('unsupported').sum())})
    assert len(checks) == 48
    pd.DataFrame(checks).to_csv(ROOT / 'sample_and_reference_audit.csv', index=False)
    print(f'Validated {len(checks)} samples and every cohort-level CSDID reference contrast.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--job', type=int, help='Run one job for inspection. The full run publishes the combined files.')
    parser.add_argument('--verify', action='store_true', help='Validate the published run from saved job files.')
    parser.add_argument('--rerun', action='store_true', help='Rebuild every selected stack and re-estimate its models.')
    args = parser.parse_args()
    if args.verify:
        verify()
        return
    jobs = list(enumerate(job_sources(), 1))
    assert len(jobs) == 24
    if args.job is not None:
        index, (key, records) = jobs[args.job - 1]
        directory = execute(index, key, records, args.rerun)
        cohort_effects = pd.read_stata(directory / 'csdid_cohort.dta')
        print(aggregate_csdid(directory, cohort_effects).head().to_string(index=False), flush=True)
        return
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(execute, index, key, records, args.rerun): index for index, (key, records) in jobs}
        directories = [future.result() for future in as_completed(futures)]
    publish(sorted(directories))
    verify()


if __name__ == '__main__':
    main()
