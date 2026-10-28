// Purpose: Estimate fresh FE1 REQ2 results with no Kappa dependencies.
// Input: Fresh narrow cohorts and SSR price data.
// Output: Specification CSVs and complete Stata logs in the isolated results root.
clear all
set more off
set trace off
cap mkdir "D:/Boardex_pharma/_repro_stata_tmp"

// ================================================================
// Purpose:
//   Estimate stacked DDD models for the incremental effect of ATC sharing.
//   The script reads the new variant-specific cohort, cohort-level ATC3
//   sharing, Kappa, SSR-price size, and company-state outputs directly.
//
// Inputs:
//   - F:/BoardPharma/data/roster_variants/<variant>/leader_tier_<tier>/cohort_data
//   - F:/BoardPharma/data/roster_variants/<variant>/leader_tier_<tier>/kappa_controls
//   - D:/Dropbox/BoardPharma/InterimData/boardex_ssr_price_sample.csv
//   - C:/Users/12150/Documents/GitHub/Grad-IO/outputs/ssr_states/ssr_134_company_states_size.csv
//
// Outputs:
//   - F:/BoardPharma/csv/ddd_atcsharing_req2_fe1_window_event_control/...
//   - F:/BoardPharma/logs/ddd_atcsharing_req2_fe1_window_event_control/...
//
// Frequency and event window:
//   - All regression variants use quarterly time. Annual roster events are
//     anchored at Q1, so every event window is t-4 through t+7, including
//     the event quarter.
//
// Fixed effects:
//   - Level 1: Product x cohort FE + period x cohort FE.
//   - Level 2: Level 1 + sales-size-quintile x period x cohort FE
//              + state x period x cohort FE.
//
// Notes:
//   - atc3_sharing is constructed by CohortPanelMaker from the complete
//     SSR price sample and is already frozen at the event-product level across
//     the event window. No ATC mapping merge is performed here.
//   - Annual company size is the within-year quintile of total quantity sold
//     across all products in SSR price sample.
//   - Kappa controls are the movement-aligned prior-year or prior-four-quarter
//     averages from KappaControlYearQuarter.py. The `none` specification omits
//     Kappa from the regression.
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
local price_sample "D:/Dropbox/BoardPharma/InterimData/boardex_ssr_price_sample.csv"
local state_file "C:/Users/12150/Documents/GitHub/Grad-IO/outputs/ssr_states/ssr_134_company_states_size.csv"

local roster_variants imputed_year imputed_yearquarter drop_sentinel_year drop_sentinel_yearquarter
local personnel_definitions narrow medium broad
args requested_roster_variant requested_personnel_definition requested_req requested_fe requested_target requested_window_event_control requested_treatment_group requested_event requested_control requested_kappa

// Optional fast path for unusually large cells that stall during post-estimation
// sample-count diagnostics. Regression estimates are unchanged; only the
// nonessential count diagnostics are recorded as missing in this mode.
local fast_ddd_counts 0
if "$BOARDPHARMA_FAST_DDD_COUNTS" == "1" local fast_ddd_counts 1
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
local kappa_controls none
// This derived script accepts only the no-Kappa specification.
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
local single_thread : environment BOARDPHARMA_STATA_SINGLE_THREAD
if `"`single_thread'"' == "1" capture noisily set processors 1

cap mkdir "`project_path'/csv"
cap mkdir "`project_path'/logs"
cap mkdir "`project_path'/csv/ddd_atcsharing_req2_fe1_window_event_control"
cap mkdir "`project_path'/logs/ddd_atcsharing_req2_fe1_window_event_control"

