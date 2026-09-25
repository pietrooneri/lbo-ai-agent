"""
LBO Engine v1.1 — deterministic calculation core.

Design choice: this module does ONLY the financial math (Sources & Uses,
debt schedule, returns). No data sourcing, no AI reasoning, no Excel I/O.
Assumption generation (assumption_generator.py) and Excel export sit on top
of this and must never change its math.

Convention notes (matters for defending this in an interview):
- Interest: by default on the OPENING balance of every tranche (Term Loan, Sub
  Notes, RCF), which avoids the circular reference (interest depends on cash
  flow, cash flow depends on interest). The circularity switch
  (interest_on_average_balance) moves it to the AVERAGE balance, as most desk
  models do: solved here by fixed-point iteration each year, and in Excel with
  iterative calculation enabled.
- Cash waterfall, each year:
    1. Cash available = FCF (after interest and tax) + cash held above the
       minimum balance from the prior year.
    2. Mandatory amortization of the Term Loan (% of ORIGINAL principal).
    3. Shortfall  -> drawn on the Revolving Credit Facility (RCF).
       Surplus    -> repays the RCF first, then sweeps the Term Loan
                     (cash_sweep_pct of what is left).
    4. Whatever remains stays on the balance sheet as excess cash.
  Subordinated Notes are bullet and non-call: never swept, repaid at exit.
- Exit net debt = Term Loan + Sub Notes + RCF - total cash on balance sheet.
- Transaction costs: M&A fees (% of EV), financing fees (% of funded debt) and
  OID on the Term Loan are Uses of funds paid at close, so they raise the equity
  cheque. Financing fees + OID are capitalised and amortised straight-line over
  fee_amortization_years: a non-cash, tax-deductible charge (added back in FCF).
  M&A fees are treated as a closing cost with no tax effect. Debt is repaid at
  face value; any unamortised balance at exit is a non-cash write-off.
- No interest income on cash, no RCF commitment fee, no tax-loss carryforwards
  (tax = max(EBT, 0) * rate).
- Only two equity cash flows (entry, exit — no dividends), so
  IRR = MOIC ^ (1 / years) - 1 exactly; no numerical solver needed.
- Year-by-year plan: revenue growth, EBITDA margin and capex can be given per
  year (ramp-ups, margin programmes, expansion capex); otherwise they are flat.
  When a year-by-year list is given, the flat value of that driver is ignored.
- Maintenance covenants (optional): maximum net debt / EBITDA (with step-downs)
  and minimum EBITDA / cash interest, tested at each year-end. Headroom is the
  EBITDA cushion: how far EBITDA can fall before the test fails (1 - actual /
  limit for leverage, 1 - limit / actual for cover). Breaches are flagged, not
  modelled (no waiver fee, repricing or default mechanics).
- Entry terms vs operating plan: entry_ebitda (LTM at closing) sizes the price
  and the debt; ebitda_margin drives the projection years only. In a base case
  they coincide (flat margins). A post-closing downside lowers ebitda_margin
  while price and debt stay as signed, which is what actually happens to a
  sponsor. The LTM margin is entry_ebitda / revenue_at_entry.
"""

from dataclasses import dataclass, fields
from typing import List, Optional, Tuple

# Operating drivers that can vary by year. `<name>_by_year` holds one value per projection
# year (year t uses element t); years beyond the list repeat its last value; None = flat.
PLAN_DRIVERS = ("revenue_growth", "ebitda_margin", "capex_pct_revenue")
PLAN_FIELDS = tuple(f"{d}_by_year" for d in PLAN_DRIVERS)
# All per-year lists (operating plan + covenant step-downs) share the same conventions.
SCHEDULE_FIELDS = PLAN_FIELDS + ("max_net_leverage_by_year",)
COVENANT_FIELDS = ("max_net_leverage", "min_interest_cover")
FLAG_FIELDS = ("interest_on_average_balance",)


