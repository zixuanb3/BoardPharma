clear all
set more off
set trace off
cap mkdir "D:/Boardex_pharma/_repro_stata_tmp"
cap mkdir "F:/BoardPharma/_repro_stata_tmp"
capture noisily set tempdir "F:/BoardPharma/_repro_stata_tmp"

// ================================================================
// Purpose:
//   Estimate stacked did_imputation event studies for ATC-sharing
//   heterogeneity using variant-specific cohort outputs.
//
// Inputs:
//   - F:/BoardPharma/data/roster_variants/<variant>/leader_tier_<tier>/cohort_data
//   - F:/BoardPharma/data/roster_variants/<variant>/leader_tier_<tier>/kappa_controls
//   - D:/Dropbox/BoardPharma/InterimData/boardex_ssr_price_sample.csv
//   - C:/Users/12150/Documents/GitHub/Grad-IO/outputs/ssr_states/ssr_134_company_states_size.csv
//
// Outputs:
//   - F:/BoardPharma/csv/did_imputation_event_study_req2_fe1_window_event_control/...
//   - F:/BoardPharma/logs/did_imputation_event_study_req2_fe1_window_event_control/...
//
// Frequency and event window:
//   - All regression variants use quarterly time: pre-4, event period 0,
//     and post-7. Annual roster events are anchored at Q1.
//
// Fixed effects:
//   - Level 1: Product x cohort FE + period x cohort FE.
//   - Level 2: Level 1 + sales-size-quintile x period x cohort FE
//              + state x period x cohort FE.
//
// Notes:
//   - atc3_sharing is constructed by CohortPanelMaker from the complete SSR
//     price sample and is already frozen at the event-product level across the
//     event window. No ATC mapping merge is performed here.
//   - Company size quintiles use annual total quantity across all SSR products.
//   - Kappa is the movement-aligned prior-year or prior-four-quarter control;
//     the `none` specification omits Kappa from the regression.
//   - Standard errors are clustered by BoardName.
// ================================================================

// =============================== Configuration ===============================
local project_path : environment BOARDPHARMA_RESULTS_ROOT
if `"`project_path'"' == "" local project_path "F:/BoardPharma"
local data_project_path : environment BOARDPHARMA_PROJECT_ROOT
if `"`data_project_path'"' == "" local data_project_path "F:/BoardPharma"
local variant_data_root : environment BOARDPHARMA_VARIANT_OUTPUT_ROOT
if `"`variant_data_root'"' == "" local variant_data_root "`data_project_path'/data/roster_variants"
local kappa_variant_data_root : environment BOARDPHARMA_KAPPA_VARIANT_OUTPUT_ROOT
if `"`kappa_variant_data_root'"' == "" local kappa_variant_data_root "`variant_data_root'"
local cache_namespace : environment BOARDPHARMA_CACHE_NAMESPACE
if `"`cache_namespace'"' == "" local cache_namespace "default"
local price_sample "D:/Dropbox/BoardPharma/InterimData/boardex_ssr_price_sample.csv"
local state_file "C:/Users/12150/Documents/GitHub/Grad-IO/outputs/ssr_states/ssr_134_company_states_size.csv"

