version 18.0
clear all
set more off
set trace off
capture file close _all

// ================================================================
// Purpose:
// Run one-sided randomization inference for ATC3 Share ATT under joint
// pseudo-treated-firm and pseudo-firm-pair assignment. The observed branch
// retains the established observed cohort, while every random replication uses
// a full-roster pseudo-event schedule, a random req0/req1 pair bundle, and its
// resulting sample-state/share columns.
//
// Process:
// 1. Estimate the observed ATT from the existing validated firm-pair RI panel.
// 2. Load one side-specific balanced pseudo-event base panel only once.
// 3. For each replication, retain its pseudo sample, delete counterpart-only
//    controls, define pseudo treatment and share, then estimate the same model.
// 4. Persist completed replication results for safe interruption and reuse.
// 5. Report the existing right-tail randomization p-value and distributions.
//
// Input:
// - data/random_inference_firm_pair/to_B_still_in_A/req1/large_sample_narrow/
//   firm_pair_randomization_{A|B}.dta
// - data/random_inference_treated_firm_pair/to_B_still_in_A/req1/
//   large_sample_narrow/treated_firm_pair_randomization_{A|B}.dta
// - data/kappa/ssr_kappa_firm_level_v5.csv
//
// Output:
// - csv/random_inference_treated_firm_pair/<event>/treat_<A|B>/replication_results.csv
// - csv/random_inference_treated_firm_pair/<event>/treat_<A|B>/permutation_results/permutation_*.dta
// - figures/random_inference_treated_firm_pair/*_{notshare|share}.png
// - logs/random_inference_treated_firm_pair/<event>/treat_<A|B>/*.log
// ================================================================

* ========================== USER CONFIG ==========================
local n_permutations 1000

local atc atc3
local event to_B_still_in_A
local req 1
local personnel_definition narrow
local treatment_groups B
local target price
local outlier_treatment_percentile p95
local cluster_var boardname
* ================================================================

* =========================== PATH SETUP ===========================
local code_path "`c(pwd)'"
local parent_path = regexr("`code_path'", "[/\\][^/\\]+$", "")
local project_path = regexr("`parent_path'", "[/\\][^/\\]+$", "")
local project_path = subinstr("`project_path'", "\\", "/", .)

local observed_data_dir "`project_path'/data/random_inference_firm_pair/`event'/req`req'/large_sample_`personnel_definition'"
local random_data_dir "`project_path'/data/random_inference_treated_firm_pair/`event'/req`req'/large_sample_`personnel_definition'"
local kappa_path "`project_path'/data/kappa/ssr_kappa_firm_level_v5.csv"
local csv_dir "`project_path'/csv/random_inference_treated_firm_pair"
local fig_dir "`project_path'/figures/random_inference_treated_firm_pair"
local log_dir "`project_path'/logs/random_inference_treated_firm_pair"

cap mkdir "`csv_dir'"
cap mkdir "`fig_dir'"
cap mkdir "`log_dir'"

* ====================== SHARED DATA PREPARATION ======================
capture program drop prepare_joint_ri_base
program define prepare_joint_ri_base, rclass
    syntax, KAPPAPATH(string) TARGET(name) ATC(name) CUTOFF(real)

    gen `target'_raw = `target'
    replace `target' = `cutoff' if `target' > `cutoff' & !missing(`target')
    replace `target' = log(`target')

    preserve
        import delimited using "`kappapath'", clear
        rename firm boardname
        keep year quarter boardname kappa_norm_mean kappa_mean
        isid year quarter boardname
        tempfile kappa_controls
        save `kappa_controls', replace
    restore
    merge m:1 year quarter boardname using `kappa_controls', keep(master match) nogen

    egen id = group(boardname product data_cohort)
    gen q_time = yq(year, quarter)
    format q_time %tq
    egen atc_id = group(`atc')

    local other_event_list "other_event_not other_event_dissolution"
    local final_cv_list ""
    foreach other_event of local other_event_list {
        capture confirm variable `other_event'
        if _rc {
            di as error "Missing other-event control variable: `other_event'"
            exit 111
        }

        tempvar first_other_q
        bysort boardname data_cohort: egen `first_other_q' = min(cond(`other_event' == 1, q_time, .))
        gen `other_event'_history = !missing(`first_other_q') & q_time >= `first_other_q'
        drop `first_other_q'

        quietly summarize `other_event'_history, meanonly
        if r(max) > 0 {
            local final_cv_list "`final_cv_list' `other_event'_history"
        }
    }

    return local fe_spec "id q_time `final_cv_list' atc_id"
    return local did_controls "controls(kappa_mean)"
