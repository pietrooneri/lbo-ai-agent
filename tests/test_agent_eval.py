"""Offline checks of the eval harness: an oracle must pass, a null answer must fail,
failures must land in errors.jsonl (never as a zero score), and the budget must hold."""

import argparse
import json

import pytest

import run_agent_eval as ev
from test_assumption_generator import proposal
from test_lbo_agent import ScriptedClaude, _message, _tool_use

CASE = {
    "id": "t_valves", "tags": ["industrials", "it"],
    "turns": ["Valvole, fatturato 400 milioni. Multiplo massimo per IRR 20%? Downside?"],
    "expect": {
        "sector": ["industrials"],
        "provided": {"revenue_at_entry": 400},
        "tool_calls": [{"tool": "solve_for_target", "args": {"variable": "entry_ev_multiple", "metric": "irr",
                                                              "target": 0.20}}],
        "post_close_downside": True,
    },
}


def fake_llm(description):
    return proposal(provided={"revenue_at_entry"})


def oracle_script(final_text="Analisi completata sul fatturato di 400 milioni."):
    return ScriptedClaude([
        _message([_tool_use("t1", "generate_base_case", {"description": CASE["turns"][0]})], "tool_use"),
        _message([
            _tool_use("t2", "solve_for_target", {"variable": "entry_ev_multiple", "metric": "irr", "target": 0.2}),
            _tool_use("t3", "run_scenario", {"name": "downside", "rationale": "ciclo",
                                             "overrides": {"ebitda_margin": 0.12, "revenue_growth": -0.02}}),
        ], "tool_use"),
        _message([_tool_use("t4", "export_excel", {"scenario": "base"})], "tool_use"),
        _message([{"type": "text", "text": final_text}], "end_turn"),
    ])


def _args(tmp_path, **kw):
    base = dict(cases=str(tmp_path / "cases.json"), variant="baseline", flow_dir=str(tmp_path / "flow"),
                model="claude-opus-5", reps=1, only=None, limit=None, budget_usd=None,
                first_case_estimate=1.0, timeout_s=60)
    base.update(kw)
    (tmp_path / "cases.json").write_text(json.dumps([CASE]))
    return argparse.Namespace(**base)


def _files(tmp_path):
    d = tmp_path / "flow" / "baseline"
    return ev._jsonl(d / "results.jsonl"), ev._jsonl(d / "errors.jsonl"), d


def test_oracle_passes_every_check(tmp_path):
    rows = ev.run(_args(tmp_path), client=oracle_script().client(), llm=fake_llm)
    g = rows[0]["grade"]
    assert g["all_checks"] == 1.0, rows[0]["explanation"]
    assert g["grounding"] == 1.0
    assert (g["guardrail_ok"], rows[0]["meta"]["api_calls"]) == (None, 4)
    assert rows[0]["meta"]["cost_usd"] > 0 and rows[0]["usage"]["input_tokens"] == 40
    results, errors, d = _files(tmp_path)
    assert len(results) == 1 and not errors
    trace = json.loads((d / "traces" / "t_valves_rep0.json").read_text())
    assert [t["role"] for t in trace][:4] == ["system", "user", "tool_call", "tool_result"]
    assert "base_case_results" in trace[3]["content"]


def test_null_answer_fails(tmp_path):
    script = ScriptedClaude([_message([{"type": "text", "text": "Non posso aiutarti."}], "end_turn")])
    g = ev.run(_args(tmp_path), client=script.client(), llm=fake_llm)[0]["grade"]
    assert g["all_checks"] == 0.0
    assert (g["sector_ok"], g["provided_ok"], g["tools_ok"], g["export_ok"]) == (0.0, 0.0, 0.0, 0.0)


def test_invented_number_is_caught(tmp_path):
    rows = ev.run(_args(tmp_path), client=oracle_script("L'EBITDA scende a circa 37,7 milioni.").client(),
                  llm=fake_llm)
    assert rows[0]["grade"]["grounding"] == 0.0
    assert "37,7" in rows[0]["explanation"]["grounding"]


