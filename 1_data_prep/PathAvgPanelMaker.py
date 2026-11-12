r"""
Purpose:
Build complete-history NDC-company-quarter formulary-path averages for
quarterly event cohorts. The script never samples histories and never writes
history-level path panels.

Process:
1. Reuse the stable quarterly cohort, formulary-history, and benefit helpers
   from PlanPanelMaker without modifying that module.
2. Apply one cohort-specific first-seen cutoff to both the effective
   treated-share NDC set and every final output row.
3. When no_spillover=1, classify paths separately by event, cohort, and A/B
   direction. Treated-share observations use paths that ever include an
   effective treated-share NDC; treated-nonshare and control observations use
   paths that never include one.
4. Calculate included, tier_raw, and avg_copay_amt as CPS-weighted and
   equal-history means over complete paths, then append quarters using a
   checkpoint that rejects rather than deletes incompatible output files.

Input:
- D:/pharma/formulary/beneficiary_cost_with_copay.csv
- data/formulary_panel_quarter_by_time[/shift_q1]/
  formulary_panel_YYYYQX.csv
- data/formulary_metadata/ndc_first_seen_quarter[_shift_q1].csv
- data/event_tables/movement_table_formulary_quarter_narrow.csv
- data/event_tables/movement_event_candidates_formulary_quarter_narrow.csv

Output:
- no_spillover=0:
  data/formulary_path_cohort_data_quarter/event/req1/Not/{shift}/plan/
  seen_y{offset}_event_q/path_average/
  {event}_path_quarter_cohort_YYYYQX.csv
- no_spillover=1:
  data/formulary_path_cohort_data_quarter/event/req1/Not/{shift}/plan/
  seen_y{offset}_event_q/path_average_no_spillover/{A|B}/
  {event}_path_quarter_cohort_YYYYQX.csv
"""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
from tqdm.auto import tqdm

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import PlanPanelMaker as plan


# ========================== USER CONFIG ==========================
# level:
# - The only allowed value is "plan". One analysis unit is one CPS
#   (CONTRACT_ID x PLAN_ID x SEGMENT_ID).
# - "state" and "county" are not implemented for quarterly path averages and
#   will raise an error. Do not change this setting under the current design.
#
# formulary_time_shift_quarters:
# - 0 reads the unshifted quarterly formulary panel.
# - 1 reads shift_q1 and uses matching shifted benefit and first-seen inputs.
#
# first_seen_year_offset:
# - Allowed values are -1, 0, and 1. An NDC must first appear no later than the
#   cohort's event quarter in event year plus this offset. At the left data
#   boundary, the cutoff is clipped to the first available cohort quarter.
# - This value is embedded in the output directory name and must match the
#   identifier selected in the downstream Stata do-file.
#
# no_spillover:
# - 0 averages every eligible complete path and writes one combined A/B file.
# - 1 constructs separate A/B ever/never path pools and writes direction-
#   specific files. It is independent of the downstream Stata exclude option.
#
# chunksize:
# - Number of raw CSV rows read per chunk. This controls memory only; it never
#   samples observations or changes the statistical population.
#
# Fixed design choices are intentionally not configuration parameters: all
# supported movement events are built, cohort windows are t-4 through t+7,
# and outcomes are included, tier_raw, and avg_copay_amt with both weighted
# and equal-history means.
RUN_CONFIG = {
    "level": "plan",
    "formulary_time_shift_quarters": 1,
    "first_seen_year_offset": -1,
    "no_spillover": 1,
    "chunksize": 2_000_000,
}
# ================================================================

SIDES = ("A", "B")


@dataclass(frozen=True)
class RuntimeConfig:
    """Validated settings for the quarterly path-average build."""

    spec: plan.AnalysisSpec
    time_shift: int
    first_seen_year_offset: int
    no_spillover: int
    chunksize: int


@dataclass(frozen=True)
class DirectionPool:
    """History allocation for one event-cohort direction."""

    treated_share_ndcs: frozenset[str]
    ever_histories: frozenset[str]
    never_histories: frozenset[str]
    active_share: bool
    generate: bool


