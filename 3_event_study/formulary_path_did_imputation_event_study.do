// ================================================================
// Purpose:
// Estimate direction-specific formulary-by-drug did_imputation event studies
// from complete-history path-average panels.
//
// Process:
// 1. Read each event-direction's selected cohort-average CSVs. First-seen
//    eligibility has already been applied by PathAvgPanelMaker.py.
// 2. Select A or B through sample_a/b and freeze event-specific ATC3 sharing
//    at the cohort anchor quarter for the full stacked unit history.
// 3. Build panel-specific NDC identifiers; winsorize average copay at p95.
// 4. Run did_imputation with hetby(ATC3 sharing), firm clustering, horizons
//    0/7, and three pretrend coefficients. Upstream averages run without
//    additional Stata weights.
// 5. Export dynamic coefficients, autosample statistics, logs, and figures.
//
// Input:
// - data/formulary_path_cohort_data_quarter/event/req1/Not/
//   shift_q{0|1}/plan/seen_y{offset}_event_q/path_average/
//   {event}_path_quarter_cohort_{year}Q{quarter}.csv
// - no_spillover=1 instead reads direction-specific files from
//   seen_y{offset}_event_q/path_average_no_spillover/{A|B}/.
//
// Output:
// - csv/formulary_path/es/{sample}/{spec}/dynamic.csv
// - csv/formulary_path/es/{sample}/{spec}/samples.csv
// - csv/formulary_path/es/{sample}/{spec}/cohort_samples.csv
// - figures/formulary_path/es/{sample}/{spec}/{event}/{side}/{target}/plot.png
// - logs/formulary_path/es/{sample}/{spec}/{event}/{side}/{target}/run.log
// ================================================================

version 18.0
clear all
set more off
set trace off

* ========================== USER CONFIG ==========================
* events:
* - Movement-event families to estimate. Every name must match the upstream
*   PathAvgPanelMaker filename and event-column conventions below.
local events interlock_dissolution to_B_still_in_A to_B_not_in_A

* treatment_groups:
* - Directions to estimate and their order. Allowed values are A and B.
local treatment_groups B A

* targets:
* - Any subset of the six upstream path-average outcomes is allowed:
*   included, tier_raw, and avg_copay_amt, each with wted/non_wted versions.
local targets included_wted included_non_wted tier_raw_wted tier_raw_non_wted avg_copay_amt_wted avg_copay_amt_non_wted

* exclude:
* - 0 keeps every upstream drug unit.
* - 1, for included outcomes only, drops a company-NDC-cohort unit when its
*   selected included outcome equals zero in any cohort-window quarter.
local exclude 1

* no_spillover:
* - 0 reads the combined baseline path_average directory.
* - 1 reads direction-specific A/B path_average_no_spillover directories.
* - This switch is independent of exclude; all four combinations are valid.
local no_spillover 1

* Input-build identifiers:
* - These settings select an existing PathAvgPanelMaker build. They do not
*   recreate or re-filter the Python data inside Stata.
* - analysis_level has only one allowed value: plan. State and county path
*   averages are not implemented and will cause the validation below to stop.
local analysis_level plan
local formulary_time_shift_quarters 1
local first_seen_year_offset -1
local req 1
local control not
local include_eventpair 0
local personnel_definition narrow
local large_sample 1

* Outcome transformation:
* - Average copay is winsorized at its unweighted p95. The validation below
*   currently requires this pair of settings.
local outlier_treatment winsorize
local outlier_percentile p95

* Regression specification:
* - atc, fe_level, and cluster_level are validated against the maintained
*   design. did_horizons and did_pretrends determine reported event time.
local atc atc3
local fe_level 1
local cluster_level firm
local did_horizons 0/7
local did_pretrends 3

