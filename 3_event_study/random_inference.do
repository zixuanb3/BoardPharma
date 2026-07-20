clear all
set more off
set trace off
capture file close _all

// ================================================================
// Purpose:
// Run randomization inference for the stacked atc3 did_imputation DDD
// specifications. For each specification, the script preserves the
// observed cohort-specific counts of control units, treated products
// without ATC sharing, and treated products with ATC sharing. Within
// each cohort, it randomly reassigns stack ids to these three groups,
// rebuilds the did_imputation event cohort and heterogeneity variables,
// and compares the observed estimates with the randomization distribution.
//
// Process:
// - Only large-sample narrow atc3 specifications are run.
// - Only winsorized p95, Not controls, price, log_transform, event,
//   req1, other-event controls, kappa_asy, separate ATC controls,
//   include_eventpair=0, FE level 1, and firm clustering are run.
// - Treatment groups A and B are run.
// - Randomization occurs at the stacked id level within each data_cohort.
//   Since id is defined as group(boardname product data_cohort), this is
//   equivalent to randomizing inside each cohort before stacking.
//
// Output:
// - figures/random_inference/*.png
// - logs/random_inference/<event>/treat_<A/B>/observed.log
// - logs/random_inference/<event>/treat_<A/B>/permutation_*.log
// - logs/random_inference/<event>/treat_<A/B>/cohort_counts.log
// - logs/random_inference/random_inference_failure.log
// ================================================================

* ================= user config =================
local n_permutations 1000
local base_seed 20260713

* ================= fixed specification =================
local atc atc3
local large_sample 1
local personnel_definition narrow
local outlier_treatment winsorize
local outlier_treatment_percentile p95
local panel_level quarter
local events to_B_still_in_A
local control not
local target price
local std log_transform
local event_type event
local req 1
local c_var other_event
local control_kappa kappa_asy
local control_atc separate
local treatment_groups A B
local include_eventpair 0
local fe_level 1
local cluster_level firm

