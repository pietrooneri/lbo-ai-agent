# Agent evaluation results

Cases in `agent_cases.json`, run against the real Claude API (claude-opus-5) with the programmatic
checks in `run_agent_eval.py`: sector, user figures kept, expected tool calls, scenarios, year-by-year
plan, covenants, guardrail flags, Excel export, answer language and **grounding** (share of the
numbers in the answer that trace back to tool outputs).

## Latest run: v2 (26 September 2026), after the bank-grade model and the Capital IQ calibration

**15/15 cases pass every check** (Wilson 95% CI 80-100%), mean grounding 98%, $4.77 for 15 cases.

| Case | Tags | All checks | Grounding | Cost | API calls | Tool calls |
|---|---|---|---|---|---|---|
| 01_valvole | industrials, it, reference | pass | 100% | $0.34 | 8 | 10 |
| 02_saas | software_saas, it, figures_given | pass | 98% | $0.26 | 6 | 7 |
| 03_uk_retail | consumer_retail, en, figures_given | pass | 99% | $0.28 | 7 | 8 |
| 04_qoe_reprice | industrials, it, reprice | pass | 99% | $0.50 | 9 | 11 |
| 05_chem_high_leverage | chemicals, it, guardrail | pass | 99% | $0.35 | 8 | 9 |
| 06_packaging_followup | industrials, it, multi_turn | pass | 97% | $0.37 | 9 | 10 |
| 07_de_it_services | business_services, en, three_cases | pass | 99% | $0.28 | 6 | 10 |
| 08_logistics_inconsistent | logistics_transport, it, guardrail | pass | 99% | $0.29 | 6 | 7 |
| 09_dental | healthcare_services, it, price_given | pass | 96% | $0.29 | 6 | 7 |
| 10_pasta | food_beverage, it, hold_given | pass | 97% | $0.30 | 7 | 10 |
| 11_small_bakery | food_beverage, it, edge_small | pass | 100% | $0.32 | 7 | 9 |
| 12_fees_given | industrials, it, fees | pass | 93% | $0.27 | 5 | 7 |
| 13_covenants_given | healthcare_services, it, covenants | pass | 99% | $0.33 | 7 | 9 |
| 14_margin_programme | industrials, it, operating_plan | pass | 99% | $0.33 | 6 | 10 |
| 15_circularity | business_services, en, circularity | pass | 100% | $0.24 | 6 | 7 |

What this run showed:

- **03_uk_retail first failed on the checker, not the agent.** Asked "what if margins compress by
  200bps after we buy it", the agent phased the squeeze in over three years (9% to 7%) instead of a
  flat 7%, and flagged a year-4 net leverage covenant breach. The check only accepted a flat margin;
  it now accepts a plan that settles at the expected level (with a test). The case was re-run
  ($0.28) and passes, with the same conclusion (-9.0% vs -9.8% IRR in the downside).
- **New bank-model cases:** stated fees and OID are kept and their IRR cost explained (12);
  stated covenants with step-downs are captured and a breach reported (13); a margin programme is
  modelled year by year (14); average-balance interest is switched on and its +15 bps IRR effect
  explained correctly: debt falls during the year, so the average balance is lower (15).
- **Capital IQ margin floors** (see `data/capiq/README.md`): estimated margins moved by about one
  point where the margin was not given (valves 15% to 14%, dental 18% to 17%, pasta 15% to 14%).
- **Grounding below 100%** is again arithmetic on tool outputs (an IRR difference in bps, a price
  gap in turns of EBITDA). In 12_fees_given the model split the fee drag into -112 bps and -72 bps;
  they add up to the 184 bps drop the tools report (13.07% to 11.23%).
- A manual test in the desktop app (premium gym chain, the case that had been misclassified as
  retail) now lands in leisure/fitness with an unclamped 20% margin, a 9.1x maximum price for a 20%
  IRR against an 11x ask, and a year-2 covenant breach in the downside ($0.33).

## Earlier run: baseline and v1 (23 September 2026)

11 cases (01-11), run against the real Claude API (claude-opus-5) with the programmatic
checks in `run_agent_eval.py`. Baseline = first run; v1 = the two failing cases re-run after the fixes.

| Case | Tags | All checks | Grounding | Cost | API calls | Tool calls |
|---|---|---|---|---|---|---|
| 01_valvole | industrials, it, reference | pass | 98% | $0.25 | 7 | 10 |
| 02_saas | software_saas, it, figures_given | pass | 99% | $0.23 | 6 | 8 |
| 03_uk_retail | consumer_retail, en, figures_given | pass | 99% | $0.29 | 8 | 14 |
| 04_qoe_reprice | industrials, it, reprice | pass | 99% | $0.33 | 10 | 11 |
| 05_chem_high_leverage | chemicals, it, guardrail | pass | 96% | $0.29 | 8 | 10 |
| 06_packaging_followup | industrials, it, multi_turn | pass | 97% | $0.34 | 10 | 13 |
| 07_de_it_services | business_services, en, three_cases | pass | 98% | $0.30 | 8 | 13 |
| 08_logistics_inconsistent | logistics_transport, it, guardrail | FAIL | 99% | $0.26 | 5 | 7 |
| 09_dental | healthcare_services, it, price_given | pass | 99% | $0.26 | 7 | 11 |
| 10_pasta | food_beverage, it, hold_given | pass | 100% | $0.25 | 7 | 11 |
| 11_small_bakery | food_beverage, it, edge_small | pass | 100% | $0.31 | 9 | 12 |
| 07_de_it_services (v1) | business_services, en, three_cases | pass | 100% | $0.26 | 8 | 12 |
| 08_logistics_inconsistent (v1) | logistics_transport, it, guardrail | pass | 94% | $0.23 | 5 | 7 |

Total API cost of all runs: **$3.61**.

### What the checks found

- `08_logistics_inconsistent` failed: a 30% margin implied by user-given revenue and EBITDA (logistics range 6-14%) was not flagged by the guardrails. Fixed in `assumption_generator.py`, passes in v1.
- `07_de_it_services` passed every check but answered in German to an English request about a German company. Added a language check and made the instruction explicit; passes in v1.
- Grounding below 100% comes from simple arithmetic the model did on tool outputs (e.g. 4.5x - 3.25x sub-debt leverage), not from invented figures.

With 11 cases run once, the pass rate has a wide confidence interval (Wilson 95%: ~62-98% for 10/11):
the suite is for catching wrong behaviour, not for measuring small improvements.
