"""
LBO agent — step 2: ties the pieces together.

    user request (company description + questions)
        -> Claude, in a tool-use loop, decides what to run:
             generate_base_case   description -> guarded assumptions + base returns
             run_scenario         overrides on an existing scenario -> returns
             solve_for_target     "what entry multiple gives a 20% IRR?" (bisection)
             compare_scenarios    side-by-side table
             export_excel         live-formula workbook for one scenario
        -> an investment-committee style answer + Excel files

Design choice: Claude orchestrates and explains, it never does the arithmetic.
Every number in the answer comes from a tool, and every tool is deterministic
code already covered by tests (engine, guardrails, exporter). The agent adds
judgement: which scenarios matter, whether the base case is financeable,
which threshold questions to answer.

Usage:
    uv run python lbo_agent.py "Produttore italiano di valvole, fatturato 320m. Qual è il prezzo massimo per un IRR del 20%?"
    uv run python lbo_agent.py --file request.txt --chat     # follow-up questions afterwards
    uv run python lbo_agent.py --load output/Project_X_deal.json --chat   # reuse a saved base case

Reproducibility: the base case comes from an LLM, so two runs on the same text
give slightly different assumptions. Every run therefore saves a deal file
(base case with its audit trail + all scenarios) to output/<company>_deal.json;
--load reuses it exactly, without asking Claude to re-estimate anything.
"""

import argparse
import dataclasses
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Literal, Optional

from anthropic.lib.tools import ToolError

from assumption_generator import (
    DEFAULT_MODEL, FALLBACK_MODELS, NUMERIC_FIELDS, GenerationResult, ProposedAssumptions,
    generate_assumptions,
)
from excel_export import export_to_excel
from lbo_engine import COVENANT_FIELDS, PLAN_DRIVERS, PLAN_FIELDS, SCHEDULE_FIELDS, Assumptions, run_model

OVERRIDABLE = NUMERIC_FIELDS + list(COVENANT_FIELDS)         # flat values accepted in `overrides`
SCHEDULE_DRIVERS = PLAN_DRIVERS + ("max_net_leverage",)       # per-year lists accepted in `plan`
from sector_benchmarks import GLOBAL_RANGES, SECTORS

MAX_TOOL_ROUNDS = 25
DEAL_FILE_FORMAT = "lbo-deal"

# First-party list prices, USD per million tokens (input, output). Cache writes bill at
# 1.25x input, cache reads at 0.1x input.
PRICES_PER_MTOK = {
    "claude-opus-5": (5.0, 25.0), "claude-opus-5-5": (4.0, 20.0), "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0), "claude-fable-5-1": (10.0, 50.0),
}


def fmt_number(v) -> str:
    """g-format a value or a year-by-year list (audit notes)."""
    if isinstance(v, (list, tuple)):
        return "/".join(f"{x:g}" for x in v)
    return "flat" if v is None else f"{v:g}"


def usage_cost_usd(usage_log: List[dict]) -> Optional[float]:
    """Cost of a list of {"model", "input_tokens", ...} records; None if a model has no known price."""
    total = 0.0
    for u in usage_log:
        price = PRICES_PER_MTOK.get(u["model"])
        if price is None:
            return None
        p_in, p_out = price
        total += (u["input_tokens"] * p_in + u["output_tokens"] * p_out
                  + u["cache_creation_input_tokens"] * p_in * 1.25
                  + u["cache_read_input_tokens"] * p_in * 0.1) / 1e6
    return total
DEAL_FILE_VERSION = 1

# Search domain for solve_for_target, per variable.
SOLVABLE: Dict[str, tuple] = {
    "entry_ev_multiple": (2.0, 30.0),
    "exit_ev_multiple": (1.0, 30.0),
    "total_leverage_x": (0.0, 12.0),
    "revenue_growth": (-0.30, 0.50),
    "ebitda_margin": (0.01, 0.80),
    "senior_rate": (0.0, 0.25),
}


# ---------------------------------------------------------------------------
# Deal session: state + deterministic tool implementations
# ---------------------------------------------------------------------------

@dataclass
class Scenario:
    name: str
    assumptions: Assumptions
    based_on: Optional[str]
    rationale: str
    overrides: Dict[str, float] = field(default_factory=dict)


