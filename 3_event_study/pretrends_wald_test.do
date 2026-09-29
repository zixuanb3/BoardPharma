* ================================================================
* Purpose:
*   Joint Wald pre-trends test (parallel-trends / placebo test) for the
*   stacked did_imputation event-study specifications used in
*   did_imputation_event_study.do.
*
*   For each (treatment_group, req) we re-estimate the event study in
*   SEPARATE-sample mode: treated units are split by the sharing
*   indicator `atc_sharing` (S = 0 and S = 1), and a separate
*   did_imputation regression (horizons 0/7, pretrends 3) is run on each
*   subsample. did_imputation already computes the joint Wald test
*       H0: pre1 = pre2 = pre3 = 0
*   (all pre-treatment / lead coefficients jointly equal zero) and
*   stores it as e(pre_F) (F-statistic) and e(pre_p) (p-value). We pull
*   those directly and report them.
*
*   Event labels (A-retained / A-exited):
*     to_B_still_in_A  = "A-retained" (interlock moves to B, person stays in A)
*     to_B_not_in_A    = "A-exited"   (interlock moves to B, person leaves A)
*
* Input:
*   - data/cohort_data_with_atcsharing_atc3/...  (same stacked cohort
*     inputs consumed by did_imputation_event_study.do)
*   - Requires the Stata command `did_imputation` (Borusyak et al.).
*
* Output:
*   - logs/pretrends_wald/pretrends_wald.log          (text log)
*   - csv/pretrends_wald/pretrends_wald_results.csv   (one row per
*     treatment_group x req x event x S; columns: treatment_group, req,
*     event, sval, F_stat, p_value)
* ================================================================

clear all
set more off
set trace off

* ================= user config (defaults from did_imputation_event_study.do) =================
local atcs atc3
* atc3 atc2
local large_sample 1
local personnel_definition narrow
* narrow medium broad
local outlier_treatment "winsorize"
* trim winsorize none
local outlier_treatment_percentile "p95"
* p90 p95 p99

local treatment_groups A B
local include_eventpair_values 0
* 1 0
local fe_levels 1
* 1 2
local cluster_levels firm
* firm

local events to_B_still_in_A to_B_not_in_A interlock_dissolution
* direct_interlock indirect_interlock to_B_still_in_A to_B_not_in_A interlock_dissolution
local controls not
* notyet purecontrol not
local targets price
* revenue quantity price0 price
local standardize_types log_transform
* log_transform standardize normalize
local event_types event
* event first_event
local reqs 0 1 2
* 0 1 2
local control_for_other_events other_event
* none other_event
local control_kappas kappa_asy
* none kappa_asy kappa_norm
local control_atcs separate
* separate
local req2_control_variations stable
* all stable changing stable_interlock stable_no_interlock

* ================= event-study horizon settings (must match did_imputation_event_study.do) =================
local did_horizons 0/7
local did_pretrend 3
local timevar q_time
local gvar event_cohort_q

* ================= path =================
local code_path "`c(pwd)'"
local parent_path = regexr("`code_path'", "[/\\][^/\\]+$", "")
local project_path = regexr("`parent_path'", "[/\\][^/\\]+$", "")
local project_path = subinstr("`project_path'", "\", "/", .)

cap mkdir "`project_path'/logs"
cap mkdir "`project_path'/csv"
cap mkdir "`project_path'/logs/pretrends_wald"
cap mkdir "`project_path'/csv/pretrends_wald"

local log_path "`project_path'/logs/pretrends_wald/pretrends_wald.log"
local csv_results_path "`project_path'/csv/pretrends_wald/pretrends_wald_results.csv"

log using "`log_path'", text replace

* ================= dependency checks =================
capture which did_imputation
if _rc {
    di as error "did_imputation is not installed. Install it before running this do-file."
    exit 198
}

* ================= movement suffix =================
if !inlist(`large_sample', 0, 1) {
    di as error "large_sample must be 0 or 1"
    exit 198
}
if `large_sample' == 1 & !inlist("`personnel_definition'", "narrow", "medium", "broad") {
    di as error "personnel_definition must be one of: narrow, medium, broad"
    exit 198
}
local movement_suffix ""
if `large_sample' == 1 {
    local movement_suffix "_large_sample_`personnel_definition'"
}

