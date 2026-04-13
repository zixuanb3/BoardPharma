clear all
set trace off

<<<<<<< Updated upstream
// ================================================================
// Purpose:
// Run stacked event-study estimation on cohort panels with atc3_sharing labels,
// and report dynamic effects separately for atc3_sharing=0 and atc3_sharing=1.
//
// Process:
// - Loop over panel, event, control, target, standardization, and event_type.
// - Stack eligible cohort files and build event-time dummies with pre_1 baseline.
// - Estimate csdid, did_imputation, TWFE, and eventstudyinteract under group split.
// - Rebuild group-specific coefficient and variance matrices, then export logs and figures.
//
// Input:
// - data/cohort_data_with_atc3sharing/{panel}/{event_type}/{control_folder}/
//   {event}_{panel}_cohort_{year} with optional first_event suffix and balanced suffix
//
// Output:
// - png figure files under figures/stacked_event_study_sharingatc3/
// - log files under logs/stacked_event_study_sharingatc3/
// ================================================================
=======
* ================================================================
* StackedEventStudy_v5.do
*
* atc3sharing mode:
*   - 1: split treated units by atc3_sharing (current sharing version)
*   - 0: run the original non-sharing version (single full-sample run)
*
* Directory switch by atc3sharing:
*   - 1: data/cohort_data_with_atc3sharing,
*        figures/stacked_event_study_sharingatc3,
*        logs/stacked_event_study_sharingatc3
*   - 0: data/cohort_data,
*        figures/stacked_event_study,
*        logs/stacked_event_study
* ================================================================

* ================= parameter =================
* 0: do NOT split by atc3_sharing (original mode)
* 1: split by atc3_sharing and output two plots per config
local atc3sharing 1
if !inlist(`atc3sharing', 0, 1) {
    di as error "atc3sharing must be 0 or 1"
    exit 198
}

* 0: keep original pre-period setting
* 1: for quarter level, shorten pre-periods to 8 quarters (t-2 years to t-1)
local shortened_preperiords 0
if !inlist(`shortened_preperiords', 0, 1) {
    di as error "shortened_preperiords must be 0 or 1"
    exit 198
}
>>>>>>> Stashed changes

* ================= path =================
local code_path "`c(pwd)'"
local project_path = regexr("`code_path'", "[/\\][^/\\]+$", "")
local project_path = subinstr("`project_path'", "\", "/", .)

if `atc3sharing' == 1 {
    local data_root "`project_path'/data/cohort_data_with_atc3sharing"
    local fig_root "`project_path'/figures/stacked_event_study_sharingatc3"
    local log_root "`project_path'/logs/stacked_event_study_sharingatc3"
}
else {
    local data_root "`project_path'/data/cohort_data"
    local fig_root "`project_path'/figures/stacked_event_study"
    local log_root "`project_path'/logs/stacked_event_study"
}

* ================= define events and cohorts =================
local panel_levels quarter 
* year
local events to_B_not_in_A direct_interlock to_B_still_in_A indirect_interlock 
local controls not notyet purecontrol
local targets revenue price quantity
local standardize_types standardize normalize
* nonstandardize
local event_types event first_event

