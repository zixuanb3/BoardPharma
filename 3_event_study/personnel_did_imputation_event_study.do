clear all
set more off
set trace off

// ================================================================
// Purpose:
// Run stacked did_imputation event-study regressions for
// firm-pair-product-quarter personnel panels.
//
// Process:
// 1. Loop over personnel definitions, event types, control sets,
//    treatment groups, balanced-panel filters, FE specifications, and targets.
// 2. Import 2009-2017 cohort CSVs and stack available cohorts.
// 3. Build log outcome, unit/time/cohort variables, D_confounded, and group4.
// 4. Estimate did_imputation with hetby(group4).
// 5. Export group4 x event-time coefficient CSVs and event_plot figures.
//
// Input:
// - data/personnel_regression_panels/{definition}/retain3yr/{event_type}/
//   {control_set}/treatment_group_{A|B}/reg_panel_cohort_{cohort}_tg{A|B}.csv
//
// Output:
// - figures/personnel_did_imputation/...
// - csv/personnel_did_imputation/...
// - logs/personnel_did_imputation/...
// ================================================================

* ================= user config =================
local definitions narrow_board medium_board_csuite broad_board_c_vp
* narrow_board medium_board_csuite broad_board_c_vp
local event_types to_B_still_in_A to_B_not_in_A dissolution
local control_sets C6A
* C1A C1B C4 C6B C6A
local treatment_groups A B
* A B
local targets price1
local standardize_types log_transform
local fe_levels 2
* 1 2
local balanced_only_values 0
* 1
local cohort_list 2009 2010 2011 2012 2013 2014 2015 2016 2017