@dataclass
class Assumptions:
    company_name: str = "Project Vanilla Industrials (illustrative)"

    # Entry
    entry_ebitda: float = 150.0          # EUR mm, LTM at closing: sizes price and debt
    entry_ev_multiple: float = 8.0       # x EBITDA
    revenue_at_entry: float = 750.0      # EUR mm, LTM at closing

    # Operating assumptions for the projection years (held flat for simplicity in v1)
    revenue_growth: float = 0.04         # per year
    ebitda_margin: float = 0.20          # of revenue, years 1..N (LTM margin = entry_ebitda / revenue_at_entry)
    capex_pct_revenue: float = 0.03
    da_pct_revenue: float = 0.03
    nwc_pct_of_rev_growth: float = 0.15  # cash used for NWC = this * revenue growth $ (negative = NWC release)

    # Financing
    total_leverage_x: float = 5.5        # x EBITDA, total funded debt at entry
    senior_leverage_x: float = 4.0       # x EBITDA, Term Loan portion
    senior_rate: float = 0.07
    senior_mandatory_amort_pct: float = 0.05  # % of ORIGINAL principal, per year
    sub_rate: float = 0.10
    cash_sweep_pct: float = 1.0          # share of surplus cash used to prepay the Term Loan
    rcf_commitment: float = 50.0         # EUR mm, undrawn at close
    rcf_rate: float = 0.065

    tax_rate: float = 0.25
    min_cash: float = 5.0                # EUR mm, funded at close, never swept

    # Transaction costs (0 = not modelled; old projects load unchanged)
    transaction_fees_pct_ev: float = 0.0     # M&A advisory, legal, due diligence: % of EV, paid at close
    financing_fees_pct_debt: float = 0.0     # arrangement / underwriting fees: % of funded debt, paid at close
    senior_oid_pct: float = 0.0              # Term Loan issued below par: 1% = funded at 99, repaid at 100
    fee_amortization_years: int = 6          # financing fees + OID amortised over the debt tenor

    # Year-by-year operating plan (optional; see PLAN_DRIVERS)
    revenue_growth_by_year: Optional[Tuple[float, ...]] = None
    ebitda_margin_by_year: Optional[Tuple[float, ...]] = None
    capex_pct_revenue_by_year: Optional[Tuple[float, ...]] = None

    # Maintenance covenants, tested at each year-end (0 = no covenant)
    max_net_leverage: float = 0.0                                  # net debt / EBITDA ceiling, x
    max_net_leverage_by_year: Optional[Tuple[float, ...]] = None   # step-downs; last value repeats
    min_interest_cover: float = 0.0                                # EBITDA / cash interest floor, x

    # Circularity switch: False = interest on opening balances (no circularity);
    # True = interest on average balances, solved iteratively (Excel: iterative calculation)
    interest_on_average_balance: bool = False

    # Exit
    hold_period_years: int = 5
    exit_ev_multiple: float = 8.0        # base case = entry multiple

    def __post_init__(self):
        for name in SCHEDULE_FIELDS:             # lists from JSON -> tuples; [] -> flat
            value = getattr(self, name)
            if value is not None and not isinstance(value, (str, bytes)):
                try:
                    setattr(self, name, tuple(value) or None)
                except TypeError:
                    pass                         # reported by validation_errors
        errors = self.validation_errors()
        if errors:
            raise ValueError("Invalid LBO assumptions:\n  - " + "\n  - ".join(errors))

    def validation_errors(self) -> List[str]:
        """Structural checks only: inputs the math cannot handle, or that are
        internally inconsistent. Market plausibility (is 9x leverage sensible
        for this sector?) is the assumption generator's job, not the engine's."""
        e = []
        for f in fields(self):
            v = getattr(self, f.name)
            if f.name in SCHEDULE_FIELDS:
                if v is not None and (not isinstance(v, tuple) or not 1 <= len(v) <= 15 or any(
                        isinstance(x, bool) or not isinstance(x, (int, float)) for x in v)):
                    e.append(f"{f.name} must be a list of 1-15 numbers (one per year), got {v!r}")
            elif f.name == "interest_on_average_balance":
                if not isinstance(v, bool):
                    e.append(f"interest_on_average_balance must be true or false, got {v!r}")
            elif f.name != "company_name" and (isinstance(v, bool) or not isinstance(v, (int, float))):
                e.append(f"{f.name} must be a number, got {v!r}")
        if e:
            return e
        bounds = {"revenue_growth": (-0.5, 1.0, "between -50% and +100%"),
                  "ebitda_margin": (0.0, 1.0, "between 0 and 1"),
                  "capex_pct_revenue": (0.0, 1.0, "between 0 and 1")}
        for driver, (lo, hi, text) in bounds.items():
            for i, x in enumerate(getattr(self, f"{driver}_by_year") or ()):
                if not lo < x < hi and not (driver == "capex_pct_revenue" and x == 0):
                    e.append(f"{driver}_by_year[{i}] = {x}: must be {text} (decimals)")
        for i, x in enumerate(self.max_net_leverage_by_year or ()):
            if not 0 < x <= 20:
                e.append(f"max_net_leverage_by_year[{i}] = {x}: must be between 0 and 20x")
        if self.max_net_leverage < 0 or self.min_interest_cover < 0:
            e.append("max_net_leverage and min_interest_cover must be >= 0 (0 = no covenant)")

        for name in ("entry_ebitda", "entry_ev_multiple", "revenue_at_entry", "exit_ev_multiple"):
            if getattr(self, name) <= 0:
                e.append(f"{name} must be > 0")
        for name in ("capex_pct_revenue", "da_pct_revenue", "senior_rate", "sub_rate", "rcf_rate",
                     "senior_mandatory_amort_pct", "cash_sweep_pct", "tax_rate"):
            if not 0 <= getattr(self, name) <= 1:
                e.append(f"{name} must be between 0 and 1 (decimals, e.g. 0.05 = 5%)")
        for name in ("transaction_fees_pct_ev", "financing_fees_pct_debt", "senior_oid_pct"):
            if not 0 <= getattr(self, name) <= 0.10:
                e.append(f"{name} must be between 0 and 0.10 (decimals, e.g. 0.02 = 2%)")
        if not isinstance(self.fee_amortization_years, int) or not 1 <= self.fee_amortization_years <= 15:
            e.append("fee_amortization_years must be an integer between 1 and 15")
        if not 0 < self.ebitda_margin < 1:
            e.append("ebitda_margin must be between 0 and 1")
        if not -0.5 < self.revenue_growth < 1:
            e.append("revenue_growth must be between -50% and +100%")
        if not -1 <= self.nwc_pct_of_rev_growth <= 1:
            e.append("nwc_pct_of_rev_growth must be between -1 and 1")
        if self.min_cash < 0 or self.rcf_commitment < 0:
            e.append("min_cash and rcf_commitment must be >= 0")
        if not isinstance(self.hold_period_years, int) or not 1 <= self.hold_period_years <= 15:
            e.append("hold_period_years must be an integer between 1 and 15")
        if not 0 <= self.senior_leverage_x <= self.total_leverage_x:
            e.append("need 0 <= senior_leverage_x <= total_leverage_x (sub debt cannot be negative)")
        if e:
            return e

        if self.entry_ebitda >= self.revenue_at_entry:
            e.append("entry_ebitda must be below revenue_at_entry (LTM margin < 100%)")
        equity = self.entry_ebitda * (self.entry_ev_multiple - self.total_leverage_x) + self.min_cash
        if equity <= 0:
            e.append("total debt >= total uses: sponsor equity would be zero or negative")
        return e

    def plan_value(self, driver: str, year: int) -> float:
        """Value of an operating driver in projection year `year` (1-based)."""
        seq = getattr(self, f"{driver}_by_year")
        return getattr(self, driver) if not seq else seq[min(year, len(seq)) - 1]

    @property
    def has_plan(self) -> bool:
        return any(getattr(self, name) for name in PLAN_FIELDS)

    @property
    def has_covenants(self) -> bool:
        return bool(self.max_net_leverage or self.max_net_leverage_by_year or self.min_interest_cover)


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
    rcf_draw: float
    rcf_repayment: float
    rcf_end_balance: float
    cash_end_balance: float
    financing_cost_amortization: float = 0.0   # non-cash: financing fees + OID


