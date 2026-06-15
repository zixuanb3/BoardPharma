r"""
Purpose:
  Build firm-pair–year movement panels and regression-ready cohort panels
  under THREE alternative personnel definitions, directly from
  ssr_company_roster.csv (the master personnel roster with role tiers).

Personnel Definitions (PARAMETERISED):
  NARROW  – board only     : leader_tier in ["board", "boardex"]
  MEDIUM  – board + C-suite: above + ["csuite"]
  BROAD   – board + C + VP : above + ["vp_tech_hr"]

For each definition:
  1. Build director-year-board assignments from the filtered roster.
  2. Detect director movements (A -> B) and classify:
       to_B_still_in_A  – director retains seat on A in year t
       to_B_not_in_A    – director leaves A in year t
       dissolution      – director was on both A and B in t-1, leaves A in t
  3. Aggregate to firm-pair–year level with stay counters.
  4. Construct event dummy controls per empirical_specification.tex:
       pre_retain, pre_exit, pre_dissolved     (before window)
       sameq_retain, sameq_exit, sameq_dissolved (same year)
       post_retain, post_exit, post_dissolved   (post window)
       retain_Xyr                               (persistence dummy)
  5. Build control groups C1A, C1B, C2A, C2B with per-cohort non-overlap.
  6. Output regression-ready panel + sample size summary.

All window lengths and retain-duration thresholds are PARAMETERISED
in RUN_CONFIG.

Input:
  D:\pharma\ssr_company_roster.csv          (master roster, 122751 rows)
  D:\Dropbox\BoardPharma\InterimData\boardex_ssr_price_sample.csv (optional SSR data)

Output (under D:\pharma\output\personnel_panels\):
  {definition}/pair_movement_panel.csv       (pair-year movement counts)
  {definition}/cohort_panels/{control_set}/reg_panel_cohort_{year}.csv
  sample_size_summary.csv

Author: Generated for pharma project
"""

import os
from pathlib import Path
from itertools import product, combinations
import warnings
import pandas as pd
import numpy as np

warnings.filterwarnings("ignore", category=FutureWarning)

# ========================== PATHS ==========================
ROSTER_PATH = Path(r"D:\pharma\ssr_company_roster.csv")
SSR_PRICE_PATH = Path(
    r"D:\Dropbox\BoardPharma\InterimData\boardex_ssr_price_sample.csv"
)
OUTPUT_ROOT = Path(r"D:\pharma\output\personnel_panels")
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

# ========================== USER CONFIG ==========================
# All key parameters are here – change freely.
RUN_CONFIG = {
    # ── Personnel definitions ──
    "personnel_defs": {
        "narrow_board":       ["board", "boardex"],
        "medium_board_csuite": ["board", "boardex", "csuite"],
        "broad_board_c_vp":    ["board", "boardex", "csuite", "vp_tech_hr"],
    },

    # ── Movement detection ──
    "min_year": 2000,             # earliest year to consider
    "max_year": 2023,             # latest year to consider

    # ── Retain persistence ──
    "retain_years_list": [3],  # years a director must stay on B to count as "retained"

    # ── Window & cohort params ──
    "window_pre_years": 2,        # years before event in estimation window
    "window_post_years": 2,       # years after event in estimation window
    "cohort_year_min": 2003,      # first cohort year (must be >= min_year + window_pre)
    "cohort_year_max": 2021,      # last cohort year  (must be <= max_year - window_post)

    # ── Balanced panel requirements ──
    # "A": strict – both firms must exist in ALL pre AND post window years
    # "B": relaxed – post-period existence NOT required (pair may exit after event)
    "post_period_variant": "A",

    # ── Control sets ──
    "control_sets": ["C1A", "C1B","C4", "C6A", "C6B"],

    # ── Event types to build panels for ──
    "event_types": [
        "to_B_still_in_A",
        "to_B_not_in_A",
        "dissolution",
    ],
}
# ===============================================================


# ╔══════════════════════════════════════════════════════════════╗
# ║  STEP 1: Load & filter personnel roster                     ║
# ╚══════════════════════════════════════════════════════════════╝

def load_roster() -> pd.DataFrame:
    """Load the master personnel roster and normalize firm names."""
    df = pd.read_csv(ROSTER_PATH)
    df["year"] = df["year"].astype(int)
    df["DirectorID"] = df["DirectorID"].astype(int)
    df["CompanyID"] = df["CompanyID"].astype(int)

    # Normalize firm names: use CompanyID→canonical BoardName mapping.
    # Same CompanyID may appear with different casings from ie_oc vs boardex_pharma.
    # Build canonical name per CompanyID (uppercase, first occurrence).
    canonical = (
        df.groupby("CompanyID")["BoardName"]
        .apply(lambda x: x.iloc[0].upper())
        .to_dict()
    )
    df["BoardName"] = df["CompanyID"].map(canonical)

    print(f"    Roster loaded: {len(df)} rows, "
          f"{df['CompanyID'].nunique()} unique firms (CompanyID), "
          f"{df['DirectorID'].nunique()} directors")
    return df


