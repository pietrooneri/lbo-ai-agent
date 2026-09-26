"""Saving and reloading a deal: the base case must come back exactly, without calling Claude."""

import json

import anthropic
import openpyxl
import pytest

import lbo_agent
from assumption_generator import GenerationResult
from lbo_agent import DealSession
from test_assumption_generator import proposal
from test_lbo_agent import ScriptedClaude, _message, _tool_use


class CountingLLM:
    def __init__(self):
        self.calls = 0

    def __call__(self, description):
        self.calls += 1
        return proposal(provided={"revenue_at_entry"})


def no_llm(description):
    raise AssertionError("Claude must not be asked to re-estimate a saved base case")


@pytest.fixture
def deal(tmp_path):
    s = DealSession(output_dir=str(tmp_path / "out"), llm=CountingLLM())
    s.generate_base_case("Valvole, fatturato 400m")
    s.run_scenario("downside", {"revenue_growth": -0.02, "ebitda_margin": 0.11}, "oil & gas capex cut")
    s.run_scenario("downside_low_exit", {"exit_ev_multiple": 6.5}, "plus derating", based_on="downside")
    s.solve_for_target("entry_ev_multiple", "irr", 0.20, save_as="max_price_20")
    return s


def test_round_trip_restores_everything(deal, tmp_path):
    path = deal.save()
    assert path == str(tmp_path / "out" / "Project_Test_deal.json")

    loaded = DealSession.load(path, str(tmp_path / "out"), llm=no_llm)
    assert loaded.generation.to_dict() == deal.generation.to_dict()
    assert list(loaded.scenarios) == list(deal.scenarios)
    for name, sc in deal.scenarios.items():
        other = loaded.scenarios[name]
        assert other.assumptions == sc.assumptions
        assert (other.based_on, other.rationale, other.overrides) == (sc.based_on, sc.rationale, sc.overrides)
        assert loaded._summary(name, other.assumptions) == deal._summary(name, sc.assumptions)
    assert loaded.description == "Valvole, fatturato 400m"
    assert loaded.provenance["loaded_from"] == path


def test_loaded_base_case_is_not_re_estimated(deal, tmp_path):
    loaded = DealSession.load(deal.save(), str(tmp_path), llm=no_llm)
    view = loaded.generate_base_case("anything the model passes")
    assert "not re-estimated" in view["note"]
    assert set(view["existing_scenarios"]) == {"downside", "downside_low_exit", "max_price_20"}
    assert view["base_case_results"] == deal._summary("base", deal.scenarios["base"].assumptions)


def test_same_session_does_not_regenerate_unless_asked(tmp_path):
    llm = CountingLLM()
    s = DealSession(output_dir=str(tmp_path), llm=llm)
    s.generate_base_case("x")
    s.run_scenario("downside", {"revenue_growth": 0.0}, "flat")
    s.generate_base_case("x")
    assert llm.calls == 1 and "downside" in s.scenarios
    s.generate_base_case("x", regenerate=True)
    assert llm.calls == 2 and list(s.scenarios) == ["base"]


def test_generator_json_loads_as_base_case(tmp_path):
    s = DealSession(output_dir=str(tmp_path), llm=CountingLLM())
    s.generate_base_case("x")
    path = tmp_path / "generator.json"
    path.write_text(json.dumps(s.generation.to_dict()))              # what assumption_generator.py --json writes
    loaded = DealSession.load(str(path), str(tmp_path), llm=no_llm)
    assert loaded.generation.assumptions == s.generation.assumptions
    assert list(loaded.scenarios) == ["base"]


@pytest.mark.parametrize("content, message", [
    ({"hello": 1}, "not a deal file"),
    ({"format": "lbo-deal", "version": 99, "base": {}}, "newer version"),
])
def test_bad_files_are_rejected_clearly(tmp_path, content, message):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(content))
    with pytest.raises(ValueError, match=message):
        DealSession.load(str(path))


def test_tampered_assumptions_fail_engine_validation(deal, tmp_path):
    data = deal.to_dict()
    data["base"]["assumptions"]["senior_leverage_x"] = 99.0
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="senior_leverage_x"):
        DealSession.load(str(path))


def test_generation_result_round_trip(deal):
    d = deal.generation.to_dict()
    assert GenerationResult.from_dict(d).to_dict() == d