PoolKey = tuple[str, int, str]
OutputKey = tuple[str, int] | tuple[str, int, str]


def validate_config(config: dict[str, object]) -> RuntimeConfig:
    """Validate the deliberately small, path-average-only configuration."""
    spec = plan.analysis_spec(config)
    time_shift = int(str(config["formulary_time_shift_quarters"]))
    first_seen_year_offset = int(str(config["first_seen_year_offset"]))
    no_spillover = int(str(config["no_spillover"]))
    chunksize = int(str(config["chunksize"]))
    if spec.level != "plan":
        raise ValueError("Quarterly PathAvgPanelMaker requires level='plan'.")
    if time_shift not in {0, 1}:
        raise ValueError("formulary_time_shift_quarters must be 0 or 1.")
    if first_seen_year_offset not in {-1, 0, 1}:
        raise ValueError("first_seen_year_offset must be -1, 0, or 1.")
    if no_spillover not in {0, 1}:
        raise ValueError("no_spillover must be 0 or 1.")
    if chunksize < 1:
        raise ValueError("chunksize must be positive.")
    return RuntimeConfig(
        spec, time_shift, first_seen_year_offset, no_spillover, chunksize
    )


def quarterly_first_seen_path(time_shift: int) -> Path:
    """Return first-seen metadata matching the quarterly shifted panel."""
    stem = "ndc_first_seen_quarter"
    if time_shift:
        stem = f"{stem}_{plan.shift_label(time_shift)}"
    return plan.FIRST_SEEN_ROOT / f"{stem}.csv"


def load_quarterly_first_seen(time_shift: int) -> pd.DataFrame:
    """Load one nonmissing first-included quarter for every quarterly NDC."""
    path = quarterly_first_seen_path(time_shift)
    data = pd.read_csv(path, dtype="string", keep_default_na=False)
    required = {"NDC", "first_seen_qtime"}
    missing = sorted(required - set(data.columns))
    if missing:
        raise KeyError(f"{path.name} is missing columns: {missing}")
    result = pd.DataFrame(
        {
            "ndc": plan.normalize_text(data["NDC"]),
            "first_seen_qtime": pd.to_numeric(
                data["first_seen_qtime"], errors="raise"
            ).astype("int32"),
        }
    )
    if result.isna().any().any() or result.duplicated("ndc").any():
        raise ValueError(f"{path.name} must contain one nonmissing row per NDC.")
    return result


def cohort_first_seen_cutoffs(
    cohort_windows: dict[int, tuple[str, ...]], year_offset: int
) -> dict[int, int]:
    """Match the do-file cutoff and clip it to the cohort's observed edge."""
    cutoffs: dict[int, int] = {}
    for cohort_key, quarters in cohort_windows.items():
        cohort_year, cohort_quarter = plan.cohort_year_quarter(cohort_key, 1)
        requested = (cohort_year + year_offset) * 4 + cohort_quarter
        first_available = min(year * 4 + quarter for year, quarter in map(plan.quarter_key, quarters))
        cutoffs[cohort_key] = max(requested, first_available)
    return cutoffs


def eligible_ndcs_by_cohort(
    first_seen: pd.DataFrame, cutoffs: dict[int, int]
) -> dict[int, frozenset[str]]:
    """Create cohort-level NDC universes used by both pool and output filters."""
    return {
        cohort_key: frozenset(
            first_seen.loc[
                first_seen["first_seen_qtime"].le(cutoff), "ndc"
            ].astype(str)
        )
        for cohort_key, cutoff in cutoffs.items()
    }


def first_seen_output_label(year_offset: int) -> str:
    """Label the cohort-relative first-seen rule used by this build."""
    offset = f"+{year_offset}" if year_offset >= 0 else str(year_offset)
    return f"seen_y{offset}_event_q"