def build_firm_existence_table(roster: pd.DataFrame) -> set:
    """
    Build set of (BoardName, year) tuples where the firm EXISTS
    (has at least one director record in that year).
    """
    exist = roster[["BoardName", "year"]].drop_duplicates()
    return set(zip(exist["BoardName"], exist["year"].astype(int)))


def filter_by_personnel_def(df: pd.DataFrame, tiers: list) -> pd.DataFrame:
    """
    Keep only rows whose leader_tier is in the given list.
    Returns a copy.
    """
    return df[df["leader_tier"].isin(tiers)].copy()


# ╔══════════════════════════════════════════════════════════════╗
# ║  STEP 2: Build director-year-board assignments              ║
# ╚══════════════════════════════════════════════════════════════╝

def build_director_board_assignments(roster: pd.DataFrame) -> pd.DataFrame:
    """
    For each director-year, collect the set of boards they sit on.

    Returns DataFrame with columns:
      DirectorID, year, boards (list of BoardName strings)
    """
    grouped = (
        roster
        .groupby(["DirectorID", "year"], as_index=False)["BoardName"]
        .agg(lambda x: sorted(set(x.tolist())))
        .rename(columns={"BoardName": "boards"})
    )
    grouped = grouped.sort_values(["DirectorID", "year"]).reset_index(drop=True)
    return grouped


# ╔══════════════════════════════════════════════════════════════╗
# ║  STEP 3: Detect director movements                          ║
# ╚══════════════════════════════════════════════════════════════╝

def detect_movements(
    assignments: pd.DataFrame,
    retain_years: int = 3,
) -> pd.DataFrame:
    """
    Detect all director movements from the year-by-year board assignments.

    For each director, for each consecutive year pair (t-1, t):
      - New boards = boards(t) - boards(t-1)
      - For each new board B and each old board A:
          * to_B_still_in_A = 1 if A in boards(t)
          * to_B_not_in_A   = 1 if A not in boards(t)

    Also detect dissolution events:
      - Director was on both A and B in t-1, leaves A in t (but stays on B).

    Also compute stay check: does director remain on B for retain_years years?

    Returns DataFrame with columns:
      DirectorID, year, A, B, to_B_still_in_A, to_B_not_in_A,
      dissolution, stay_X_years
    """
    # Build fast lookup: (DirectorID, year) -> set of boards
    board_lookup = {}
    for _, row in assignments.iterrows():
        key = (int(row["DirectorID"]), int(row["year"]))
        board_lookup[key] = set(row["boards"])

    rows = []
    for _, row in assignments.iterrows():
        did = int(row["DirectorID"])
        year = int(row["year"])
        current_boards = row["boards"]
        prev_boards = board_lookup.get((did, year - 1))
        if not prev_boards:
            continue

        current_set = set(current_boards)
        prev_set = set(prev_boards)

        new_boards = current_set - prev_set
        if not new_boards:
            continue

        for A in prev_set:
            for B in new_boards:
                if A == B:
                    continue

                still = 1 if A in current_set else 0
                not_in = 1 if A not in current_set else 0

                # Dissolution: director was on both A and B in prev, leaves A in t
                # (B status is irrelevant — director may stay or also leave B)
                diss = 1 if (A in prev_set and B in prev_set
                             and A not in current_set) else 0

                # Stay check: is director still on B in year + retain_years - 1?
                stay_ok = 1
                for offset in range(1, retain_years):
                    future_boards = board_lookup.get((int(did), year + offset), set())
                    if B not in future_boards:
                        stay_ok = 0
                        break

                rows.append({
                    "DirectorID": int(did),
                    "year": year,
                    "A": A,
                    "B": B,
                    "to_B_still_in_A": still,
                    "to_B_not_in_A": not_in,
                    "dissolution": diss,
                    f"stay_{retain_years}_years": stay_ok,
                })

    # --- Standalone dissolution detection ---
    # Director was on both A and B in t-1, leaves A in t (B may or may not stay).
    # This catches dissolution where B is NOT a "new" board in year t.
    for _, row in assignments.iterrows():
        did = int(row["DirectorID"])
        year = int(row["year"])
        current_boards = row["boards"]
        prev_boards = board_lookup.get((did, year - 1))
        if not prev_boards:
            continue
        current_set = set(current_boards)
        prev_set = set(prev_boards)
        # For all pairs (A, B) where both were present in t-1
        prev_list = sorted(prev_set)
        for i in range(len(prev_list)):
            for j in range(i + 1, len(prev_list)):
                A, B = prev_list[i], prev_list[j]
                # Dissolution: A left (regardless of B's status)
                if A not in current_set:
                    rows.append({
                        "DirectorID": did, "year": year,
                        "A": A, "B": B,
                        "to_B_still_in_A": 0, "to_B_not_in_A": 0,
                        "dissolution": 1,
                        f"stay_{retain_years}_years": 0,
                    })
                # Symmetric: B left
                if B not in current_set:
                    rows.append({
                        "DirectorID": did, "year": year,
                        "A": B, "B": A,
                        "to_B_still_in_A": 0, "to_B_not_in_A": 0,
                        "dissolution": 1,
                        f"stay_{retain_years}_years": 0,
                    })

    return pd.DataFrame(rows).drop_duplicates(
        subset=["DirectorID", "year", "A", "B", "to_B_still_in_A",
                "to_B_not_in_A", "dissolution"]
    ).reset_index(drop=True)