end

* ======================= FIXED SPECIFICATION ======================
foreach treatment_group of local treatment_groups {
    local treatment_group = upper("`treatment_group'")
    local counterpart "A"
    if "`treatment_group'" == "A" {
        local counterpart "B"
    }

    local group_label "`treatment_group'_with_`counterpart'"
    local observed_data_file "`observed_data_dir'/firm_pair_randomization_`treatment_group'.dta"
    local random_data_file "`random_data_dir'/treated_firm_pair_randomization_`treatment_group'.dta"
    local results_root "`csv_dir'/`event'"
    local results_dir "`results_root'/treat_`treatment_group'"
    local results_path "`results_dir'/replication_results.csv"
    local permutation_results_dir "`results_dir'/permutation_results"
    local notshare_fig "`fig_dir'/`event'_`group_label'_notshare.png"
    local share_fig "`fig_dir'/`event'_`group_label'_share.png"
    local spec_log_root "`log_dir'/`event'"
    local spec_log_dir "`spec_log_root'/treat_`treatment_group'"
    local observed_log "`spec_log_dir'/observed.log"
    local summary_log "`spec_log_dir'/summary.log"

    cap mkdir "`results_root'"
    cap mkdir "`results_dir'"
    cap mkdir "`permutation_results_dir'"
    cap mkdir "`spec_log_root'"
    cap mkdir "`spec_log_dir'"

    foreach required_file in "`observed_data_file'" "`random_data_file'" "`kappa_path'" {
        capture confirm file `"`required_file'"'
        if _rc {
            di as error "Missing required input: `required_file'"
            exit 601
        }
    }

    * ------------------ validate and fix observed outcome scale ------------------
    use "`observed_data_file'", clear
    foreach required_var in boardname product year quarter data_cohort treated_in_stack event_cohort `atc' `target' share_obs onlypair_obs {
        capture confirm variable `required_var'
        if _rc {
            di as error "Missing observed variable `required_var' in `observed_data_file'"
            exit 111
        }
    }
    quietly summarize `target', detail
    local fixed_p95 = r(`outlier_treatment_percentile')
    if missing(`fixed_p95') {
        di as error "The fixed observed `outlier_treatment_percentile' cutoff is missing."
        exit 459
    }

    gen byte canonical_treat = treated_in_stack
    gen int canonical_event_cohort = event_cohort if canonical_treat == 1
    gen byte canonical_share = share_obs
    drop if onlypair_obs == 1 & canonical_treat == 0
    keep boardname product year quarter data_cohort canonical_treat canonical_event_cohort canonical_share
    isid boardname product year quarter data_cohort
    tempfile canonical_observed
    save `canonical_observed', replace

    use "`random_data_file'", clear
    foreach required_var in boardname product year quarter data_cohort `atc' `target' sample_state_obs share_obs sample_state_ri_0001 share_ri_0001 {
        capture confirm variable `required_var'
        if _rc {
            di as error "Missing randomization variable `required_var' in `random_data_file'"
            exit 111
        }
    }
    tempfile random_raw
    save `random_raw', replace

    keep if sample_state_obs > 0
    drop if sample_state_obs == 3
    gen byte generated_treat = sample_state_obs == 2
    gen int generated_event_cohort = data_cohort if generated_treat == 1
    gen byte generated_share = share_obs
    keep boardname product year quarter data_cohort generated_treat generated_event_cohort generated_share
    isid boardname product year quarter data_cohort
    merge 1:1 boardname product year quarter data_cohort using `canonical_observed'
    assert _merge == 3
    assert generated_treat == canonical_treat
    assert generated_event_cohort == canonical_event_cohort
    assert generated_share == canonical_share
    drop _merge

    * ---------------------- observed estimate ----------------------
    use `random_raw', clear
    prepare_joint_ri_base, kappapath("`kappa_path'") target(`target') atc(`atc') cutoff(`fixed_p95')
    local observed_fe_spec "`r(fe_spec)'"
    local observed_did_controls "`r(did_controls)'"

    keep if sample_state_obs > 0
    drop if sample_state_obs == 3
    gen byte treat = sample_state_obs == 2
    gen double event_cohort_did_imputation = yq(data_cohort, 1) if treat == 1
    gen byte atc_sharing = share_obs
    replace atc_sharing = 0 if treat == 0
    gen byte atc_sharing_het = atc_sharing if treat == 1
    replace atc_sharing_het = 0 if treat == 0

    capture log close ri_observed
    log using "`observed_log'", text replace name(ri_observed)
    di as text "Observed estimate for joint treated-firm/pair RI"
    di as text "event=`event'"
    di as text "treatment_group=`treatment_group'"
    di as text "outcome=`target'"
    di as text "cluster=`cluster_var'"

    capture noisily did_imputation `target' id q_time event_cohort_did_imputation, ///
        fe(`observed_fe_spec') ///
        hetby(atc_sharing_het) ///
        autosample tol(0.1) minn(0) cluster(`cluster_var') `observed_did_controls'
    local observed_rc = _rc
    local observed_notshare = .
    local observed_share = .
    if `observed_rc' == 0 {
        capture noisily lincom tau_0
        if _rc == 0 {
            local observed_notshare = r(estimate)
        }
        else {
            local observed_rc = _rc
        }

        capture noisily lincom tau_1
        if _rc == 0 {
            local observed_share = r(estimate)
        }
        else {
            local observed_rc = _rc
        }
    }
    log close ri_observed

    if `observed_rc' != 0 | missing(`observed_notshare') | missing(`observed_share') {
        di as error "Observed estimate failed for `group_label'; rc=`observed_rc'"
        exit 459
    }

    * ------------------- prepare randomization base -------------------
    use `random_raw', clear
    prepare_joint_ri_base, kappapath("`kappa_path'") target(`target') atc(`atc') cutoff(`fixed_p95')
    local random_fe_spec "`r(fe_spec)'"
    local random_did_controls "`r(did_controls)'"
    tempfile random_base
    save `random_base', replace

    * ---------------------- random estimates ----------------------
    tempname ri_post
    tempfile ri_results
    postfile `ri_post' int rep byte success int failure_rc ///
        long n_dropped n_share_treated n_notshare_treated ///
        double beta_notshare beta_share using `ri_results', replace

    local reused_permutations 0
    local estimated_permutations 0
    forvalues rep = 1/`n_permutations' {
        local data_rep_tag : display %04.0f `rep'
        local data_rep_tag = strtrim("`data_rep_tag'")
        local log_rep_tag : display %06.0f `rep'
        local log_rep_tag = strtrim("`log_rep_tag'")
        local state_var "sample_state_ri_`data_rep_tag'"
        local share_var "share_ri_`data_rep_tag'"
        local perm_log "`spec_log_dir'/permutation_`log_rep_tag'.log"
        local perm_result "`permutation_results_dir'/permutation_`log_rep_tag'.dta"

        capture confirm file "`perm_result'"
        if _rc == 0 {
            use "`perm_result'", clear
            foreach cached_var in rep success failure_rc n_dropped n_share_treated n_notshare_treated beta_notshare beta_share {
                capture confirm variable `cached_var'
                if _rc {
                    di as error "Invalid saved permutation result: `perm_result'"
                    exit 459
                }
            }
            quietly count
            if r(N) != 1 | rep[1] != `rep' {
                di as error "Saved permutation result is invalid: `perm_result'"
                exit 459
            }
            post `ri_post' (rep[1]) (success[1]) (failure_rc[1]) ///
                (n_dropped[1]) (n_share_treated[1]) (n_notshare_treated[1]) ///
                (beta_notshare[1]) (beta_share[1])
            local reused_permutations = `reused_permutations' + 1
            continue
        }

        use `random_base', clear
        foreach required_var in `state_var' `share_var' {
            capture confirm variable `required_var'
            if _rc {
                di as error "Missing `required_var' in `random_data_file'"
                exit 111
            }
        }

        keep if `state_var' > 0
        quietly count if `state_var' == 3
        local n_dropped = r(N)
        drop if `state_var' == 3
        gen byte treat = `state_var' == 2
        gen double event_cohort_did_imputation = yq(data_cohort, 1) if treat == 1
        gen byte atc_sharing = `share_var'
        replace atc_sharing = 0 if treat == 0
        quietly count if treat == 1 & q_time == yq(data_cohort, 1) & atc_sharing == 1
        local n_share_treated = r(N)
        quietly count if treat == 1 & q_time == yq(data_cohort, 1) & atc_sharing == 0
        local n_notshare_treated = r(N)

        local permutation_success 0
        local failure_rc 0
        local beta_notshare = .
        local beta_share = .

        capture log close ri_permutation
        log using "`perm_log'", text replace name(ri_permutation)
        di as text "Joint treated-firm/pair randomization permutation"
        di as text "rep=`rep'"
        di as text "event=`event'"
        di as text "treatment_group=`treatment_group'"
        di as text "sample_state_variable=`state_var'"
        di as text "share_variable=`share_var'"
        di as result "n_dropped_onlypair_controls=`n_dropped'"
        di as result "n_share_treated=`n_share_treated'"
        di as result "n_notshare_treated=`n_notshare_treated'"

        if `n_share_treated' == 0 | `n_notshare_treated' == 0 {
            local failure_rc 498
            di as error "Permutation skipped: one treated heterogeneity group is empty."
        }
        else {
            gen byte atc_sharing_het = atc_sharing if treat == 1
            replace atc_sharing_het = 0 if treat == 0
            capture noisily did_imputation `target' id q_time event_cohort_did_imputation, ///
                fe(`random_fe_spec') ///
                hetby(atc_sharing_het) ///
                autosample tol(0.1) minn(0) cluster(`cluster_var') `random_did_controls'
            local ri_rc = _rc

            if `ri_rc' == 0 {
                capture noisily lincom tau_0
                local notshare_rc = _rc
                if `notshare_rc' == 0 {
                    local beta_notshare = r(estimate)
                }
                capture noisily lincom tau_1
                local share_rc = _rc
                if `share_rc' == 0 {
                    local beta_share = r(estimate)
                }
                if `notshare_rc' != 0 | `share_rc' != 0 {
                    local ri_rc 459
                    di as error "Permutation lincom failed."
                }
            }
            else {
                di as error "Permutation did_imputation failed with r(`ri_rc')."
            }

            if `ri_rc' == 0 {
                local permutation_success 1
            }
            else {
                local failure_rc `ri_rc'
            }
        }

        di as result "success=`permutation_success'"
        di as result "failure_rc=`failure_rc'"
        di as result "beta_notshare=`beta_notshare'"
        di as result "beta_share=`beta_share'"
        log close ri_permutation

        post `ri_post' (`rep') (`permutation_success') (`failure_rc') ///
            (`n_dropped') (`n_share_treated') (`n_notshare_treated') ///
            (`beta_notshare') (`beta_share')

        clear
        set obs 1
        gen int rep = `rep'
        gen byte success = `permutation_success'
        gen int failure_rc = `failure_rc'
        gen long n_dropped = `n_dropped'
        gen long n_share_treated = `n_share_treated'
        gen long n_notshare_treated = `n_notshare_treated'
        gen double beta_notshare = `beta_notshare'
        gen double beta_share = `beta_share'
        save "`perm_result'", replace
        local estimated_permutations = `estimated_permutations' + 1
    }
    postclose `ri_post'

    * ---------------------- one-sided inference ----------------------
    use `ri_results', clear
    quietly count if success == 1 & !missing(beta_notshare)
    local valid_notshare = r(N)
    quietly count if success == 1 & !missing(beta_share)
    local valid_share = r(N)
    quietly count if success == 0
    local failed_permutations = r(N)
    if `valid_notshare' == 0 | `valid_share' == 0 {
        di as error "No valid Not Share or Share ATT replications for `group_label'."
        exit 459
    }

    quietly count if success == 1 & beta_notshare >= `observed_notshare'
    local right_tail_exceedances_notshare = r(N)
    local ri_p_notshare = `right_tail_exceedances_notshare' / `valid_notshare'
    quietly count if success == 1 & beta_share >= `observed_share'
    local right_tail_exceedances_share = r(N)
    local ri_p_share = `right_tail_exceedances_share' / `valid_share'

    gen str1 side = "`treatment_group'"
    gen str40 event = "`event'"
    gen double observed_notshare = `observed_notshare'
    gen double observed_share = `observed_share'
    gen double ri_p_notshare = `ri_p_notshare'
    gen double ri_p_share = `ri_p_share'
    export delimited using "`results_path'", replace

    capture log close ri_summary
    log using "`summary_log'", text replace name(ri_summary)
    di as text "Joint treated-firm/pair randomization inference summary"
    di as result "event=`event'"
    di as result "treatment_group=`treatment_group'"
    di as result "requested_permutations=`n_permutations'"
    di as result "reused_completed_permutations=`reused_permutations'"
    di as result "estimated_new_permutations=`estimated_permutations'"
    di as result "valid_notshare=`valid_notshare'"
    di as result "valid_share=`valid_share'"
    di as result "failed_permutations=`failed_permutations'"
    di as result "observed_notshare_att=`observed_notshare'"
    di as result "observed_share_att=`observed_share'"
    di as result "right_tail_exceedances_notshare=`right_tail_exceedances_notshare'"
    di as result "right_tail_exceedances_share=`right_tail_exceedances_share'"
    di as result "right_tail_p_notshare=`ri_p_notshare'"
    di as result "right_tail_p_share=`ri_p_share'"
    log close ri_summary

    * ---------------------- distribution figures ----------------------
    quietly summarize beta_notshare if success == 1 & !missing(beta_notshare), meanonly
    local notshare_xmin = min(r(min), `observed_notshare')
    local notshare_xmax = max(r(max), `observed_notshare')
    local notshare_xpad = (`notshare_xmax' - `notshare_xmin') * 0.05
    if missing(`notshare_xpad') | `notshare_xpad' <= 0 {
        local notshare_xpad = 0.01
    }
    local notshare_xmin = `notshare_xmin' - `notshare_xpad'
    local notshare_xmax = `notshare_xmax' + `notshare_xpad'
    local ri_p_notshare_label : display %6.4f `ri_p_notshare'
    histogram beta_notshare if success == 1 & !missing(beta_notshare), ///
        frequency fcolor(ebblue) lcolor(ebblue) fintensity(40) ///
        xscale(range(`notshare_xmin' `notshare_xmax')) ///
        xline(`observed_notshare', lcolor(red) lwidth(medthick)) ///
        title("Joint RI: Not Share ATT") ///
        subtitle("`event' | `group_label' | valid=`valid_notshare'/`n_permutations' | right-tail p=`ri_p_notshare_label'") ///
        xtitle("tau_0") ytitle("Frequency") ///
        graphregion(color(white)) plotregion(color(white)) ///
        name(g_notshare, replace)
    graph export "`notshare_fig'", replace width(2400)

    quietly summarize beta_share if success == 1 & !missing(beta_share), meanonly
    local share_xmin = min(r(min), `observed_share')
    local share_xmax = max(r(max), `observed_share')
    local share_xpad = (`share_xmax' - `share_xmin') * 0.05
    if missing(`share_xpad') | `share_xpad' <= 0 {
        local share_xpad = 0.01
    }
    local share_xmin = `share_xmin' - `share_xpad'
    local share_xmax = `share_xmax' + `share_xpad'
    local ri_p_share_label : display %6.4f `ri_p_share'
    histogram beta_share if success == 1 & !missing(beta_share), ///
        frequency fcolor(ebblue) lcolor(ebblue) fintensity(40) ///
        xscale(range(`share_xmin' `share_xmax')) ///
        xline(`observed_share', lcolor(red) lwidth(medthick)) ///
        title("Joint RI: Share ATT") ///
        subtitle("`event' | `group_label' | valid=`valid_share'/`n_permutations' | right-tail p=`ri_p_share_label'") ///
        xtitle("tau_1") ytitle("Frequency") ///
        graphregion(color(white)) plotregion(color(white)) ///
        name(g_share, replace)
    graph export "`share_fig'", replace width(2400)
}

clear all
