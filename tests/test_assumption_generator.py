from types import SimpleNamespace

import pytest

from assumption_generator import (
    ProposedAssumptions, apply_guardrails, generate_assumptions, propose_with_claude,
)
from lbo_engine import run_model
from sector_benchmarks import SECTORS

# A clean, in-range proposal for an Italian industrial — what a good LLM answer looks like.
BASE = dict(
    revenue_at_entry=400.0, entry_ebitda=60.0, ebitda_margin=0.15, entry_ev_multiple=8.5,
    revenue_growth=0.04, capex_pct_revenue=0.035, da_pct_revenue=0.03, nwc_pct_of_rev_growth=0.15,
    total_leverage_x=4.75, senior_leverage_x=3.75, senior_rate=0.065, senior_mandatory_amort_pct=0.05,
    sub_rate=0.10, cash_sweep_pct=1.0, rcf_commitment=40.0, rcf_rate=0.06, tax_rate=0.28,
    min_cash=8.0, hold_period_years=5, exit_ev_multiple=8.5,
    transaction_fees_pct_ev=0.02, financing_fees_pct_debt=0.025, senior_oid_pct=0.005, fee_amortization_years=6,
)


FLAT_PLAN = {"revenue_growth_by_year": [], "ebitda_margin_by_year": [], "capex_pct_revenue_by_year": [],
             "source": "estimated", "rationale": "no reason for a profile: flat"}


def proposal(provided=(), sector="industrials", plan=None, **overrides) -> ProposedAssumptions:
    vals = {**BASE, **overrides}
    return ProposedAssumptions(
        company_name="Project Test", sector=sector, sector_rationale="test", currency="EUR",
        key_risks=["cyclicality"], operating_plan={**FLAT_PLAN, **(plan or {})},
        **{k: {"value": v, "source": "provided" if k in provided else "estimated", "rationale": "r"}
           for k, v in vals.items()},
    )


def test_clean_proposal_passes_untouched():
    res = apply_guardrails(proposal())
    assert res.adjustments == []
    assert res.warnings == []
    assert res.assumptions.entry_ebitda == pytest.approx(60.0)
    assert res.trace["entry_ebitda"].source == "derived"
    run_model(res.assumptions)  # engine accepts it


def test_schema_exposes_every_sector():
    schema = ProposedAssumptions.model_json_schema()
    assert set(schema["properties"]["sector"]["enum"]) == set(SECTORS)


def test_estimated_value_outside_sector_range_is_clamped():
    res = apply_guardrails(proposal(ebitda_margin=0.35, entry_ev_multiple=13.0))
    a = res.assumptions
    assert a.ebitda_margin == pytest.approx(0.20)          # industrials cap
    assert a.entry_ev_multiple == pytest.approx(10.0)
    assert a.entry_ebitda == pytest.approx(400 * 0.20)     # re-derived from clamped margin
    assert res.trace["ebitda_margin"].source == "adjusted"
    assert res.trace["ebitda_margin"].llm_value == 0.35


def test_provided_value_outside_range_is_kept_and_flagged():
    res = apply_guardrails(proposal(provided={"revenue_growth"}, revenue_growth=0.15))
    assert res.assumptions.revenue_growth == pytest.approx(0.15)
    assert any("revenue_growth" in w and "provided" in w for w in res.warnings)


def test_whole_number_percentages_are_converted():
    res = apply_guardrails(proposal(revenue_growth=4.0, tax_rate=28.0))
    assert res.assumptions.revenue_growth == pytest.approx(0.04)
    assert res.assumptions.tax_rate == pytest.approx(0.28)


def test_provided_revenue_and_ebitda_imply_margin():
    res = apply_guardrails(proposal(provided={"revenue_at_entry", "entry_ebitda"},
                                    revenue_at_entry=500.0, entry_ebitda=90.0, ebitda_margin=0.15))
    assert res.assumptions.ebitda_margin == pytest.approx(0.18)
    assert res.trace["ebitda_margin"].source == "derived"


def test_provided_ebitda_only_implies_revenue():
    res = apply_guardrails(proposal(provided={"entry_ebitda"}, entry_ebitda=75.0))
    assert res.assumptions.revenue_at_entry == pytest.approx(75.0 / 0.15)
    assert res.trace["revenue_at_entry"].source == "derived"


def test_inconsistent_llm_ebitda_is_rederived():
    res = apply_guardrails(proposal(entry_ebitda=70.0))  # 400 x 15% = 60, not 70
    assert res.assumptions.entry_ebitda == pytest.approx(60.0)


def test_no_multiple_expansion_in_base_case():
    res = apply_guardrails(proposal(exit_ev_multiple=9.5))
    assert res.assumptions.exit_ev_multiple == pytest.approx(8.5)


def test_thin_equity_cuts_leverage_and_keeps_mix():
    # 7x EV with 5.5x debt -> equity ~21% of uses; must come down to <= 70% debt
    res = apply_guardrails(proposal(entry_ev_multiple=7.0, exit_ev_multiple=7.0,
                                    total_leverage_x=5.5, senior_leverage_x=4.4))
    a = res.assumptions
    uses = a.entry_ebitda * a.entry_ev_multiple + a.min_cash
    assert a.entry_ebitda * a.total_leverage_x <= 0.70 * uses + 1e-9
    assert a.senior_leverage_x / a.total_leverage_x == pytest.approx(0.8)


def test_senior_above_total_is_capped_even_if_provided():
    res = apply_guardrails(proposal(provided={"senior_leverage_x"}, senior_leverage_x=5.5))
    assert res.assumptions.senior_leverage_x == pytest.approx(res.assumptions.total_leverage_x)
    assert res.warnings