class DealSession:
    """Everything the agent has built so far. Tool methods return JSON-able dicts
    and raise ToolError with an actionable message when the request is invalid."""

    def __init__(self, output_dir: str = "output", model: str = DEFAULT_MODEL,
                 llm: Optional[Callable[[str], ProposedAssumptions]] = None):
        self.output_dir = Path(output_dir)
        self.model = model
        self.llm = llm                      # injectable proposal step (tests / other providers)
        self.generation: Optional[GenerationResult] = None
        self.description: Optional[str] = None
        self.provenance: Dict[str, str] = {}     # how the base case came to be (model, date, file)
        self.scenarios: Dict[str, Scenario] = {}
        self.exports: List[str] = []
        self.usage_log: List[dict] = []          # one record per Claude call (agent turns + generator)
        self.history: List[dict] = []            # {question, answer, at}: what was asked and answered
        self.usage_tracked = True                # False for projects saved before usage was recorded

    def record_usage(self, model: str, usage) -> None:
        self.usage_log.append({
            "model": model,
            "input_tokens": usage.input_tokens or 0,
            "output_tokens": usage.output_tokens or 0,
            "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None) or 0,
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None) or 0,
        })

    # -- persistence ------------------------------------------------------------

    def company_stem(self) -> str:
        name = self.generation.assumptions.company_name if self.generation else "lbo"
        return re.sub(r"[^A-Za-z0-9]+", "_", name)[:40].strip("_") or "lbo"

    def to_dict(self) -> dict:
        if not self.generation:
            raise ValueError("Nothing to save: no base case yet")
        return {
            "format": DEAL_FILE_FORMAT,
            "version": DEAL_FILE_VERSION,
            "saved_at": dt.datetime.now().isoformat(timespec="seconds"),
            "description": self.description,
            "provenance": self.provenance,
            "base": self.generation.to_dict(),
            "scenarios": [
                {"name": sc.name, "based_on": sc.based_on, "rationale": sc.rationale,
                 "overrides": sc.overrides, "assumptions": dataclasses.asdict(sc.assumptions)}
                for sc in self.scenarios.values() if sc.name != "base"
            ],
            "history": self.history,             # the agent's answers survive a reload
            "usage": self.usage_log,             # so does the API cost of the whole project
        }

    def save(self, path: Optional[str] = None) -> str:
        """Write the deal file (default: <output_dir>/<company>_deal.json) and return its path."""
        target = Path(path) if path else self.output_dir / f"{self.company_stem()}_deal.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return str(target)

    @classmethod
    def load(cls, path: str, output_dir: str = "output", model: str = DEFAULT_MODEL,
             llm: Optional[Callable[[str], ProposedAssumptions]] = None) -> "DealSession":
        """Rebuild a session from a deal file, or from the JSON written by
        assumption_generator.py --json (base case only). Nothing is re-estimated."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("format") == DEAL_FILE_FORMAT:
            if data.get("version", 0) > DEAL_FILE_VERSION:
                raise ValueError(f"{path} was written by a newer version (v{data['version']})")
            base, saved_scenarios = data["base"], data.get("scenarios", [])
        elif "assumptions" in data and "trace" in data:
            base, saved_scenarios = data, []
        else:
            raise ValueError(f"{path} is not a deal file or a generator JSON")

        session = cls(output_dir, model=model, llm=llm)
        session.generation = GenerationResult.from_dict(base)
        session.description = data.get("description")
        session.provenance = {**data.get("provenance", {}), "loaded_from": str(path)}
        session.scenarios = {"base": Scenario("base", session.generation.assumptions, None, "Generated base case")}
        session.history = list(data.get("history", []))
        session.usage_log = list(data.get("usage", []))
        session.usage_tracked = "usage" in data
        for sc in saved_scenarios:
            if sc["based_on"] not in session.scenarios:
                raise ValueError(f"Scenario {sc['name']!r} is based on unknown {sc['based_on']!r}")
            session.scenarios[sc["name"]] = Scenario(sc["name"], Assumptions(**sc["assumptions"]),
                                                     sc["based_on"], sc["rationale"], sc.get("overrides", {}))
        return session

    def provenance_text(self) -> str:
        p = self.provenance
        text = f"Estimated by {p.get('model', 'Claude')} on {p.get('generated_at', 'unknown date')}"
        if p.get("loaded_from"):
            text += f"; reloaded unchanged from {Path(p['loaded_from']).name}"
        return text

    # -- helpers --------------------------------------------------------------

    def _scenario(self, name: str) -> Scenario:
        if name not in self.scenarios:
            raise ToolError(f"Unknown scenario '{name}'. Existing: {sorted(self.scenarios) or 'none'}. "
                            f"Call generate_base_case first if there is no base case.")
        return self.scenarios[name]

    @staticmethod
    def apply_overrides(base: Assumptions, overrides: Dict[str, float], reprice_entry: bool = False,
                        plan: Optional[Dict[str, List[float]]] = None) -> Assumptions:
        """Operating overrides change the projection only: price and debt stay as signed
        (they are multiples of LTM EBITDA, which does not move). reprice_entry=True is the
        pre-signing view: the new margin is already true today, so LTM EBITDA -- and with it
        price and debt -- follow it."""
        unknown = sorted(set(overrides) - set(OVERRIDABLE))
        if unknown:
            raise ToolError(f"Unknown assumption(s) {unknown}. Valid names: {OVERRIDABLE}. "
                            f"Year-by-year values go in `plan`, not `overrides`.")
        plan = plan or {}
        bad_plan = sorted(set(plan) - set(SCHEDULE_DRIVERS))
        if bad_plan:
            raise ToolError(f"Unknown plan driver(s) {bad_plan}. Valid: {list(SCHEDULE_DRIVERS)}")
        if reprice_entry and "entry_ebitda" in overrides:
            raise ToolError("Pass either entry_ebitda or reprice_entry=true, not both: "
                            "reprice_entry derives entry_ebitda from revenue x the new margin.")
        vals = dataclasses.asdict(base)
        ltm_margin = base.entry_ebitda / base.revenue_at_entry
        # Changing total leverage alone keeps the senior / sub mix of the base.
        if "total_leverage_x" in overrides and "senior_leverage_x" not in overrides and base.total_leverage_x > 0:
            vals["senior_leverage_x"] = overrides["total_leverage_x"] * base.senior_leverage_x / base.total_leverage_x
        vals.update(overrides)
        for driver in SCHEDULE_DRIVERS:
            if driver in plan:                              # a new year-by-year profile
                vals[f"{driver}_by_year"] = list(plan[driver]) or None
            elif driver in overrides:                       # a flat value replaces any profile
                vals[f"{driver}_by_year"] = None
        if "entry_ebitda" not in overrides:
            if reprice_entry:
                vals["entry_ebitda"] = vals["revenue_at_entry"] * vals["ebitda_margin"]
            elif "revenue_at_entry" in overrides:   # a different-sized company, same LTM margin
                vals["entry_ebitda"] = vals["revenue_at_entry"] * ltm_margin
        for name in ("hold_period_years", "fee_amortization_years"):
            vals[name] = int(round(vals[name]))
        try:
            return Assumptions(**vals)
        except ValueError as exc:
            raise ToolError(f"{exc}\n(Percentages are decimals: 0.04 = 4%.)") from None

    def _benchmark_notes(self, a: Assumptions, fields_to_check) -> List[str]:
        if not self.generation:
            return []
        sector = SECTORS[self.generation.sector]
        notes = []
        for name in fields_to_check:
            if name in PLAN_FIELDS:
                driver = name[: -len("_by_year")]
                lo, hi = getattr(sector, driver)
                for year, v in enumerate(getattr(a, name) or (), start=1):
                    if not lo <= v <= hi:
                        notes.append(f"{driver} year {year} = {v:g} is outside the {self.generation.sector} "
                                     f"benchmark {lo:g}-{hi:g}")
                continue
            rng = getattr(sector, name, None) or GLOBAL_RANGES.get(name)
            v = getattr(a, name)
            if rng and not rng[0] <= v <= rng[1]:
                notes.append(f"{name} = {v:g} is outside the {self.generation.sector} benchmark "
                             f"{rng[0]:g}-{rng[1]:g}")
        return notes

    @staticmethod
    def _summary(name: str, a: Assumptions) -> dict:
        out = run_model(a)
        r, years, su = out["returns"], out["years"], out["sources_uses"]
        return {
            "scenario": name,
            "moic": round(r["moic"], 3),
            "irr": round(r["irr"], 4),
            "entry_equity": round(r["entry_equity"], 1),
            "equity_pct_of_uses": round(r["entry_equity"] / su["total_uses"], 3),
            "entry_ev": round(su["entry_ev"], 1),
            "entry_ltm_ebitda": round(a.entry_ebitda, 1),
            "entry_debt": round(su["total_debt"], 1),
            "entry_fees_and_oid": round(su["uses"]["transaction_fees"] + su["capitalised_financing_costs"], 1),
            "ltm_margin": round(a.entry_ebitda / a.revenue_at_entry, 4),
            "projected_margin": round(a.plan_value("ebitda_margin", 1), 4),
            **({"operating_plan": {d: [round(a.plan_value(d, t), 4) for t in range(1, a.hold_period_years + 1)]
                                   for d in PLAN_DRIVERS}} if a.has_plan else {}),
            "exit_ev": round(r["exit_ev"], 1),
            "exit_equity": round(r["exit_equity_value"], 1),
            "exit_net_debt": round(r["exit_net_debt"], 1),
            "entry_total_leverage_x": round(a.total_leverage_x, 2),
            "entry_senior_leverage_x": round(a.senior_leverage_x, 2),
            "entry_sub_leverage_x": round(a.total_leverage_x - a.senior_leverage_x, 2),
            "net_debt_to_ebitda_by_year": [
                round((y.senior_end_balance + y.sub_end_balance + y.rcf_end_balance - y.cash_end_balance)
                      / y.ebitda, 2) for y in years],
            "revenue_by_year": [round(y.revenue, 1) for y in years],
            "ebitda_by_year": [round(y.ebitda, 1) for y in years],
            "fcf_by_year": [round(y.fcf_pre_sweep, 1) for y in years],
            "peak_rcf_draw": round(r["peak_rcf_draw"], 1),
            "min_interest_coverage": (round(r["min_interest_coverage"], 2)
                                      if r["min_interest_coverage"] != float("inf") else None),
            "engine_warnings": r["warnings"],
            **({"covenants": {
                "max_net_leverage_by_year": [t["leverage_limit"] for t in r["covenants"]["tests"]],
                "net_leverage_by_year": [round(t["net_leverage"], 2) for t in r["covenants"]["tests"]],
                "min_interest_cover": a.min_interest_cover or None,
                "first_breach_year": r["covenants"]["first_breach_year"],
                "lowest_headroom": (round(r["covenants"]["min_headroom"], 3)
                                    if r["covenants"]["min_headroom"] is not None else None),
            }} if r["covenants"] else {}),
        }

    # -- tools ------------------------------------------------------------------

    def generate_base_case(self, description: str, regenerate: bool = False) -> dict:
        if self.generation and not regenerate:
            view = self._base_case_view()
            view["note"] = ("A base case already exists (" + self.provenance_text() + "). Returned unchanged, "
                            "not re-estimated. Use regenerate=true only if the user asks to re-estimate it or "
                            "describes a different company.")
            return view
        res = generate_assumptions(description, model=self.model, llm=self.llm, on_usage=self.record_usage)
        self.generation = res
        self.description = description
        self.provenance = {"model": self.model, "generated_at": dt.datetime.now().isoformat(timespec="seconds")}
        self.scenarios = {"base": Scenario("base", res.assumptions, None, "Generated base case")}
        return self._base_case_view()

    def _base_case_view(self) -> dict:
        res = self.generation
        return {
            "company": res.assumptions.company_name,
            "sector": res.sector,
            "sector_rationale": res.sector_rationale,
            "currency": res.currency,
            "assumptions": {n: {"value": ([round(x, 4) for x in t.value] if isinstance(t.value, (list, tuple))
                                          else round(t.value, 4)), "source": t.source, "rationale": t.rationale}
                            for n, t in res.trace.items()},
            "guardrail_adjustments": res.adjustments,
            "guardrail_warnings": res.warnings,
            "key_risks": res.key_risks,
            "base_case_results": self._summary("base", res.assumptions),
            "existing_scenarios": [n for n in self.scenarios if n != "base"],
        }

    def run_scenario(self, name: str, overrides: Dict[str, float], rationale: str, based_on: str = "base",
                     reprice_entry: bool = False, plan: Optional[Dict[str, List[float]]] = None) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,40}", name):
            raise ToolError("Scenario name: 1-40 letters, digits, '_' or '-'.")
        if name == "base":
            raise ToolError("'base' is the generated base case; pick another name.")
        parent = self._scenario(based_on)
        a = self.apply_overrides(parent.assumptions, overrides, reprice_entry, plan)
        # Record what actually moved (incl. knock-on changes such as the senior tranche),
        # not just what was asked for.
        changed = {k: getattr(a, k) for k in OVERRIDABLE + list(SCHEDULE_FIELDS)
                   if getattr(a, k) != getattr(parent.assumptions, k)}
        merged = {**parent.overrides, **changed}
        self.scenarios[name] = Scenario(name, a, based_on, rationale, merged)
        result = self._summary(name, a)
        result["overrides_vs_base"] = merged
        result["entry_terms"] = ("re-priced: price and debt follow the new LTM EBITDA"
                                 if a.entry_ebitda != parent.assumptions.entry_ebitda
                                 else "as signed: same price and debt as the parent scenario")
        result["benchmark_notes"] = self._benchmark_notes(a, merged)
        return result

    def solve_for_target(self, variable: str, metric: Literal["irr", "moic"], target: float,
                         scenario: str = "base", save_as: str = "") -> dict:
        if variable not in SOLVABLE:
            raise ToolError(f"variable must be one of {sorted(SOLVABLE)}")
        if metric not in ("irr", "moic"):
            raise ToolError("metric must be 'irr' or 'moic'")
        base = self._scenario(scenario).assumptions
        lo, hi = SOLVABLE[variable]

        def value_at(x):
            try:
                a = self.apply_overrides(base, {variable: x})
            except ToolError:
                return None                      # outside the valid domain (e.g. equity <= 0)
            return run_model(a)["returns"][metric]

        grid = [lo + (hi - lo) * i / 200 for i in range(201)]
        points = [(x, value_at(x)) for x in grid]
        points = [(x, v) for x, v in points if v is not None]
        if not points:
            raise ToolError(f"No valid value of {variable} in [{lo}, {hi}] for this scenario.")
        bracket = next(((x0, x1) for (x0, v0), (x1, v1) in zip(points, points[1:])
                        if (v0 - target) * (v1 - target) <= 0), None)
        if bracket is None:
            vals = [v for _, v in points]
            raise ToolError(f"{metric} = {target:g} is not reachable by moving {variable} within [{lo}, {hi}]: "
                            f"{metric} ranges from {min(vals):.4g} to {max(vals):.4g}.")
        x0, x1 = bracket
        f0 = value_at(x0) - target
        for _ in range(100):
            mid = (x0 + x1) / 2
            v = value_at(mid)
            if v is None:
                break
            fm = v - target
            if (fm <= 0) == (f0 <= 0):
                x0, f0 = mid, fm
            else:
                x1 = mid
            if x1 - x0 < 1e-10:
                break
        solution = (x0 + x1) / 2
        crossings = sum(1 for (_, v0), (_, v1) in zip(points, points[1:]) if (v0 - target) * (v1 - target) < 0)
        result = {
            "variable": variable, "solution": round(solution, 4), "scenario": scenario,
            "metric": metric, "target": target,
            "check": self._summary(save_as or f"{scenario}+{variable}", self.apply_overrides(base, {variable: solution})),
        }
        if crossings > 1:
            result["note"] = f"{metric} crosses the target {crossings} times in the range; this is the lowest {variable}."
        if save_as:
            self.run_scenario(save_as, {variable: solution},
                              f"Solved {variable} for {metric} = {target:g} on '{scenario}'", based_on=scenario)
        return result

    def compare_scenarios(self) -> dict:
        if not self.scenarios:
            raise ToolError("No scenarios yet: call generate_base_case first.")
        keys = ("moic", "irr", "entry_equity", "exit_equity", "entry_total_leverage_x", "peak_rcf_draw",
                "min_interest_coverage")
        rows = []
        for s in self.scenarios.values():
            summary = self._summary(s.name, s.assumptions)
            rows.append({"scenario": s.name, "based_on": s.based_on, "overrides": s.overrides,
                         **{k: summary[k] for k in keys}, "warnings": summary["engine_warnings"]})
        return {"scenarios": rows}

    def export_excel(self, scenario: str, filename: str = "") -> dict:
        s = self._scenario(scenario)
        stem = re.sub(r"[^A-Za-z0-9_\-]+", "_", Path(filename).stem if filename else "")[:60].strip("_")
        if not stem:
            stem = f"{self.company_stem()}_{scenario}"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / f"{stem}.xlsx"     # always inside output_dir, always .xlsx
        export_to_excel(s.assumptions, str(path), self._audit_for(s))
        self.exports.append(str(path))
        return {"path": str(path), "scenario": scenario}

    def _audit_for(self, s: Scenario) -> Optional[dict]:
        if not self.generation:
            return None
        audit = self.generation.to_dict()
        audit["assumptions"] = dataclasses.asdict(s.assumptions)
        audit["provenance"] = self.provenance_text()
        if s.name != "base":
            base = self.scenarios["base"].assumptions
            for name, value in s.overrides.items():
                t = audit["trace"].setdefault(name, {"value": value, "source": "scenario", "rationale": "",
                                                      "llm_value": value, "notes": []})   # older projects
                t.update(source="scenario", rationale=s.rationale, value=value,
                         notes=t["notes"] + [f"scenario '{s.name}': base {fmt_number(getattr(base, name))} -> "
                                             f"{fmt_number(value)}"])
            audit["trace"]["entry_ebitda"]["value"] = s.assumptions.entry_ebitda
            audit["scenario"] = {"name": s.name, "based_on": s.based_on, "rationale": s.rationale,
                                 "overrides": s.overrides}
        return audit


# ---------------------------------------------------------------------------
# Claude tool wrappers
# ---------------------------------------------------------------------------

def make_tools(session: DealSession):
    from anthropic import beta_tool

    def as_json(result: dict) -> str:
        return json.dumps(result, ensure_ascii=False)

    @beta_tool
    def generate_base_case(description: str, regenerate: bool = False) -> str:
        """Build the base case: estimate every LBO assumption from the company description,
        apply sector guardrails, and run the LBO engine. Call this first, before any other tool.
        If a base case already exists (this session or a loaded deal file) it is returned
        unchanged with the list of existing scenarios; nothing is re-estimated.

        Args:
            description: The user's company description, verbatim, with every figure they gave.
            regenerate: Re-estimate from scratch, deleting all scenarios. Only when the user
                explicitly asks for a new estimate or describes a different company.
        """
        return as_json(session.generate_base_case(description, regenerate))

    @beta_tool
    def run_scenario(name: str, overrides: Dict[str, float], rationale: str, based_on: str = "base",
                     reprice_entry: bool = False, plan: Optional[Dict[str, List[float]]] = None) -> str:
        """Run the LBO engine on a variant of an existing scenario. Call this for downside / upside
        cases, a revised capital structure, or any 'what if' the user asks. Only the listed
        assumptions change; everything else is inherited from `based_on`. By default the deal
        stays as signed: operating changes (margin, growth, capex, NWC, exit) hit the projection
        while price and debt, which are multiples of LTM EBITDA, do not move.

        Args:
            name: Short unique id, e.g. "downside" or "lower_leverage".
            overrides: Assumption name -> new value. Percentages as decimals (0.04 = 4%),
                leverage and multiples in x, amounts in millions. Valid names: revenue_at_entry,
                ebitda_margin, entry_ev_multiple, revenue_growth, capex_pct_revenue, da_pct_revenue,
                nwc_pct_of_rev_growth, total_leverage_x, senior_leverage_x, senior_rate,
                senior_mandatory_amort_pct, sub_rate, cash_sweep_pct, rcf_commitment, rcf_rate,
                tax_rate, min_cash, hold_period_years, exit_ev_multiple, transaction_fees_pct_ev,
                financing_fees_pct_debt, senior_oid_pct, fee_amortization_years, max_net_leverage and
                min_interest_cover (covenants, 0 = none), entry_ebitda (LTM EBITDA,
                e.g. a quality-of-earnings adjustment). ebitda_margin is the projected margin.
                Changing total_leverage_x alone keeps the base senior/sub mix.
            rationale: One sentence on why this scenario matters; it is written into the Excel audit trail.
            based_on: Scenario to start from (default "base").
            reprice_entry: Pre-signing view: true when the new margin is already the company's current
                (LTM) margin, so price and debt are re-sized on it. Leave false for post-closing
                downsides and upsides.
            plan: Year-by-year profile, driver -> list of values for years 1, 2, 3... (later years repeat
                the last value). Drivers: revenue_growth, ebitda_margin, capex_pct_revenue, and
                max_net_leverage for covenant step-downs. Use it when
                the change builds up over time (a margin that erodes gradually, a recovery, expansion
                capex); a flat value in `overrides` replaces any profile of that driver.
        """
        return as_json(session.run_scenario(name, overrides, rationale, based_on, reprice_entry, plan))

    @beta_tool
    def solve_for_target(variable: str, metric: str, target: float, scenario: str = "base",
                         save_as: str = "") -> str:
        """Find the value of one assumption that hits a target IRR or MOIC, holding everything else
        fixed. Call this whenever the user asks for a threshold: maximum entry multiple / price for
        a target return, leverage needed, break-even exit multiple, minimum growth. Never estimate
        such thresholds yourself.

        Args:
            variable: One of entry_ev_multiple, exit_ev_multiple, total_leverage_x, revenue_growth,
                ebitda_margin (projected margin, price and debt as signed), senior_rate. Solving on
                revenue_growth or ebitda_margin uses one flat value for every year.
            metric: "irr" or "moic".
            target: Target value, IRR as a decimal (0.20 = 20%), MOIC in x (2.5).
            scenario: Scenario to solve on (default "base").
            save_as: If set, also stores the solved case as a new scenario with this name.
        """
        return as_json(session.solve_for_target(variable, metric, target, scenario, save_as))

    @beta_tool
    def compare_scenarios() -> str:
        """Side-by-side returns and credit metrics for every scenario built so far. Call this before
        writing the final answer when more than one scenario exists."""
        return as_json(session.compare_scenarios())

    @beta_tool
    def export_excel(scenario: str = "base", filename: str = "") -> str:
        """Write the live-formula Excel model (Assumptions with sources and rationale, full LBO,
        IRR sensitivity, audit trail) for one scenario. Always export the base case before
        answering; export other scenarios when the user asks for them.

        Args:
            scenario: Scenario to export (default "base").
            filename: Optional file name without folder; defaults to <company>_<scenario>.xlsx.
        """
        return as_json(session.export_excel(scenario, filename))

    return [generate_base_case, run_scenario, solve_for_target, compare_scenarios, export_excel]


SYSTEM_PROMPT = """You are an LBO analyst at a mid-market private equity fund. The user describes a \
company and possibly asks questions about a buyout of it. You have tools that estimate assumptions, \
run a deterministic LBO engine, solve for thresholds and export a live Excel model.

