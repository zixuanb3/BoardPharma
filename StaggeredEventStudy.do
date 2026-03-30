*==============================================================================
* OVERVIEW:
* This do-file performs staggered event study analyses on firm-level panel data,
* examining the effects of board interlocks and director transitions in a setting
* where different firms experience events at different times. It implements an
* estimator robust to heterogeneous treatment effects across staggered treatment
* adoption timings (csdid, eventstudyinteract) and compares with standard TWFE.
* Both yearly and quarterly frequency analyses are supported.
*
* INPUT:
* - data/staggered_data/{year|quarter}-level/staggered_firm_level_panel_{freq}_{event}_{control}_balanced.csv
*   Staggered panel data for each combination of:
*     * frequency: year or quarter
*     * event: to_B_not_in_A, to_B_still_in_A, direct_interlock, indirect_interlock
*     * control: not_yet (uses last cohort as comparison), pure_control (uses never-treated)
*
* OUTPUT:
* - logs/staggered_event_study/{year|quarter}/{event}/{freq}_{event}_{control}_{target}_{std}.log
*   Log files documenting analysis execution and results
* - figures/staggered_event_study/{year|quarter}/{event}/{freq}_{event}_{control}_{target}_{std}.png
*   Event study plots comparing estimation methods
*
* FILE STRUCTURE:
* project_root/
* │
* ├── codes/
* │   └── StaggeredEventStudy.do
* │
* ├── data/
* │   └── staggered_data/
* │       ├── year-level/
* │       │   └── staggered_firm_level_panel_year_{event}_{control}_balanced.csv
* │       └── quarter-level/
* │           └── staggered_firm_level_panel_quarter_{event}_{control}_balanced.csv
* │
* ├── logs/
* │   └── staggered_event_study/
* │       ├── year/
* │       │   └── {event}/
* │       └── quarter/
* │           └── {event}/
* │
* └── figures/
*     └── staggered_event_study/
*         ├── year/
*         │   └── {event}/
*         └── quarter/
*             └── {event}/
*
* IMPLEMENTATION WORKFLOW:
* 1. Define project paths dynamically from current working directory
* 2. Loop over panel frequencies: year, quarter
* 3. Set event window parameters:
*    - Yearly: pre_max=4, post_max=4 ([-4, +4] window)
*    - Quarterly: pre_max=16, post_max=19 ([-16, +19] quarters window)
* 4. For each frequency, loop over events and control specifications
* 5. For each event-control-target-std combination:
*    a) Load staggered panel data
*    b) Create treatment time variable: first_event_year or first_event_quarter
*    c) Create event time variable: relative time since treatment
*    d) Apply standardization if specified (z-score within firm-product groups)
*    e) Generate ID variable for firm-product pairs
*    f) Create event window indicator variables (pre_1 to pre_max, post_0 to post_max)
*    g) Execute three estimation methods:
*       - csdid: Callaway & Sant'Anna DiD for staggered adoption
*       - TWFE: Two-way fixed effects regression (potentially biased with staggered timing)
*       - eventstudyinteract: Sun & Abraham interaction weighting for heterogeneous effects
*    h) Apply control group logic:
*       - not_yet: drops last cohort treated units in post-period, uses them as comparison
*       - pure_control: uses never-treated units as comparison group
*    i) Create event study plots with coefficient comparisons
*    j) Save plots and logs to structured directories
*
* ENVIRONMENT:
* - Stata 18
*==============================================================================

clear all
set more off
set trace off



* ================= paths =================
local code_path "`c(pwd)'"
display "code_path = `code_path'"
local project_path = regexr("`code_path'", "[/\\][^/\\]+$", "")
display "project_path = `project_path'"

local data_path "`project_path'/data/staggered_data_with_atc3sharing"
local fig_base  "`project_path'/figures/staggered_event_study_sharingatc3"
local log_base  "`project_path'/logs/staggered_event_study_sharingatc3"

cap mkdir "`project_path'/figures"
cap mkdir "`project_path'/logs"
cap mkdir "`fig_base'"
cap mkdir "`log_base'"

* ================= loop setup =================
local panel_levels quarter
local events to_B_not_in_A to_B_still_in_A direct_interlock indirect_interlock
local targets revenue price quantity
local controls not_yet pure_control
local standardization standardize normalize

