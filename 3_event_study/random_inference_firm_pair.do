version 18.0
clear all
set more off
set trace off
capture file close _all

// ================================================================
// Purpose:
// Run one-sided randomization inference for ATC3 Share ATT using conditional
// firm-pair placebo panels. The design holds actual event firms and event timing
// fixed, while each replication changes ATC sharing and counterpart-only controls
// according to a random partner draw created in Python.
//
// Process:
// 1. Load one side-specific complete stacked panel containing observed and 1,000
//    simulated share/onlypair columns.
// 2. Estimate the observed result after applying the observed counterpart-only
//    control exclusion from the same complete panel.
// 3. For every replication, delete only random counterpart-only controls, replace
//    the share label, and re-estimate the identical did_imputation specification.
// 4. Persist each completed permutation result and reuse it on a later rerun.
// 5. Report the existing right-tail randomization p-value for Share ATT.
//
// Input:
// - data/random_inference_firm_pair/to_B_still_in_A/req1/large_sample_narrow/
//   firm_pair_randomization_{A|B}.dta
// - data/kappa/ssr_kappa_firm_level_v5.csv
//
// Output:
// - csv/random_inference_firm_pair/<event>/treat_<A|B>/replication_results.csv
// - csv/random_inference_firm_pair/<event>/treat_<A|B>/permutation_results/permutation_*.dta
// - figures/random_inference_firm_pair/*_{notshare|share}.png
// - logs/random_inference_firm_pair/<event>/treat_<A|B>/observed.log
// - logs/random_inference_firm_pair/<event>/treat_<A|B>/permutation_*.log
// - logs/random_inference_firm_pair/<event>/treat_<A|B>/summary.log
// ================================================================

* ========================== USER CONFIG ==========================
local n_permutations 1000

local atc atc3
local event to_B_still_in_A
local req 1
local personnel_definition narrow
local treatment_groups A B
local target price
local outlier_treatment_percentile p95
local cluster_var boardname
local c_var other_event
local control_kappa kappa_asy
local control_atc separate
* ================================================================

* =========================== PATH SETUP ===========================
local code_path "`c(pwd)'"
local parent_path = regexr("`code_path'", "[/\\][^/\\]+$", "")
local project_path = regexr("`parent_path'", "[/\\][^/\\]+$", "")
local project_path = subinstr("`project_path'", "\\", "/", .)

local data_dir "`project_path'/data/random_inference_firm_pair/`event'/req`req'/large_sample_`personnel_definition'"
local kappa_path "`project_path'/data/kappa/ssr_kappa_firm_level_v5.csv"
local csv_dir "`project_path'/csv/random_inference_firm_pair"
local fig_dir "`project_path'/figures/random_inference_firm_pair"
local log_dir "`project_path'/logs/random_inference_firm_pair"

cap mkdir "`csv_dir'"
cap mkdir "`fig_dir'"
cap mkdir "`log_dir'"

