clear all
set more off
set trace off

local code_path "`c(pwd)'"
local project_path = regexr("`code_path'", "[/\\][^/\\]+$", "")
local project_path = subinstr("`project_path'", "\", "/", .)

local data_root "`project_path'/data/cohort_data_with_atc3sharing"
local log_root "`project_path'/logs/ddd_atc3sharing"
local tex_root "`project_path'/tex/ddd_atc3sharing"
local csv_root "`project_path'/csv/ddd_atc3sharing"

cap mkdir "`project_path'/logs"
cap mkdir "`project_path'/tex"
cap mkdir "`project_path'/csv"
cap mkdir "`log_root'"
cap mkdir "`tex_root'"
cap mkdir "`csv_root'"

local panel_levels quarter
local events to_B_not_in_A direct_interlock to_B_still_in_A indirect_interlock
local controls not notyet purecontrol
local targets revenue price quantity
local standardize_types standardize normalize
local event_types event first_event

foreach panel_level of local panel_levels {
    local data_path "`data_root'/`panel_level'"
    cap mkdir "`log_root'/`panel_level'"
    cap mkdir "`tex_root'/`panel_level'"
    cap mkdir "`csv_root'/`panel_level'"

    foreach event of local events {
        cap mkdir "`log_root'/`panel_level'/`event'"
        cap mkdir "`tex_root'/`panel_level'/`event'"
        cap mkdir "`csv_root'/`panel_level'/`event'"

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

                    local file_stub "`event'_`event_type'_`control_fname'_`std'_ddd_atc3sharing"

                    cap log close
                    log using "`log_root'/`panel_level'/`event'/`file_stub'.log", text replace

                    estimates clear

                    local csv_file "`csv_root'/`panel_level'/`event'/`file_stub'.csv"
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

                            gen event_cohort = .
                            gen treated_in_stack = 0

                            if "`event_type'" == "event" {
                                replace event_cohort = `cohort' if event_`cohort' == 1
                                replace treated_in_stack = event_`cohort' == 1
                            }
                            else {
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
                            bysort boardname product data_cohort: egen group_max_norm = max(`target')
                            bysort boardname product data_cohort: egen group_min_norm = min(`target')
                            gen group_gap_norm = group_max_norm - group_min_norm

                            preserve
                            keep boardname product data_cohort group_gap_norm
                            bysort boardname product data_cohort: keep if _n == 1
                            quietly summarize group_gap_norm, detail
                            local p95_group_gap = r(p95)
                            restore

                            drop if group_gap_norm > `p95_group_gap' & !missing(group_gap_norm)
                            drop group_max_norm group_min_norm group_gap_norm
                        }

                        egen id = group(boardname product data_cohort)
                        gen q_time = yq(year, quarter)
                        format q_time %tq

                        gen treat = treated_in_stack
                        gen post = q_time >= yq(data_cohort, 1)
                        gen did = treat * post
                        gen g = atc3_sharing if treat == 1
                        replace g = 0 if missing(g)
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

                        reghdfe `target' did did_g, absorb(i.id i.q_time)
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

                        did_imputation `target' id q_time event_cohort_did_imputation, ///
                            fe(id q_time) ///
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

                    local tex_file "`tex_root'/`panel_level'/`event'/`file_stub'.tex"
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
                        title("DDD: `event', `event_type', `control', `std'")

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