def test_sub_debt_priced_above_senior():
    res = apply_guardrails(proposal(senior_rate=0.085, sub_rate=0.09))
    assert res.assumptions.sub_rate == pytest.approx(0.10)


def test_hold_period_rounded_and_bounded():
    assert apply_guardrails(proposal(hold_period_years=4.6)).assumptions.hold_period_years == 5
    assert apply_guardrails(proposal(hold_period_years=12)).assumptions.hold_period_years == 7


def test_generate_with_injected_llm_runs_end_to_end():
    res = generate_assumptions("any text", llm=lambda d: proposal(sector="software_saas",
                                                                  ebitda_margin=0.30, entry_ev_multiple=14.0,
                                                                  revenue_growth=0.12, nwc_pct_of_rev_growth=-0.05,
                                                                  total_leverage_x=6.0, senior_leverage_x=6.0,
                                                                  exit_ev_multiple=14.0))
    out = run_model(res.assumptions)
    assert out["returns"]["moic"] > 0
    d = res.to_dict()
    assert d["sector_label"] == "Software / SaaS"
    assert set(d["trace"]) >= {"entry_ebitda", "exit_ev_multiple"}


def test_propose_with_claude_request_shape():
    """Wiring check without network: the SDK call gets the right model, schema, and fallback."""
    calls = {}
    expected = proposal()

    def fake_parse(**kwargs):
        calls.update(kwargs)
        return SimpleNamespace(stop_reason="end_turn", stop_details=None, parsed_output=expected)

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(parse=fake_parse)))
    got = propose_with_claude("Azienda italiana di packaging", model="claude-opus-5", client=client)

    assert got is expected
    assert calls["model"] == "claude-opus-5"
    assert calls["output_format"] is ProposedAssumptions
    assert calls["fallbacks"] == "default"
    assert "Azienda italiana di packaging" in calls["messages"][0]["content"]
    assert "industrials" in calls["system"]


def test_propose_with_claude_raises_on_refusal():
    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(
        parse=lambda **k: SimpleNamespace(stop_reason="refusal", stop_details="x", parsed_output=None))))
    with pytest.raises(RuntimeError, match="declined"):
        propose_with_claude("x", client=client)


def test_implied_margin_outside_range_is_flagged():
    """Found by the agent eval (case 08): revenue 200 + EBITDA 60 given -> 30% margin for a
    logistics company must raise a warning even though both figures are the user's."""
    res = apply_guardrails(proposal(provided={"revenue_at_entry", "entry_ebitda"}, sector="logistics_transport",
                                    revenue_at_entry=200.0, entry_ebitda=60.0, ebitda_margin=0.12,
                                    capex_pct_revenue=0.05, da_pct_revenue=0.045, nwc_pct_of_rev_growth=0.10,
                                    entry_ev_multiple=8.0, exit_ev_multiple=8.0, total_leverage_x=4.5,
                                    senior_leverage_x=3.5))
    assert res.assumptions.ebitda_margin == pytest.approx(0.30)       # kept
    assert any("ebitda_margin" in w and "provided" in w for w in res.warnings)


def test_gym_chain_margin_is_not_clamped_as_retail():
    """Found by using the app: a premium gym chain (18% margin) was classified as retail and
    its margin cut to retail levels. Leisure & fitness now has its own sector."""
    res = apply_guardrails(proposal(sector="leisure_fitness", ebitda_margin=0.18, entry_ebitda=72.0, entry_ev_multiple=10.0,
                                    exit_ev_multiple=10.0, capex_pct_revenue=0.08, da_pct_revenue=0.07,
                                    nwc_pct_of_rev_growth=-0.05))
    assert res.assumptions.ebitda_margin == pytest.approx(0.18) and res.adjustments == []
    assert "leisure_fitness" in ProposedAssumptions.model_json_schema()["properties"]["sector"]["enum"]


def test_transaction_costs_are_guarded():
    res = apply_guardrails(proposal(transaction_fees_pct_ev=0.08, financing_fees_pct_debt=2.5,
                                    senior_oid_pct=0.005, fee_amortization_years=6.4))
    a = res.assumptions
    assert a.transaction_fees_pct_ev == pytest.approx(0.03)          # clamped to market range
    assert a.financing_fees_pct_debt == pytest.approx(0.025)         # "2.5" meant 2.5%
    assert a.fee_amortization_years == 6 and isinstance(a.fee_amortization_years, int)


def test_year_by_year_plan_is_guarded():
    res = apply_guardrails(proposal(plan={"ebitda_margin_by_year": [0.15, 0.17, 0.30],
                                          "revenue_growth_by_year": [3, 5, 6], "source": "estimated",
                                          "rationale": "margin programme"}))
    a = res.assumptions
    assert a.revenue_growth_by_year == (0.03, 0.05, 0.06)                  # whole numbers read as %
    assert a.ebitda_margin_by_year == (0.15, 0.17, 0.20)                   # year 3 clamped to industrials cap
    assert a.capex_pct_revenue_by_year is None                             # empty list = flat
    assert res.trace["ebitda_margin_by_year"].source == "adjusted"
    assert any("year 3" in adj for adj in res.adjustments)


def test_provided_plan_is_flagged_not_changed():
    res = apply_guardrails(proposal(plan={"revenue_growth_by_year": [0.05, 0.15], "source": "provided",
                                          "rationale": "management plan"}))
    assert res.assumptions.revenue_growth_by_year == (0.05, 0.15)
    assert any("revenue_growth_by_year year 2" in w for w in res.warnings)


def test_flat_plan_changes_nothing():
    res = apply_guardrails(proposal())
    assert not res.assumptions.has_plan and "revenue_growth_by_year" not in res.trace