def build_sources_uses(a: Assumptions) -> dict:
    entry_ev = a.entry_ebitda * a.entry_ev_multiple
    total_debt = a.entry_ebitda * a.total_leverage_x
    senior_debt = a.entry_ebitda * a.senior_leverage_x
    sub_debt = total_debt - senior_debt

    transaction_fees = entry_ev * a.transaction_fees_pct_ev
    financing_fees = total_debt * a.financing_fees_pct_debt
    oid = senior_debt * a.senior_oid_pct   # lenders fund face x (1 - OID): the gap is a Use

    # min_cash is funded once at close and held on the balance sheet,
    # untouched by the sweep, and netted back off debt at exit (see
    # calculate_returns). It is a genuine Use of funds, not just a label.
    total_uses = entry_ev + a.min_cash + transaction_fees + financing_fees + oid
    sponsor_equity = total_uses - total_debt  # plug

    return {
        "entry_ev": entry_ev,
        "uses": {"purchase_of_enterprise": entry_ev, "minimum_cash_funding": a.min_cash,
                 "transaction_fees": transaction_fees, "financing_fees": financing_fees,
                 "oid_on_term_loan": oid},
        "capitalised_financing_costs": financing_fees + oid,
        "sources": {
            "senior_term_loan": senior_debt,
            "subordinated_notes": sub_debt,
            "sponsor_equity": sponsor_equity,
        },
        "total_uses": total_uses,
        "total_debt": total_debt,
    }


