"""
LBO Engine v1 — deterministic calculation core.

Design choice: this module does ONLY the financial math (Sources & Uses,
debt schedule, returns). No data sourcing, no AI reasoning, no Excel I/O yet.
Get this right and validated first — everything else (assumption generation,
Excel export, the "agent" wrapper) sits on top of this and is much lower risk
once the engine itself is correct.

Convention notes (matters for defending this in an interview):
- Interest is calculated on the BEGINNING-of-year debt balance, not the
  average. This avoids the classic circular reference (interest depends on
  cash flow, cash flow depends on interest) without needing iterative/
  goal-seek calculation. It's a standard simplification for a first model;
  real desks often use average-balance + circularity switch instead.
- Cash sweep waterfall: 100% of excess free cash flow after mandatory
  amortization pays down the Term Loan first. Subordinated Notes are bullet
  (no paydown) until the Term Loan is fully retired. This is a common,
  defensible structure — not the only one.
- No transaction fees / OID modeled in v1. Easy to add as a Uses line later.
"""

from dataclasses import dataclass
from typing import List
import numpy_financial as npf  # pip install numpy-financial


@dataclass
class Assumptions:
    company_name: str = "Project Vanilla Industrials (illustrative)"

    # Entry
    entry_ebitda: float = 150.0          # EUR mm, LTM at entry
    entry_ev_multiple: float = 8.0       # x EBITDA
    revenue_at_entry: float = 750.0      # EUR mm

    # Operating assumptions (held flat for simplicity in v1)
    revenue_growth: float = 0.04         # per year
    ebitda_margin: float = 0.20          # of revenue
    capex_pct_revenue: float = 0.03
    da_pct_revenue: float = 0.03
    nwc_pct_of_rev_growth: float = 0.15  # cash used for NWC = this * revenue growth $

    # Financing
    total_leverage_x: float = 5.5        # x EBITDA, total debt at entry
    senior_leverage_x: float = 4.0       # x EBITDA, Term Loan portion
    senior_rate: float = 0.07
    senior_mandatory_amort_pct: float = 0.05  # % of ORIGINAL principal, per year
    sub_rate: float = 0.10

    tax_rate: float = 0.25
    min_cash: float = 5.0                # EUR mm, not swept

    # Exit
    hold_period_years: int = 5
    exit_ev_multiple: float = 8.0        # base case = entry multiple


@dataclass
class YearResult:
    year: int
    revenue: float
    ebitda: float
    da: float
    ebit: float
    interest_expense: float
    ebt: float
    tax: float
    net_income: float
    capex: float
    nwc_change: float
    fcf_pre_sweep: float
    mandatory_amort: float
    cash_sweep: float
    senior_end_balance: float
    sub_end_balance: float


def build_sources_uses(a: Assumptions) -> dict:
    entry_ev = a.entry_ebitda * a.entry_ev_multiple
    total_debt = a.entry_ebitda * a.total_leverage_x
    senior_debt = a.entry_ebitda * a.senior_leverage_x
    sub_debt = total_debt - senior_debt

    # min_cash is funded once at close and held on the balance sheet,
    # untouched by the sweep, and netted back off debt at exit (see
    # calculate_returns). It is a genuine Use of funds, not just a label.
    total_uses = entry_ev + a.min_cash
    sponsor_equity = total_uses - total_debt  # plug; fees excluded in v1

    return {
        "entry_ev": entry_ev,
        "uses": {"purchase_of_enterprise": entry_ev, "minimum_cash_funding": a.min_cash},
        "sources": {
            "senior_term_loan": senior_debt,
            "subordinated_notes": sub_debt,
            "sponsor_equity": sponsor_equity,
        },
        "total_debt": total_debt,
    }


