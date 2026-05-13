*==============================================================================
* Purpose:
* Run staggered event-study regressions on balanced firm-product panels and
* compare csdid, did_imputation, TWFE, and eventstudyinteract estimates.
*
* Process:
* - Resolve project paths and create output folders.
* - Read USER CONFIG for panel_levels, events, targets, controls,
*   standardization, and pre/post event windows.
* - For each configuration, load staggered panel data and build event-time
*   indicators around first_event timing.
* - Estimate model coefficients and export plots/logs by configuration.
*
* Input:
* - data/staggered_data/{year|quarter}-level/
*   staggered_firm_level_panel_{panel}_{event}_{control}_balanced.csv
*
* Output:
* - logs/staggered_event_study/{panel}/{event}/{panel}_{event}_{control}_{target}_{std}.log
* - figures/staggered_event_study/{panel}/{event}/{panel}_{event}_{control}_{target}_{std}.png
*==============================================================================

clear all
set more off
set trace off



* ================= paths =================
local code_path "`c(pwd)'"
display "code_path = `code_path'"
local parent_path = regexr("`code_path'", "[/\\][^/\\]+$", "")
local project_path = regexr("`parent_path'", "[/\\][^/\\]+$", "")
display "project_path = `project_path'"

local data_path "`project_path'/data/staggered_data"
local fig_base  "`project_path'/figures/staggered_event_study"
local log_base  "`project_path'/logs/staggered_event_study"

cap mkdir "`project_path'/figures"
cap mkdir "`project_path'/logs"
cap mkdir "`fig_base'"
cap mkdir "`log_base'"

* ================= USER CONFIG =================
* panel_levels: year or quarter.
local panel_levels quarter

* events: treatment definitions used to load staggered panels.
local events to_B_still_in_A direct_interlock indirect_interlock to_B_not_in_A

* targets: outcomes to estimate.
local targets revenue price quantity

* controls: not_yet or pure_control control construction.
local controls pure_control
* not_yet pure_control

* standardization: use "standardize" for within-id z-scores, or empty for raw scale.
local standardization standardize 

* Event windows by panel level.
local pre_max_year 4
local post_max_year 4
local pre_max_quarter 4
local post_max_quarter 7

* ================= loop setup =================

