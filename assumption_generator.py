"""
Assumption generator — step 1 of the LBO agent.

    free-text description
        -> Claude (structured output): proposes every input, each tagged
           "provided" (stated in the text) or "estimated", with a rationale
        -> deterministic guardrails: unit fixes, EBITDA/revenue/margin
           reconciliation, sector benchmark clamps, cross-field rules
        -> lbo_engine.Assumptions + an audit trail of what changed and why

Design choice: the LLM never has the last word. Anything it ESTIMATES is
clamped to the sector ranges in sector_benchmarks.py; anything the user
PROVIDED is kept as given and only flagged. Every change is logged so the
Excel export can show value, source and rationale side by side.

Usage:
    uv run python assumption_generator.py "Italian packaging maker, EUR 400m revenue..."
    uv run python assumption_generator.py --file description.txt --run --json out.json --xlsx lbo.xlsx
"""

import argparse
import dataclasses
import json
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from lbo_engine import Assumptions, run_model
from sector_benchmarks import (
    GLOBAL_RANGES, MIN_CASH_PCT_REVENUE, MIN_EQUITY_PCT_OF_USES, MIN_SUB_SPREAD_OVER_SENIOR,
    RCF_COMMITMENT_X_EBITDA, SECTORS, SENIOR_SHARE_OF_TOTAL_LEVERAGE, benchmark_table_for_prompt,
)

DEFAULT_MODEL = os.environ.get("LBO_AGENT_MODEL", "claude-opus-5")
# Server-side refusal fallback is only offered on these models.
FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5-1"}

NUMERIC_FIELDS = [f.name for f in dataclasses.fields(Assumptions) if f.name != "company_name"]
# Fields expressed as decimals; a value above 1 almost certainly means "4" was meant as 4%.
PCT_FIELDS = {"revenue_growth", "ebitda_margin", "capex_pct_revenue", "da_pct_revenue",
              "nwc_pct_of_rev_growth", "senior_rate", "senior_mandatory_amort_pct", "sub_rate",
              "cash_sweep_pct", "rcf_rate", "tax_rate",
              "transaction_fees_pct_ev", "financing_fees_pct_debt", "senior_oid_pct"}
INT_FIELDS = ("hold_period_years", "fee_amortization_years")


# ---------------------------------------------------------------------------
# LLM output schema
# ---------------------------------------------------------------------------

class Estimate(BaseModel):
    value: float
    source: Literal["provided", "estimated"] = Field(
        description="'provided' only if the figure is explicitly stated in the description "
                    "(after unit conversion); otherwise 'estimated'.")
    rationale: str = Field(description="1-2 sentences: the cue in the description or the benchmark used.")


class ProposedAssumptions(BaseModel):
    company_name: str = Field(description="Name from the description, or a short 'Project X' codename.")
    sector: Literal[tuple(SECTORS)]  # type: ignore[valid-type]
    sector_rationale: str
    currency: str = Field(description="ISO code of the figures, e.g. EUR. Amounts are in millions.")
    revenue_at_entry: Estimate = Field(description="LTM revenue, millions.")
    entry_ebitda: Estimate = Field(description="LTM EBITDA, millions. Must equal revenue x margin.")
    ebitda_margin: Estimate = Field(description="Decimal, e.g. 0.18.")
    entry_ev_multiple: Estimate = Field(description="Entry EV / LTM EBITDA, x.")
    revenue_growth: Estimate = Field(description="Annual growth over the hold, decimal.")
    capex_pct_revenue: Estimate
    da_pct_revenue: Estimate
    nwc_pct_of_rev_growth: Estimate = Field(
        description="Cash absorbed by NWC per unit of revenue growth, decimal; negative if NWC releases cash.")
    total_leverage_x: Estimate = Field(description="Total funded debt / EBITDA at entry, x.")
    senior_leverage_x: Estimate = Field(description="Term Loan / EBITDA, x; <= total leverage.")
    senior_rate: Estimate = Field(description="All-in cash interest rate on the Term Loan, decimal.")
    senior_mandatory_amort_pct: Estimate = Field(description="% of original TL principal per year, decimal.")
    sub_rate: Estimate = Field(description="All-in rate on subordinated notes, decimal.")
    cash_sweep_pct: Estimate = Field(description="Share of surplus cash prepaying the TL, decimal.")
    rcf_commitment: Estimate = Field(description="Revolver size, millions, undrawn at close.")
    rcf_rate: Estimate
    tax_rate: Estimate = Field(description="Effective cash tax rate, decimal.")
    min_cash: Estimate = Field(description="Operating cash kept on balance sheet, millions.")
    transaction_fees_pct_ev: Estimate = Field(
        description="M&A advisory, legal and due diligence fees paid at close, as a decimal share of EV.")
    financing_fees_pct_debt: Estimate = Field(
        description="Arrangement / underwriting fees on the debt paid at close, as a decimal share of funded debt.")
    senior_oid_pct: Estimate = Field(
        description="Original issue discount on the Term Loan, decimal (0.005 = issued at 99.5).")
    fee_amortization_years: Estimate = Field(
        description="Years over which financing fees and OID are amortised (the debt tenor), whole years.")
    hold_period_years: Estimate = Field(description="Whole years.")
    exit_ev_multiple: Estimate = Field(description="Exit EV / EBITDA, x. Base case: <= entry multiple.")
    key_risks: List[str] = Field(description="3-5 deal-specific risks to underwrite.")