# ╔══════════════════════════════════════════════════════════════╗
# ║  STEP 4: Aggregate to pair-year panel                      ║
# ╚══════════════════════════════════════════════════════════════╝

def build_pair_year_panel(
    movements: pd.DataFrame,
    roster: pd.DataFrame,
    firm_existence: set,
    retain_years: int = 3,
    min_year: int = 2000,
    max_year: int = 2023,
) -> pd.DataFrame:
    """
    Aggregate director-level movements to firm-pair–year level,
    then fill in all possible (A, B, year) combinations with zeros
    so the output is a COMPLETE balanced panel.

    FIRM EXISTENCE FILTER: rows are kept only if BOTH firms A and B
    exist in that year (have at least one director record from the roster).

    Returns columns:
      A, B, year, total_moves, retain, exit, dissolution, stay_X_years
    """
    stay_col = f"stay_{retain_years}_years"

    # 1. Get all unique firms from the filtered roster
    all_firms = sorted(roster["BoardName"].unique())
    all_years = list(range(min_year, max_year + 1))

    print(f"      Building full panel: {len(all_firms)} firms x "
          f"{len(all_firms)} firms x {len(all_years)} years = "
          f"{len(all_firms) * (len(all_firms) - 1) * len(all_years)} potential rows")

    # 2. Build Cartesian product of all (A, B, year) with A != B
    pairs = []
    for i, a in enumerate(all_firms):
        for b in all_firms:
            if a == b:
                continue
            pairs.append((a, b))
    pair_df = pd.DataFrame(pairs, columns=["A", "B"])
    # Cross-join with years
    years_df = pd.DataFrame(all_years, columns=["year"])
    pair_df["_key"] = 1
    years_df["_key"] = 1
    full_panel = pair_df.merge(years_df, on="_key").drop(columns=["_key"])
    full_panel = full_panel.sort_values(["A", "B", "year"]).reset_index(drop=True)

    # 3. STRICT EXISTENCE FILTER: keep only rows where BOTH A and B exist in that year
    before = len(full_panel)
    exist_df = pd.DataFrame(
        [(f, y) for f, y in firm_existence], columns=["BoardName", "year"]
    )
    # Merge with A existence
    full_panel = full_panel.merge(
        exist_df.rename(columns={"BoardName": "A"}), on=["A", "year"], how="inner"
    )
    # Merge with B existence
    full_panel = full_panel.merge(
        exist_df.rename(columns={"BoardName": "B"}), on=["B", "year"], how="inner"
    )
    print(f"      After existence filter: {len(full_panel)} rows (dropped {before - len(full_panel)})")

    # 3. Aggregate actual movements
    if movements.empty:
        agg = pd.DataFrame(columns=["A", "B", "year", "total_moves",
                                     "retain", "exit", "dissolution", stay_col])
    else:
        agg = (
            movements
            .groupby(["A", "B", "year"], as_index=False)
            .agg(
                total_moves=("DirectorID", "count"),
                retain=("to_B_still_in_A", "sum"),
                exit=("to_B_not_in_A", "sum"),
                dissolution=("dissolution", "sum"),
                **{stay_col: (stay_col, "sum")},
            )
        )

    # 4. Left-join movement counts onto full panel, fill zeros
    pair_year = full_panel.merge(agg, on=["A", "B", "year"], how="left")
    for col in ["total_moves", "retain", "exit", "dissolution", stay_col]:
        if col in pair_year.columns:
            pair_year[col] = pair_year[col].fillna(0).astype(int)

    print(f"      Full panel rows: {len(pair_year)}")
    print(f"      Rows with moves >0: {(pair_year['total_moves'] > 0).sum()}")
    return pair_year


# ╔══════════════════════════════════════════════════════════════╗
# ║  STEP 5: Build firm-level event history (for dummy ctrl)   ║
# ╚══════════════════════════════════════════════════════════════╝

