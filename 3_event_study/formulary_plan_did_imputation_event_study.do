version 18.0
clear all
set more off
set trace off

// ================================================================
// Purpose:
// Estimate direction-specific contract-plan-drug did_imputation event studies
// with ATC3-sharing heterogeneity at the plan, plan-state, or plan-county
// level while reproducing the established formulary figure format.
//
// Process:
// 1. Read each event's five plan cohort CSVs once, merge shift-specific NDC
//    first-seen metadata, and apply the selected cohort-relative cutoff.
// 2. Select A or B through sample_a/b and freeze event-specific ATC3 sharing
//    at cohort-year Q1 for the full stacked unit history.
// 3. Build level-specific identifiers; winsorize only average copay at p95.
// 4. Run did_imputation with hetby(ATC3 sharing), firm clustering, horizons
//    0/7, three pretrend coefficients, and FE1 or plan-quarter FE2.
// 5. Export dynamic coefficients, autosample statistics, logs, and figures.
//
// Input:
// - data/formulary_plan_cohort_data/event/req1/Not/
//   shift_q{0|1}/{plan|state|county}/
//   {event}_plan_quarter_cohort_{year}.csv
// - data/formulary_metadata/ndc_first_seen.csv or
//   data/formulary_metadata/ndc_first_seen_shift_q1.csv
//
// Output:
// - csv/formulary_plan/es/{sample}/{spec}/dynamic.csv
// - csv/formulary_plan/es/{sample}/{spec}/samples.csv
// - figures/formulary_plan/es/{sample}/{spec}/{event}/{side}/{target}/plot.png
// - logs/formulary_plan/es/{sample}/{spec}/{event}/{side}/{target}/run.log
// ================================================================

* ================= user config =================
local events to_B_not_in_A interlock_dissolution to_B_still_in_A
local treatment_groups B
local targets tier_upgrade tier_downgrade avg_copay_amt prefer
local outlier_treatment winsorize
local outlier_percentile p95
local req 1
local control not
local include_eventpair 0
local personnel_definition narrow
local large_sample 1
local analysis_level plan
local formulary_time_shift_quarters 1
local first_seen_year_offset -1
local first_seen_quarter 1
local atc atc3
local fe_level 1
local cluster_level firm
local did_horizons 0/7
local did_pretrends 3
local coef_names pre_3 pre_2 pre_1 post_0 post_1 post_2 post_3 post_4 post_5 post_6 post_7
local n_total 11
local trimlead 3
local trimlag 7
local xlabel_spec -3(1)7
local graph_width 3000
local perturb_step 0.15
local perturb_span 0.30