foreach panel_level of local panel_levels {

    local timevar year
    local pre_max 4
    local post_max 3
	
    if "`panel_level'" == "quarter" {
        local pre_max 4
        local post_max 7
    }

    cap mkdir "`fig_base'/`panel_level'"
    cap mkdir "`log_base'/`panel_level'"

    foreach event of local events {

        cap mkdir "`fig_base'/`panel_level'/`event'"
        cap mkdir "`log_base'/`panel_level'/`event'"

        foreach control of local controls {
            foreach target of local targets {
                foreach std of local standardization {

                    if "`control'" == "not_yet" {
                        local control_folder "Not Yet"
                    }
                    else if "`control'" == "pure_control" {
                        local control_folder "Pure Control"
                    }
                    else {
                        di as error "Unknown control type: `control'"
                        exit 198
                    }

                    local infile "`data_path'/`panel_level'/first_event/`control_folder'/staggered_firm_level_panel_`panel_level'_`event'_`control'_balanced.csv"
                    local basename "`panel_level'_`event'_`control'_`target'_`std'"
                    local logfile "`log_base'/`panel_level'/`event'/`basename'.log"

                    log using "`logfile'", text replace

                    clear
                    import delimited "`infile'", clear

                    * -------- treatment-time variable and event time --------
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

                    * -------- standardization --------
                    if "`std'" == "standardize" {
                        foreach var of local targets {
                            bysort boardname product: egen temp = std(`var')
                            replace `var' = temp
                            drop temp
                        }
                    }
                    else if "`std'" == "normalize" {
                        * Baseline follows stacked style.
                        * For treated/future-treated units: first_event_year.
                        * For never-treated units: earliest observed year in the unit panel.
                        tempvar min_year baseline_year baseline baseline_value
                        bysort boardname product: egen `min_year' = min(year)
                        gen `baseline_year' = first_event_year
                        replace `baseline_year' = `min_year' if missing(`baseline_year')

                        if "`panel_level'" == "quarter" {
                            gen `baseline' = `target' if year == `baseline_year' & quarter == 1
                        }
                        else {
                            gen `baseline' = `target' if year == `baseline_year'
                        }

                        bysort boardname product: egen `baseline_value' = max(`baseline')
                        replace `target' = `target' / `baseline_value'
                    }

                    * -------- generate id --------
                    egen id = group(boardname product)

                    * -------- event dummies --------
                    forvalues k = 1/`pre_max' {
                        gen pre_`k' = (`event_time' == -`k') if !missing(`event_time')
                    }
                    forvalues k = 0/`post_max' {
                        gen post_`k' = (`event_time' == `k') if !missing(`event_time')
                    }

                    * build treatment variable list excluding pre_1 as baseline
                    local treatvar ""
                    forvalues k = `pre_max'(-1)2 {
                        local treatvar `treatvar' pre_`k'
                    }
                    forvalues k = 0/`post_max' {
                        local treatvar `treatvar' post_`k'
                    }

                    local coef_n = (`pre_max' - 1) + (`post_max' + 1)
                    local total_n = `coef_n' + 1

                    local allnames ""
                    forvalues k = `pre_max'(-1)1 {
                        local allnames `allnames' pre_`k'
                    }
                    forvalues k = 0/`post_max' {
                        local allnames `allnames' post_`k'
                    }

                    local run_esi = ("`control'" == "pure_control")

                    * -------- prepare treatment flags --------
                    gen treated = !missing(`treat_time')
                    gen never_treated = missing(`treat_time')

                    * -------- csdid: run by atc3_sharing subgroup --------
                    tempvar gvar_csdid
                    gen `gvar_csdid' = `treat_time'
                    replace `gvar_csdid' = 0 if missing(`gvar_csdid')

                    preserve
                    drop if treated == 1 & atc3_sharing == 0
                    csdid `target', ivar(id) time(`timevar') gvar(`gvar_csdid') agg(event) notyet method(dripw) long rseed(1)
                    est store did1_s1
                    restore

                    preserve
                    drop if treated == 1 & atc3_sharing == 1
                    csdid `target', ivar(id) time(`timevar') gvar(`gvar_csdid') agg(event) notyet method(dripw) long rseed(1)
                    est store did1_s0
                    restore

                    * -------- did_imputation with sharing heterogeneity --------
                    gen atc3_sharing_het = atc3_sharing if treated == 1
                    replace atc3_sharing_het = 0 if treated == 0

                    did_imputation `target' id `timevar' `treat_time', ///
                        fe(id, `timevar') horizons(0/`post_max') pretrend(2) ///
                        hetby(atc3_sharing_het) autosample
                    est store did2

                    * -------- twfe with interaction terms --------
                    foreach v of local treatvar {
                        gen `v'_x_atc3 = `v' * atc3_sharing
                    }

                    local twfe_interact_terms ""
                    foreach v of local treatvar {
                        local twfe_interact_terms "`twfe_interact_terms' `v'_x_atc3"
                    }

                    reghdfe `target' `treatvar' `twfe_interact_terms', absorb(i.id i.`timevar')
                    matrix twfe_b_full = e(b)
                    matrix twfe_V_full = e(V)

                    local n_old = `coef_n'
                    local n_pre_nonref = `pre_max' - 1

                    foreach sval in 0 1 {
                        matrix twfe_b_s`sval' = J(1, `total_n', .)

                        forvalues i = 1/`n_pre_nonref' {
                            local base_coef = twfe_b_full[1, `i']
                            if `sval' == 1 {
                                local inter_coef = twfe_b_full[1, `n_old' + `i']
                                matrix twfe_b_s`sval'[1, `i'] = `base_coef' + `inter_coef'
                            }
                            else {
                                matrix twfe_b_s`sval'[1, `i'] = `base_coef'
                            }
                        }
                        matrix twfe_b_s`sval'[1, `=`n_pre_nonref' + 1'] = 0

                        forvalues i = `=`n_pre_nonref' + 1'/`n_old' {
                            local base_coef = twfe_b_full[1, `i']
                            if `sval' == 1 {
                                local inter_coef = twfe_b_full[1, `n_old' + `i']
                                matrix twfe_b_s`sval'[1, `i' + 1] = `base_coef' + `inter_coef'
                            }
                            else {
                                matrix twfe_b_s`sval'[1, `i' + 1] = `base_coef'
                            }
                        }
                        matrix colnames twfe_b_s`sval' = `allnames'

                        matrix twfe_V_s`sval' = J(`total_n', `total_n', 0)

                        if `sval' == 0 {
                            forvalues i = 1/`n_pre_nonref' {
                                forvalues j = 1/`n_pre_nonref' {
                                    matrix twfe_V_s0[`i', `j'] = twfe_V_full[`i', `j']
                                }
                            }
                            forvalues i = `=`n_pre_nonref' + 1'/`n_old' {
                                forvalues j = `=`n_pre_nonref' + 1'/`n_old' {
                                    matrix twfe_V_s0[`i' + 1, `j' + 1] = twfe_V_full[`i', `j']
                                }
                            }
                            forvalues i = 1/`n_pre_nonref' {
                                forvalues j = `=`n_pre_nonref' + 1'/`n_old' {
                                    matrix twfe_V_s0[`i', `j' + 1] = twfe_V_full[`i', `j']
                                    matrix twfe_V_s0[`j' + 1, `i'] = twfe_V_full[`j', `i']
                                }
                            }
                        }
                        else {
                            forvalues i = 1/`n_pre_nonref' {
                                forvalues j = 1/`n_pre_nonref' {
                                    local vbb = twfe_V_full[`i', `j']
                                    local vgg = twfe_V_full[`n_old' + `i', `n_old' + `j']
                                    local vbg = twfe_V_full[`i', `n_old' + `j']
                                    local vgb = twfe_V_full[`n_old' + `i', `j']
                                    matrix twfe_V_s1[`i', `j'] = `vbb' + `vgg' + `vbg' + `vgb'
                                }
                            }
                            forvalues i = `=`n_pre_nonref' + 1'/`n_old' {
                                forvalues j = `=`n_pre_nonref' + 1'/`n_old' {
                                    local vbb = twfe_V_full[`i', `j']
                                    local vgg = twfe_V_full[`n_old' + `i', `n_old' + `j']
                                    local vbg = twfe_V_full[`i', `n_old' + `j']
                                    local vgb = twfe_V_full[`n_old' + `i', `j']
                                    matrix twfe_V_s1[`i' + 1, `j' + 1] = `vbb' + `vgg' + `vbg' + `vgb'
                                }
                            }
                            forvalues i = 1/`n_pre_nonref' {
                                forvalues j = `=`n_pre_nonref' + 1'/`n_old' {
                                    local vbb = twfe_V_full[`i', `j']
                                    local vgg = twfe_V_full[`n_old' + `i', `n_old' + `j']
                                    local vbg = twfe_V_full[`i', `n_old' + `j']
                                    local vgb = twfe_V_full[`n_old' + `i', `j']
                                    matrix twfe_V_s1[`i', `j' + 1] = `vbb' + `vgg' + `vbg' + `vgb'
                                    matrix twfe_V_s1[`j' + 1, `i'] = `vbb' + `vgg' + `vbg' + `vgb'
                                }
                            }
                        }
                        matrix colnames twfe_V_s`sval' = `allnames'
                        matrix rownames twfe_V_s`sval' = `allnames'
                    }

                    * -------- eventstudyinteract with interacted dummies (pure_control only) --------
                    if `run_esi' {
                        foreach v of local treatvar {
                            gen `v'_s0 = `v' * (1 - atc3_sharing)
                            gen `v'_s1 = `v' * atc3_sharing
                        }

                        local esi_vars_s0 ""
                        local esi_vars_s1 ""
                        foreach v of local treatvar {
                            local esi_vars_s0 "`esi_vars_s0' `v'_s0"
                            local esi_vars_s1 "`esi_vars_s1' `v'_s1"
                        }

                        eventstudyinteract `target' `esi_vars_s0' `esi_vars_s1', ///
                            cohort(`treat_time') ///
                            control_cohort(never_treated) ///
                            absorb(i.id i.`timevar')
                        matrix sa_b_all = e(b_iw)
                        matrix sa_V_all = e(V_iw)

                        foreach sval in 0 1 {
                            local offset = `sval' * `coef_n'

                            matrix sa_b_s`sval' = J(1, `total_n', .)
                            forvalues i = 1/`n_pre_nonref' {
                                matrix sa_b_s`sval'[1, `i'] = sa_b_all[1, `offset' + `i']
                            }
                            matrix sa_b_s`sval'[1, `=`n_pre_nonref' + 1'] = 0
                            forvalues i = `=`n_pre_nonref' + 1'/`coef_n' {
                                matrix sa_b_s`sval'[1, `i' + 1] = sa_b_all[1, `offset' + `i']
                            }
                            matrix colnames sa_b_s`sval' = `allnames'

                            matrix sa_V_s`sval' = J(`total_n', `total_n', 0)
                            forvalues i = 1/`n_pre_nonref' {
                                forvalues j = 1/`n_pre_nonref' {
                                    matrix sa_V_s`sval'[`i', `j'] = sa_V_all[`offset' + `i', `offset' + `j']
                                }
                            }
                            forvalues i = `=`n_pre_nonref' + 1'/`coef_n' {
                                forvalues j = `=`n_pre_nonref' + 1'/`coef_n' {
                                    matrix sa_V_s`sval'[`i' + 1, `j' + 1] = sa_V_all[`offset' + `i', `offset' + `j']
                                }
                            }
                            forvalues i = 1/`n_pre_nonref' {
                                forvalues j = `=`n_pre_nonref' + 1'/`coef_n' {
                                    matrix sa_V_s`sval'[`i', `j' + 1] = sa_V_all[`offset' + `i', `offset' + `j']
                                    matrix sa_V_s`sval'[`j' + 1, `i'] = sa_V_all[`offset' + `j', `offset' + `i']
                                }
                            }
                            matrix colnames sa_V_s`sval' = `allnames'
                            matrix rownames sa_V_s`sval' = `allnames'
                        }
                    }
                    else {
                        di as text "control=`control', skipping eventstudyinteract"
                    }

                    * -------- did_imputation: extract group-specific coefficients --------
                    est restore did2
                    matrix did2_b_full = e(b)
                    matrix did2_V_full = e(V)

                    local did2_ncol = colsof(did2_b_full)
                    local did2_names : colnames did2_b_full

                    foreach sval in 0 1 {
                        matrix did2_b_s`sval' = J(1, `total_n', .)
                        matrix did2_V_s`sval' = J(`total_n', `total_n', 0)

                        tempname col_map
                        matrix `col_map' = J(1, `total_n', 0)

                        forvalues i = 1/`total_n' {
                            local col_name : word `i' of `allnames'
                            local cname1 ""
                            local cname2 ""

                            if substr("`col_name'", 1, 4) == "pre_" {
                                local k = substr("`col_name'", 5, .)
                                local cname1 "pre`k'_`sval'"
                                local cname2 "pre`k'"
                            }
                            else if substr("`col_name'", 1, 5) == "post_" {
                                local h = substr("`col_name'", 6, .)
                                local cname1 "tau`h'_`sval'"
                                local cname2 "tau`h'"
                            }

                            if "`cname1'" != "" {
                                forvalues c = 1/`did2_ncol' {
                                    local cn : word `c' of `did2_names'
                                    if "`cn'" == "`cname1'" | "`cn'" == "`cname2'" {
                                        matrix `col_map'[1, `i'] = `c'
                                        matrix did2_b_s`sval'[1, `i'] = did2_b_full[1, `c']
                                        continue, break
                                    }
                                }
                            }
                        }

                        forvalues i = 1/`total_n' {
                            local ci = `col_map'[1, `i']
                            if `ci' == 0 continue
                            forvalues j = 1/`total_n' {
                                local cj = `col_map'[1, `j']
                                if `cj' == 0 continue
                                matrix did2_V_s`sval'[`i', `j'] = did2_V_full[`ci', `cj']
                            }
                        }

                        matrix colnames did2_b_s`sval' = `allnames'
                        matrix colnames did2_V_s`sval' = `allnames'
                        matrix rownames did2_V_s`sval' = `allnames'
                    }

                    * -------- event plots: one figure per atc3_sharing subgroup --------
                    foreach sval in 0 1 {
                        if `sval' == 1 {
                            local share_label "atc3sharing=1"
                        }
                        else {
                            local share_label "atc3sharing=0"
                        }

                        cap est drop __plot_csdid
                        est restore did1_s`sval'
                        est store __plot_csdid

                        matrix did2_b = did2_b_s`sval'
                        matrix did2_V = did2_V_s`sval'

                        matrix twfe_b = twfe_b_s`sval'
                        matrix twfe_V = twfe_V_s`sval'

                        if `run_esi' {
                            matrix sa_b = sa_b_s`sval'
                            matrix sa_V = sa_V_s`sval'
                        }
                        else {
                            matrix sa_b = J(1, `total_n', 0)
                            matrix colnames sa_b = `allnames'
                            matrix sa_V = J(`total_n', `total_n', .)
                            matrix colnames sa_V = `allnames'
                            matrix rownames sa_V = `allnames'
                        }

                        if `run_esi' {
                            event_plot __plot_csdid did2_b#did2_V sa_b#sa_V twfe_b#twfe_V, ///
                                stub_lag(Tp# tau# post_# post_#) ///
                                stub_lead(Tm# pre# pre_# pre_#) ///
                                trimlead(`pre_max') trimlag(`post_max') ///
                                plottype(scatter) ciplottype(rcap) ///
                                together perturb(-0.325(0.1)0.325) noautolegend ///
                                graph_opt( ///
                                    title("`panel_level' `event' `control' (`share_label')", size(med)) ///
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
                            event_plot __plot_csdid did2_b#did2_V twfe_b#twfe_V, ///
                                stub_lag(Tp# tau# post_#) ///
                                stub_lead(Tm# pre# pre_#) ///
                                trimlead(`pre_max') trimlag(`post_max') ///
                                plottype(scatter) ciplottype(rcap) ///
                                together perturb(-0.25(0.1)0.25) noautolegend ///
                                graph_opt( ///
                                    title("`panel_level' `event' `control' (`share_label')", size(med)) ///
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

                        local figfile "`fig_base'/`panel_level'/`event'/`basename'_atc3sharing`sval'.png"
                        graph export "`figfile'", replace width(4000)
                        cap est drop __plot_csdid
                    }

                    cap est drop did1_s0
                    cap est drop did1_s1
                    cap est drop did2

                    log close
                }
            }
        }
    }
}