Ground rules:
- Every number you state must come from a tool result. Do not compute returns, prices or \
thresholds yourself. If you need a number you do not have, call a tool.
- The same goes for claims about what would or would not work ("more leverage cannot close the \
gap", "the price would need to fall to..."): test them with solve_for_target or run_scenario first.
- Stay within what the model covers. It models transaction fees and OID, a year-by-year operating \
plan and maintenance covenants (max net leverage with step-downs, min interest cover; the tools \
report the first breach year and the lowest EBITDA headroom). It has no refinancing, no waiver or \
default mechanics, no dividends or recaps and no management equity: do not assert what happens \
after a breach beyond what the tools report. When covenants were set from the base case (no user \
terms), say they assume ~30% headroom, as lenders typically set them.
- In a downside, always report whether and when the covenants would be breached and the lowest \
headroom: that, more than the IRR, tells whether the structure survives.
- Start with generate_base_case, passing the user's description verbatim. If it returns an \
existing base case (e.g. loaded from a saved deal file), work from it and its existing scenarios \
as they are: they are the agreed starting point.
- Review the base case like an investment committee would. If the engine or the guardrails flag \
a problem (RCF drawn beyond its commitment, interest coverage below 2x, thin equity), do not \
bury it: run a revised structure as a separate scenario and say which one you would underwrite.
- Unless the user asks for something narrower, run a downside scenario that reflects the \
company's specific risks (not a generic haircut), and an upside only if it adds information. \
State the rationale of every scenario in one sentence.
- Use a year-by-year `plan` when a change realistically builds up over time (margins eroding over \
two or three years, a recovery, a ramp-up of new sites, front-loaded expansion capex); a flat \
override is fine for a simple sensitivity.
- Keep the scenario set small and readable: at most 4-5 scenarios in total (base included) unless \
the user asks for more. In follow-up questions, answer with one new scenario (or by reusing an \
existing one) instead of re-running variants of every earlier case.
- A downside is post-closing by default: the sponsor has already paid the price and raised \
the debt, and the operating plan disappoints afterwards. run_scenario keeps entry terms as \
signed; do not tweak entry multiple or leverage to hold price and debt constant. Use \
reprice_entry only to answer 'what if the business is already weaker today' (pre-signing).
- Answer the user's explicit questions with solve_for_target or run_scenario.
- Export the base case to Excel before answering, and any other scenario the user asks for.
- Final answer in the language the user wrote the request in (an English request about a German \
company gets an English answer): a short deal snapshot, a returns table (MOIC, IRR, \
leverage, key flags) per scenario, the answers to the user's questions, what the guardrails \
changed and why, the main risks, and the Excel file path(s). Be concise; no preamble."""


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

