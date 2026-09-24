import dataclasses

import numpy_financial as npf
import pytest

from lbo_engine import Assumptions, build_sources_uses, calculate_returns, run_lbo, run_model

TOL = 1e-9

# Scenarios chosen to hit every branch of the cash waterfall.
SCENARIOS = {
    "base": Assumptions(),
    # Term Loan fully repaid mid-hold -> surplus must accumulate as cash (v1 lost it)
    "deleveraged": Assumptions(senior_leverage_x=2.0, total_leverage_x=3.0, ebitda_margin=0.25,
                               entry_ebitda=187.5, hold_period_years=7),
    # FCF below mandatory amortization -> RCF draw (v1 created cash from nothing)
    "stressed": Assumptions(ebitda_margin=0.10, entry_ebitda=75, revenue_growth=0.0,
                            total_leverage_x=6.5, senior_leverage_x=5.0),
    # Shortfall first, recovery later -> RCF drawn then repaid
    "draw_then_repay": Assumptions(ebitda_margin=0.12, entry_ebitda=90, revenue_growth=0.08,
                                   total_leverage_x=6.0, senior_leverage_x=5.0,
                                   senior_mandatory_amort_pct=0.10, hold_period_years=7),
    # Partial sweep, negative NWC (software-like), unitranche (no sub notes)
    "partial_sweep": Assumptions(cash_sweep_pct=0.5, nwc_pct_of_rev_growth=-0.05,
                                 senior_leverage_x=5.5),
    "one_year_hold": Assumptions(hold_period_years=1),
}


@pytest.mark.parametrize("name", SCENARIOS)
def test_sources_equal_uses(name):
    su = build_sources_uses(SCENARIOS[name])
    assert sum(su["sources"].values()) == pytest.approx(sum(su["uses"].values()), abs=TOL)


@pytest.mark.parametrize("name", SCENARIOS)
def test_cash_is_conserved_every_year(name):
    """Every euro of FCF ends up in debt paydown or on the balance sheet — nothing
    appears or disappears. This is the check the v1 engine failed."""
    a = SCENARIOS[name]
    su = build_sources_uses(a)
    senior, sub, rcf, cash = (su["sources"]["senior_term_loan"],
                              su["sources"]["subordinated_notes"], 0.0, a.min_cash)
    for r in run_lbo(a):
        net_debt_before = senior + sub + rcf - cash
        net_debt_after = r.senior_end_balance + r.sub_end_balance + r.rcf_end_balance - r.cash_end_balance
        assert net_debt_before - net_debt_after == pytest.approx(r.fcf_pre_sweep, abs=1e-6)
        # balance roll-forwards
        assert r.senior_end_balance == pytest.approx(senior - r.mandatory_amort - r.cash_sweep, abs=TOL)
        assert r.rcf_end_balance == pytest.approx(rcf + r.rcf_draw - r.rcf_repayment, abs=TOL)
        senior, sub, rcf, cash = r.senior_end_balance, r.sub_end_balance, r.rcf_end_balance, r.cash_end_balance


@pytest.mark.parametrize("name", SCENARIOS)
def test_balances_stay_non_negative(name):
    for r in run_lbo(SCENARIOS[name]):
        assert r.senior_end_balance >= -TOL
        assert r.rcf_end_balance >= -TOL
        assert r.cash_end_balance >= SCENARIOS[name].min_cash - TOL
        assert r.cash_sweep >= 0 and r.rcf_draw >= 0 and r.rcf_repayment >= 0


@pytest.mark.parametrize("name", SCENARIOS)
def test_irr_matches_numpy_financial(name):
    a = SCENARIOS[name]
    ret = run_model(a)["returns"]
    flows = [-ret["entry_equity"]] + [0] * (a.hold_period_years - 1) + [ret["exit_equity_value"]]
    assert ret["irr"] == pytest.approx(npf.irr(flows), abs=1e-9)


def test_base_case_unchanged_vs_v1():
    """In the base case neither bug triggers, so v1.1 must reproduce v1 exactly
    (values captured from the original lbo_engine.py run)."""
    out = run_model(Assumptions())
    ret, last = out["returns"], out["years"][-1]
    assert out["sources_uses"]["sources"]["sponsor_equity"] == pytest.approx(380.0)
    assert last.senior_end_balance == pytest.approx(299.7, abs=0.05)
    assert ret["exit_equity_value"] == pytest.approx(940.2, abs=0.05)
    assert ret["moic"] == pytest.approx(2.47, abs=0.005)
    assert ret["irr"] == pytest.approx(0.199, abs=0.0005)
    assert all(r.rcf_draw == 0 for r in out["years"])
    assert ret["warnings"] == []


def test_year1_hand_calculation():
    r = run_lbo(Assumptions())[0]
    assert r.revenue == pytest.approx(780.0)
    assert r.interest_expense == pytest.approx(600 * 0.07 + 225 * 0.10)
    assert r.tax == pytest.approx((156.0 - 23.4 - 64.5) * 0.25)
    assert r.fcf_pre_sweep == pytest.approx(46.575)
    assert r.cash_sweep == pytest.approx(46.575 - 30.0)


def test_surplus_cash_accumulates_once_term_loan_repaid():
    a = SCENARIOS["deleveraged"]
    out = run_model(a)
    last = out["years"][-1]
    assert last.senior_end_balance == pytest.approx(0, abs=TOL)
    assert last.cash_end_balance > a.min_cash + 100
    assert out["returns"]["exit_net_debt"] == pytest.approx(
        last.sub_end_balance - last.cash_end_balance)


def test_shortfall_draws_rcf_and_warns():
    out = run_model(SCENARIOS["stressed"])
    assert out["returns"]["peak_rcf_draw"] > 0
    assert any("RCF" in w or "Liquidity" in w for w in out["returns"]["warnings"])


def test_rcf_is_repaid_before_term_loan_sweep():
    years = run_lbo(SCENARIOS["draw_then_repay"])
    assert any(r.rcf_draw > 0 for r in years)
    assert any(r.rcf_repayment > 0 for r in years)
    for r in years:
        if r.cash_sweep > 0:
            assert r.rcf_end_balance == pytest.approx(0, abs=TOL)


@pytest.mark.parametrize("overrides, message", [
    ({"senior_leverage_x": 6.0}, "senior_leverage_x"),
    ({"total_leverage_x": 8.5, "senior_leverage_x": 4.0}, "sponsor equity"),
    ({"entry_ebitda": 800.0}, "below revenue_at_entry"),
    ({"revenue_growth": 4.0}, "revenue_growth"),
    ({"senior_rate": 7.0}, "senior_rate"),
    ({"hold_period_years": 5.5}, "hold_period_years"),
    ({"ebitda_margin": 0.0, "entry_ebitda": 150.0}, "ebitda_margin"),
])
def test_invalid_inputs_are_rejected(overrides, message):
    with pytest.raises(ValueError, match=message):
        dataclasses.replace(Assumptions(), **overrides)


def test_post_close_margin_change_keeps_entry_terms():
    """Operating plan and entry terms are separate: a lower projected margin must not
    re-price the deal or resize the debt."""
    base, downside = Assumptions(), Assumptions(ebitda_margin=0.15)   # LTM margin still 20%
    su_base, su_down = build_sources_uses(base), build_sources_uses(downside)
    assert su_down == su_base
    y1 = run_lbo(downside)[0]
    assert y1.ebitda == pytest.approx(780 * 0.15)
    assert run_model(downside)["returns"]["irr"] < run_model(base)["returns"]["irr"]