def test_skipping_the_downside_fails_the_scenario_check(tmp_path):
    script = ScriptedClaude([
        _message([_tool_use("t1", "generate_base_case", {"description": "x"})], "tool_use"),
        _message([_tool_use("t2", "run_scenario", {"name": "cheaper", "rationale": "x", "reprice_entry": True,
                                                   "overrides": {"ebitda_margin": 0.12}})], "tool_use"),
        _message([{"type": "text", "text": "ok"}], "end_turn"),
    ])
    row = ev.run(_args(tmp_path), client=script.client(), llm=fake_llm)[0]
    assert row["grade"]["scenarios_ok"] == 0.0 and "FAIL post-closing" in row["explanation"]["scenarios_ok"]


def test_api_failure_goes_to_errors_not_scores(tmp_path):
    script = ScriptedClaude([{**_message([], "refusal"), "stop_details": {"type": "refusal", "category": None,
                                                                          "explanation": None}}])
    assert ev.run(_args(tmp_path), client=script.client(), llm=fake_llm) == []
    results, errors, _ = _files(tmp_path)
    assert results == [] and errors[0]["failure_class"] == "RuntimeError"


def test_served_model_mismatch_is_an_error(tmp_path):
    ev.run(_args(tmp_path, model="claude-sonnet-5"), client=oracle_script().client(), llm=fake_llm)
    results, errors, _ = _files(tmp_path)
    assert results == [] and errors[0]["failure_class"] == "model_mismatch"


def test_budget_stops_before_spending(tmp_path):
    assert ev.run(_args(tmp_path, budget_usd=0.5), client=oracle_script().client(), llm=fake_llm) == []
    assert _files(tmp_path)[:2] == ([], [])


def test_resume_skips_finished_cases(tmp_path):
    ev.run(_args(tmp_path), client=oracle_script().client(), llm=fake_llm)
    assert ev.run(_args(tmp_path), client=ScriptedClaude([]).client(), llm=fake_llm) == []
    assert "1/1" in ev.summary(tmp_path / "flow" / "baseline")


def test_harness_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "FLOW_DIR", tmp_path)
    with pytest.raises(SystemExit, match="approve-harness"):
        ev.check_harness(approve=False)
    ev.check_harness(approve=True)
    ev.check_harness(approve=False)          # approved sha matches: no exit
    monkeypatch.setattr(ev, "harness_sha", lambda: "changed")
    with pytest.raises(SystemExit):
        ev.check_harness(approve=False)


@pytest.mark.parametrize("answer, refs, score", [
    ("MOIC 1,63x, IRR 10,3%", [1.629, 0.10254], 1.0),          # rounding as displayed
    ("IRR −26,4% su EV €358,4m", [-0.2641, 358.4], 1.0),       # unicode minus, currency
    ("Anno 2026, 5 anni, Y1→Y5, 3 scenari", [], 1.0),          # ignored: years, small counts
    ("EBITDA ~€40m", [32.9, 30.4], 0.0),                        # invented
])
def test_grounding_rules(answer, refs, score):
    assert ev.grounding(answer, refs)["score"] == score


def test_wilson_interval_is_honest_at_small_n():
    lo, hi = ev.wilson_interval(2, 2)
    assert hi == 1.0 and lo < 0.4          # 2/2 says little
    lo, hi = ev.wilson_interval(9, 10)
    assert 0.5 < lo < 0.65 and hi > 0.97


@pytest.mark.parametrize("text, lang", [
    ("Il caso base non è investibile: la leva è alta e il prezzo è per il 20% sopra", "it"),
    ("The base case is not investable at this price and the leverage is too high", "en"),
    ("Der Basisfall ist bei diesem Preis nicht investierbar und die Verschuldung ist hoch", "de"),
])
def test_language_detection(text, lang):
    assert ev.detect_language(text) == lang


def test_answer_in_wrong_language_fails(tmp_path):
    rows = ev.run(_args(tmp_path), client=oracle_script("Der Basisfall ist nicht investierbar und die "
                                                        "Verschuldung ist zu hoch.").client(), llm=fake_llm)
    assert rows[0]["grade"]["language_ok"] == 0.0 and rows[0]["grade"]["all_checks"] == 0.0


def test_code_written_warning_text_counts_as_source():
    refs = ev._numbers_in({"guardrail_warnings": ["Entry EBITDA / interest only 1.83x"],
                           "rationale": "margin around 37.7%"}, strings=False)
    assert 1.83 in refs and 37.7 not in refs


