// Estimate dynamic REQ2 effects from one stacked cohort sample.
// Input: stack.dta prepared by run_event_study.py in the current directory.
// Output: reghdfe estimates, cohort CSDID estimates and influence functions.
version 18
clear all
set more off
set processors 1
which reghdfe
which csdid
which drdid
use stack.dta, clear
isid unit_id relative_quarter
bysort unit_id (relative_quarter): assert _N == 12 & relative_quarter == _n - 5
assert inrange(relative_quarter, -4, 7)
egen long product_cohort = group(product cohort)
egen long quarter_cohort = group(cohort relative_quarter)
gen byte study_time = relative_quarter + 5
gen byte adoption = 5 * treated
levelsof cohort, local(cohorts)

// Return a coefficient or contrast only when all of its terms are estimable.
program define report_effect
    args handle target mode group q term1 term2 sample_n df
    local supported 1
    foreach term in `term1' `term2' {
        local column = colnumb(model_b, "`term'")
        if missing(`column') local supported 0
        else if model_omit[1, `column'] local supported 0
    }
    if `q' == -1 {
        post `handle' ("`target'") ("`mode'") ("`group'") (`q') (0) (0) (.) (`sample_n') ("reference")
    }
    else if `supported' {
        local expression "`term1'"
        if "`term2'" != "" local expression "`term1' - `term2'"
        quietly lincom `expression'
        post `handle' ("`target'") ("`mode'") ("`group'") (`q') (r(estimate)) (r(se)) (r(p)) (`sample_n') ("estimated")
    }
    else {
        post `handle' ("`target'") ("`mode'") ("`group'") (`q') (.) (.) (.) (`sample_n') ("unsupported")
    }
end

postfile hd str8 outcome str16 overlap_mode str12 effect int relative_quarter double estimate std_error p_value n_obs str12 status using reghdfe.dta, replace
postfile cs str8 outcome long cohort int relative_quarter double estimate std_error n_treated n_control using csdid_cohort.dta, replace
foreach target in price1 price0 {
    quietly summarize `target', detail
    gen double outcome = ln(min(`target', r(p95)))
    assert !missing(outcome)
    local terms
    forvalues q = -4/7 {
        if `q' != -1 {
            local suffix = `q' + 5
            gen byte p`suffix' = treated & relative_quarter == `q'
            local terms `terms' p`suffix'
        }
    }
    quietly reghdfe outcome treated `terms', absorb(product_cohort quarter_cohort) vce(cluster firm)
    local sample_n = e(N)
    matrix model_b = e(b)
    _ms_omit_info model_b
    matrix model_omit = r(omit)
    forvalues q = -4/7 {
        local suffix = `q' + 5
        report_effect hd `target' pooled overall `q' p`suffix' "" `sample_n' 0
    }
    drop `terms'

    foreach mode in event_quarter window_all window_any quarterly {
        gen byte treated_share = treated * share_`mode'
        local terms
        forvalues q = -4/7 {
            if `q' != -1 {
                local suffix = `q' + 5
                gen byte a`suffix' = treated & !treated_share & relative_quarter == `q'
                gen byte b`suffix' = treated_share & relative_quarter == `q'
                local terms `terms' a`suffix' b`suffix'
            }
        }
        quietly reghdfe outcome treated treated_share `terms', absorb(product_cohort quarter_cohort) vce(cluster firm)
        local sample_n = e(N)
        matrix model_b = e(b)
        _ms_omit_info model_b
        matrix model_omit = r(omit)
        quietly count if e(sample) & relative_quarter == -1 & treated & !treated_share
        local base0 = r(N)
        quietly count if e(sample) & relative_quarter == -1 & treated_share
        local base1 = r(N)
        forvalues q = -4/7 {
            local suffix = `q' + 5
            local a a`suffix'
            local b b`suffix'
            if !`base0' local a unavailable
            if !`base1' local b unavailable
            report_effect hd `target' `mode' nonshare `q' `a' "" `sample_n' 0
            report_effect hd `target' `mode' share `q' `b' "" `sample_n' 0
            report_effect hd `target' `mode' difference `q' `b' `a' `sample_n' 0
        }
        drop `terms' treated_share
    }

    // Restrict each comparison to its original cohort's controls.
    foreach g of local cohorts {
        preserve
        quietly keep if cohort == `g'
        local rows = _N
        quietly count if treated & study_time == 4
        local nt = r(N)
        quietly count if !treated & study_time == 4
        local nc = r(N)
        assert `nt' > 0 & `nc' > 0
        quietly csdid outcome, ivar(unit_id) time(study_time) gvar(adoption) method(reg) long2 cluster(firm) saverif(rif_`target'_`g') replace
        assert e(N) == `rows'
        matrix gt = e(gtt)
        matrix att = e(b_attgt)
        matrix cov = e(V_attgt)
        forvalues i = 1/`=rowsof(gt)' {
            assert gt[`i', 4] == 0
            local q = gt[`i', 3] - 5
            if gt[`i', 3] == 4 local q = gt[`i', 2] - 5
            post cs ("`target'") (`g') (`q') (att[1, `i']) (sqrt(cov[`i', `i'])) (`nt') (`nc')
        }
        restore
    }
    drop outcome
}
postclose hd
postclose cs
file open marker using done.txt, write replace
file write marker "complete" _n
file close marker