local did_horizons 0/11
local did_pretrend 6
local coef_names pre_6 pre_5 pre_4 pre_3 pre_2 pre_1 post_0 post_1 post_2 post_3 post_4 post_5 post_6 post_7 post_8 post_9 post_10 post_11
local n_total 18
local trimlead 6
local trimlag 11
local xlabel_spec "-6(1)11"
local graph_width 3000
local perturb_step 0.10
local perturb_span 0.30
local perturb_increment = 2 * `perturb_step'
local timevar q_time
local gvar event_cohort_q

* ================= path =================
local code_path "`c(pwd)'"
local code_path = subinstr("`code_path'", "\", "/", .)

if regexm("`code_path'", "/3_event_study$") {
    local parent_path = regexr("`code_path'", "/[^/]+$", "")
    local project_path = regexr("`parent_path'", "/[^/]+$", "")
}
else if regexm("`code_path'", "/codes$") {
    local project_path = regexr("`code_path'", "/[^/]+$", "")
}
else {
    local parent_path = regexr("`code_path'", "/[^/]+$", "")
    local project_path = regexr("`parent_path'", "/[^/]+$", "")
}

local input_root "`project_path'/data/personnel_regression_panels"
local fig_root "`project_path'/figures/personnel_did_imputation"
local csv_root "`project_path'/csv/personnel_did_imputation"
local log_root "`project_path'/logs/personnel_did_imputation"

cap mkdir "`project_path'/figures"
cap mkdir "`project_path'/csv"
cap mkdir "`project_path'/logs"
cap mkdir "`fig_root'"
cap mkdir "`csv_root'"
cap mkdir "`log_root'"

* ================= loop =================
foreach definition of local definitions {
foreach event_type of local event_types {
foreach control_set of local control_sets {
foreach treatment_group of local treatment_groups {
foreach balanced_only of local balanced_only_values {
foreach fe_level of local fe_levels {
foreach target of local targets {
foreach std of local standardize_types {

    if !inlist("`treatment_group'", "A", "B") {
        di as error "treatment_group must be one of: A, B"
        exit 198
    }
    if !inlist(`balanced_only', 0, 1) {
        di as error "balanced_only must be one of: 0, 1"
        exit 198
    }
    if !inlist(`fe_level', 1, 2) {
        di as error "fe_level must be one of: 1, 2"
        exit 198
    }
    if "`std'" != "log_transform" {
        di as error "Only log_transform is configured for this do-file."
        exit 198
    }

    * -------- setup paths for this iteration --------
    local input_path "`input_root'/`definition'/retain3yr/`event_type'/`control_set'/treatment_group_`treatment_group'"

    foreach base in "`fig_root'" "`csv_root'" "`log_root'" {
        cap mkdir "`base'/`definition'"
        cap mkdir "`base'/`definition'/`event_type'"
        cap mkdir "`base'/`definition'/`event_type'/`control_set'"
        cap mkdir "`base'/`definition'/`event_type'/`control_set'/treatment_group_`treatment_group'"
        cap mkdir "`base'/`definition'/`event_type'/`control_set'/treatment_group_`treatment_group'/balanced_`balanced_only'"
        cap mkdir "`base'/`definition'/`event_type'/`control_set'/treatment_group_`treatment_group'/balanced_`balanced_only'/fe`fe_level'"
        cap mkdir "`base'/`definition'/`event_type'/`control_set'/treatment_group_`treatment_group'/balanced_`balanced_only'/fe`fe_level'/`target'"
    }

    local fig_path "`fig_root'/`definition'/`event_type'/`control_set'/treatment_group_`treatment_group'/balanced_`balanced_only'/fe`fe_level'/`target'"
    local csv_path "`csv_root'/`definition'/`event_type'/`control_set'/treatment_group_`treatment_group'/balanced_`balanced_only'/fe`fe_level'/`target'"
    local log_path "`log_root'/`definition'/`event_type'/`control_set'/treatment_group_`treatment_group'/balanced_`balanced_only'/fe`fe_level'/`target'"
    local file_stub "`event_type'_`control_set'_tg`treatment_group'_balanced`balanced_only'_fe`fe_level'_`target'_log_hetby4"

    cap log close
    log using "`log_path'/`file_stub'.log", text replace

    di as text "============================================================"
    di as text "personnel did_imputation event-study"
    di as text "input_path=`input_path'"
    di as text "cohort_list=`cohort_list'"
    di as text "definition=`definition'"
    di as text "event_type=`event_type'"
    di as text "control_set=`control_set'"
    di as text "treatment_group=`treatment_group'"
    di as text "balanced_only=`balanced_only'"
    di as text "fe_level=`fe_level'"
    di as text "target=`target'"
    di as text "transform=`std'"
    di as text "timevar=`timevar'"
    di as text "gvar=`gvar'"
    di as text "============================================================"

    * -------- read and stack cohort data --------
    local first = 1
    local used_cohorts ""
    local skipped_cohorts ""

    foreach cohort of local cohort_list {
        local data_file "`input_path'/reg_panel_cohort_`cohort'_tg`treatment_group'.csv"

        capture confirm file "`data_file'"
        if _rc {
            di as error "Missing cohort file, skipping cohort: `data_file'"
            local skipped_cohorts "`skipped_cohorts' `cohort'"
            continue
        }

        capture noisily import delimited "`data_file'", clear varnames(1) case(preserve)
        if _rc {
            di as error "Failed to import cohort file, skipping cohort: `data_file'"
            local skipped_cohorts "`skipped_cohorts' `cohort'"
            continue
        }

        local bad_cohort 0
        foreach v in year quarter treat event_time {
            capture confirm variable `v'
            if _rc {
                di as error "Missing required variable `v' in `data_file'; skipping cohort"
                local skipped_cohorts "`skipped_cohorts' `cohort'"
                local bad_cohort 1
                continue, break
            }
            capture confirm numeric variable `v'
            if _rc {
                capture noisily destring `v', replace ignore(" ,")
                if _rc {
                    di as error "Could not destring `v' in `data_file'; skipping cohort"
                    local skipped_cohorts "`skipped_cohorts' `cohort'"
                    local bad_cohort 1
                    continue, break
                }
            }
        }
        if `bad_cohort' {
            continue
        }

        gen data_cohort = `cohort'
        gen q_time = yq(year, quarter)
        format q_time %tq
        gen event_cohort_q = yq(data_cohort, 1) if treat == 1
        replace event_cohort_q = . if treat == 0 | missing(treat)

        keep if event_time >= -6 & event_time <= 11
        count
        if r(N) == 0 {
            di as error "No rows in event window for cohort `cohort'; skipping cohort"
            local skipped_cohorts "`skipped_cohorts' `cohort'"
            continue
        }

        if `first' {
            tempfile stack
            save `stack', replace
            local first = 0
        }
        else {
            append using `stack'
            save `stack', replace
        }
        local used_cohorts "`used_cohorts' `cohort'"
    }

    di as text "used_cohorts=`used_cohorts'"
    di as text "skipped_cohorts=`skipped_cohorts'"

    if `first' {
        di as error "No cohort data were available after reading and event-window filtering. Skipping this parameter combination."
        log close
        continue
    }

    use `stack', clear

    * -------- validate and transform variables --------
    local numeric_vars year quarter treat event_time share_atc3 balanced_panel `target' ///
        pre_retain_W pre_exit_W pre_dissolved_W ///
        sameq_retain sameq_exit sameq_dissolved ///
        post_retain post_exit post_dissolved
    local bad_numeric 0

    foreach v of local numeric_vars {
        capture confirm variable `v'
        if _rc {
            di as error "Missing required variable after stacking: `v'"
            local bad_numeric 1
        }
        else {
            capture confirm numeric variable `v'
            if _rc {
                capture noisily destring `v', replace ignore(" ,")
                if _rc {
                    di as error "Could not destring required variable after stacking: `v'"
                    local bad_numeric 1
                }
            }
        }
    }

    capture confirm variable A
    if _rc {
        di as error "Missing required firm-pair variable after stacking: A"
        local bad_numeric 1
    }
    capture confirm variable B
    if _rc {
        di as error "Missing required firm-pair variable after stacking: B"
        local bad_numeric 1
    }
    capture confirm variable product
    if _rc {
        di as error "Missing required product variable after stacking: product"
        local bad_numeric 1
    }

    if `bad_numeric' {
        di as error "Skipping this parameter combination because required variables are missing or not numeric."
        log close
        continue
    }

    gen y = log(`target')

    egen id = group(A B product data_cohort)
    format q_time %tq

    egen D_confounded = rowmax( ///
        pre_retain_W pre_exit_W pre_dissolved_W ///
        sameq_retain sameq_exit sameq_dissolved ///
        post_retain post_exit post_dissolved ///
    )
    replace D_confounded = 0 if treat == 0
    replace D_confounded = 0 if missing(D_confounded)

    replace share_atc3 = 0 if missing(share_atc3)
    gen group4 = .
    replace group4 = 0 if treat == 1 & share_atc3 == 0 & D_confounded == 0
    replace group4 = 1 if treat == 1 & share_atc3 == 1 & D_confounded == 0
    replace group4 = 2 if treat == 1 & share_atc3 == 0 & D_confounded == 1
    replace group4 = 3 if treat == 1 & share_atc3 == 1 & D_confounded == 1
    replace group4 = 0 if treat == 0

    label define group4_lbl 0 "clean_nonshare" 1 "clean_share" 2 "confounded_nonshare" 3 "confounded_share", replace
    label values group4 group4_lbl

    if `balanced_only' == 1 {
        keep if balanced_panel == 1
        di as text "Applied balanced_panel == 1 filter."
    }

    count
    if r(N) == 0 {
        di as error "No rows remain after balanced-panel filtering. Skipping this parameter combination."
        log close
        continue
    }

    if `fe_level' == 1 {
        egen fe_cqt_A = group(data_cohort q_time A)
        egen fe_cqt_B = group(data_cohort q_time B)
        local fe_spec "id fe_cqt_A fe_cqt_B"
        local fe_title "cohort-quarter-by-firm A and cohort-quarter-by-firm B"
    }
    else if `fe_level' == 2 {
        egen fe_ct = group(data_cohort q_time)
        egen fe_c_A = group(data_cohort A)
        egen fe_c_B = group(data_cohort B)
        local fe_spec "id fe_ct fe_c_A fe_c_B"
        local fe_title "cohort-quarter, cohort-by-firm A, and cohort-by-firm B"
    }

    * -------- compute sample stats --------
    count if treat == 0
    local obs_control = r(N)
    count if treat == 1 & group4 == 0
    local obs_group0 = r(N)
    count if treat == 1 & group4 == 1
    local obs_group1 = r(N)
    count if treat == 1 & group4 == 2
    local obs_group2 = r(N)
    count if treat == 1 & group4 == 3
    local obs_group3 = r(N)

    tempvar tag_pair_control tag_pair_g0 tag_pair_g1 tag_pair_g2 tag_pair_g3
    egen `tag_pair_control' = tag(A B) if treat == 0
    egen `tag_pair_g0' = tag(A B) if treat == 1 & group4 == 0
    egen `tag_pair_g1' = tag(A B) if treat == 1 & group4 == 1
    egen `tag_pair_g2' = tag(A B) if treat == 1 & group4 == 2
    egen `tag_pair_g3' = tag(A B) if treat == 1 & group4 == 3

    count if `tag_pair_control' == 1
    local pairs_control = r(N)
    count if `tag_pair_g0' == 1
    local pairs_group0 = r(N)
    count if `tag_pair_g1' == 1
    local pairs_group1 = r(N)
    count if `tag_pair_g2' == 1
    local pairs_group2 = r(N)
    count if `tag_pair_g3' == 1
    local pairs_group3 = r(N)

    tempvar tag_product_g0 tag_product_g1 tag_product_g2 tag_product_g3
    egen `tag_product_g0' = tag(product) if treat == 1 & group4 == 0
    egen `tag_product_g1' = tag(product) if treat == 1 & group4 == 1
    egen `tag_product_g2' = tag(product) if treat == 1 & group4 == 2
    egen `tag_product_g3' = tag(product) if treat == 1 & group4 == 3

    count if `tag_product_g0' == 1
    local products_group0 = r(N)
    count if `tag_product_g1' == 1
    local products_group1 = r(N)
    count if `tag_product_g2' == 1
    local products_group2 = r(N)
    count if `tag_product_g3' == 1
    local products_group3 = r(N)

    di as text "sample counts:"
    di as text "obs_control=`obs_control'"
    di as text "obs_group0=`obs_group0'"
    di as text "obs_group1=`obs_group1'"
    di as text "obs_group2=`obs_group2'"
    di as text "obs_group3=`obs_group3'"
    di as text "pairs_control=`pairs_control'"
    di as text "pairs_group0=`pairs_group0'"
    di as text "pairs_group1=`pairs_group1'"
    di as text "pairs_group2=`pairs_group2'"
    di as text "pairs_group3=`pairs_group3'"
    di as text "products_group0=`products_group0'"
    di as text "products_group1=`products_group1'"
    di as text "products_group2=`products_group2'"
    di as text "products_group3=`products_group3'"

    forvalues g = 0/3 {
        local this_obs = .
        if `g' == 0 {
            local this_obs `obs_group0'
        }
        else if `g' == 1 {
            local this_obs `obs_group1'
        }
        else if `g' == 2 {
            local this_obs `obs_group2'
        }
        else if `g' == 3 {
            local this_obs `obs_group3'
        }
        if `this_obs' == 0 {
            di as result "Warning: no treated observations for group4 == `g'. did_imputation will still run; coefficients may be missing."
        }
    }

    foreach g in 0 1 2 3 {
        matrix did_b_g`g' = J(1, `n_total', .)
        matrix did_V_g`g' = J(`n_total', `n_total', 0)
        matrix colnames did_b_g`g' = `coef_names'
        matrix colnames did_V_g`g' = `coef_names'
        matrix rownames did_V_g`g' = `coef_names'
    }

    * -------- run did_imputation --------
    di as text "fixed effects: `fe_title'"
    di as text "did_imputation command:"
    di as text "did_imputation y id `timevar' `gvar', fe(`fe_spec') horizons(`did_horizons') pretrends(`did_pretrend') hetby(group4) autosample tol(0.1) minn(0)"

    capture noisily did_imputation y id `timevar' `gvar', ///
        fe(`fe_spec') ///
        horizons(`did_horizons') ///
        pretrends(`did_pretrend') ///
        hetby(group4) ///
        autosample tol(0.1) minn(0)
    local did_rc = _rc

    if `did_rc' != 0 {
        di as error "did_imputation failed: r(`did_rc'). Skipping this parameter combination."
        log close
        continue
    }

    local N_est = e(N)
    matrix did_b_full = e(b)
    matrix did_V_full = e(V)
    local did_ncol = colsof(did_b_full)
    local did_names : colnames did_b_full

    * -------- map e(b) and e(V) to four group matrices --------
    forvalues g = 0/3 {
        matrix col_map_g`g' = J(1, `n_total', 0)

        forvalues i = 1/`n_total' {
            local coef_name : word `i' of `coef_names'
            local found_col 0
            local candidate_list ""

            if substr("`coef_name'", 1, 4) == "pre_" {
                local k = substr("`coef_name'", 5, .)
                local candidate_list pre`k'_`g' pre`k'
            }
            else if substr("`coef_name'", 1, 5) == "post_" {
                local h = substr("`coef_name'", 6, .)
                local candidate_list tau`h'_`g'
            }

            foreach candidate of local candidate_list {
                if `found_col' == 0 {
                    forvalues c = 1/`did_ncol' {
                        local cn : word `c' of `did_names'
                        if "`cn'" == "`candidate'" {
                            local found_col `c'
                        }
                    }
                }
            }

            if `found_col' > 0 {
                matrix col_map_g`g'[1, `i'] = `found_col'
                matrix did_b_g`g'[1, `i'] = did_b_full[1, `found_col']
            }
            else {
                di as result "Coefficient not estimated; leaving missing: group4=`g', coef=`coef_name'"
            }
        }

        forvalues i = 1/`n_total' {
            local ci = el(col_map_g`g', 1, `i')
            if `ci' == 0 {
                continue
            }
            forvalues j = 1/`n_total' {
                local cj = el(col_map_g`g', 1, `j')
                if `cj' == 0 {
                    continue
                }
                matrix did_V_g`g'[`i', `j'] = did_V_full[`ci', `cj']
            }
        }
    }

    * -------- export regression results for re-plotting --------
    preserve
    clear
    set obs 0

    gen str32 definition = ""
    gen str32 event_type = ""
    gen str16 control_set = ""
    gen str1 treatment_group = ""
    gen balanced_only = .
    gen fe_level = .
    gen str16 target = ""
    gen str20 transform = ""
    gen group4 = .
    gen str32 group_label = ""
    gen str16 coef_name = ""
    gen rel_quarter = .
    gen estimate = .
    gen variance = .
    gen std_error = .
    gen ci_lb_95 = .
    gen ci_ub_95 = .
    gen N = .
    gen obs_control = .
    gen obs_group0 = .
    gen obs_group1 = .
    gen obs_group2 = .
    gen obs_group3 = .
    gen pairs_control = .
    gen pairs_group0 = .
    gen pairs_group1 = .
    gen pairs_group2 = .
    gen pairs_group3 = .
    gen products_group0 = .
    gen products_group1 = .
    gen products_group2 = .
    gen products_group3 = .

    forvalues g = 0/3 {
        local this_group_label "clean_nonshare"
        if `g' == 1 {
            local this_group_label "clean_share"
        }
        else if `g' == 2 {
            local this_group_label "confounded_nonshare"
        }
        else if `g' == 3 {
            local this_group_label "confounded_share"
        }

        forvalues i = 1/`n_total' {
            local this_coef : word `i' of `coef_names'
            local b = el(did_b_g`g', 1, `i')
            local v = el(did_V_g`g', `i', `i')
            if missing(`b') {
                local v = .
            }

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
            replace definition = "`definition'" in `row'
            replace event_type = "`event_type'" in `row'
            replace control_set = "`control_set'" in `row'
            replace treatment_group = "`treatment_group'" in `row'
            replace balanced_only = `balanced_only' in `row'
            replace fe_level = `fe_level' in `row'
            replace target = "`target'" in `row'
            replace transform = "`std'" in `row'
            replace group4 = `g' in `row'
            replace group_label = "`this_group_label'" in `row'
            replace coef_name = "`this_coef'" in `row'
            replace rel_quarter = `rel_q' in `row'
            replace estimate = `b' in `row'
            replace variance = `v' in `row'
            replace std_error = `se' in `row'
            replace ci_lb_95 = `lb' in `row'
            replace ci_ub_95 = `ub' in `row'
            replace N = `N_est' in `row'
            replace obs_control = `obs_control' in `row'
            replace obs_group0 = `obs_group0' in `row'
            replace obs_group1 = `obs_group1' in `row'
            replace obs_group2 = `obs_group2' in `row'
            replace obs_group3 = `obs_group3' in `row'
            replace pairs_control = `pairs_control' in `row'
            replace pairs_group0 = `pairs_group0' in `row'
            replace pairs_group1 = `pairs_group1' in `row'
            replace pairs_group2 = `pairs_group2' in `row'
            replace pairs_group3 = `pairs_group3' in `row'
            replace products_group0 = `products_group0' in `row'
            replace products_group1 = `products_group1' in `row'
            replace products_group2 = `products_group2' in `row'
            replace products_group3 = `products_group3' in `row'
        }
    }

    sort group4 rel_quarter
    export delimited using "`csv_path'/`file_stub'.csv", replace
    restore

    * Common pretrends are copied to all groups in the CSV, but shown once in the graph.
    forvalues g = 1/3 {
        forvalues i = 1/`n_total' {
            local coef_name : word `i' of `coef_names'
            if substr("`coef_name'", 1, 4) == "pre_" {
                matrix did_b_g`g'[1, `i'] = .
                forvalues j = 1/`n_total' {
                    matrix did_V_g`g'[`i', `j'] = 0
                    matrix did_V_g`g'[`j', `i'] = 0
                }
            }
        }
    }

    * -------- plot four group4 ATT curves together --------
    capture noisily event_plot did_b_g0#did_V_g0 did_b_g1#did_V_g1 did_b_g2#did_V_g2 did_b_g3#did_V_g3, ///
        stub_lag(post_# post_# post_# post_#) ///
        stub_lead(pre_# pre_# pre_# pre_#) ///
        trimlead(`trimlead') trimlag(`trimlag') ///
        plottype(scatter) ciplottype(rcap) ///
        together perturb(-`perturb_span'(`perturb_increment')`perturb_span') noautolegend ///
        graph_opt( ///
            title("`definition' `event_type' `control_set' tg=`treatment_group' balanced=`balanced_only' fe=`fe_level' `target' log", size(vsmall)) ///
            xtitle("Quarters since event", size(small)) ///
            ytitle("log(`target')", size(small)) ///
            xlabel(`xlabel_spec', nogrid) ///
            legend(order(1 "clean, non-share" 3 "clean, share" 5 "confounded, non-share" 7 "confounded, share") ///
                rows(2) position(6) region(style(none))) ///
            xline(-0.5, lcolor(gs8) lpattern(dash)) ///
            yline(0, lcolor(gs8)) ///
            graphregion(color(white)) bgcolor(white) ///
            ylabel(, angle(horizontal)) ///
        ) ///
        lag_opt1(msymbol(O) color(black)) lag_ci_opt1(color(black)) ///
        lag_opt2(msymbol(Th) color(navy)) lag_ci_opt2(color(navy)) ///
        lag_opt3(msymbol(S) color(maroon)) lag_ci_opt3(color(maroon)) ///
        lag_opt4(msymbol(D) color(green)) lag_ci_opt4(color(green))
    local plot_rc = _rc

    if `plot_rc' == 0 {
        graph export "`fig_path'/`file_stub'.png", replace width(`graph_width')
    }
    else {
        di as error "event_plot failed: r(`plot_rc'). CSV output was still exported."
    }

    di as text "success: `file_stub'"
    log close

}
}
}
}
}
}
}
}

clear all
