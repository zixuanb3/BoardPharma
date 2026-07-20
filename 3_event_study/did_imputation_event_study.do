clear all
set more off
set trace off

// ================================================================
// Purpose:
// Run stacked did_imputation event-study regressions for ATC-sharing
// heterogeneity in movement-event cohorts.
//
// Process:
// 1. Loop over ATC level, treatment group, event type, requirement level,
//    control definition, target outcome, transformation, fixed-effect setup,
//    and did_imputation clustering level.
// 2. Import balanced cohort CSVs with ATC-sharing labels and stack cohorts.
// 3. Apply the configured outcome transformation and optional controls.
// 4. Estimate did_imputation either with hetby(atc_sharing) or with separate
//    sharing/non-sharing samples, using the configured cluster() variable.
// 5. Export coefficient CSVs, logs, and overlaid dynamic-effect figures.
//    Exported sample-count columns are computed on the autosample-adjusted
//    e(sample).
//
// Input:
// - data/cohort_data_with_atcsharing_{atc}/quarter-level_{group}/event/req*/.../*.csv
// - data/cohort_data_with_atcsharing_{atc}/quarter-level_{group}_large_sample_{definition}/event/req*/.../*_large_sample_{definition}_*.csv when large_sample == 1
// - data/kappa/ssr_kappa_firm_level_v5.csv when kappa controls are enabled
//
// Output:
// - figures/didimp_es_{atc}*/cluster_{cluster}/q_*.../*.png
// - csv/didimp_es_{atc}*/cluster_{cluster}/q_*.../*.csv
// - logs/didimp_es_{atc}*/cluster_{cluster}/q_*.../*.log
// ================================================================

* ================= user config =================
local atcs atc3 atc2
* atc3 atc2
local large_sample 1
local personnel_definition medium
* narrow medium broad
local separate_modes 0
* 1
local outlier_treatment "winsorize"
* trim winsorize none
local outlier_treatment_percentile "p95"
*p90 p95 p99

* ================= path =================
local code_path "`c(pwd)'"
local parent_path = regexr("`code_path'", "[/\\][^/\\]+$", "")
local project_path = regexr("`parent_path'", "[/\\][^/\\]+$", "")
local project_path = subinstr("`project_path'", "\", "/", .)

cap mkdir "`project_path'/figures"
cap mkdir "`project_path'/csv"
cap mkdir "`project_path'/logs"
local failure_log_path "`project_path'/logs/did_imputation_event_study_failure.log"

local events interlock_dissolution to_B_still_in_A to_B_not_in_A
*direct_interlock indirect_interlock to_B_still_in_A to_B_not_in_A
local controls not 
* notyet purecontrol not
local targets price
*revenue quantity price0 price
local standardize_types log_transform
* log_transform standardize normalize
local event_types event
* first_event
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
local treatment_groups A B
local include_eventpair_values 0
* 1 0
local fe_levels 1
* 1 2
local cluster_levels firm
* firm

local coef_names pre_3 pre_2 pre_1 post_0 post_1 post_2 post_3 post_4 post_5 post_6 post_7
local n_total 11
local trimlead 3
local trimlag 7
local xlabel_spec -3(1)7
local graph_width 3000
local perturb_step 0.15
local perturb_span 0.30
local did_horizons 0/7
local did_pretrend 3
local timevar q_time
local gvar event_cohort_q

if !inlist(`large_sample', 0, 1) {
    di as error "large_sample must be 0 or 1"
    exit 198
}
if `large_sample' == 1 & !inlist("`personnel_definition'", "narrow", "medium", "broad") {
    di as error "personnel_definition must be one of: narrow, medium, broad"
    exit 198
}
local movement_suffix ""
local movement_output_suffix ""
if `large_sample' == 1 {
    local movement_suffix "_large_sample_`personnel_definition'"
    local movement_output_suffix = "_ls" + substr("`personnel_definition'", 1, 1)
}