def test_excel_from_reloaded_deal_states_its_source(deal, tmp_path):
    loaded = DealSession.load(deal.save(), str(tmp_path / "xl"), llm=no_llm)
    path = loaded.export_excel("base")["path"]
    audit = [c.value for c in openpyxl.load_workbook(path)["Audit"]["C"] if isinstance(c.value, str)]
    assert any("reloaded unchanged from Project_Test_deal.json" in v for v in audit)


def test_cli_load_reuses_base_case_and_saves_back(deal, tmp_path, monkeypatch, capsys):
    """--load: the scripted Claude calls generate_base_case, gets the saved base, adds a
    scenario; the deal file is written back with the new scenario and the same base."""
    path = deal.save()
    script = ScriptedClaude([
        _message([_tool_use("t1", "generate_base_case", {"description": "follow-up"})], "tool_use"),
        _message([_tool_use("t2", "run_scenario", {"name": "lev4", "overrides": {"total_leverage_x": 4.0},
                                                   "rationale": "lower leverage"})], "tool_use"),
        _message([{"type": "text", "text": "done"}], "end_turn"),
    ])
    client = script.client()
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: client)
    monkeypatch.setattr(lbo_agent, "generate_assumptions", lambda *a, **k: no_llm(""))

    lbo_agent.main(["--load", path, "--out-dir", str(tmp_path / "out"), "E con leva 4x?"])

    assert "done" in capsys.readouterr().out
    first_tool_result = json.loads(script.requests[1]["messages"][-1]["content"][0]["content"])
    assert "not re-estimated" in first_tool_result["note"]
    saved = json.loads(open(path).read())
    assert saved["base"] == json.loads(json.dumps(deal.generation.to_dict()))   # tuples become lists in JSON
    assert [s["name"] for s in saved["scenarios"]] == ["downside", "downside_low_exit", "max_price_20", "lev4"]


def test_answers_and_costs_survive_a_reload(tmp_path):
    """Found by using the app: reopening a project lost the agent's answers and the cost."""
    script = ScriptedClaude([
        _message([_tool_use("t1", "generate_base_case", {"description": "x"})], "tool_use"),
        _message([{"type": "text", "text": "Prima risposta."}], "end_turn"),
    ])
    from lbo_agent import LBOAgent
    s = DealSession(output_dir=str(tmp_path), llm=CountingLLM())
    LBOAgent(s, client=script.client()).ask("testo con istruzioni extra", display_question="Com'è il deal?")
    loaded = DealSession.load(s.save(), str(tmp_path), llm=no_llm)
    assert loaded.history[0]["question"] == "Com'è il deal?" and loaded.history[0]["answer"] == "Prima risposta."
    assert loaded.usage_log == s.usage_log and len(loaded.usage_log) == 2


def test_projects_saved_before_fees_load_with_zero_fees(tmp_path):
    """Deal files written before transaction costs existed have no fee fields: they reload
    unchanged (fees = 0) and can still be exported and extended."""
    loaded = DealSession.load("examples/valves_deal.json", str(tmp_path), llm=no_llm)
    a = loaded.scenarios["base"].assumptions
    assert (a.transaction_fees_pct_ev, a.financing_fees_pct_debt, a.senior_oid_pct) == (0.0, 0.0, 0.0)
    loaded.run_scenario("with_fees", {"transaction_fees_pct_ev": 0.02}, "add fees")
    assert loaded.export_excel("with_fees")["path"].endswith(".xlsx")


def test_new_analysis_of_same_company_does_not_overwrite(deal, tmp_path):
    out = tmp_path / "out"
    first = deal.save()
    deal.export_excel("base")
    again = DealSession(output_dir=str(out), llm=CountingLLM())
    again.generate_base_case("Valvole, fatturato 400m")
    again.export_excel("base")
    second = again.save()
    assert second == str(out / "Project_Test_v2_deal.json") and first != second
    assert (out / "Project_Test_base.xlsx").exists() and (out / "Project_Test_v2_base.xlsx").exists()
    assert again.save() == second                                   # later saves stay on its own file
    third = DealSession(output_dir=str(out), llm=CountingLLM())
    third.generate_base_case("Valvole, fatturato 400m")
    assert third.save().endswith("Project_Test_v3_deal.json")


def test_reopened_project_keeps_saving_to_its_file(deal, tmp_path):
    out = tmp_path / "out"
    path = deal.save()
    reopened = DealSession.load(path, str(out), llm=no_llm)
    reopened.run_scenario("extra", {"exit_ev_multiple": 7.0}, "follow-up question")
    assert reopened.save() == path
    assert sorted(p.name for p in out.glob("*_deal.json")) == ["Project_Test_deal.json"]