class LBOAgent:
    """Multi-turn wrapper around the SDK tool runner. Keeps the full message history
    (thinking and tool blocks included) so follow-up questions reuse the same session."""

    def __init__(self, session: DealSession, *, model: str = DEFAULT_MODEL, client=None,
                 on_tool_call: Optional[Callable[[str, dict], None]] = None):
        import anthropic

        self.session = session
        self.model = model
        self.client = client or anthropic.Anthropic()
        self.tools = make_tools(session)
        self.messages: List[dict] = []
        self.on_tool_call = on_tool_call

    def ask(self, user_text: str, display_question: Optional[str] = None) -> str:
        """Run one user turn. `display_question` is what gets stored in the project history
        (the app sends extra instructions the user did not type)."""
        self.messages.append({"role": "user", "content": user_text})
        extra = {}
        if self.model in FALLBACK_MODELS:
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}

        runner = self.client.beta.messages.tool_runner(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=self.tools,
            messages=list(self.messages),
            output_config={"effort": "high"},
            cache_control={"type": "ephemeral"},   # system + tools + history are re-sent every round
            max_iterations=MAX_TOOL_ROUNDS,
            **extra,
        )
        last = None
        for message in runner:
            last = message
            self.session.record_usage(message.model, message.usage)
            # Mirror history: the runner keeps its own copy and does not expose it.
            self.messages.append({"role": "assistant", "content": message.content})
            if self.on_tool_call:
                for block in message.content:
                    if block.type == "tool_use":
                        self.on_tool_call(block.name, block.input)
            tool_response = runner.generate_tool_call_response()  # cached: tools run once
            if tool_response is not None:
                self.messages.append(tool_response)

        if last is None:
            raise RuntimeError("No response from the model")
        if last.stop_reason == "refusal":
            raise RuntimeError(f"Claude declined the request: {last.stop_details}")
        if last.stop_reason == "max_tokens":
            raise RuntimeError("Response cut off at max_tokens")
        if last.stop_reason == "tool_use":
            raise RuntimeError(f"Stopped after {MAX_TOOL_ROUNDS} tool rounds without a final answer")
        answer = "".join(b.text for b in last.content if b.type == "text")
        self.session.history.append({"question": display_question or user_text, "answer": answer,
                                     "at": dt.datetime.now().isoformat(timespec="seconds")})
        return answer


