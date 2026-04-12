clear all
set more off
set trace off

local code_path "`c(pwd)'"
local project_path = regexr("`code_path'", "[/\\][^/\\]+$", "")
local project_path = subinstr("`project_path'", "\", "/", .)

local data_root "`project_path'/data/cohort_data_with_atc3sharing"
local log_root "`project_path'/logs/ddd_atc3sharing"
local tex_root "`project_path'/tex/ddd_atc3sharing"

cap mkdir "`project_path'/logs"
cap mkdir "`project_path'/tex"
cap mkdir "`log_root'"
cap mkdir "`tex_root'"

cap which reghdfe
if _rc {
    di as error "reghdfe is not installed."
    exit 199
}

cap which did_imputation
if _rc {
    di as error "did_imputation is not installed."
    exit 199
}

cap which esttab
if _rc {
    di as error "esttab is not installed."
    exit 199
}

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

    foreach event of local events {
        cap mkdir "`log_root'/`panel_level'/`event'"
        cap mkdir "`tex_root'/`panel_level'/`event'"

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

                    foreach target of local targets {
                        local first 1

                        foreach cohort of local cohort_list {
                            local data_file "`data_path'/`event_type'/`control_folder'/`event'_`panel_level'_cohort_`cohort'`suffix'_balanced.csv"

                            import delimited "`data_file'", clear

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

                        egen id = group(boardname product data_cohort)
                        gen q_time = yq(year, quarter)
                        format q_time %tq

                        gen treat = treated_in_stack
                        gen post = q_time >= yq(data_cohort, 1)
                        gen did = treat * post
                        gen g = atc3_sharing if treat == 1
                        replace g = 0 if missing(g)
                        gen did_g = did * g
                        gen event_cohort_did_imputation = yq(event_cohort, 1) if !missing(event_cohort)
                        gen atc3_sharing_het = atc3_sharing if treat == 1
                        replace atc3_sharing_het = 0 if treat == 0

                        reghdfe `target' did did_g, absorb(i.id i.q_time)
                        estimates store m_`target'

                        did_imputation `target' id q_time event_cohort_did_imputation, ///
                            fe(id q_time) ///
                            hetby(atc3_sharing_het) ///
                            autosample tol(0.1)

                        lincom tau_1 - tau_0
                        local didimp_diff = r(estimate)
                        local didimp_diff_se = r(se)
                        local didimp_diff_p = r(p)

                        estimates restore m_`target'
                        estadd scalar didimp_diff = `didimp_diff'
                        estadd scalar didimp_diff_se = `didimp_diff_se'
                        estadd scalar didimp_diff_p = `didimp_diff_p'
                    }

                    esttab m_revenue m_price m_quantity using "`tex_root'/`panel_level'/`event'/`file_stub'.tex", ///
                        replace booktabs se star(* 0.10 ** 0.05 *** 0.01) ///
                        keep(did did_g) ///
                        coeflabels( ///
                            did "\$\beta_0$: Treat $\times$ Post" ///
                            did_g "\$\beta_1$: Treat $\times$ Post $\times$ G" ///
                        ) ///
                        mtitles("Revenue" "Price" "Quantity") ///
                        stats( ///
                            N ///
                            didimp_diff ///
                            didimp_diff_se ///
                            didimp_diff_p, ///
                            fmt(0 3 3 3) ///
                            labels( ///
                                "Observations" ///
                                "did\_imputation: $\tau_1-\tau_0$" ///
                                "did\_imputation SE" ///
                                "did\_imputation p-value" ///
                            ) ///
                        ) ///
                        title("DDD: `event', `event_type', `control', `std'")

                    log close
                }
            }
        }
    }
}