def build_firm_event_history(pair_panel: pd.DataFrame) -> pd.DataFrame:
    """
    From the pair panel, compute for each firm-year what event types occurred.

    Returns DataFrame: BoardName, year, has_retain, has_exit, has_dissolution
    """
    records = []

    # Retain events: firm B receives a retain move
    retain = pair_panel[pair_panel["retain"] > 0][["B", "year"]].drop_duplicates()
    retain = retain.rename(columns={"B": "BoardName"})
    retain["has_retain"] = 1
    records.append(retain)

    # Exit events: firm B receives an exit move
    exit_ = pair_panel[pair_panel["exit"] > 0][["B", "year"]].drop_duplicates()
    exit_ = exit_.rename(columns={"B": "BoardName"})
    exit_["has_exit"] = 1
    records.append(exit_)

    # Dissolution events: firm A is the dissolved-from firm
    diss = pair_panel[pair_panel["dissolution"] > 0][["A", "year"]].drop_duplicates()
    diss = diss.rename(columns={"A": "BoardName"})
    diss["has_dissolution"] = 1
    records.append(diss)

    if not records:
        return pd.DataFrame(columns=["BoardName", "year", "any_event"])

    history = records[0]
    for r in records[1:]:
        history = history.merge(r, on=["BoardName", "year"], how="outer")

    for col in ["has_retain", "has_exit", "has_dissolution"]:
        if col in history.columns:
            history[col] = history[col].fillna(0).astype(np.int8)

    history["any_event"] = (
        history[[c for c in ["has_retain", "has_exit", "has_dissolution"]
                 if c in history.columns]]
        .max(axis=1).astype(np.int8)
    )
    return history


# ╔══════════════════════════════════════════════════════════════╗
# ║  STEP 6: Build event dummy controls per treated pair       ║
# ╚══════════════════════════════════════════════════════════════╝

def build_event_dummies(
    firm_A: str,
    firm_B: str,
    cohort_year: int,
    event_type: str,
    window_pre: int,
    window_post: int,
    pair_panel: pd.DataFrame,
) -> dict:
    """
    Construct event-level dummy controls for a specific firm PAIR (A, B).

    All dummies capture whether THIS PAIR (A, B) experienced other
    board movement events of various types, NOT whether either firm
    individually had events.

    Parameters
    ----------
    firm_A, firm_B : origin and destination firms
    cohort_year : event year
    event_type : 'to_B_still_in_A', 'to_B_not_in_A', or 'dissolution'
    window_pre : years before event
    window_post : years after event
    pair_panel : full pair-year panel (A, B, year, retain, exit, dissolution, stay_3_years)
    """
    dummies = {
        "pre_retain_W": 0, "pre_exit_W": 0, "pre_dissolved_W": 0,
        "pre_retain_F": 0, "pre_exit_F": 0, "pre_dissolved_F": 0,
        "sameq_retain": 0, "sameq_exit": 0, "sameq_dissolved": 0,
        "post_retain": 0, "post_exit": 0, "post_dissolved": 0,
        "retain_3yr": 0,
    }

    # Filter pair_panel to this specific pair (A, B)
    pp = pair_panel[(pair_panel["A"] == firm_A) & (pair_panel["B"] == firm_B)]

    # --- Pre-period (Window-based W): pair events in [cohort_year - window_pre, cohort_year - 1] ---
    pre_w_start = cohort_year - window_pre
    pre_w = pp[(pp["year"] >= pre_w_start) & (pp["year"] < cohort_year)]
    if not pre_w.empty:
        if pre_w["retain"].max() > 0:
            dummies["pre_retain_W"] = 1
        if pre_w["exit"].max() > 0:
            dummies["pre_exit_W"] = 1
        if pre_w["dissolution"].max() > 0:
            dummies["pre_dissolved_W"] = 1

    # --- Pre-period (Full-history F): pair events at any time before cohort_year ---
    pre_f = pp[pp["year"] < cohort_year]
    if not pre_f.empty:
        if pre_f["retain"].max() > 0:
            dummies["pre_retain_F"] = 1
        if pre_f["exit"].max() > 0:
            dummies["pre_exit_F"] = 1
        if pre_f["dissolution"].max() > 0:
            dummies["pre_dissolved_F"] = 1

    # --- Same-year: OTHER event types in the SAME year for THIS pair ---
    sy = pp[pp["year"] == cohort_year]
    if not sy.empty:
        if event_type != "to_B_still_in_A" and sy["retain"].max() > 0:
            dummies["sameq_retain"] = 1
        if event_type != "to_B_not_in_A" and sy["exit"].max() > 0:
            dummies["sameq_exit"] = 1
        if event_type != "dissolution" and sy["dissolution"].max() > 0:
            dummies["sameq_dissolved"] = 1

    # --- Post-period: pair events in (cohort_year, cohort_year + window_post] ---
    post = pp[(pp["year"] > cohort_year) & (pp["year"] <= cohort_year + window_post)]
    if not post.empty:
        if post["retain"].max() > 0:
            dummies["post_retain"] = 1
        if post["exit"].max() > 0:
            dummies["post_exit"] = 1
        if post["dissolution"].max() > 0:
            dummies["post_dissolved"] = 1

    # --- retain_3yr: THIS pair had a retain event with stay_3_years >= 1 in cohort_year ---
    if event_type == "to_B_still_in_A":
        firm_retains = pp[pp["year"] == cohort_year]
        if "stay_3_years" in firm_retains.columns and firm_retains["stay_3_years"].sum() > 0:
            dummies["retain_3yr"] = 1

    return dummies