def run_lbo(a: Assumptions) -> List[YearResult]:
    su = build_sources_uses(a)
    senior_balance = su["sources"]["senior_term_loan"]
    sub_balance = su["sources"]["subordinated_notes"]
    senior_original = senior_balance

    revenue = a.revenue_at_entry
    results = []

    for year in range(1, a.hold_period_years + 1):
        prior_revenue = revenue
        revenue = revenue * (1 + a.revenue_growth)
        ebitda = revenue * a.ebitda_margin
        da = revenue * a.da_pct_revenue
        ebit = ebitda - da

        # Interest on BEGINNING balances (see convention note above)
        interest_expense = senior_balance * a.senior_rate + sub_balance * a.sub_rate

        ebt = ebit - interest_expense
        tax = max(ebt, 0) * a.tax_rate
        net_income = ebt - tax

        capex = revenue * a.capex_pct_revenue
        nwc_change = (revenue - prior_revenue) * a.nwc_pct_of_rev_growth

        fcf_pre_sweep = net_income + da - capex - nwc_change

        mandatory_amort = min(senior_original * a.senior_mandatory_amort_pct, senior_balance)
        cash_after_mandatory = fcf_pre_sweep - mandatory_amort
        cash_sweep = max(min(cash_after_mandatory, senior_balance - mandatory_amort), 0)

        senior_balance = senior_balance - mandatory_amort - cash_sweep
        # once senior is fully repaid, further excess cash could sweep sub debt —
        # left as a TODO for v2, sub_balance is bullet in this version

        results.append(YearResult(
            year=year, revenue=revenue, ebitda=ebitda, da=da, ebit=ebit,
            interest_expense=interest_expense, ebt=ebt, tax=tax, net_income=net_income,
            capex=capex, nwc_change=nwc_change, fcf_pre_sweep=fcf_pre_sweep,
            mandatory_amort=mandatory_amort, cash_sweep=cash_sweep,
            senior_end_balance=senior_balance, sub_end_balance=sub_balance,
        ))

    return results


def calculate_returns(a: Assumptions, results: List[YearResult], su: dict) -> dict:
    exit_ebitda = results[-1].ebitda
    exit_ev = exit_ebitda * a.exit_ev_multiple
    exit_net_debt = results[-1].senior_end_balance + results[-1].sub_end_balance - a.min_cash
    exit_equity_value = exit_ev - exit_net_debt

    entry_equity = su["sources"]["sponsor_equity"]
    moic = exit_equity_value / entry_equity

    cashflows = [-entry_equity] + [0] * (a.hold_period_years - 1) + [exit_equity_value]
    irr = npf.irr(cashflows)

    return {
        "exit_ev": exit_ev,
        "exit_net_debt": exit_net_debt,
        "exit_equity_value": exit_equity_value,
        "entry_equity": entry_equity,
        "moic": moic,
        "irr": irr,
    }


if __name__ == "__main__":
    a = Assumptions()
    su = build_sources_uses(a)
    results = run_lbo(a)
    returns = calculate_returns(a, results, su)

    print(f"=== {a.company_name} — LBO v1 ===\n")
    print("Sources & Uses (EUR mm)")
    print(f"  Entry EV:            {su['entry_ev']:.1f}  (={a.entry_ebitda:.1f} EBITDA x {a.entry_ev_multiple:.1f}x)")
    print(f"  Senior Term Loan:    {su['sources']['senior_term_loan']:.1f}  ({a.senior_leverage_x:.1f}x)")
    print(f"  Subordinated Notes:  {su['sources']['subordinated_notes']:.1f}  ({a.total_leverage_x - a.senior_leverage_x:.1f}x)")
    print(f"  Sponsor Equity:      {su['sources']['sponsor_equity']:.1f}")
    print()
    print(f"{'Yr':<4}{'Rev':>8}{'EBITDA':>9}{'IntExp':>9}{'NI':>8}{'FCF':>8}{'Amort':>8}{'Sweep':>8}{'SrEnd':>8}{'SubEnd':>8}")
    for r in results:
        print(f"{r.year:<4}{r.revenue:>8.1f}{r.ebitda:>9.1f}{r.interest_expense:>9.1f}{r.net_income:>8.1f}"
              f"{r.fcf_pre_sweep:>8.1f}{r.mandatory_amort:>8.1f}{r.cash_sweep:>8.1f}{r.senior_end_balance:>8.1f}{r.sub_end_balance:>8.1f}")
    print()
    print("Exit / Returns")
    print(f"  Exit EBITDA:         {results[-1].ebitda:.1f}")
    print(f"  Exit EV:             {returns['exit_ev']:.1f}  (at {a.exit_ev_multiple:.1f}x)")
    print(f"  Exit Net Debt:       {returns['exit_net_debt']:.1f}")
    print(f"  Exit Equity Value:   {returns['exit_equity_value']:.1f}")
    print(f"  Entry Equity:        {returns['entry_equity']:.1f}")
    print(f"  MOIC:                {returns['moic']:.2f}x")
    print(f"  IRR:                 {returns['irr']*100:.1f}%")
