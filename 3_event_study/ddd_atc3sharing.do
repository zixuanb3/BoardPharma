clear all
set more off
set trace off

// ================================================================
// Purpose:
// Estimate stacked DDD specifications that compare post-event outcome
// changes for treated products with and without ATC3 sharing, and report
// the corresponding did_imputation heterogeneity contrast as a check.
//
// Design:
// - Build stacked cohort-specific samples for each event/control/event_type
//   combination, then estimate Y = beta0 * (Treat x Post)
//                        + beta1 * (Treat x Post x ATC3-sharing) + FE.
// - beta0 is the post-event treatment effect for treated products without
//   ATC3 sharing; beta1 is the additional effect for sharing products; and
//   beta0 + beta1 is the total effect for sharing products.
// - did_imputation with hetby(atc3_sharing_het) provides an alternative
//   staggered-adoption estimator, where tau_1 - tau_0 summarizes the gap
//   between sharing and non-sharing treatment effects.
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
// - logs/ddd_atc3sharing_fe*/
// - tex/ddd_atc3sharing_fe*/
// - csv/ddd_atc3sharing_fe*/
// ================================================================

* ================= user config =================
local trim_percentile "p90"

* ================= path =================
local code_path "`c(pwd)'"
local parent_path = regexr("`code_path'", "[/\\][^/\\]+$", "")
local project_path = regexr("`parent_path'", "[/\\][^/\\]+$", "")
local project_path = subinstr("`project_path'", "\", "/", .)

local data_root "`project_path'/data/cohort_data_with_atc3sharing"
local output_tag_base "ddd_atc3sharing"
local panel_levels quarter

cap mkdir "`project_path'/logs"
cap mkdir "`project_path'/tex"
cap mkdir "`project_path'/csv"

local events to_B_not_in_A direct_interlock to_B_still_in_A indirect_interlock
local controls not notyet purecontrol
local targets revenue price quantity
local standardize_types standardize normalize log_transform
local event_types event first_event
local treatment_groups B A
local include_eventpair_values 1 0
local fe_levels 1 2 3

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
            local panel_group_folder "`panel_level'-level_`group_label'"
            local data_path "`data_root'/`panel_group_folder'"

            foreach fe_level of local fe_levels {
                if !inlist(`fe_level', 1, 2, 3) {
                    di as error "fe_level must be one of: 1, 2, 3"
                    exit 198
                }

                local fe_output_tag "`output_tag_base'_trim`trim_percentile'_fe`fe_level'"
                local fe_log_root "`project_path'/logs/`fe_output_tag'"
                local fe_tex_root "`project_path'/tex/`fe_output_tag'"
                local fe_csv_root "`project_path'/csv/`fe_output_tag'"

                cap mkdir "`fe_log_root'"
                cap mkdir "`fe_tex_root'"
                cap mkdir "`fe_csv_root'"
                cap mkdir "`fe_log_root'/`panel_group_folder'"
                cap mkdir "`fe_tex_root'/`panel_group_folder'"
                cap mkdir "`fe_csv_root'/`panel_group_folder'"

                foreach event of local events {
                    * Interlock events do not vary with treatment/include parameters.
                    * Keep only one canonical run: treatment_group=B and include_eventpair=1.
                    if inlist("`event'", "direct_interlock", "indirect_interlock") & ("`treatment_group'" != "B" | `include_eventpair' != 1) {
                        continue
                    }

                    cap mkdir "`fe_log_root'/`panel_group_folder'/`event'"
                    cap mkdir "`fe_tex_root'/`panel_group_folder'/`event'"
                    cap mkdir "`fe_csv_root'/`panel_group_folder'/`event'"

                    foreach control of local controls {
                        foreach std of local standardize_types {
                            foreach event_type of local event_types {
                                local cohort_list ""

                                if "`panel_level'" == "quarter" {
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
                                        if "`treatment_group'" == "A" {
                                            local cohort_list 2008 2009 2010 2012 2013 2016 2017 2018
                                        }
                                        else {
                                            local cohort_list 2009 2010 2012 2013 2014 2016 2017 2018
                                        }
                                    }
                                    else if "`event'" == "to_B_not_in_A" & "`event_type'" == "event" {
                                        if "`treatment_group'" == "A" {
                                            local cohort_list 2008 2009 2010 2011 2012 2013 2014 2015 2016 2017 2018
                                        }
                                        else {
                                            local cohort_list 2009 2010 2011 2012 2013 2014 2016 2017 2018
                                        }
                                    }
                                    else if "`event'" == "to_B_still_in_A" & "`event_type'" == "first_event" {
                                        if "`treatment_group'" == "A" {
                                            local cohort_list 2009 2010 2011 2012 2013 2015 2017
                                        }
                                        else {
                                            local cohort_list 2008 2009 2010 2011 2013 2014 2017 2018
                                        }
                                    }
                                    else if "`event'" == "to_B_still_in_A" & "`event_type'" == "event" {
                                        if "`treatment_group'" == "A" {
                                            local cohort_list 2008 2009 2010 2011 2012 2013 2014 2015 2016 2017 2018
                                        }
                                        else {
                                            local cohort_list 2008 2009 2010 2011 2013 2014 2015 2017 2018
                                        }
                                    }
                                }

                                if "`cohort_list'" == "" {
                                    continue
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

                                local file_stub "`event'_`event_type'_`control_fname'_`std'_ddd_atc3sharing_fe`fe_level'_`group_label'"

                                cap log close
                                log using "`fe_log_root'/`panel_group_folder'/`event'/`file_stub'.log", text replace

                                estimates clear

                                local csv_file "`fe_csv_root'/`panel_group_folder'/`event'/`file_stub'.csv"
                                tempfile run_results
                                tempname posth

                                postfile `posth' ///
                                    str12 panel_level ///
                                    str30 event ///
                                    str15 control ///
                                    str15 std ///
                                    str15 event_type ///
                                    str20 target ///
                                    str20 model ///
                                    double beta0 beta0_se beta0_p ///
                                    double beta1 beta1_se beta1_p ///
                                    double te_share te_share_se te_share_p ///
                                    double didimp_diff didimp_diff_se didimp_diff_p ///
                                    double pcteff_notshare pcteff_share ///
                                    double premean_notshare premean_share ///
                                    double N_obs N_notshare N_share ///
                                    double product_notshare product_share ///
                                    using `run_results', replace

                                foreach target of local targets {
                                    local first 1

                                    foreach cohort of local cohort_list {
                                        local data_file "`data_path'/`event_type'/`control_folder'/`event'_`panel_level'_cohort_`cohort'`suffix'_balanced.csv"

                                        import delimited "`data_file'", clear
                                        gen target_raw = `target'

                                        if "`std'" == "standardize" {
                                            bysort boardname product: egen temp = std(`target')
                                            replace `target' = temp
                                            drop temp
                                        }
                                        else if "`std'" == "normalize" {
                                            * Normalization rescales each product by its cohort-entry baseline,
                                            * so post coefficients are interpretable relative to that baseline.
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
                                        else if "`std'" == "log_transform" {
                                            replace `target' = log(`target')
                                        }

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

                                    if "`std'" == "normalize" {
                                        * Trim stacks with unusually volatile normalized paths so that a few extreme denominator cases do not dominate the estimates.
                                        bysort boardname product data_cohort: egen group_max_norm = max(`target')
                                        bysort boardname product data_cohort: egen group_min_norm = min(`target')
                                        gen group_gap_norm = group_max_norm - group_min_norm

                                        preserve
                                        keep boardname product data_cohort group_gap_norm
                                        bysort boardname product data_cohort: keep if _n == 1
                                        quietly summarize group_gap_norm, detail
                                        local pct_group_gap = r(`trim_percentile')
                                        restore

                                        drop if group_gap_norm > `pct_group_gap' & !missing(group_gap_norm)
                                        drop group_max_norm group_min_norm group_gap_norm
                                    }

                                    * Stack-specific panel id: the same product can reappear in another cohort stack and should be treated as a distinct unit there
                                    egen id = group(boardname product data_cohort)
                                    gen q_time = yq(year, quarter)
                                    format q_time %tq

                                    * Post is defined relative to each stack's own cohort entry date
                                    gen treat = treated_in_stack
                                    gen post = q_time >= yq(data_cohort, 1)
                                    gen did = treat * post
                                    * g marks the heterogeneity dimension of interest: ATC3 sharing status among treated units.
                                    gen g = atc3_sharing if treat == 1
                                    replace g = 0 if missing(g)
                                    * did_g is the DDD interaction. Its coefficient is the incremental treatment effect for sharing products relative to non-sharing ones.
                                    gen did_g = did * g
                                    gen pre_period = q_time < yq(data_cohort, 1)

                                    local premean_notshare = .
                                    local premean_share = .
                                    local basesd_notshare = .
                                    local basesd_share = .

                                    if "`std'" == "standardize" {
                                        quietly summarize target_raw if treat == 1 & atc3_sharing == 0 & pre_period == 1
                                        local premean_notshare = r(mean)
                                        local basesd_notshare = r(sd)

                                        quietly summarize target_raw if treat == 1 & atc3_sharing == 1 & pre_period == 1
                                        local premean_share = r(mean)
                                        local basesd_share = r(sd)
                                    }
                                    else {
                                        quietly summarize `target' if treat == 1 & atc3_sharing == 0 & pre_period == 1, meanonly
                                        local premean_notshare = r(mean)
                                        quietly summarize `target' if treat == 1 & atc3_sharing == 1 & pre_period == 1, meanonly
                                        local premean_share = r(mean)
                                    }

                                    quietly count if treat == 1 & atc3_sharing == 0
                                    local N_notshare = r(N)
                                    quietly count if treat == 1 & atc3_sharing == 1
                                    local N_share = r(N)

                                    egen tag_id_notshare = tag(id) if treat == 1 & atc3_sharing == 0
                                    quietly count if tag_id_notshare == 1
                                    local product_notshare = r(N)
                                    drop tag_id_notshare

                                    egen tag_id_share = tag(id) if treat == 1 & atc3_sharing == 1
                                    quietly count if tag_id_share == 1
                                    local product_share = r(N)
                                    drop tag_id_share

                                    gen event_cohort_did_imputation = yq(event_cohort, 1) if !missing(event_cohort)
                                    gen atc3_sharing_het = atc3_sharing if treat == 1
                                    replace atc3_sharing_het = 0 if treat == 0
                                    
                                    egen cohort_q_time_fe = group(data_cohort q_time)
                                    cap egen atc3_q_time_fe = group(atc3 q_time)

                                    * FE level 1 uses stack-unit and common calendar-quarter FE.
                                    * FE level 2 replaces common time FE with cohort-by-quarter FE, allowing each stacked cohort to have its own aggregate path.
                                    local fe_spec "id q_time"
                                    if `fe_level' == 2 {
                                        local fe_spec "id cohort_q_time_fe"
                                    }
                                    else if `fe_level' == 3 {
                                        local fe_spec "id atc3_q_time_fe"
                                    }

                                    reghdfe `target' did did_g, absorb(`fe_spec')
                                    local N_obs_reghdfe = e(N)

                                    lincom did
                                    local beta0 = r(estimate)
                                    local beta0_se = r(se)
                                    local beta0_p = r(p)

                                    lincom did_g
                                    local beta1 = r(estimate)
                                    local beta1_se = r(se)
                                    local beta1_p = r(p)

                                    lincom did + did_g
                                    local te_share = r(estimate)
                                    local te_share_se = r(se)
                                    local te_share_p = r(p)

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

                                    estimates store m_`target'

                                    * did_imputation serves as a staggered-adoption robustness check;
                                    * tau_1 - tau_0 is the heterogeneity contrast between sharing and non-sharing treated products under that estimator.
                                    did_imputation `target' id q_time event_cohort_did_imputation, ///
                                        fe(`fe_spec') ///
                                        hetby(atc3_sharing_het) ///
                                        autosample tol(0.1)

                                    local N_obs_didimp = e(N)

                                    lincom tau_1 - tau_0
                                    local didimp_diff = r(estimate)
                                    local didimp_diff_se = r(se)
                                    local didimp_diff_p = r(p)

                                    post `posth' ///
                                        ("`panel_level'") ///
                                        ("`event'") ///
                                        ("`control'") ///
                                        ("`std'") ///
                                        ("`event_type'") ///
                                        ("`target'") ///
                                        ("reghdfe") ///
                                        (`beta0') (`beta0_se') (`beta0_p') ///
                                        (`beta1') (`beta1_se') (`beta1_p') ///
                                        (`te_share') (`te_share_se') (`te_share_p') ///
                                        (.) (.) (.) ///
                                        (`pcteff_notshare') (`pcteff_share') ///
                                        (`premean_notshare') (`premean_share') ///
                                        (`N_obs_reghdfe') (`N_notshare') (`N_share') ///
                                        (`product_notshare') (`product_share')

                                    post `posth' ///
                                        ("`panel_level'") ///
                                        ("`event'") ///
                                        ("`control'") ///
                                        ("`std'") ///
                                        ("`event_type'") ///
                                        ("`target'") ///
                                        ("did_imputation") ///
                                        (.) (.) (.) ///
                                        (.) (.) (.) ///
                                        (.) (.) (.) ///
                                        (`didimp_diff') (`didimp_diff_se') (`didimp_diff_p') ///
                                        (`pcteff_notshare') (`pcteff_share') ///
                                        (`premean_notshare') (`premean_share') ///
                                        (`N_obs_didimp') (`N_notshare') (`N_share') ///
                                        (`product_notshare') (`product_share')

                                    local didimp_star ""
                                    if !missing(`didimp_diff_p') {
                                        if `didimp_diff_p' < 0.01 {
                                            local didimp_star "***"
                                        }
                                        else if `didimp_diff_p' < 0.05 {
                                            local didimp_star "**"
                                        }
                                        else if `didimp_diff_p' < 0.10 {
                                            local didimp_star "*"
                                        }
                                    }
                                    local didimp_diff_disp : display %9.3f `didimp_diff'
                                    local didimp_diff_disp = strtrim("`didimp_diff_disp'")
                                    local didimp_diff_star "`didimp_diff_disp'`didimp_star'"

                                    estimates restore m_`target'
                                    estadd scalar premean_notshare = `premean_notshare'
                                    estadd scalar premean_share = `premean_share'
                                    estadd scalar pcteff_notshare = `pcteff_notshare'
                                    estadd scalar pcteff_share = `pcteff_share'
                                    estadd scalar N_notshare = `N_notshare'
                                    estadd scalar N_share = `N_share'
                                    estadd scalar product_notshare = `product_notshare'
                                    estadd scalar product_share = `product_share'
                                    estadd local didimp_diff_star = "`didimp_diff_star'"
                                    estadd scalar didimp_diff = `didimp_diff'
                                    estadd scalar didimp_diff_se = `didimp_diff_se'
                                    estadd scalar didimp_diff_p = `didimp_diff_p'
                                }

                                postclose `posth'
                                preserve
                                use `run_results', clear
                                export delimited using "`csv_file'", replace
                                restore

                                local tex_file "`fe_tex_root'/`panel_group_folder'/`event'/`file_stub'.tex"
                                tempfile tex_fragment

                                esttab m_revenue m_price m_quantity using "`tex_fragment'", ///
                                    replace booktabs se star(* 0.10 ** 0.05 *** 0.01) ///
                                    keep(did did_g) ///
                                    coeflabels( ///
                                        did "beta0: Treat x Post" ///
                                        did_g "beta1: Treat x Post x G" ///
                                    ) ///
                                    mtitles("Revenue" "Price" "Quantity") ///
                                    stats( ///
                                        premean_notshare ///
                                        premean_share ///
                                        pcteff_notshare ///
                                        pcteff_share ///
                                        N ///
                                        N_notshare ///
                                        N_share ///
                                        product_notshare ///
                                        product_share ///
                                        didimp_diff_star ///
                                        didimp_diff_se ///
                                        didimp_diff_p, ///
                                        fmt(3 3 2 2 0 0 0 0 0 %9s 3 3) ///
                                        labels( ///
                                            "Pre-treatment mean Y (Not Share)" ///
                                            "Pre-treatment mean Y (Share)" ///
                                            "Percent effect (Not Share)" ///
                                            "Percent effect (Share)" ///
                                            "Observations" ///
                                            "N (Not Share, treated)" ///
                                            "N (Share, treated)" ///
                                            "Product count (Not Share, treated)" ///
                                            "Product count (Share, treated)" ///
                                            "did-imputation: tau1-tau0" ///
                                            "did-imputation SE" ///
                                            "did-imputation p-value" ///
                                        ) ///
                                    ) ///
                                    title("DDD: `event', `event_type', `control', `std' | `group_label' | FE `fe_level'")

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