* Plotting and coefficient layout:
* - If horizons or pretrends change, update coef_names, n_total, trimlead,
*   trimlag, and xlabel_spec together.
local coef_names pre_3 pre_2 pre_1 post_0 post_1 post_2 post_3 post_4 post_5 post_6 post_7
local n_total 11
local trimlead 3
local trimlag 7
local xlabel_spec -3(1)7
local graph_width 3000
local perturb_step 0.15
local perturb_span 0.30

* Optional command-line overrides:
* - Running "do <this-file> 0 1" overrides no_spillover=0 and exclude=1.
* - Omit both arguments to use the USER CONFIG values above.
* - The first argument is no_spillover; the second argument is exclude.
args no_spillover_override exclude_override
if "`no_spillover_override'" != "" {
    capture confirm number `no_spillover_override'
    if _rc {
        di as error "The no_spillover override must be numeric 0 or 1."
        exit 198
    }
    local no_spillover = real("`no_spillover_override'")
}
if "`exclude_override'" != "" {
    capture confirm number `exclude_override'
    if _rc {
        di as error "The exclude override must be numeric 0 or 1."
        exit 198
    }
    local exclude = real("`exclude_override'")
}
* ================================================================

foreach target of local targets {
    if !inlist("`target'", ///
        "included_wted", "included_non_wted", ///
        "tier_raw_wted", "tier_raw_non_wted", ///
        "avg_copay_amt_wted", "avg_copay_amt_non_wted") {
        di as error "Path-average files require an outcome ending in _wted or _non_wted."
        exit 198
    }
}

if `large_sample' != 1 | "`personnel_definition'" != "narrow" {
    di as error "Formulary regressions require large_sample=1 and personnel_definition=narrow."
    exit 198
}
if `req' != 1 | "`control'" != "not" | `include_eventpair' != 0 {
    di as error "Formulary regressions require req1, Not controls, and include_eventpair=0."
    exit 198
}
if "`analysis_level'" != "plan" {
    di as error "PathAvgPanelMaker outputs require analysis_level=plan."
    exit 198
}
if "`outlier_treatment'" != "winsorize" | "`outlier_percentile'" != "p95" {
    di as error "Average copay requires upper-tail p95 winsorization."
    exit 198
}
if `fe_level' != 1 | "`cluster_level'" != "firm" | "`atc'" != "atc3" {
    di as error "This do-file requires FE1, firm clustering, and ATC3 sharing."
    exit 198
}
if !inlist(`exclude', 0, 1) {
    di as error "exclude must be 0 or 1."
    exit 198
}
if !inlist(`no_spillover', 0, 1) {
    di as error "no_spillover must be 0 or 1."
    exit 198
}
if !inlist(`first_seen_year_offset', -1, 0, 1) {
    di as error "first_seen_year_offset must identify the upstream rule."
    exit 198
}

* ================= paths =================
local code_path "`c(pwd)'"
local parent_path = regexr("`code_path'", "[/\\][^/\\]+$", "")
local project_path = regexr("`parent_path'", "[/\\][^/\\]+$", "")
local project_path = subinstr("`project_path'", "\", "/", .)

local seen_offset "`first_seen_year_offset'"
if `first_seen_year_offset' >= 0 {
    local seen_offset "+`first_seen_year_offset'"
}
local first_seen_label "seen_y`seen_offset'_event_q"
local data_root "`project_path'/data/formulary_path_cohort_data_quarter/event/req1/Not/shift_q`formulary_time_shift_quarters'/`analysis_level'/`first_seen_label'"
local data_path "`data_root'/path_average"
if `no_spillover' == 1 {
    local data_path "`data_root'/path_average_no_spillover"
}
local panel_suffix "_path_avg_`analysis_level'"
local panel_label "`analysis_level'_path_avg_ndc"
local panel_title "mean"
local import_stringcols "4"