* ================= loop =================
foreach panel_level of local panel_levels {
    local data_path "`data_root'/`panel_level'"
    local output_panel_level "`panel_level'"
    local fig_base_path "`fig_root'/`output_panel_level'"
    local log_base_path "`log_root'/`output_panel_level'"

    cap mkdir "`project_path'/figures"
    cap mkdir "`project_path'/logs"
    cap mkdir "`fig_root'"
    cap mkdir "`log_root'"
    cap mkdir "`fig_base_path'"
    cap mkdir "`log_base_path'"

    foreach event of local events {
        foreach target of local targets {
            foreach control of local controls {
                foreach std of local standardize_types {
                    foreach event_type of local event_types {

                        * -------- determine cohort list --------
                        local cohort_list ""

                        if "`panel_level'" == "year" {
                            if "`event'" == "direct_interlock" & "`event_type'" == "first_event" {
                                local cohort_list 2012 2013
                            }
                            else if "`event'" == "direct_interlock" & "`event_type'" == "event" {
                                local cohort_list 2011 2012 2013 2014 2015 2016
                            }
                            else if "`event'" == "indirect_interlock" & "`event_type'" == "first_event" {
                                local cohort_list 2011 2012 2013
                            }
                            else if "`event'" == "indirect_interlock" & "`event_type'" == "event" {
                                local cohort_list 2011 2012 2013 2014 2015 2016
                            }
                            else if "`event'" == "to_B_not_in_A" & "`event_type'" == "first_event" {
                                local cohort_list 2011 2012 2013 2015
                            }
                            else if "`event'" == "to_B_not_in_A" & "`event_type'" == "event" {
                                local cohort_list 2011 2012 2013 2014 2015 2016
                            }
                            else if "`event'" == "to_B_still_in_A" & "`event_type'" == "first_event" {
                                local cohort_list 2013
                            }
                            else if "`event'" == "to_B_still_in_A" & "`event_type'" == "event" {
                                local cohort_list 2011 2013 2014 2015 2016
                            }
                        }
                        else if "`panel_level'" == "quarter" {
                            if "`event'" == "direct_interlock" & "`event_type'" == "first_event" {
                                local cohort_list 2008 2009 2010 2011 2012 2013 2014 2017 2018
                            }
                            else if "`event'" == "direct_interlock" & "`event_type'" == "event" {
                                local cohort_list 2008 2009 2010 2011 2012 2013 2014 2015 2016 2017 2018
                            }
                            else if "`event'" == "indirect_interlock" & "`event_type'" == "first_event" {
                                local cohort_list 2008 2010 2011 2012 2013 2014
                            }
                            else if "`event'" == "indirect_interlock" & "`event_type'" == "event" {
                                local cohort_list 2008 2009 2010 2011 2012 2013 2014 2015 2016 2017 2018
                            }
                            else if "`event'" == "to_B_not_in_A" & "`event_type'" == "first_event" {
                                local cohort_list 2009 2010 2012 2013 2014 2016 2017 2018
                            }
                            else if "`event'" == "to_B_not_in_A" & "`event_type'" == "event" {
                                local cohort_list 2009 2010 2011 2012 2013 2014 2016 2017 2018
                            }
							else if "`event'" == "to_B_still_in_A" & "`event_type'" == "first_event" {
                                local cohort_list 2008 2009 2010 2011 2013 2014 2017 2018
                            }
                            else if "`event'" == "to_B_still_in_A" & "`event_type'" == "event" {
                                local cohort_list 2008 2009 2010 2011 2013 2014 2015 2017 2018
                            }
                        }

                        * No eligible cohorts under this combination
                        if "`cohort_list'" == "" {
                            continue
                        }

                        * -------- determine suffix for event_type --------
                        local suffix ""
                        if "`event_type'" == "first_event" {
                            local suffix "_first_event"
                        }

                        * -------- determine control folder name --------
                        if "`control'" == "notyet" {
                            local control_folder = "Not Yet"
                            local control_fname = "not_yet"
                        }
                        else if "`control'" == "purecontrol" {
                            local control_folder = "Pure Control"
                            local control_fname = "pure_control"
                        }
                        else if "`control'" == "not" {
                            local control_folder = "Not"
                            local control_fname = "not"
                        }
                        else {
                            di as error "Unknown control type"
                            exit 198
                        }

                        * -------- event name for display --------
                        local event_name = subinstr("`event'", "_", " ", .)

                        * -------- setup paths for this iteration --------
                        local event_fig_path "`fig_base_path'/`event'/`target'"
                        local event_log_path "`log_base_path'/`event'/`target'"
                        cap mkdir "`fig_base_path'/`event'"
                        cap mkdir "`log_base_path'/`event'"
                        cap mkdir "`event_fig_path'"
                        cap mkdir "`event_log_path'"

                        * -------- start log --------
                        log using "`event_log_path'/`event'_`target'_`event_type'_`control_fname'_`std'.log", text replace

                        local first = 1

                        foreach cohort of local cohort_list {

                            * -------- construct data file path --------
                            local data_file "`data_path'/`event_type'/`control_folder'/`event'_`panel_level'_cohort_`cohort'`suffix'_balanced.csv"

                            * -------- import data --------
                            import delimited "`data_file'", clear

                            * -------- standardization --------
                            if "`std'" == "standardize" {
                                bysort boardname product: egen temp = std(`target')
                                replace `target' = temp
                                drop temp
                            }
                            else if "`std'" == "normalize" {
                                * Quarter normalization now uses cohort-year Q1 only.
                                if "`panel_level'" == "quarter" {
                                    bysort boardname product: gen baseline = `target' if year == `cohort' & quarter == 1
                                }
                                else {
                                    bysort boardname product: gen baseline = `target' if year == `cohort'
                                }
                                bysort boardname product: egen baseline_value = max(baseline)
                                replace `target' = `target' / baseline_value
                                drop baseline baseline_value
                            }

                            * -------- generate event_cohort for type 'event' and data_cohort --------
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

                            * -------- generate event dummies --------
                            local event_anchor_year `cohort'

                            if "`panel_level'" == "year" {
                                gen rel_year = year - `event_anchor_year' if treated_in_stack == 1
                                gen pre_4  = treated_in_stack == 1 & rel_year == -4
                                gen pre_3  = treated_in_stack == 1 & rel_year == -3
                                gen pre_2  = treated_in_stack == 1 & rel_year == -2
                                gen pre_1  = treated_in_stack == 1 & rel_year == -1

                                gen post_0 = treated_in_stack == 1 & rel_year == 0
                                gen post_1 = treated_in_stack == 1 & rel_year == 1
                                gen post_2 = treated_in_stack == 1 & rel_year == 2
                                gen post_3 = treated_in_stack == 1 & rel_year == 3
                                drop rel_year
                            }
                            else {
                                local event_anchor_q = yq(`event_anchor_year', 1)
                                gen rel_quarter = yq(year, quarter) - `event_anchor_q' if treated_in_stack == 1
								
                                forvalues i = 1/4 {
                                    gen pre_`i' = treated_in_stack == 1 & rel_quarter == -`i'
                                }
                                forvalues i = 0/7 {
                                    gen post_`i' = treated_in_stack == 1 & rel_quarter == `i'
                                }
                                drop rel_quarter
                            }

                            gen data_cohort = `cohort'

                            * -------- append --------
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

                        * -------- generate id --------
                        egen id = group(boardname product data_cohort)

                        * -------- level-specific settings --------
                        if "`panel_level'" == "year" {
                            local timevar year
                            local gvar event_cohort
                            local twfe_terms pre_4 pre_3 pre_2 post_0 post_1 post_2 post_3
                            local coef_names pre_4 pre_3 pre_2 pre_1 post_0 post_1 post_2 post_3
                            local n_pre_nonref 3
                            local n_post 4
                            local trimlead 4
                            local trimlag 3
                            local xlabel_spec -4(1)3
                            local graph_width 2000
                            local perturb_step 0.1
                            local did_horizons 0/3
                            local did_pretrend 3
                        }
                        else {
                            gen q_time = yq(year, quarter)
                            format q_time %tq
                            gen event_cohort_q = yq(event_cohort, 1) if !missing(event_cohort)
							
							local twfe_terms ///
								pre_4 pre_3 pre_2 ///
								post_0 post_1 post_2 post_3 post_4 post_5 post_6 post_7
							local coef_names ///
								pre_4 pre_3 pre_2 pre_1 ///
								post_0 post_1 post_2 post_3 post_4 post_5 post_6 post_7 
							local n_pre_nonref 3
							local n_post 8
							local trimlead 4
							local trimlag 7
							local xlabel_spec -4(1)7
							local did_horizons 0/7
							local did_pretrend 2
                            local timevar q_time
                            local gvar event_cohort_q
                            local graph_width 3000
                            local perturb_step 0.15
                        }
						
                        local n_old = `n_pre_nonref' + `n_post'
                        local n_total = `n_pre_nonref' + 1 + `n_post'

                        * -------- prepare treatment indicator and atc3_sharing-aware vars --------
                        gen treated = !missing(event_cohort) & event_cohort != 0
                        gen event_cohort_did_imputation = `gvar'
                        replace event_cohort_did_imputation = . if event_cohort_did_imputation == 0
                        gen never_treated = missing(event_cohort_did_imputation)

                        * control-specific estimator switches
                        local run_csdid = ("`control'" != "not")
                        local run_esi = ("`control'" == "purecontrol")

                        * ============================================================
                        *  1. csdid
                        * ============================================================
                        if `run_csdid' {
                            tempvar gvar_csdid
                            gen `gvar_csdid' = `gvar'
                            replace `gvar_csdid' = 0 if missing(`gvar_csdid')

<<<<<<< Updated upstream
                            * --- csdid: atc3_sharing == 1 subsample (treated with sharing + all controls) ---
                            preserve
                            drop if treated == 1 & atc3_sharing == 0
                            csdid `target', ivar(id) time(`timevar') gvar(`gvar_csdid') agg(event) notyet method(dripw) long rseed(1)
                            est store did1_s1
                            restore

                            * --- csdid: atc3_sharing == 0 subsample (treated with no sharing + all controls) ---
                            preserve
                            drop if treated == 1 & atc3_sharing == 1
                            csdid `target', ivar(id) time(`timevar') gvar(`gvar_csdid') agg(event) notyet method(dripw) long rseed(1)
                            est store did1_s0
                            restore
=======
                            if `atc3sharing' == 1 {
                                * sharing mode: two treated-group-specific runs
                                preserve
                                drop if treated == 1 & atc3_sharing == 0
                                cap noi csdid `target', ivar(id) time(`timevar') gvar(`gvar_csdid') agg(event) notyet method(dripw) long rseed(1)
                                if _rc == 0 {
                                    est store did1_s1
                                }
                                else {
                                    di as text "csdid failed for atc3_sharing==1, skipping"
                                }
                                restore

                                preserve
                                drop if treated == 1 & atc3_sharing == 1
                                cap noi csdid `target', ivar(id) time(`timevar') gvar(`gvar_csdid') agg(event) notyet method(dripw) long rseed(1)
                                if _rc == 0 {
                                    est store did1_s0
                                }
                                else {
                                    di as text "csdid failed for atc3_sharing==0, skipping"
                                }
                                restore
                            }
                            else {
                                * non-sharing mode: single full-sample run
                                cap noi csdid `target', ivar(id) time(`timevar') gvar(`gvar_csdid') agg(event) notyet method(dripw) long rseed(1)
                                if _rc == 0 {
                                    est store did1_s0
                                }
                                else {
                                    di as text "csdid failed in non-sharing mode, skipping"
                                }
                            }
>>>>>>> Stashed changes
                        }
						


                        * ============================================================
                        *  2. did_imputation
                        * ============================================================
<<<<<<< Updated upstream
                        gen atc3_sharing_het = atc3_sharing if treated == 1
                        replace atc3_sharing_het = 0 if treated == 0
						
                        did_imputation `target' id `timevar' event_cohort_did_imputation, ///
                            fe(id `timevar') horizons(`did_horizons') pretrends(`did_pretrend') ///
                            hetby(atc3_sharing_het) autosample tol(0.1)
                        
                        est store did2
						matrix did2_b_full = e(b)
						matrix did2_V_full = e(V)

						* Identify column positions for each group
						local did2_ncol = colsof(did2_b_full)
						local did2_names : colnames did2_b_full

						foreach sval in 0 1 {
							matrix did2_b_s`sval' = J(1, `n_total', .)
							matrix did2_V_s`sval' = J(`n_total', `n_total', 0)

							tempname col_map
							matrix `col_map' = J(1, `n_total', 0)

							* Unified mapping: pre_K -> preK, post_h -> tauh_s
							forvalues i = 1/`n_total' {
								local col_name : word `i' of `coef_names'
								local cname ""

								if substr("`col_name'", 1, 4) == "pre_" {
									local k = substr("`col_name'", 5, .)
									local cname "pre`k'"
								}
								else if substr("`col_name'", 1, 5) == "post_" {
									local h = substr("`col_name'", 6, .)
									local cname "tau`h'_`sval'"
								}

								if "`cname'" != "" {
									forvalues c = 1/`did2_ncol' {
										local cn : word `c' of `did2_names'
										if "`cn'" == "`cname'" {
											matrix `col_map'[1, `i'] = `c'
											matrix did2_b_s`sval'[1, `i'] = did2_b_full[1, `c']
											continue, break
										}
									}
								}
							}

							forvalues i = 1/`n_total' {
								local ci = `col_map'[1, `i']
								if `ci' == 0 continue
								forvalues j = 1/`n_total' {
									local cj = `col_map'[1, `j']
									if `cj' == 0 continue
									matrix did2_V_s`sval'[`i', `j'] = did2_V_full[`ci', `cj']
								}
							}

							matrix colnames did2_b_s`sval' = `coef_names'
							matrix colnames did2_V_s`sval' = `coef_names'
							matrix rownames did2_V_s`sval' = `coef_names'
						}
                     

                        * ============================================================
                        *  3. TWFE — interaction terms
                        * ============================================================
                        * Generate interaction terms: event_dummy * atc3_sharing
                        foreach v of local twfe_terms {
                            gen `v'_x_atc3 = `v' * atc3_sharing
                        }

                        * Build interaction term list
                        local twfe_interact_terms ""
                        foreach v of local twfe_terms {
                            local twfe_interact_terms "`twfe_interact_terms' `v'_x_atc3"
                        }

                        reghdfe `target' `twfe_terms' `twfe_interact_terms', absorb(i.id i.`timevar')
                        matrix twfe_b_full = e(b)
                        matrix twfe_V_full = e(V)

                        * Extract TWFE coefficients for each group:
                        * For atc3_sharing==0: coefficient = beta on event_dummy alone
                        * For atc3_sharing==1: coefficient = beta on event_dummy + beta on interaction
                        * We build separate b and V matrices for each group.

                        foreach sval in 0 1 {
                            * Build b vector with pre_1=0 inserted
                            matrix twfe_b_s`sval' = J(1, `n_total', .)

                            forvalues i = 1/`n_pre_nonref' {
                                local base_coef = twfe_b_full[1, `i']
                                if `sval' == 1 {
                                    local inter_coef = twfe_b_full[1, `n_old' + `i']
                                    matrix twfe_b_s`sval'[1, `i'] = `base_coef' + `inter_coef'
                                }
                                else {
                                    matrix twfe_b_s`sval'[1, `i'] = `base_coef'
                                }
                            }
                            * pre_1 = 0 (reference)
                            matrix twfe_b_s`sval'[1, `=`n_pre_nonref' + 1'] = 0

                            forvalues i = `=`n_pre_nonref' + 1'/`n_old' {
                                local base_coef = twfe_b_full[1, `i']
                                if `sval' == 1 {
                                    local inter_coef = twfe_b_full[1, `n_old' + `i']
                                    matrix twfe_b_s`sval'[1, `i' + 1] = `base_coef' + `inter_coef'
                                }
                                else {
                                    matrix twfe_b_s`sval'[1, `i' + 1] = `base_coef'
                                }
                            }
                            matrix colnames twfe_b_s`sval' = `coef_names'

                            * Build V matrix with pre_1 row/col = 0
                            matrix twfe_V_s`sval' = J(`n_total', `n_total', 0)

                            if `sval' == 0 {
                                * For s=0: V is just the upper-left block of twfe_V_full
                                forvalues i = 1/`n_pre_nonref' {
                                    forvalues j = 1/`n_pre_nonref' {
                                        matrix twfe_V_s0[`i', `j'] = twfe_V_full[`i', `j']
                                    }
                                }
                                forvalues i = `=`n_pre_nonref' + 1'/`n_old' {
                                    forvalues j = `=`n_pre_nonref' + 1'/`n_old' {
                                        matrix twfe_V_s0[`i' + 1, `j' + 1] = twfe_V_full[`i', `j']
                                    }
                                }
                                forvalues i = 1/`n_pre_nonref' {
                                    forvalues j = `=`n_pre_nonref' + 1'/`n_old' {
                                        matrix twfe_V_s0[`i', `j' + 1] = twfe_V_full[`i', `j']
                                        matrix twfe_V_s0[`j' + 1, `i'] = twfe_V_full[`j', `i']
                                    }
                                }
=======
                        if `atc3sharing' == 1 {
                            gen atc3_sharing_het = atc3_sharing if treated == 1
                            replace atc3_sharing_het = 0 if treated == 0

                            cap noi did_imputation `target' id `timevar' event_cohort_did_imputation, ///
                                fe(id `timevar') horizons(`did_horizons') pretrends(`did_pretrend') ///
                                hetby(atc3_sharing_het) autosample tol(0.1)
                            local did2_ok = (_rc == 0)
                            if `did2_ok' {
                                est store did2
>>>>>>> Stashed changes
                            }
                            else {
                                di as text "did_imputation with hetby failed, skipping"
                            }
                        }
                        else {
                            cap noi did_imputation `target' id `timevar' event_cohort_did_imputation, ///
                                fe(id `timevar') horizons(`did_horizons') pretrends(`did_pretrend') ///
                                autosample tol(0.1)
                            local did2_ok = (_rc == 0)
                            if `did2_ok' {
                                est store did2
                            }
                            else {
                                di as text "did_imputation failed in non-sharing mode, skipping"
                            }
                        }

                        * ============================================================
                        *  3. TWFE
                        * ============================================================
                        if `atc3sharing' == 1 {
                            foreach v of local twfe_terms {
                                gen `v'_x_atc3 = `v' * atc3_sharing
                            }

                            local twfe_interact_terms ""
                            foreach v of local twfe_terms {
                                local twfe_interact_terms "`twfe_interact_terms' `v'_x_atc3"
                            }

                            reghdfe `target' `twfe_terms' `twfe_interact_terms', absorb(i.id i.`timevar')
                            matrix twfe_b_full = e(b)
                            matrix twfe_V_full = e(V)

                            foreach sval in 0 1 {
                                matrix twfe_b_s`sval' = J(1, `n_total', .)

                                forvalues i = 1/`n_pre_nonref' {
                                    local base_coef = twfe_b_full[1, `i']
                                    if `sval' == 1 {
                                        local inter_coef = twfe_b_full[1, `n_old' + `i']
                                        matrix twfe_b_s`sval'[1, `i'] = `base_coef' + `inter_coef'
                                    }
                                    else {
                                        matrix twfe_b_s`sval'[1, `i'] = `base_coef'
                                    }
                                }
                                matrix twfe_b_s`sval'[1, `=`n_pre_nonref' + 1'] = 0

                                forvalues i = `=`n_pre_nonref' + 1'/`n_old' {
                                    local base_coef = twfe_b_full[1, `i']
                                    if `sval' == 1 {
                                        local inter_coef = twfe_b_full[1, `n_old' + `i']
                                        matrix twfe_b_s`sval'[1, `i' + 1] = `base_coef' + `inter_coef'
                                    }
                                    else {
                                        matrix twfe_b_s`sval'[1, `i' + 1] = `base_coef'
                                    }
                                }
                                matrix colnames twfe_b_s`sval' = `coef_names'

                                matrix twfe_V_s`sval' = J(`n_total', `n_total', 0)

                                if `sval' == 0 {
                                    forvalues i = 1/`n_pre_nonref' {
                                        forvalues j = 1/`n_pre_nonref' {
                                            matrix twfe_V_s0[`i', `j'] = twfe_V_full[`i', `j']
                                        }
                                    }
                                    forvalues i = `=`n_pre_nonref' + 1'/`n_old' {
                                        forvalues j = `=`n_pre_nonref' + 1'/`n_old' {
                                            matrix twfe_V_s0[`i' + 1, `j' + 1] = twfe_V_full[`i', `j']
                                        }
                                    }
                                    forvalues i = 1/`n_pre_nonref' {
                                        forvalues j = `=`n_pre_nonref' + 1'/`n_old' {
                                            matrix twfe_V_s0[`i', `j' + 1] = twfe_V_full[`i', `j']
                                            matrix twfe_V_s0[`j' + 1, `i'] = twfe_V_full[`j', `i']
                                        }
                                    }
                                }
                                else {
                                    forvalues i = 1/`n_pre_nonref' {
                                        forvalues j = 1/`n_pre_nonref' {
                                            local vbb = twfe_V_full[`i', `j']
                                            local vgg = twfe_V_full[`n_old' + `i', `n_old' + `j']
                                            local vbg = twfe_V_full[`i', `n_old' + `j']
                                            local vgb = twfe_V_full[`n_old' + `i', `j']
                                            matrix twfe_V_s1[`i', `j'] = `vbb' + `vgg' + `vbg' + `vgb'
                                        }
                                    }
                                    forvalues i = `=`n_pre_nonref' + 1'/`n_old' {
                                        forvalues j = `=`n_pre_nonref' + 1'/`n_old' {
                                            local vbb = twfe_V_full[`i', `j']
                                            local vgg = twfe_V_full[`n_old' + `i', `n_old' + `j']
                                            local vbg = twfe_V_full[`i', `n_old' + `j']
                                            local vgb = twfe_V_full[`n_old' + `i', `j']
                                            matrix twfe_V_s1[`i' + 1, `j' + 1] = `vbb' + `vgg' + `vbg' + `vgb'
                                        }
                                    }
                                    forvalues i = 1/`n_pre_nonref' {
                                        forvalues j = `=`n_pre_nonref' + 1'/`n_old' {
                                            local vbb = twfe_V_full[`i', `j']
                                            local vgg = twfe_V_full[`n_old' + `i', `n_old' + `j']
                                            local vbg = twfe_V_full[`i', `n_old' + `j']
                                            local vgb = twfe_V_full[`n_old' + `i', `j']
                                            matrix twfe_V_s1[`i', `j' + 1] = `vbb' + `vgg' + `vbg' + `vgb'
                                            matrix twfe_V_s1[`j' + 1, `i'] = `vbb' + `vgg' + `vbg' + `vgb'
                                        }
                                    }
                                }
                                matrix colnames twfe_V_s`sval' = `coef_names'
                                matrix rownames twfe_V_s`sval' = `coef_names'
                            }
                        }
                        else {
                            reghdfe `target' `twfe_terms', absorb(i.id i.`timevar')
                            matrix twfe_b_full = e(b)
                            matrix twfe_V_full = e(V)

                            matrix twfe_b_s0 = J(1, `n_total', .)
                            forvalues i = 1/`n_pre_nonref' {
                                matrix twfe_b_s0[1, `i'] = twfe_b_full[1, `i']
                            }
                            matrix twfe_b_s0[1, `=`n_pre_nonref' + 1'] = 0
                            forvalues i = `=`n_pre_nonref' + 1'/`n_old' {
                                matrix twfe_b_s0[1, `i' + 1] = twfe_b_full[1, `i']
                            }
                            matrix colnames twfe_b_s0 = `coef_names'

                            matrix twfe_V_s0 = J(`n_total', `n_total', 0)
                            forvalues i = 1/`n_pre_nonref' {
                                forvalues j = 1/`n_pre_nonref' {
                                    matrix twfe_V_s0[`i', `j'] = twfe_V_full[`i', `j']
                                }
                            }
                            forvalues i = `=`n_pre_nonref' + 1'/`n_old' {
                                forvalues j = `=`n_pre_nonref' + 1'/`n_old' {
                                    matrix twfe_V_s0[`i' + 1, `j' + 1] = twfe_V_full[`i', `j']
                                }
                            }
                            forvalues i = 1/`n_pre_nonref' {
                                forvalues j = `=`n_pre_nonref' + 1'/`n_old' {
                                    matrix twfe_V_s0[`i', `j' + 1] = twfe_V_full[`i', `j']
                                    matrix twfe_V_s0[`j' + 1, `i'] = twfe_V_full[`j', `i']
                                }
                            }
                            matrix colnames twfe_V_s0 = `coef_names'
                            matrix rownames twfe_V_s0 = `coef_names'
                        }

                        * ============================================================
                        *  4. eventstudyinteract
                        * ============================================================
                        local controlcohort never_treated

                        if `run_esi' {
<<<<<<< Updated upstream
                            eventstudyinteract `target' `esi_vars_s0' `esi_vars_s1', ///
                                cohort(event_cohort_did_imputation) ///
                                control_cohort(`controlcohort') ///
                                absorb(i.id i.`timevar')

							matrix sa_b_all = e(b_iw)
							matrix sa_V_all = e(V_iw)

							* The first n_old columns correspond to _s0 vars,
							* the next n_old columns correspond to _s1 vars.
							foreach sval in 0 1 {
								local offset = `sval' * `n_old'

								matrix sa_b_s`sval' = J(1, `n_total', .)
								forvalues i = 1/`n_pre_nonref' {
									matrix sa_b_s`sval'[1, `i'] = sa_b_all[1, `offset' + `i']
								}
								matrix sa_b_s`sval'[1, `=`n_pre_nonref' + 1'] = 0
								forvalues i = `=`n_pre_nonref' + 1'/`n_old' {
									matrix sa_b_s`sval'[1, `i' + 1] = sa_b_all[1, `offset' + `i']
								}
								matrix colnames sa_b_s`sval' = `coef_names'

								matrix sa_V_s`sval' = J(`n_total', `n_total', 0)
								forvalues i = 1/`n_pre_nonref' {
									forvalues j = 1/`n_pre_nonref' {
										matrix sa_V_s`sval'[`i', `j'] = sa_V_all[`offset' + `i', `offset' + `j']
									}
								}
								forvalues i = `=`n_pre_nonref' + 1'/`n_old' {
									forvalues j = `=`n_pre_nonref' + 1'/`n_old' {
										matrix sa_V_s`sval'[`i' + 1, `j' + 1] = sa_V_all[`offset' + `i', `offset' + `j']
									}
								}
								forvalues i = 1/`n_pre_nonref' {
									forvalues j = `=`n_pre_nonref' + 1'/`n_old' {
										matrix sa_V_s`sval'[`i', `j' + 1] = sa_V_all[`offset' + `i', `offset' + `j']
										matrix sa_V_s`sval'[`j' + 1, `i'] = sa_V_all[`offset' + `j', `offset' + `i']
									}
								}
								matrix colnames sa_V_s`sval' = `coef_names'
								matrix rownames sa_V_s`sval' = `coef_names'
                                }
                            }
