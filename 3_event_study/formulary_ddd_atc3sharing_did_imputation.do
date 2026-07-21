version 18.0
clear all
set more off
set trace off

// ================================================================
// Purpose:
// Estimate direction-specific, drug-level ATC3-sharing DDD contrasts with
// did_imputation and no exposure variable.
//
// Process:
// 1. Stack combined-direction Not-control cohorts and select A or B through
//    the direction-specific sample flag.
// 2. Winsorize each raw outcome at its stacked-sample p95.
// 3. Add same-direction other-event histories and FE1 identifiers.
// 4. Run did_imputation with hetby(ATC3 sharing) and firm clustering.
// 5. Report non-sharing ATT, sharing ATT, their DDD contrast, and all sample
//    statistics after did_imputation autosample selection.
//
// Input:
// - data/formulary_cohort_data/event/req1/Not/
//   {event}_quarter_cohort_{year}.csv
//
// Output:
// - csv/formulary_ddd_atc3sharing_did_imputation/*.csv
// - tex/formulary_ddd_atc3sharing_did_imputation/*.tex
// - logs/formulary_ddd_atc3sharing_did_imputation/*.log
// ================================================================

* ================= user config =================
local events interlock_dissolution to_B_not_in_A to_B_still_in_A
local treatment_groups A B
local targets included_count included_share mean_tiera mean_tier_raw
local outlier_treatment winsorize
local outlier_percentile p95
local req 1
local control not
local include_eventpair 0
local personnel_definition narrow
local large_sample 1
local atc atc3
local fe_level 1
local cluster_level firm
local exposure none

if `large_sample' != 1 | "`personnel_definition'" != "narrow" {
    di as error "Formulary regressions require large_sample=1 and personnel_definition=narrow."
    exit 198
}
if `req' != 1 | "`control'" != "not" | `include_eventpair' != 0 {
    di as error "Formulary regressions require req1, Not controls, and include_eventpair=0."
    exit 198
}
if "`outlier_treatment'" != "winsorize" | "`outlier_percentile'" != "p95" {
    di as error "This do-file is fixed at upper-tail p95 winsorization."
    exit 198
}
if !inlist(`fe_level', 1, 2) | "`cluster_level'" != "firm" | "`atc'" != "atc3" {
    di as error "This do-file requires FE1 or FE2, firm clustering, and ATC3 sharing."
    exit 198
}
if "`exposure'" != "none" {
    di as error "The formulary DDD specification does not use exposure."
    exit 198
}

* ================= paths =================
local code_path "`c(pwd)'"
local parent_path = regexr("`code_path'", "[/\\][^/\\]+$", "")
local project_path = regexr("`parent_path'", "[/\\][^/\\]+$", "")
local project_path = subinstr("`project_path'", "\", "/", .)

local data_path "`project_path'/data/formulary_cohort_data/event/req1/Not"
local csv_root "`project_path'/csv/formulary_ddd_atc3sharing_did_imputation"
local tex_root "`project_path'/tex/formulary_ddd_atc3sharing_did_imputation"
local log_root "`project_path'/logs/formulary_ddd_atc3sharing_did_imputation"
cap mkdir "`project_path'/csv"
cap mkdir "`project_path'/tex"
cap mkdir "`project_path'/logs"
cap mkdir "`csv_root'"
cap mkdir "`tex_root'"
cap mkdir "`log_root'"

capture which did_imputation
if _rc {
    di as error "did_imputation is not installed. Install it before running this do-file."
    exit 199
}
capture which esttab
if _rc {
    di as error "esttab is not installed. Install estout before running this do-file."
    exit 199
}
capture which estadd
if _rc {
    di as error "estadd is not installed. Install estout before running this do-file."
    exit 199
}

capture program drop _post_formulary_ddd_estimates
program define _post_formulary_ddd_estimates, eclass
    args bmat vmat obs
    ereturn post `bmat' `vmat', obs(`obs')
    ereturn local cmd "did_imputation"
end