MAX_CIRCULARITY_ITERATIONS = 200
CIRCULARITY_TOLERANCE = 1e-10


def run_lbo(a: Assumptions) -> List[YearResult]:
    su = build_sources_uses(a)
    senior_balance = su["sources"]["senior_term_loan"]
    sub_balance = su["sources"]["subordinated_notes"]
    senior_original = senior_balance
    rcf_balance = 0.0
    cash = a.min_cash

    revenue = a.revenue_at_entry
    results = []
    annual_cost_amortization = su["capitalised_financing_costs"] / a.fee_amortization_years

    for year in range(1, a.hold_period_years + 1):
        prior_revenue = revenue
        revenue = revenue * (1 + a.plan_value("revenue_growth", year))

        def project(interest_expense: float) -> YearResult:
            """One year given its interest charge: P&L, FCF, waterfall and closing balances."""
            ebitda = revenue * a.plan_value("ebitda_margin", year)
            da = revenue * a.da_pct_revenue
            ebit = ebitda - da
            cost_amortization = annual_cost_amortization if year <= a.fee_amortization_years else 0.0

            ebt = ebit - interest_expense - cost_amortization
            tax = max(ebt, 0) * a.tax_rate
            net_income = ebt - tax

            capex = revenue * a.plan_value("capex_pct_revenue", year)
            nwc_change = (revenue - prior_revenue) * a.nwc_pct_of_rev_growth
            fcf_pre_sweep = net_income + da + cost_amortization - capex - nwc_change   # amortisation is non-cash

            mandatory_amort = min(senior_original * a.senior_mandatory_amort_pct, senior_balance)
            available = (cash - a.min_cash) + fcf_pre_sweep - mandatory_amort
            rcf_draw = rcf_repayment = cash_sweep = 0.0
            if available < 0:
                rcf_draw = -available  # shortfall funded by the revolver, never by thin air
                available = 0.0
            else:
                rcf_repayment = min(available, rcf_balance)
                available -= rcf_repayment
                cash_sweep = min(available * a.cash_sweep_pct, senior_balance - mandatory_amort)
                available -= cash_sweep

            return YearResult(
                year=year, revenue=revenue, ebitda=ebitda, da=da, ebit=ebit,
                interest_expense=interest_expense, ebt=ebt, tax=tax, net_income=net_income,
                capex=capex, nwc_change=nwc_change, fcf_pre_sweep=fcf_pre_sweep,
                mandatory_amort=mandatory_amort, cash_sweep=cash_sweep,
                senior_end_balance=senior_balance - mandatory_amort - cash_sweep, sub_end_balance=sub_balance,
                rcf_draw=rcf_draw, rcf_repayment=rcf_repayment, rcf_end_balance=rcf_balance + rcf_draw - rcf_repayment,
                cash_end_balance=a.min_cash + available,  # sub notes are bullet: surplus accumulates
                financing_cost_amortization=cost_amortization,
            )

        # Interest on opening balances: no circularity (see convention note above).
        interest = senior_balance * a.senior_rate + sub_balance * a.sub_rate + rcf_balance * a.rcf_rate
        r = project(interest)
        if a.interest_on_average_balance:
            # Circularity switch on: interest on (opening + closing) / 2, and the closing balances
            # depend on the interest through tax, FCF and the sweep. Fixed-point iteration: each
            # pass changes interest by < rate x (1 - tax) / 2 of the previous change, so it converges.
            for _ in range(MAX_CIRCULARITY_ITERATIONS):
                new = (a.senior_rate * (senior_balance + r.senior_end_balance) / 2
                       + a.sub_rate * (sub_balance + r.sub_end_balance) / 2
                       + a.rcf_rate * (rcf_balance + r.rcf_end_balance) / 2)
                if abs(new - interest) < CIRCULARITY_TOLERANCE:
                    break
                interest = new
                r = project(interest)
            else:
                raise RuntimeError(f"Average-balance interest did not converge in year {year}")

        senior_balance, rcf_balance, cash = r.senior_end_balance, r.rcf_end_balance, r.cash_end_balance
        results.append(r)

    return results