=======
                            if `atc3sharing' == 1 {
                                foreach v of local twfe_terms {
                                    gen `v'_s0 = `v' * (1 - atc3_sharing)
                                    gen `v'_s1 = `v' * atc3_sharing
                                }

                                local esi_vars_s0 ""
                                local esi_vars_s1 ""
                                foreach v of local twfe_terms {
                                    local esi_vars_s0 "`esi_vars_s0' `v'_s0"
                                    local esi_vars_s1 "`esi_vars_s1' `v'_s1"
                                }

                                cap noi eventstudyinteract `target' `esi_vars_s0' `esi_vars_s1', ///
                                    cohort(event_cohort_did_imputation) ///
                                    control_cohort(`controlcohort') ///
                                    absorb(i.id i.`timevar')
                                local esi_ok = (_rc == 0)

                                if `esi_ok' {
                                    matrix sa_b_all = e(b_iw)
                                    matrix sa_V_all = e(V_iw)

                                    foreach sval in 0 1 {
                                        local offset = `sval' * `n_old'

                                        matrix sa_b_s`sval' = J(1, `n_total', .)
                                        forvalues i = 1/`n_pre_nonref' {
                                            matrix sa_b_s`sval'[1, `i'] = sa_b_all[1, `offset' + `i']
                                        }
                                        matrix sa_b_s`sval'[1, `=`n_pre_nonref' + 1'] = 0
                                        forvalues i = `=`n_pre_nonref' + 1'/`n_old' {
                                            matrix sa_b_s`sval'[1, `i' + 1] = sa_b_all[1, `offset' + `i']
                                        }
                                        matrix colnames sa_b_s`sval' = `coef_names'

                                        matrix sa_V_s`sval' = J(`n_total', `n_total', 0)
                                        forvalues i = 1/`n_pre_nonref' {
                                            forvalues j = 1/`n_pre_nonref' {
                                                matrix sa_V_s`sval'[`i', `j'] = sa_V_all[`offset' + `i', `offset' + `j']
                                            }
                                        }
                                        forvalues i = `=`n_pre_nonref' + 1'/`n_old' {
                                            forvalues j = `=`n_pre_nonref' + 1'/`n_old' {
                                                matrix sa_V_s`sval'[`i' + 1, `j' + 1] = sa_V_all[`offset' + `i', `offset' + `j']
                                            }
                                        }
                                        forvalues i = 1/`n_pre_nonref' {
                                            forvalues j = `=`n_pre_nonref' + 1'/`n_old' {
                                                matrix sa_V_s`sval'[`i', `j' + 1] = sa_V_all[`offset' + `i', `offset' + `j']
                                                matrix sa_V_s`sval'[`j' + 1, `i'] = sa_V_all[`offset' + `j', `offset' + `i']
                                            }
                                        }
                                        matrix colnames sa_V_s`sval' = `coef_names'
                                        matrix rownames sa_V_s`sval' = `coef_names'
                                    }
                                }
                                else {
                                    di as text "eventstudyinteract failed, skipping"
                                }
                            }
                            else {
                                cap noi eventstudyinteract `target' `twfe_terms', ///
                                    cohort(event_cohort_did_imputation) ///
                                    control_cohort(`controlcohort') ///
                                    absorb(i.id i.`timevar')
                                local esi_ok = (_rc == 0)

                                if `esi_ok' {
                                    matrix sa_b_all = e(b_iw)
                                    matrix sa_V_all = e(V_iw)

                                    matrix sa_b_s0 = J(1, `n_total', .)
                                    forvalues i = 1/`n_pre_nonref' {
                                        matrix sa_b_s0[1, `i'] = sa_b_all[1, `i']
                                    }
                                    matrix sa_b_s0[1, `=`n_pre_nonref' + 1'] = 0
                                    forvalues i = `=`n_pre_nonref' + 1'/`n_old' {
                                        matrix sa_b_s0[1, `i' + 1] = sa_b_all[1, `i']
                                    }
                                    matrix colnames sa_b_s0 = `coef_names'

                                    matrix sa_V_s0 = J(`n_total', `n_total', 0)
                                    forvalues i = 1/`n_pre_nonref' {
                                        forvalues j = 1/`n_pre_nonref' {
                                            matrix sa_V_s0[`i', `j'] = sa_V_all[`i', `j']
                                        }
                                    }
                                    forvalues i = `=`n_pre_nonref' + 1'/`n_old' {
                                        forvalues j = `=`n_pre_nonref' + 1'/`n_old' {
                                            matrix sa_V_s0[`i' + 1, `j' + 1] = sa_V_all[`i', `j']
                                        }
                                    }
                                    forvalues i = 1/`n_pre_nonref' {
                                        forvalues j = `=`n_pre_nonref' + 1'/`n_old' {
                                            matrix sa_V_s0[`i', `j' + 1] = sa_V_all[`i', `j']
                                            matrix sa_V_s0[`j' + 1, `i'] = sa_V_all[`j', `i']
                                        }
                                    }
                                    matrix colnames sa_V_s0 = `coef_names'
                                    matrix rownames sa_V_s0 = `coef_names'
                                }
                                else {
                                    di as text "eventstudyinteract failed in non-sharing mode, skipping"
                                }
                            }
                        }
                        else {
                            local esi_ok = 0
                            di as text "control=`control', skipping eventstudyinteract"
                        }
