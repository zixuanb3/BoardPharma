"""Purpose: Infer event-specific membership and events from uncertain date bounds.
Input: Matched original IE/OC records, annual BoardEx membership and company maps.
Output: Certain/possible rosters, endpoint audits, confirmed movement candidates,
    firm-event uncertainty and certain/possible firm-interlock histories.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations, permutations
import json
from pathlib import Path

import numpy as np
import pandas as pd

NEG_INF = -100000
POS_INF = 100000


def bits_between(start: int, end: int, lower: int, upper: int) -> int:
    """Encode an inclusive interval without treating study limits as event dates."""
    start, end = max(int(start), lower), min(int(end), upper)
    if start > end:
        return 0
    return ((1 << (end - start + 1)) - 1) << (start - lower)


def positions(bits: int):
    """Iterate set bits in increasing order using integer operations."""
    while bits:
        one = bits & -bits
        yield one.bit_length() - 1
        bits ^= one


def carry_board(eligible: int, board: int) -> int:
    """Carry board identity forward through each uninterrupted eligible spell."""
    result = 0
    while eligible:
        start = (eligible & -eligible).bit_length() - 1
        shifted = eligible >> start
        length = ((shifted + 1) & ~shifted).bit_length() - 1
        run = ((1 << length) - 1) << start
        onset = board & run
        if onset:
            first = (onset & -onset).bit_length() - 1
            result |= run & ~((1 << first) - 1)
        eligible &= ~run
    return result


def endpoint_bounds(raw: pd.DataFrame, builder) -> pd.DataFrame:
    """Keep year-only dates as ranges and fully unknown endpoints as unbounded."""
    out = raw.copy().reset_index(drop=True)
    bounds = {}
    for side in ['start', 'end']:
        flag = pd.to_numeric(out[f'date{side}roleflag'], errors='coerce')
        point = builder.parse_period_series(out[f'date{side}role'])
        year = builder.parse_year_series(out[f'date{side}role'])
        low, high = point.astype(float).copy(), point.astype(float).copy()
        yearly = flag.isin(builder.YEAR_ONLY_FLAGS)
        low.loc[yearly], high.loc[yearly] = year.loc[yearly] * 4, year.loc[yearly] * 4 + 3
        unknown = flag.isin(builder.UNKNOWN_FLAGS)
        low.loc[unknown], high.loc[unknown] = NEG_INF, POS_INF
        if side == 'end':
            current = flag.eq(40)
            low.loc[current], high.loc[current] = builder.CUTOFF_PERIOD, POS_INF
        valid = flag.isin(builder.KNOWN_FLAGS) & low.notna() & high.notna()
        bounds[side] = (low, high, valid)
    sl, su, sv = bounds['start']
    el, eu, ev = bounds['end']
    # An existing, valid role implies start <= end. Tighten only bounds implied
    # by that constraint; never substitute a company-wide earliest/latest date.
    su = np.minimum(su, eu)
    el = np.maximum(el, sl)
    current = pd.to_numeric(out.dateendroleflag, errors='coerce').eq(40)
    su = pd.Series(su).where(~current, np.minimum(su, builder.CUTOFF_PERIOD))
    valid = sv & ev & pd.to_numeric(out.directorid, errors='coerce').notna() & sl.le(eu)
    valid &= ~current | sl.le(builder.CUTOFF_PERIOD)
    for column, value in [('start_lower', sl), ('start_upper', su), ('end_lower', el), ('end_upper', eu)]:
        out[column] = pd.Series(value).fillna(0).astype('int64')
    out['event_specific_valid'] = valid.astype('int8')
    out['date_rule'] = 'event_specific'
    out['start_imputed'] = 0
    out['end_censored'] = pd.to_numeric(out.dateendroleflag, errors='coerce').isin({40, 75, 80}).astype('int8')
    return out


def build_rosters(raw, bp, display, info, builder, output_dir: Path):
    """Build compact annual/quarterly states while preserving observed role gaps."""
    records = endpoint_bounds(raw, builder)
    records.to_parquet(output_dir / 'event_specific_date_rule_records.parquet', index=False)
    selected = records[records.event_specific_valid.eq(1) & records.role_key.ne('')].copy()
    selected['DirectorID'] = pd.to_numeric(selected.directorid).astype('int64')
    selected['leader_tier'] = [builder.classify_leader(*values) for values in
        selected[['rolename', 'brdposition', 'seniority']].itertuples(index=False, name=None)]
    selected['role_priority'] = selected.leader_tier.map(builder.TIER_PRIORITY).astype(int)
    baseline = bp[bp.inSSR.eq(1)][['DirectorID', 'BoardName', 'DirectorName', 'year']].copy()
    baseline['company_key'] = builder.normalize_series(baseline.BoardName)
    baseline['DirectorID'] = pd.to_numeric(baseline.DirectorID).astype('int64')
    baseline['year'] = pd.to_numeric(baseline.year).astype(int)
    baseline_groups = {key: group for key, group in baseline.groupby(['DirectorID', 'company_key'], sort=False)}
    groups = {key: group for key, group in selected.groupby(['DirectorID', 'company_key'], sort=False)}
    audit = []
    for frequency, suffix in [('year', 'year'), ('quarter', 'yearquarter')]:
        lower = builder.SAMPLE_START - 1 if frequency == 'year' else builder.SAMPLE_START * 4 - 1
        cutoff = builder.CUTOFF_YEAR if frequency == 'year' else builder.CUTOFF_PERIOD
        upper = cutoff + (4 if frequency == 'year' else 12)
        study_lower = builder.SAMPLE_START if frequency == 'year' else builder.SAMPLE_START * 4
        data, evidence = [], []
        for key in sorted(set(groups) | set(baseline_groups)):
            person, company = key
            roles = groups.get(key)
            board = baseline_groups.get(key)
            cp = {tier: 0 for tier in builder.TIER_PRIORITY}
            pp = dict(cp)
            emp_c = emp_p = 0
            source_roles = []
            if roles is not None:
                for item in roles.itertuples(index=False):
                    sl, su, el, eu = item.start_lower, item.start_upper, item.end_lower, item.end_upper
                    if frequency == 'year':
                        sl, su, el, eu = sl // 4, su // 4, el // 4, eu // 4
                    p = bits_between(sl, eu, lower, upper)
                    c = bits_between(su, el, lower, upper)
                    c &= bits_between(lower, cutoff, lower, upper)
                    assert not c & ~p
                    emp_c |= c
                    emp_p |= p
                    cp[item.leader_tier] |= c
                    pp[item.leader_tier] |= p
                    source_roles.append((item, c, p))
            baseline_bits = 0
            if board is not None:
                for year in sorted(set(board.year)):
                    first, last = (year, year) if frequency == 'year' else (year * 4, year * 4 + 3)
                    baseline_bits |= bits_between(first, last, lower, upper)
            # Keep T2's covered-period annual board labeling for known pairs.
            # Annual-only evidence is certain at the annual frequency, but is
            # possible (not certain) in an individual quarter without date support.
            baseline_p = baseline_bits & emp_p if roles is not None else baseline_bits
            baseline_c = (baseline_p if frequency == 'year' else baseline_bits & emp_c)
            baseline_c &= bits_between(lower, cutoff, lower, upper)
            pp['board'] |= baseline_p
            cp['board'] |= baseline_c
            eligible_p = pp['board'] | pp['csuite'] | pp['vp_tech_hr']
            eligible_c = cp['board'] | cp['csuite'] | cp['vp_tech_hr']
            narrow_p = carry_board(eligible_p, pp['board'])
            narrow_c = carry_board(eligible_c, cp['board'])
            assert not narrow_c & ~narrow_p
            if not eligible_p:
                continue
            name = (str(roles.DirectorName.iloc[0]) if roles is not None else str(board.DirectorName.iloc[0]))
            source_roles.sort(key=lambda value: value[0].role_priority, reverse=True)
            for index in positions(eligible_p):
                time_id = lower + index
                bit = 1 << index
                flags = {'membership_certain': int(bool(eligible_c & bit)), 'membership_possible': 1,
                         'narrow_certain': int(bool(narrow_c & bit)), 'narrow_possible': int(bool(narrow_p & bit)),
                         'medium_certain': int(bool((cp['board'] | cp['csuite']) & bit)),
                         'medium_possible': int(bool((pp['board'] | pp['csuite']) & bit)),
                         'broad_certain': int(bool(eligible_c & bit)), 'broad_possible': 1}
                evidence.append({'DirectorID': person, 'BoardName': display[company], 'time_id': time_id, **flags})
                if not study_lower <= time_id <= cutoff:
                    continue
                chosen = next((item for item, c, p in source_roles if p & bit and item.role_priority > 0), None)
                board_here = bool(pp['board'] & bit)
                tier = 'board' if board_here else (chosen.leader_tier if chosen is not None else 'excluded')
                year = time_id if frequency == 'year' else time_id // 4
                row = {'DirectorID': person, 'DirectorName': name, 'BoardName': display[company],
                       'company_key': company, 'CompanyID': info[company][1], 'HOCountryName': info[company][0],
                       'year': year, 'leader_tier': tier, 'role_priority': builder.TIER_PRIORITY[tier],
                       'board_continuity': int(bool(narrow_p & bit)), 'inSSR': 1,
                       'membership_state': 1 if flags['membership_certain'] else -1,
                       'date_rule': 'event_specific', 'start_imputed': 0,
                       'baseline_present': int(bool(baseline_bits & bit)), **flags}
                if frequency == 'quarter':
                    row.update(quarter=time_id % 4 + 1, yearquarter=f'{year}q{time_id % 4 + 1}')
                if chosen is not None:
                    for column in ['rolename', 'brdposition', 'seniority', 'source_dataset', 'mapping_source', 'RawCompanyID',
                                   'datestartrole', 'dateendrole', 'datestartroleflag', 'dateendroleflag',
                                   'start_lower', 'start_upper', 'end_lower', 'end_upper', 'end_censored']:
                        row[column] = getattr(chosen, column)
                else:
                    row.update(rolename='', brdposition='BOARD_BASELINE', seniority='', source_dataset='boardex_pharma',
                               mapping_source='boardex_pharma', RawCompanyID=-1, datestartrole='', dateendrole='',
                               datestartroleflag=np.nan, dateendroleflag=np.nan, start_lower=np.nan, start_upper=np.nan,
                               end_lower=np.nan, end_upper=np.nan, end_censored=0)
                data.append(row)
        roster = pd.DataFrame(data).sort_values(['BoardName', 'year'] + (['quarter'] if frequency == 'quarter' else []) + ['DirectorID']).reset_index(drop=True)
        # Only the ledger holds +/- infinity sentinels; exported date-bound
        # columns use missing values for genuinely unbounded endpoints.
        for column in ['start_lower', 'start_upper', 'end_lower', 'end_upper']:
            roster[column] = pd.to_numeric(roster[column], errors='coerce').replace({NEG_INF: np.nan, POS_INF: np.nan})
        variant = 'event_specific_' + suffix
        builder.validate_roster(roster, variant, len(display), frequency)
        builder.atomic_write_outputs(roster, 'ssr_company_roster_' + variant)
        evidence = pd.DataFrame(evidence)
        evidence.to_parquet(output_dir / f'{variant}_membership_evidence.parquet', index=False)
        audit.append({'variant': variant, 'rows': len(roster), 'certain_leadership_rows': int(roster.membership_certain.sum()),
                      'certain_narrow_rows': int(roster.narrow_certain.sum()), 'possible_narrow_rows': int(roster.narrow_possible.sum()),
                      'companies': int(roster.CompanyID.nunique()), 'directors': int(roster.DirectorID.nunique()),
                      'evidence_lower': lower, 'evidence_upper': upper, 'cutoff': cutoff})
        print(json.dumps(audit[-1]), flush=True)
    return audit


def persistent(bits: int, periods: int) -> int:
    """Return event starts with membership in every required future period."""
    result = bits
    for lag in range(1, periods):
        result &= bits >> lag
    return result


def transition_bounds(input_path, frequency, tier, lower, upper):
    """Require possible transitions to have a feasible underlying spell boundary.

    Marginal uncertain membership in two adjacent periods alone cannot imply a
    possible exit: a spell with a known future end cannot end arbitrarily early.
    These masks preserve that temporal restriction without point-imputing dates.
    """
    import build_ssr_roster_t2 as source
    ledger = pd.read_parquet(input_path.parent / 'event_specific_date_rule_records.parquet')
    ledger = ledger[ledger.event_specific_valid.eq(1) & ledger.role_key.ne('')].copy()
    ledger['DirectorID'] = pd.to_numeric(ledger.directorid).astype(int)
    ledger['tier'] = [source.classify_leader(*values) for values in ledger[['rolename', 'brdposition', 'seniority']].itertuples(index=False, name=None)]
    from pipeline_variant_config import INTERIM_DATA_PATH
    manifest_path = input_path.parent / 't2_build_manifest.json'
    baseline_path = INTERIM_DATA_PATH / 'boardex_pharma.dta'
    if manifest_path.exists():
        baseline_path = Path(json.loads(manifest_path.read_text(encoding='utf-8'))['source_files']['pharma']['path'])
    bp = pd.read_stata(baseline_path, columns=['DirectorID', 'BoardName', 'year', 'inSSR'])
    bp = bp[bp.inSSR.eq(1)].copy()
    bp['company_key'] = source.normalize_series(bp.BoardName)
    roles = {key: group for key, group in ledger.groupby(['DirectorID', 'company_key'], sort=False)}
    years = {key: set(group.year.astype(int)) for key, group in bp.groupby(['DirectorID', 'company_key'], sort=False)}
    allowed = {'board'} if tier == 'narrow' else ({'board', 'csuite'} if tier == 'medium' else {'board', 'csuite', 'vp_tech_hr'})
    full = (1 << (upper - lower + 1)) - 1
    result = {}
    for key in set(roles) | set(years):
        emp_p = emp_start = emp_exit = entry = exit_mask = 0
        group = roles.get(key)
        if group is not None:
            for row in group.itertuples(index=False):
                sl, su, el, eu = row.start_lower, row.start_upper, row.end_lower, row.end_upper
                if frequency == 'year':
                    sl, su, el, eu = sl // 4, su // 4, el // 4, eu // 4
                emp_p |= bits_between(sl, eu, lower, upper)
                onset = bits_between(sl, su, lower, upper)
                ending = (bits_between(el, eu, lower, upper) << 1) & full
                emp_start |= onset
                emp_exit |= ending
                if row.tier in allowed:
                    entry |= onset
                # Narrow board identity continues through later leadership
                # titles, so its exit requires a possible end of leadership.
                if row.tier in (allowed if tier != 'narrow' else {'board', 'csuite', 'vp_tech_hr'}):
                    exit_mask |= ending
        baseline = first = last = 0
        for year in years.get(key, set()):
            lo, hi = (year, year) if frequency == 'year' else (year * 4, year * 4 + 3)
            baseline |= bits_between(lo, hi, lower, upper)
            first |= bits_between(lo, lo, lower, upper)
            last |= bits_between(hi + 1, hi + 1, lower, upper)
        if group is None:
            # A year-only positive observation can begin/end within that year.
            entry |= baseline
            exit_mask |= (baseline << 1) & full
        else:
            entry |= (first & emp_p) | (emp_start & baseline)
            exit_mask |= (last & (emp_p << 1)) | (emp_exit & (baseline << 1))
        result[int(key[0]), str(key[1])] = (entry & full, exit_mask & full)
    return result


def build_movement(builder):
    """Infer confirmed/possible events without replacing unknown states by zero."""
    from pipeline_variant_config import personnel_output_dir
    variant = builder.input_path.stem.replace('ssr_company_roster_', '')
    root = personnel_output_dir(variant, builder.personnel_definition) / 'event_tables'
    root.mkdir(parents=True, exist_ok=True)
    path = builder.input_path.parent / f'{variant}_membership_evidence.parquet'
    tier = builder.personnel_definition
    evidence = pd.read_parquet(path, columns=['DirectorID', 'BoardName', 'time_id', tier + '_certain', tier + '_possible', 'membership_certain'])
    lower, upper = int(evidence.time_id.min()), int(evidence.time_id.max())
    cutoff = 2026 if builder.panel_level == 'year' else 2026 * 4 + 2
    start = 1990 if builder.panel_level == 'year' else 1990 * 4
    output_mask = bits_between(start, cutoff, lower, upper)
    histories = defaultdict(dict)
    eligible_certain = {}
    # Transition bounds use canonical company keys; roster display names map
    # one-to-one to the current T2 company universe.
    names = pd.read_csv(builder.input_path, usecols=['BoardName', 'company_key']).drop_duplicates()
    canonical = dict(zip(names.BoardName, names.company_key))
    boundaries = transition_bounds(builder.input_path, builder.panel_level, tier, lower, upper)
    for (person, firm), group in evidence.groupby(['DirectorID', 'BoardName'], sort=False):
        c = p = 0
        for time_id, certain, possible in group[['time_id', tier + '_certain', tier + '_possible']].itertuples(index=False, name=None):
            bit = 1 << (int(time_id) - lower)
            if possible:
                p |= bit
            if certain:
                c |= bit
        if p:
            histories[int(person)][str(firm)] = (c, p)
            eligible_certain[int(person), str(firm)] = sum(1 << (int(t) - lower) for t in
                group.loc[group.membership_certain.eq(1), 'time_id'])
    pair_c, pair_p = defaultdict(int), defaultdict(int)
    for boards in histories.values():
        for a, b in combinations(sorted(boards), 2):
            ca, pa = boards[a]
            cb, pb = boards[b]
            pair_c[a, b] |= ca & cb
            pair_p[a, b] |= pa & pb
    firm_states = defaultdict(lambda: [0, 0, 0, 0])
    rows = []

    def accumulate(event, side, firm, c, p, cr2, pr2):
        values = firm_states[event, side, firm]
        for j, mask in enumerate([c, p, cr2, pr2]):
            values[j] |= mask & output_mask

    def pair_state(c, p, index):
        if index < 0:
            return 0
        bit = 1 << index
        return 1 if c & bit else (-1 if p & bit else 0)

    def emit(event, person, a, b, mask, stay, req_a, req_b, pc, pp, lag=0):
        for i in positions(mask & output_mask):
            j = i + lag
            a_time, b_time = lower + i, lower + j
            row = {'event_type': event, 'DirectorID': person, 'FirmA': a, 'FirmB': b,
                   'time_id': a_time, 'year': a_time if builder.panel_level == 'year' else a_time // 4,
                   builder.stay_col: int(bool(stay & (1 << i))),
                   'FirmA_event_time_id': a_time, 'FirmB_event_time_id': b_time,
                   'requirement1_A': int(bool(req_a & (1 << i))), 'requirement1_B': int(bool(req_b & (1 << i))),
                   'event_evidence': 'confirmed', 'date_rule': 'event_specific'}
            row['requirement1'] = row['requirement1_A'] & row['requirement1_B']
            if builder.panel_level == 'quarter':
                row['quarter'] = a_time % 4 + 1
            for offset, label in [(-1, 't-1'), (0, 't'), (1, 't+1')]:
                row['pair_interlock_' + label] = pair_state(pc, pp, i + offset)
                row['pair_B_interlock_' + label] = pair_state(pc, pp, j + offset)
            rows.append(row)

    for person, boards in histories.items():
        if len(boards) < 2:
            continue
        for a, b in permutations(sorted(boards), 2):
            ca, pa = boards[a]
            cb, pb = boards[b]
            entry_b, exit_b = boundaries.get((person, canonical[b]), (0, 0))
            _, exit_a = boundaries.get((person, canonical[a]), (0, 0))
            if tier == 'narrow':
                exit_b &= ~eligible_certain[person, b]
                exit_a &= ~eligible_certain[person, a]
            pc, pp = pair_c[tuple(sorted((a, b)))], pair_p[tuple(sorted((a, b)))]
            cstay, pstay = persistent(cb, builder.forward_stay_periods), persistent(pb, builder.forward_stay_periods)
            # A remains; B enters. A confirmed entry also requires confirmed
            # absence in the preceding period, not merely a missing record.
            cs = ca & (ca << 1) & cb & ~(pb << 1)
            ps = pa & (pa << 1) & pb & ~(cb << 1) & entry_b
            r = ~(pp << 1)
            pr = ~(pc << 1)
            for side, firm in [('A', a), ('B', b)]:
                accumulate('to_B_still_in_A', side, firm, cs, ps, cs & cstay & r, ps & pstay & pr)
            emit('to_B_still_in_A', person, a, b, cs, cstay, r, r, pc, pp)
            # B leaves and A was jointly held in t-1. A can remain or leave;
            # REQ2 additionally requires no possible firm interlock at t,t+1.
            cd = (ca << 1) & (cb << 1) & ~pb
            pdiss = (pa << 1) & (pb << 1) & ~cb & exit_b
            rd = (pc << 1) & ~(pp | (pp >> 1))
            prd = (pp << 1) & ~(pc | (pc >> 1))
            for side, firm in [('A', a), ('B', b)]:
                accumulate('interlock_dissolution', side, firm, cd, pdiss, cd & rd, pdiss & prd)
            emit('interlock_dissolution', person, a, b, cd, cd, rd, rd, pc, pp)
            # A exits; B first enters within the existing allowed delay. All
            # intervening B absences must be confirmed for a confirmed move.
            cleft, pleft = (ca << 1) & ~pa, (pa << 1) & ~ca & exit_a
            delays = range(builder.delayed_entry_quarters + 1) if builder.panel_level == 'quarter' else range(1)
            for lag in delays:
                prior_p, prior_c = pb << 1, cb << 1
                for k in range(lag):
                    prior_p |= pb >> k
                    prior_c |= cb >> k
                cn = cleft & (cb >> lag) & ~prior_p
                pn = pleft & (pb >> lag) & ~prior_c & (entry_b >> lag)
                no_pair_c = ~(pc << 1 | pc | pc >> 1)
                no_pair_p = ~(pp << 1 | pp | pp >> 1)
                r_a, r_b = no_pair_p, no_pair_p >> lag
                pr_a, pr_b = no_pair_c, no_pair_c >> lag
                stay_c, stay_p = cstay >> lag, pstay >> lag
                accumulate('to_B_not_in_A', 'A', a, cn, pn, cn & stay_c & r_a, pn & stay_p & pr_a)
                accumulate('to_B_not_in_A', 'B', b, cn << lag, pn << lag,
                           (cn & stay_c & r_b) << lag, (pn & stay_p & pr_b) << lag)
                emit('to_B_not_in_A', person, a, b, cn, stay_c, r_a, r_b, pc, pp, lag)
    required = ['event_type', 'DirectorID', 'FirmA', 'FirmB', 'time_id', 'year', builder.stay_col,
                'FirmA_event_time_id', 'FirmB_event_time_id', 'requirement1_A', 'requirement1_B', 'requirement1',
                'pair_interlock_t-1', 'pair_interlock_t', 'pair_interlock_t+1', 'pair_B_interlock_t-1',
                'pair_B_interlock_t', 'pair_B_interlock_t+1', 'event_evidence', 'date_rule']
    if builder.panel_level == 'quarter':
        required.append('quarter')
    candidates = pd.DataFrame(rows, columns=required).drop_duplicates().sort_values(['event_type', 'DirectorID', 'time_id', 'FirmA', 'FirmB']).reset_index(drop=True)
    states = []
    for (event, side, firm), (c, p, cr2, pr2) in sorted(firm_states.items()):
        assert not c & ~p and not cr2 & ~pr2 and not cr2 & ~c
        for i in positions(p):
            bit = 1 << i
            time_id = lower + i
            row = {'event_type': event, 'firm_type': side, 'BoardName': firm, 'time_id': time_id,
                   'year': time_id if builder.panel_level == 'year' else time_id // 4,
                   'base_confirmed': int(bool(c & bit)), 'base_possible': 1,
                   'req2_confirmed': int(bool(cr2 & bit)), 'req2_possible': int(bool(pr2 & bit)),
                   'base_state': 1 if c & bit else -1,
                   'req2_state': 1 if cr2 & bit else (-1 if pr2 & bit else 0)}
            if builder.panel_level == 'quarter':
                row['quarter'] = time_id % 4 + 1
            states.append(row)
    pd.DataFrame(states).to_csv(root / 'firm_event_evidence.csv', index=False)
    edges = []
    possible_edges = []
    for (a, b), p in sorted(pair_p.items()):
        c = pair_c[a, b]
        for i in positions(p & output_mask):
            time_id = lower + i
            common = {'time_id': time_id, 'year': time_id if builder.panel_level == 'year' else time_id // 4}
            if builder.panel_level == 'quarter':
                common['quarter'] = time_id % 4 + 1
            for left, right in [(a, b), (b, a)]:
                row = {'BoardName': left, 'CounterpartBoard': right, **common}
                possible_edges.append({**row, 'interlock_certain': int(bool(c & (1 << i))), 'interlock_possible': 1})
                if c & (1 << i):
                    edges.append(row)
    pd.DataFrame(possible_edges).to_csv(root / 'firm_interlock_evidence.csv', index=False)
    edge_columns = ['BoardName', 'CounterpartBoard', 'time_id', 'year'] + (['quarter'] if builder.panel_level == 'quarter' else [])
    return candidates, pd.DataFrame(edges, columns=edge_columns)