SYSTEM_PROMPT = f"""You are a private equity associate building the BASE CASE assumptions for a \
leveraged buyout model of the company described by the user. The model is a simple annual LBO: \
flat revenue growth and margins, a Term Loan with mandatory amortization and cash sweep, bullet \
subordinated notes, a revolver for shortfalls, exit at a multiple of final-year EBITDA.

How to fill each input:
- Mark a value "provided" only when the description states it explicitly. Convert it to the model's \
units (millions of currency; percentages as decimals, 0.04 = 4%) and still mark it "provided".
- Everything else is "estimated": derive it from cues in the description (business model, growth \
story, asset intensity, customer concentration, geography, size) and from the benchmark ranges \
below. Stay near the middle of the sector range unless the description gives a concrete reason \
to move, and say what that reason is.
- If the size of the company is not given, infer it from any cue available (employees, number of \
sites, market position) and say explicitly in the rationale that size is a guess.
- entry_ebitda must equal revenue_at_entry x ebitda_margin.
- This is a base case: exit multiple at or below the entry multiple, no heroic growth or margin story.
- Transaction costs, unless the description gives them: M&A fees around 1.5-2.5% of EV (higher \
for small deals), financing fees around 2-3% of the debt, Term Loan OID 0-1%, amortised over a 6-7 \
year debt tenor.
- Tax rate: use the statutory corporate rate of the company's main country (e.g. Italy IRES + IRAP \
~28%, Germany ~30%, France 25%, UK 25%, Spain 25%).
- Financing must be consistent with today's market for a company of this size and sector: \
smaller or cyclical companies get less leverage and pricier debt.
- Write every rationale, the sector rationale and the key risks in the language of the description. \
Keep rationales to one or two sentences.

Benchmark ranges (illustrative, European mid-market LBO, base case):
{benchmark_table_for_prompt()}
"""


def propose_with_claude(description: str, *, model: str = DEFAULT_MODEL, client=None,
                        on_usage: Optional[Callable[[str, object], None]] = None) -> ProposedAssumptions:
    """One structured-output call: description in, validated ProposedAssumptions out."""
    import anthropic

    client = client or anthropic.Anthropic()
    extra = {}
    if model in FALLBACK_MODELS:
        # If a safety classifier declines, the API re-runs the call on a fallback model.
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}

    response = client.beta.messages.parse(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user",
                   "content": f"<company_description>\n{description.strip()}\n</company_description>"}],
        output_config={"effort": "high"},
        output_format=ProposedAssumptions,
        **extra,
    )
    if on_usage:
        on_usage(response.model, response.usage)
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined the request: {response.stop_details}")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise RuntimeError(f"No complete structured output (stop_reason={response.stop_reason})")
    return response.parsed_output


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

@dataclass
class FieldTrace:
    value: float
    source: str                   # provided | estimated | derived | adjusted
    rationale: str
    llm_value: float              # what Claude originally proposed
    notes: List[str] = field(default_factory=list)