* ======================= FIXED SPECIFICATION ======================
foreach treatment_group of local treatment_groups {
    local treatment_group = upper("`treatment_group'")
    local counterpart "A"
    if "`treatment_group'" == "A" {
        local counterpart "B"
    }

    local group_label "`treatment_group'_with_`counterpart'"
    local data_file "`data_dir'/firm_pair_randomization_`treatment_group'.dta"
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

    capture confirm file "`data_file'"
    if _rc {
        di as error "Missing firm-pair RI panel: `data_file'"
        exit 601
    }
    capture confirm file "`kappa_path'"
    if _rc {
        di as error "Missing kappa control file: `kappa_path'"
        exit 601
    }

    * ------------------- load and prepare base stack -------------------
    use "`data_file'", clear

    foreach required_var in boardname product year quarter data_cohort treated_in_stack event_cohort `atc' share_obs onlypair_obs {
        capture confirm variable `required_var'
        if _rc {
            di as error "Missing required variable `required_var' in `data_file'"
            exit 111
        }
    }

    gen target_raw = `target'
    quietly summarize `target', detail
    local p95_value = r(`outlier_treatment_percentile')
    replace `target' = `p95_value' if `target' > `p95_value' & !missing(`target')
    replace `target' = log(`target')

    preserve
        import delimited "`kappa_path'", clear
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
    gen byte treat = treated_in_stack
    gen post = q_time >= yq(data_cohort, 1)
    gen pre_period = q_time < yq(data_cohort, 1)
    gen event_cohort_did_imputation = yq(event_cohort, 1) if !missing(event_cohort)
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

    local fe_spec "id q_time `final_cv_list' atc_id"
    local did_controls "controls(kappa_mean)"
    tempfile base_stack
    save `base_stack', replace

    * ---------------------- observed estimate ----------------------
    use `base_stack', clear
    drop if onlypair_obs == 1 & treat == 0
    gen byte atc_sharing = share_obs
    replace atc_sharing = 0 if treat == 0
    gen byte atc_sharing_het = atc_sharing if treat == 1
    replace atc_sharing_het = 0 if treat == 0

    capture log close ri_observed
    log using "`observed_log'", text replace name(ri_observed)
    di as text "Observed conditional firm-pair estimate"
    di as text "event=`event'"
    di as text "treatment_group=`treatment_group'"
    di as text "group_label=`group_label'"
    di as text "outcome=`target'"
    di as text "cluster=`cluster_var'"

    capture noisily did_imputation `target' id q_time event_cohort_did_imputation, ///
        fe(`fe_spec') ///
        hetby(atc_sharing_het) ///
        autosample tol(0.1) minn(0) cluster(`cluster_var') `did_controls'
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

    di as result "observed_notshare_att=`observed_notshare'"
    di as result "observed_share_att=`observed_share'"
    log close ri_observed

    if `observed_rc' != 0 | missing(`observed_notshare') | missing(`observed_share') {
        di as error "Observed firm-pair estimate failed for `group_label'; rc=`observed_rc'"
        exit 459
    }

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
        local share_var "share_ri_`data_rep_tag'"
        local onlypair_var "onlypair_ri_`data_rep_tag'"
        local perm_log "`spec_log_dir'/permutation_`log_rep_tag'.log"
        local perm_result "`permutation_results_dir'/permutation_`log_rep_tag'.dta"

        * Reuse a completed persistent result after an interrupted prior run.
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
            if r(N) != 1 {
                di as error "Saved permutation result must contain exactly one row: `perm_result'"
                exit 459
            }
            local cached_rep = rep[1]
            if `cached_rep' != `rep' {
                di as error "Saved permutation result has the wrong replication number: `perm_result'"
                exit 459
            }

            local cached_success = success[1]
            local cached_failure_rc = failure_rc[1]
            local cached_n_dropped = n_dropped[1]
            local cached_n_share_treated = n_share_treated[1]
            local cached_n_notshare_treated = n_notshare_treated[1]
            local cached_beta_notshare = beta_notshare[1]
            local cached_beta_share = beta_share[1]
            post `ri_post' (`rep') (`cached_success') (`cached_failure_rc') ///
                (`cached_n_dropped') (`cached_n_share_treated') (`cached_n_notshare_treated') ///
                (`cached_beta_notshare') (`cached_beta_share')
            local reused_permutations = `reused_permutations' + 1
            continue
        }

        use `base_stack', clear
        capture confirm variable `share_var'
        if _rc {
            di as error "Missing `share_var' in `data_file'"
            exit 111
        }
        capture confirm variable `onlypair_var'
        if _rc {
            di as error "Missing `onlypair_var' in `data_file'"
            exit 111
        }

        quietly count if `onlypair_var' == 1 & treat == 0
        local n_dropped = r(N)
        drop if `onlypair_var' == 1 & treat == 0

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
        di as text "Conditional firm-pair randomization permutation"
        di as text "rep=`rep'"
        di as text "event=`event'"
        di as text "treatment_group=`treatment_group'"
        di as text "group_label=`group_label'"
        di as text "outcome=`target'"
        di as text "cluster=`cluster_var'"
        di as text "share_variable=`share_var'"
        di as text "onlypair_variable=`onlypair_var'"
        di as result "n_dropped_controls=`n_dropped'"
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
                fe(`fe_spec') ///
                hetby(atc_sharing_het) ///
                autosample tol(0.1) minn(0) cluster(`cluster_var') `did_controls'
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
    di as text "Conditional firm-pair randomization inference summary"
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

    * ---------------------- distribution figure ----------------------
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
        title("Conditional firm-pair RI: Not Share ATT") ///
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
        title("Conditional firm-pair RI: Share ATT") ///
        subtitle("`event' | `group_label' | valid=`valid_share'/`n_permutations' | right-tail p=`ri_p_share_label'") ///
        xtitle("tau_1") ytitle("Frequency") ///
        graphregion(color(white)) plotregion(color(white)) ///
        name(g_share, replace)
    graph export "`share_fig'", replace width(2400)
}

clear all