* Do not estimate while PathAvgPanelMaker is still appending quarters.
* A new START after an older COMPLETE resets completion status.
local path_build_log "`data_path'/_path_avg_build.log"
capture confirm file "`path_build_log'"
if _rc {
    di as error "Missing path-average build log: `path_build_log'"
    exit 601
}
tempname path_build_handle
file open `path_build_handle' using "`path_build_log'", read text
local path_build_complete 0
file read `path_build_handle' path_build_line
while r(eof) == 0 {
    if strpos("`path_build_line'", "START fresh path-average build") {
        local path_build_complete 0
    }
    if strpos("`path_build_line'", "COMPLETE all path-average quarters") {
        local path_build_complete 1
    }
    file read `path_build_handle' path_build_line
}
file close `path_build_handle'
if `path_build_complete' != 1 {
    di as error "Path-average build is incomplete: `path_build_log'"
    exit 459
}

* First-seen eligibility is fixed upstream in PathAvgPanelMaker.py for both
* treated-share pool construction and final NDC output. Do not re-filter here.
local sample_spec "shift_q`formulary_time_shift_quarters'_`first_seen_label'_quarter_event"
local spec_folder "req1_not_wsp95_fe`fe_level'_firm`panel_suffix'"
if `no_spillover' == 1 {
    local spec_folder "`spec_folder'_no_spillover"
}
if `exclude' == 1 {
    local spec_folder "`spec_folder'_exclude_anyzero"
}
local csv_root "`project_path'/csv/formulary_path/es"
local figure_root "`project_path'/figures/formulary_path/es"
local log_root "`project_path'/logs/formulary_path/es"
local csv_run_root "`csv_root'/`sample_spec'/`spec_folder'"
local figure_run_root "`figure_root'/`sample_spec'/`spec_folder'"
local log_run_root "`log_root'/`sample_spec'/`spec_folder'"
cap mkdir "`project_path'/csv"
cap mkdir "`project_path'/csv/formulary_path"
cap mkdir "`project_path'/figures"
cap mkdir "`project_path'/figures/formulary_path"
cap mkdir "`project_path'/logs"
cap mkdir "`project_path'/logs/formulary_path"
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

tempname coefficient_post sample_post cohort_sample_post
tempfile coefficient_data sample_data cohort_sample_data
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
postfile `cohort_sample_post' ///
    str32 event str1 direction str24 target str12 scope str6 cohort ///
    long obs_total obs_control obs_treated obs_treated_notshare obs_treated_share ///
    long ids_total ids_control ids_treated ids_treated_notshare ids_treated_share ///
    using `cohort_sample_data', replace