def covenant_tests(a: Assumptions, results: List[YearResult]) -> List[dict]:
    """Year-end maintenance tests. Headroom = EBITDA cushion before a breach (negative = breach)."""
    tests = []
    for r in results:
        net_debt = r.senior_end_balance + r.sub_end_balance + r.rcf_end_balance - r.cash_end_balance
        net_leverage = net_debt / r.ebitda if r.ebitda > 0 else float("inf")
        lev_limit = a.plan_value("max_net_leverage", r.year)
        lev_headroom = (1 - net_leverage / lev_limit) if lev_limit > 0 else None
        cover = r.ebitda / r.interest_expense if r.interest_expense > 0 else None
        cover_headroom = (1 - a.min_interest_cover / cover) if (a.min_interest_cover > 0 and cover) else None
        if a.min_interest_cover > 0 and cover is not None and cover <= 0:
            cover_headroom = -1.0
        tests.append({
            "year": r.year, "net_leverage": net_leverage, "leverage_limit": lev_limit or None,
            "leverage_headroom": lev_headroom, "interest_cover": cover,
            "cover_limit": a.min_interest_cover or None, "cover_headroom": cover_headroom,
            "breach": any(h is not None and h < 0 for h in (lev_headroom, cover_headroom)),
        })
    return tests


def calculate_returns(a: Assumptions, results: List[YearResult], su: dict) -> dict:
    last = results[-1]
    exit_ebitda = last.ebitda
    exit_ev = exit_ebitda * a.exit_ev_multiple
    exit_net_debt = (last.senior_end_balance + last.sub_end_balance + last.rcf_end_balance
                     - last.cash_end_balance)
    exit_equity_value = exit_ev - exit_net_debt

    entry_equity = su["sources"]["sponsor_equity"]
    moic = exit_equity_value / entry_equity
    irr = moic ** (1 / a.hold_period_years) - 1 if moic > 0 else -1.0

    peak_rcf = max(r.rcf_end_balance for r in results)
    min_coverage = min((r.ebitda / r.interest_expense for r in results if r.interest_expense > 0),
                       default=float("inf"))

    warnings = []
    if peak_rcf > a.rcf_commitment + 1e-9:
        warnings.append(f"Liquidity: RCF drawn up to {peak_rcf:.1f} vs {a.rcf_commitment:.1f} "
                        f"committed — the capital structure does not fund itself")
    elif peak_rcf > 0:
        warnings.append(f"RCF drawn (peak {peak_rcf:.1f}) to cover a cash shortfall")
    if min_coverage < 2.0:
        warnings.append(f"Minimum EBITDA / interest coverage {min_coverage:.2f}x (< 2.0x)")
    if exit_equity_value <= 0:
        warnings.append("Exit equity value <= 0: sponsor loses the entire investment")

    covenants = None
    if a.has_covenants:
        tests = covenant_tests(a, results)
        headrooms = [h for t in tests for h in (t["leverage_headroom"], t["cover_headroom"]) if h is not None]
        first = next((t for t in tests if t["breach"]), None)
        covenants = {"tests": tests, "first_breach_year": first["year"] if first else None,
                     "min_headroom": min(headrooms) if headrooms else None}
        if first:
            parts = []
            if first["leverage_headroom"] is not None and first["leverage_headroom"] < 0:
                parts.append(f"net leverage {first['net_leverage']:.2f}x vs max {first['leverage_limit']:.2f}x")
            if first["cover_headroom"] is not None and first["cover_headroom"] < 0:
                parts.append(f"interest cover {first['interest_cover']:.2f}x vs min {first['cover_limit']:.2f}x")
            warnings.append(f"Covenant breach in year {first['year']}: " + " and ".join(parts))

    return {
        "exit_ev": exit_ev,
        "exit_net_debt": exit_net_debt,
        "exit_equity_value": exit_equity_value,
        "entry_equity": entry_equity,
        "moic": moic,
        "irr": irr,
        "peak_rcf_draw": peak_rcf,
        "min_interest_coverage": min_coverage,
        "covenants": covenants,
        "warnings": warnings,
    }


