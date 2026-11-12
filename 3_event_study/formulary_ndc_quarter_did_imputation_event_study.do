// ================================================================
// Purpose:
// Estimate quarterly drug-level formulary event studies for included_share
// under three first-seen/exit sample restrictions.
//
// Process:
// 1. Read direction-specific NDC-company-quarter cohorts built with actual
//    A/B event quarters and stack all available cohorts for each event/side.
// 2. Keep the raw equal-formulary-weighted included_share without p95
//    winsorization and validate consecutive 11- or 12-quarter histories.
// 3. Run FE1 did_imputation models with NDC-company-cohort and calendar-quarter
//    fixed effects, same-direction other-event histories, ATC3-sharing
//    heterogeneity, and company-clustered standard errors.
// 4. Export dynamic estimates plus exact cohort, stacked, and autosample counts.
// 5. Reproduce the original Stata event_plot figures from those estimates.
//
// Input:
// - data/formulary_cohort_data_quarter/event/req1/Not/
//   shift_q1_first_seen_{0|1}_exit_{0|1}_event_q/{A|B}/
//   {event}_quarter_cohort_YYYYQX.csv
//
// Output:
// - csv/formulary_ndc/es/{sample}/req1_not_raw_fe1_firm/{dynamic,samples,cohort_samples}.csv
// - figures/formulary_ndc/es/{sample}/req1_not_raw_fe1_firm/
//   {event}/{side}/included_share/plot.png
// - logs/formulary_ndc/es/{sample}/req1_not_raw_fe1_firm/{event}/{side}/run.log
// ================================================================

version 18.0
clear all
set more off
set trace off

* ========================== USER CONFIG ==========================
* Six models per sample: three events x two treatment directions.
local events interlock_dissolution to_B_still_in_A to_B_not_in_A
local treatment_groups B A
local target included_share

* Maintained first_seen/exit samples. The unsupported 0/1 pair is omitted.
local sample_keys fs0e0 fs1e0 fs1e1
local first_seen_fs0e0 0
local exit_fs0e0 0
local first_seen_fs1e0 1
local exit_fs1e0 0
local first_seen_fs1e1 1
local exit_fs1e1 1

* Fixed data-build and regression design.
local formulary_time_shift_quarters 1
local req 1
local control not
local include_eventpair 0
local outlier_treatment none
local atc atc3
local fe_level 1
local cluster_level firm
local did_horizons 0/7
local did_pretrends 3
local autosample_tolerance 0.1
local autosample_minn 0
* ================================================================

if `req' != 1 | "`control'" != "not" | `include_eventpair' != 0 {
    di as error "This design requires req1, Not controls, and include_eventpair=0."
    exit 198
}
if "`outlier_treatment'" != "none" {
    di as error "included_share must remain raw; p95 winsorization is not allowed."
    exit 198
}
if `fe_level' != 1 | "`cluster_level'" != "firm" | "`atc'" != "atc3" {
    di as error "This design requires FE1, firm clustering, and ATC3 sharing."
    exit 198
}

* Run this do-file from codes/3_event_study.
local code_path "`c(pwd)'"
local parent_path = regexr("`code_path'", "[/\\][^/\\]+$", "")
local project_path = regexr("`parent_path'", "[/\\][^/\\]+$", "")
local project_path = subinstr("`project_path'", "\\", "/", .)

capture confirm file "`code_path'/formulary_ndc_quarter_did_imputation_event_study.do"
if _rc {
    di as error "Run this do-file with the working directory set to codes/3_event_study."
    exit 601
}

capture which did_imputation
if _rc {
    di as error "did_imputation is not installed."
    exit 199
}

local input_base "`project_path'/data/formulary_cohort_data_quarter/event/req1/Not"
local csv_base "`project_path'/csv/formulary_ndc/es"
local log_base "`project_path'/logs/formulary_ndc/es"
cap mkdir "`project_path'/csv"
cap mkdir "`project_path'/csv/formulary_ndc"
cap mkdir "`csv_base'"
cap mkdir "`project_path'/logs"
cap mkdir "`project_path'/logs/formulary_ndc"
cap mkdir "`log_base'"
local completion_manifest "`csv_base'/run_complete.txt"
capture erase "`completion_manifest'"