>>>>>>> Stashed changes

                        * ============================================================
                        *  5. did_imputation — extract estimates
                        * ============================================================
                        if `did2_ok' {
                            est restore did2
                            matrix did2_b_full = e(b)
                            matrix did2_V_full = e(V)

                            local did2_ncol = colsof(did2_b_full)
                            local did2_names : colnames did2_b_full

                            if `atc3sharing' == 1 {
                                foreach sval in 0 1 {
                                    matrix did2_b_s`sval' = J(1, `n_total', .)
                                    matrix did2_V_s`sval' = J(`n_total', `n_total', 0)

                                    tempname col_map
                                    matrix `col_map' = J(1, `n_total', 0)

                                    forvalues i = 1/`n_total' {
                                        local col_name : word `i' of `coef_names'
                                        local cname ""

                                        if substr("`col_name'", 1, 4) == "pre_" {
                                            local k = substr("`col_name'", 5, .)
                                            local cname "pre`k'"
                                        }
                                        else if substr("`col_name'", 1, 5) == "post_" {
                                            local h = substr("`col_name'", 6, .)
                                            local cname "tau`h'_`sval'"
                                        }

                                        if "`cname'" != "" {
                                            forvalues c = 1/`did2_ncol' {
                                                local cn : word `c' of `did2_names'
                                                if "`cn'" == "`cname'" {
                                                    matrix `col_map'[1, `i'] = `c'
                                                    matrix did2_b_s`sval'[1, `i'] = did2_b_full[1, `c']
                                                    continue, break
                                                }
                                            }
                                        }
                                    }

                                    forvalues i = 1/`n_total' {
                                        local ci = `col_map'[1, `i']
                                        if `ci' == 0 continue
                                        forvalues j = 1/`n_total' {
                                            local cj = `col_map'[1, `j']
                                            if `cj' == 0 continue
                                            matrix did2_V_s`sval'[`i', `j'] = did2_V_full[`ci', `cj']
                                        }
                                    }

                                    matrix colnames did2_b_s`sval' = `coef_names'
                                    matrix colnames did2_V_s`sval' = `coef_names'
                                    matrix rownames did2_V_s`sval' = `coef_names'
                                }
                            }
                            else {
                                matrix did2_b_s0 = J(1, `n_total', .)
                                matrix did2_V_s0 = J(`n_total', `n_total', 0)

                                tempname col_map
                                matrix `col_map' = J(1, `n_total', 0)

                                forvalues i = 1/`n_total' {
                                    local col_name : word `i' of `coef_names'
                                    local cname ""

                                    if substr("`col_name'", 1, 4) == "pre_" {
                                        local k = substr("`col_name'", 5, .)
                                        local cname "pre`k'"
                                    }
                                    else if substr("`col_name'", 1, 5) == "post_" {
                                        local h = substr("`col_name'", 6, .)
                                        local cname "tau`h'"
                                    }

                                    if "`cname'" != "" {
                                        forvalues c = 1/`did2_ncol' {
                                            local cn : word `c' of `did2_names'
                                            if "`cn'" == "`cname'" {
                                                matrix `col_map'[1, `i'] = `c'
                                                matrix did2_b_s0[1, `i'] = did2_b_full[1, `c']
                                                continue, break
                                            }
                                        }
                                    }
                                }

                                forvalues i = 1/`n_total' {
                                    local ci = `col_map'[1, `i']
                                    if `ci' == 0 continue
                                    forvalues j = 1/`n_total' {
                                        local cj = `col_map'[1, `j']
                                        if `cj' == 0 continue
                                        matrix did2_V_s0[`i', `j'] = did2_V_full[`ci', `cj']
                                    }
                                }

                                matrix colnames did2_b_s0 = `coef_names'
                                matrix colnames did2_V_s0 = `coef_names'
                                matrix rownames did2_V_s0 = `coef_names'
                            }
                        }

                        * ============================================================
                        *  6. Plot
                        * ============================================================
                        local control_title = "`control'"
                        local plot_svals 0 1
                        if `atc3sharing' == 0 {
                            local plot_svals 0
                        }

                        foreach sval of local plot_svals {
                            local title_suffix ""
                            local file_suffix ""
                            if `atc3sharing' == 1 {
                                if `sval' == 1 {
                                    local title_suffix " (atc3sharing=1)"
                                }
                                else {
                                    local title_suffix " (atc3sharing=0)"
                                }
                                local file_suffix "_atc3sharing`sval'"
                            }

                            * Prepare all matrices with zero-fill for failed estimators

                            * --- csdid ---
                            cap est drop __plot_csdid
                            cap est restore did1_s`sval'
                            if _rc == 0 {
                                est store __plot_csdid
                            }
                            else {
                                if `run_csdid' {
                                    matrix csdid_b = J(1, `n_total', 0)
                                }
                                else {
                                    matrix csdid_b = J(1, `n_total', .)
                                }
                                matrix colnames csdid_b = `coef_names'
                                matrix csdid_V = J(`n_total', `n_total', .)
                                matrix colnames csdid_V = `coef_names'
                                matrix rownames csdid_V = `coef_names'
                            }

                            * --- did_imputation ---
                            if `did2_ok' {
                                matrix did2_b = did2_b_s`sval'
                                matrix did2_V = did2_V_s`sval'
                            }
                            else {
                                matrix did2_b = J(1, `n_total', 0)
                                matrix colnames did2_b = `coef_names'
                                matrix did2_V = J(`n_total', `n_total', .)
                                matrix colnames did2_V = `coef_names'
                                matrix rownames did2_V = `coef_names'
                            }

                            * --- eventstudyinteract ---
                            if `run_esi' & `esi_ok' {
                                matrix sa_b = sa_b_s`sval'
                                matrix sa_V = sa_V_s`sval'
                            }
                            else {
                                if `run_esi' {
                                    matrix sa_b = J(1, `n_total', 0)
                                }
                                else {
                                    matrix sa_b = J(1, `n_total', .)
                                }
                                matrix colnames sa_b = `coef_names'
                                matrix sa_V = J(`n_total', `n_total', .)
                                matrix colnames sa_V = `coef_names'
                                matrix rownames sa_V = `coef_names'
                            }

                            * --- twfe ---
                            matrix twfe_b = twfe_b_s`sval'
                            matrix twfe_V = twfe_V_s`sval'

                            local graph_title_size medium
                            * Plot based on enabled estimators only
                            if `run_csdid' {
                                cap est restore __plot_csdid
                                if `run_esi' {
                                    if _rc == 0 {
                                        event_plot __plot_csdid did2_b#did2_V sa_b#sa_V twfe_b#twfe_V, ///
                                            stub_lag(Tp# post_# post_# post_#) ///
                                            stub_lead(Tm# pre_# pre_# pre_#) ///
                                            trimlead(`trimlead') trimlag(`trimlag') ///
                                            plottype(scatter) ciplottype(rcap) ///
                                            together perturb(-0.325(`perturb_step')0.325) noautolegend ///
                                            graph_opt( ///
                                                title("`event_name' `event_type' `control_title' `std'`title_suffix'", size(`graph_title_size')) ///
                                                xtitle("Periods since the event", size(small)) ///
                                                ytitle("`target'", size(`graph_title_size')) ///
                                                xlabel(`xlabel_spec', nogrid) ///
                                                legend(order(1 "csdid" 3 "did imputation" 5 "event study interact" 7 "TWFE") ///
                                                       rows(1) position(6) region(style(none))) ///
                                                xline(-0.5, lcolor(gs8) lpattern(dash)) ///
                                                yline(0, lcolor(gs8)) ///
                                                graphregion(color(white)) bgcolor(white) ///
                                                ylabel(, angle(horizontal)) ///
                                            ) ///
                                            lag_opt1(msymbol(+) color(black)) lag_ci_opt1(color(black)) ///
                                            lag_opt2(msymbol(O) color(cranberry)) lag_ci_opt2(color(cranberry)) ///
                                            lag_opt3(msymbol(Th) color(navy)) lag_ci_opt3(color(navy)) ///
                                            lag_opt4(msymbol(O) color(green)) lag_ci_opt4(color(green))
                                    }
                                    else {
                                        event_plot csdid_b#csdid_V did2_b#did2_V sa_b#sa_V twfe_b#twfe_V, ///
                                            stub_lag(post_# post_# post_# post_#) ///
                                            stub_lead(pre_# pre_# pre_# pre_#) ///
                                            trimlead(`trimlead') trimlag(`trimlag') ///
                                            plottype(scatter) ciplottype(rcap) ///
                                            together perturb(-0.325(`perturb_step')0.325) noautolegend ///
                                            graph_opt( ///
                                                title("`event_name' `event_type' `control_title' `std'`title_suffix'", size(`graph_title_size')) ///
                                                xtitle("Periods since the event", size(small)) ///
                                                ytitle("`target'", size(`graph_title_size')) ///
                                                xlabel(`xlabel_spec', nogrid) ///
                                                legend(order(1 "csdid" 3 "did imputation" 5 "event study interact" 7 "TWFE") ///
                                                       rows(1) position(6) region(style(none))) ///
                                                xline(-0.5, lcolor(gs8) lpattern(dash)) ///
                                                yline(0, lcolor(gs8)) ///
                                                graphregion(color(white)) bgcolor(white) ///
                                                ylabel(, angle(horizontal)) ///
                                            ) ///
                                            lag_opt1(msymbol(+) color(black)) lag_ci_opt1(color(black)) ///
                                            lag_opt2(msymbol(O) color(cranberry)) lag_ci_opt2(color(cranberry)) ///
                                            lag_opt3(msymbol(Th) color(navy)) lag_ci_opt3(color(navy)) ///
                                            lag_opt4(msymbol(O) color(green)) lag_ci_opt4(color(green))
                                    }
                                }
                                else {
                                    if _rc == 0 {
                                        event_plot __plot_csdid did2_b#did2_V twfe_b#twfe_V, ///
                                            stub_lag(Tp# post_# post_#) ///
                                            stub_lead(Tm# pre_# pre_#) ///
                                            trimlead(`trimlead') trimlag(`trimlag') ///
                                            plottype(scatter) ciplottype(rcap) ///
                                            together perturb(-0.25(`perturb_step')0.25) noautolegend ///
                                            graph_opt( ///
                                                title("`event_name' `event_type' `control_title' `std'`title_suffix'", size(`graph_title_size')) ///
                                                xtitle("Periods since the event", size(small)) ///
                                                ytitle("`target'", size(`graph_title_size')) ///
                                                xlabel(`xlabel_spec', nogrid) ///
                                                legend(order(1 "csdid" 3 "did imputation" 5 "TWFE") ///
                                                       rows(1) position(6) region(style(none))) ///
                                                xline(-0.5, lcolor(gs8) lpattern(dash)) ///
                                                yline(0, lcolor(gs8)) ///
                                                graphregion(color(white)) bgcolor(white) ///
                                                ylabel(, angle(horizontal)) ///
                                            ) ///
                                            lag_opt1(msymbol(+) color(black)) lag_ci_opt1(color(black)) ///
                                            lag_opt2(msymbol(O) color(cranberry)) lag_ci_opt2(color(cranberry)) ///
                                            lag_opt3(msymbol(O) color(green)) lag_ci_opt3(color(green))
                                    }
                                    else {
                                        event_plot csdid_b#csdid_V did2_b#did2_V twfe_b#twfe_V, ///
                                            stub_lag(post_# post_# post_#) ///
                                            stub_lead(pre_# pre_# pre_#) ///
                                            trimlead(`trimlead') trimlag(`trimlag') ///
                                            plottype(scatter) ciplottype(rcap) ///
                                            together perturb(-0.25(`perturb_step')0.25) noautolegend ///
                                            graph_opt( ///
                                                title("`event_name' `event_type' `control_title' `std'`title_suffix'", size(`graph_title_size')) ///
                                                xtitle("Periods since the event", size(small)) ///
                                                ytitle("`target'", size(`graph_title_size')) ///
                                                xlabel(`xlabel_spec', nogrid) ///
                                                legend(order(1 "csdid" 3 "did imputation" 5 "TWFE") ///
                                                       rows(1) position(6) region(style(none))) ///
                                                xline(-0.5, lcolor(gs8) lpattern(dash)) ///
                                                yline(0, lcolor(gs8)) ///
                                                graphregion(color(white)) bgcolor(white) ///
                                                ylabel(, angle(horizontal)) ///
                                            ) ///
                                            lag_opt1(msymbol(+) color(black)) lag_ci_opt1(color(black)) ///
                                            lag_opt2(msymbol(O) color(cranberry)) lag_ci_opt2(color(cranberry)) ///
                                            lag_opt3(msymbol(O) color(green)) lag_ci_opt3(color(green))
                                    }
                                }
                            }
                            else if `run_esi' {
                                event_plot did2_b#did2_V sa_b#sa_V twfe_b#twfe_V, ///
                                    stub_lag(post_# post_# post_#) ///
                                    stub_lead(pre_# pre_# pre_#) ///
                                    trimlead(`trimlead') trimlag(`trimlag') ///
                                    plottype(scatter) ciplottype(rcap) ///
                                    together perturb(-0.25(`perturb_step')0.25) noautolegend ///
                                    graph_opt( ///
                                        title("`event_name' `event_type' `control_title' `std'`title_suffix'", size(`graph_title_size')) ///
                                        xtitle("Periods since the event", size(small)) ///
                                        ytitle("`target'", size(`graph_title_size')) ///
                                        xlabel(`xlabel_spec', nogrid) ///
                                        legend(order(1 "did imputation" 3 "event study interact" 5 "TWFE") ///
                                               rows(1) position(6) region(style(none))) ///
                                        xline(-0.5, lcolor(gs8) lpattern(dash)) ///
                                        yline(0, lcolor(gs8)) ///
                                        graphregion(color(white)) bgcolor(white) ///
                                        ylabel(, angle(horizontal)) ///
                                    ) ///
                                    lag_opt1(msymbol(O) color(cranberry)) lag_ci_opt1(color(cranberry)) ///
                                    lag_opt2(msymbol(Th) color(navy)) lag_ci_opt2(color(navy)) ///
                                    lag_opt3(msymbol(O) color(green)) lag_ci_opt3(color(green))
                            }
                            else {
                                event_plot did2_b#did2_V twfe_b#twfe_V, ///
                                    stub_lag(post_# post_#) ///
                                    stub_lead(pre_# pre_#) ///
                                    trimlead(`trimlead') trimlag(`trimlag') ///
                                    plottype(scatter) ciplottype(rcap) ///
                                    together perturb(-0.2(`perturb_step')0.2) noautolegend ///
                                    graph_opt( ///
                                        title("`event_name' `event_type' `control_title' `std'`title_suffix'", size(`graph_title_size')) ///
                                        xtitle("Periods since the event", size(small)) ///
                                        ytitle("`target'", size(`graph_title_size')) ///
                                        xlabel(`xlabel_spec', nogrid) ///
                                        legend(order(1 "did imputation" 3 "TWFE") ///
                                               rows(1) position(6) region(style(none))) ///
                                        xline(-0.5, lcolor(gs8) lpattern(dash)) ///
                                        yline(0, lcolor(gs8)) ///
                                        graphregion(color(white)) bgcolor(white) ///
                                        ylabel(, angle(horizontal)) ///
                                    ) ///
                                    lag_opt1(msymbol(O) color(cranberry)) lag_ci_opt1(color(cranberry)) ///
                                    lag_opt2(msymbol(O) color(green)) lag_ci_opt2(color(green))
                            }

                            * -------- save plot --------
                            local fname = "`event'_`target'_`event_type'_`control_fname'_`std'`file_suffix'.png"
                            graph export "`event_fig_path'/`fname'", replace width(`graph_width')

                            * Clean up temp estimate
                            cap est drop __plot_csdid
                        }

                        * Clean up stored estimates for this iteration
                        cap est drop did1_s0
                        cap est drop did1_s1
                        cap est drop did2

                        log close
                    }
                }
            }
        }
    }
}

clear all
