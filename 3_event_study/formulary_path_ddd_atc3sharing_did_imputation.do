version 18.0
clear all
set more off
set trace off
// ================================================================
// Purpose:
// Estimate path-frequency-weighted ATC3-sharing DDD contrasts from the
// formulary-path-by-NDC cohort panels.  The regression unit is a complete
// formulary path, NDC, and cohort; state-level CPS members are represented by
// n_path (or the outcome-specific valid-path count).
//
// Output contract matches formulary_plan_ddd_atc3sharing_did_imputation.do:
// - result.csv, sample_before_autosample.csv, sample_after_autosample.csv
// - result.tex, run.log, and failure.log
//
// Input:
// - D:/BoardPharma/data/formulary_path_cohort_data/event/req1/Not/
//   shift_q{0|1}/{plan|state|county}/
//   {event}_path_quarter_cohort_{year}.csv
// - data/formulary_metadata/ndc_first_seen.csv
// - I/O behavior: for each event, the five cohort CSVs are imported once and
//   cached as a temporary Stata dataset after the first-seen filter. Each
//   direction-by-outcome regression reloads that temporary dataset from disk;
//   the original CSVs are not read again within the same event.
//
// Output:
// - csv/formulary_path/ddd/{sample}/{spec}/{event}/{side}/{target}/result.csv
// - csv/formulary_path/ddd/{sample}/{spec}/{event}/{side}/{target}/sample_before_autosample.csv
// - csv/formulary_path/ddd/{sample}/{spec}/{event}/{side}/{target}/sample_after_autosample.csv
// - tex/formulary_path/ddd/{sample}/{spec}/{event}/{side}/{target}/result.tex
// - logs/formulary_path/ddd/{sample}/{spec}/{event}/{side}/{target}/run.log
// - logs/formulary_path/ddd/{sample}/{spec}/failure.log
// ================================================================

* ================= user config =================
local events to_B_still_in_A
* to_B_not_in_A interlock_dissolution
local treatment_groups A B
local targets included tier_raw tier_upgrade tier_downgrade avg_copay_amt prefer
* Uniformly sample this fraction of distinct history_id paths inside each cohort.
* Sampling is independent of n_path; 1 keeps every path and 0 keeps none.
local path_sample_fraction 1
local path_sample_seed 20260818
local outlier_treatment winsorize
local outlier_percentile p95
local req 1
local control not
local include_eventpair 0
local personnel_definition narrow
local large_sample 1
local source_level state
local formulary_time_shift_quarters 1
local first_seen_year_offset -1
local first_seen_quarter 1
local atc atc3
local fe_level 1
local cluster_level firm
local exposure none