def effective_treated_share_ndcs(
    source_paths: dict[str, Path],
    samples: dict[tuple[str, int], plan.CohortSample],
    eligible_ndcs: dict[int, frozenset[str]],
    chunksize: int,
) -> dict[PoolKey, frozenset[str]]:
    """Freeze treated-share NDCs at each cohort's event quarter."""
    collected: dict[PoolKey, set[str]] = {
        (event_type, cohort_key, side): set()
        for event_type, cohort_key in samples
        for side in SIDES
    }
    keys_by_anchor: dict[str, list[PoolKey]] = {}
    for event_type, cohort_key in samples:
        year, quarter = plan.cohort_year_quarter(cohort_key, 1)
        for side in SIDES:
            keys_by_anchor.setdefault(f"{year}Q{quarter}", []).append(
                (event_type, cohort_key, side)
            )

    for year_q, pool_keys in sorted(keys_by_anchor.items(), key=lambda item: plan.quarter_key(item[0])):
        source_path = source_paths[year_q]
        share_columns = {
            key: f"{plan.event_column(key[0], key[2])}_sharingATC3"
            for key in pool_keys
        }
        usecols = ["NDC", "id", *sorted(set(share_columns.values()))]
        for chunk in pd.read_csv(
            source_path, usecols=usecols, dtype="string", chunksize=chunksize
        ):
            ndc = plan.normalize_text(chunk["NDC"])
            firm_id = plan.normalize_company_id(chunk["id"], source_path.name)
            for key in pool_keys:
                event_type, cohort_key, side = key
                sample = samples[(event_type, cohort_key)]
                treated = sample.treated_a if side == "A" else sample.treated_b
                sharing = plan.parse_numeric(
                    chunk[share_columns[key]], share_columns[key], source_path.name
                )
                if not sharing.isin([0, 1]).all():
                    raise ValueError(
                        f"{source_path.name} has nonbinary {share_columns[key]}."
                    )
                keep = (
                    firm_id.isin(treated)
                    & sharing.eq(1)
                    & ndc.isin(eligible_ndcs[cohort_key])
                )
                collected[key].update(ndc.loc[keep].dropna().astype(str))
    return {key: frozenset(values) for key, values in collected.items()}


def used_histories_by_quarter(
    path_cohorts: dict[int, plan.PathCohort],
) -> tuple[dict[str, set[str]], dict[str, pd.DataFrame]]:
    """Index relevant formularies and histories once for streaming scans."""
    used_formularies: dict[str, set[str]] = {}
    history_frames: dict[str, list[pd.DataFrame]] = {}
    for path_cohort in path_cohorts.values():
        for year_q, frame in path_cohort.history_quarters.groupby("year_q", sort=False):
            tag = str(year_q)
            used_formularies.setdefault(tag, set()).update(
                frame["formulary_id"].dropna().astype(str)
            )
            history_frames.setdefault(tag, []).append(
                frame[["history_id", "data_cohort", "formulary_id"]]
            )
    histories = {
        year_q: pd.concat(frames, ignore_index=True)
        for year_q, frames in history_frames.items()
    }
    return used_formularies, histories


def normalize_used_formulary_rows(
    chunk: pd.DataFrame,
    formularies: set[str],
    year_q: str,
    source_name: str,
) -> pd.DataFrame:
    """Normalize only rows belonging to histories used by active cohorts."""
    raw_formulary = plan.normalize_text(chunk["FORMULARY_ID"])
    raw_numeric = pd.to_numeric(raw_formulary, errors="coerce")
    numeric_formularies = {
        int(formulary_id) for formulary_id in formularies if formulary_id.isdigit()
    }
    nonnumeric_formularies = {
        formulary_id for formulary_id in formularies if not formulary_id.isdigit()
    }
    keep = raw_formulary.isin(nonnumeric_formularies)
    if numeric_formularies:
        keep |= raw_numeric.isin(numeric_formularies)
    if not keep.any():
        return pd.DataFrame(
            columns=["year_q", "formulary_id", "ndc", "included", "tier_raw"]
        )
    current = pd.DataFrame(index=chunk.index[keep])
    current["year_q"] = year_q
    selected_numeric = raw_numeric.loc[keep]
    current["formulary_id"] = selected_numeric.astype("Int64").astype("string")
    nonnumeric = selected_numeric.isna()
    if nonnumeric.any():
        current.loc[nonnumeric, "formulary_id"] = plan.normalize_numeric_identifier(
            raw_formulary.loc[keep].loc[nonnumeric]
        )
    current["ndc"] = plan.normalize_text(chunk.loc[current.index, "NDC"])
    current["included"] = plan.parse_numeric(
        chunk.loc[current.index, "included"], "included", source_name
    )
    current["tier_raw"] = plan.parse_numeric(
        chunk.loc[current.index, "tier_raw"], "tier_raw", source_name
    )
    current = current.loc[current["formulary_id"].isin(formularies)].copy()
    if current["ndc"].isna().any():
        raise ValueError(f"{source_name} has missing NDC values in used formularies.")
    if not current["included"].isin([0, 1]).all():
        raise ValueError(f"{source_name} has included values outside 0/1.")
    return current