foreach panel_level of local panel_levels {

    local timevar year
    if "`panel_level'" == "year" {
        local pre_max `pre_max_year'
        local post_max `post_max_year'
    }
    else if "`panel_level'" == "quarter" {
        local pre_max `pre_max_quarter'
        local post_max `post_max_quarter'
    }
    else {
        di as error "Unsupported panel_level: `panel_level'"
        continue
    }

    cap mkdir "`fig_base'/`panel_level'"
    cap mkdir "`log_base'/`panel_level'"

    foreach event of local events {

        cap mkdir "`fig_base'/`panel_level'/`event'"
        cap mkdir "`log_base'/`panel_level'/`event'"

        foreach control of local controls {
            foreach target of local targets {
                foreach std of local standardization {

                    local infile "`data_path'/`panel_level'-level/staggered_firm_level_panel_`panel_level'_`event'_`control'_balanced.csv"
                    local basename "`panel_level'_`event'_`control'_`target'_`std'"
                    local logfile "`log_base'/`panel_level'/`event'/`basename'.log"
                    local figfile "`fig_base'/`panel_level'/`event'/`basename'.png"

                    log using "`logfile'", text replace

                    clear
                    import delimited "`infile'", clear

                    * Build treatment timing and relative event time.
                    tempvar treat_time event_time
                    if "`panel_level'" == "year" {
                        gen `treat_time' = first_event_year
                        gen `event_time' = year - first_event_year if !missing(first_event_year)
                        local timevar year
                    }
                    else if "`panel_level'" == "quarter" {
                        gen __q_time = yq(year, quarter)
                        gen __q_treat = yq(first_event_year, 1) if !missing(first_event_year)
                        format __q_time %tq
                        format __q_treat %tq

                        gen `treat_time' = __q_treat
                        gen `event_time' = __q_time - __q_treat if !missing(__q_treat)
                        local timevar __q_time
                    }

                    * Optional within-firm-product standardization.
                    if "`std'" == "standardize" {
                        foreach var of local targets {
                            bysort boardname product: egen temp = std(`var')
                            replace `var' = temp
                            drop temp
                        }
                    }
					
                    * Panel identifier at firm-product level.
                    egen id = group(boardname product)

                    * Event-time indicators: leads pre_k and lags post_k.
                    forvalues k = 1/`pre_max' {
                        gen pre_`k' = 0
                        replace pre_`k' = (`event_time' == -`k') if !missing(`event_time')
                    }
                    forvalues k = 0/`post_max' {
                        gen post_`k' = 0
                        replace post_`k' = (`event_time' == `k') if !missing(`event_time')
                    }

                    * Regression design excludes pre_1 as omitted baseline.
                    local treatvar ""
                    forvalues k = `pre_max'(-1)2 {
                        local treatvar `treatvar' pre_`k'
                    }
                    forvalues k = 0/`post_max' {
                        local treatvar `treatvar' post_`k'
                    }

                    local coef_n = (`pre_max' - 1) + (`post_max' + 1)
                    local total_n = `coef_n' + 1

                    * csdid with control-specific option.
                    local csdid_opt ""
                    if "`control'" == "not_yet" {
                        local csdid_opt "notyet"
                    }
                    tempvar gvar_csdid
                    gen `gvar_csdid' = `treat_time'
                    replace `gvar_csdid' = 0 if missing(`gvar_csdid')
                    csdid `target', ivar(id) time(`timevar') gvar(`gvar_csdid') agg(event) `csdid_opt' method(dripw) long rseed(1)
                    est store did1

                    * did_imputation specification.
                    did_imputation `target' id `timevar' `treat_time', fe(id `timevar') horizons(0/`post_max') pretrends(2) autosample
                    est store did2

                    * TWFE with absorbed id and time fixed effects.
                    reghdfe `target' `treatvar', absorb(i.id i.`timevar')
                    matrix twfe_b = e(b)
                    matrix twfe_V = e(V)

                    matrix twfe_b_with_pre1 = J(1, `total_n', 0)
                    local idx = 1
                    forvalues k = `pre_max'(-1)2 {
                        matrix twfe_b_with_pre1[1, `idx'] = twfe_b[1, `idx']
                        local idx = `idx' + 1
                    }
                    local idx = `idx' + 1
                    forvalues k = 0/`post_max' {
                        matrix twfe_b_with_pre1[1, `idx'] = twfe_b[1, `idx' - 1]
                        local idx = `idx' + 1
                    }

                    local allnames ""
                    forvalues k = `pre_max'(-1)1 {
                        local allnames `allnames' pre_`k'
                    }
                    forvalues k = 0/`post_max' {
                        local allnames `allnames' post_`k'
                    }
                    matrix colnames twfe_b_with_pre1 = `allnames'

                    matrix twfe_V_with_pre1 = J(`total_n', `total_n', 0)

                    * Reinsert pre_1 as zero to align plotting grids across estimators.
                    local map_old = ""
                    local map_new = ""
                    local old_idx = 1
                    local new_idx = 1
                    forvalues k = `pre_max'(-1)2 {
                        local map_old `map_old' `old_idx'
                        local map_new `map_new' `new_idx'
                        local old_idx = `old_idx' + 1
                        local new_idx = `new_idx' + 1
                    }
                    local new_idx = `new_idx' + 1
                    forvalues k = 0/`post_max' {
                        local map_old `map_old' `old_idx'
                        local map_new `map_new' `new_idx'
                        local old_idx = `old_idx' + 1
                        local new_idx = `new_idx' + 1
                    }

                    local mcount : word count `map_old'
                    forvalues i = 1/`mcount' {
                        local oi : word `i' of `map_old'
                        local ni : word `i' of `map_new'
                        forvalues j = 1/`mcount' {
                            local oj : word `j' of `map_old'
                            local nj : word `j' of `map_new'
                            matrix twfe_V_with_pre1[`ni', `nj'] = twfe_V[`oi', `oj']
                        }
                    }

                    matrix colnames twfe_V_with_pre1 = `allnames'
                    matrix rownames twfe_V_with_pre1 = `allnames'

                    matrix twfe_b = twfe_b_with_pre1
                    matrix twfe_V = twfe_V_with_pre1

                    * eventstudyinteract runs only under pure_control.
                    local run_esi = ("`control'" == "pure_control")
                    gen never_treated = missing(`treat_time')

                    if `run_esi' {
                        local controlcohort never_treated

                        eventstudyinteract `target' `treatvar', ///
                            cohort(`treat_time') ///
                            control_cohort(`controlcohort') ///
                            absorb(i.id i.`timevar')

                        matrix sa_b = e(b_iw)
                        matrix sa_V = e(V_iw)

                        matrix sa_b_with_pre1 = J(1, `total_n', 0)
                        local idx = 1
                        forvalues k = `pre_max'(-1)2 {
                            matrix sa_b_with_pre1[1, `idx'] = sa_b[1, `idx']
                            local idx = `idx' + 1
                        }
                        local idx = `idx' + 1
                        forvalues k = 0/`post_max' {
                            matrix sa_b_with_pre1[1, `idx'] = sa_b[1, `idx' - 1]
                            local idx = `idx' + 1
                        }
                        matrix colnames sa_b_with_pre1 = `allnames'

                        matrix sa_V_with_pre1 = J(`total_n', `total_n', 0)
                        forvalues i = 1/`mcount' {
                            local oi : word `i' of `map_old'
                            local ni : word `i' of `map_new'
                            forvalues j = 1/`mcount' {
                                local oj : word `j' of `map_old'
                                local nj : word `j' of `map_new'
                                matrix sa_V_with_pre1[`ni', `nj'] = sa_V[`oi', `oj']
                            }
                        }

                        matrix colnames sa_V_with_pre1 = `allnames'
                        matrix rownames sa_V_with_pre1 = `allnames'

                        matrix sa_b = sa_b_with_pre1
                        matrix sa_V = sa_V_with_pre1
                    }

                    * Unified event-plot output.
					
                    if `run_esi' {
                        event_plot did1 did2 sa_b#sa_V twfe_b#twfe_V, ///
                            stub_lag(Tp# tau# post_# post_#) ///
                            stub_lead(Tm# pre# pre_# pre_#) ///
                            trimlead(`pre_max') trimlag(`post_max') ///
                            plottype(scatter) ciplottype(rcap) ///
                            together perturb(-0.325(0.1)0.325) noautolegend ///
                            graph_opt( ///
                                title("`panel_level' `event' `control'", size(med)) ///
                                xtitle("Periods since the event", size(small)) ///
                                ytitle("`target'", size(med)) ///
                                xlabel(-`pre_max'(1)`post_max', nogrid) ///
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
                            lag_opt4(msymbol(Ah) color(green)) lag_ci_opt4(color(green))
                    }
                    else {
                        event_plot did1 did2 twfe_b#twfe_V, ///
                            stub_lag(Tp# tau# post_#) ///
                            stub_lead(Tm# pre# pre_#) ///
                            trimlead(`pre_max') trimlag(`post_max') ///
                            plottype(scatter) ciplottype(rcap) ///
                            together perturb(-0.25(0.125)0.25) noautolegend ///
                            graph_opt( ///
                                title("`panel_level' `event' `control'", size(med)) ///
                                xtitle("Periods since the event", size(small)) ///
                                ytitle("`target'", size(med)) ///
                                xlabel(-`pre_max'(1)`post_max', nogrid) ///
                                legend(order(1 "csdid" 3 "did imputation" 5 "TWFE") ///
                                       rows(1) position(6) region(style(none))) ///
                                xline(-0.5, lcolor(gs8) lpattern(dash)) ///
                                yline(0, lcolor(gs8)) ///
                                graphregion(color(white)) bgcolor(white) ///
                                ylabel(, angle(horizontal)) ///
                            ) ///
                            lag_opt1(msymbol(+) color(black)) lag_ci_opt1(color(black)) ///
                            lag_opt2(msymbol(O) color(cranberry)) lag_ci_opt2(color(cranberry)) ///
                            lag_opt3(msymbol(Ah) color(green)) lag_ci_opt3(color(green))
                    }

                    graph export "`figfile'", replace width(4000)

                    log close
                }
            }
        }
    }
}