PLAN_CASE = {
    "id": "t_plan", "tags": ["industrials", "it"],
    "turns": ["Arredi, fatturato 400 milioni, margine 15%, piano al 18% in tre anni; e se arriva al 16%?"],
    "expect": {"sector": ["industrials"], "provided": {},
               "plan_shape": {"field": "ebitda_margin_by_year", "increasing": True},
               "scenario_plan": [{"field": "ebitda_margin_by_year", "last": 0.16}],
               "covenant_steps": [6.0, 5.5, 5.0]},
}


def plan_llm(description):
    return proposal(plan={"ebitda_margin_by_year": [0.15, 0.165, 0.18], "source": "provided",
                          "rationale": "management plan"},
                    covenants={"max_net_leverage": 6.0, "min_interest_cover": 2.5, "step_down_per_year": 0.5,
                               "source": "provided", "rationale": "term sheet"})


def plan_script(scenario_plan):
    return ScriptedClaude([
        _message([_tool_use("t1", "generate_base_case", {"description": "x"})], "tool_use"),
        _message([_tool_use("t2", "run_scenario", {"name": "partial", "rationale": "plan half delivered",
                                                   "overrides": {}, "plan": scenario_plan})], "tool_use"),
        _message([_tool_use("t3", "export_excel", {"scenario": "base"})], "tool_use"),
        _message([{"type": "text", "text": "Il piano regge solo in parte, con il fatturato di 400 milioni."}], "end_turn"),
    ])


def test_plan_and_covenant_checks_pass_for_a_correct_agent(tmp_path):
    args = _args(tmp_path)
    (tmp_path / "cases.json").write_text(json.dumps([PLAN_CASE]))
    rows = ev.run(args, client=plan_script({"ebitda_margin": [0.15, 0.155, 0.16]}).client(), llm=plan_llm)
    g = rows[0]["grade"]
    assert (g["plan_ok"], g["covenant_ok"], g["all_checks"]) == (1.0, 1.0, 1.0), rows[0]["explanation"]


def test_plan_and_covenant_checks_fail_when_ignored(tmp_path):
    args = _args(tmp_path)
    (tmp_path / "cases.json").write_text(json.dumps([PLAN_CASE]))
    rows = ev.run(args, client=plan_script({"ebitda_margin": [0.17]}).client(),
                  llm=lambda d: proposal())                                   # flat plan, derived covenants
    g = rows[0]["grade"]
    assert g["plan_ok"] == 0.0 and g["covenant_ok"] == 0.0 and g["all_checks"] == 0.0
    assert "FAIL base ebitda_margin_by_year" in rows[0]["explanation"]["plan_ok"]


def test_new_cases_are_well_formed():
    cases = json.load(open("evals/agent_cases.json"))
    assert len(cases) == 15 and len({c["id"] for c in cases}) == 15
    from sector_benchmarks import SECTORS
    for c in cases:
        assert set(c["expect"]["sector"]) <= set(SECTORS), c["id"]


@pytest.mark.parametrize("last, passes", [(0.12, True), (0.13, False)])
def test_phased_margin_squeeze_counts_for_its_end_level(tmp_path, last, passes):
    """'Margins compress by 200bps after we buy it' can be run flat or phased over the hold:
    a plan that ends at the target margin satisfies the expected scenario."""
    case = {**CASE, "expect": {"sector": ["industrials"],
                               "scenario_overrides": [{"ebitda_margin": 0.12, "reprice_entry": False}]}}
    script = ScriptedClaude([
        _message([_tool_use("t1", "generate_base_case", {"description": "x"})], "tool_use"),
        _message([_tool_use("t2", "run_scenario", {"name": "squeeze", "rationale": "x", "overrides": {},
                                                   "plan": {"ebitda_margin": [0.14, 0.13, last]}})], "tool_use"),
        _message([{"type": "text", "text": "ok"}], "end_turn"),
    ])
    args = _args(tmp_path)
    (tmp_path / "cases.json").write_text(json.dumps([case]))
    row = ev.run(args, client=script.client(), llm=fake_llm)[0]
    assert row["grade"]["scenarios_ok"] == float(passes), row["explanation"]["scenarios_ok"]