if `large_sample' != 1 | "`personnel_definition'" != "narrow" {
    di as error "Formulary regressions require large_sample=1 and personnel_definition=narrow."
    exit 198
}
if `req' != 1 | "`control'" != "not" | `include_eventpair' != 0 {
    di as error "Formulary regressions require req1, Not controls, and include_eventpair=0."
    exit 198
}
if !inlist("`analysis_level'", "plan", "state", "county") {
    di as error "analysis_level must be plan, state, or county."
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

* ================= paths =================
local code_path "`c(pwd)'"
local parent_path = regexr("`code_path'", "[/\\][^/\\]+$", "")
local project_path = regexr("`parent_path'", "[/\\][^/\\]+$", "")
local project_path = subinstr("`project_path'", "\", "/", .)

local data_path "`project_path'/data/formulary_plan_cohort_data/event/req1/Not/shift_q`formulary_time_shift_quarters'/`analysis_level'"
local panel_suffix "_`analysis_level'"
local panel_label "`analysis_level'_ndc"
local import_stringcols "1/11 20 21"
local cell_required_vars ""
local plan_components "contract_id plan_id segment_id"
if "`analysis_level'" == "state" {
    local import_stringcols "1/12 21 22"
    local cell_required_vars "state"
    local plan_components "contract_id plan_id segment_id state"
}
else if "`analysis_level'" == "county" {
    local import_stringcols "1/15 24 25"
    local cell_required_vars "state county_code ma_region_code pdp_region_code"
    local plan_components "contract_id plan_id segment_id county_code"
}
local id_components "`plan_components' ndc data_cohort"
local sample_spec "baseline"
if `formulary_time_shift_quarters' != 0 | `first_seen_year_offset' != 0 | `first_seen_quarter' != 1 {
    local seen_offset "`first_seen_year_offset'"
    if `first_seen_year_offset' >= 0 {
        local seen_offset "+`first_seen_year_offset'"
    }
    local sample_spec "shift_q`formulary_time_shift_quarters'_seen_y`seen_offset'_q`first_seen_quarter'"
}
local spec_folder "req1_not_wsp95_fe`fe_level'_firm`panel_suffix'"
local csv_root "`project_path'/csv/formulary_plan/es"
local figure_root "`project_path'/figures/formulary_plan/es"
local log_root "`project_path'/logs/formulary_plan/es"
local csv_run_root "`csv_root'/`sample_spec'/`spec_folder'"
local figure_run_root "`figure_root'/`sample_spec'/`spec_folder'"
local log_run_root "`log_root'/`sample_spec'/`spec_folder'"
cap mkdir "`project_path'/csv"
cap mkdir "`project_path'/csv/formulary_plan"
cap mkdir "`project_path'/figures"
cap mkdir "`project_path'/figures/formulary_plan"
cap mkdir "`project_path'/logs"
cap mkdir "`project_path'/logs/formulary_plan"
cap mkdir "`csv_root'"
cap mkdir "`figure_root'"
cap mkdir "`log_root'"
cap mkdir "`csv_root'/`sample_spec'"
cap mkdir "`figure_root'/`sample_spec'"
cap mkdir "`log_root'/`sample_spec'"
cap mkdir "`csv_run_root'"
cap mkdir "`figure_run_root'"
cap mkdir "`log_run_root'"

capture which did_imputation
if _rc {
    di as error "did_imputation is not installed. Install it before running this do-file."
    exit 199
}
capture which event_plot
if _rc {
    di as error "event_plot is not installed. Install it before running this do-file."
    exit 199
}

* Load the shift-consistent global NDC first-seen metadata once.  Eligible
* NDCs retain their complete plan-cohort histories after the cutoff is applied.
local first_seen_file "`project_path'/data/formulary_metadata/ndc_first_seen.csv"
if `formulary_time_shift_quarters' == 1 {
    local first_seen_file ///
        "`project_path'/data/formulary_metadata/ndc_first_seen_shift_q1.csv"
}
capture confirm file "`first_seen_file'"
if _rc {
    di as error "Missing first-seen metadata: `first_seen_file'"
    exit 601
}
import delimited "`first_seen_file'", clear varnames(1) case(lower) ///
    stringcols(1 2)