* ================= results collector =================
tempname results_handle
tempfile results_raw
postfile `results_handle' ///
    str2 treatment_group int req str30 event int sval ///
    double F_stat double p_value ///
    using `results_raw', replace

* ================= main loop =================
foreach atc of local atcs {
    if !inlist("`atc'", "atc2", "atc3") {
        di as error "atc must be one of: atc2, atc3"
        exit 198
    }

    local data_root "`project_path'/data/cohort_data_with_atcsharing_`atc'"

    foreach treatment_group of local treatment_groups {
        local treatment_group = upper("`treatment_group'")

        local counterpart "A"
        if "`treatment_group'" == "A" {
            local counterpart "B"
        }

        foreach include_eventpair of local include_eventpair_values {
            local relation "without"
            if `include_eventpair' == 1 {
                local relation "with"
            }
            local group_label "`treatment_group'_`relation'_`counterpart'"
            local panel_group_folder "quarter-level_`group_label'`movement_suffix'"
            local data_path "`data_root'/`panel_group_folder'"

            foreach fe_level of local fe_levels {

                foreach event of local events {
                    foreach target of local targets {
                        foreach control of local controls {
                            foreach std of local standardize_types {
                                foreach event_type of local event_types {
                                    foreach req of local reqs {
                                        foreach c_var of local control_for_other_events {
                                            foreach control_kappa of local control_kappas {
                                                foreach control_atc of local control_atcs {

                                    * -------- determine quarter cohort list --------
                                    local cohort_list ""

                                    if "`event_type'" != "event" | !inlist("`req'", "0", "1", "2") {
                                        di as error "Unsupported req or event_type: req=`req', event_type=`event_type'"
                                        exit 198
                                    }

                                    if !inlist("`treatment_group'", "A", "B") {
                                        di as error "Unsupported treatment_group: `treatment_group'"
                                        exit 198
                                    }

                                    if !inlist("`event'", "interlock_dissolution", "to_B_not_in_A", "to_B_still_in_A") {
                                        di as error "Unsupported event: `event'"
                                        exit 198
                                    }

                                    if `large_sample' == 0 {
                                        if inlist("`req'", "0", "1") {
                                            local cohort_list 2009 2010 2011 2012 2013 2014 2015 2016 2017 2018
                                        }
                                        else if "`req'" == "2" {
                                            if "`treatment_group'" == "A" {
                                                if "`event'" == "interlock_dissolution" {
                                                    local cohort_list 2009 2010 2011 2012 2014 2015 2016 2017 2018
                                                }
                                                else if "`event'" == "to_B_not_in_A" {
                                                    local cohort_list 2009 2010 2012 2014 2015 2016 2017 2018
                                                }
                                                else if "`event'" == "to_B_still_in_A" {
                                                    local cohort_list ""
                                                }
                                            }
                                            else if "`treatment_group'" == "B" {
                                                if "`event'" == "interlock_dissolution" {
                                                    local cohort_list 2010 2011 2012 2014 2015 2016 2017 2018
                                                }
                                                else if "`event'" == "to_B_not_in_A" {
                                                    local cohort_list 2012 2015 2017 2018
                                                }
                                                else if "`event'" == "to_B_still_in_A" {
                                                    local cohort_list 2009 2010 2012 2013 2015 2016 2017 2018
                                                }
                                            }
                                        }
                                    }
                                    else {
                                        if "`req'" == "0" {
                                            local cohort_list 2009 2010 2011 2012 2013 2014 2015 2016 2017 2018
                                        }
                                        else if "`req'" == "1" {
                                            local cohort_list 2009 2010 2011 2012 2013 2014 2015 2016 2017 2018
                                            if "`personnel_definition'" == "narrow" & "`event'" == "to_B_not_in_A" {
                                                if "`treatment_group'" == "A" {
                                                    local cohort_list 2009 2010 2012 2013 2014 2015 2016 2017 2018
                                                }
                                                else if "`treatment_group'" == "B" {
                                                    local cohort_list 2009 2010 2012 2014 2015 2016 2017 2018
                                                }
                                            }
                                        }
                                        else if "`req'" == "2" {
                                            if "`personnel_definition'" == "medium" {
                                                if "`treatment_group'" == "A" {
                                                    if "`event'" == "interlock_dissolution" {
                                                        local cohort_list 2010 2011 2012 2013 2014 2015 2016 2017 2018
                                                    }
                                                    else if "`event'" == "to_B_not_in_A" {
                                                        local cohort_list 2010 2012 2018
                                                    }
                                                    else if "`event'" == "to_B_still_in_A" {
                                                        local cohort_list 2009 2010 2011 2012 2013 2014 2016 2018
                                                    }
                                                }
                                                else if "`treatment_group'" == "B" {
                                                    if "`event'" == "interlock_dissolution" {
                                                        local cohort_list 2009 2010 2011 2012 2013 2014 2015 2016 2017 2018
                                                    }
                                                    else if "`event'" == "to_B_not_in_A" {
                                                        local cohort_list 2010 2011 2014
                                                    }
                                                    else if "`event'" == "to_B_still_in_A" {
                                                        local cohort_list 2009 2010 2011 2012 2013 2014 2015 2016 2017
                                                    }
                                                }
                                            }
                                            else if "`personnel_definition'" == "narrow" {
                                                if "`treatment_group'" == "A" {
                                                    if "`event'" == "interlock_dissolution" {
                                                        local cohort_list 2010 2011 2012 2013 2014 2015 2016 2017 2018
                                                    }
                                                    else if "`event'" == "to_B_not_in_A" {
                                                        local cohort_list 2010 2012 2013 2014 2015 2016 2018
                                                    }
                                                    else if "`event'" == "to_B_still_in_A" {
                                                        local cohort_list 2009 2010 2011 2012 2013 2014 2015 2016 2018
                                                    }
                                                }
                                                else if "`treatment_group'" == "B" {
                                                    if "`event'" == "interlock_dissolution" {
                                                        local cohort_list 2009 2010 2011 2012 2013 2014 2015 2016 2017 2018
                                                    }
                                                    else if "`event'" == "to_B_not_in_A" {
                                                        local cohort_list 2010 2012 2015 2016 2018
                                                    }
                                                    else if "`event'" == "to_B_still_in_A" {
                                                        local cohort_list 2009 2010 2011 2012 2014 2015 2016 2017
                                                    }
                                                }
                                            }
                                        }
                                    }

                                    if "`cohort_list'" == "" {
                                        continue
                                    }

                                    * -------- other-event controls --------
                                    local other_event_list ""
                                    if "`c_var'" == "other_event" {
                                        if "`event'" == "to_B_not_in_A" {
                                            if inlist("`req'", "0", "1") {
                                                local other_event_list "other_event_still other_event_dissolution"
                                            }
                                        }
                                        else if "`event'" == "to_B_still_in_A" {
                                            if inlist("`req'", "0", "1") {
                                                local other_event_list "other_event_not other_event_dissolution"
                                            }
                                            else if "`req'" == "2" {
                                                local other_event_list "other_event_not"
                                            }
                                        }
                                        else if "`event'" == "interlock_dissolution" {
                                            if inlist("`req'", "0", "1") {
                                                local other_event_list "other_event_not other_event_still"
                                            }
                                            else if "`req'" == "2" {
                                                local other_event_list "other_event_not"
                                            }
                                        }
                                        if "`other_event_list'" == "" {
                                            continue
                                        }
                                    }
                                    else if "`c_var'" != "none" {
                                        di as error "control_for_other_events must be one of: none, other_event"
                                        exit 198
                                    }

                                    * -------- event_type suffix --------
                                    local suffix ""
                                    if "`event_type'" == "first_event" {
                                        local suffix "_first_event"
                                    }

                                    * -------- control folder name --------
                                    if "`control'" == "notyet" {
                                        local control_folder "Not Yet"
                                        local control_fname "not_yet"
                                    }
                                    else if "`control'" == "purecontrol" {
                                        local control_folder "Pure Control"
                                        local control_fname "pure_control"
                                    }
                                    else if "`control'" == "not" {
                                        local control_folder "Not"
                                        local control_fname "not"
                                    }
                                    else {
                                        di as error "Unknown control type"
                                        exit 198
                                    }

                                    * -------- req2 control variation --------
                                    local control_variation_values all
                                    if "`req'" == "2" {
                                        local control_variation_values "`req2_control_variations'"
                                    }

                                    foreach control_variation of local control_variation_values {
                                        foreach cluster_level of local cluster_levels {
                                            local cluster_var ""
                                            if "`cluster_level'" == "firm" {
                                                local cluster_var boardname
                                            }
                                            else {
                                                di as error "cluster_level must be one of: firm"
                                                exit 198
                                            }

                                            di as text "===================================================================="
                                            di as text "pretrends Wald: atc=`atc' group=`group_label' event=`event' req=`req' control=`control_fname' target=`target' std=`std' cluster=`cluster_level'"

                                            * -------- stack cohorts --------
                                            local first 1
                                            foreach cohort of local cohort_list {
                                                local data_file "`data_path'/`event_type'/req`req'/`control_folder'/`event'_quarter_cohort_`cohort'`suffix'_balanced`movement_suffix'_`atc'.csv"

                                                capture confirm file "`data_file'"
                                                if _rc {
                                                    di as error "Missing input file: `data_file'"
                                                    exit 111
                                                }

                                                import delimited "`data_file'", clear

                                                local event_anchor_q = yq(`cohort', 1)
                                                gen rel_quarter_all = yq(year, quarter) - `event_anchor_q'
                                                keep if rel_quarter_all >= -4 & rel_quarter_all <= 7
                                                drop rel_quarter_all

                                                gen event_cohort = .
                                                gen treated_in_stack = 0
                                                if "`event_type'" == "event" {
                                                    replace event_cohort = `cohort' if event_`cohort' == 1
                                                    replace treated_in_stack = (event_`cohort' == 1)
                                                }
                                                if "`event_type'" == "first_event" {
                                                    replace event_cohort = `cohort' if first_event_year == `cohort'
                                                    replace treated_in_stack = first_event_year == `cohort'
                                                }

                                                gen data_cohort = `cohort'

                                                if "`control_variation'" != "all" {
                                                    capture confirm variable control_`control_variation'
                                                    if _rc {
                                                        di as error "Missing req2 control column: control_`control_variation'"
                                                        di as error "File: `data_file'"
                                                        exit 111
                                                    }
                                                    keep if treated_in_stack == 1 | control_`control_variation' == 1
                                                }

                                                if `first' {
                                                    tempfile master
                                                    save `master', replace
                                                    local first = 0
                                                }
                                                else {
                                                    append using `master'
                                                    save `master', replace
                                                }
                                            }

                                            if `first' {
                                                di as error "No cohort files stacked for event=`event' req=`req' control=`control_fname'."
                                                post `results_handle' ("`treatment_group'") (`req') ("`event'") (.) (.) (.)
                                                continue
                                            }

                                            use `master', clear

                                            * -------- merge kappa controls --------
                                            if "`control_kappa'" != "none" {
                                                capture confirm file "`project_path'/data/kappa/ssr_kappa_firm_level_v5.csv"
                                                if _rc {
                                                    di as error "Missing kappa file: `project_path'/data/kappa/ssr_kappa_firm_level_v5.csv"
                                                    exit 111
                                                }
                                                preserve
                                                import delimited "`project_path'/data/kappa/ssr_kappa_firm_level_v5.csv", clear
                                                rename firm boardname
                                                keep year quarter boardname kappa_norm_mean kappa_mean
                                                isid year quarter boardname
                                                tempfile kappa_controls
                                                save `kappa_controls', replace
                                                restore
                                                merge m:1 year quarter boardname using `kappa_controls', keep(master match) nogen
                                            }

                                            * -------- outlier treatment --------
                                            if "`outlier_treatment'" == "trim" {
                                                bysort boardname product data_cohort: egen group_max_norm = max(`target')
                                                bysort boardname product data_cohort: egen group_min_norm = min(`target')
                                                gen group_ratio_norm = .
                                                replace group_ratio_norm = group_max_norm / group_min_norm if group_min_norm != 0 & !missing(group_min_norm)
                                                preserve
                                                keep boardname product data_cohort group_ratio_norm
                                                bysort boardname product data_cohort: keep if _n == 1
                                                quietly summarize group_ratio_norm, detail
                                                local p95_group_ratio = r(`outlier_treatment_percentile')
                                                restore
                                                drop if group_ratio_norm > `p95_group_ratio' & !missing(group_ratio_norm)
                                                drop group_max_norm group_min_norm group_ratio_norm
                                            }
                                            else if "`outlier_treatment'" == "winsorize" {
                                                quietly summarize `target', detail
                                                local pt_val = r(`outlier_treatment_percentile')
                                                replace `target' = `pt_val' if `target' > `pt_val' & !missing(`target')
                                            }

                                            * -------- outcome transformation --------
                                            if "`std'" == "standardize" {
                                                bysort boardname product data_cohort: egen temp = std(`target')
                                                replace `target' = temp
                                                drop temp
                                            }
                                            else if "`std'" == "normalize" {
                                                bysort boardname product data_cohort: gen baseline = `target' if year == data_cohort & quarter == 1
                                                bysort boardname product data_cohort: egen baseline_value = max(baseline)
                                                replace `target' = `target' / baseline_value
                                                drop baseline baseline_value
                                            }
                                            else if "`std'" == "log_transform" {
                                                replace `target' = log(`target')
                                            }

                                            * -------- identifiers / fixed effects --------
                                            egen id = group(boardname product data_cohort)
                                            gen q_time = yq(year, quarter)
                                            format q_time %tq
                                            gen event_cohort_q = yq(event_cohort, 1) if !missing(event_cohort)

                                            egen cohort_q_time_fe = group(data_cohort q_time)
                                            capture confirm variable `atc'
                                            if _rc {
                                                di as error "Missing ATC variable: `atc'"
                                                exit 111
                                            }
                                            egen atc_id = group(`atc')

                                            local cv_list ""
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
                                                    local cv_list "`cv_list' `other_event'_history"
                                                }
                                            }

                                            local fe_spec "id `timevar'"
                                            if `fe_level' == 2 {
                                                local fe_spec "id cohort_q_time_fe"
                                            }
                                            if "`cv_list'" != "" {
                                                local fe_spec "`fe_spec' `cv_list'"
                                            }

                                            local kappa_control_var ""
                                            if "`control_kappa'" == "kappa_asy" {
                                                local kappa_control_var "kappa_mean"
                                            }
                                            else if "`control_kappa'" == "kappa_norm" {
                                                local kappa_control_var "kappa_norm_mean"
                                            }
                                            else if "`control_kappa'" != "none" {
                                                di as error "control_kappa must be one of: none, kappa_asy, kappa_norm"
                                                exit 198
                                            }

                                            local did_controls ""
                                            if "`kappa_control_var'" != "" {
                                                local did_controls "controls(`kappa_control_var')"
                                            }

                                            if "`control_atc'" == "separate" {
                                                local fe_spec "`fe_spec' atc_id"
                                            }

                                            gen treated = !missing(event_cohort) & event_cohort != 0
                                            gen event_cohort_did_imputation = `gvar'
                                            replace event_cohort_did_imputation = . if event_cohort_did_imputation == 0

                                            * -------- separate-mode: run one did_imputation per S subsample --------
                                            foreach sval in 0 1 {
                                                preserve
                                                drop if treated == 1 & atc_sharing != `sval'

                                                capture noisily did_imputation `target' id `timevar' event_cohort_did_imputation, ///
                                                    fe(`fe_spec') horizons(`did_horizons') pretrends(`did_pretrend') ///
                                                    autosample tol(0.1) minn(0) cluster(`cluster_var') `did_controls'
                                                local did_rc = _rc

                                                local F_stat = .
                                                local p_value = .

                                                if `did_rc' != 0 {
                                                    di as error "did_imputation failed (rc=`did_rc') for S=`sval'."
                                                }
                                                else {
                                                    * did_imputation stores the joint pre-trends Wald test as
                                                    * e(pre_F) and e(pre_p) (H0: pre1 = pre2 = pre3 = 0).
                                                    capture local F_stat = e(pre_F)
                                                    capture local p_value = e(pre_p)
                                                    di as result "  S=`sval': F-stat = " %8.4f `F_stat' ", p-value = " %6.4f `p_value'
                                                }

                                                post `results_handle' ("`treatment_group'") (`req') ("`event'") (`sval') (`F_stat') (`p_value')
                                                restore
                                            }

                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
}
}
}
}
}
}

postclose `results_handle'

* -------- export collected results to CSV --------
preserve
use `results_raw', clear
export delimited using "`csv_results_path'", replace
restore

log close

di as text "Done. Pre-trends Wald test results written to: `csv_results_path'"