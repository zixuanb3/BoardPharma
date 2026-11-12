/*
Purpose:
Generate the maintained Stata event_plot figures for the 18 completed quarterly
formulary NDC included-share regressions.

Process:
1. Read each sample's dynamic.csv exported by the regression do-file.
2. Rebuild the two 11-coefficient b/V pairs used by the existing formulary
   event-study graph workflow; common pretrends appear only for sharing=0.
3. Apply the original event_plot scatter/rcap, perturbation, color, title,
   legend, axis, and export settings without re-estimating the regressions.

Input:
- csv/formulary_ndc/es/shift_q1_first_seen_{0|1}_exit_{0|1}_event_q/
  req1_not_raw_fe1_firm/dynamic.csv

Output:
- figures/formulary_ndc/es/shift_q1_first_seen_{0|1}_exit_{0|1}_event_q/
  req1_not_raw_fe1_firm/{event}/{A|B}/included_share/plot.png
- figures/formulary_ndc/es/run_complete.txt
*/

clear all
set more off
set varabbrev off

* ========================== USER CONFIG ==========================
local sample_folders ///
    shift_q1_first_seen_0_exit_0_event_q ///
    shift_q1_first_seen_1_exit_0_event_q ///
    shift_q1_first_seen_1_exit_1_event_q
local events interlock_dissolution to_B_still_in_A to_B_not_in_A
local treatment_groups B A
local target included_share
local spec_folder req1_not_raw_fe1_firm
local coef_names pre_3 pre_2 pre_1 post_0 post_1 post_2 post_3 post_4 post_5 post_6 post_7
local n_total 11
local trimlead 3
local trimlag 7
local xlabel_spec -3(1)7
local graph_width 3000
local perturb_step 0.15
local perturb_span 0.30
* ================================================================

capture program drop set_ndc_plot_state
program define set_ndc_plot_state, eclass
    tempname placeholder_b placeholder_V
    matrix `placeholder_b' = J(1, 1, 0)
    matrix colnames `placeholder_b' = placeholder
    matrix `placeholder_V' = J(1, 1, 0)
    matrix colnames `placeholder_V' = placeholder
    matrix rownames `placeholder_V' = placeholder
    ereturn post `placeholder_b' `placeholder_V'
    ereturn local cmd "did_imputation"
end

local code_path "`c(pwd)'"
local parent_path = regexr("`code_path'", "[/\\][^/\\]+$", "")
local project_path = regexr("`parent_path'", "[/\\][^/\\]+$", "")
local project_path = subinstr("`project_path'", "\", "/", .)
capture confirm file "`code_path'/formulary_ndc_quarter_event_study_plots.do"
if _rc {
    di as error "Run this do-file with the working directory set to codes/3_event_study."
    exit 601
}

capture which event_plot
if _rc {
    di as error "event_plot is not installed. Install it before running this do-file."
    exit 199
}

local csv_base "`project_path'/csv/formulary_ndc/es"
local figure_base "`project_path'/figures/formulary_ndc/es"
cap mkdir "`project_path'/figures"
cap mkdir "`project_path'/figures/formulary_ndc"
cap mkdir "`figure_base'"
local completion_manifest "`figure_base'/run_complete.txt"
capture erase "`completion_manifest'"

local plot_count 0
foreach sample_folder of local sample_folders {
    local dynamic_file "`csv_base'/`sample_folder'/`spec_folder'/dynamic.csv"
    capture confirm file "`dynamic_file'"
    if _rc {
        di as error "Missing regression coefficient file: `dynamic_file'"
        exit 601
    }

    foreach event of local events {
        if "`event'" == "to_B_not_in_A" {
            local event_folder "notinA"
        }
        else if "`event'" == "to_B_still_in_A" {
            local event_folder "stillA"
        }
        else if "`event'" == "interlock_dissolution" {
            local event_folder "dissolve"
        }
        else {
            di as error "Unsupported event: `event'"
            exit 198
        }

        foreach treatment_group of local treatment_groups {
            import delimited "`dynamic_file'", clear varnames(1) case(lower)
            keep if event == "`event'" & direction == "`treatment_group'" & target == "`target'"
            assert _N == 19
            assert !missing(rel_quarter)
            assert !missing(sharing)
            assert !missing(estimate)
            assert !missing(std_error)
            assert inrange(rel_quarter, -3, 7)
            assert inlist(sharing, 0, 1)
            isid rel_quarter sharing

            foreach sharing in 0 1 {
                matrix did2_b_s`sharing' = J(1, `n_total', .)
                matrix did2_V_s`sharing' = J(`n_total', `n_total', 0)
                matrix colnames did2_b_s`sharing' = `coef_names'
                matrix colnames did2_V_s`sharing' = `coef_names'
                matrix rownames did2_V_s`sharing' = `coef_names'
            }

            forvalues pre = 1/3 {
                local coefficient_index = 4 - `pre'
                quietly summarize estimate if rel_quarter == -`pre' & sharing == 0, meanonly
                assert r(N) == 1
                matrix did2_b_s0[1, `coefficient_index'] = r(mean)
                quietly summarize std_error if rel_quarter == -`pre' & sharing == 0, meanonly
                matrix did2_V_s0[`coefficient_index', `coefficient_index'] = r(mean)^2
            }
            forvalues horizon = 0/7 {
                local coefficient_index = 4 + `horizon'
                foreach sharing in 0 1 {
                    quietly summarize estimate if ///
                        rel_quarter == `horizon' & sharing == `sharing', meanonly
                    assert r(N) == 1
                    matrix did2_b_s`sharing'[1, `coefficient_index'] = r(mean)
                    quietly summarize std_error if ///
                        rel_quarter == `horizon' & sharing == `sharing', meanonly
                    matrix did2_V_s`sharing'[`coefficient_index', `coefficient_index'] = r(mean)^2
                }
            }

            local figure_dir ///
                "`figure_base'/`sample_folder'/`spec_folder'/`event_folder'/`treatment_group'/`target'"
            cap mkdir "`figure_base'/`sample_folder'"
            cap mkdir "`figure_base'/`sample_folder'/`spec_folder'"
            cap mkdir "`figure_base'/`sample_folder'/`spec_folder'/`event_folder'"
            cap mkdir "`figure_base'/`sample_folder'/`spec_folder'/`event_folder'/`treatment_group'"
            cap mkdir "`figure_dir'"
            capture erase "`figure_dir'/plot.png"

            * Preserve the exact maintained event identifier in every figure.
            local event_name "`event'"
            local counterpart "A"
            if "`treatment_group'" == "A" {
                local counterpart "B"
            }
            local group_label "`treatment_group'_without_`counterpart'"
            local graph_title_size small
            local cv_title " |other_event"
            local kappa_title " |none|atc3|none"

            * This event_plot version evaluates e(cmd) even when stubs are
            * supplied explicitly. Recreate the active did_imputation state
            * that exists when the original regression do-file draws a graph.
            set_ndc_plot_state

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

            graph export "`figure_dir'/plot.png", replace width(`graph_width')
            graph drop _all
            local ++plot_count
        }
    }
}

assert `plot_count' == 18
tempname completion_handle
file open `completion_handle' using "`completion_manifest'", write text replace
file write `completion_handle' "COMPLETE 18 Stata event_plot figures" _n
file close `completion_handle'
di as result "Completed all 18 formulary NDC Stata event_plot figures."