if `large_sample' != 1 | "`personnel_definition'" != "narrow" {
    di as error "Formulary regressions require large_sample=1 and personnel_definition=narrow."
    exit 198
}
if `req' != 1 | "`control'" != "not" | `include_eventpair' != 0 {
    di as error "This do-file requires req1, Not controls, and include_eventpair=0."
    exit 198
}
if !inlist("`source_level'", "plan", "state", "county") {
    di as error "source_level must be plan, state, or county."
    exit 198
}
if !((`first_seen_year_offset' == -1 & `first_seen_quarter' == 1) | ///
    (`first_seen_year_offset' == 0 & `first_seen_quarter' == 1) | ///
    (`first_seen_year_offset' == 1 & `first_seen_quarter' == 4)) {
    di as error "First-seen cutoff must be y-1 Q1, y Q1, or y+1 Q4."
    exit 198
}
if "`outlier_treatment'" != "winsorize" | "`outlier_percentile'" != "p95" {
    di as error "Average copay requires upper-tail p95 winsorization."
    exit 198
}
if !inlist(`fe_level', 1, 2) | "`cluster_level'" != "firm" | "`atc'" != "atc3" {
    di as error "This do-file requires FE1/FE2, firm clustering, and ATC3 sharing."
    exit 198
}
if "`exposure'" != "none" {
    di as error "The formulary DDD specification does not use exposure."
    exit 198
}
if !inrange(`path_sample_fraction', 0, 1) {
    di as error "path_sample_fraction must be between 0 and 1."
    exit 198
}

* ================= paths and dependencies =================
local code_path "`c(pwd)'"
local parent_path = regexr("`code_path'", "[/\\][^/\\]+$", "")
local project_path = regexr("`parent_path'", "[/\\][^/\\]+$", "")
local project_path = subinstr("`project_path'", "\\", "/", .)

local path_data_root "D:/BoardPharma/data"
local data_path "`path_data_root'/formulary_path_cohort_data/event/req1/Not/shift_q`formulary_time_shift_quarters'/`source_level'"
local panel_suffix "_planpath"
local panel_label "planpath_ndc"
local panel_level "quarter_planpath"
local unit_label "Unique Formulary-Path-NDC-Cohorts"
local import_stringcols "1 6/9"
local sample_spec "baseline"
if `formulary_time_shift_quarters' != 0 | `first_seen_year_offset' != 0 | `first_seen_quarter' != 1 {
    local seen_offset "`first_seen_year_offset'"
    if `first_seen_year_offset' >= 0 local seen_offset "+`first_seen_year_offset'"
    local sample_spec "shift_q`formulary_time_shift_quarters'_seen_y`seen_offset'_q`first_seen_quarter'"
}
if `path_sample_fraction' < 1 {
    local path_sample_label = subinstr("`path_sample_fraction'", ".", "p", .)
    local sample_spec "`sample_spec'_pathsample`path_sample_label'"
}
local spec_folder "req1_not_wsp95_fe`fe_level'_firm`panel_suffix'"
local csv_root "`project_path'/csv/formulary_path/ddd"
local tex_root "`project_path'/tex/formulary_path/ddd"
local log_root "`project_path'/logs/formulary_path/ddd"
local csv_run_root "`csv_root'/`sample_spec'/`spec_folder'"
local tex_run_root "`tex_root'/`sample_spec'/`spec_folder'"
local log_run_root "`log_root'/`sample_spec'/`spec_folder'"
foreach directory in ///
    "`project_path'/csv" "`project_path'/csv/formulary_path" ///
    "`project_path'/tex" "`project_path'/tex/formulary_path" ///
    "`project_path'/logs" "`project_path'/logs/formulary_path" ///
    "`csv_root'" "`tex_root'" "`log_root'" ///
    "`csv_root'/`sample_spec'" "`tex_root'/`sample_spec'" "`log_root'/`sample_spec'" ///
    "`csv_run_root'" "`tex_run_root'" "`log_run_root'" {
    cap mkdir "`directory'"
}
foreach command in did_imputation esttab estadd {
    capture which `command'
    if _rc {
        di as error "`command' is not installed."
        exit 199
    }
}

local first_seen_file "`project_path'/data/formulary_metadata/ndc_first_seen.csv"
capture confirm file "`first_seen_file'"
if _rc {
    di as error "Missing first-seen metadata: `first_seen_file'"
    exit 601
}
import delimited "`first_seen_file'", clear varnames(1) case(lower) stringcols(1 2)
foreach variable in ndc first_seen_qtime {
    capture confirm variable `variable'
    if _rc {
        di as error "Missing variable `variable' in `first_seen_file'"
        exit 111
    }
}
capture confirm numeric variable first_seen_qtime
if _rc destring first_seen_qtime, replace
assert !missing(ndc) & !missing(first_seen_qtime)
isid ndc
keep ndc first_seen_qtime
tempfile first_seen_metadata
save `first_seen_metadata', replace

local panel_event_vars ///
    event_to_b_not_in_a_a event_to_b_not_in_a_b ///
    event_to_b_still_in_a_a event_to_b_still_in_a_b ///
    event_interlock_dissolution_a event_interlock_dissolution_b

capture program drop _post_planpath_ddd_estimates
program define _post_planpath_ddd_estimates, eclass
    args bmat vmat obs
    ereturn post `bmat' `vmat', obs(`obs')
    ereturn local cmd "did_imputation"
end

* ================= estimation loop =================
set seed `path_sample_seed'
foreach event of local events {
    if "`event'" == "to_B_not_in_A" {
        local imported_share_a "event_to_b_not_in_a_a_sharingatc"
        local imported_share_b "event_to_b_not_in_a_b_sharingatc"
        local other_event_list_a "event_to_b_still_in_a_a event_interlock_dissolution_a"
        local other_event_list_b "event_to_b_still_in_a_b event_interlock_dissolution_b"
        local event_folder "notinA"
    }
    else if "`event'" == "to_B_still_in_A" {
        local imported_share_a "event_to_b_still_in_a_a_sharinga"
        local imported_share_b "event_to_b_still_in_a_b_sharinga"
        local other_event_list_a "event_to_b_not_in_a_a event_interlock_dissolution_a"
        local other_event_list_b "event_to_b_not_in_a_b event_interlock_dissolution_b"
        local event_folder "stillA"
    }
    else if "`event'" == "interlock_dissolution" {
        local imported_share_a "event_interlock_dissolution_a_sh"
        local imported_share_b "event_interlock_dissolution_b_sh"
        local other_event_list_a "event_to_b_not_in_a_a event_to_b_still_in_a_a"
        local other_event_list_b "event_to_b_not_in_a_b event_to_b_still_in_a_b"
        local event_folder "dissolve"
    }

    local first 1
    foreach cohort in 2020 2021 2022 2023 2024 {
        local data_file "`data_path'/`event'_path_quarter_cohort_`cohort'.csv"
        capture confirm file "`data_file'"
        if _rc {
            di as error "Missing cohort file: `data_file'"
            exit 601
        }
        import delimited "`data_file'", clear varnames(1) case(lower) ///
            stringcols(`import_stringcols')
        foreach variable in history_id n_path n_path_copay n_path_prefer ndc boardname ///
            year quarter data_cohort treated_a treated_b sample_a sample_b ///
            `panel_event_vars' `imported_share_a' `imported_share_b' `targets' {
            capture confirm variable `variable'
            if _rc {
                di as error "Missing variable `variable' in `data_file'"
                exit 111
            }
        }
        rename `imported_share_a' cohort_sharing_a
        rename `imported_share_b' cohort_sharing_b
        foreach variable in year quarter data_cohort n_path n_path_copay n_path_prefer ///
            treated_a treated_b sample_a sample_b `panel_event_vars' ///
            cohort_sharing_a cohort_sharing_b `targets' {
            capture confirm numeric variable `variable'
            if _rc destring `variable', replace
        }
        foreach variable in included treated_a treated_b sample_a sample_b ///
            `panel_event_vars' cohort_sharing_a cohort_sharing_b {
            assert inlist(`variable', 0, 1)
        }
        foreach variable in tier_upgrade tier_downgrade {
            assert inlist(`variable', 0, 1) if !missing(`variable')
        }
        assert inrange(prefer, 0, 1) if !missing(prefer)
        assert n_path > 0 & n_path == floor(n_path)
        assert n_path_copay >= 0 & n_path_copay <= n_path & n_path_copay == floor(n_path_copay)
        assert n_path_prefer >= 0 & n_path_prefer <= n_path & n_path_prefer == floor(n_path_prefer)
        assert data_cohort == `cohort'

        * Draw paths uniformly within the cohort.  Every NDC and quarter on a
        * selected path is retained; n_path never affects the draw.
        if `path_sample_fraction' < 1 {
            sort history_id ndc year quarter
            by history_id: gen byte __path_tag = _n == 1
            gen double __path_draw = runiform() if __path_tag
            egen long __path_rank = rank(__path_draw), unique
            egen long __path_count = total(__path_tag)
            gen byte __path_selected = __path_tag & ///
                __path_rank <= ceil(`path_sample_fraction' * __path_count)
            by history_id: egen byte __path_selected_all = max(__path_selected)
            keep if __path_selected_all == 1
            drop __path_tag __path_draw __path_rank __path_count ///
                __path_selected __path_selected_all
        }
        keep history_id n_path n_path_copay n_path_prefer ndc boardname ///
            year quarter data_cohort treated_a treated_b sample_a sample_b ///
            `panel_event_vars' cohort_sharing_a cohort_sharing_b `targets'
        if `first' {
            tempfile event_master
            save `event_master', replace
            local first 0
        }
        else {
            append using `event_master'
            save `event_master', replace
        }
    }

    use `event_master', clear
    merge m:1 ndc using `first_seen_metadata', keepusing(first_seen_qtime)
    assert _merge == 3
    drop _merge
    gen long first_seen_cutoff = ///
        (data_cohort + `first_seen_year_offset') * 4 + `first_seen_quarter'
    keep if first_seen_qtime <= first_seen_cutoff
    drop first_seen_qtime first_seen_cutoff
    compress
    save `event_master', replace

    * The included regression uses event_master with both zeroes and ones.
    * Intensive-margin outcomes use only path-NDC histories covered throughout
    * the available cohort window.
    bysort history_id ndc data_cohort: egen byte __always_included = min(included)
    assert inlist(__always_included, 0, 1)
    keep if __always_included == 1
    drop __always_included
    sort history_id ndc data_cohort year quarter
    by history_id ndc data_cohort: gen byte __first_window_quarter = _n == 1
    foreach tier_change in tier_upgrade tier_downgrade {
        assert !missing(`tier_change') | __first_window_quarter
        replace `tier_change' = 0 if missing(`tier_change') & __first_window_quarter
        assert !missing(`tier_change')
    }
    assert !missing(tier_raw)
    drop __first_window_quarter
    compress
    tempfile event_master_always_included
    save `event_master_always_included', replace

    foreach treatment_group of local treatment_groups {
        local side = lower("`treatment_group'")
        local sample_var "sample_`side'"
        local treated_var "treated_`side'"
        local sharing_source "cohort_sharing_`side'"
        local other_event_list "`other_event_list_`side''"

        foreach target of local targets {
            local csv_dir "`csv_run_root'/`event_folder'/`treatment_group'/`target'"
            local tex_dir "`tex_run_root'/`event_folder'/`treatment_group'/`target'"
            local log_dir "`log_run_root'/`event_folder'/`treatment_group'/`target'"
            foreach directory in ///
                "`csv_run_root'/`event_folder'" "`tex_run_root'/`event_folder'" "`log_run_root'/`event_folder'" ///
                "`csv_run_root'/`event_folder'/`treatment_group'" ///
                "`tex_run_root'/`event_folder'/`treatment_group'" ///
                "`log_run_root'/`event_folder'/`treatment_group'" ///
                "`csv_dir'" "`tex_dir'" "`log_dir'" {
                cap mkdir "`directory'"
            }
            local csv_file "`csv_dir'/result.csv"
            local sample_before_file "`csv_dir'/sample_before_autosample.csv"
            local sample_after_file "`csv_dir'/sample_after_autosample.csv"
            local tex_file "`tex_dir'/result.tex"
            foreach file in "`csv_file'" "`sample_before_file'" "`sample_after_file'" "`tex_file'" {
                capture erase "`file'"
            }
            capture log close
            log using "`log_dir'/run.log", replace text
            di as text "Formulary-planpath ATC3-sharing DDD: `event', `treatment_group', `target'"

            if "`target'" == "included" {
                use `event_master', clear
            }
            else {
                use `event_master_always_included', clear
                * Coverage alone does not guarantee complete copay/prefer data.
                * Balance the path-NDC panel separately for the current outcome.
                bysort history_id ndc data_cohort: egen byte __target_complete = ///
                    min(!missing(`target'))
                keep if __target_complete == 1
                drop __target_complete
                assert !missing(`target')
            }
            local weight_var n_path
            if "`target'" == "avg_copay_amt" {
                local weight_var n_path_copay
                keep if n_path_copay > 0
            }
            else if "`target'" == "prefer" {
                local weight_var n_path_prefer
                keep if n_path_prefer > 0
            }
            keep if `sample_var' == 1
            gen byte treat = `treated_var'
            count
            if r(N) == 0 {
                di as error "No observations remain after sample and outcome-weight filtering."
                log close
                continue
            }

            tempvar q1_share_min q1_share_max
            bysort ndc boardname data_cohort: egen byte `q1_share_min' = ///
                min(cond(year == data_cohort & quarter == 1, `sharing_source', .))
            bysort ndc boardname data_cohort: egen byte `q1_share_max' = ///
                max(cond(year == data_cohort & quarter == 1, `sharing_source', .))
            assert !missing(`q1_share_min') & `q1_share_min' == `q1_share_max'
            gen byte atc_sharing = `q1_share_max'
            replace atc_sharing = 0 if treat == 0
            drop `q1_share_min' `q1_share_max'

            gen double target_raw = `target'
            quietly summarize `target' [aw=`weight_var'], detail
            if r(N) == 0 {
                di as error "Target `target' is missing for every stacked observation."
                log close
                continue
            }
            if "`target'" == "avg_copay_amt" {
                * Winsorization cutoff is the unweighted P95; regression remains weighted below.
                quietly summarize `target', detail
                local p95_value = r(p95)
                replace `target' = `p95_value' if `target' > `p95_value' & !missing(`target')
            }

            gen int q_time = yq(year, quarter)
            format q_time %tq
            assert inrange(quarter, 1, 4)
            assert !missing(history_id) & !missing(ndc) & !missing(boardname) & `weight_var' > 0
            bysort ndc boardname data_cohort: assert treat == treat[1]
            bysort ndc boardname data_cohort: assert atc_sharing == atc_sharing[1]
            bysort ndc data_cohort boardname: gen byte board_tag = _n == 1
            bysort ndc data_cohort: egen int board_count = total(board_tag)
            assert board_count == 1
            drop board_tag board_count
            egen long id = group(history_id ndc data_cohort)
            egen long history_cohort_q = group(history_id data_cohort q_time)
            isid id q_time
            gen byte expected_quarters = 12
            replace expected_quarters = 11 if `formulary_time_shift_quarters' == 1 & data_cohort == 2020
            replace expected_quarters = 11 if `formulary_time_shift_quarters' == 0 & data_cohort == 2024
            bysort id: assert _N == expected_quarters[1]
            drop expected_quarters
            gen int event_cohort_q = yq(data_cohort, 1) if treat == 1
            format event_cohort_q %tq
            gen byte pre_period = q_time < yq(data_cohort, 1)
            bysort id (q_time): assert treat == treat[1]
            bysort id (q_time): assert atc_sharing == atc_sharing[1]

            * Store the same before-autosample fields as the existing formulary DDD.
            gen byte estimation_input = !missing(`target')
            foreach group in control notshare share {
                local condition_`group' "estimation_input & treat == 0"
            }
            local condition_notshare "estimation_input & treat == 1 & atc_sharing == 0"
            local condition_share "estimation_input & treat == 1 & atc_sharing == 1"
            quietly count if estimation_input
            local N_obs_before = r(N)
            foreach group in control notshare share {
                quietly count if `condition_`group''
                local N_`group'_before = r(N)
                egen byte tag_id_`group'_before = tag(id) if `condition_`group''
                quietly count if tag_id_`group'_before
                local product_`group'_before = r(N)
                egen byte tag_board_`group'_before = tag(boardname) if `condition_`group''
                quietly count if tag_board_`group'_before
                local board_`group'_before = r(N)
                egen byte tag_board_cohort_`group'_before = tag(boardname data_cohort) if `condition_`group''
                quietly count if tag_board_cohort_`group'_before
                local board_cohort_`group'_before = r(N)
            }
            tempfile sample_before_results
            tempname sample_before_posth
            postfile `sample_before_posth' str20 sample_stage str30 event str1 direction str20 target ///
                double N_obs N_control N_notshare N_share ///
                double product_control product_notshare product_share ///
                double board_control board_notshare board_share ///
                double board_cohort_control board_cohort_notshare board_cohort_share ///
                using `sample_before_results', replace
            post `sample_before_posth' ("before_autosample") ("`event'") ("`treatment_group'") ("`target'") ///
                (`N_obs_before') (`N_control_before') (`N_notshare_before') (`N_share_before') ///
                (`product_control_before') (`product_notshare_before') (`product_share_before') ///
                (`board_control_before') (`board_notshare_before') (`board_share_before') ///
                (`board_cohort_control_before') (`board_cohort_notshare_before') (`board_cohort_share_before')
            postclose `sample_before_posth'
            preserve
            use `sample_before_results', clear
            export delimited using "`sample_before_file'", replace
            restore

            local other_history_list ""
            foreach other_event of local other_event_list {
                tempvar first_other_q other_history
                bysort boardname data_cohort: egen `first_other_q' = min(cond(`other_event' == 1, q_time, .))
                gen byte `other_history' = !missing(`first_other_q') & q_time >= `first_other_q'
                drop `first_other_q'
                quietly summarize `other_history', meanonly
                if r(max) > 0 local other_history_list "`other_history_list' `other_history'"
            }
            local fe_spec "id q_time `other_history_list'"
            if `fe_level' == 2 local fe_spec "id history_cohort_q `other_history_list'"
            gen byte atc_sharing_het = atc_sharing if treat == 1
            replace atc_sharing_het = 0 if treat == 0
            capture noisily did_imputation `target' id q_time event_cohort_q [aw=`weight_var'], ///
                fe(`fe_spec') hetby(atc_sharing_het) autosample tol(0.1) minn(0) cluster(boardname)
            local did_rc = _rc
            if `did_rc' {
                capture confirm file "`log_run_root'/failure.log"
                if _rc {
                    file open failure_log using "`log_run_root'/failure.log", write text replace
                }
                else {
                    file open failure_log using "`log_run_root'/failure.log", write text append
                }
                file write failure_log "event=`event', direction=`treatment_group', target=`target', rc=`did_rc'" _n
                file close failure_log
                log close
                continue
            }

            tempvar didimp_esample
            gen byte `didimp_esample' = e(sample)
            quietly count if `didimp_esample'
            local N_obs = r(N)
            foreach group in control notshare share {
                local condition_`group' "`didimp_esample' & treat == 0"
            }
            local condition_notshare "`didimp_esample' & treat == 1 & atc_sharing == 0"
            local condition_share "`didimp_esample' & treat == 1 & atc_sharing == 1"
            foreach group in control notshare share {
                quietly count if `condition_`group''
                local N_`group' = r(N)
                egen byte tag_id_`group' = tag(id) if `condition_`group''
                quietly count if tag_id_`group'
                local product_`group' = r(N)
                egen byte tag_board_`group' = tag(boardname) if `condition_`group''
                quietly count if tag_board_`group'
                local board_`group' = r(N)
                egen byte tag_board_cohort_`group' = tag(boardname data_cohort) if `condition_`group''
                quietly count if tag_board_cohort_`group'
                local board_cohort_`group' = r(N)
            }
            quietly summarize `target' if `didimp_esample' & treat == 1 & atc_sharing == 0 & pre_period, meanonly
            local premean_notshare = r(mean)
            quietly summarize `target' if `didimp_esample' & treat == 1 & atc_sharing == 1 & pre_period, meanonly
            local premean_share = r(mean)
            tempfile sample_after_results
            tempname sample_after_posth
            postfile `sample_after_posth' str20 sample_stage str30 event str1 direction str20 target ///
                double N_obs N_control N_notshare N_share ///
                double product_control product_notshare product_share ///
                double board_control board_notshare board_share ///
                double board_cohort_control board_cohort_notshare board_cohort_share ///
                using `sample_after_results', replace
            post `sample_after_posth' ("after_autosample") ("`event'") ("`treatment_group'") ("`target'") ///
                (`N_obs') (`N_control') (`N_notshare') (`N_share') ///
                (`product_control') (`product_notshare') (`product_share') ///
                (`board_control') (`board_notshare') (`board_share') ///
                (`board_cohort_control') (`board_cohort_notshare') (`board_cohort_share')
            postclose `sample_after_posth'
            preserve
            use `sample_after_results', clear
            export delimited using "`sample_after_file'", replace
            restore

            capture noisily lincom tau_0
            if _rc {
                local failed "lincom tau_0 failed"
            }
            else {
                local beta1 = r(estimate)
                local beta1_se = r(se)
                local beta1_p = r(p)
                capture noisily lincom tau_1 - tau_0
                if _rc local failed "lincom tau_1 - tau_0 failed"
                else {
                    local beta2 = r(estimate)
                    local beta2_se = r(se)
                    local beta2_p = r(p)
                    capture noisily lincom tau_1
                    if _rc local failed "lincom tau_1 failed"
                    else {
                        local te_share = r(estimate)
                        local te_share_se = r(se)
                        local te_share_p = r(p)
                        local failed ""
                    }
                }
            }
            if "`failed'" != "" {
                capture confirm file "`log_run_root'/failure.log"
                if _rc {
                    file open failure_log using "`log_run_root'/failure.log", write text replace
                }
                else {
                    file open failure_log using "`log_run_root'/failure.log", write text append
                }
                file write failure_log "event=`event', direction=`treatment_group', target=`target', failed=`failed'" _n
                file close failure_log
                log close
                continue
            }
            local pcteff_notshare = cond(!missing(`premean_notshare') & `premean_notshare' != 0, 100 * `beta1' / abs(`premean_notshare'), .)
            local pcteff_share = cond(!missing(`premean_share') & `premean_share' != 0, 100 * `te_share' / abs(`premean_share'), .)

            * Same result.csv schema as SSR/formulary DDD; unsupported exposure fields remain missing.
            tempfile run_results
            tempname posth
            postfile `posth' str24 panel_level str30 event str15 control str25 control_variation ///
                str15 std str15 event_type str20 target str20 model str15 control_var ///
                str15 control_kappa str15 atc str15 control_atc str15 exposure_type ///
                str15 exposure_metric str32 exposure_var ///
                double exposure_raw_min exposure_raw_mean exposure_raw_max ///
                double exposure_dm_min exposure_dm_mean exposure_dm_max exposure_share_id_n ///
                double beta1 beta1_se beta1_p beta2 beta2_se beta2_p beta3 beta3_se beta3_p ///
                double te_share te_share_se te_share_p ///
                double gap_min gap_min_se gap_min_p gap_mean gap_mean_se gap_mean_p gap_max gap_max_se gap_max_p ///
                double share_att_min share_att_min_se share_att_min_p share_att_mean share_att_mean_se share_att_mean_p ///
                double share_att_max share_att_max_se share_att_max_p ///
                double pcteff_notshare pcteff_share premean_notshare premean_share ///
                double N_obs N_control N_notshare N_share product_control product_notshare product_share ///
                double board_control board_notshare board_share ///
                double board_cohort_control board_cohort_notshare board_cohort_share ///
                using `run_results', replace
            post `posth' ("`panel_level'") ("`event'") ("not") ("all_controls") ///
                ("raw") ("event") ("`target'") ("did_imputation") ("other_event") ///
                ("none") ("atc3") ("none") ("none") ("none") ("none") ///
                (.) (.) (.) (.) (.) (.) (.) ///
                (`beta1') (`beta1_se') (`beta1_p') (`beta2') (`beta2_se') (`beta2_p') (.) (.) (.) ///
                (`te_share') (`te_share_se') (`te_share_p') ///
                (.) (.) (.) (.) (.) (.) (.) (.) (.) ///
                (.) (.) (.) (.) (.) (.) (.) (.) (.) ///
                (`pcteff_notshare') (`pcteff_share') (`premean_notshare') (`premean_share') ///
                (`N_obs') (`N_control') (`N_notshare') (`N_share') ///
                (`product_control') (`product_notshare') (`product_share') ///
                (`board_control') (`board_notshare') (`board_share') ///
                (`board_cohort_control') (`board_cohort_notshare') (`board_cohort_share')
            postclose `posth'
            preserve
            use `run_results', clear
            export delimited using "`csv_file'", replace
            restore

            tempname b V
            matrix `b' = (`beta1', `beta2')
            matrix colnames `b' = beta1 beta2
            matrix `V' = (`beta1_se'^2, 0 \ 0, `beta2_se'^2)
            matrix rownames `V' = beta1 beta2
            matrix colnames `V' = beta1 beta2
            _post_planpath_ddd_estimates `b' `V' `N_obs'
            estimates store planpath_ddd
            estadd scalar premean_notshare = `premean_notshare'
            estadd scalar premean_share = `premean_share'
            estadd scalar pcteff_notshare = `pcteff_notshare'
            estadd scalar pcteff_share = `pcteff_share'
            estadd scalar N_control = `N_control'
            estadd scalar N_notshare = `N_notshare'
            estadd scalar N_share = `N_share'
            estadd scalar product_control = `product_control'
            estadd scalar product_notshare = `product_notshare'
            estadd scalar product_share = `product_share'
            estadd scalar board_control = `board_control'
            estadd scalar board_notshare = `board_notshare'
            estadd scalar board_share = `board_share'
            estadd scalar board_cohort_control = `board_cohort_control'
            estadd scalar board_cohort_notshare = `board_cohort_notshare'
            estadd scalar board_cohort_share = `board_cohort_share'
            foreach scalar in ///
                exposure_raw_min exposure_raw_mean exposure_raw_max ///
                exposure_dm_min exposure_dm_mean exposure_dm_max exposure_share_id_n ///
                gap_min gap_min_se gap_min_p gap_mean gap_mean_se gap_mean_p gap_max gap_max_se gap_max_p ///
                share_att_min share_att_min_se share_att_min_p ///
                share_att_mean share_att_mean_se share_att_mean_p ///
                share_att_max share_att_max_se share_att_max_p {
                estadd scalar `scalar' = .
            }
            tempfile tex_fragment
            esttab planpath_ddd using "`tex_fragment'", replace booktabs se star(* 0.10 ** 0.05 *** 0.01) ///
                keep(beta1 beta2) ///
                coeflabels(beta1 "beta1: ATT (Not Share)" beta2 "beta2: ATT gap (Share - Not Share)") ///
                mtitles("`target'") ///
                stats(premean_notshare premean_share pcteff_notshare pcteff_share ///
                    exposure_raw_min exposure_raw_mean exposure_raw_max exposure_dm_min exposure_dm_mean exposure_dm_max exposure_share_id_n ///
                    gap_min gap_min_se gap_min_p gap_mean gap_mean_se gap_mean_p gap_max gap_max_se gap_max_p ///
                    share_att_min share_att_min_se share_att_min_p share_att_mean share_att_mean_se share_att_mean_p share_att_max share_att_max_se share_att_max_p ///
                    N N_control N_share N_notshare board_control board_share board_notshare ///
                    board_cohort_control board_cohort_share board_cohort_notshare product_control product_share product_notshare, ///
                    fmt(3 3 2 2 3 3 3 3 3 3 0 3 3 3 3 3 3 3 3 3 3 3 3 3 3 3 3 3 3 0 0 0 0 0 0 0 0 0 0 0 0 0 0) ///
                    labels("Pre-treatment mean Y (Not Share)" "Pre-treatment mean Y (Share)" ///
                    "Percent effect (Not Share)" "Percent effect (Share, mean exposure)" ///
                    "Exposure raw min (Share IDs)" "Exposure raw mean (Share IDs)" "Exposure raw max (Share IDs)" ///
                    "Exposure demeaned min" "Exposure demeaned mean" "Exposure demeaned max" "Unique Share IDs for exposure" ///
                    "Share - Not Share gap at min" "SE: gap at min" "p-value: gap at min" ///
                    "Share - Not Share gap at mean" "SE: gap at mean" "p-value: gap at mean" ///
                    "Share - Not Share gap at max" "SE: gap at max" "p-value: gap at max" ///
                    "Share ATT at min" "SE: Share ATT at min" "p-value: Share ATT at min" ///
                    "Share ATT at mean" "SE: Share ATT at mean" "p-value: Share ATT at mean" ///
                    "Share ATT at max" "SE: Share ATT at max" "p-value: Share ATT at max" ///
                    "Observations Total" "Observations (Control)" "Observations (Share, Treated)" "Observations (Not Share, Treated)" ///
                    "Unique Boards (Control)" "Unique Boards (Share, Treated)" "Unique Boards (Not Share, Treated)" ///
                    "Board-Cohorts (Control)" "Board-Cohorts (Share, Treated)" "Board-Cohorts (Not Share, Treated)" ///
                    "`unit_label' (Control)" "`unit_label' (Share, Treated)" "`unit_label' (Not Share, Treated)")) ///
                title("did_imputation DDD: `event', event, Not | `treatment_group' | FE `fe_level' | atc=atc3 | planpath-weighted")
            tempname tex_in tex_out
            file open `tex_in' using "`tex_fragment'", read text
            file open `tex_out' using "`tex_file'", write text replace
            file write `tex_out' "\documentclass[11pt]{article}" _n
            file write `tex_out' "\usepackage{booktabs}" _n
            file write `tex_out' "\begin{document}" _n
            file read `tex_in' tex_line
            while r(eof) == 0 {
                file write `tex_out' "`tex_line'" _n
                file read `tex_in' tex_line
            }
            file write `tex_out' "\end{document}" _n
            file close `tex_in'
            file close `tex_out'
            estimates drop planpath_ddd
            log close
        }
    }
}

di as result "Saved SSR-format formulary-planpath ATC3-sharing DDD outputs."