def build_average_metadata(
    source_paths: dict[str, Path],
    path_cohorts: dict[int, plan.PathCohort],
    chunksize: int,
    treated_share_ndcs: dict[PoolKey, frozenset[str]] | None,
) -> tuple[pd.DataFrame, dict[int, pd.DataFrame], dict[PoolKey, DirectionPool]]:
    """Build intensive-outcome eligibility, tier schedule, and path pools."""
    share_sets = treated_share_ndcs or {}
    used_formularies, histories_by_quarter = used_histories_by_quarter(path_cohorts)
    eligible: dict[int, pd.DataFrame | None] = {
        cohort_key: None for cohort_key in path_cohorts
    }
    scanned_quarters: dict[int, list[str]] = {
        cohort_key: [] for cohort_key in path_cohorts
    }
    ever_histories: dict[PoolKey, set[str]] = {
        key: set() for key in share_sets
    }
    keys_by_cohort: dict[int, list[PoolKey]] = {}
    for key in ever_histories:
        keys_by_cohort.setdefault(key[1], []).append(key)
    tier_frames: list[pd.DataFrame] = []

    progress = tqdm(
        sorted(source_paths.items(), key=lambda item: plan.quarter_key(item[0])),
        desc="Scanning complete path formularies",
        unit="quarter",
    )
    for year_q, source_path in progress:
        formularies = used_formularies.get(year_q, set())
        if not formularies:
            continue
        progress.set_postfix_str(year_q)
        histories = histories_by_quarter[year_q]
        included_parts: dict[int, list[pd.DataFrame]] = {
            int(value): [] for value in histories["data_cohort"].unique()
        }
        reader = pd.read_csv(
            source_path,
            usecols=["FORMULARY_ID", "NDC", "included", "tier_raw"],
            dtype="string",
            chunksize=chunksize,
        )
        for chunk in reader:
            current = normalize_used_formulary_rows(
                chunk, formularies, year_q, source_path.name
            )
            if current.empty:
                continue
            included = current.loc[
                current["included"].eq(1), ["formulary_id", "ndc"]
            ].drop_duplicates()
            if not included.empty:
                included_histories = included.merge(
                    histories,
                    on="formulary_id",
                    how="inner",
                    validate="many_to_many",
                )
                for cohort_key, cohort_data in included_histories.groupby(
                    "data_cohort", sort=False
                ):
                    cohort_int = int(cohort_key)
                    included_parts[cohort_int].append(
                        cohort_data[["history_id", "ndc"]].drop_duplicates()
                    )
                    for key in keys_by_cohort.get(cohort_int, []):
                        share_ndcs = share_sets[key]
                        if share_ndcs:
                            ever_histories[key].update(
                                cohort_data.loc[
                                    cohort_data["ndc"].isin(share_ndcs), "history_id"
                                ].astype(str)
                            )
            tiered = plan.add_tier_key(current, source_path.name)
            tier_frames.append(
                tiered.loc[
                    tiered["tier"].notna(), ["year_q", "formulary_id", "tier"]
                ]
            )
        for cohort_key, parts in included_parts.items():
            current_eligible = (
                pd.concat(parts, ignore_index=True).drop_duplicates()
                if parts
                else pd.DataFrame(columns=["history_id", "ndc"])
            )
            previous = eligible[cohort_key]
            eligible[cohort_key] = (
                current_eligible
                if previous is None
                else previous.merge(
                    current_eligible,
                    on=["history_id", "ndc"],
                    how="inner",
                    validate="one_to_one",
                )
            )
            scanned_quarters[cohort_key].append(year_q)

    if not tier_frames:
        raise ValueError("No active formulary tiers were found for path cohorts.")
    eligibility: dict[int, pd.DataFrame] = {}
    all_histories: dict[int, frozenset[str]] = {}
    for cohort_key, path_cohort in path_cohorts.items():
        if tuple(scanned_quarters[cohort_key]) != path_cohort.required_quarters:
            raise ValueError(
                f"Cohort {cohort_key} scan covered {scanned_quarters[cohort_key]}, "
                f"expected {list(path_cohort.required_quarters)}."
            )
        frame = eligible[cohort_key]
        if frame is None:
            frame = pd.DataFrame(columns=["history_id", "ndc"])
        eligibility[cohort_key] = frame.assign(__path_average_eligible=True)
        all_histories[cohort_key] = frozenset(
            path_cohort.history_quarters["history_id"].drop_duplicates().astype(str)
        )

    pools: dict[PoolKey, DirectionPool] = {}
    for key, share_ndcs in share_sets.items():
        ever = frozenset(ever_histories[key])
        never = all_histories[key[1]] - ever
        active_share = bool(share_ndcs and ever)
        generate = not (active_share and not never)
        pools[key] = DirectionPool(
            treated_share_ndcs=share_ndcs,
            ever_histories=ever if active_share else frozenset(),
            never_histories=never if active_share else all_histories[key[1]],
            active_share=active_share,
            generate=generate,
        )
    tiers = pd.concat(tier_frames, ignore_index=True).drop_duplicates()
    return tiers, eligibility, pools