local roster_variants imputed_year imputed_yearquarter drop_sentinel_year drop_sentinel_yearquarter
local personnel_definitions narrow medium broad
args requested_roster_variant requested_personnel_definition requested_req requested_fe requested_target requested_window_event_control requested_treatment_group requested_event requested_control requested_kappa
if `"`requested_roster_variant'"' != "" & `"`requested_roster_variant'"' != "all" local roster_variants `requested_roster_variant'
if `"`requested_personnel_definition'"' != "" & `"`requested_personnel_definition'"' != "all" local personnel_definitions `requested_personnel_definition'
local atc_levels atc3
local events interlock_dissolution to_B_not_in_A to_B_still_in_A
if `"`requested_event'"' != "" & `"`requested_event'"' != "all" local events `requested_event'
local controls not not_yet pure_control
if `"`requested_control'"' != "" & `"`requested_control'"' != "all" local controls `requested_control'
local requirements 0 1 2
if `"`requested_req'"' != "" & `"`requested_req'"' != "all" local requirements `requested_req'
local treatment_groups A B
if `"`requested_treatment_group'"' != "" & `"`requested_treatment_group'"' != "all" local treatment_groups `requested_treatment_group'
local include_eventpair_values 0
local kappa_controls kappa_norm none kappa_asy kappa_norm_p95 kappa_asy_p95
if `"`requested_kappa'"' != "" & `"`requested_kappa'"' != "all" local kappa_controls `requested_kappa'
// Optional sensitivity mode: retain the full panel by coding missing Kappa as zero.
local zero_fill_kappa : environment BOARDPHARMA_KAPPA_MISSING_ZERO
if `"`zero_fill_kappa'"' == "" local zero_fill_kappa 0
local window_event_controls 0 1
local fe_levels 1 2
if `"`requested_fe'"' != "" & `"`requested_fe'"' != "all" local fe_levels `requested_fe'
local targets price1 price0
if `"`requested_target'"' != "" & `"`requested_target'"' != "all" local targets `requested_target'
if `"`requested_window_event_control'"' != "" & `"`requested_window_event_control'"' != "all" local window_event_controls `requested_window_event_control'
local outlier_treatment winsorize
local outlier_percentile p95
local pretrend_quarter 4
local post_horizon_quarter 7
// Annual roster events are still estimated on the quarterly price panel.
local did_minn : environment BOARDPHARMA_DID_MINN
if `"`did_minn'"' == "" local did_minn 0

// A single-threaded mode is available for isolated diagnostics of Stata MP
// specifications that trigger a long-running internal wait on some machines.
local single_thread : environment BOARDPHARMA_STATA_SINGLE_THREAD
if `"`single_thread'"' == "1" capture noisily set processors 1

cap mkdir "`project_path'/csv"
cap mkdir "`project_path'/logs"
cap mkdir "`project_path'/csv/did_imputation_event_study_req2_fe1_window_event_control"
cap mkdir "`project_path'/logs/did_imputation_event_study_req2_fe1_window_event_control"

