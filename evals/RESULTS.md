# Agent evaluation results

11 cases in `agent_cases.json`, run against the real Claude API (claude-opus-5) with the programmatic
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

## What the checks found

- `08_logistics_inconsistent` failed: a 30% margin implied by user-given revenue and EBITDA (logistics range 6-14%) was not flagged by the guardrails. Fixed in `assumption_generator.py`, passes in v1.
- `07_de_it_services` passed every check but answered in German to an English request about a German company. Added a language check and made the instruction explicit; passes in v1.
- Grounding below 100% comes from simple arithmetic the model did on tool outputs (e.g. 4.5x - 3.25x sub-debt leverage), not from invented figures.

With 11 cases run once, the pass rate has a wide confidence interval (Wilson 95%: ~62-98% for 10/11):
the suite is for catching wrong behaviour, not for measuring small improvements.
