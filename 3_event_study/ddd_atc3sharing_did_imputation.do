clear all
set more off
set trace off

// ================================================================
// Purpose:
// Estimate stacked did_imputation specifications that compare post-event
// outcome changes for treated products with and without ATC sharing.
//
// Design:
// - Build stacked cohort-specific samples for each event/control/event_type
//   combination. If exposure lists are nonempty, run did_imputation with
//   project(atc_sharing exposure); if both exposure lists are empty, run the
//   original hetby(atc_sharing) DDD specification.
// - With exposure, beta1 is tau_cons, the ATT for treated products without ATC
//   sharing; beta2 is tau_atc_sharing; beta3 is tau_exposure_project.
// - Without exposure, beta1 is tau_0 and beta2 is tau_1 - tau_0.
// - beta1 + beta2 is the projected ATT for sharing products at zero exposure.
//
// Sample / Measurement:
// - The script loops over treatment-side definitions, whether event-pair
//   restrictions are imposed, control-group definitions, and two outcome
//   scaling choices: within-product standardization or cohort-entry
//   normalization.
// - Under normalization, the top 5% of (boardname, product, data_cohort)
//   groups by within-group max-min range are dropped to reduce leverage
//   from extreme normalized trajectories.
//
// Output:
// - logs/ddd_atcsharing_did_imputation_{atc}*_fe*/
// - tex/ddd_atcsharing_did_imputation_{atc}*_fe*/
// - csv/ddd_atcsharing_did_imputation_{atc}*_fe*/
// ================================================================

* ================= user config =================
local atcs atc3 atc2
* atc3 atc2
local large_sample 1
* 0 1
local personnel_definition narrow
* narrow medium broad
local outlier_treatment "winsorize"
* trim winsorize none
local outlier_treatment_percentile "p95"
* p90 p95 p99

* ================= path =================
local code_path "`c(pwd)'"
local parent_path = regexr("`code_path'", "[/\\][^/\\]+$", "")
local project_path = regexr("`parent_path'", "[/\\][^/\\]+$", "")
local project_path = subinstr("`project_path'", "\", "/", .)

local panel_levels quarter
* quarter

cap mkdir "`project_path'/logs"
cap mkdir "`project_path'/tex"
cap mkdir "`project_path'/csv"
local failure_log_path "`project_path'/logs/ddd_atcsharing_did_imputation_failure.log"

capture program drop _post_didimp_estimates
program define _post_didimp_estimates, eclass
    args bmat vmat obs
    ereturn post `bmat' `vmat', obs(`obs')
    ereturn local cmd "did_imputation"
end

local events interlock_dissolution to_B_not_in_A to_B_still_in_A
* interlock_dissolution to_B_not_in_A to_B_still_in_A
local controls not
* not notyet purecontrol
local targets price
* price price0 revenue quantity
local standardize_types log_transform
* log_transform standardize normalize
local event_types event
* event first_event
local reqs 0 1
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
* A B
local include_eventpair_values 0
* 1 0
local fe_levels 1
* 1 2 3
local exposure_types rival_share hhi
* rival_share hhi
local exposure_metrics quantity revenue
* quantity revenue

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
    local movement_output_suffix "_ls_`personnel_definition'"
}

local exposure_type_values "`exposure_types'"
local exposure_metric_values "`exposure_metrics'"
if "`exposure_type_values'" == "" & "`exposure_metric_values'" == "" {
    local exposure_type_values none
    local exposure_metric_values none
}
else if "`exposure_type_values'" == "" | "`exposure_metric_values'" == "" {
    di as error "exposure_types and exposure_metrics must either both be empty or both be nonempty"
    exit 198
}