def average_output_paths(
    samples: dict[tuple[str, int], plan.CohortSample],
    config: RuntimeConfig,
    pools: dict[PoolKey, DirectionPool],
) -> dict[OutputKey, Path]:
    """Resolve baseline or direction-specific no-spillover outputs."""
    base = (
        plan.path_cohort_output_dir(config.spec, config.time_shift, quarter=1)
        / first_seen_output_label(config.first_seen_year_offset)
    )
    paths: dict[OutputKey, Path] = {}
    for key, sample in samples.items():
        if not sample.treated_a and not sample.treated_b:
            continue
        filename = plan.path_cohort_output_path(
            config.spec, config.time_shift, *key, quarter=1
        ).name
        if not config.no_spillover:
            paths[key] = base / "path_average" / filename
            continue
        for side in SIDES:
            treated = sample.treated_a if side == "A" else sample.treated_b
            if treated and pools[(key[0], key[1], side)].generate:
                paths[(key[0], key[1], side)] = (
                    base / "path_average_no_spillover" / side / filename
                )
    if not paths:
        raise ValueError("No active path-average cohort outputs remain.")
    return paths


def output_columns(event_type: str) -> list[str]:
    """Return the existing compact quarterly path-average schema."""
    return plan.path_average_columns(event_type, quarter=1)


def restrict_no_spillover(
    selected: pd.DataFrame, side: str, pool: DirectionPool
) -> pd.DataFrame:
    """Apply mutually exclusive ever/never histories to one direction."""
    treated = selected[f"treated_{side.lower()}"].eq(1)
    treated_share = treated & selected["ndc"].isin(pool.treated_share_ndcs)
    if not pool.active_share:
        return selected.loc[~treated_share].copy()
    use_ever = treated_share & selected["history_id"].isin(pool.ever_histories)
    use_never = ~treated_share & selected["history_id"].isin(pool.never_histories)
    return selected.loc[use_ever | use_never].copy()