* ================= estimation loop =================
foreach event of local events {
    local cohort_list "2020 2021 2022 2023 2024"

    foreach treatment_group of local treatment_groups {
        local side = lower("`treatment_group'")
        local sample_var "sample_`side'"
        local treated_var "treated_`side'"
        local sharing_var "sharingatc3_`side'"

        local other_event_list ""
        if "`event'" == "to_B_not_in_A" {
            local other_event_list ///
                "event_to_b_still_in_a_`side' event_interlock_dissolution_`side'"
        }
        else if "`event'" == "to_B_still_in_A" {
            local other_event_list ///
                "event_to_b_not_in_a_`side' event_interlock_dissolution_`side'"
        }
        else if "`event'" == "interlock_dissolution" {
            local other_event_list ///
                "event_to_b_not_in_a_`side' event_to_b_still_in_a_`side'"
        }
        else {
            di as error "Unsupported event: `event'"
            exit 198
        }

        foreach target of local targets {
            local file_stub "`event'_`treatment_group'_`target'_req1_not_wsp95_fe`fe_level'_clusterfirm"
            local csv_file "`csv_root'/`file_stub'.csv"
            local tex_file "`tex_root'/`file_stub'.tex"
            capture erase "`csv_file'"
            capture erase "`tex_file'"
            capture log close
            log using "`log_root'/`file_stub'.log", replace text
            di as text "Formulary ATC3-sharing DDD: `event', side `treatment_group', target `target'"

            local first 1
            foreach cohort of local cohort_list {
                local data_file "`data_path'/`event'_quarter_cohort_`cohort'.csv"
                capture confirm file "`data_file'"
                if _rc {
                    di as error "Missing cohort file: `data_file'"
                    log close
                    exit 601
                }

                import delimited "`data_file'", clear varnames(1) case(lower) ///
                    stringcols(1 2 3 7)
                foreach required in ndc boardname year quarter data_cohort `sample_var' `treated_var' `sharing_var' `target' {
                    capture confirm variable `required'
                    if _rc {
                        di as error "Missing variable `required' in `data_file'"
                        log close
                        exit 111
                    }
                }
                assert data_cohort == `cohort'
                keep if `sample_var' == 1
                gen byte treated_in_stack = `treated_var'
                gen byte atc_sharing = `sharing_var'

                if `first' == 1 {
                    tempfile master
                    save `master', replace
                    local first 0
                }
                else {
                    append using `master'
                    save `master', replace
                }
            }

            use `master', clear
            count
            if r(N) == 0 {
                di as error "No observations remain after direction-specific sample filtering."
                log close
                continue
            }

            gen double target_raw = `target'
            quietly summarize `target', detail
            if r(N) == 0 {
                di as error "Target `target' is missing for every stacked observation."
                log close
                continue
            }
            local p95_value = r(p95)
            replace `target' = `p95_value' if `target' > `p95_value' & !missing(`target')

            gen int q_time = yq(year, quarter)
            format q_time %tq
            assert inrange(quarter, 1, 4)
            gen int lag_formulary_year = year + (quarter == 4)
            local time_fe_var "q_time"
            if `fe_level' == 2 {
                local time_fe_var "lag_formulary_year"
            }
            assert !missing(ndc) & !missing(boardname)
            bysort ndc data_cohort boardname: gen byte board_tag = _n == 1
            bysort ndc data_cohort: egen int board_count = total(board_tag)
            assert board_count == 1
            drop board_tag board_count
            egen long id = group(ndc data_cohort)
            isid id q_time

            gen byte treat = treated_in_stack
            gen int event_cohort_q = yq(data_cohort, 1) if treat == 1
            format event_cohort_q %tq
            gen byte post = q_time >= yq(data_cohort, 1)
            gen byte pre_period = q_time < yq(data_cohort, 1)
            bysort id (q_time): assert treat == treat[1]
            bysort id (q_time): assert atc_sharing == atc_sharing[1]

            local other_history_list ""
            foreach other_event of local other_event_list {
                capture confirm variable `other_event'
                if _rc {
                    di as error "Missing same-direction other-event variable: `other_event'"
                    log close
                    exit 111
                }
                tempvar first_other_q other_history
                bysort boardname data_cohort: egen `first_other_q' = ///
                    min(cond(`other_event' == 1, q_time, .))
                gen byte `other_history' = ///
                    !missing(`first_other_q') & q_time >= `first_other_q'
                drop `first_other_q'
                quietly summarize `other_history', meanonly
                if r(max) > 0 {
                    local other_history_list "`other_history_list' `other_history'"
                }
            }

            local fe_spec "id `time_fe_var' `other_history_list'"
            gen byte atc_sharing_het = atc_sharing if treat == 1
            replace atc_sharing_het = 0 if treat == 0

            capture noisily did_imputation `target' id q_time event_cohort_q, ///
                fe(`fe_spec') hetby(atc_sharing_het) ///
                autosample tol(0.1) minn(0) cluster(boardname)
            local did_rc = _rc
            if `did_rc' != 0 {
                di as error "did_imputation DDD failed with r(`did_rc')."
                capture file close failure_log
                capture confirm file "`log_root'/failure.log"
                if _rc {
                    file open failure_log using "`log_root'/failure.log", write text replace
                }
                else {
                    file open failure_log using "`log_root'/failure.log", write text append
                }
                file write failure_log ///
                    "event=`event', direction=`treatment_group', target=`target', rc=`did_rc'" _n
                file close failure_log
                log close
                continue
            }

            tempvar didimp_esample
            gen byte `didimp_esample' = e(sample)
            quietly count if `didimp_esample'
            local N_obs_didimp = r(N)

            * Match the SSR table statistics to the autosample-adjusted sample.
            quietly summarize `target' if ///
                `didimp_esample' & treat == 1 & atc_sharing == 0 & pre_period == 1, meanonly
            local premean_notshare = r(mean)
            quietly summarize `target' if ///
                `didimp_esample' & treat == 1 & atc_sharing == 1 & pre_period == 1, meanonly
            local premean_share = r(mean)

            quietly count if `didimp_esample' & treat == 0
            local N_control = r(N)
            quietly count if `didimp_esample' & treat == 1 & atc_sharing == 0
            local N_notshare = r(N)
            quietly count if `didimp_esample' & treat == 1 & atc_sharing == 1
            local N_share = r(N)

            tempvar tag_id_control tag_id_notshare tag_id_share
            egen byte `tag_id_control' = tag(id) if `didimp_esample' & treat == 0
            quietly count if `tag_id_control' == 1
            local product_control = r(N)
            egen byte `tag_id_notshare' = tag(id) if ///
                `didimp_esample' & treat == 1 & atc_sharing == 0
            quietly count if `tag_id_notshare' == 1
            local product_notshare = r(N)
            egen byte `tag_id_share' = tag(id) if ///
                `didimp_esample' & treat == 1 & atc_sharing == 1
            quietly count if `tag_id_share' == 1
            local product_share = r(N)

            tempvar tag_board_control tag_board_notshare tag_board_share
            egen byte `tag_board_control' = tag(boardname) if ///
                `didimp_esample' & treat == 0
            quietly count if `tag_board_control' == 1
            local board_control = r(N)
            egen byte `tag_board_notshare' = tag(boardname) if ///
                `didimp_esample' & treat == 1 & atc_sharing == 0
            quietly count if `tag_board_notshare' == 1
            local board_notshare = r(N)
            egen byte `tag_board_share' = tag(boardname) if ///
                `didimp_esample' & treat == 1 & atc_sharing == 1
            quietly count if `tag_board_share' == 1
            local board_share = r(N)

            * Follow the SSR no-exposure coefficient definitions exactly:
            * beta1 = tau_0; beta2 = tau_1 - tau_0; te_share = tau_1.
            local beta0 = .
            local beta0_se = .
            local beta0_p = .
            local beta1 = .
            local beta1_se = .
            local beta1_p = .
            local beta2 = .
            local beta2_se = .
            local beta2_p = .
            local te_share = .
            local te_share_se = .
            local te_share_p = .
            local failure_reason ""

            capture noisily lincom tau_0
            local lincom_beta0_rc = _rc
            if `lincom_beta0_rc' != 0 {
                local failure_reason "lincom tau_0 failed: r(`lincom_beta0_rc')"
            }
            else {
                local beta0 = r(estimate)
                local beta0_se = r(se)
                local beta0_p = r(p)
            }

            if "`failure_reason'" == "" {
                capture noisily lincom tau_1 - tau_0
                local lincom_beta1_rc = _rc
                if `lincom_beta1_rc' != 0 {
                    local failure_reason ///
                        "lincom tau_1 - tau_0 failed: r(`lincom_beta1_rc')"
                }
                else {
                    local beta1 = r(estimate)
                    local beta1_se = r(se)
                    local beta1_p = r(p)
                }
            }

            if "`failure_reason'" == "" {
                capture noisily lincom tau_1
                local lincom_share_rc = _rc
                if `lincom_share_rc' != 0 {
                    local failure_reason "lincom tau_1 failed: r(`lincom_share_rc')"
                }
                else {
                    local te_share = r(estimate)
                    local te_share_se = r(se)
                    local te_share_p = r(p)
                }
            }

            if "`failure_reason'" != "" {
                di as error "Skipping specification: `failure_reason'"
                capture file close failure_log
                capture confirm file "`log_root'/failure.log"
                if _rc {
                    file open failure_log using "`log_root'/failure.log", write text replace
                }
                else {
                    file open failure_log using "`log_root'/failure.log", write text append
                }
                file write failure_log ///
                    "event=`event', direction=`treatment_group', target=`target', failed=`failure_reason'" _n
                file close failure_log
                capture erase "`csv_file'"
                capture erase "`tex_file'"
                log close
                continue
            }

            local pcteff_notshare = .
            if !missing(`premean_notshare') & `premean_notshare' != 0 {
                local pcteff_notshare = 100 * `beta0' / abs(`premean_notshare')
            }
            local pcteff_share = .
            if !missing(`premean_share') & `premean_share' != 0 {
                local pcteff_share = 100 * `te_share' / abs(`premean_share')
            }

            * Exposure-only fields remain missing, exactly as in SSR's
            * supported no-exposure branch.
            local exposure_raw_min = .
            local exposure_raw_mean = .
            local exposure_raw_max = .
            local exposure_dm_min = .
            local exposure_dm_mean = .
            local exposure_dm_max = .
            local exposure_share_id_n = .
            foreach point in min mean max {
                local gap_`point' = .
                local gap_`point'_se = .
                local gap_`point'_p = .
                local share_att_`point' = .
                local share_att_`point'_se = .
                local share_att_`point'_p = .
            }

            tempname didimp_b didimp_V
            matrix `didimp_b' = (`beta0', `beta1')
            matrix colnames `didimp_b' = beta1 beta2
            matrix `didimp_V' = J(2, 2, 0)
            matrix rownames `didimp_V' = beta1 beta2
            matrix colnames `didimp_V' = beta1 beta2
            matrix `didimp_V'[1, 1] = `beta0_se'^2
            matrix `didimp_V'[2, 2] = `beta1_se'^2
            _post_formulary_ddd_estimates `didimp_b' `didimp_V' `N_obs_didimp'
            estimates store formulary_ddd

            tempfile run_results
            tempname posth
            postfile `posth' ///
                str12 panel_level str30 event str15 control ///
                str25 control_variation str15 std str15 event_type ///
                str20 target str20 model str15 control_var ///
                str15 control_kappa str15 atc str15 control_atc ///
                str15 exposure_type str15 exposure_metric str32 exposure_var ///
                double exposure_raw_min exposure_raw_mean exposure_raw_max ///
                double exposure_dm_min exposure_dm_mean exposure_dm_max ///
                double exposure_share_id_n ///
                double beta1 beta1_se beta1_p ///
                double beta2 beta2_se beta2_p ///
                double beta3 beta3_se beta3_p ///
                double te_share te_share_se te_share_p ///
                double gap_min gap_min_se gap_min_p ///
                double gap_mean gap_mean_se gap_mean_p ///
                double gap_max gap_max_se gap_max_p ///
                double share_att_min share_att_min_se share_att_min_p ///
                double share_att_mean share_att_mean_se share_att_mean_p ///
                double share_att_max share_att_max_se share_att_max_p ///
                double pcteff_notshare pcteff_share ///
                double premean_notshare premean_share ///
                double N_obs N_control N_notshare N_share ///
                double product_control product_notshare product_share ///
                double board_control board_notshare board_share ///
                using `run_results', replace

            post `posth' ///
                ("quarter") ("`event'") ("not") ("all_controls") ///
                ("raw") ("event") ("`target'") ("did_imputation") ///
                ("other_event") ("none") ("atc3") ("none") ///
                ("none") ("none") ("none") ///
                (`exposure_raw_min') (`exposure_raw_mean') (`exposure_raw_max') ///
                (`exposure_dm_min') (`exposure_dm_mean') (`exposure_dm_max') ///
                (`exposure_share_id_n') ///
                (`beta0') (`beta0_se') (`beta0_p') ///
                (`beta1') (`beta1_se') (`beta1_p') ///
                (`beta2') (`beta2_se') (`beta2_p') ///
                (`te_share') (`te_share_se') (`te_share_p') ///
                (`gap_min') (`gap_min_se') (`gap_min_p') ///
                (`gap_mean') (`gap_mean_se') (`gap_mean_p') ///
                (`gap_max') (`gap_max_se') (`gap_max_p') ///
                (`share_att_min') (`share_att_min_se') (`share_att_min_p') ///
                (`share_att_mean') (`share_att_mean_se') (`share_att_mean_p') ///
                (`share_att_max') (`share_att_max_se') (`share_att_max_p') ///
                (`pcteff_notshare') (`pcteff_share') ///
                (`premean_notshare') (`premean_share') ///
                (`N_obs_didimp') (`N_control') (`N_notshare') (`N_share') ///
                (`product_control') (`product_notshare') (`product_share') ///
                (`board_control') (`board_notshare') (`board_share')
            postclose `posth'
            preserve
            use `run_results', clear
            export delimited using "`csv_file'", replace
            restore

            estimates restore formulary_ddd
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
            estadd scalar exposure_raw_min = `exposure_raw_min'
            estadd scalar exposure_raw_mean = `exposure_raw_mean'
            estadd scalar exposure_raw_max = `exposure_raw_max'
            estadd scalar exposure_dm_min = `exposure_dm_min'
            estadd scalar exposure_dm_mean = `exposure_dm_mean'
            estadd scalar exposure_dm_max = `exposure_dm_max'
            estadd scalar exposure_share_id_n = `exposure_share_id_n'
            foreach point in min mean max {
                estadd scalar gap_`point' = `gap_`point''
                estadd scalar gap_`point'_se = `gap_`point'_se'
                estadd scalar gap_`point'_p = `gap_`point'_p'
                estadd scalar share_att_`point' = `share_att_`point''
                estadd scalar share_att_`point'_se = `share_att_`point'_se'
                estadd scalar share_att_`point'_p = `share_att_`point'_p'
            }

            tempfile tex_fragment
            esttab formulary_ddd using "`tex_fragment'", ///
                replace booktabs se star(* 0.10 ** 0.05 *** 0.01) ///
                keep(beta1 beta2) ///
                coeflabels( ///
                    beta1 "beta1: ATT (Not Share)" ///
                    beta2 "beta2: ATT gap (Share - Not Share)" ///
                ) ///
                mtitles("`target'") ///
                stats( ///
                    premean_notshare ///
                    premean_share ///
                    pcteff_notshare ///
                    pcteff_share ///
                    exposure_raw_min ///
                    exposure_raw_mean ///
                    exposure_raw_max ///
                    exposure_dm_min ///
                    exposure_dm_mean ///
                    exposure_dm_max ///
                    exposure_share_id_n ///
                    gap_min gap_min_se gap_min_p ///
                    gap_mean gap_mean_se gap_mean_p ///
                    gap_max gap_max_se gap_max_p ///
                    share_att_min share_att_min_se share_att_min_p ///
                    share_att_mean share_att_mean_se share_att_mean_p ///
                    share_att_max share_att_max_se share_att_max_p ///
                    N ///
                    N_control ///
                    N_share ///
                    N_notshare ///
                    board_control ///
                    board_share ///
                    board_notshare ///
                    product_control ///
                    product_share ///
                    product_notshare, ///
                    fmt( ///
                        3 3 2 2 ///
                        3 3 3 3 3 3 0 ///
                        3 3 3 3 3 3 3 3 3 ///
                        3 3 3 3 3 3 3 3 3 ///
                        0 0 0 0 0 0 0 0 0 0 ///
                    ) ///
                    labels( ///
                        "Pre-treatment mean Y (Not Share)" ///
                        "Pre-treatment mean Y (Share)" ///
                        "Percent effect (Not Share)" ///
                        "Percent effect (Share, mean exposure)" ///
                        "Exposure raw min (Share IDs)" ///
                        "Exposure raw mean (Share IDs)" ///
                        "Exposure raw max (Share IDs)" ///
                        "Exposure demeaned min" ///
                        "Exposure demeaned mean" ///
                        "Exposure demeaned max" ///
                        "Unique Share IDs for exposure" ///
                        "Share - Not Share gap at min" ///
                        "SE: gap at min" ///
                        "p-value: gap at min" ///
                        "Share - Not Share gap at mean" ///
                        "SE: gap at mean" ///
                        "p-value: gap at mean" ///
                        "Share - Not Share gap at max" ///
                        "SE: gap at max" ///
                        "p-value: gap at max" ///
                        "Share ATT at min" ///
                        "SE: Share ATT at min" ///
                        "p-value: Share ATT at min" ///
                        "Share ATT at mean" ///
                        "SE: Share ATT at mean" ///
                        "p-value: Share ATT at mean" ///
                        "Share ATT at max" ///
                        "SE: Share ATT at max" ///
                        "p-value: Share ATT at max" ///
                        "Observations Total" ///
                        "Observations (Control)" ///
                        "Observations (Share, Treated)" ///
                        "Observations (Not Share, Treated)" ///
                        "Unique Boards (Control)" ///
                        "Unique Boards (Share, Treated)" ///
                        "Unique Boards (Not Share, Treated)" ///
                        "Unique Products (Control)" ///
                        "Unique Products (Share, Treated)" ///
                        "Unique Products (Not Share, Treated)" ///
                    ) ///
                ) ///
                title("did_imputation DDD: `event', event, Not, raw | `treatment_group' | FE `fe_level' | kappa=none | atc=atc3 | control_atc=none | exposure=none")

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
            estimates drop formulary_ddd
            log close
        }
    }
}

di as result "Saved SSR-format formulary ATC3-sharing DDD outputs."