foreach atc of local atcs {
    if !inlist("`atc'", "atc2", "atc3") {
        di as error "atc must be one of: atc2, atc3"
        exit 198
    }

    local data_root "`project_path'/data/cohort_data_with_atcsharing_`atc'"
    foreach exposure_type of local exposure_type_values {
        local use_exposure 1
        if "`exposure_type'" == "none" {
            local use_exposure 0
        }
        if `use_exposure' & !inlist("`exposure_type'", "rival_share", "hhi") {
            di as error "exposure_type must be one of: rival_share, hhi"
            exit 198
        }

        foreach exposure_metric of local exposure_metric_values {
            if (`use_exposure' & !inlist("`exposure_metric'", "quantity", "revenue")) | (!`use_exposure' & "`exposure_metric'" != "none") {
                di as error "exposure_metric must be one of: quantity, revenue"
                exit 198
            }

            local exposure_var "none"
            local exposure_type_tag "noD"
            local exposure_metric_tag "base"
            if `use_exposure' {
                local exposure_var "`exposure_type'_`exposure_metric'_`atc'"
                local exposure_type_tag "`exposure_type'"
                local exposure_metric_tag "`exposure_metric'"
                if "`exposure_type'" == "rival_share" {
                    local exposure_type_tag "rs"
                }
                if "`exposure_metric'" == "quantity" {
                    local exposure_metric_tag "qty"
                }
                if "`exposure_metric'" == "revenue" {
                    local exposure_metric_tag "rev"
                }
            }
            local output_tag_base "ddd_atcsh_didimp_`atc'`movement_output_suffix'_`exposure_type_tag'_`exposure_metric_tag'"

foreach panel_level of local panel_levels {
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
            local panel_group_folder "`panel_level'-level_`group_label'`movement_suffix'"
            local panel_output_folder "`panel_level'_`group_label'`movement_output_suffix'"
            local data_path "`data_root'/`panel_group_folder'"

            foreach fe_level of local fe_levels {
                if !inlist(`fe_level', 1, 2, 3) {
                    di as error "fe_level must be one of: 1, 2, 3"
                    exit 198
                }

                local outlier_output_tag "`outlier_treatment'`outlier_treatment_percentile'"
                if "`outlier_treatment'" == "winsorize" {
                    local outlier_output_tag "ws`outlier_treatment_percentile'"
                }
                else if "`outlier_treatment'" == "trim" {
                    local outlier_output_tag "tr`outlier_treatment_percentile'"
                }
                local fe_output_tag "`output_tag_base'_`outlier_output_tag'_fe`fe_level'"
                local fe_log_root "`project_path'/logs/`fe_output_tag'"
                local fe_tex_root "`project_path'/tex/`fe_output_tag'"
                local fe_csv_root "`project_path'/csv/`fe_output_tag'"

                cap mkdir "`fe_log_root'"
                cap mkdir "`fe_tex_root'"
                cap mkdir "`fe_csv_root'"
                cap mkdir "`fe_log_root'/`panel_output_folder'"
                cap mkdir "`fe_tex_root'/`panel_output_folder'"
                cap mkdir "`fe_csv_root'/`panel_output_folder'"

                foreach event of local events {
                    * Interlock events do not vary with treatment/include parameters.
                    * Keep only one canonical run: treatment_group=B and include_eventpair=1.
                    if inlist("`event'", "direct_interlock", "indirect_interlock") & ("`treatment_group'" != "B" | `include_eventpair' != 1) {
                        continue
                    }

                    cap mkdir "`fe_log_root'/`panel_output_folder'/`event'"
                    cap mkdir "`fe_tex_root'/`panel_output_folder'/`event'"
                    cap mkdir "`fe_csv_root'/`panel_output_folder'/`event'"

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
                                        if "`treatment_group'" == "A" & "`event'" == "to_B_still_in_A" {
                                            local cohort_list ""
                                        }
                                        else if "`treatment_group'" == "A" {
                                            if "`event'" == "interlock_dissolution" {
                                                local cohort_list 2009 2010 2011 2012 2014 2015 2016 2017 2018
                                            }
                                            else if "`event'" == "to_B_not_in_A" {
                                                local cohort_list 2009 2010 2012 2014 2015 2016 2017 2018
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
                                            local cohort_list 2009 2010 2012 2013 2014 2015 2016 2017 2018
                                        }
                                    }
                                    else if "`req'" == "2" {
                                        if "`event'" == "interlock_dissolution" {
                                            local cohort_list 2009 2010 2011 2012 2013 2014 2015 2016 2017 2018
                                            if "`treatment_group'" == "B" & "`personnel_definition'" == "narrow" {
                                                local cohort_list 2009 2010 2011 2012 2014 2015 2016 2017 2018
                                            }
                                        }
                                        else if "`treatment_group'" == "A" & "`event'" == "to_B_not_in_A" {
                                            if "`personnel_definition'" == "broad" {
                                                local cohort_list 2012 2017
                                            }
                                            else if "`personnel_definition'" == "medium" {
                                                local cohort_list 2012 2014 2017
                                            }
                                            else if "`personnel_definition'" == "narrow" {
                                                local cohort_list 2010 2012 2013 2014 2016 2017
                                            }
                                        }
                                        else if "`treatment_group'" == "A" & "`event'" == "to_B_still_in_A" {
                                            if "`atc'" == "atc3" {
                                                local cohort_list ""
                                            }
                                            else if "`personnel_definition'" == "broad" {
                                                local cohort_list 2010 2011 2012 2013 2014 2016 2017 2018
                                            }
                                            else if "`personnel_definition'" == "medium" {
                                                local cohort_list 2009 2010 2011 2012 2013 2014 2016 2017 2018
                                            }
                                            else if "`personnel_definition'" == "narrow" {
                                                local cohort_list 2009 2010 2011 2012 2013 2014 2015 2016 2017
                                            }
                                        }
                                        else if "`treatment_group'" == "B" & "`event'" == "to_B_not_in_A" {
                                            if "`personnel_definition'" == "broad" {
                                                local cohort_list 2010 2016
                                            }
                                            else if "`personnel_definition'" == "medium" {
                                                local cohort_list 2010 2011 2014 2016
                                            }
                                            else if "`personnel_definition'" == "narrow" {
                                                local cohort_list 2010 2012 2015 2016
                                            }
                                        }
                                        else if "`treatment_group'" == "B" & "`event'" == "to_B_still_in_A" {
                                            if inlist("`personnel_definition'", "broad", "medium") {
                                                local cohort_list 2010 2012 2016 2017 2018
                                            }
                                            else if "`personnel_definition'" == "narrow" {
                                                local cohort_list 2010 2012 2015 2016 2017 2018
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
                                    di as error "Unknown control type: `control'"
                                    exit 198
                                }

                                local suffix ""
                                if "`event_type'" == "first_event" {
                                    local suffix "_first_event"
                                }

                                local control_variation_values all
                                if "`req'" == "2" {
                                    local control_variation_values "`req2_control_variations'"
                                }

                                foreach control_variation of local control_variation_values {
                                local control_variation_folder ""
                                local control_variation_title "all_controls"
                                local control_title "`control'"
                                if "`control_variation'" != "all" {
                                    local control_variation_folder "/`control_variation'"
                                    local control_variation_title "`control_variation'"
                                    local control_title "`control', `control_variation'"
                                }
                                
                                local control_var_tag ""
                                if "`c_var'" != "none" {
                                    local control_var_tag "_cv_`c_var'"
                                }
                                local kappa_tag ""
                                if "`control_kappa'" != "none" {
                                    local kappa_tag "_kappa_`control_kappa'"
                                }
                                local atc_tag "_atc_`control_atc'"
                                local file_stub "`event_type'_`control_fname'_`std'`control_var_tag'`kappa_tag'`atc_tag'"

                                cap mkdir "`fe_log_root'/`panel_output_folder'/`event'/req`req'"
                                cap mkdir "`fe_tex_root'/`panel_output_folder'/`event'/req`req'"
                                cap mkdir "`fe_csv_root'/`panel_output_folder'/`event'/req`req'"
                                cap mkdir "`fe_log_root'/`panel_output_folder'/`event'/req`req'`control_variation_folder'"
                                cap mkdir "`fe_tex_root'/`panel_output_folder'/`event'/req`req'`control_variation_folder'"
                                cap mkdir "`fe_csv_root'/`panel_output_folder'/`event'/req`req'`control_variation_folder'"

                                local log_file "`fe_log_root'/`panel_output_folder'/`event'/req`req'`control_variation_folder'/`file_stub'.log"

                                cap log close
                                log using "`log_file'", text replace

                                estimates clear

                                local csv_file "`fe_csv_root'/`panel_output_folder'/`event'/req`req'`control_variation_folder'/`file_stub'.csv"
                                local tex_file "`fe_tex_root'/`panel_output_folder'/`event'/req`req'`control_variation_folder'/`file_stub'.tex"
                                tempfile run_results
                                tempname posth
                                local skip_specification 0

                                postfile `posth' ///
                                    str12 panel_level ///
                                    str30 event ///
                                    str15 control ///
                                    str25 control_variation ///
                                    str15 std ///
                                    str15 event_type ///
                                    str20 target ///
                                    str20 model ///
                                    str15 control_var ///
                                    str15 control_kappa ///
                                    str15 atc ///
                                    str15 control_atc ///
                                    str15 exposure_type ///
                                    str15 exposure_metric ///
                                    str32 exposure_var ///
                                    double beta1 beta1_se beta1_p ///
                                    double beta2 beta2_se beta2_p ///
                                    double beta3 beta3_se beta3_p ///
                                    double te_share te_share_se te_share_p ///
                                    double pcteff_notshare pcteff_share ///
                                    double premean_notshare premean_share ///
                                    double N_obs N_control N_notshare N_share ///
                                    double product_control product_notshare product_share ///
                                    double board_control board_notshare board_share ///
                                    using `run_results', replace

                                foreach target of local targets {
                                    local first 1

                                    foreach cohort of local cohort_list {
                                        local data_file "`data_path'/`event_type'/req`req'/`control_folder'/`event'_`panel_level'_cohort_`cohort'`suffix'_balanced`movement_suffix'_`atc'.csv"

                                        import delimited "`data_file'", clear
                                        gen target_raw = `target'
                                        
                                        local event_anchor_q = yq(`cohort', 1)
                                        gen rel_quarter_all = yq(year, quarter) - `event_anchor_q'
                                        keep if rel_quarter_all >= -4 & rel_quarter_all <= 7
                                        drop rel_quarter_all

                                        gen event_cohort = .
                                        gen treated_in_stack = 0

                                        if "`event_type'" == "event" {
                                            replace event_cohort = `cohort' if event_`cohort' == 1
                                            replace treated_in_stack = event_`cohort' == 1
                                        }
                                        else {
                                            * first_event isolates the first adoption timing and avoids mixing later repeat exposures into the treated definition.
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
                                            local first 0
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
                                        bysort boardname product data_cohort: egen group_max_raw = max(`target')
                                        bysort boardname product data_cohort: egen group_min_raw = min(`target')
                                        gen group_ratio_raw = .
                                        replace group_ratio_raw = group_max_raw / group_min_raw if group_min_raw != 0 & !missing(group_min_raw)

                                        preserve
                                        keep boardname product data_cohort group_ratio_raw
                                        bysort boardname product data_cohort: keep if _n == 1
                                        quietly summarize group_ratio_raw, detail
                                        local p95_group_ratio = r(`outlier_treatment_percentile')
                                        restore

                                        drop if group_ratio_raw > `p95_group_ratio' & !missing(group_ratio_raw)
                                        drop group_max_raw group_min_raw group_ratio_raw
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
                                        * Normalization rescales each product by its cohort-entry baseline,
                                        * so post coefficients are interpretable relative to that baseline.
                                        if "`panel_level'" == "quarter" {
                                            bysort boardname product data_cohort: gen baseline = `target' if year == data_cohort & quarter == 1
                                        }
                                        else {
                                            bysort boardname product data_cohort: gen baseline = `target' if year == data_cohort
                                        }
                                        bysort boardname product data_cohort: egen baseline_value = max(baseline)
                                        replace `target' = `target' / baseline_value
                                        drop baseline baseline_value
                                    }
                                    else if "`std'" == "log_transform" {
                                        replace `target' = log(`target')
                                    }

                                    * Stack-specific panel id: the same product can reappear in another cohort stack and should be treated as a distinct unit there
                                    egen id = group(boardname product data_cohort)
                                    gen q_time = yq(year, quarter)
                                    format q_time %tq

                                    * Post is defined relative to each stack's own cohort entry date
                                    gen treat = treated_in_stack
                                    gen post = q_time >= yq(data_cohort, 1)
                                    gen pre_period = q_time < yq(data_cohort, 1)

                                    local premean_notshare = .
                                    local premean_share = .
                                    local basesd_notshare = .
                                    local basesd_share = .

                                    if "`std'" == "standardize" {
                                        quietly summarize target_raw if treat == 1 & atc_sharing == 0 & pre_period == 1
                                        local premean_notshare = r(mean)
                                        local basesd_notshare = r(sd)

                                        quietly summarize target_raw if treat == 1 & atc_sharing == 1 & pre_period == 1
                                        local premean_share = r(mean)
                                        local basesd_share = r(sd)
                                    }
                                    else {
                                        quietly summarize `target' if treat == 1 & atc_sharing == 0 & pre_period == 1, meanonly
                                        local premean_notshare = r(mean)
                                        quietly summarize `target' if treat == 1 & atc_sharing == 1 & pre_period == 1, meanonly
                                        local premean_share = r(mean)
                                    }

                                    quietly count if treat == 1 & atc_sharing == 0
                                    local N_notshare = r(N)
                                    quietly count if treat == 1 & atc_sharing == 1
                                    local N_share = r(N)
                                    quietly count if treat == 0
                                    local N_control = r(N)

                                    egen tag_id_notshare = tag(id) if treat == 1 & atc_sharing == 0
                                    quietly count if tag_id_notshare == 1
                                    local product_notshare = r(N)
                                    drop tag_id_notshare

                                    egen tag_id_share = tag(id) if treat == 1 & atc_sharing == 1
                                    quietly count if tag_id_share == 1
                                    local product_share = r(N)
                                    drop tag_id_share

                                    egen tag_id_control = tag(id) if treat == 0
                                    quietly count if tag_id_control == 1
                                    local product_control = r(N)
                                    drop tag_id_control

                                    * Also calculate unique boardnames
                                    egen tag_board_notshare = tag(boardname) if treat == 1 & atc_sharing == 0
                                    quietly count if tag_board_notshare == 1
                                    local board_notshare = r(N)
                                    drop tag_board_notshare

                                    egen tag_board_share = tag(boardname) if treat == 1 & atc_sharing == 1
                                    quietly count if tag_board_share == 1
                                    local board_share = r(N)
                                    drop tag_board_share

                                    egen tag_board_control = tag(boardname) if treat == 0
                                    quietly count if tag_board_control == 1
                                    local board_control = r(N)
                                    drop tag_board_control

                                    gen event_cohort_did_imputation = yq(event_cohort, 1) if !missing(event_cohort)
                                    replace atc_sharing = 0 if missing(atc_sharing)
                                    
                                    egen cohort_q_time_fe = group(data_cohort q_time)
                                    capture confirm variable `atc'
                                    if _rc {
                                        di as error "Missing ATC variable: `atc'"
                                        exit 111
                                    }
                                    if `use_exposure' {
                                        capture confirm numeric variable `exposure_var'
                                        if _rc {
                                            di as error "Missing or nonnumeric exposure variable: `exposure_var'"
                                            exit 111
                                        }
                                        replace `exposure_var' = 0 if missing(`exposure_var')
                                        gen exposure_project = atc_sharing * `exposure_var'
                                    }
                                    else {
                                        gen atc_sharing_het = atc_sharing if treat == 1
                                        replace atc_sharing_het = 0 if treat == 0
                                    }
                                    egen atc_id = group(`atc')
                                    egen cohort_q_time_atc_fe = group(cohort_q_time_fe atc_id)

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

                                    * FE level 1 uses stack-unit and common calendar-quarter FE.
                                    * FE level 2 replaces common time FE with cohort-by-quarter FE, allowing each stacked cohort to have its own aggregate path.
                                    * FE level 3 replaces separate time and ATC FE with cohort-by-quarter-by-ATC FE.
                                    local fe_spec "id q_time `final_cv_list'"
                                    if `fe_level' == 2 {
                                        local fe_spec "id cohort_q_time_fe `final_cv_list'"
                                    }
                                    else if `fe_level' == 3 {
                                        local fe_spec "id cohort_q_time_atc_fe `final_cv_list'"
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

                                    if "`control_atc'" == "separate" & `fe_level' != 3 {
                                        local fe_spec "`fe_spec' atc_id"
                                    }

                                    local N_obs_didimp = .
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
                                    local did_rc = .

                                    if `use_exposure' {
                                        tempvar didimp_sample
                                        capture noisily did_imputation `target' id q_time event_cohort_did_imputation, ///
                                            fe(`fe_spec') ///
                                            autosample tol(0.1) minn(0) `did_controls'
                                        local autosample_rc = _rc

                                        if `autosample_rc' != 0 {
                                            local failure_reason "did_imputation autosample pre-run failed in main DDD estimator: r(`autosample_rc')"
                                        }
                                        else {
                                            gen byte `didimp_sample' = e(sample)

                                            capture noisily did_imputation `target' id q_time event_cohort_did_imputation if `didimp_sample', ///
                                                fe(`fe_spec') ///
                                                project(atc_sharing exposure_project) ///
                                                tol(0.1) minn(0) `did_controls'
                                            local did_rc = _rc
                                        }
                                    }
                                    else {
                                        capture noisily did_imputation `target' id q_time event_cohort_did_imputation, ///
                                            fe(`fe_spec') ///
                                            hetby(atc_sharing_het) ///
                                            autosample tol(0.1) minn(0) `did_controls'
                                        local did_rc = _rc
                                    }

                                    if "`failure_reason'" == "" & `did_rc' != 0 {
                                        local failure_reason "did_imputation failed in main DDD estimator: r(`did_rc')"
                                    }
                                    else if "`failure_reason'" == "" {
                                        quietly count if e(sample)
                                        local N_obs_didimp = r(N)

                                        local beta0_expr "tau_0"
                                        local beta1_expr "tau_1 - tau_0"
                                        local share_expr "tau_1"
                                        if `use_exposure' {
                                            local beta0_expr "tau_cons"
                                            local beta1_expr "tau_atc_sharing"
                                            local beta2_expr "tau_exposure_project"
                                            local share_expr "tau_cons + tau_atc_sharing"
                                        }

                                        capture noisily lincom `beta0_expr'
                                        local lincom_beta0_rc = _rc
                                        if `lincom_beta0_rc' != 0 {
                                            local failure_reason "lincom `beta0_expr' failed in main DDD estimator: r(`lincom_beta0_rc')"
                                        }
                                        else {
                                            local beta0 = r(estimate)
                                            local beta0_se = r(se)
                                            local beta0_p = r(p)
                                        }

                                        capture noisily lincom `beta1_expr'
                                        local lincom_beta1_rc = _rc
                                        if `lincom_beta1_rc' != 0 {
                                            local failure_reason "lincom `beta1_expr' failed in main DDD estimator: r(`lincom_beta1_rc')"
                                        }
                                        else {
                                            local beta1 = r(estimate)
                                            local beta1_se = r(se)
                                            local beta1_p = r(p)
                                        }

                                        if `use_exposure' {
                                            capture noisily lincom `beta2_expr'
                                            local lincom_beta2_rc = _rc
                                            if `lincom_beta2_rc' != 0 {
                                                local failure_reason "lincom `beta2_expr' failed in main DDD estimator: r(`lincom_beta2_rc')"
                                            }
                                            else {
                                                local beta2 = r(estimate)
                                                local beta2_se = r(se)
                                                local beta2_p = r(p)
                                            }
                                        }

                                        capture noisily lincom `share_expr'
                                        local lincom_share_rc = _rc
                                        if `lincom_share_rc' != 0 {
                                            local failure_reason "lincom `share_expr' failed in main DDD estimator: r(`lincom_share_rc')"
                                        }
                                        else {
                                            local te_share = r(estimate)
                                            local te_share_se = r(se)
                                            local te_share_p = r(p)
                                        }
                                    }

                                    local pcteff_notshare = .
                                    if "`std'" == "standardize" {
                                        if !missing(`premean_notshare') & `premean_notshare' != 0 & !missing(`basesd_notshare') {
                                            local pcteff_notshare = 100 * (`beta0' * `basesd_notshare') / abs(`premean_notshare')
                                        }
                                    }
                                    else if !missing(`premean_notshare') & `premean_notshare' != 0 {
                                        local pcteff_notshare = 100 * `beta0' / abs(`premean_notshare')
                                    }

                                    local pcteff_share = .
                                    if "`std'" == "standardize" {
                                        if !missing(`premean_share') & `premean_share' != 0 & !missing(`basesd_share') {
                                            local pcteff_share = 100 * (`te_share' * `basesd_share') / abs(`premean_share')
                                        }
                                    }
                                    else if !missing(`premean_share') & `premean_share' != 0 {
                                        local pcteff_share = 100 * `te_share' / abs(`premean_share')
                                    }

                                    if "`failure_reason'" != "" {
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
                                        file write failure_log "control_for_other_events=`c_var'" _n
                                        file write failure_log "control_kappa=`control_kappa'" _n
                                        file write failure_log "control_atc=`control_atc'" _n
                                        file write failure_log "exposure_type=`exposure_type'" _n
                                        file write failure_log "exposure_metric=`exposure_metric'" _n
                                        file write failure_log "exposure_var=`exposure_var'" _n
                                        file write failure_log "cohort_list=`cohort_list'" _n
                                        file write failure_log "log_path=`log_file'" _n
                                        file close failure_log

                                        local skip_specification 1
                                        continue, break
                                    }

                                    tempname didimp_b didimp_V
                                    if `use_exposure' {
                                        matrix `didimp_b' = (`beta0', `beta1', `beta2')
                                        matrix colnames `didimp_b' = beta1 beta2 beta3
                                        matrix `didimp_V' = J(3, 3, 0)
                                        matrix rownames `didimp_V' = beta1 beta2 beta3
                                        matrix colnames `didimp_V' = beta1 beta2 beta3
                                        matrix `didimp_V'[1, 1] = `beta0_se'^2
                                        matrix `didimp_V'[2, 2] = `beta1_se'^2
                                        matrix `didimp_V'[3, 3] = `beta2_se'^2
                                    }
                                    else {
                                        matrix `didimp_b' = (`beta0', `beta1')
                                        matrix colnames `didimp_b' = beta1 beta2
                                        matrix `didimp_V' = J(2, 2, 0)
                                        matrix rownames `didimp_V' = beta1 beta2
                                        matrix colnames `didimp_V' = beta1 beta2
                                        matrix `didimp_V'[1, 1] = `beta0_se'^2
                                        matrix `didimp_V'[2, 2] = `beta1_se'^2
                                    }
                                    _post_didimp_estimates `didimp_b' `didimp_V' `N_obs_didimp'
                                    estimates store m_`target'

                                    post `posth' ///
                                        ("`panel_level'") ///
                                        ("`event'") ///
                                        ("`control'") ///
                                        ("`control_variation_title'") ///
                                        ("`std'") ///
                                        ("`event_type'") ///
                                        ("`target'") ///
                                        ("did_imputation") ///
                                        ("`c_var'") ///
                                        ("`control_kappa'") ///
                                        ("`atc'") ///
                                        ("`control_atc'") ///
                                        ("`exposure_type'") ///
                                        ("`exposure_metric'") ///
                                        ("`exposure_var'") ///
                                        (`beta0') (`beta0_se') (`beta0_p') ///
                                        (`beta1') (`beta1_se') (`beta1_p') ///
                                        (`beta2') (`beta2_se') (`beta2_p') ///
                                        (`te_share') (`te_share_se') (`te_share_p') ///
                                        (`pcteff_notshare') (`pcteff_share') ///
                                        (`premean_notshare') (`premean_share') ///
                                        (`N_obs_didimp') (`N_control') (`N_notshare') (`N_share') ///
                                        (`product_control') (`product_notshare') (`product_share') ///
                                        (`board_control') (`board_notshare') (`board_share')

                                    estimates restore m_`target'
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
                                }

                                if `skip_specification' {
                                    capture erase "`csv_file'"
                                    capture erase "`tex_file'"
                                    postclose `posth'
                                    log close
                                    continue
                                }

                                postclose `posth'
                                preserve
                                use `run_results', clear
                                export delimited using "`csv_file'", replace
                                restore

                                tempfile tex_fragment
                                local esttab_keep "beta1 beta2 beta3"
                                local beta2_label "beta2: ATT gap (Share - Not Share, exposure=0)"
                                local beta3_label "beta3: Shared-class exposure gradient"
                                if !`use_exposure' {
                                    local esttab_keep "beta1 beta2"
                                    local beta2_label "beta2: ATT gap (Share - Not Share)"
                                    local beta3_label ""
                                }

                                esttab m_price using "`tex_fragment'", ///
                                    replace booktabs se star(* 0.10 ** 0.05 *** 0.01) ///
                                    keep(`esttab_keep') ///
                                    coeflabels( ///
                                        beta1 "beta1: ATT (Not Share)" ///
                                        beta2 "`beta2_label'" ///
                                        beta3 "`beta3_label'" ///
                                    ) ///
                                    mtitles("Price") ///
                                    stats( ///
                                        premean_notshare ///
                                        premean_share ///
                                        pcteff_notshare ///
                                        pcteff_share ///
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
                                        fmt(3 3 2 2 0 0 0 0 0 0 0 0 0 0) ///
                                        labels( ///
                                            "Pre-treatment mean Y (Not Share)" ///
                                            "Pre-treatment mean Y (Share)" ///
                                            "Percent effect (Not Share)" ///
                                            "Percent effect (Share)" ///
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
                                    title("did_imputation DDD: `event', `event_type', `control_title', `std' | `group_label' | FE `fe_level' | kappa=`control_kappa' | atc=`atc' | control_atc=`control_atc' | exposure=`exposure_var'")

                                tempname fhin fhout
                                file open `fhin' using "`tex_fragment'", read text
                                file open `fhout' using "`tex_file'", write text replace
                                file write `fhout' "\documentclass[11pt]{article}" _n
                                file write `fhout' "\usepackage{booktabs}" _n
                                file write `fhout' "\begin{document}" _n

                                file read `fhin' line
                                while r(eof) == 0 {
                                    file write `fhout' "`line'" _n
                                    file read `fhin' line
                                }

                                file write `fhout' "\end{document}" _n
                                file close `fhin'
                                file close `fhout'

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