* ================= path =================
local code_path "`c(pwd)'"
local parent_path = regexr("`code_path'", "[/\\][^/\\]+$", "")
local project_path = regexr("`parent_path'", "[/\\][^/\\]+$", "")
local project_path = subinstr("`project_path'", "\", "/", .)

local data_root "`project_path'/data/cohort_data_with_atcsharing_`atc'"
local movement_suffix "_large_sample_`personnel_definition'"
local movement_output_suffix "_ls_`personnel_definition'"
local fig_root "`project_path'/figures"
local fig_dir "`fig_root'/random_inference"
local log_root "`project_path'/logs"
local log_dir "`log_root'/random_inference"
local failure_log_path "`log_dir'/random_inference_failure.log"

cap mkdir "`fig_root'"
cap mkdir "`fig_dir'"
cap mkdir "`log_root'"
cap mkdir "`log_dir'"

capture erase "`failure_log_path'"

local spec_index 0

foreach treatment_group of local treatment_groups {
    local treatment_group = upper("`treatment_group'")
    local counterpart "A"
    if "`treatment_group'" == "A" {
        local counterpart "B"
    }

    local relation "without"
    local group_label "`treatment_group'_`relation'_`counterpart'"
    local panel_group_folder "`panel_level'-level_`group_label'`movement_suffix'"
    local panel_output_folder "`panel_level'_`group_label'`movement_output_suffix'"
    local data_path "`data_root'/`panel_group_folder'"

    foreach event of local events {
        local spec_index = `spec_index' + 1

        * -------- determine quarter cohort list --------
        local cohort_list 2009 2010 2011 2012 2013 2014 2015 2016 2017 2018
        if "`event'" == "to_B_not_in_A" {
            if "`treatment_group'" == "A" {
                local cohort_list 2009 2010 2012 2013 2014 2015 2016 2017 2018
            }
            else if "`treatment_group'" == "B" {
                local cohort_list 2009 2010 2012 2014 2015 2016 2017 2018
            }
        }

        local other_event_list ""
        if "`event'" == "to_B_not_in_A" {
            local other_event_list "other_event_still other_event_dissolution"
        }
        else if "`event'" == "to_B_still_in_A" {
            local other_event_list "other_event_not other_event_dissolution"
        }
        else if "`event'" == "interlock_dissolution" {
            local other_event_list "other_event_not other_event_still"
        }
        else {
            di as error "Unsupported event: `event'"
            exit 198
        }

        local control_folder "Not"
        local control_fname "not"
        local cluster_var boardname
        local control_variation_title "all_controls"
        local output_stub "`event'_`group_label'_req`req'_`event_type'_`control_fname'_`std'_cv_`c_var'_kappa_`control_kappa'_atc_`control_atc'"
        local spec_log_root "`log_dir'/`event'"
        local spec_log_dir "`spec_log_root'/treat_`treatment_group'"
        local observed_log "`spec_log_dir'/observed.log"
        local counts_log "`spec_log_dir'/cohort_counts.log"
        local summary_log "`spec_log_dir'/summary.log"
        local notshare_fig "`fig_dir'/`output_stub'_notshare.png"
        local share_fig "`fig_dir'/`output_stub'_share.png"

        cap mkdir "`spec_log_root'"
        cap mkdir "`spec_log_dir'"

        di as text "============================================================"
        di as text "Randomization inference: event=`event', group=`group_label'"
        di as text "Cohorts: `cohort_list'"

        * -------- build stacked sample --------
        local first 1
        local stack_failed 0
        foreach cohort of local cohort_list {
            local data_file "`data_path'/`event_type'/req`req'/`control_folder'/`event'_`panel_level'_cohort_`cohort'_balanced`movement_suffix'_`atc'.csv"

            capture confirm file "`data_file'"
            if _rc {
                file open failure_log using "`failure_log_path'", write text append
                file write failure_log "Missing input file: `data_file'" _n
                file close failure_log
                local stack_failed 1
                continue, break
            }

            import delimited "`data_file'", clear
            gen target_raw = `target'

            local event_anchor_q = yq(`cohort', 1)
            gen rel_quarter_all = yq(year, quarter) - `event_anchor_q'
            keep if rel_quarter_all >= -4 & rel_quarter_all <= 7
            drop rel_quarter_all

            gen event_cohort = .
            gen treated_in_stack = 0
            replace event_cohort = `cohort' if event_`cohort' == 1
            replace treated_in_stack = event_`cohort' == 1
            gen data_cohort = `cohort'

            if `first' {
                tempfile master
                save `master', replace
                local first 0
            }
            else {
                append using `master'
                save `master', replace
            }
        }

        if `stack_failed' {
            di as error "Stack build failed for event=`event', group=`group_label'"
            capture log close ri_summary
            log using "`summary_log'", text replace name(ri_summary)
            di as error "Stack build failed"
            di as text "event=`event'"
            di as text "treatment_group=`treatment_group'"
            di as text "cohort_list=`cohort_list'"
            log close ri_summary
            continue
        }

        if `first' {
            di as error "No stack was built for event=`event', group=`group_label'"
            capture log close ri_summary
            log using "`summary_log'", text replace name(ri_summary)
            di as error "No stack was built"
            di as text "event=`event'"
            di as text "treatment_group=`treatment_group'"
            di as text "cohort_list=`cohort_list'"
            log close ri_summary
            continue
        }

        use `master', clear

        preserve
        import delimited "`project_path'/data/kappa/ssr_kappa_firm_level_v5.csv", clear
        rename firm boardname
        keep year quarter boardname kappa_norm_mean kappa_mean
        isid year quarter boardname
        tempfile kappa_controls
        save `kappa_controls', replace
        restore

        merge m:1 year quarter boardname using `kappa_controls', keep(master match) nogen

        quietly summarize `target', detail
        local pt_val = r(`outlier_treatment_percentile')
        replace `target' = `pt_val' if `target' > `pt_val' & !missing(`target')
        replace `target' = log(`target')

        * Stack-specific panel id: the same product can reappear in another cohort stack and should be treated as a distinct unit there
        egen id = group(boardname product data_cohort)
        gen q_time = yq(year, quarter)
        format q_time %tq

        gen treat = treated_in_stack
        gen post = q_time >= yq(data_cohort, 1)
        gen pre_period = q_time < yq(data_cohort, 1)

        replace atc_sharing = 0 if missing(atc_sharing)
        gen event_cohort_did_imputation = yq(event_cohort, 1) if !missing(event_cohort)
        gen atc_sharing_het = atc_sharing if treat == 1
        replace atc_sharing_het = 0 if treat == 0

        capture confirm variable `atc'
        if _rc {
            di as error "Missing ATC variable: `atc'"
            exit 111
        }
        egen atc_id = group(`atc')

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

        local fe_spec "id q_time `final_cv_list'"
        local fe_spec "`fe_spec' atc_id"
        local did_controls "controls(kappa_mean)"

        preserve
            keep id data_cohort treat atc_sharing
            bysort id: keep if _n == 1
            gen byte ri_type = 0
            replace ri_type = 1 if treat == 1 & atc_sharing == 0
            replace ri_type = 2 if treat == 1 & atc_sharing == 1
            label define ri_type_lbl 0 "Control" 1 "Not Share Treated" 2 "Share Treated", replace
            label values ri_type ri_type_lbl
            contract data_cohort ri_type
            rename _freq n_ids
            capture log close ri_counts
            log using "`counts_log'", text replace name(ri_counts)
            di as text "Cohort-level randomization counts"
            di as text "event=`event'"
            di as text "treatment_group=`treatment_group'"
            di as text "group_label=`group_label'"
            di as text "cohort_list=`cohort_list'"
            list data_cohort ri_type n_ids, sepby(data_cohort) noobs
            log close ri_counts
        restore

        tempfile base_stack
        save `base_stack', replace

        * -------- observed stacked did_imputation estimate --------
        capture log close ri_estlog
        log using "`observed_log'", text replace name(ri_estlog)
        di as text "Observed stacked did_imputation estimate"
        di as text "event=`event'"
        di as text "treatment_group=`treatment_group'"
        di as text "group_label=`group_label'"
        di as text "cohort_list=`cohort_list'"
        di as text "outcome=`target'"
        di as text "cluster=`cluster_var'"

        capture noisily did_imputation `target' id q_time event_cohort_did_imputation, ///
            fe(`fe_spec') ///
            hetby(atc_sharing_het) ///
            autosample tol(0.1) minn(0) cluster(`cluster_var') `did_controls'
        local true_rc = _rc

        local true_notshare = .
        local true_share = .
        if `true_rc' == 0 {
            di as text "Observed lincom: not-share ATT"
            capture noisily lincom tau_0
            if _rc == 0 {
                local true_notshare = r(estimate)
            }
            else {
                local true_rc = _rc
            }

            di as text "Observed lincom: share ATT"
            capture noisily lincom tau_1
            if _rc == 0 {
                local true_share = r(estimate)
            }
            else {
                local true_rc = _rc
            }
        }
        else {
            di as error "Observed did_imputation failed with r(`true_rc')"
        }
        di as result "true_notshare=`true_notshare'"
        di as result "true_share=`true_share'"
        log close ri_estlog

        if `true_rc' != 0 | missing(`true_notshare') | missing(`true_share') {
            file open failure_log using "`failure_log_path'", write text append
            file write failure_log "Observed did_imputation failed: event=`event', group=`group_label', r(`true_rc')" _n
            file close failure_log
            continue
        }

        * -------- randomized estimates --------
        tempfile ri_results
        tempname ri_post
        postfile `ri_post' int rep double beta_notshare beta_share using `ri_results', replace

        local failed_permutations 0
        forvalues r = 1/`n_permutations' {
            use `base_stack', clear

            preserve
                keep id data_cohort treat atc_sharing
                bysort id: keep if _n == 1

                gen byte orig_share_treated = treat == 1 & atc_sharing == 1
                gen byte orig_notshare_treated = treat == 1 & atc_sharing == 0
                bysort data_cohort: egen n_share = total(orig_share_treated)
                bysort data_cohort: egen n_notshare = total(orig_notshare_treated)

                sort data_cohort id
                local iter_seed = `base_seed' + `spec_index' * 100000 + `r'
                set seed `iter_seed'
                gen double u = runiform()
                bysort data_cohort (u id): gen rank = _n

                gen byte treat_ri_id = 0
                gen byte atc_sharing_ri_id = 0
                replace treat_ri_id = 1 if rank <= n_share
                replace atc_sharing_ri_id = 1 if rank <= n_share
                replace treat_ri_id = 1 if rank > n_share & rank <= n_share + n_notshare
                replace atc_sharing_ri_id = 0 if rank > n_share & rank <= n_share + n_notshare

                keep id treat_ri_id atc_sharing_ri_id
                tempfile ri_assign
                save `ri_assign', replace
            restore

            merge m:1 id using `ri_assign', nogen
            gen event_cohort_ri = yq(data_cohort, 1) if treat_ri_id == 1
            gen atc_sharing_het_ri = atc_sharing_ri_id if treat_ri_id == 1
            replace atc_sharing_het_ri = 0 if treat_ri_id == 0

            local rep_tag : display %06.0f `r'
            local rep_tag = strtrim("`rep_tag'")
            local perm_log "`spec_log_dir'/permutation_`rep_tag'.log"

            capture log close ri_estlog
            log using "`perm_log'", text replace name(ri_estlog)
            di as text "Randomization inference permutation estimate"
            di as text "rep=`r'"
            di as text "seed=`iter_seed'"
            di as text "event=`event'"
            di as text "treatment_group=`treatment_group'"
            di as text "group_label=`group_label'"
            di as text "cohort_list=`cohort_list'"
            di as text "outcome=`target'"
            di as text "cluster=`cluster_var'"

            capture noisily did_imputation `target' id q_time event_cohort_ri, ///
                fe(`fe_spec') ///
                hetby(atc_sharing_het_ri) ///
                autosample tol(0.1) minn(0) cluster(`cluster_var') `did_controls'
            local ri_rc = _rc

            if `ri_rc' == 0 {
                di as text "Permutation lincom: not-share ATT"
                capture noisily lincom tau_0
                local lincom_notshare_rc = _rc
                if `lincom_notshare_rc' == 0 {
                    local beta_notshare = r(estimate)
                }

                di as text "Permutation lincom: share ATT"
                capture noisily lincom tau_1
                local lincom_share_rc = _rc
                if `lincom_share_rc' == 0 {
                    local beta_share = r(estimate)
                }

                if `lincom_notshare_rc' == 0 & `lincom_share_rc' == 0 {
                    di as result "beta_notshare=`beta_notshare'"
                    di as result "beta_share=`beta_share'"
                    post `ri_post' (`r') (`beta_notshare') (`beta_share')
                }
                else {
                    di as error "Permutation lincom failed"
                    local failed_permutations = `failed_permutations' + 1
                }
            }
            else {
                di as error "Permutation did_imputation failed with r(`ri_rc')"
                local failed_permutations = `failed_permutations' + 1
            }
            log close ri_estlog
        }

        postclose `ri_post'
        use `ri_results', clear

        quietly count if !missing(beta_notshare)
        local valid_notshare = r(N)
        quietly count if !missing(beta_share)
        local valid_share = r(N)

        * One-sided right-tail RI tests for the positive-ATT alternatives.
        local ri_p_notshare = .
        local ri_p_share = .
        if `valid_notshare' > 0 {
            quietly count if !missing(beta_notshare) & beta_notshare >= `true_notshare'
            local ri_p_notshare = r(N) / `valid_notshare'
        }
        if `valid_share' > 0 {
            quietly count if !missing(beta_share) & beta_share >= `true_share'
            local ri_p_share = r(N) / `valid_share'
        }

        capture log close ri_summary
        log using "`summary_log'", text replace name(ri_summary)
        di as text "Randomization inference summary"
        di as text "event=`event'"
        di as text "treatment_group=`treatment_group'"
        di as text "group_label=`group_label'"
        di as text "cohort_list=`cohort_list'"
        di as result "requested_permutations=`n_permutations'"
        di as result "valid_notshare=`valid_notshare'"
        di as result "valid_share=`valid_share'"
        di as result "failed_permutations=`failed_permutations'"
        di as result "true_notshare=`true_notshare'"
        di as result "true_share=`true_share'"
        di as result "ri_tail=right"
        di as result "ri_p_notshare=`ri_p_notshare'"
        di as result "ri_p_share=`ri_p_share'"
        log close ri_summary

        * -------- figures --------
        local ri_p_notshare_label : display %6.4f `ri_p_notshare'
        local ri_p_share_label : display %6.4f `ri_p_share'

        if `valid_notshare' > 0 {
            quietly summarize beta_notshare if !missing(beta_notshare), meanonly
            local notshare_xmin = min(r(min), `true_notshare')
            local notshare_xmax = max(r(max), `true_notshare')
            local notshare_xpad = (`notshare_xmax' - `notshare_xmin') * 0.05
            if missing(`notshare_xpad') | `notshare_xpad' <= 0 {
                local notshare_xpad = 0.01
            }
            local notshare_xmin = `notshare_xmin' - `notshare_xpad'
            local notshare_xmax = `notshare_xmax' + `notshare_xpad'

            histogram beta_notshare if !missing(beta_notshare), ///
                frequency fcolor(ebblue) lcolor(ebblue) fintensity(40) ///
                xscale(range(`notshare_xmin' `notshare_xmax')) ///
                xline(`true_notshare', lcolor(red) lwidth(medthick)) ///
                title("RI distribution: Not Share ATT") ///
                subtitle("`event' | `group_label' | valid=`valid_notshare'/`n_permutations' | right-tail RI p=`ri_p_notshare_label'") ///
                xtitle("tau_0") ytitle("Frequency") ///
                graphregion(color(white)) plotregion(color(white)) ///
                name(g_notshare, replace)
            graph export "`notshare_fig'", replace width(2400)
        }

        if `valid_share' > 0 {
            quietly summarize beta_share if !missing(beta_share), meanonly
            local share_xmin = min(r(min), `true_share')
            local share_xmax = max(r(max), `true_share')
            local share_xpad = (`share_xmax' - `share_xmin') * 0.05
            if missing(`share_xpad') | `share_xpad' <= 0 {
                local share_xpad = 0.01
            }
            local share_xmin = `share_xmin' - `share_xpad'
            local share_xmax = `share_xmax' + `share_xpad'

            histogram beta_share if !missing(beta_share), ///
                frequency fcolor(ebblue) lcolor(ebblue) fintensity(40) ///
                xscale(range(`share_xmin' `share_xmax')) ///
                xline(`true_share', lcolor(red) lwidth(medthick)) ///
                title("RI distribution: Share ATT") ///
                subtitle("`event' | `group_label' | valid=`valid_share'/`n_permutations' | right-tail RI p=`ri_p_share_label'") ///
                xtitle("tau_1") ytitle("Frequency") ///
                graphregion(color(white)) plotregion(color(white)) ///
                name(g_share, replace)
            graph export "`share_fig'", replace width(2400)
        }
    }
}

clear all