def build_one_quarter(
    source_path: Path,
    path_cohorts: dict[int, plan.PathCohort],
    benefit_values: pd.DataFrame,
    benefit_counts: pd.DataFrame,
    samples: dict[tuple[str, int], plan.CohortSample],
    eligibility: dict[int, pd.DataFrame],
    eligible_ndcs: dict[int, frozenset[str]],
    pools: dict[PoolKey, DirectionPool],
    output_paths: dict[OutputKey, Path],
    config: RuntimeConfig,
) -> dict[OutputKey, pd.DataFrame]:
    """Accumulate all event-cohort averages for one source quarter."""
    year_q = source_path.stem.removeprefix("formulary_panel_")
    frames = [
        cohort.history_quarters.loc[cohort.history_quarters["year_q"].eq(year_q)]
        for cohort in path_cohorts.values()
        if year_q in cohort.required_quarters
    ]
    if not frames:
        return {}
    histories = pd.concat(frames, ignore_index=True).merge(
        benefit_counts,
        on=["history_id", "data_cohort"],
        how="left",
        validate="many_to_one",
    )
    histories["n_path_copay"] = histories["n_path_copay"].fillna(0).astype("int32")
    histories["n_path_prefer"] = histories["n_path_prefer"].fillna(0).astype("int32")
    used_formularies = set(histories["formulary_id"].dropna().astype(str))
    totals: dict[OutputKey, pd.DataFrame] = {}
    expanded_rows = 0

    reader = pd.read_csv(
        source_path,
        usecols=plan.QUARTER_RAW_FORMULARY_COLUMNS,
        dtype="string",
        chunksize=config.chunksize,
    )
    for raw_chunk in reader:
        raw = plan.normalize_raw_chunk(raw_chunk, source_path.name, quarter=1)
        observed = set(raw["year_q"].dropna().astype(str).unique())
        if observed != {year_q}:
            raise ValueError(f"{source_path.name} contains quarters {sorted(observed)}.")
        raw = raw.loc[raw["formulary_id"].isin(used_formularies)].copy()
        if raw.empty:
            continue
        raw = plan.add_tier_key(raw, source_path.name)
        expanded = raw.merge(
            histories,
            on=["year_q", "formulary_id"],
            how="inner",
            validate="many_to_many",
        )
        if expanded.empty:
            continue
        expanded = expanded.merge(
            benefit_values,
            on=["history_id", "data_cohort", "year_q", "tier"],
            how="left",
            validate="many_to_one",
        )
        expanded_rows += len(expanded)
        for cohort_value in sorted(expanded["data_cohort"].unique()):
            cohort_key = int(cohort_value)
            cohort_data = expanded.loc[
                expanded["data_cohort"].eq(cohort_value)
                & expanded["ndc"].isin(eligible_ndcs[cohort_key])
            ].merge(
                eligibility[cohort_key],
                on=["history_id", "ndc"],
                how="left",
                validate="many_to_one",
            )
            cohort_data["__path_average_eligible"] = cohort_data[
                "__path_average_eligible"
            ].fillna(False)
            for event_type in plan.EVENT_TYPES:
                sample_key = (event_type, cohort_key)
                selected = plan.add_sample_columns(
                    cohort_data, samples[sample_key], quarter=1
                )
                if selected.empty:
                    continue
                if not config.no_spillover:
                    if sample_key not in output_paths:
                        continue
                    totals[sample_key] = plan.accumulate_path_average(
                        totals.get(sample_key),
                        selected,
                        output_columns(event_type),
                        quarter=1,
                    )
                    continue
                for side in SIDES:
                    output_key = (event_type, cohort_key, side)
                    if output_key not in output_paths:
                        continue
                    side_selected = selected.loc[
                        selected[f"sample_{side.lower()}"].eq(1)
                    ]
                    side_selected = restrict_no_spillover(
                        side_selected, side, pools[output_key]
                    )
                    if side_selected.empty:
                        continue
                    totals[output_key] = plan.accumulate_path_average(
                        totals.get(output_key),
                        side_selected,
                        output_columns(event_type),
                        quarter=1,
                    )
    if expanded_rows == 0:
        raise ValueError(f"No path-by-NDC rows match histories in {year_q}.")
    return {
        key: plan.finish_path_average(
            value, output_columns(key[0])
        )
        for key, value in totals.items()
    }


def output_root(paths: dict[OutputKey, Path], config: RuntimeConfig) -> Path:
    """Return the configured build root, independent of active directions."""
    first = next(iter(paths.values()))
    return first.parent.parent if config.no_spillover else first.parent