# ╔══════════════════════════════════════════════════════════════╗
# ║  STEP 7: Build control groups (C1A/B, C2A/B)              ║
# ╚══════════════════════════════════════════════════════════════╝

def build_control_sets(
    pair_panel: pd.DataFrame,
    firm_history: pd.DataFrame,
    event_type: str,
    cohort_year: int,
    window_pre: int,
    window_post: int,
) -> dict:
    """
    For a given event_type and cohort_year, produce control groups C1A, C1B, C2A, C2B.

    Returns dict: {control_set_name: list of firm-pair tuples (A, B)}
    """
    # All unique pairs in the pair panel
    all_pairs = pair_panel[["A", "B"]].drop_duplicates()
    all_pairs_set = set(zip(all_pairs["A"], all_pairs["B"]))
    all_firms = set(all_pairs["A"].unique()) | set(all_pairs["B"].unique())

    # Treated pairs in this cohort year for this event type
    if event_type == "to_B_still_in_A":
        treated = pair_panel[
            (pair_panel["year"] == cohort_year) & (pair_panel["retain"] > 0)
        ]
    elif event_type == "to_B_not_in_A":
        treated = pair_panel[
            (pair_panel["year"] == cohort_year) & (pair_panel["exit"] > 0)
        ]
    elif event_type == "dissolution":
        treated = pair_panel[
            (pair_panel["year"] == cohort_year) & (pair_panel["dissolution"] > 0)
        ]
    else:
        treated = pd.DataFrame(columns=["A", "B"])

    treated_pairs = set(zip(treated["A"], treated["B"]))
    treated_firms_B = set(treated["B"].unique())  # destination firms
    treated_firms_A = set(treated["A"].unique())  # origin firms
    treated_firms = treated_firms_A | treated_firms_B

    # Window years
    window_years = set(range(cohort_year - window_pre, cohort_year + window_post + 1))

    # Helper: does a pair have any same-type event in the window?
    def _has_event_in_window(pair_panel, event_type, window_years, tpairs=None):
        wdf = pair_panel[pair_panel["year"].isin(window_years)]
        if event_type == "to_B_still_in_A":
            ev = wdf[wdf["retain"] > 0]
        elif event_type == "to_B_not_in_A":
            ev = wdf[wdf["exit"] > 0]
        else:
            ev = wdf[wdf["dissolution"] > 0]
        return set(zip(ev["A"], ev["B"]))

    # Helper: does a pair have any same-type event ever?
    def _has_event_ever(pair_panel, event_type):
        if event_type == "to_B_still_in_A":
            ev = pair_panel[pair_panel["retain"] > 0]
        elif event_type == "to_B_not_in_A":
            ev = pair_panel[pair_panel["exit"] > 0]
        else:
            ev = pair_panel[pair_panel["dissolution"] > 0]
        return set(zip(ev["A"], ev["B"]))

    pairs_with_event_window = _has_event_in_window(pair_panel, event_type, window_years)
    pairs_with_event_ever = _has_event_ever(pair_panel, event_type)

    # C1-A: never treated in window (set operations, fast)
    c1a_pairs = all_pairs_set - treated_pairs - pairs_with_event_window

    # C1-B: never treated ever
    c1b_pairs = all_pairs_set - treated_pairs - pairs_with_event_ever

    # Firms "involved" in any board movement (vectorized)
    wdf = pair_panel[pair_panel["year"].isin(window_years)]
    involved_window = set(wdf.loc[wdf["total_moves"] > 0, "A"].unique()) | \
                      set(wdf.loc[wdf["total_moves"] > 0, "B"].unique())
    involved_ever = set(pair_panel.loc[pair_panel["total_moves"] > 0, "A"].unique()) | \
                    set(pair_panel.loc[pair_panel["total_moves"] > 0, "B"].unique())

    # C2-A: ever-involved in window, and pair not treated
    c2a_pairs = {
        (a, b) for a, b in all_pairs_set
        if (a, b) not in treated_pairs
        and (a in involved_window or b in involved_window)
    }

    # C2-B: ever-involved ever, and pair not treated
    c2b_pairs = {
        (a, b) for a, b in all_pairs_set
        if (a, b) not in treated_pairs
        and (a in involved_ever or b in involved_ever)
    }

    # Apply per-cohort firm-level non-overlap:
    # No control pair may contain any treated firm.
    def _exclude_treated_firms(pairs, tfirms):
        return {(a, b) for a, b in pairs if a not in tfirms and b not in tfirms}

    # C3: Pure Mover–Pure Recipient — A only sends, B only receives (never treated itself)
    all_movers = set(pair_panel.loc[pair_panel["total_moves"] > 0, "A"].unique())
    all_recipients = set(pair_panel.loc[pair_panel["total_moves"] > 0, "B"].unique())
    pure_movers = all_movers - all_recipients
    pure_recipients = all_recipients - all_movers
    c3_pairs = {
        (a, b) for a, b in all_pairs_set
        if (a, b) not in treated_pairs
        and a in pure_movers and b in pure_recipients
    }

    # C4: Future-Treated (Not-Yet-Treated) — pair will be treated ONLY after window
    # Must NOT have event within the current window (clean future-treated only)
    c4_pairs = {
        (a, b) for a, b in all_pairs_set
        if (a, b) not in treated_pairs
        and (a, b) in pairs_with_event_ever
        and (a, b) not in pairs_with_event_window
    }

    # C5: C3 ∩ C4
    c5_pairs = c3_pairs & c4_pairs

    # C6: Pairs where NEITHER firm has ANY type of board movement
    # C6-A: window-internal (both firms have zero total_moves in window)
    firms_with_moves_window = set(
        wdf.loc[wdf["total_moves"] > 0, "A"].unique()
    ) | set(
        wdf.loc[wdf["total_moves"] > 0, "B"].unique()
    )
    firms_no_moves_window = set(all_firms) - firms_with_moves_window
    c6a_pairs = {
        (a, b) for a, b in all_pairs_set
        if (a, b) not in treated_pairs
        and a in firms_no_moves_window and b in firms_no_moves_window
    }
    # C6-B: full-history (both firms have zero total_moves ever)
    firms_with_moves_ever = set(
        pair_panel.loc[pair_panel["total_moves"] > 0, "A"].unique()
    ) | set(
        pair_panel.loc[pair_panel["total_moves"] > 0, "B"].unique()
    )
    firms_no_moves_ever = set(all_firms) - firms_with_moves_ever
    c6b_pairs = {
        (a, b) for a, b in all_pairs_set
        if (a, b) not in treated_pairs
        and a in firms_no_moves_ever and b in firms_no_moves_ever
    }

    return {
        "C1A": _exclude_treated_firms(c1a_pairs, treated_firms),
        "C1B": _exclude_treated_firms(c1b_pairs, treated_firms),
        "C2A": _exclude_treated_firms(c2a_pairs, treated_firms),
        "C2B": _exclude_treated_firms(c2b_pairs, treated_firms),
        "C3":  _exclude_treated_firms(c3_pairs, treated_firms),
        "C4":  _exclude_treated_firms(c4_pairs, treated_firms),
        "C5":  _exclude_treated_firms(c5_pairs, treated_firms),
        "C6A": _exclude_treated_firms(c6a_pairs, treated_firms),
        "C6B": _exclude_treated_firms(c6b_pairs, treated_firms),
    }