* Remove every prior result before any estimation begins, so a failed run
* cannot leave unvisited sample folders looking current.
foreach sample_key of local sample_keys {
    local first_seen = `first_seen_`sample_key''
    local exit_restriction = `exit_`sample_key''
    local sample_folder ///
        "shift_q`formulary_time_shift_quarters'_first_seen_`first_seen'_exit_`exit_restriction'_event_q"
    local spec_folder "req1_not_raw_fe`fe_level'_firm"
    local csv_root "`csv_base'/`sample_folder'/`spec_folder'"
    cap mkdir "`csv_base'/`sample_folder'"
    cap mkdir "`csv_root'"
    capture erase "`csv_root'/dynamic.csv"
    capture erase "`csv_root'/samples.csv"
    capture erase "`csv_root'/cohort_samples.csv"
}

foreach sample_key of local sample_keys {
    local first_seen = `first_seen_`sample_key''
    local exit_restriction = `exit_`sample_key''
    local sample_folder ///
        "shift_q`formulary_time_shift_quarters'_first_seen_`first_seen'_exit_`exit_restriction'_event_q"
    local input_root "`input_base'/`sample_folder'"
    local spec_folder "req1_not_raw_fe`fe_level'_firm"
    local csv_root "`csv_base'/`sample_folder'/`spec_folder'"
    local log_root "`log_base'/`sample_folder'/`spec_folder'"
    cap mkdir "`csv_base'/`sample_folder'"
    cap mkdir "`log_base'/`sample_folder'"
    cap mkdir "`csv_root'"
    cap mkdir "`log_root'"

    tempname coefficient_post sample_post cohort_post
    tempfile coefficient_data sample_data cohort_data
    postfile `coefficient_post' ///
        byte first_seen exit str32 event str1 direction str24 target ///
        byte sharing int rel_quarter double estimate std_error ci_low ci_high ///
        long autosample_n using `coefficient_data', replace
    postfile `sample_post' ///
        byte first_seen exit str32 event str1 direction str24 target ///
        long obs_total obs_control obs_treated obs_treated_notshare obs_treated_share ///
        long ids_total ids_control ids_treated ids_treated_notshare ids_treated_share ///
        long firms_total firms_control firms_treated ///
        double premean_notshare premean_share using `sample_data', replace
    postfile `cohort_post' ///
        byte first_seen exit str32 event str1 direction str24 target str12 scope ///
        str6 cohort long obs_total obs_control obs_treated ///
        obs_treated_notshare obs_treated_share ids_total ids_control ids_treated ///
        ids_treated_notshare ids_treated_share using `cohort_data', replace

    foreach event of local events {
        local other_event_list ""
        if "`event'" == "to_B_not_in_A" {
            local other_event_stem1 "event_to_b_still_in_a"
            local other_event_stem2 "event_interlock_dissolution"
            local event_folder "notinA"
        }
        else if "`event'" == "to_B_still_in_A" {
            local other_event_stem1 "event_to_b_not_in_a"
            local other_event_stem2 "event_interlock_dissolution"
            local event_folder "stillA"
        }
        else if "`event'" == "interlock_dissolution" {
            local other_event_stem1 "event_to_b_not_in_a"
            local other_event_stem2 "event_to_b_still_in_a"
            local event_folder "dissolve"
        }
        else {
            di as error "Unsupported event: `event'"
            exit 198
        }

        foreach treatment_group of local treatment_groups {
            local side = lower("`treatment_group'")
            local sample_var "sample_`side'"
            local treated_var "treated_`side'"
            local sharing_var "sharingatc3_`side'"
            local other_event_list ///
                "`other_event_stem1'_`side' `other_event_stem2'_`side'"
            local data_dir "`input_root'/`treatment_group'"
            local cohort_files : dir "`data_dir'" files "`event'_quarter_cohort_*.csv"
            if `"`cohort_files'"' == "" {
                di as error "No cohort files found in `data_dir' for `event'."
                exit 601
            }

            cap mkdir "`log_root'/`event_folder'"
            cap mkdir "`log_root'/`event_folder'/`treatment_group'"
            capture log close
            log using "`log_root'/`event_folder'/`treatment_group'/run.log", replace text
            di as text "Sample `sample_folder': `event', side `treatment_group', target `target'"

            local first_file 1
            local cohort_count 0
            foreach cohort_file of local cohort_files {
                local data_file "`data_dir'/`cohort_file'"
                import delimited "`data_file'", clear varnames(1) case(lower) ///
                    stringcols(1 2 3 9)

                foreach required in ndc boardname year quarter ///
                    data_cohort_year data_cohort_quarter data_cohort_qtime ///
                    included_share `sample_var' `treated_var' `sharing_var' ///
                    `other_event_list' {
                    capture confirm variable `required'
                    if _rc {
                        di as error "Missing `required' in `data_file'."
                        log close
                        exit 111
                    }
                }
                rename boardname companyid
                assert !missing(ndc) & !missing(companyid)
                assert inrange(included_share, 0, 1) & !missing(included_share)
                assert `sample_var' == 1
                assert inlist(`treated_var', 0, 1)
                assert inlist(`sharing_var', 0, 1)
                assert `sharing_var' == 0 if `treated_var' == 0
                assert data_cohort_qtime == ///
                    data_cohort_year * 4 + data_cohort_quarter

                gen int q_time = yq(year, quarter)
                format q_time %tq
                gen int event_anchor_q = yq(data_cohort_year, data_cohort_quarter)
                format event_anchor_q %tq
                gen int rel_quarter = q_time - event_anchor_q
                assert inrange(rel_quarter, -4, 7)
                gen byte treated = `treated_var'
                gen byte atc_sharing = `sharing_var'
                quietly count if treated == 1
                if r(N) == 0 {
                    di as error "Cohort file has no treated observations: `data_file'"
                    log close
                    exit 459
                }

                quietly summarize data_cohort_year, meanonly
                local cohort_year = r(min)
                assert data_cohort_year == `cohort_year'
                quietly summarize data_cohort_quarter, meanonly
                local cohort_quarter = r(min)
                assert data_cohort_quarter == `cohort_quarter'
                local cohort_tag "`cohort_year'Q`cohort_quarter'"

                egen long cohort_id = group(companyid ndc data_cohort_qtime)
                isid cohort_id q_time
                tempvar id_tag control_tag treated_tag notshare_tag share_tag
                egen byte `id_tag' = tag(cohort_id)
                egen byte `control_tag' = tag(cohort_id) if treated == 0
                egen byte `treated_tag' = tag(cohort_id) if treated == 1
                egen byte `notshare_tag' = tag(cohort_id) if treated == 1 & atc_sharing == 0
                egen byte `share_tag' = tag(cohort_id) if treated == 1 & atc_sharing == 1
                quietly count
                local c_obs_total = r(N)
                quietly count if treated == 0
                local c_obs_control = r(N)
                quietly count if treated == 1
                local c_obs_treated = r(N)
                quietly count if treated == 1 & atc_sharing == 0
                local c_obs_notshare = r(N)
                quietly count if treated == 1 & atc_sharing == 1
                local c_obs_share = r(N)
                quietly count if `id_tag' == 1
                local c_ids_total = r(N)
                quietly count if `control_tag' == 1
                local c_ids_control = r(N)
                quietly count if `treated_tag' == 1
                local c_ids_treated = r(N)
                quietly count if `notshare_tag' == 1
                local c_ids_notshare = r(N)
                quietly count if `share_tag' == 1
                local c_ids_share = r(N)
                post `cohort_post' ///
                    (`first_seen') (`exit_restriction') ("`event'") ///
                    ("`treatment_group'") ("`target'") ("cohort") ///
                    ("`cohort_tag'") (`c_obs_total') (`c_obs_control') ///
                    (`c_obs_treated') (`c_obs_notshare') (`c_obs_share') ///
                    (`c_ids_total') (`c_ids_control') (`c_ids_treated') ///
                    (`c_ids_notshare') (`c_ids_share')
                drop cohort_id `id_tag' `control_tag' `treated_tag' ///
                    `notshare_tag' `share_tag'

                keep ndc companyid year quarter q_time event_anchor_q rel_quarter ///
                    data_cohort_year data_cohort_quarter data_cohort_qtime ///
                    included_share treated atc_sharing `other_event_list'
                tempfile cohort_part
                save `cohort_part', replace
                if `first_file' == 1 {
                    tempfile event_master
                    save `event_master', replace
                    local first_file 0
                }
                else {
                    append using `event_master'
                    save `event_master', replace
                }
                local ++cohort_count
            }

            if `cohort_count' == 0 {
                di as error "No usable cohorts for `event' side `treatment_group'."
                log close
                exit 459
            }
            use `event_master', clear
            egen long id = group(companyid ndc data_cohort_qtime)
            isid id q_time
            bysort id (q_time): assert treated == treated[1]
            bysort id (q_time): assert atc_sharing == atc_sharing[1]
            bysort id (q_time): assert event_anchor_q == event_anchor_q[1]
            bysort data_cohort_qtime: egen int window_start = min(q_time)
            bysort data_cohort_qtime: egen int window_end = max(q_time)
            assert window_start >= event_anchor_q - 4
            assert window_end <= event_anchor_q + 7
            gen byte window_periods = window_end - window_start + 1
            assert inlist(window_periods, 11, 12)
            bysort id (q_time): assert _N == window_end[1] - window_start[1] + 1
            bysort id (q_time): assert q_time == window_start[1] + _n - 1
            drop window_start window_end window_periods

            local other_history_list ""
            foreach other_event of local other_event_list {
                tempvar first_other_q other_history
                bysort companyid data_cohort_qtime: egen `first_other_q' = ///
                    min(cond(`other_event' == 1, q_time, .))
                gen byte `other_history' = ///
                    !missing(`first_other_q') & q_time >= `first_other_q'
                drop `first_other_q'
                quietly summarize `other_history', meanonly
                if r(max) > 0 {
                    local other_history_list "`other_history_list' `other_history'"
                }
            }

            gen int event_cohort_q = event_anchor_q if treated == 1
            format event_cohort_q %tq
            gen byte pre_period = q_time < event_anchor_q
            gen byte atc_sharing_het = atc_sharing if treated == 1
            replace atc_sharing_het = 0 if treated == 0
            local fe_spec "id q_time `other_history_list'"

            * Record the complete stacked sample before did_imputation autosample.
            tempvar stack_id_tag stack_control_tag stack_treated_tag ///
                stack_notshare_tag stack_share_tag
            egen byte `stack_id_tag' = tag(id)
            egen byte `stack_control_tag' = tag(id) if treated == 0
            egen byte `stack_treated_tag' = tag(id) if treated == 1
            egen byte `stack_notshare_tag' = tag(id) if ///
                treated == 1 & atc_sharing == 0
            egen byte `stack_share_tag' = tag(id) if ///
                treated == 1 & atc_sharing == 1
            quietly count
            local stack_obs_total = r(N)
            quietly count if treated == 0
            local stack_obs_control = r(N)
            quietly count if treated == 1
            local stack_obs_treated = r(N)
            quietly count if treated == 1 & atc_sharing == 0
            local stack_obs_notshare = r(N)
            quietly count if treated == 1 & atc_sharing == 1
            local stack_obs_share = r(N)
            quietly count if `stack_id_tag' == 1
            local stack_ids_total = r(N)
            quietly count if `stack_control_tag' == 1
            local stack_ids_control = r(N)
            quietly count if `stack_treated_tag' == 1
            local stack_ids_treated = r(N)
            quietly count if `stack_notshare_tag' == 1
            local stack_ids_notshare = r(N)
            quietly count if `stack_share_tag' == 1
            local stack_ids_share = r(N)
            post `cohort_post' ///
                (`first_seen') (`exit_restriction') ("`event'") ///
                ("`treatment_group'") ("`target'") ("stack_total") ("ALL") ///
                (`stack_obs_total') (`stack_obs_control') (`stack_obs_treated') ///
                (`stack_obs_notshare') (`stack_obs_share') ///
                (`stack_ids_total') (`stack_ids_control') (`stack_ids_treated') ///
                (`stack_ids_notshare') (`stack_ids_share')
            drop `stack_id_tag' `stack_control_tag' `stack_treated_tag' ///
                `stack_notshare_tag' `stack_share_tag'

            * Raw included_share is used directly: no trimming or winsorization.
            capture noisily did_imputation included_share id q_time event_cohort_q, ///
                fe(`fe_spec') horizons(`did_horizons') pretrends(`did_pretrends') ///
                hetby(atc_sharing_het) autosample tol(`autosample_tolerance') ///
                minn(`autosample_minn') cluster(companyid)
            if _rc {
                local did_rc = _rc
                di as error "did_imputation failed with r(`did_rc')."
                log close
                exit `did_rc'
            }

            local missing_coefficients ""
            forvalues pre = 1/`did_pretrends' {
                capture scalar __b = _b[pre`pre']
                local b_rc = _rc
                capture scalar __se = _se[pre`pre']
                local se_rc = _rc
                if `b_rc' | `se_rc' | missing(__b) | missing(__se) {
                    local missing_coefficients "`missing_coefficients' pre`pre'"
                }
            }
            forvalues horizon = 0/7 {
                foreach sharing in 0 1 {
                    capture scalar __b = _b[tau`horizon'_`sharing']
                    local b_rc = _rc
                    capture scalar __se = _se[tau`horizon'_`sharing']
                    local se_rc = _rc
                    if `b_rc' | `se_rc' | missing(__b) | missing(__se) {
                        local missing_coefficients ///
                            "`missing_coefficients' tau`horizon'_`sharing'"
                    }
                }
            }
            if "`missing_coefficients'" != "" {
                di as error "Unavailable estimates:`missing_coefficients'"
                log close
                exit 459
            }

            tempvar did_sample id_tag control_tag treated_tag notshare_tag share_tag
            gen byte `did_sample' = e(sample)
            egen byte `id_tag' = tag(id) if `did_sample'
            egen byte `control_tag' = tag(id) if `did_sample' & treated == 0
            egen byte `treated_tag' = tag(id) if `did_sample' & treated == 1
            egen byte `notshare_tag' = tag(id) if ///
                `did_sample' & treated == 1 & atc_sharing == 0
            egen byte `share_tag' = tag(id) if ///
                `did_sample' & treated == 1 & atc_sharing == 1
            quietly count if `did_sample'
            local obs_total = r(N)
            quietly count if `did_sample' & treated == 0
            local obs_control = r(N)
            quietly count if `did_sample' & treated == 1
            local obs_treated = r(N)
            quietly count if `did_sample' & treated == 1 & atc_sharing == 0
            local obs_notshare = r(N)
            quietly count if `did_sample' & treated == 1 & atc_sharing == 1
            local obs_share = r(N)
            quietly count if `id_tag' == 1
            local ids_total = r(N)
            quietly count if `control_tag' == 1
            local ids_control = r(N)
            quietly count if `treated_tag' == 1
            local ids_treated = r(N)
            quietly count if `notshare_tag' == 1
            local ids_notshare = r(N)
            quietly count if `share_tag' == 1
            local ids_share = r(N)
            egen byte firm_tag = tag(companyid) if `did_sample'
            quietly count if firm_tag == 1
            local firms_total = r(N)
            drop firm_tag
            egen byte firm_tag = tag(companyid) if `did_sample' & treated == 0
            quietly count if firm_tag == 1
            local firms_control = r(N)
            drop firm_tag
            egen byte firm_tag = tag(companyid) if `did_sample' & treated == 1
            quietly count if firm_tag == 1
            local firms_treated = r(N)
            quietly summarize included_share if ///
                `did_sample' & treated == 1 & atc_sharing == 0 & pre_period, meanonly
            local premean_notshare = r(mean)
            quietly summarize included_share if ///
                `did_sample' & treated == 1 & atc_sharing == 1 & pre_period, meanonly
            local premean_share = r(mean)
            post `sample_post' ///
                (`first_seen') (`exit_restriction') ("`event'") ///
                ("`treatment_group'") ("`target'") (`obs_total') ///
                (`obs_control') (`obs_treated') (`obs_notshare') (`obs_share') ///
                (`ids_total') (`ids_control') (`ids_treated') ///
                (`ids_notshare') (`ids_share') (`firms_total') ///
                (`firms_control') (`firms_treated') ///
                (`premean_notshare') (`premean_share')
            forvalues pre = 1/`did_pretrends' {
                scalar __b = _b[pre`pre']
                scalar __se = _se[pre`pre']
                scalar __lo = __b - invnormal(0.975) * __se
                scalar __hi = __b + invnormal(0.975) * __se
                post `coefficient_post' ///
                    (`first_seen') (`exit_restriction') ("`event'") ///
                    ("`treatment_group'") ("`target'") (0) (-`pre') ///
                    (__b) (__se) (__lo) (__hi) (`obs_total')
            }
            forvalues horizon = 0/7 {
                foreach sharing in 0 1 {
                    scalar __b = _b[tau`horizon'_`sharing']
                    scalar __se = _se[tau`horizon'_`sharing']
                    scalar __lo = __b - invnormal(0.975) * __se
                    scalar __hi = __b + invnormal(0.975) * __se
                    post `coefficient_post' ///
                        (`first_seen') (`exit_restriction') ("`event'") ///
                        ("`treatment_group'") ("`target'") (`sharing') ///
                        (`horizon') (__b) (__se) (__lo) (__hi) (`obs_total')
                }
            }
            log close
        }
    }

    postclose `coefficient_post'
    postclose `sample_post'
    postclose `cohort_post'

    use `coefficient_data', clear
    assert _N == 6 * (`did_pretrends' + 2 * 8)
    sort event direction target sharing rel_quarter
    export delimited using "`csv_root'/dynamic.csv", replace

    use `sample_data', clear
    assert _N == 6
    sort event direction target
    export delimited using "`csv_root'/samples.csv", replace

    use `cohort_data', clear
    sort event direction target scope cohort
    export delimited using "`csv_root'/cohort_samples.csv", replace
    di as result "Saved six formulary NDC models for `sample_folder'."
}

* A plotting failure prevents the regression run from being marked complete.
do "formulary_ndc_quarter_event_study_plots.do"

tempname completion_handle
file open `completion_handle' using "`completion_manifest'", write text replace
file write `completion_handle' ///
    "COMPLETE 18 models: 3 samples x 3 events x 2 directions" _n
file close `completion_handle'
di as result "Completed all 18 formulary NDC included_share models."