def _print_tool_call(name: str, args: dict):
    shown = {k: v for k, v in args.items() if k != "description"}
    print(f"  -> {name}({json.dumps(shown, ensure_ascii=False)[:160]})", file=sys.stderr, flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="LBO agent: description + questions in, analysis + Excel out.")
    ap.add_argument("request", nargs="?", help="Company description and questions (or --file / stdin)")
    ap.add_argument("--file", help="Read the request from a text file")
    ap.add_argument("--chat", action="store_true", help="Keep the session open for follow-up questions")
    ap.add_argument("--load", metavar="DEAL_JSON",
                    help="Start from a saved deal file (or a generator --json file): no re-estimation")
    ap.add_argument("--save", metavar="DEAL_JSON",
                    help="Where to save the deal file (default: the --load file, else output/<company>_deal.json)")
    ap.add_argument("--out-dir", default="output")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args(argv)

    request = None
    if args.file:
        request = Path(args.file).read_text(encoding="utf-8")
    elif args.request:
        request = args.request
    elif not sys.stdin.isatty():
        request = sys.stdin.read()
    elif not (args.load and args.chat):
        ap.error("provide a request, --file, or pipe text on stdin (or --load with --chat)")

    import anthropic

    if args.load:
        try:
            session = DealSession.load(args.load, args.out_dir, model=args.model)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            sys.exit(f"Cannot load {args.load}: {exc}")
        print(f"Loaded {args.load}: {session.provenance_text()}; scenarios: {', '.join(session.scenarios)}",
              file=sys.stderr)
    else:
        session = DealSession(args.out_dir, model=args.model)
    save_path = args.save or args.load

    def ask_and_save(text):
        print(agent.ask(text))
        if session.generation:
            print(f"\nDeal saved: {session.save(save_path)}", file=sys.stderr)

    agent = LBOAgent(session, model=args.model, on_tool_call=_print_tool_call)
    try:
        if request:
            ask_and_save(request)
        while args.chat:
            try:
                follow_up = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if follow_up.lower() in ("", "exit", "quit", "esci"):
                break
            ask_and_save(follow_up)
    except anthropic.AuthenticationError:
        sys.exit("API key rejected (401). Check ANTHROPIC_API_KEY in this terminal: "
                 "echo ${#ANTHROPIC_API_KEY} should print ~100+, not 10 (the 'sk-ant-...' placeholder).")
    except anthropic.PermissionDeniedError as exc:
        sys.exit(f"API key lacks permission for this request (403): {exc.message}")
    except anthropic.BadRequestError as exc:
        sys.exit(f"Request rejected (400): {exc.message}\n"
                 "If it mentions credit or billing, add credit at console.anthropic.com -> Billing.")
    except anthropic.RateLimitError:
        sys.exit("Rate limited (429) after retries; wait a minute and run again.")
    if agent.session.exports:
        print("\nExcel: " + ", ".join(agent.session.exports), file=sys.stderr)
    cost = usage_cost_usd(session.usage_log)
    if session.usage_log:
        print(f"API usage: {len(session.usage_log)} calls, "
              + (f"~${cost:.2f} at list prices" if cost is not None else "cost unknown for this model"),
              file=sys.stderr)


if __name__ == "__main__":
    main()