foreach roster_variant of local roster_variants {
    // Roster frequency determines event-date provenance, not regression frequency.
    // Annual events are emitted as YYYYQ1 cohort files.
    local panel_level quarter

    foreach personnel_definition of local personnel_definitions {
        local variant_root "`variant_data_root'/`roster_variant'/leader_tier_`personnel_definition'"
        local kappa_variant_root "`kappa_variant_data_root'/`roster_variant'/leader_tier_`personnel_definition'"
        local crosswalk_file "`kappa_variant_root'/kappa_controls/ssr_cusip_mapping.csv"
        local kappa_file_level yearquarter
        local kappa_file "`kappa_variant_root'/kappa_controls/kappa_controls_movement_`kappa_file_level'.csv"

        // Build a year-varying size map from annual total SSR quantity.
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

        // State is a firm-level lookup. Size variables in this file are not used.
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

            // Map display/historical names to the canonical Kappa firm key.
            // No Kappa or CUSIP lookup is required for this run.

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
                        local output_root "`project_path'/csv/ddd_atcsharing_req2_fe1_window_event_control/`roster_variant'/leader_tier_`personnel_definition'/fe`fe_level'/`atc'/`panel_group_folder'"
                        local log_root "`project_path'/logs/ddd_atcsharing_req2_fe1_window_event_control/`roster_variant'/leader_tier_`personnel_definition'/fe`fe_level'/`atc'/`panel_group_folder'"
                        cap mkdir "`project_path'/csv/ddd_atcsharing_req2_fe1_window_event_control/`roster_variant'"
                        cap mkdir "`project_path'/csv/ddd_atcsharing_req2_fe1_window_event_control/`roster_variant'/leader_tier_`personnel_definition'"
                        cap mkdir "`project_path'/csv/ddd_atcsharing_req2_fe1_window_event_control/`roster_variant'/leader_tier_`personnel_definition'/fe`fe_level'"
                        cap mkdir "`project_path'/csv/ddd_atcsharing_req2_fe1_window_event_control/`roster_variant'/leader_tier_`personnel_definition'/fe`fe_level'/`atc'"
                        cap mkdir "`project_path'/logs/ddd_atcsharing_req2_fe1_window_event_control/`roster_variant'"
                        cap mkdir "`project_path'/logs/ddd_atcsharing_req2_fe1_window_event_control/`roster_variant'/leader_tier_`personnel_definition'"
                        cap mkdir "`project_path'/logs/ddd_atcsharing_req2_fe1_window_event_control/`roster_variant'/leader_tier_`personnel_definition'/fe`fe_level'"
                        cap mkdir "`project_path'/logs/ddd_atcsharing_req2_fe1_window_event_control/`roster_variant'/leader_tier_`personnel_definition'/fe`fe_level'/`atc'"
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

                                    // Each parallel regression shard gets its own
                                    // short cache directory to prevent concurrent
                                    // Stata writes to the same stacked-price file.
                                    local cache_namespace : environment BOARDPHARMA_CACHE_NAMESPACE
                                    if `"`cache_namespace'"' == "" local cache_namespace "default"
                                    local cache_root "`variant_root'/cache_`cache_namespace'"
                                    cap mkdir "`cache_root'"
                                    local cache_file "`cache_root'/`panel_group_folder'_`event'_req`req'_`control_name'_cohort_atc3_stacked.dta"

                                    foreach window_event_control of local window_event_controls {
                                    local window_event_label without_window_other_event
                                    local event_control_spec ""
                                    if `window_event_control' == 1 {
                                        local window_event_label with_window_other_event
                                        local event_control_spec "c.window_other_event"
                                    }
                                    foreach target of local targets {
                                    foreach control_kappa of local kappa_controls {
                                        local kappa_var kappa_raw_pre
                                        if "`control_kappa'" == "kappa_norm" local kappa_var kappa_norm_pre
                                        if "`control_kappa'" == "kappa_norm_p95" local kappa_var kappa_norm_p95_pre
                                        if "`control_kappa'" == "kappa_asy_p95" local kappa_var kappa_raw_p95_pre
                                        if "`control_kappa'" == "none" local kappa_var ""
                                        local output_file "`output_root'/`event'_req`req'_`control_name'_`window_event_label'_`control_kappa'_`target'.csv"
                                        local log_file "`log_root'/`event'_req`req'_`control_name'_`window_event_label'_`control_kappa'_`target'.log"
                                        cap log close
                                        log using "`log_file'", text replace

                                        tempfile run_results
                                        if `fe_level' == 1 & "`control_kappa'" == "kappa_asy" cap erase "`cache_file'"
                                        // The raw cache can already exist when the
                                        // second Kappa specification reuses it. Keep
                                        // the cohort count defined in both branches.
                                        local n_cohorts : word count `cohort_files'
                                        capture confirm file "`cache_file'"
                                        if _rc {
                                            tempfile master
                                            local first_file 1
                                            local n_cohorts 0

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
                                            merge m:1 boardname year using `size_map', keep(master match) nogen
                                            merge m:1 boardname using `state_map', keep(master match) nogen

                                            egen id = group(boardname product data_cohort)
                                            egen board_cluster = group(boardname)
                                            gen byte treat = treated_in_stack
                                            gen byte post = `period_var' >= event_period
                                            gen byte did = treat * post
                                            gen byte did_g = did * atc_sharing
                                            gen byte pre_period = `period_var' < event_period
                                            save "`cache_file'", replace
                                        }

                                        use "`cache_file'", clear

                                        // Apply the outcome-specific transformation after
                                        // loading the shared raw-price cache.
                                        if "`outlier_treatment'" == "winsorize" {
                                            quietly summarize `target', detail
                                            replace `target' = r(`outlier_percentile') if `target' > r(`outlier_percentile') & !missing(`target')
                                        }
                                        else if "`outlier_treatment'" == "trim" {
                                            quietly summarize `target', detail
                                            drop if `target' > r(`outlier_percentile') & !missing(`target')
                                        }
                                        replace `target' = log(`target')

                                        // No observation is excluded for missing Kappa.
                                        local missing_kappa = 0
                                        if `fe_level' == 2 drop if missing(size_quintile) | missing(state)

                                        egen product_cohort_fe = group(product data_cohort)
                                        egen cohort_period_fe = group(data_cohort `period_var')
                                        local fe_spec "product_cohort_fe cohort_period_fe"
                                        if `fe_level' == 2 {
                                            egen size_cohort_period_fe = group(size_quintile data_cohort `period_var')
                                            egen state_cohort_period_fe = group(state data_cohort `period_var')
                                            local fe_spec "`fe_spec' size_cohort_period_fe state_cohort_period_fe"
                                        }

                                        local beta0 .
                                        local beta0_se .
                                        local beta0_p .
                                        local beta1 .
                                        local beta1_se .
                                        local beta1_p .
                                        local share_effect .
                                        local share_effect_se .
                                        local share_effect_p .
                                        local n_obs .
                                        // Bound the alternating-projection solver so a
                                        // degenerate FE cell returns a recorded result
                                        // instead of holding the complete grid forever.
                                        local ddd_controls "`event_control_spec'"
                                        // The outcome model has no Kappa regressor.
                                        capture noisily reghdfe `target' did did_g `ddd_controls', absorb(`fe_spec') vce(cluster board_cluster) tol(1e-8) iterate(1000)
                                        local reghdfe_rc = _rc
                                        tempvar estimation_sample
                                        gen byte `estimation_sample' = 0
                                        if `reghdfe_rc' == 0 {
                                            replace `estimation_sample' = e(sample)
                                            local n_obs = e(N)
                                            quietly lincom did
                                            local beta0 = r(estimate)
                                            local beta0_se = r(se)
                                            local beta0_p = r(p)
                                            quietly lincom did_g
                                            local beta1 = r(estimate)
                                            local beta1_se = r(se)
                                            local beta1_p = r(p)
                                            quietly lincom did + did_g
                                            local share_effect = r(estimate)
                                            local share_effect_se = r(se)
                                            local share_effect_p = r(p)
                                        }

                                        if `fast_ddd_counts' {
                                            // The regression sample size remains exact; only the
                                            // secondary unit-count diagnostics are omitted here.
                                            local n_control = .
                                            local n_notshare = .
                                            local n_share = .
                                            local n_treated_firms = .
                                            local n_control_firms = .
                                            local n_treated_products = .
                                            local n_control_products = .
                                        }
                                        else {
                                            quietly count if `estimation_sample' == 1 & treat == 0
                                            local n_control = r(N)
                                            quietly count if `estimation_sample' == 1 & treat == 1 & atc_sharing == 0
                                            local n_notshare = r(N)
                                            quietly count if `estimation_sample' == 1 & treat == 1 & atc_sharing == 1
                                            local n_share = r(N)
                                            tempvar tag_treated_firm tag_control_firm tag_treated_product tag_control_product
                                            egen `tag_treated_firm' = tag(boardname) if `estimation_sample' == 1 & treat == 1
                                            egen `tag_control_firm' = tag(boardname) if `estimation_sample' == 1 & treat == 0
                                            egen `tag_treated_product' = tag(boardname product) if `estimation_sample' == 1 & treat == 1
                                            egen `tag_control_product' = tag(boardname product) if `estimation_sample' == 1 & treat == 0
                                            quietly count if `tag_treated_firm' == 1
                                            local n_treated_firms = r(N)
                                            quietly count if `tag_control_firm' == 1
                                            local n_control_firms = r(N)
                                            quietly count if `tag_treated_product' == 1
                                            local n_treated_products = r(N)
                                            quietly count if `tag_control_product' == 1
                                            local n_control_products = r(N)
                                        }

                                        tempname posth
                                        postfile `posth' ///
                                             str32 roster_variant str16 personnel_definition str12 panel_level ///
                                            str12 atc str35 panel_group str30 event str16 control str8 outcome double req fe_level ///
                                            str16 kappa_control byte window_event_control double beta0 beta0_se beta0_p beta1 beta1_se beta1_p ///
                                            double share_effect share_effect_se share_effect_p n_obs n_control n_notshare n_share ///
                                            n_treated_firms n_control_firms n_treated_products n_control_products ///
                                            missing_kappa n_cohorts using `run_results', replace
                                        post `posth' ("`roster_variant'") ("`personnel_definition'") ("`panel_level'") ///
                                            ("`atc'") ("`panel_group_folder'") ("`event'") ("`control_name'") ("`target'") (`req') (`fe_level') ///
                                            ("`control_kappa'") (`window_event_control') (`beta0') (`beta0_se') (`beta0_p') (`beta1') (`beta1_se') (`beta1_p') ///
                                            (`share_effect') (`share_effect_se') (`share_effect_p') (`n_obs') (`n_control') ///
                                            (`n_notshare') (`n_share') (`n_treated_firms') (`n_control_firms') ///
                                            (`n_treated_products') (`n_control_products') (`missing_kappa') (`n_cohorts')
                                        postclose `posth'
                                        use `run_results', clear
                                        export delimited using "`output_file'", replace
                                        log close
                                    }
                                    }
                                    }
                                    // Remove the shared raw-price cache after both
                                    // outcomes and both Kappa specifications use it.
                                    cap erase "`cache_file'"
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