# ╔══════════════════════════════════════════════════════════════╗
# ║  STEP 8: Build the full regression panel                   ║
# ╚══════════════════════════════════════════════════════════════╝

def build_regression_panel(
    pair_panel: pd.DataFrame,
    event_type: str,
    cohort_year: int,
    window_pre: int,
    window_post: int,
    control_set: str,
    control_pairs: set,
    post_period_variant: str = "A",
) -> pd.DataFrame | None:
    """
    Build a single regression panel for one cohort × control_set.

    post_period_variant:
      "A" (strict): both firms must exist in ALL pre AND post window years
      "B" (relaxed): pre-period strict; post-period existence NOT required
    """
    # --- Treated ---
    if event_type == "to_B_still_in_A":
        ev_col = "retain"
    elif event_type == "to_B_not_in_A":
        ev_col = "exit"
    else:
        ev_col = "dissolution"

    treated = pair_panel[
        (pair_panel["year"] == cohort_year) & (pair_panel[ev_col] > 0)
    ].copy()
    if treated.empty:
        return None

    wstart = cohort_year - window_pre
    wend = cohort_year + window_post
    treated_pairs_df = treated[["A", "B"]].drop_duplicates()

    treated_window = pair_panel[
        pair_panel["year"].between(wstart, wend)
    ].merge(treated_pairs_df, on=["A", "B"], how="inner")

    # Variant A: require full window coverage for treated pairs
    # Variant B: require only pre-period coverage
    if post_period_variant == "A":
        required_years = set(range(wstart, wend + 1))
    else:
        required_years = set(range(wstart, cohort_year))  # pre only

    treated_obs = treated_window.groupby(["A", "B"])["year"].apply(set)
    valid_treated = treated_obs[treated_obs.apply(lambda s: required_years.issubset(s))].index
    treated_window = treated_window[
        treated_window.set_index(["A", "B"]).index.isin(valid_treated)
    ].reset_index(drop=True)

    if treated_window.empty:
        return None

    treated_window["treat"] = 1

    # Attach PAIR-LEVEL dummies (iterate over unique treated pairs A,B)
    treated_pairs_unique = treated[["A", "B"]].drop_duplicates()
    dummy_cache = {}
    for _, tp in treated_pairs_unique.iterrows():
        a, b = tp["A"], tp["B"]
        dummy_cache[(a, b)] = build_event_dummies(
            firm_A=a,
            firm_B=b,
            cohort_year=cohort_year,
            event_type=event_type,
            window_pre=window_pre,
            window_post=window_post,
            pair_panel=pair_panel,
        )
    for (a, b), dvals in dummy_cache.items():
        mask = (treated_window["A"] == a) & (treated_window["B"] == b)
        for key, val in dvals.items():
            treated_window.loc[mask, key] = val
    dummy_keys = list(dummy_cache[next(iter(dummy_cache))].keys())
    for dk in dummy_keys:
        treated_window[dk] = treated_window[dk].fillna(0).astype(np.int8)

    # --- Interaction terms (Approach B): treat × each dummy ---
    for dk in dummy_keys:
        treated_window["treat_x_" + dk] = (treated_window["treat"] * treated_window[dk]).astype(np.int8)

    # --- Control ---
    if not control_pairs:
        return None
    # Convert control_pairs set to DataFrame for fast merge
    ctrl_list = list(control_pairs)
    ctrl_df = pd.DataFrame(ctrl_list, columns=["A", "B"])
    control_window = pair_panel[
        pair_panel["year"].between(wstart, wend)
    ].merge(ctrl_df, on=["A", "B"], how="inner")

    # Apply same year-coverage requirement as treated
    ctrl_obs = control_window.groupby(["A", "B"])["year"].apply(set)
    valid_ctrl = ctrl_obs[ctrl_obs.apply(lambda s: required_years.issubset(s))].index
    control_window = control_window[
        control_window.set_index(["A", "B"]).index.isin(valid_ctrl)
    ].reset_index(drop=True)

    if control_window.empty:
        return None

    control_window["treat"] = 0
    for dk in dummy_keys:
        control_window[dk] = 0
        control_window["treat_x_" + dk] = 0

    # --- Stack ---
    cohort_df = pd.concat([treated_window, control_window], ignore_index=True)
    cohort_df["event_time"] = cohort_df["year"] - cohort_year
    cohort_df["cohort_year"] = cohort_year
    cohort_df["control_set"] = control_set

    return cohort_df


