"""Run T2 reghdfe and CSDID event studies for the Event-specific date policy.

Input: Archived r1028c REQ2 cohort files and the existing r1110 Stata specification.
Output: Isolated Stata jobs, pooled and overlap coefficients, and a sample audit.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from shutil import copyfile

import numpy as np
import pandas as pd

import estimate_csdid_overlap as overlap
import run_event_study as base


HERE = Path(__file__).resolve().parent
OUTPUT = HERE / 'event_specific'
ARCHIVE = HERE.parent / 'r1028c'
VARIANTS = ('event_specific_year', 'event_specific_yearquarter')
PATH_PATTERN = (
    r'quarter-level_([AB])_without_[AB].*[\\/]([^\\/]+)_quarter_cohort_(\d{4}q[1-4])_balanced\.csv$'
)


def source_jobs():
    """Index the two archived Event-specific rosters without rebuilding them."""
    manifest = pd.read_csv(ARCHIVE / 'cohort_manifest.csv').rename(columns={'variant': 'roster_variant'})
    manifest = manifest[manifest.roster_variant.isin(VARIANTS)]
    parsed = manifest.relative_path.str.extract(PATH_PATTERN)
    parsed.columns = ['direction', 'event', 'cohort_label']
    assert parsed.notna().all().all()
    manifest = pd.concat([manifest, parsed], axis=1)
    assert not manifest.duplicated([*base.JOB_KEYS, 'cohort_label', 'overlap_mode']).any()
    jobs = list(manifest.groupby(base.JOB_KEYS, sort=True))
    assert len(jobs) == 12
    return jobs


def audit_and_publish_overlap(directories):
    """Check samples against r1028c and overlap machinery against Stata output."""
    prior = pd.read_csv(ARCHIVE / 'estimation_sample_cohort_audit.csv')
    prior = prior[prior.model.eq('did') & prior.overlap_mode.eq('event_quarter')].copy()
    prior['direction'] = prior.panel_group.str.extract(r'quarter-level_([AB])_')[0]
    keys = [*base.JOB_KEYS, 'outcome']
    prior = prior.set_index(keys).sort_index()
    actual = pd.read_csv(OUTPUT / 'csdid_event_study.csv')
    reference = actual[actual.relative_quarter.eq(-1)].set_index(keys).sort_index()
    assert reference.index.equals(prior.index) and len(reference) == 24
    assert reference.reference_quarter.eq(-1).all() and reference.estimate.eq(0).all()
    pd.testing.assert_series_equal(reference.n_treated_drugs * 12, prior.treated_rows, check_names=False)
    pd.testing.assert_series_equal(reference.n_control_drugs * 12, prior.control_rows, check_names=False)
    pd.testing.assert_series_equal(reference.n_cohorts, prior.cohorts, check_names=False)

    estimates = pd.concat([overlap.estimate(directory) for directory in directories], ignore_index=True)
    term_keys = [*keys, 'relative_quarter']
    columns = ['estimate', 'std_error', 'n_cohorts', 'n_treated_drugs',
               'n_control_drugs', 'n_firm_clusters']
    pooled = estimates[estimates.effect.eq('overall')].set_index(term_keys).sort_index()
    original = actual.set_index(term_keys).sort_index()
    assert pooled.index.equals(original.index)
    np.testing.assert_allclose(pooled[columns], original[columns], rtol=1e-7, atol=1e-9)
    for variant in VARIANTS:
        # Cross-check fixed and varying overlap groups against Stata for both rosters.
        directory = next(path for path in directories if
                         pd.read_json(path / 'job.json', typ='series').roster_variant == variant)
        overlap.verify_stata(directory)

    share = estimates[estimates.effect.eq('share')]
    assert len(share) == 12 * 2 * 4 * 12
    hd = pd.read_csv(OUTPUT / 'reghdfe_event_study.csv')
    assert len(hd) == 12 * 2 * 13 * 12
    assert hd.loc[hd.effect.eq('overall') & hd.relative_quarter.ne(-1), 'status'].eq('estimated').all()
    assert hd.loc[hd.relative_quarter.eq(-1), 'estimate'].eq(0).all()
    expected_rows = (prior.treated_rows + prior.control_rows).reindex(
        pd.MultiIndex.from_frame(hd[keys]))
    np.testing.assert_array_equal(hd.n_obs, expected_rows)
    share.to_csv(OUTPUT / 'csdid_overlap_event_study.csv', index=False)
    audit = prior[['treated_rows', 'control_rows', 'cohorts']].copy()
    audit['sample_matches_archive'] = True
    audit['max_pooled_att_error'] = (pooled.estimate - original.estimate).abs().groupby(keys).max()
    audit['max_pooled_se_error'] = (pooled.std_error - original.std_error).abs().groupby(keys).max()
    audit['unsupported_reghdfe'] = hd.status.eq('unsupported').groupby([hd[k] for k in keys]).sum()
    audit['unsupported_csdid_overlap'] = share.status.eq('unsupported').groupby(
        [share[k] for k in keys]).sum()
    audit.to_csv(OUTPUT / 'sample_and_reference_audit.csv')
    print(f'Verified {len(audit)} samples against r1028c and {len(original)} pooled CSDID estimates.', flush=True)
    print(share.groupby(['overlap_mode', 'status']).size().to_string(), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workers', type=int, default=4)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--rerun', action='store_true')
    mode.add_argument('--retry-incomplete', action='store_true',
                      help='Reuse completed jobs and run incomplete jobs serially in an isolated workspace.')
    args = parser.parse_args()

    # Redirect the existing runner within this process only. Preserve the old
    # programs, coefficients, and jobs, and snapshot the shared Stata input.
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / 'tmp').mkdir(exist_ok=True)
    copyfile(HERE / 'event_study.do', OUTPUT / 'event_study.do')
    base.SOURCE, base.ROOT = ARCHIVE, OUTPUT
    overlap.ROOT = OUTPUT
    jobs = list(enumerate(source_jobs(), 1))
    if args.retry_incomplete:
        # A previous Stata process can still hold a job log or data file open.
        # Isolate retries so they never write into the original job directory.
        retry = OUTPUT / 'retry'
        retry.mkdir(exist_ok=True)
        copyfile(HERE / 'event_study.do', retry / 'event_study.do')
        directories = []
        for index, (key, records) in jobs:
            original = OUTPUT / 'jobs' / f'{index:02d}'
            if (original / 'done.txt').exists():
                directories.append(original)
            else:
                base.ROOT = retry
                directories.append(base.execute(index, key, records))
        base.ROOT = OUTPUT
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(base.execute, index, key, records, args.rerun)
                       for index, (key, records) in jobs]
            directories = [future.result() for future in futures]
    base.publish(directories)
    audit_and_publish_overlap(directories)
    manifest = OUTPUT / 'completed_job_directories.json'
    manifest.write_text(json.dumps([str(path.relative_to(OUTPUT)) for path in directories], indent=2),
                        encoding='utf-8')



if __name__ == '__main__':
    main()