foreach required in ndc first_seen_qtime {
    capture confirm variable `required'
    if _rc {
        di as error "Missing variable `required' in `first_seen_file'"
        exit 111
    }
}
capture confirm numeric variable first_seen_qtime
if _rc {
    destring first_seen_qtime, replace
}
assert !missing(ndc) & !missing(first_seen_qtime)
isid ndc
keep ndc first_seen_qtime
tempfile first_seen_metadata
save `first_seen_metadata', replace

local panel_event_vars ///
    event_to_b_not_in_a_a event_to_b_not_in_a_b ///
    event_to_b_still_in_a_a event_to_b_still_in_a_b ///
    event_interlock_dissolution_a event_interlock_dissolution_b

tempname coefficient_post sample_post
tempfile coefficient_data sample_data
postfile `coefficient_post' ///
    str32 event str1 direction str24 target byte sharing ///
    int rel_quarter double estimate std_error ci_low ci_high ///
    long autosample_n using `coefficient_data', replace
postfile `sample_post' ///
    str32 event str1 direction str24 target ///
    long obs_control obs_treated_notshare obs_treated_share ///
    long ids_control ids_treated_notshare ids_treated_share ///
    long firms_control firms_treated_notshare firms_treated_share ///
    double premean_notshare premean_share ///
    using `sample_data', replace

* ================= estimation loop =================
foreach event of local events {
    local cohort_list "2020 2021 2022 2023 2024"
    local event_lower = lower("`event'")
    if "`event'" == "to_B_not_in_A" {
        local imported_share_a "event_to_b_not_in_a_a_sharingatc"
        local imported_share_b "event_to_b_not_in_a_b_sharingatc"
    }
    else if "`event'" == "to_B_still_in_A" {
        local imported_share_a "event_to_b_still_in_a_a_sharinga"
        local imported_share_b "event_to_b_still_in_a_b_sharinga"
    }
    else if "`event'" == "interlock_dissolution" {
        local imported_share_a "event_interlock_dissolution_a_sh"
        local imported_share_b "event_interlock_dissolution_b_sh"
    }

    * Cache the five first-seen-filtered cohort files once per event.  Every
    * direction and outcome below reuses this temporary Stata dataset.
    local first 1
    foreach cohort of local cohort_list {
        local data_file ///
            "`data_path'/`event'_plan_quarter_cohort_`cohort'.csv"
        capture confirm file "`data_file'"
        if _rc {
            di as error "Missing cohort file: `data_file'"
            exit 601
        }

        import delimited "`data_file'", clear varnames(1) case(lower) ///
            stringcols(`import_stringcols')
        foreach required in contract_id plan_id segment_id ndc boardname ///
            year quarter data_cohort `cell_required_vars' ///
            treated_a treated_b sample_a sample_b ///
            `panel_event_vars' `imported_share_a' `imported_share_b' ///
            `targets' {
            capture confirm variable `required'
            if _rc {
                di as error "Missing variable `required' in `data_file'"
                exit 111
            }
        }
        rename `imported_share_a' cohort_sharing_a
        rename `imported_share_b' cohort_sharing_b
        foreach numeric_var in year quarter data_cohort ///
            treated_a treated_b sample_a sample_b `panel_event_vars' ///
            cohort_sharing_a cohort_sharing_b `targets' {
            capture confirm numeric variable `numeric_var'
            if _rc {
                destring `numeric_var', replace
            }
        }
        foreach binary_var in treated_a treated_b sample_a sample_b ///
            `panel_event_vars' cohort_sharing_a cohort_sharing_b ///
            tier_upgrade tier_downgrade {
            assert inlist(`binary_var', 0, 1)
        }
        assert inlist(prefer, 0, 1) if !missing(prefer)
        assert data_cohort == `cohort'
        keep contract_id plan_id segment_id `cell_required_vars' ///
            ndc boardname year quarter data_cohort ///
            treated_a treated_b sample_a sample_b `panel_event_vars' ///
            cohort_sharing_a cohort_sharing_b `targets'

        if `first' == 1 {
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

    foreach treatment_group of local treatment_groups {
        local side = lower("`treatment_group'")
        local sample_var "sample_`side'"
        local treated_var "treated_`side'"
        local sharing_source "cohort_sharing_`side'"

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

        local event_folder "`event'"
        if "`event'" == "to_B_not_in_A" {
            local event_folder "notinA"
        }
        else if "`event'" == "to_B_still_in_A" {
            local event_folder "stillA"
        }
        else if "`event'" == "interlock_dissolution" {
            local event_folder "dissolve"
        }

        foreach target of local targets {
            local figure_dir "`figure_run_root'/`event_folder'/`treatment_group'/`target'"
            local log_dir "`log_run_root'/`event_folder'/`treatment_group'/`target'"
            cap mkdir "`figure_run_root'/`event_folder'"
            cap mkdir "`log_run_root'/`event_folder'"
            cap mkdir "`figure_run_root'/`event_folder'/`treatment_group'"
            cap mkdir "`log_run_root'/`event_folder'/`treatment_group'"
            cap mkdir "`figure_dir'"
            cap mkdir "`log_dir'"
            capture erase "`figure_dir'/plot.png"
            capture log close
            log using "`log_dir'/run.log", replace text
            di as text "Formulary dynamic did_imputation: `event', side `treatment_group', target `target', level `panel_label'"

            use `event_master', clear
            keep if `sample_var' == 1
            gen byte treated_in_stack = `treated_var'
            count
            if r(N) == 0 {
                di as error "No observations remain after direction-specific sample filtering."
                log close
                continue
            }

            * Freeze the event-specific sharing classification at cohort Q1,
            * exactly as in the existing formulary cohort construction.
            tempvar q1_share_min q1_share_max
            bysort ndc boardname data_cohort: egen byte `q1_share_min' = ///
                min(cond(year == data_cohort & quarter == 1, `sharing_source', .))
            bysort ndc boardname data_cohort: egen byte `q1_share_max' = ///
                max(cond(year == data_cohort & quarter == 1, `sharing_source', .))
            assert !missing(`q1_share_min') & `q1_share_min' == `q1_share_max'
            gen byte atc_sharing = `q1_share_max'
            replace atc_sharing = 0 if treated_in_stack == 0
            drop `q1_share_min' `q1_share_max'

            gen int rel_quarter_all = ///
                yq(year, quarter) - yq(data_cohort, 1)
            keep if rel_quarter_all >= -4 & rel_quarter_all <= 7
            gen int rel_quarter = rel_quarter_all if treated_in_stack == 1
            drop rel_quarter_all
            forvalues lead = 1/4 {
                gen byte pre_`lead' = ///
                    treated_in_stack == 1 & rel_quarter == -`lead'
            }
            forvalues lag = 0/7 {
                gen byte post_`lag' = ///
                    treated_in_stack == 1 & rel_quarter == `lag'
            }
            drop rel_quarter

            gen double target_raw = `target'
            quietly summarize `target', detail
            if r(N) == 0 {
                di as error "Target `target' is missing for every stacked observation."
                log close
                continue
            }
            if "`target'" == "avg_copay_amt" {
                local p95_value = r(p95)
                replace `target' = `p95_value' if ///
                    `target' > `p95_value' & !missing(`target')
            }

            gen int q_time = yq(year, quarter)
            format q_time %tq
            assert inrange(quarter, 1, 4)
            assert !missing(ndc) & !missing(boardname)
            foreach component of local plan_components {
                assert !missing(`component')
            }
            bysort ndc boardname data_cohort: ///
                assert treated_in_stack == treated_in_stack[1]
            bysort ndc boardname data_cohort: ///
                assert atc_sharing == atc_sharing[1]
            bysort ndc data_cohort boardname: gen byte board_tag = _n == 1
            bysort ndc data_cohort: egen int board_count = total(board_tag)
            assert board_count == 1
            drop board_tag board_count
            egen long id = group(`id_components')
            egen long plan_cohort_q = group(`plan_components' data_cohort q_time)
            isid id q_time

            gen byte expected_quarters = 12
            replace expected_quarters = 11 if ///
                `formulary_time_shift_quarters' == 1 & data_cohort == 2020
            replace expected_quarters = 11 if ///
                `formulary_time_shift_quarters' == 0 & data_cohort == 2024
            bysort id: assert _N == expected_quarters[1]
            drop expected_quarters

            gen byte treated = treated_in_stack
            gen int event_cohort_q = yq(data_cohort, 1) if treated == 1
            format event_cohort_q %tq
            gen byte pre_period = q_time < yq(data_cohort, 1)
            bysort id (q_time): assert treated == treated[1]
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

            local fe_spec "id q_time `other_history_list'"
            if `fe_level' == 2 {
                local fe_spec "id plan_cohort_q `other_history_list'"
            }
            gen byte atc_sharing_het = atc_sharing if treated == 1
            replace atc_sharing_het = 0 if treated == 0

            capture noisily did_imputation `target' id q_time event_cohort_q, ///
                fe(`fe_spec') ///
                horizons(`did_horizons') pretrends(`did_pretrends') ///
                hetby(atc_sharing_het) autosample tol(0.1) minn(0) ///
                cluster(boardname)
            local did_rc = _rc
            if `did_rc' != 0 {
                di as error "did_imputation failed with r(`did_rc')."
                capture file close failure_log
                file open failure_log using "`log_run_root'/failure.log", write text append
                file write failure_log ///
                    "event=`event', direction=`treatment_group', target=`target', rc=`did_rc'" _n
                file close failure_log
                log close
                continue
            }

            // A specification is valid only when every requested coefficient and
            // standard error exists. Validate the complete result before posting
            // statistics, coefficients, or figures.
            local missing_coefficients ""
            forvalues pre = 1/`did_pretrends' {
                scalar __b = .
                scalar __se = .
                capture scalar __b = _b[pre`pre']
                local b_rc = _rc
                capture scalar __se = _se[pre`pre']
                local se_rc = _rc
                if `b_rc' != 0 | `se_rc' != 0 | missing(__b) | missing(__se) {
                    local missing_coefficients "`missing_coefficients' pre`pre'"
                }
            }
            forvalues horizon = 0/7 {
                foreach sharing in 0 1 {
                    scalar __b = .
                    scalar __se = .
                    capture scalar __b = _b[tau`horizon'_`sharing']
                    local b_rc = _rc
                    capture scalar __se = _se[tau`horizon'_`sharing']
                    local se_rc = _rc
                    if `b_rc' != 0 | `se_rc' != 0 | missing(__b) | missing(__se) {
                        local missing_coefficients ///
                            "`missing_coefficients' tau`horizon'_`sharing'"
                    }
                }
            }
            if "`missing_coefficients'" != "" {
                di as error "Skipping entire specification; unavailable estimates:`missing_coefficients'"
                capture file close failure_log
                file open failure_log using "`log_run_root'/failure.log", write text append
                file write failure_log ///
                    "event=`event', direction=`treatment_group', target=`target', unavailable=`missing_coefficients'" _n
                file close failure_log
                log close
                continue
            }

            * Rebuild the two coefficient/VCV pairs exactly as in the existing
            * did_imputation_event_study graph workflow.
            foreach sharing in 0 1 {
                matrix did2_b_s`sharing' = J(1, `n_total', .)
                matrix did2_V_s`sharing' = J(`n_total', `n_total', 0)
                matrix colnames did2_b_s`sharing' = `coef_names'
                matrix colnames did2_V_s`sharing' = `coef_names'
                matrix rownames did2_V_s`sharing' = `coef_names'
            }
            matrix did2_b_full = e(b)
            matrix did2_V_full = e(V)
            local did2_ncol = colsof(did2_b_full)
            local did2_names : colnames did2_b_full
            local graph_ready 1

            foreach sharing in 0 1 {
                tempname column_map
                matrix `column_map' = J(1, `n_total', 0)

                forvalues coefficient_index = 1/`n_total' {
                    local coefficient_name : word `coefficient_index' of `coef_names'
                    local candidate_name ""
                    if substr("`coefficient_name'", 1, 4) == "pre_" {
                        local lead = substr("`coefficient_name'", 5, .)
                        local candidate_name "pre`lead'"
                    }
                    else if substr("`coefficient_name'", 1, 5) == "post_" {
                        local lag = substr("`coefficient_name'", 6, .)
                        local candidate_name "tau`lag'_`sharing'"
                    }

                    local found_column 0
                    forvalues source_column = 1/`did2_ncol' {
                        local source_name : word `source_column' of `did2_names'
                        if `found_column' == 0 & "`source_name'" == "`candidate_name'" {
                            local found_column `source_column'
                        }
                    }
                    if `found_column' == 0 {
                        local graph_ready 0
                        di as error ///
                            "Coefficient missing for original graph design: `candidate_name'"
                    }
                    else {
                        matrix `column_map'[1, `coefficient_index'] = `found_column'
                        matrix did2_b_s`sharing'[1, `coefficient_index'] = ///
                            did2_b_full[1, `found_column']
                    }
                }

                if `graph_ready' == 1 {
                    forvalues row_index = 1/`n_total' {
                        local source_row = el(`column_map', 1, `row_index')
                        forvalues column_index = 1/`n_total' {
                            local source_column = el(`column_map', 1, `column_index')
                            matrix did2_V_s`sharing'[`row_index', `column_index'] = ///
                                did2_V_full[`source_row', `source_column']
                        }
                    }
                }
            }

            * did_imputation pretrends are common; show them only once.
            if `graph_ready' == 1 {
                forvalues coefficient_index = 1/`n_total' {
                    local coefficient_name : word `coefficient_index' of `coef_names'
                    if substr("`coefficient_name'", 1, 4) == "pre_" {
                        matrix did2_b_s1[1, `coefficient_index'] = .
                        forvalues covariance_index = 1/`n_total' {
                            matrix did2_V_s1[`coefficient_index', `covariance_index'] = 0
                            matrix did2_V_s1[`covariance_index', `coefficient_index'] = 0
                        }
                    }
                }
            }
            if `graph_ready' == 0 {
                di as error "Skipping specification because the original event_plot inputs are incomplete."
                log close
                continue
            }

            tempvar did_sample tag
            gen byte `did_sample' = e(sample)
            quietly count if `did_sample'
            local autosample_n = r(N)

            quietly count if `did_sample' & treated == 0
            local obs_control = r(N)
            quietly count if `did_sample' & treated == 1 & atc_sharing == 0
            local obs_treated_notshare = r(N)
            quietly count if `did_sample' & treated == 1 & atc_sharing == 1
            local obs_treated_share = r(N)

            foreach group in control treated_notshare treated_share {
                local condition "treated == 0"
                if "`group'" == "treated_notshare" {
                    local condition "treated == 1 & atc_sharing == 0"
                }
                else if "`group'" == "treated_share" {
                    local condition "treated == 1 & atc_sharing == 1"
                }

                tempvar id_tag firm_tag
                egen byte `id_tag' = tag(id) if `did_sample' & `condition'
                quietly count if `id_tag' == 1
                local ids_`group' = r(N)
                egen byte `firm_tag' = tag(boardname) if `did_sample' & `condition'
                quietly count if `firm_tag' == 1
                local firms_`group' = r(N)
            }

            quietly summarize `target' if ///
                `did_sample' & treated == 1 & atc_sharing == 0 & pre_period == 1, meanonly
            local premean_notshare = r(mean)
            quietly summarize `target' if ///
                `did_sample' & treated == 1 & atc_sharing == 1 & pre_period == 1, meanonly
            local premean_share = r(mean)

            post `sample_post' ///
                ("`event'") ("`treatment_group'") ("`target'") ///
                (`obs_control') (`obs_treated_notshare') (`obs_treated_share') ///
                (`ids_control') (`ids_treated_notshare') (`ids_treated_share') ///
                (`firms_control') (`firms_treated_notshare') (`firms_treated_share') ///
                (`premean_notshare') (`premean_share')

            forvalues pre = 1/`did_pretrends' {
                scalar __b = _b[pre`pre']
                scalar __se = _se[pre`pre']
                scalar __lo = __b - invnormal(0.975) * __se
                scalar __hi = __b + invnormal(0.975) * __se
                // did_imputation estimates common, not hetby-specific, pretrends.
                post `coefficient_post' ///
                    ("`event'") ("`treatment_group'") ("`target'") (0) ///
                    (-`pre') (__b) (__se) (__lo) (__hi) (`autosample_n')
            }

            forvalues horizon = 0/7 {
                foreach sharing in 0 1 {
                    scalar __b = _b[tau`horizon'_`sharing']
                    scalar __se = _se[tau`horizon'_`sharing']
                    scalar __lo = __b - invnormal(0.975) * __se
                    scalar __hi = __b + invnormal(0.975) * __se
                    post `coefficient_post' ///
                        ("`event'") ("`treatment_group'") ("`target'") (`sharing') ///
                        (`horizon') (__b) (__se) (__lo) (__hi) (`autosample_n')
                }
            }

            * Keep the existing did_imputation_event_study visual design.
            local event_name = subinstr("`event'", "_", " ", .)
            local counterpart "A"
            if "`treatment_group'" == "A" {
                local counterpart "B"
            }
            local group_label "`treatment_group'_without_`counterpart'"
            local graph_title_size small
            local cv_title " |other_event"
            local kappa_title " |none|atc3|none"

            event_plot did2_b_s0#did2_V_s0 did2_b_s1#did2_V_s1, ///
                stub_lag(post_# post_#) ///
                stub_lead(pre_# pre_#) ///
                trimlead(`trimlead') trimlag(`trimlag') ///
                plottype(scatter) ciplottype(rcap) ///
                together perturb(-`perturb_step'(`perturb_span')`perturb_step') ///
                noautolegend ///
                graph_opt( ///
                    title("`event_name' event Not raw | `group_label'`cv_title'`kappa_title'", ///
                        size(`graph_title_size')) ///
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

            graph export "`figure_dir'/plot.png", ///
                replace width(`graph_width')
            log close
        }
    }
}

postclose `coefficient_post'
postclose `sample_post'

use `coefficient_data', clear
sort event direction target sharing rel_quarter
export delimited using "`csv_run_root'/dynamic.csv", replace

use `sample_data', clear
sort event direction target
export delimited using "`csv_run_root'/samples.csv", replace

di as result "Saved formulary dynamic did_imputation outputs."