@dataclass
class GenerationResult:
    assumptions: Assumptions
    sector: str
    sector_rationale: str
    currency: str
    trace: Dict[str, FieldTrace]
    adjustments: List[str]
    warnings: List[str]
    key_risks: List[str]

    def to_dict(self) -> dict:
        return {
            "company_name": self.assumptions.company_name,
            "sector": self.sector,
            "sector_label": SECTORS[self.sector].label,
            "sector_rationale": self.sector_rationale,
            "currency": self.currency,
            "assumptions": dataclasses.asdict(self.assumptions),
            "trace": {k: dataclasses.asdict(v) for k, v in self.trace.items()},
            "adjustments": self.adjustments,
            "warnings": self.warnings,
            "key_risks": self.key_risks,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GenerationResult":
        """Inverse of to_dict: rebuild a saved result without calling Claude again."""
        if d.get("sector") not in SECTORS:
            raise ValueError(f"Unknown sector {d.get('sector')!r} in saved base case")
        return cls(
            assumptions=Assumptions(**d["assumptions"]),
            sector=d["sector"],
            sector_rationale=d.get("sector_rationale", ""),
            currency=d.get("currency", "EUR"),
            trace={k: FieldTrace(**v) for k, v in d.get("trace", {}).items()},
            adjustments=list(d.get("adjustments", [])),
            warnings=list(d.get("warnings", [])),
            key_risks=list(d.get("key_risks", [])),
        )


def _fmt(name: str, v: float) -> str:
    if name in PCT_FIELDS:
        return f"{v * 100:.2f}%"
    if name.endswith("_x") or name.endswith("multiple"):
        return f"{v:.2f}x"
    return f"{v:,.1f}" if name != "hold_period_years" else f"{v:g}"


class _Guardrails:
    """Mutable working state for one pass of guardrails over a proposal."""

    def __init__(self, p: ProposedAssumptions):
        self.p = p
        self.sector = SECTORS[p.sector]
        self.vals = {n: getattr(p, n).value for n in NUMERIC_FIELDS}
        self.provided = {n for n in NUMERIC_FIELDS if getattr(p, n).source == "provided"}
        self.trace = {n: FieldTrace(getattr(p, n).value, getattr(p, n).source, getattr(p, n).rationale,
                                    getattr(p, n).value) for n in NUMERIC_FIELDS}
        self.adjustments: List[str] = []
        self.warnings: List[str] = []

    def set(self, name: str, new: float, reason: str, kind: str = "adjusted"):
        old = self.vals[name]
        if math.isclose(old, new, rel_tol=1e-12, abs_tol=1e-12):
            if kind == "derived":  # consistent already, but the value is still computed, not chosen
                self.trace[name].source = kind
            return
        self.vals[name] = new
        t = self.trace[name]
        t.value = new
        t.source = kind
        t.notes.append(reason)
        self.adjustments.append(f"{name}: {_fmt(name, old)} -> {_fmt(name, new)} ({reason})")

    def bound(self, name: str, lo: float, hi: float, what: str):
        """Clamp an estimated value into [lo, hi]; only flag a provided one."""
        v = self.vals[name]
        if lo - 1e-12 <= v <= hi + 1e-12:
            return
        if name in self.provided:
            self.warnings.append(f"{name} = {_fmt(name, v)} (provided) is outside {what} "
                                 f"{_fmt(name, lo)}-{_fmt(name, hi)}; kept as given")
        else:
            self.set(name, min(max(v, lo), hi), f"outside {what} {_fmt(name, lo)}-{_fmt(name, hi)}")

    # -- passes, in order ---------------------------------------------------

    def fix_units(self):
        for n in PCT_FIELDS:
            if 1 < abs(self.vals[n]) <= 100:
                self.set(n, self.vals[n] / 100, "percentage given as a whole number, converted to decimal",
                         kind="provided" if n in self.provided else "adjusted")
        for name in INT_FIELDS:
            years = self.vals[name]
            if years != round(years):
                self.set(name, float(round(years)), "rounded to whole years")

    def reconcile_ebitda(self):
        """entry_ebitda = revenue x margin must hold exactly; provided figures win."""
        ebitda_given = "entry_ebitda" in self.provided
        revenue_given = "revenue_at_entry" in self.provided
        v = self.vals
        if ebitda_given and revenue_given:
            self.set("ebitda_margin", v["entry_ebitda"] / v["revenue_at_entry"],
                     "implied by provided EBITDA / revenue", kind="derived")
            self.provided.add("ebitda_margin")        # user's numbers: flag, never change
            self.bound("ebitda_margin", *self.sector.ebitda_margin, f"{self.p.sector} margin range")
            return
        self.bound("ebitda_margin", *self.sector.ebitda_margin, f"{self.p.sector} margin range")
        if ebitda_given:
            self.set("revenue_at_entry", v["entry_ebitda"] / v["ebitda_margin"],
                     "implied by provided EBITDA / margin", kind="derived")
        else:
            self.set("entry_ebitda", v["revenue_at_entry"] * v["ebitda_margin"],
                     "revenue x margin", kind="derived")

    def sector_and_market_ranges(self):
        s, label = self.sector, self.p.sector
        for name in ("entry_ev_multiple", "revenue_growth", "capex_pct_revenue", "da_pct_revenue",
                     "nwc_pct_of_rev_growth", "total_leverage_x"):
            self.bound(name, *getattr(s, name), f"{label} range")
        for name, (lo, hi) in GLOBAL_RANGES.items():
            self.bound(name, lo, hi, "market range")
        ebitda, revenue = self.vals["entry_ebitda"], self.vals["revenue_at_entry"]
        self.bound("rcf_commitment", *(x * ebitda for x in RCF_COMMITMENT_X_EBITDA), "RCF size range")
        self.bound("min_cash", *(x * revenue for x in MIN_CASH_PCT_REVENUE), "minimum cash range")

    def cross_field_rules(self):
        v = self.vals
        # 1. No multiple expansion in the base case.
        if v["exit_ev_multiple"] > v["entry_ev_multiple"]:
            if "exit_ev_multiple" in self.provided:
                self.warnings.append("Exit multiple above entry (provided): returns rely on multiple expansion")
            else:
                self.set("exit_ev_multiple", v["entry_ev_multiple"], "base case: no multiple expansion")

        # 2. Sponsor equity floor — cut leverage (keeping the senior/sub mix) if the cheque is too thin.
        uses = v["entry_ebitda"] * v["entry_ev_multiple"] + v["min_cash"]
        max_total_x = (1 - MIN_EQUITY_PCT_OF_USES) * uses / v["entry_ebitda"]
        if v["total_leverage_x"] > max_total_x + 1e-9:
            if "total_leverage_x" in self.provided:
                self.warnings.append(f"Sponsor equity below {MIN_EQUITY_PCT_OF_USES:.0%} of uses "
                                     f"with the provided leverage")
            else:
                share = v["senior_leverage_x"] / v["total_leverage_x"] if v["total_leverage_x"] else 1.0
                new_total = math.floor(max_total_x * 20) / 20  # round down to 0.05x
                self.set("total_leverage_x", new_total,
                         f"sponsor equity must be >= {MIN_EQUITY_PCT_OF_USES:.0%} of uses")
                if "senior_leverage_x" not in self.provided:
                    self.set("senior_leverage_x", new_total * share, "scaled with total leverage")

        # 3. Senior tranche between 50% and 100% of total. Above 100% is structurally
        #    impossible (negative sub debt), so it is corrected even when provided.
        lo, hi = (x * v["total_leverage_x"] for x in SENIOR_SHARE_OF_TOTAL_LEVERAGE)
        if v["senior_leverage_x"] > hi:
            if "senior_leverage_x" in self.provided:
                self.warnings.append("Provided senior leverage exceeded total leverage; capped at total")
            self.set("senior_leverage_x", hi, "senior debt cannot exceed total debt")
        else:
            self.bound("senior_leverage_x", lo, hi, "senior share of total leverage")

        # 4. Subordinated debt must price above senior.
        floor = v["senior_rate"] + MIN_SUB_SPREAD_OVER_SENIOR
        if v["sub_rate"] < floor:
            if "sub_rate" in self.provided:
                self.warnings.append("Provided sub rate is less than senior + 150bps")
            else:
                self.set("sub_rate", floor, "sub debt must price >= senior + 150bps")

        # 5. Soft checks — flag, never change.
        if v["da_pct_revenue"] > 1.5 * v["capex_pct_revenue"]:
            self.warnings.append("D&A well above capex: the asset base is shrinking over the hold")
        interest = (v["entry_ebitda"] * (v["senior_leverage_x"] * v["senior_rate"]
                    + (v["total_leverage_x"] - v["senior_leverage_x"]) * v["sub_rate"]))
        if interest > 0 and v["entry_ebitda"] / interest < 2.0:
            self.warnings.append(f"Entry EBITDA / interest only {v['entry_ebitda'] / interest:.2f}x")

    def result(self) -> GenerationResult:
        kwargs = dict(self.vals)
        for name in INT_FIELDS:
            kwargs[name] = int(kwargs[name])
        assumptions = Assumptions(company_name=self.p.company_name, **kwargs)  # engine validation
        return GenerationResult(
            assumptions=assumptions, sector=self.p.sector, sector_rationale=self.p.sector_rationale,
            currency=self.p.currency, trace=self.trace, adjustments=self.adjustments,
            warnings=self.warnings, key_risks=list(self.p.key_risks),
        )


def apply_guardrails(proposal: ProposedAssumptions) -> GenerationResult:
    g = _Guardrails(proposal)
    g.fix_units()
    g.reconcile_ebitda()          # before ranges: RCF / min cash bounds scale with EBITDA and revenue
    g.sector_and_market_ranges()
    g.cross_field_rules()
    return g.result()


def generate_assumptions(
    description: str,
    *,
    model: str = DEFAULT_MODEL,
    llm: Optional[Callable[[str], ProposedAssumptions]] = None,
    on_usage: Optional[Callable[[str, object], None]] = None,
) -> GenerationResult:
    """Description -> guarded Assumptions. `llm` is injectable for tests / other providers;
    `on_usage(model, usage)` receives the token usage of the Claude call."""
    proposal = (llm or (lambda d: propose_with_claude(d, model=model, on_usage=on_usage)))(description)
    return apply_guardrails(proposal)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def print_result(res: GenerationResult):
    a = res.assumptions
    print(f"\n=== {a.company_name} — {SECTORS[res.sector].label} ({res.currency} mm) ===")
    print(f"Sector: {res.sector_rationale}\n")
    print(f"{'Assumption':<28}{'Value':>11}  {'Source':<10}Rationale")
    for name, t in res.trace.items():
        note = f" [{'; '.join(t.notes)}]" if t.notes else ""
        print(f"{name:<28}{_fmt(name, t.value):>11}  {t.source:<10}{t.rationale}{note}")
    for title, items in (("Guardrail adjustments", res.adjustments), ("Warnings", res.warnings),
                         ("Key risks", res.key_risks)):
        if items:
            print(f"\n{title}:")
            for item in items:
                print(f"  - {item}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate LBO assumptions from a company description.")
    ap.add_argument("description", nargs="?", help="Company / sector description (or use --file / stdin)")
    ap.add_argument("--file", help="Read the description from a text file")
    ap.add_argument("--json", help="Write the full result (assumptions + audit trail) to this JSON file")
    ap.add_argument("--run", action="store_true", help="Also run the LBO engine and print returns")
    ap.add_argument("--xlsx", help="Also export the live Excel model to this path")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args(argv)

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            description = f.read()
    elif args.description:
        description = args.description
    elif not sys.stdin.isatty():
        description = sys.stdin.read()
    else:
        ap.error("provide a description, --file, or pipe text on stdin")

    res = generate_assumptions(description, model=args.model)
    print_result(res)

    if args.run:
        out = run_model(res.assumptions)
        r = out["returns"]
        print(f"\nEngine: entry equity {r['entry_equity']:,.1f} -> exit equity {r['exit_equity_value']:,.1f} | "
              f"MOIC {r['moic']:.2f}x | IRR {r['irr'] * 100:.1f}%")
        for w in r["warnings"]:
            print(f"  WARNING: {w}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(res.to_dict(), f, indent=2, ensure_ascii=False)
        print(f"\nSaved to {args.json}")

    if args.xlsx:
        from excel_export import export_to_excel
        print(f"Excel model: {export_to_excel(res.assumptions, args.xlsx, res.to_dict())}")


if __name__ == "__main__":
    main()