foreach roster_variant of local roster_variants {
    // The regression time unit is quarterly for every roster variant.
    local panel_level quarter
    local window_pre `pretrend_quarter'
    local window_post `post_horizon_quarter'

    foreach personnel_definition of local personnel_definitions {
        local variant_root "`variant_data_root'/`roster_variant'/leader_tier_`personnel_definition'"
        local kappa_variant_root "`kappa_variant_data_root'/`roster_variant'/leader_tier_`personnel_definition'"
        local crosswalk_file "`kappa_variant_root'/kappa_controls/ssr_cusip_mapping.csv"
        local kappa_file_level yearquarter
        local kappa_file "`kappa_variant_root'/kappa_controls/kappa_controls_movement_`kappa_file_level'.csv"

        // Build annual sales-size quintiles from all products in SSR price sample.
        preserve
        import delimited using "`price_sample'", clear varnames(1) stringcols(_all)
        capture rename BoardName boardname
        capture recast str128 boardname
        keep boardname year quantity
        destring year, replace force
        destring quantity, replace ignore(",") force
        drop if missing(boardname) | missing(year) | missing(quantity)
        collapse (sum) annual_quantity=quantity, by(boardname year)
        gen byte size_quintile = .
        levelsof year, local(size_years)
        foreach size_year of local size_years {
            quietly xtile size_q_tmp = annual_quantity if year == `size_year', nq(5)
            replace size_quintile = size_q_tmp if year == `size_year'
            drop size_q_tmp
        }
        keep boardname year annual_quantity size_quintile
        isid boardname year
        tempfile size_map
        save `size_map', replace
        restore

        // State is a firm-level lookup; size fields in this file are ignored.
        preserve
        import delimited using "`state_file'", clear varnames(1) stringcols(_all)
        capture rename BoardName boardname
        capture recast str128 boardname
        keep boardname state
        duplicates drop boardname, force
        tempfile state_map
        save `state_map', replace
        restore

        foreach atc of local atc_levels {
            // The atc loop is retained for the output directory convention.
            // CohortPanelMaker now supplies atc3_sharing directly in every
            // cohort file, so no roster-filtered mapping file is read here.

            // Build the BoardName to canonical Kappa-firm crosswalk.
            preserve
            import delimited using "`crosswalk_file'", clear varnames(1) stringcols(_all)
            capture rename BoardName boardname
            capture rename MatchedKey firm_key
            capture rename matchedkey firm_key
            capture recast str128 boardname
            keep boardname firm_key
            duplicates drop
            isid boardname
            tempfile firm_crosswalk
            save `firm_crosswalk', replace
            restore

            // Kappa controls are keyed by canonical firm and data_cohort.
            preserve
            import delimited using "`kappa_file'", clear varnames(1) stringcols(_all)
            foreach kappa_numeric_var in kappa_raw_pre kappa_norm_pre kappa_raw_p95_pre kappa_norm_p95_pre kappa_pre_valid {
                capture confirm string variable `kappa_numeric_var'
                if !_rc destring `kappa_numeric_var', replace ignore(",") force
            }
            keep firm_key data_cohort kappa_raw_pre kappa_norm_pre kappa_raw_p95_pre kappa_norm_p95_pre kappa_pre_valid
            duplicates drop firm_key data_cohort, force
            tempfile kappa_map
            save `kappa_map', replace
            restore

            foreach treatment_group of local treatment_groups {
                local treatment_group = upper("`treatment_group'")
                local counterpart A
                if "`treatment_group'" == "A" local counterpart B

                foreach include_eventpair of local include_eventpair_values {
                    local relation without
                    if `include_eventpair' == 1 local relation with
                    local panel_group_folder "quarter-level_`treatment_group'_`relation'_`counterpart'"
                    local cohort_root "`variant_root'/cohort_data/`panel_group_folder'"

                    foreach fe_level of local fe_levels {
                        local output_root "`project_path'/csv/did_imputation_event_study_req2_fe1_window_event_control/`roster_variant'/leader_tier_`personnel_definition'/fe`fe_level'/`atc'/`panel_group_folder'"
                        local log_root "`project_path'/logs/did_imputation_event_study_req2_fe1_window_event_control/`roster_variant'/leader_tier_`personnel_definition'/fe`fe_level'/`atc'/`panel_group_folder'"
                        cap mkdir "`project_path'/csv/did_imputation_event_study_req2_fe1_window_event_control/`roster_variant'"
                        cap mkdir "`project_path'/csv/did_imputation_event_study_req2_fe1_window_event_control/`roster_variant'/leader_tier_`personnel_definition'"
                        cap mkdir "`project_path'/csv/did_imputation_event_study_req2_fe1_window_event_control/`roster_variant'/leader_tier_`personnel_definition'/fe`fe_level'"
                        cap mkdir "`project_path'/csv/did_imputation_event_study_req2_fe1_window_event_control/`roster_variant'/leader_tier_`personnel_definition'/fe`fe_level'/`atc'"
                        cap mkdir "`project_path'/logs/did_imputation_event_study_req2_fe1_window_event_control/`roster_variant'"
                        cap mkdir "`project_path'/logs/did_imputation_event_study_req2_fe1_window_event_control/`roster_variant'/leader_tier_`personnel_definition'"
                        cap mkdir "`project_path'/logs/did_imputation_event_study_req2_fe1_window_event_control/`roster_variant'/leader_tier_`personnel_definition'/fe`fe_level'"
                        cap mkdir "`project_path'/logs/did_imputation_event_study_req2_fe1_window_event_control/`roster_variant'/leader_tier_`personnel_definition'/fe`fe_level'/`atc'"
                        cap mkdir "`output_root'"
                        cap mkdir "`log_root'"

                        foreach event of local events {
                            foreach control of local controls {
                                local control_folder ""
                                local control_name ""
                                if "`control'" == "not" {
                                    local control_folder "Not"
                                    local control_name not
                                }
                                else if "`control'" == "not_yet" {
                                    local control_folder "Not Yet"
                                    local control_name not_yet
                                }
                                else if "`control'" == "pure_control" {
                                    local control_folder "Pure Control"
                                    local control_name pure_control
                                }
                                else {
                                    di as error "Unknown control: `control'"
                                    exit 198
                                }

                                foreach req of local requirements {
                                    local cohort_dir "`cohort_root'/event/req`req'/`control_folder'"
                                    local cohort_files : dir "`cohort_dir'" files "`event'_`panel_level'_cohort_*_balanced.csv"
                                    if `"`cohort_files'"' == "" {
                                        di as error "No cohort files found: `cohort_dir'"
                                        continue
                                    }

                                    foreach window_event_control of local window_event_controls {
                                    local window_event_label without_window_other_event
                                    local event_control_var ""
                                    if `window_event_control' == 1 {
                                        local window_event_label with_window_other_event
                                        local event_control_var window_other_event
                                    }
                                    foreach target of local targets {
                                    foreach control_kappa of local kappa_controls {
                                        local kappa_var kappa_raw_pre
                                        if "`control_kappa'" == "kappa_norm" local kappa_var kappa_norm_pre
                                        if "`control_kappa'" == "kappa_norm_p95" local kappa_var kappa_norm_p95_pre
                                        if "`control_kappa'" == "kappa_asy_p95" local kappa_var kappa_raw_p95_pre
                                        if "`control_kappa'" == "none" local kappa_var ""
                                        local output_file "`output_root'/`event'_req`req'_`control_name'_`window_event_label'_`control_kappa'_`target'.csv"
                                        // Force re-estimation so the result uses the
                                        // cohort-level atc3_sharing definition.
                                        local log_file "`log_root'/`event'_req`req'_`control_name'_`window_event_label'_`control_kappa'_`target'.log"
                                        cap log close
                                        log using "`log_file'", text replace

                                        // Cache the raw stacked cohort once per event,
                                        // control, and treatment-group combination.
                                        // This avoids rereading every cohort CSV for
                                        // each Kappa, outcome, and window-control run.
                                        // Use a run-specific cache namespace so stale stacked
                                        // panels from an earlier pipeline run cannot be reused.
                                        local cache_root "`variant_root'/cache_`cache_namespace'"
                                        cap mkdir "`cache_root'"
                                        local raw_cache_file "`cache_root'/did_`panel_group_folder'_`event'_req`req'_`control_name'_stacked.dta"
                                        // Define the quarterly time variable before the cache branch.
                                        // A cached stack skips the import branch, so defining it
                                        // only inside that branch leaves did_imputation with too
                                        // few variables on an isolated rerun.
                                        local period_var q_time
                                        tempfile master result_file
                                        local first_file 1
                                        local n_cohorts : word count `cohort_files'

                                        capture confirm file "`raw_cache_file'"
                                        if _rc {
                                        foreach cohort_file of local cohort_files {
                                            if !regexm("`cohort_file'", "_cohort_([0-9]+q[1-4])_balanced") continue
                                            local cohort_token = regexs(1)
                                            local cohort_year = real(substr("`cohort_token'", 1, 4))
                                            local cohort_quarter = real(substr("`cohort_token'", 6, 1))
                                            local event_period = yq(`cohort_year', `cohort_quarter')
                                            local period_var q_time
                                            local event_label "`cohort_token'"
                                            local window_pre 4
                                            local window_post 7
                                            local data_cohort_value "Y`cohort_year'Q`cohort_quarter'"

                                            import delimited using "`cohort_dir'/`cohort_file'", clear varnames(1)
                                            quietly count
                                            if r(N) == 0 continue
                                            capture recast str128 boardname
                                            capture confirm string variable year
                                            if !_rc destring year, replace force
                                            if "`panel_level'" == "quarter" {
                                                capture confirm string variable quarter
                                                if !_rc destring quarter, replace force
                                            }
                                            gen str32 data_cohort = "`data_cohort_value'"
                                            gen double event_period = `event_period'
                                            capture confirm variable atc3_sharing
                                            if _rc {
                                                di as error "Missing atc3_sharing in `cohort_file'"
                                                exit 111
                                            }
                                            capture confirm string variable atc3_sharing
                                            if !_rc destring atc3_sharing, replace force
                                            gen byte atc_sharing = atc3_sharing
                                            replace atc_sharing = 0 if missing(atc_sharing)
                                            drop atc3_sharing
                                            merge m:1 boardname using `firm_crosswalk', keep(master match) nogen
                                            merge m:1 firm_key data_cohort using `kappa_map', keep(master match) nogen

                                            capture confirm variable event_`event_label'
                                            if _rc {
                                                di as error "Missing event_`event_label' in `cohort_file'"
                                                exit 111
                                            }
                                            capture confirm variable window_other_event
                                            if _rc {
                                                di as error "Missing window_other_event in `cohort_file'"
                                                exit 111
                                            }
                                            capture confirm string variable window_other_event
                                            if !_rc destring window_other_event, replace force
                                            gen byte treated_in_stack = event_`event_label' == 1
                                            gen double event_cohort = event_period if treated_in_stack == 1
                                            if "`panel_level'" == "quarter" {
                                                gen double q_time = yq(year, quarter)
                                                format q_time %tq
                                                gen double rel_period = q_time - event_period
                                            }
                                            else gen double rel_period = year - event_period
                                            keep if inrange(rel_period, -`window_pre', `window_post')
                                            quietly count
                                            if r(N) == 0 continue

                                            if `first_file' {
                                                save `master', replace
                                                local first_file 0
                                            }
                                            else {
                                                append using `master'
                                                save `master', replace
                                            }
                                            local n_cohorts = `n_cohorts' + 1
                                        }

                                        if `first_file' {
                                            di as error "No valid cohort files were parsed in `cohort_dir'"
                                            log close
                                            continue
                                        }

                                        use `master', clear
                                        save "`raw_cache_file'", replace
                                        }
                                        else {
                                            use "`raw_cache_file'", clear
                                        }

                                        merge m:1 boardname year using `size_map', keep(master match) nogen
                                        merge m:1 boardname using `state_map', keep(master match) nogen

                                        if "`outlier_treatment'" == "winsorize" {
                                            quietly summarize `target', detail
                                            replace `target' = r(`outlier_percentile') if `target' > r(`outlier_percentile') & !missing(`target')
                                        }
                                        else if "`outlier_treatment'" == "trim" {
                                            quietly summarize `target', detail
                                            drop if `target' > r(`outlier_percentile') & !missing(`target')
                                        }
                                        replace `target' = log(`target')
                                        egen id = group(boardname product data_cohort)
                                        egen board_cluster = group(boardname)
                                        gen treated = treated_in_stack
                                        gen event_cohort_did_imputation = event_cohort

                                        if "`control_kappa'" == "none" {
                                            local missing_kappa = .
                                        }
                                        else {
                                            count if missing(`kappa_var')
                                            local missing_kappa = r(N)
                                            if `"`zero_fill_kappa'"' == "1" {
                                                replace `kappa_var' = 0 if missing(`kappa_var')
                                            }
                                            else {
                                                drop if missing(`kappa_var')
                                            }
                                        }
                                        if `fe_level' == 2 drop if missing(size_quintile) | missing(state)

                                        egen product_cohort_fe = group(product data_cohort)
                                        egen cohort_period_fe = group(data_cohort `period_var')
                                        local fe_spec "product_cohort_fe cohort_period_fe"
                                        if `fe_level' == 2 {
                                            egen size_cohort_period_fe = group(size_quintile data_cohort `period_var')
                                            egen state_cohort_period_fe = group(state data_cohort `period_var')
                                            local fe_spec "`fe_spec' size_cohort_period_fe state_cohort_period_fe"
                                        }

                                        gen atc_sharing_het = atc_sharing if treated == 1
                                        replace atc_sharing_het = 0 if treated == 0
                                        if "`control_kappa'" == "none" {
                                            local did_controls "controls(`event_control_var')"
                                        }
                                        else {
                                            local did_controls "controls(`kappa_var' `event_control_var')"
                                        }
                                        local horizons "0/`window_post'"

                                            capture noisily did_imputation `target' id `period_var' event_cohort_did_imputation, ///
                                            fe(`fe_spec') horizons(`horizons') pretrends(`window_pre') ///
                                            hetby(atc_sharing_het) autosample tol(0.1) minn(`did_minn') ///
                                            cluster(board_cluster) `did_controls'
                                        local did_rc = _rc

                                        tempvar estimation_sample
                                        gen byte `estimation_sample' = 0
                                        if `did_rc' == 0 {
                                            replace `estimation_sample' = e(sample)
                                        }

                                        tempname posth
                                        postfile `posth' ///
                                             str32 roster_variant str16 personnel_definition str12 panel_level ///
                                            str12 atc str35 panel_group str30 event str16 control str8 outcome double req fe_level ///
                                            str16 kappa_control byte window_event_control double sharing_group rel_period estimate std_error p_value ///
                                            double N_obs N_treated N_control n_treated_firms n_control_firms ///
                                            n_treated_products n_control_products missing_kappa n_cohorts did_rc ///
                                            using `result_file', replace

                                        if `did_rc' == 0 {
                                            matrix b = e(b)
                                            matrix V = e(V)
                                            local names : colnames b
                                            local ncol = colsof(b)
                                            local N_obs = e(N)
                                            quietly count if `estimation_sample' == 1 & treated == 1
                                            local N_treated = r(N)
                                            quietly count if `estimation_sample' == 1 & treated == 0
                                            local N_control = r(N)
                                            tempvar tag_treated_firm tag_control_firm tag_treated_product tag_control_product
                                            egen `tag_treated_firm' = tag(boardname) if `estimation_sample' == 1 & treated == 1
                                            egen `tag_control_firm' = tag(boardname) if `estimation_sample' == 1 & treated == 0
                                            egen `tag_treated_product' = tag(boardname product) if `estimation_sample' == 1 & treated == 1
                                            egen `tag_control_product' = tag(boardname product) if `estimation_sample' == 1 & treated == 0
                                            quietly count if `tag_treated_firm' == 1
                                            local n_treated_firms = r(N)
                                            quietly count if `tag_control_firm' == 1
                                            local n_control_firms = r(N)
                                            quietly count if `tag_treated_product' == 1
                                            local n_treated_products = r(N)
                                            quietly count if `tag_control_product' == 1
                                            local n_control_products = r(N)

                                            forvalues s = 0/1 {
                                                forvalues h = 0/`window_post' {
                                                    local candidate "tau`h'_`s'"
                                                    local found 0
                                                    forvalues j = 1/`ncol' {
                                                        local colname : word `j' of `names'
                                                        if "`colname'" == "`candidate'" local found `j'
                                                    }
                                                    local estimate .
                                                    local std_error .
                                                    local p_value .
                                                    if `found' > 0 {
                                                        local estimate = b[1,`found']
                                                        local std_error = sqrt(V[`found',`found'])
                                                        if `std_error' > 0 local p_value = 2 * normal(-abs(`estimate'/`std_error'))
                                                    }
                                                    post `posth' ("`roster_variant'") ("`personnel_definition'") ("`panel_level'") ///
                                                        ("`atc'") ("`panel_group_folder'") ("`event'") ("`control_name'") ("`target'") (`req') (`fe_level') ///
                                                        ("`control_kappa'") (`window_event_control') (`s') (`h') (`estimate') (`std_error') (`p_value') ///
                                                        (`N_obs') (`N_treated') (`N_control') (`n_treated_firms') (`n_control_firms') ///
                                                        (`n_treated_products') (`n_control_products') (`missing_kappa') (`n_cohorts') (`did_rc')
                                                }
                                            }

                                            forvalues j = 1/`window_pre' {
                                                local candidate "pre`j'"
                                                local found 0
                                                forvalues k = 1/`ncol' {
                                                    local colname : word `k' of `names'
                                                    if "`colname'" == "`candidate'" local found `k'
                                                }
                                                local estimate .
                                                local std_error .
                                                local p_value .
                                                if `found' > 0 {
                                                    local estimate = b[1,`found']
                                                    local std_error = sqrt(V[`found',`found'])
                                                    if `std_error' > 0 local p_value = 2 * normal(-abs(`estimate'/`std_error'))
                                                }
                                                post `posth' ("`roster_variant'") ("`personnel_definition'") ("`panel_level'") ///
                                                    ("`atc'") ("`panel_group_folder'") ("`event'") ("`control_name'") ("`target'") (`req') (`fe_level') ///
                                                    ("`control_kappa'") (`window_event_control') (-1) (-`j') (`estimate') (`std_error') (`p_value') ///
                                                    (`N_obs') (`N_treated') (`N_control') (`n_treated_firms') (`n_control_firms') ///
                                                    (`n_treated_products') (`n_control_products') (`missing_kappa') (`n_cohorts') (`did_rc')
                                            }
                                        }
                                        else {
                                            post `posth' ("`roster_variant'") ("`personnel_definition'") ("`panel_level'") ///
                                                ("`atc'") ("`panel_group_folder'") ("`event'") ("`control_name'") ("`target'") (`req') (`fe_level') ///
                                                ("`control_kappa'") (`window_event_control') (.) (.) (.) (.) (.) (.) (.) (.) (.) (.) (.) (.) (`missing_kappa') (`n_cohorts') (`did_rc')
                                        }
                                        postclose `posth'
                                        use `result_file', clear
                                        export delimited using "`output_file'", replace
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

// Signal completion only after every requested result file and log is closed.
local completion_marker : environment BOARDPHARMA_COMPLETION_MARKER
if `"`completion_marker'"' != "" {
    capture file close _completion_marker
    capture file open _completion_marker using "`completion_marker'", write text replace
    if !_rc {
        file write _completion_marker "completed" _n
        file close _completion_marker
    }
}

clear all