# ╔══════════════════════════════════════════════════════════════╗
# ║  STEP 9: Sample size counting                             ║
# ╚══════════════════════════════════════════════════════════════╝

def count_sample_size(cohort_df: pd.DataFrame) -> dict:
    treated = cohort_df[cohort_df["treat"] == 1]
    control = cohort_df[cohort_df["treat"] == 0]
    return {
        "N_obs": len(cohort_df),
        "N_treated_obs": len(treated),
        "N_control_obs": len(control),
        "N_pairs_treated": treated[["A", "B"]].drop_duplicates().shape[0],
        "N_pairs_control": control[["A", "B"]].drop_duplicates().shape[0],
        "N_firms_treated_B": treated["B"].nunique(),
        "N_firms_control": control[["A", "B"]].stack().nunique(),
    }


# ╔══════════════════════════════════════════════════════════════╗
# ║  MAIN                                                      ║
# ╚══════════════════════════════════════════════════════════════╝

def main():
    cfg = RUN_CONFIG
    print("=" * 70)
    print("BUILDING PERSONNEL-DEFINITION MOVEMENT PANELS")
    print("=" * 70)

    # Load roster
    print("\n[1] Loading master roster...")
    roster_full = load_roster()
    print(f"    Total rows: {len(roster_full)}")
    print(f"    Unique firms: {roster_full['BoardName'].nunique()}")
    print(f"    Unique directors: {roster_full['DirectorID'].nunique()}")

    all_sample_sizes = []

    for def_name, tiers in cfg["personnel_defs"].items():
        print(f"\n{'='*60}")
        print(f"  PERSONNEL DEFINITION: {def_name}")
        print(f"  Tiers: {tiers}")
        print(f"{'='*60}")

        def_dir = OUTPUT_ROOT / def_name
        def_dir.mkdir(parents=True, exist_ok=True)

        # Filter roster
        print("\n  [2] Filtering roster...")
        roster = filter_by_personnel_def(roster_full, tiers)
        roster = roster[
            (roster["year"] >= cfg["min_year"]) & (roster["year"] <= cfg["max_year"])
        ]
        print(f"      Filtered rows: {len(roster)}")
        print(f"      Unique firms: {roster['BoardName'].nunique()}")
        print(f"      Unique directors: {roster['DirectorID'].nunique()}")

        # Build director-board assignments
        print("\n  [3] Building director-board assignments...")
        assignments = build_director_board_assignments(roster)
        print(f"      Director-year rows: {len(assignments)}")

        # For each retain_years value
        for retain_years in cfg["retain_years_list"]:
            print(f"\n  [4] Detecting movements (retain {retain_years}yr)...")
            movements = detect_movements(assignments, retain_years=retain_years)
            print(f"      Total movements: {len(movements)}")
            if not movements.empty:
                print(f"      still_in_A: {movements['to_B_still_in_A'].sum()}")
                print(f"      not_in_A:   {movements['to_B_not_in_A'].sum()}")
                print(f"      dissolution:{movements['dissolution'].sum()}")

            # Build firm existence table from THIS definition's roster
            print("\n  [4b] Building firm existence table...")
            firm_existence = build_firm_existence_table(roster)
            print(f"      Firm-year existence tuples: {len(firm_existence)}")

            # Aggregate to pair-year panel
            print("\n  [5] Aggregating to pair-year panel (FULL balanced)...")
            pair_panel = build_pair_year_panel(
                movements,
                roster=roster,
                firm_existence=firm_existence,
                retain_years=retain_years,
                min_year=cfg["min_year"],
                max_year=cfg["max_year"],
            )
            print(f"      Pair-year rows: {len(pair_panel)}")
            if not pair_panel.empty:
                print(f"      Pairs with moves: {(pair_panel['total_moves']>0).sum()}")

            # Save pair panel
            pp_path = def_dir / f"pair_movement_panel_retain{retain_years}yr.csv"
            pair_panel.to_csv(pp_path, index=False)
            print(f"      Saved: {pp_path}")

            # Build firm event history
            print("\n  [6] Building firm event history...")
            firm_history = build_firm_event_history(pair_panel)
            print(f"      Firm-year event records: {len(firm_history)}")

            # For each event type, build cohort panels
            for event_type in cfg["event_types"]:
                print(f"\n  [7] Building cohort panels for: {event_type}")

                # Check if this event type has any events
                if event_type == "to_B_still_in_A":
                    ev_mask = pair_panel["retain"] > 0
                elif event_type == "to_B_not_in_A":
                    ev_mask = pair_panel["exit"] > 0
                else:
                    ev_mask = pair_panel["dissolution"] > 0

                event_years = sorted(
                    pair_panel.loc[ev_mask, "year"].unique()
                )
                cohort_years = [
                    y for y in event_years
                    if cfg["cohort_year_min"] <= y <= cfg["cohort_year_max"]
                ]
                print(f"      Event years: {len(event_years)}, "
                      f"Cohort years: {len(cohort_years)}")

                if not cohort_years:
                    print("      No cohort years in range, skipping.")
                    continue

                for cs_name in cfg["control_sets"]:
                    cs_dir = def_dir / "cohort_panels" / f"retain{retain_years}yr" / event_type / cs_name
                    cs_dir.mkdir(parents=True, exist_ok=True)

                    for cy in cohort_years:
                        # Build control sets
                        controls = build_control_sets(
                            pair_panel=pair_panel,
                            firm_history=firm_history,
                            event_type=event_type,
                            cohort_year=cy,
                            window_pre=cfg["window_pre_years"],
                            window_post=cfg["window_post_years"],
                        )
                        ctrl_pairs = controls[cs_name]

                        # Build regression panel
                        reg_panel = build_regression_panel(
                            pair_panel=pair_panel,
                            event_type=event_type,
                            cohort_year=cy,
                            window_pre=cfg["window_pre_years"],
                            window_post=cfg["window_post_years"],
                            control_set=cs_name,
                            control_pairs=ctrl_pairs,
                            post_period_variant=cfg["post_period_variant"],
                        )

                        if reg_panel is None or reg_panel.empty:
                            continue

                        out_path = cs_dir / f"reg_panel_cohort_{cy}.csv"
                        reg_panel.to_csv(out_path, index=False)

                        counts = count_sample_size(reg_panel)
                        counts["definition"] = def_name
                        counts["retain_years"] = retain_years
                        counts["event_type"] = event_type
                        counts["control_set"] = cs_name
                        counts["cohort_year"] = cy
                        all_sample_sizes.append(counts)

                # Summary for this event_type
                n_panels = len([
                    r for r in all_sample_sizes
                    if r["definition"] == def_name
                    and r["retain_years"] == retain_years
                    and r["event_type"] == event_type
                ])
                print(f"      Panels built: {n_panels}")

    # Save sample size summary
    print(f"\n{'='*70}")
    print("SAMPLE SIZE SUMMARY")
    print(f"{'='*70}")
    if all_sample_sizes:
        summary_df = pd.DataFrame(all_sample_sizes)
        summary_path = OUTPUT_ROOT / "sample_size_summary.csv"
        summary_df.to_csv(summary_path, index=False)
        print(f"Saved: {summary_path}")
        print(f"Total records: {len(summary_df)}")

        # Aggregate summary
        print("\n--- By definition, retain_years, event_type, control_set ---")
        agg = summary_df.groupby(
            ["definition", "retain_years", "event_type", "control_set"]
        ).agg(
            total_obs=("N_obs", "sum"),
            mean_treated_pairs=("N_pairs_treated", "mean"),
            mean_control_pairs=("N_pairs_control", "mean"),
            mean_treated_firms=("N_firms_treated_B", "mean"),
            n_cohorts=("cohort_year", "count"),
        ).reset_index()
        print(agg.to_string(index=False))
    else:
        print("No panels built. Check data and parameters.")

    print("\nDone!")


if __name__ == "__main__":
    main()