* ================= estimation loop =================
local retained_outcomes `targets'
local retained_outcomes : list uniq retained_outcomes
local cohort_required_vars "data_cohort_year data_cohort_quarter data_cohort_qtime"
local source_company_var "id"
foreach event of local events {
    if "`event'" == "to_B_not_in_A" {
        local imported_share_a "event_to_b_not_in_a_a_sharingatc"
        local imported_share_b "event_to_b_not_in_a_b_sharingatc"
        local other_event_list_a ///
            "event_to_b_still_in_a_a event_interlock_dissolution_a"
        local other_event_list_b ///
            "event_to_b_still_in_a_b event_interlock_dissolution_b"
    }
    else if "`event'" == "to_B_still_in_A" {
        local imported_share_a "event_to_b_still_in_a_a_sharinga"
        local imported_share_b "event_to_b_still_in_a_b_sharinga"
        local other_event_list_a ///
            "event_to_b_not_in_a_a event_interlock_dissolution_a"
        local other_event_list_b ///
            "event_to_b_not_in_a_b event_interlock_dissolution_b"
    }
    else if "`event'" == "interlock_dissolution" {
        local imported_share_a "event_interlock_dissolution_a_sh"
        local imported_share_b "event_interlock_dissolution_b_sh"
        local other_event_list_a ///
            "event_to_b_not_in_a_a event_to_b_still_in_a_a"
        local other_event_list_b ///
            "event_to_b_not_in_a_b event_to_b_still_in_a_b"
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

    foreach treatment_group of local treatment_groups {
        local side = lower("`treatment_group'")
        local sample_var "sample_`side'"
        local treated_var "treated_`side'"
        local sharing_source "cohort_sharing_`side'"
        local other_event_list "`other_event_list_`side''"
        local direction_data_path "`data_path'"
        if `no_spillover' == 1 {
            local direction_data_path "`data_path'/`treatment_group'"
        }

        * Discover only files actually generated for this direction. A file
        * can contain controls but no treated rows for the selected direction;
        * such a cohort does not enter that direction's regression stack.
        local cohort_files ""
        capture local cohort_files : dir "`direction_data_path'" files ///
            "`event'_path_quarter_cohort_*.csv"
        local cohort_dir_rc = _rc
        if `cohort_dir_rc' != 0 {
            di as text "Skipping `event' side `treatment_group': upstream direction directory is absent."
            continue
        }
        if `"`cohort_files'"' == "" {
            di as text "Skipping `event' side `treatment_group': no upstream cohort-average files."
            continue
        }
        local cohort_list ""
        foreach cohort_file of local cohort_files {
            if regexm("`cohort_file'", ///
                "_cohort_([0-9][0-9][0-9][0-9][Qq][1-4])[.]csv$") {
                local cohort_tag = upper(regexs(1))
                local cohort_list "`cohort_list' `cohort_tag'"
            }
        }
        local cohort_list : list uniq cohort_list
        if "`cohort_list'" == "" {
            di as text "Skipping `event' side `treatment_group': no valid quarterly cohort filenames."
            continue
        }

        local cohort_part_count 0
        local first_part ""
        local append_parts ""
        foreach cohort of local cohort_list {
            local data_file ///
                "`direction_data_path'/`event'_path_quarter_cohort_`cohort'.csv"
            capture confirm file "`data_file'"
            if _rc {
                di as error "Discovered cohort file is missing: `data_file'"
                exit 601
            }

            import delimited "`data_file'", clear varnames(1) case(lower) ///
                stringcols(`import_stringcols')
            foreach required in ndc `source_company_var' year quarter ///
                `cohort_required_vars' treated_a treated_b sample_a sample_b ///
                `other_event_list_a' `other_event_list_b' ///
                `imported_share_a' `imported_share_b' `retained_outcomes' {
                capture confirm variable `required'
                if _rc {
                    di as error "Missing variable `required' in `data_file'"
                    exit 111
                }
            }
            rename `source_company_var' companyid
            rename `imported_share_a' cohort_sharing_a
            rename `imported_share_b' cohort_sharing_b
            foreach cohort_var in data_cohort_year data_cohort_quarter data_cohort_qtime {
                capture confirm numeric variable `cohort_var'
                if _rc destring `cohort_var', replace
            }
            gen long data_cohort = data_cohort_qtime
            foreach numeric_var in year quarter data_cohort ///
                data_cohort_year data_cohort_quarter data_cohort_qtime ///
                treated_a treated_b sample_a sample_b ///
                `other_event_list_a' `other_event_list_b' ///
                cohort_sharing_a cohort_sharing_b `retained_outcomes' {
                capture confirm numeric variable `numeric_var'
                if _rc destring `numeric_var', replace
            }
            foreach binary_var in treated_a treated_b sample_a sample_b ///
                `other_event_list_a' `other_event_list_b' ///
                cohort_sharing_a cohort_sharing_b {
                assert inlist(`binary_var', 0, 1)
            }
            assert !missing(included_wted) & inrange(included_wted, 0, 1)
            assert !missing(included_non_wted) & inrange(included_non_wted, 0, 1)
            assert data_cohort_year == real(substr("`cohort'", 1, 4))
            assert data_cohort_quarter == real(substr("`cohort'", 6, 1))
            assert data_cohort == 4 * data_cohort_year + data_cohort_quarter
            assert data_cohort_qtime == data_cohort

            keep ndc companyid year quarter data_cohort ///
                data_cohort_year data_cohort_quarter data_cohort_qtime ///
                treated_a treated_b sample_a sample_b ///
                `other_event_list_a' `other_event_list_b' ///
                cohort_sharing_a cohort_sharing_b `retained_outcomes'
            keep if `sample_var' == 1
            quietly count if `treated_var' == 1
            if r(N) == 0 {
                di as text "Skipping cohort file with no treated rows: `data_file'"
                continue
            }

            * Exclusion is outcome-specific and direction-specific. It neither
            * changes nor is changed by the no-spillover path-pool setting.
            if `exclude' == 1 {
                foreach outcome of local targets {
                    if strpos("`outcome'", "included") == 1 {
                        local eligibility_var "__eligible_`outcome'"
                        bysort companyid ndc data_cohort: egen byte `eligibility_var' = ///
                            min(`outcome' > 0)
                        assert inlist(`eligibility_var', 0, 1)
                    }
                }
            }
            compress

            local ++cohort_part_count
            tempfile cohort_part
            save `cohort_part', replace
            if `cohort_part_count' == 1 {
                local first_part "`cohort_part'"
            }
            else {
                local append_parts `"`append_parts' "`cohort_part'""'
            }
        }
        if `cohort_part_count' == 0 {
            di as text "Skipping `event' side `treatment_group': no cohort has treated rows."
            continue
        }
        use "`first_part'", clear
        if `cohort_part_count' > 1 {
            append using `append_parts'
        }
        assert `sample_var' == 1
        compress
        tempfile event_master
        save `event_master', replace

        foreach target of local targets {
            local included_target = strpos("`target'", "included") == 1
            local figure_dir "`figure_run_root'/`event_folder'/`treatment_group'/`target'"
            local log_dir "`log_run_root'/`event_folder'/`treatment_group'/`target'"
            cap mkdir "`figure_run_root'/`event_folder'"
            cap mkdir "`log_run_root'/`event_folder'"
            cap mkdir "`figure_run_root'/`event_folder'/`treatment_group'"
            cap mkdir "`log_run_root'/`event_folder'/`treatment_group'"
            cap mkdir "`figure_dir'"
            cap mkdir "`log_dir'"
            capture log close
            log using "`log_dir'/run.log", replace text
            di as text "Formulary dynamic did_imputation: `event', side `treatment_group', target `target', level `panel_label'"

            use `event_master', clear
            if `included_target' {
                if `exclude' == 1 {
                    local eligibility_var "__eligible_`target'"
                    capture confirm variable `eligibility_var'
                    if _rc {
                        di as error "Missing exclusion flag `eligibility_var'."
                        log close
                        exit 111
                    }
                    keep if `eligibility_var' == 1
                }
            }
            else {
                * PathAvgPanelMaker emits each intensive outcome either in every
                * panel quarter or in none. Verify that invariant and discard
                * all-missing units that cannot enter the regression.
                bysort companyid ndc data_cohort: egen byte __target_all = ///
                    min(!missing(`target'))
                bysort companyid ndc data_cohort: egen byte __target_any = ///
                    max(!missing(`target'))
                assert __target_all == __target_any
                keep if __target_all == 1
                drop __target_all __target_any
                assert !missing(`target')
            }
            assert `sample_var' == 1
            gen byte treated_in_stack = `treated_var'
            count
            if r(N) == 0 {
                di as error "No observations remain after direction-specific sample filtering."
                log close
                continue
            }

            * Freeze the event-specific sharing classification at the cohort anchor,
            * exactly as in the existing formulary cohort construction.
            tempvar q1_share_min q1_share_max
            bysort ndc companyid data_cohort: egen byte `q1_share_min' = ///
                min(cond(year == data_cohort_year & quarter == data_cohort_quarter, `sharing_source', .))
            bysort ndc companyid data_cohort: egen byte `q1_share_max' = ///
                max(cond(year == data_cohort_year & quarter == data_cohort_quarter, `sharing_source', .))
            assert !missing(`q1_share_min') & `q1_share_min' == `q1_share_max'
            gen byte atc_sharing = `q1_share_max'
            replace atc_sharing = 0 if treated_in_stack == 0
            drop `q1_share_min' `q1_share_max'

            gen int rel_quarter_all = ///
                yq(year, quarter) - yq(data_cohort_year, data_cohort_quarter)
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

            quietly summarize `target', detail
            if r(N) == 0 {
                di as error "Target `target' is missing for every stacked observation."
                log close
                continue
            }
            if strpos("`target'", "avg_copay_amt") == 1 {
                * Copay alone is winsorized at its unweighted P95.
                quietly summarize `target', detail
                local p95_value = r(p95)
                replace `target' = `p95_value' if ///
                    `target' > `p95_value' & !missing(`target')
            }

            gen int q_time = yq(year, quarter)
            format q_time %tq
            assert inrange(quarter, 1, 4)
            assert !missing(ndc) & !missing(companyid)
            bysort ndc companyid data_cohort: ///
                assert treated_in_stack == treated_in_stack[1]
            bysort ndc companyid data_cohort: ///
                assert atc_sharing == atc_sharing[1]
            bysort ndc data_cohort companyid: gen byte company_tag = _n == 1
            bysort ndc data_cohort: egen int company_count = total(company_tag)
            assert company_count == 1
            drop company_tag company_count
            egen long id = group(companyid ndc data_cohort)
            isid id q_time

            bysort data_cohort: egen int window_start = min(q_time)
            bysort data_cohort: egen int window_end = max(q_time)
            assert window_start >= yq(data_cohort_year, data_cohort_quarter) - 4
            assert window_end <= yq(data_cohort_year, data_cohort_quarter) + 7
            bysort id (q_time): assert _N == window_end[1] - window_start[1] + 1
            bysort id (q_time): assert q_time == window_start[1] + _n - 1
            drop window_start window_end

            gen byte treated = treated_in_stack
            gen int event_cohort_q = yq(data_cohort_year, data_cohort_quarter) if treated == 1
            format event_cohort_q %tq
            gen byte pre_period = q_time < yq(data_cohort_year, data_cohort_quarter)
            bysort id (q_time): assert treated == treated[1]
            bysort id (q_time): assert atc_sharing == atc_sharing[1]

            * Record the exact stacked sample immediately before did_imputation.
            * Observation counts are panel rows. ID counts are unique
            * company-NDC-cohort units; treated-share is a subset of treated.
            tempvar pre_id_tag pre_control_tag pre_treated_tag ///
                pre_notshare_tag pre_share_tag
            egen byte `pre_id_tag' = tag(id)
            egen byte `pre_control_tag' = tag(id) if treated == 0
            egen byte `pre_treated_tag' = tag(id) if treated == 1
            egen byte `pre_notshare_tag' = tag(id) if ///
                treated == 1 & atc_sharing == 0
            egen byte `pre_share_tag' = tag(id) if ///
                treated == 1 & atc_sharing == 1

            levelsof data_cohort, local(regression_cohorts)
            foreach regression_cohort of local regression_cohorts {
                quietly summarize data_cohort_year if ///
                    data_cohort == `regression_cohort', meanonly
                local report_cohort_year = r(min)
                quietly summarize data_cohort_quarter if ///
                    data_cohort == `regression_cohort', meanonly
                local report_cohort_quarter = r(min)
                local report_cohort ///
                    "`report_cohort_year'Q`report_cohort_quarter'"

                quietly count if data_cohort == `regression_cohort'
                local pre_obs_total = r(N)
                quietly count if data_cohort == `regression_cohort' & treated == 0
                local pre_obs_control = r(N)
                quietly count if data_cohort == `regression_cohort' & treated == 1
                local pre_obs_treated = r(N)
                quietly count if data_cohort == `regression_cohort' & ///
                    treated == 1 & atc_sharing == 0
                local pre_obs_notshare = r(N)
                quietly count if data_cohort == `regression_cohort' & ///
                    treated == 1 & atc_sharing == 1
                local pre_obs_share = r(N)

                quietly count if data_cohort == `regression_cohort' & ///
                    `pre_id_tag' == 1
                local pre_ids_total = r(N)
                quietly count if data_cohort == `regression_cohort' & ///
                    `pre_control_tag' == 1
                local pre_ids_control = r(N)
                quietly count if data_cohort == `regression_cohort' & ///
                    `pre_treated_tag' == 1
                local pre_ids_treated = r(N)
                quietly count if data_cohort == `regression_cohort' & ///
                    `pre_notshare_tag' == 1
                local pre_ids_notshare = r(N)
                quietly count if data_cohort == `regression_cohort' & ///
                    `pre_share_tag' == 1
                local pre_ids_share = r(N)

                post `cohort_sample_post' ///
                    ("`event'") ("`treatment_group'") ("`target'") ///
                    ("cohort") ("`report_cohort'") ///
                    (`pre_obs_total') (`pre_obs_control') (`pre_obs_treated') ///
                    (`pre_obs_notshare') (`pre_obs_share') ///
                    (`pre_ids_total') (`pre_ids_control') (`pre_ids_treated') ///
                    (`pre_ids_notshare') (`pre_ids_share')
            }

            quietly count
            local pre_obs_total = r(N)
            quietly count if treated == 0
            local pre_obs_control = r(N)
            quietly count if treated == 1
            local pre_obs_treated = r(N)
            quietly count if treated == 1 & atc_sharing == 0
            local pre_obs_notshare = r(N)
            quietly count if treated == 1 & atc_sharing == 1
            local pre_obs_share = r(N)
            quietly count if `pre_id_tag' == 1
            local pre_ids_total = r(N)
            quietly count if `pre_control_tag' == 1
            local pre_ids_control = r(N)
            quietly count if `pre_treated_tag' == 1
            local pre_ids_treated = r(N)
            quietly count if `pre_notshare_tag' == 1
            local pre_ids_notshare = r(N)
            quietly count if `pre_share_tag' == 1
            local pre_ids_share = r(N)
            post `cohort_sample_post' ///
                ("`event'") ("`treatment_group'") ("`target'") ///
                ("stack_total") ("ALL") ///
                (`pre_obs_total') (`pre_obs_control') (`pre_obs_treated') ///
                (`pre_obs_notshare') (`pre_obs_share') ///
                (`pre_ids_total') (`pre_ids_control') (`pre_ids_treated') ///
                (`pre_ids_notshare') (`pre_ids_share')
            drop `pre_id_tag' `pre_control_tag' `pre_treated_tag' ///
                `pre_notshare_tag' `pre_share_tag'

            local other_history_list ""
            foreach other_event of local other_event_list {
                capture confirm variable `other_event'
                if _rc {
                    di as error "Missing same-direction other-event variable: `other_event'"
                    log close
                    exit 111
                }
                tempvar first_other_q other_history
                bysort companyid data_cohort: egen `first_other_q' = ///
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
            gen byte atc_sharing_het = atc_sharing if treated == 1
            replace atc_sharing_het = 0 if treated == 0

            capture noisily did_imputation `target' id q_time event_cohort_q, ///
                fe(`fe_spec') ///
                horizons(`did_horizons') pretrends(`did_pretrends') ///
                hetby(atc_sharing_het) autosample tol(0.1) minn(0) ///
                cluster(companyid)
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
                egen byte `firm_tag' = tag(companyid) if `did_sample' & `condition'
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
                    title("`event_name' event Not `panel_title' | `group_label'`cv_title'`kappa_title'", ///
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
postclose `cohort_sample_post'

use `coefficient_data', clear
sort event direction target sharing rel_quarter
export delimited using "`csv_run_root'/dynamic.csv", replace

use `sample_data', clear
sort event direction target
export delimited using "`csv_run_root'/samples.csv", replace

use `cohort_sample_data', clear
sort event direction target scope cohort
export delimited using "`csv_run_root'/cohort_samples.csv", replace

di as result "Saved formulary dynamic did_imputation outputs."