def run_model(a: Assumptions) -> dict:
    """One-call entry point for the agent: S&U, yearly schedule, returns."""
    su = build_sources_uses(a)
    results = run_lbo(a)
    return {"sources_uses": su, "years": results, "returns": calculate_returns(a, results, su)}


if __name__ == "__main__":
    a = Assumptions()
    out = run_model(a)
    su, results, returns = out["sources_uses"], out["years"], out["returns"]

    print(f"=== {a.company_name} — LBO v1.1 ===\n")
    print("Sources & Uses (EUR mm)")
    print(f"  Entry EV:            {su['entry_ev']:.1f}  (={a.entry_ebitda:.1f} EBITDA x {a.entry_ev_multiple:.1f}x)")
    print(f"  Senior Term Loan:    {su['sources']['senior_term_loan']:.1f}  ({a.senior_leverage_x:.1f}x)")
    print(f"  Subordinated Notes:  {su['sources']['subordinated_notes']:.1f}  ({a.total_leverage_x - a.senior_leverage_x:.1f}x)")
    print(f"  Sponsor Equity:      {su['sources']['sponsor_equity']:.1f}")
    print()
    print(f"{'Yr':<4}{'Rev':>8}{'EBITDA':>9}{'IntExp':>9}{'NI':>8}{'FCF':>8}{'Amort':>8}{'Sweep':>8}"
          f"{'SrEnd':>8}{'SubEnd':>8}{'RCF':>7}{'Cash':>7}")
    for r in results:
        print(f"{r.year:<4}{r.revenue:>8.1f}{r.ebitda:>9.1f}{r.interest_expense:>9.1f}{r.net_income:>8.1f}"
              f"{r.fcf_pre_sweep:>8.1f}{r.mandatory_amort:>8.1f}{r.cash_sweep:>8.1f}{r.senior_end_balance:>8.1f}"
              f"{r.sub_end_balance:>8.1f}{r.rcf_end_balance:>7.1f}{r.cash_end_balance:>7.1f}")
    print()
    print("Exit / Returns")
    print(f"  Exit EBITDA:         {results[-1].ebitda:.1f}")
    print(f"  Exit EV:             {returns['exit_ev']:.1f}  (at {a.exit_ev_multiple:.1f}x)")
    print(f"  Exit Net Debt:       {returns['exit_net_debt']:.1f}")
    print(f"  Exit Equity Value:   {returns['exit_equity_value']:.1f}")
    print(f"  Entry Equity:        {returns['entry_equity']:.1f}")
    print(f"  MOIC:                {returns['moic']:.2f}x")
    print(f"  IRR:                 {returns['irr']*100:.1f}%")
    for w in returns["warnings"]:
        print(f"  WARNING: {w}")