* ================= loop =================
foreach atc of local atcs {
    if !inlist("`atc'", "atc2", "atc3") {
        di as error "atc must be one of: atc2, atc3"
        exit 198
    }

    local data_root "`project_path'/data/cohort_data_with_atcsharing_`atc'"
    local output_tag_base "didimp_es_`atc'`movement_output_suffix'"

foreach treatment_group of local treatment_groups {
    local treatment_group = upper("`treatment_group'")
    if !inlist("`treatment_group'", "A", "B") {
        di as error "treatment_group must be one of: A, B"
        exit 198
    }

    local counterpart "A"
    if "`treatment_group'" == "A" {
        local counterpart "B"
    }

    foreach include_eventpair of local include_eventpair_values {
        if !inlist(`include_eventpair', 0, 1) {
            di as error "include_eventpair must be one of: 0, 1"
            exit 198
        }

        local relation "without"
        if `include_eventpair' == 1 {
            local relation "with"
        }

        local group_label "`treatment_group'_`relation'_`counterpart'"
        local panel_group_folder "quarter-level_`group_label'`movement_suffix'"
        local relation_output_tag "wo"
        if `include_eventpair' == 1 {
            local relation_output_tag "w"
        }
        local panel_output_folder "q_`treatment_group'`relation_output_tag'`counterpart'`movement_output_suffix'"
        local data_path "`data_root'/`panel_group_folder'"

        foreach fe_level of local fe_levels {
            foreach separate of local separate_modes {
                if `separate' == 0 {
                    local mode_tag hetby
                    local mode_title "hetby"
                }
                else {
                    local mode_tag separate
                    local mode_title "separate samples"
                }

                if "`outlier_treatment'" == "none" {
                    local fe_output_tag "`output_tag_base'_none_fe`fe_level'"
                }
                else {
                    local fe_output_tag "`output_tag_base'_`outlier_treatment'`outlier_treatment_percentile'_fe`fe_level'"
                }
                
                local fe_fig_root "`project_path'/figures/`fe_output_tag'"
                local fe_csv_root "`project_path'/csv/`fe_output_tag'"
                local fe_log_root "`project_path'/logs/`fe_output_tag'"

                cap mkdir "`fe_fig_root'"
                cap mkdir "`fe_csv_root'"
                cap mkdir "`fe_log_root'"

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

                                * -------- determine suffix for event_type --------
                                local suffix ""
                                if "`event_type'" == "first_event" {
                                    local suffix "_first_event"
                                }

                                * -------- determine control folder name --------
                                if "`control'" == "notyet" {
                                    local control_folder "Not Yet"
                                    local control_fname "not_yet"
                                    local control_title "Not Yet"
                                }
                                else if "`control'" == "purecontrol" {
                                    local control_folder "Pure Control"
                                    local control_fname "pure_control"
                                    local control_title "Pure Control"
                                }
                                else if "`control'" == "not" {
                                    local control_folder "Not"
                                    local control_fname "not"
                                    local control_title "Not"
                                }
                                else {
                                    di as error "Unknown control type"
                                    exit 198
                                }

                                local event_name = subinstr("`event'", "_", " ", .)

                                local control_variation_values all
                                if "`req'" == "2" {
                                    local control_variation_values "`req2_control_variations'"
                                }

                                foreach control_variation of local control_variation_values {
                                local control_variation_folder ""
                                local control_variation_title "all_controls"
                                if "`control_variation'" != "all" {
                                    local control_variation_folder "/`control_variation'"
                                    local control_variation_title "`control_variation'"
                                }

                                foreach cluster_level of local cluster_levels {
                                local cluster_var ""
                                local cluster_folder "cluster_`cluster_level'"
                                if "`cluster_level'" == "firm" {
                                    local cluster_var boardname
                                }
                                else {
                                    di as error "cluster_level must be one of: firm"
                                    exit 198
                                }

                                * -------- setup paths for this iteration --------
                                local event_fig_path "`fe_fig_root'/`cluster_folder'/`panel_output_folder'/`event'/req`req'`control_variation_folder'/`target'"
                                local event_csv_path "`fe_csv_root'/`cluster_folder'/`panel_output_folder'/`event'/req`req'`control_variation_folder'/`target'"
                                local event_log_path "`fe_log_root'/`cluster_folder'/`panel_output_folder'/`event'/req`req'`control_variation_folder'/`target'"
                                cap mkdir "`fe_fig_root'/`cluster_folder'"
                                cap mkdir "`fe_csv_root'/`cluster_folder'"
                                cap mkdir "`fe_log_root'/`cluster_folder'"
                                cap mkdir "`fe_fig_root'/`cluster_folder'/`panel_output_folder'"
                                cap mkdir "`fe_csv_root'/`cluster_folder'/`panel_output_folder'"
                                cap mkdir "`fe_log_root'/`cluster_folder'/`panel_output_folder'"
                                cap mkdir "`fe_fig_root'/`cluster_folder'/`panel_output_folder'/`event'"
                                cap mkdir "`fe_csv_root'/`cluster_folder'/`panel_output_folder'/`event'"
                                cap mkdir "`fe_log_root'/`cluster_folder'/`panel_output_folder'/`event'"
                                cap mkdir "`fe_fig_root'/`cluster_folder'/`panel_output_folder'/`event'/req`req'"
                                cap mkdir "`fe_csv_root'/`cluster_folder'/`panel_output_folder'/`event'/req`req'"
                                cap mkdir "`fe_log_root'/`cluster_folder'/`panel_output_folder'/`event'/req`req'"
                                cap mkdir "`fe_fig_root'/`cluster_folder'/`panel_output_folder'/`event'/req`req'`control_variation_folder'"
                                cap mkdir "`fe_csv_root'/`cluster_folder'/`panel_output_folder'/`event'/req`req'`control_variation_folder'"
                                cap mkdir "`fe_log_root'/`cluster_folder'/`panel_output_folder'/`event'/req`req'`control_variation_folder'"
                                cap mkdir "`event_fig_path'"
                                cap mkdir "`event_csv_path'"
                                cap mkdir "`event_log_path'"

                                local std_tag "`std'"
                                if "`std'" == "log_transform" {
                                    local std_tag "log"
                                }
                                else if "`std'" == "standardize" {
                                    local std_tag "std"
                                }
                                else if "`std'" == "normalize" {
                                    local std_tag "norm"
                                }
                                local mode_file_tag "h"
                                if "`mode_tag'" == "separate" {
                                    local mode_file_tag "s"
                                }
                                local control_var_tag ""
                                if "`c_var'" != "none" {
                                    local control_var_tag "_oe"
                                }
                                local kappa_tag ""
                                if "`control_kappa'" == "kappa_asy" {
                                    local kappa_tag "_ka"
                                }
                                else if "`control_kappa'" == "kappa_norm" {
                                    local kappa_tag "_kn"
                                }
                                local atc_tag "_atc`control_atc'"
                                local file_stub "`event_type'_`control_fname'_`std_tag'_`mode_file_tag'`control_var_tag'`kappa_tag'`atc_tag'"

                                cap log close
                                log using "`event_log_path'/`file_stub'.log", text replace

                                local first = 1

                                foreach cohort of local cohort_list {
                                    local data_file "`data_path'/`event_type'/req`req'/`control_folder'/`event'_quarter_cohort_`cohort'`suffix'_balanced`movement_suffix'_`atc'.csv"

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

                                    gen rel_quarter = yq(year, quarter) - `event_anchor_q' if treated_in_stack == 1

                                    forvalues i = 1/4 {
                                        gen pre_`i' = treated_in_stack == 1 & rel_quarter == -`i'
                                    }
                                    forvalues i = 0/7 {
                                        gen post_`i' = treated_in_stack == 1 & rel_quarter == `i'
                                    }
                                    drop rel_quarter

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

                                if !inlist("`control_atc'", "separate") {
                                    di as error "control_atc must be: separate"
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
								

                                local obs_control = .
                                local obs_treated0 = .
                                local obs_treated1 = .
                                local brd_control = .
                                local brd_treated0 = .
                                local brd_treated1 = .
                                local prd_control = .
                                local prd_treated0 = .
                                local prd_treated1 = .

                                matrix sample_stats = J(2, 9, .)
                                matrix colnames sample_stats = obs_control obs_treated0 obs_treated1 brd_control brd_treated0 brd_treated1 prd_control prd_treated0 prd_treated1

                                foreach sval in 0 1 {
                                    matrix did2_b_s`sval' = J(1, `n_total', .)
                                    matrix did2_V_s`sval' = J(`n_total', `n_total', 0)
                                    matrix colnames did2_b_s`sval' = `coef_names'
                                    matrix colnames did2_V_s`sval' = `coef_names'
                                    matrix rownames did2_V_s`sval' = `coef_names'
                                }
                                local spec_failed 0

                                * ============================================================
                                * 1. separate = 0: one did_imputation with hetby
                                * ============================================================
                                if `separate' == 0 {
                                    gen atc_sharing_het = atc_sharing if treated == 1
                                    replace atc_sharing_het = 0 if treated == 0

                                    capture noisily did_imputation `target' id `timevar' event_cohort_did_imputation, fe(`fe_spec') horizons(`did_horizons') pretrends(`did_pretrend') hetby(atc_sharing_het) autosample tol(0.1) minn(0) cluster(`cluster_var') `did_controls'
                                    local did_rc = _rc
                                    if `did_rc' != 0 {
                                        local spec_failed 1
                                        local failure_reason "did_imputation failed in hetby mode: r(`did_rc')"
                                        capture confirm file "`failure_log_path'"
                                        if _rc {
                                            file open failure_log using "`failure_log_path'", write text replace
                                        }
                                        else {
                                            file open failure_log using "`failure_log_path'", write text append
                                        }
                                        file write failure_log "============================================================" _n
                                        file write failure_log "`c(current_date)' `c(current_time)'" _n
                                        file write failure_log "`failure_reason'" _n
                                        file write failure_log "atc=`atc'" _n
                                        file write failure_log "large_sample=`large_sample'" _n
                                        file write failure_log "personnel_definition=`personnel_definition'" _n
                                        file write failure_log "panel_group_folder=`panel_group_folder'" _n
                                        file write failure_log "event=`event'" _n
                                        file write failure_log "req=`req'" _n
                                        file write failure_log "control_variation=`control_variation_title'" _n
                                        file write failure_log "target=`target'" _n
                                        file write failure_log "event_type=`event_type'" _n
                                        file write failure_log "control=`control_fname'" _n
                                        file write failure_log "standardize=`std'" _n
                                        file write failure_log "mode=`mode_tag'" _n
                                        file write failure_log "control_for_other_events=`c_var'" _n
                                        file write failure_log "control_kappa=`control_kappa'" _n
                                        file write failure_log "control_atc=`control_atc'" _n
                                        file write failure_log "cohort_list=`cohort_list'" _n
                                        file write failure_log "obs_control=`obs_control'" _n
                                        file write failure_log "obs_treated0=`obs_treated0'" _n
                                        file write failure_log "obs_treated1=`obs_treated1'" _n
                                        file write failure_log "brd_control=`brd_control'" _n
                                        file write failure_log "brd_treated0=`brd_treated0'" _n
                                        file write failure_log "brd_treated1=`brd_treated1'" _n
                                        file write failure_log "prd_control=`prd_control'" _n
                                        file write failure_log "prd_treated0=`prd_treated0'" _n
                                        file write failure_log "prd_treated1=`prd_treated1'" _n
                                        file write failure_log "log_path=`event_log_path'/`file_stub'.log" _n
                                        file close failure_log
                                    }

                                    if `spec_failed' == 1 {
                                        log close
                                        continue
                                    }

                                    tempvar didimp_esample tag_brd_c tag_brd_t0 tag_brd_t1 tag_prd_c tag_prd_t0 tag_prd_t1
                                    gen byte `didimp_esample' = e(sample)

                                    count if `didimp_esample' & treated == 0
                                    local obs_control = r(N)
                                    count if `didimp_esample' & treated == 1 & atc_sharing == 0
                                    local obs_treated0 = r(N)
                                    count if `didimp_esample' & treated == 1 & atc_sharing == 1
                                    local obs_treated1 = r(N)

                                    egen `tag_brd_c' = tag(boardname) if `didimp_esample' & treated == 0
                                    egen `tag_brd_t0' = tag(boardname) if `didimp_esample' & treated == 1 & atc_sharing == 0
                                    egen `tag_brd_t1' = tag(boardname) if `didimp_esample' & treated == 1 & atc_sharing == 1

                                    count if `tag_brd_c' == 1
                                    local brd_control = r(N)
                                    count if `tag_brd_t0' == 1
                                    local brd_treated0 = r(N)
                                    count if `tag_brd_t1' == 1
                                    local brd_treated1 = r(N)

                                    egen `tag_prd_c' = tag(product) if `didimp_esample' & treated == 0
                                    egen `tag_prd_t0' = tag(product) if `didimp_esample' & treated == 1 & atc_sharing == 0
                                    egen `tag_prd_t1' = tag(product) if `didimp_esample' & treated == 1 & atc_sharing == 1

                                    count if `tag_prd_c' == 1
                                    local prd_control = r(N)
                                    count if `tag_prd_t0' == 1
                                    local prd_treated0 = r(N)
                                    count if `tag_prd_t1' == 1
                                    local prd_treated1 = r(N)

                                    forvalues sample_row = 1/2 {
                                        matrix sample_stats[`sample_row', 1] = `obs_control'
                                        matrix sample_stats[`sample_row', 2] = `obs_treated0'
                                        matrix sample_stats[`sample_row', 3] = `obs_treated1'
                                        matrix sample_stats[`sample_row', 4] = `brd_control'
                                        matrix sample_stats[`sample_row', 5] = `brd_treated0'
                                        matrix sample_stats[`sample_row', 6] = `brd_treated1'
                                        matrix sample_stats[`sample_row', 7] = `prd_control'
                                        matrix sample_stats[`sample_row', 8] = `prd_treated0'
                                        matrix sample_stats[`sample_row', 9] = `prd_treated1'
                                    }

                                    matrix did2_b_full = e(b)
                                    matrix did2_V_full = e(V)
                                    local did2_ncol = colsof(did2_b_full)
                                    local did2_names : colnames did2_b_full

                                    foreach sval in 0 1 {
                                        tempname col_map
                                        matrix `col_map' = J(1, `n_total', 0)

                                        forvalues i = 1/`n_total' {
                                            local coef_name : word `i' of `coef_names'
                                            local found_col 0

                                            local candidate_list ""

                                            if substr("`coef_name'", 1, 4) == "pre_" {
                                                local k = substr("`coef_name'", 5, .)
                                                local candidate_list pre`k'
                                            }
                                            else if substr("`coef_name'", 1, 5) == "post_" {
                                                local h = substr("`coef_name'", 6, .)
                                                local candidate_list tau`h'_`sval'
                                            }

                                            foreach candidate of local candidate_list {
                                                forvalues c = 1/`did2_ncol' {
                                                    local cn : word `c' of `did2_names'
                                                    if `found_col' == 0 & "`cn'" == "`candidate'" {
                                                        local found_col `c'
                                                    }
                                                }
                                            }

                                            if `found_col' > 0 {
                                                matrix `col_map'[1, `i'] = `found_col'
                                                matrix did2_b_s`sval'[1, `i'] = did2_b_full[1, `found_col']
                                            }
                                            else {
                                                local spec_failed 1
                                                local failure_reason "Coefficient not estimated in hetby mode: `coef_name' for sharingatc=`sval'"
                                                di as error "`failure_reason'"
                                                capture confirm file "`failure_log_path'"
                                                if _rc {
                                                    file open failure_log using "`failure_log_path'", write text replace
                                                }
                                                else {
                                                    file open failure_log using "`failure_log_path'", write text append
                                                }
                                                file write failure_log "============================================================" _n
                                                file write failure_log "`c(current_date)' `c(current_time)'" _n
                                                file write failure_log "`failure_reason'" _n
                                                file write failure_log "atc=`atc'" _n
                                                file write failure_log "large_sample=`large_sample'" _n
                                                file write failure_log "personnel_definition=`personnel_definition'" _n
                                                file write failure_log "panel_group_folder=`panel_group_folder'" _n
                                                file write failure_log "event=`event'" _n
                                                file write failure_log "req=`req'" _n
                                                file write failure_log "control_variation=`control_variation_title'" _n
                                                file write failure_log "target=`target'" _n
                                                file write failure_log "event_type=`event_type'" _n
                                                file write failure_log "control=`control_fname'" _n
                                                file write failure_log "standardize=`std'" _n
                                                file write failure_log "mode=`mode_tag'" _n
                                                file write failure_log "control_for_other_events=`c_var'" _n
                                                file write failure_log "control_kappa=`control_kappa'" _n
                                                file write failure_log "control_atc=`control_atc'" _n
                                                file write failure_log "cohort_list=`cohort_list'" _n
                                                file write failure_log "obs_control=`obs_control'" _n
                                                file write failure_log "obs_treated0=`obs_treated0'" _n
                                                file write failure_log "obs_treated1=`obs_treated1'" _n
                                                file write failure_log "brd_control=`brd_control'" _n
                                                file write failure_log "brd_treated0=`brd_treated0'" _n
                                                file write failure_log "brd_treated1=`brd_treated1'" _n
                                                file write failure_log "prd_control=`prd_control'" _n
                                                file write failure_log "prd_treated0=`prd_treated0'" _n
                                                file write failure_log "prd_treated1=`prd_treated1'" _n
                                                file write failure_log "log_path=`event_log_path'/`file_stub'.log" _n
                                                file close failure_log
                                                continue, break
                                            }
                                        }

                                        if `spec_failed' == 1 {
                                            continue, break
                                        }
                                        forvalues i = 1/`n_total' {
                                            local ci = el(`col_map', 1, `i')
                                            if `ci' == 0 {
                                                continue
                                            }
                                            forvalues j = 1/`n_total' {
                                                local cj = el(`col_map', 1, `j')
                                                if `cj' == 0 {
                                                    continue
                                                }
                                                matrix did2_V_s`sval'[`i', `j'] = did2_V_full[`ci', `cj']
                                            }
                                        }
                                    }

                                    * Common pretrends should be shown only once in the graph.
                                    if `spec_failed' == 0 {
                                        forvalues i = 1/`n_total' {
                                            local coef_name : word `i' of `coef_names'
                                            if substr("`coef_name'", 1, 4) == "pre_" {
                                                matrix did2_b_s1[1, `i'] = .
                                                forvalues j = 1/`n_total' {
                                                    matrix did2_V_s1[`i', `j'] = 0
                                                    matrix did2_V_s1[`j', `i'] = 0
                                                }
                                            }
                                        }
                                    }
                                }

                                * ============================================================
                                * 2. separate = 1: run two separate did_imputation regressions
                                * ============================================================
                                if `separate' == 1 {
                                    foreach sval in 0 1 {
                                        preserve
                                        drop if treated == 1 & atc_sharing != `sval'

                                        capture noisily did_imputation `target' id `timevar' event_cohort_did_imputation, fe(`fe_spec') horizons(`did_horizons') pretrends(`did_pretrend') autosample tol(0.1) minn(0) cluster(`cluster_var') `did_controls'
                                        local did_rc = _rc
                                        if `did_rc' != 0 {
                                            local spec_failed 1
                                            local failure_reason "did_imputation failed in separate mode for sharingatc=`sval': r(`did_rc')"
                                            capture confirm file "`failure_log_path'"
                                            if _rc {
                                                file open failure_log using "`failure_log_path'", write text replace
                                            }
                                            else {
                                                file open failure_log using "`failure_log_path'", write text append
                                            }
                                            file write failure_log "============================================================" _n
                                            file write failure_log "`c(current_date)' `c(current_time)'" _n
                                            file write failure_log "`failure_reason'" _n
                                            file write failure_log "atc=`atc'" _n
                                            file write failure_log "large_sample=`large_sample'" _n
                                            file write failure_log "personnel_definition=`personnel_definition'" _n
                                            file write failure_log "panel_group_folder=`panel_group_folder'" _n
                                            file write failure_log "event=`event'" _n
                                            file write failure_log "req=`req'" _n
                                            file write failure_log "control_variation=`control_variation_title'" _n
                                            file write failure_log "target=`target'" _n
                                            file write failure_log "event_type=`event_type'" _n
                                            file write failure_log "control=`control_fname'" _n
                                            file write failure_log "standardize=`std'" _n
                                            file write failure_log "mode=`mode_tag'" _n
                                            file write failure_log "control_for_other_events=`c_var'" _n
                                            file write failure_log "control_kappa=`control_kappa'" _n
                                            file write failure_log "control_atc=`control_atc'" _n
                                            file write failure_log "cohort_list=`cohort_list'" _n
                                            file write failure_log "obs_control=`obs_control'" _n
                                            file write failure_log "obs_treated0=`obs_treated0'" _n
                                            file write failure_log "obs_treated1=`obs_treated1'" _n
                                            file write failure_log "brd_control=`brd_control'" _n
                                            file write failure_log "brd_treated0=`brd_treated0'" _n
                                            file write failure_log "brd_treated1=`brd_treated1'" _n
                                            file write failure_log "prd_control=`prd_control'" _n
                                            file write failure_log "prd_treated0=`prd_treated0'" _n
                                            file write failure_log "prd_treated1=`prd_treated1'" _n
                                            file write failure_log "log_path=`event_log_path'/`file_stub'.log" _n
                                            file close failure_log
                                            restore
                                            continue, break
                                        }

                                        tempvar didimp_esample tag_brd_c tag_brd_t0 tag_brd_t1 tag_prd_c tag_prd_t0 tag_prd_t1
                                        gen byte `didimp_esample' = e(sample)

                                        count if `didimp_esample' & treated == 0
                                        local obs_control = r(N)
                                        count if `didimp_esample' & treated == 1 & atc_sharing == 0
                                        local obs_treated0 = r(N)
                                        count if `didimp_esample' & treated == 1 & atc_sharing == 1
                                        local obs_treated1 = r(N)

                                        egen `tag_brd_c' = tag(boardname) if `didimp_esample' & treated == 0
                                        egen `tag_brd_t0' = tag(boardname) if `didimp_esample' & treated == 1 & atc_sharing == 0
                                        egen `tag_brd_t1' = tag(boardname) if `didimp_esample' & treated == 1 & atc_sharing == 1

                                        count if `tag_brd_c' == 1
                                        local brd_control = r(N)
                                        count if `tag_brd_t0' == 1
                                        local brd_treated0 = r(N)
                                        count if `tag_brd_t1' == 1
                                        local brd_treated1 = r(N)

                                        egen `tag_prd_c' = tag(product) if `didimp_esample' & treated == 0
                                        egen `tag_prd_t0' = tag(product) if `didimp_esample' & treated == 1 & atc_sharing == 0
                                        egen `tag_prd_t1' = tag(product) if `didimp_esample' & treated == 1 & atc_sharing == 1

                                        count if `tag_prd_c' == 1
                                        local prd_control = r(N)
                                        count if `tag_prd_t0' == 1
                                        local prd_treated0 = r(N)
                                        count if `tag_prd_t1' == 1
                                        local prd_treated1 = r(N)

                                        local sample_row = `sval' + 1
                                        matrix sample_stats[`sample_row', 1] = `obs_control'
                                        matrix sample_stats[`sample_row', 2] = `obs_treated0'
                                        matrix sample_stats[`sample_row', 3] = `obs_treated1'
                                        matrix sample_stats[`sample_row', 4] = `brd_control'
                                        matrix sample_stats[`sample_row', 5] = `brd_treated0'
                                        matrix sample_stats[`sample_row', 6] = `brd_treated1'
                                        matrix sample_stats[`sample_row', 7] = `prd_control'
                                        matrix sample_stats[`sample_row', 8] = `prd_treated0'
                                        matrix sample_stats[`sample_row', 9] = `prd_treated1'

										matrix did2_b_full = e(b)
                                        matrix did2_V_full = e(V)
                                        local did2_ncol = colsof(did2_b_full)
                                        local did2_names : colnames did2_b_full

                                        tempname col_map
                                        matrix `col_map' = J(1, `n_total', 0)

                                        forvalues i = 1/`n_total' {
                                            local coef_name : word `i' of `coef_names'
                                            local found_col 0

                                            local candidate_list ""

                                            if substr("`coef_name'", 1, 3) == "pre_" {
                                                local k = substr("`coef_name'", 5, .)
                                                local candidate_list pre`k'
                                            }
                                            else if substr("`coef_name'", 1, 5) == "post_" {
                                                local h = substr("`coef_name'", 6, .)
                                                local candidate_list tau`h'
                                            }

                                            foreach candidate of local candidate_list {
                                                forvalues c = 1/`did2_ncol' {
                                                    local cn : word `c' of `did2_names'
                                                    if `found_col' == 0 & "`cn'" == "`candidate'" {
                                                        local found_col `c'
                                                    }
                                                }
                                            }

                                            if `found_col' > 0 {
                                                matrix `col_map'[1, `i'] = `found_col'
                                                matrix did2_b_s`sval'[1, `i'] = did2_b_full[1, `found_col']
                                            }
                                            else {
                                                di as error "Coefficient not estimated in separate mode: `coef_name' for sharingatc=`sval'"
                                                exit 498
                                            }
                                        }

                                        forvalues i = 1/`n_total' {
                                            local ci = el(`col_map', 1, `i')
                                            if `ci' == 0 {
                                                continue
                                            }
                                            forvalues j = 1/`n_total' {
                                                local cj = el(`col_map', 1, `j')
                                                if `cj' == 0 {
                                                    continue
                                                }
                                                matrix did2_V_s`sval'[`i', `j'] = did2_V_full[`ci', `cj']
                                            }
                                        }
                                        restore
                                    }
                                }

                                if `spec_failed' == 1 {
                                    log close
                                    continue
                                }

                                * -------- export regression results for re-plotting --------
                                preserve
                                clear
                                set obs 0

                                gen str32 event = ""
                                gen str16 target = ""
                                gen str16 control = ""
                                gen str25 control_variation = ""
                                gen str16 standardize = ""
                                gen str16 event_type = ""
                                gen str16 mode = ""
                                gen sharingatc = .
                                gen str16 coef_name = ""
                                gen rel_quarter = .
                                gen estimate = .
                                gen variance = .
                                gen std_error = .
                                gen ci_lb_95 = .
                                gen ci_ub_95 = .
                                gen obs_control = .
                                gen obs_treated0 = .
                                gen obs_treated1 = .
                                gen brd_control = .
                                gen brd_treated0 = .
                                gen brd_treated1 = .
                                gen prd_control = .
                                gen prd_treated0 = .
                                gen prd_treated1 = .
                                gen str16 control_var = "`c_var'"
                                gen str16 control_kappa = "`control_kappa'"
                                gen str16 atc = "`atc'"
                                gen str16 control_atc = "`control_atc'"
                                foreach cov_name of local coef_names {
                                    gen cov_`cov_name' = .
                                }

                                forvalues sval = 0/1 {
                                    local sample_row = `sval' + 1
                                    local obs_control_out = el(sample_stats, `sample_row', 1)
                                    local obs_treated0_out = el(sample_stats, `sample_row', 2)
                                    local obs_treated1_out = el(sample_stats, `sample_row', 3)
                                    local brd_control_out = el(sample_stats, `sample_row', 4)
                                    local brd_treated0_out = el(sample_stats, `sample_row', 5)
                                    local brd_treated1_out = el(sample_stats, `sample_row', 6)
                                    local prd_control_out = el(sample_stats, `sample_row', 7)
                                    local prd_treated0_out = el(sample_stats, `sample_row', 8)
                                    local prd_treated1_out = el(sample_stats, `sample_row', 9)

                                    forvalues i = 1/`n_total' {
                                        local this_coef : word `i' of `coef_names'
                                        local b = el(did2_b_s`sval', 1, `i')
                                        local v = el(did2_V_s`sval', `i', `i')
                                        local se = .
                                        local lb = .
                                        local ub = .
                                        local rel_q = .

                                        if !missing(`v') & `v' >= 0 {
                                            local se = sqrt(`v')
                                        }
                                        if !missing(`b') & !missing(`se') {
                                            local lb = `b' - 1.96 * `se'
                                            local ub = `b' + 1.96 * `se'
                                        }

                                        if substr("`this_coef'", 1, 4) == "pre_" {
                                            local rel_q = -real(substr("`this_coef'", 5, .))
                                        }
                                        else if substr("`this_coef'", 1, 5) == "post_" {
                                            local rel_q = real(substr("`this_coef'", 6, .))
                                        }

                                        local row = _N + 1
                                        set obs `row'
                                        replace event = "`event'" in `row'
                                        replace target = "`target'" in `row'
                                        replace control = "`control'" in `row'
                                        replace control_variation = "`control_variation_title'" in `row'
                                        replace standardize = "`std'" in `row'
                                        replace event_type = "`event_type'" in `row'
                                        replace mode = "`mode_tag'" in `row'
                                        replace control_var = "`c_var'" in `row'
                                        replace control_kappa = "`control_kappa'" in `row'
                                        replace atc = "`atc'" in `row'
                                        replace control_atc = "`control_atc'" in `row'
                                        replace sharingatc = `sval' in `row'
                                        replace coef_name = "`this_coef'" in `row'
                                        replace rel_quarter = `rel_q' in `row'
                                        replace estimate = `b' in `row'
                                        replace variance = `v' in `row'
                                        replace std_error = `se' in `row'
                                        replace ci_lb_95 = `lb' in `row'
                                        replace ci_ub_95 = `ub' in `row'
                                        replace obs_control = `obs_control_out' in `row'
                                        replace obs_treated0 = `obs_treated0_out' in `row'
                                        replace obs_treated1 = `obs_treated1_out' in `row'
                                        replace brd_control = `brd_control_out' in `row'
                                        replace brd_treated0 = `brd_treated0_out' in `row'
                                        replace brd_treated1 = `brd_treated1_out' in `row'
                                        replace prd_control = `prd_control_out' in `row'
                                        replace prd_treated0 = `prd_treated0_out' in `row'
                                        replace prd_treated1 = `prd_treated1_out' in `row'

                                        forvalues j = 1/`n_total' {
                                            local cov_coef : word `j' of `coef_names'
                                            replace cov_`cov_coef' = el(did2_V_s`sval', `i', `j') in `row'
                                        }
                                    }
                                }

                                sort sharingatc rel_quarter
                                export delimited using "`event_csv_path'/`file_stub'.csv", replace
                                restore

                                * -------- plot two sharing groups together --------
                                local graph_title_size small
                                
                                local cv_title ""
                                if "`c_var'" != "none" {
                                    local cv_title " |`c_var'"
                                }
                                local kappa_title " |`control_kappa'|`atc'|`control_atc'"

                                event_plot did2_b_s0#did2_V_s0 did2_b_s1#did2_V_s1, ///
                                    stub_lag(post_# post_#) ///
                                    stub_lead(pre_# pre_#) ///
                                    trimlead(`trimlead') trimlag(`trimlag') ///
                                    plottype(scatter) ciplottype(rcap) ///
                                    together perturb(-`perturb_step'(`perturb_span')`perturb_step') noautolegend ///
                                    graph_opt( ///
                                        title("`event_name' `event_type' `control_title' `std' | `group_label'`cv_title'`kappa_title'", size(`graph_title_size')) ///
                                        xtitle("Periods since the event", size(small)) ///
                                        ytitle("`target'", size(`graph_title_size')) ///
                                        xlabel(`xlabel_spec', nogrid) ///
                                        legend(order(1 "sharingatc=0" 3 "sharingatc=1") ///
                                            rows(1) position(6) region(style(none))) ///
                                        xline(-0.5, lcolor(gs8) lpattern(dash)) ///
                                        yline(0, lcolor(gs8)) ///
                                        graphregion(color(white)) bgcolor(white) ///
                                        ylabel(, angle(horizontal)) ///
                                    ) ///
                                    lag_opt1(msymbol(O) color(black)) lag_ci_opt1(color(black)) ///
                                    lag_opt2(msymbol(Th) color(navy)) lag_ci_opt2(color(navy))

                                graph export "`event_fig_path'/`file_stub'.png", replace width(`graph_width')

                                log close
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
clear all