def validate_output_namespace(
    paths: dict[OutputKey, Path], config: RuntimeConfig
) -> None:
    """Reject stale CSVs instead of deleting or silently mixing them."""
    root = output_root(paths, config)
    if not root.exists():
        return
    expected = {path.resolve() for path in paths.values()}
    unexpected = sorted(
        path for path in root.rglob("*.csv") if path.resolve() not in expected
    )
    if unexpected:
        examples = "\n".join(str(path) for path in unexpected[:10])
        raise FileExistsError(
            "The path-average output directory contains CSVs that are not "
            f"part of this build. Move or remove them explicitly:\n{examples}"
        )


def checkpoint_payload(
    completed: list[str], paths: dict[OutputKey, Path], config: RuntimeConfig
) -> dict[str, object]:
    """Record committed byte boundaries used to reject unsafe resumes."""
    return {
        "version": 1,
        "completed_quarters": completed,
        "no_spillover": config.no_spillover,
        "time_shift": config.time_shift,
        "first_seen_year_offset": config.first_seen_year_offset,
        "files": {
            str(path): path.stat().st_size if path.exists() else 0
            for path in paths.values()
        },
    }


def write_checkpoint(path: Path, payload: dict[str, object]) -> None:
    """Atomically replace the small build checkpoint."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)


def initialize_outputs(
    paths: dict[OutputKey, Path], ordered_quarters: list[str], config: RuntimeConfig
) -> tuple[list[str], Path, Path]:
    """Initialize a new build or restore the last committed append boundary."""
    root = output_root(paths, config)
    root.mkdir(parents=True, exist_ok=True)
    checkpoint = root / "_path_avg_build_checkpoint.json"
    log = root / "_path_avg_build.log"
    checkpoint_temporary = checkpoint.with_suffix(".tmp")
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    if not checkpoint.exists():
        conflicts = [path for path in paths.values() if path.exists()]
        conflicts.extend(path for path in (log, checkpoint_temporary) if path.exists())
        if conflicts:
            examples = "\n".join(str(path) for path in conflicts[:10])
            raise FileExistsError(
                "A fresh path-average build requires an empty output state, "
                "but files already exist. Move or remove them explicitly:\n"
                f"{examples}"
            )
        write_checkpoint(checkpoint, checkpoint_payload([], paths, config))
        append_log(log, "START fresh path-average build")
        return [], checkpoint, log

    if not log.exists():
        raise FileNotFoundError(
            f"Checkpoint exists but its build log is missing: {log}"
        )
    if checkpoint_temporary.exists():
        raise FileExistsError(
            "An unfinished checkpoint update exists. Resolve it explicitly: "
            f"{checkpoint_temporary}"
        )

    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    expected = {
        "version": 1,
        "no_spillover": config.no_spillover,
        "time_shift": config.time_shift,
        "first_seen_year_offset": config.first_seen_year_offset,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ValueError(f"Checkpoint {key}={payload.get(key)!r}; expected {value!r}.")
    completed = list(payload.get("completed_quarters", []))
    if completed != ordered_quarters[: len(completed)]:
        raise ValueError("Checkpoint quarters are not a leading source-quarter prefix.")
    recorded_files = payload.get("files", {})
    expected_files = {str(path) for path in paths.values()}
    if set(recorded_files) != expected_files:
        raise ValueError("Checkpoint output files do not match the current build.")
    for path in paths.values():
        committed_size = int(recorded_files[str(path)])
        actual_size = path.stat().st_size if path.exists() else 0
        if actual_size < committed_size:
            raise ValueError(f"{path} is shorter than its committed checkpoint size.")
        if actual_size > committed_size:
            raise ValueError(
                f"{path} is longer than its committed checkpoint size "
                f"({actual_size} > {committed_size}). Refusing to truncate it."
            )
    return completed, checkpoint, log


def append_log(path: Path, message: str) -> None:
    """Append one timestamped build status line."""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")


def append_quarter_outputs(
    frames: dict[OutputKey, pd.DataFrame], paths: dict[OutputKey, Path]
) -> None:
    """Append one fully computed quarter to its active cohort files."""
    for key, frame in frames.items():
        path = paths[key]
        needs_header = not path.exists() or path.stat().st_size == 0
        frame.to_csv(path, mode="a", index=False, header=needs_header)


def main() -> None:
    """Build quarterly complete-history path averages and nothing else."""
    config = validate_config(RUN_CONFIG)
    source_paths = plan.quarter_panel_paths(plan.formulary_input_dir(config.time_shift, 1))
    quarter_sources = plan.load_quarter_event_sources()
    specifications = plan.quarter_cohort_specifications(quarter_sources.movement)
    if not specifications:
        raise ValueError("No quarterly req1 event cohorts were found.")
    required, windows = plan.required_quarter_windows(source_paths, specifications)
    cohort_windows = {
        year * 4 + event_quarter: tuple(windows[(year, event_quarter)])
        for _, year, event_quarter in specifications
    }
    source_paths = {tag: source_paths[tag] for tag in required}
    samples = plan.build_samples(cohort_windows, quarter=1, quarter_sources=quarter_sources)

    first_seen = load_quarterly_first_seen(config.time_shift)
    cutoffs = cohort_first_seen_cutoffs(
        cohort_windows, config.first_seen_year_offset
    )
    eligible_ndcs = eligible_ndcs_by_cohort(first_seen, cutoffs)
    share_ndcs = (
        effective_treated_share_ndcs(
            source_paths, samples, eligible_ndcs, config.chunksize
        )
        if config.no_spillover
        else None
    )

    target_quarters = set(source_paths)
    crosswalks, copay = plan.load_quarter_copay_inputs(
        plan.BENEFICIARY_COPAY_PATH,
        config.time_shift,
        target_quarters,
        config.chunksize,
    )
    availability = plan.load_panel_formulary_availability(
        source_paths, config.chunksize
    )
    balanced = plan.balanced_plan_units(
        crosswalks,
        cohort_windows,
        config.spec,
        formulary_availability=availability,
    )
    path_cohorts = plan.build_path_cohorts(
        crosswalks, balanced, cohort_windows, config.spec, quarter=1
    )
    tiers, eligibility, pools = build_average_metadata(
        source_paths,
        path_cohorts,
        config.chunksize,
        share_ndcs,
    )
    paths = average_output_paths(samples, config, pools)
    validate_output_namespace(paths, config)
    ordered_quarters = sorted(source_paths, key=plan.quarter_key)
    completed, checkpoint, log = initialize_outputs(paths, ordered_quarters, config)
    if len(completed) == len(ordered_quarters):
        print("[path average] All source quarters are already complete.")
        return

    plan_tier = plan.combine_plan_tier_features(copay, prefer=None)
    benefit_values, benefit_counts = plan.build_benefit_path_features(
        path_cohorts, tiers, plan_tier, config.spec
    )
    for position, year_q in enumerate(ordered_quarters, start=1):
        if year_q in completed:
            continue
        append_log(log, f"START quarter {position}/{len(ordered_quarters)} {year_q}")
        try:
            frames = build_one_quarter(
                source_paths[year_q],
                path_cohorts,
                benefit_values,
                benefit_counts,
                samples,
                eligibility,
                eligible_ndcs,
                pools,
                paths,
                config,
            )
            append_quarter_outputs(frames, paths)
        except Exception:
            append_log(log, f"FAILED {year_q}\n{traceback.format_exc()}")
            raise
        completed.append(year_q)
        write_checkpoint(checkpoint, checkpoint_payload(completed, paths, config))
        append_log(log, f"COMMITTED quarter {position}/{len(ordered_quarters)} {year_q}")
        print(f"[path average] committed {year_q} ({position}/{len(ordered_quarters)})")

    missing = [path for path in paths.values() if not path.exists()]
    if missing:
        raise ValueError(f"No rows were written for outputs: {missing}")
    append_log(log, "COMPLETE all path-average quarters")
    for path in sorted(paths.values()):
        print(f"Wrote path-average panel: {path}")


if __name__ == "__main__":
    main()
