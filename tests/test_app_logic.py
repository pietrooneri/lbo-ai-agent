"""App logic without the UI: form -> request, key handling, Italian texts, a full offline run."""

import json

import keyring
import pytest

import app_logic as L
from test_assumption_generator import proposal
from test_lbo_agent import ScriptedClaude, _message, _tool_use


def test_request_carries_every_known_figure_and_question():
    text = L.build_request("Produttore di valvole", revenue=320, ebitda=48, asking_multiple=8.5,
                           downside=True, irr_target=20, moic_target=2.5, other_questions="E l'export?")
    for piece in ("fatturato 320 milioni", "EBITDA 48 milioni", "chiede 8,5x", "IRR del 20%",
                  "MOIC di 2,5x", "downside", "E l'export?", "non è esperto"):
        assert piece in text


def test_request_needs_a_description():
    with pytest.raises(ValueError, match="descriva"):
        L.build_request("   ")


def test_unknown_figures_are_left_out():
    text = L.build_request("Pastificio", revenue=None, ebitda="", downside=False)
    assert "Dati noti" not in text and "downside" not in text


@pytest.mark.parametrize("raw, ok", [
    ("“sk-ant-api03-" + "a" * 95 + "”", True),           # curly quotes from Notes / TextEdit
    ("  sk-ant-api03-" + "a" * 95 + "\n", True),
    ("sk-ant-...", False),                                # the placeholder
    ("sk-ant-admin01-" + "a" * 95, False),                # admin keys cannot call the model
])
def test_key_cleaning_and_shape(raw, ok):
    assert L.looks_like_key(L.clean_key(raw)) is ok


def test_key_goes_to_the_keychain_only(monkeypatch):
    stored = {}
    monkeypatch.setattr(keyring, "set_password", lambda s, u, k: stored.update({(s, u): k}))
    monkeypatch.setattr(keyring, "get_password", lambda s, u: stored.get((s, u)))
    L.save_api_key("“sk-ant-api03-" + "b" * 95 + "”")
    assert stored[(L.KEYRING_SERVICE, L.KEYRING_USER)] == "sk-ant-api03-" + "b" * 95
    assert L.mask_key(L.get_api_key()) == "sk-ant-api03-…bbbb"
    with pytest.raises(ValueError, match="sk-ant-"):
        L.save_api_key("hello")


@pytest.mark.parametrize("english, italian", [
    ("RCF drawn (peak 3.4) to cover a cash shortfall", "fino a 3,4 mln"),
    ("Liquidity: RCF drawn up to 31.0 vs 24.0 committed — x", "servirebbero 31,0 mln"),
    ("Minimum EBITDA / interest coverage 1.83x (< 2.0x)", "solo 1,83 volte"),
    ("Exit equity value <= 0: sponsor loses the entire investment", "andrebbe perso"),
    ("ebitda_margin = 30.00% (provided) is outside logistics_transport margin range 6.00%-14.00%; kept as given",
     "margine 30,00%"),
])
def test_warnings_in_plain_italian(english, italian):
    assert italian in L.translate_warning(english)


def test_progress_messages():
    assert "IRR del 20%" in L.progress_message("solve_for_target", {"variable": "entry_ev_multiple",
                                                                    "metric": "irr", "target": 0.2})
    assert "margine 12,0%" in L.progress_message("run_scenario", {"name": "downside",
                                                                  "overrides": {"ebitda_margin": 0.12}})


def test_full_offline_analysis(tmp_path):
    script = ScriptedClaude([
        _message([_tool_use("t1", "generate_base_case", {"description": "x"})], "tool_use"),
        _message([_tool_use("t2", "run_scenario", {"name": "downside", "rationale": "r",
                                                   "overrides": {"ebitda_margin": 0.12}}),
                  _tool_use("t3", "export_excel", {"scenario": "base"})], "tool_use"),
        _message([{"type": "text", "text": "Risposta."}], "end_turn"),
    ])
    a = L.Analysis.new(tmp_path, client=script.client(), llm=lambda d: proposal(provided={"revenue_at_entry"}))
    assert a.ask("Valvole") == "Risposta."
    assert a.progress()[0].startswith("Stimo le ipotesi") and len(a.progress()) == 3
    assert a.has_results and a.excel_files and not a.running

    rows = L.table_rows(a.session)
    assert [r["scenario"] for r in rows] == ["Caso base", "Downside"]
    assert rows[0]["irr"].endswith("%") and "," in rows[0]["irr"]
    labels = [k["label"] for k in L.kpis(a.session)]
    assert labels[0] == "EBITDA di partenza" and labels[-1] == "IRR"
    assert len(L.irr_chart(a.session)["series"][0]["data"]) == 2
    assert len(L.debt_chart(a.session)["series"]) == 2

    projects = L.list_projects(tmp_path)
    assert projects[0]["company"] == "Project Test" and projects[0]["scenarios"] == 2
    reopened = L.Analysis.open(projects[0]["path"], tmp_path)
    assert L.table_rows(reopened.session) == rows


def test_errors_are_explained_in_italian(tmp_path):
    import anthropic
    import httpx2
    from anthropic import DefaultHttpxClient

    def deny(request):
        return httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error",
                                                                      "message": "invalid x-api-key"}})
    client = anthropic.Anthropic(api_key="bad", max_retries=0,
                                 http_client=DefaultHttpxClient(transport=httpx2.MockTransport(deny)))
    a = L.Analysis.new(tmp_path, client=client)
    with pytest.raises(anthropic.AuthenticationError) as exc:
        a.ask("x")
    assert "Impostazioni" in L.friendly_error(exc.value)
    assert not a.running


def test_english_request_and_texts():
    text = L.build_request("Italian valve maker", revenue=320.5, irr_target=20, lang="en")
    assert "revenue €320.5 million" in text and "20% IRR" in text and "plain words" in text
    assert L.progress_message("solve_for_target", {"variable": "entry_ev_multiple", "metric": "irr",
                                                   "target": 0.2}, "en") == "Finding the entry multiple that gives a 20% IRR"
    assert L.translate_warning("RCF drawn (peak 3.4) to cover a cash shortfall", "en").endswith("(up to €3.4m)")
    assert set(L.TEXT["it"]) == set(L.TEXT["en"])                    # no string missing in either language
    assert len(L.GLOSSARY["it"]) == len(L.GLOSSARY["en"])


def test_number_formats_follow_the_language():
    assert (L.money(1234.56, "it"), L.money(1234.56, "en")) == ("1.234,6 mln", "€1,234.6m")
    assert (L.pct(0.10254, "it"), L.pct(0.10254, "en")) == ("10,3%", "10.3%")


def test_language_preference_is_remembered(tmp_path):
    f = tmp_path / "settings.json"
    assert L.get_language(f) == "it"                                  # default
    L.set_language("en", f)
    assert L.get_language(f) == "en"
    with pytest.raises(ValueError):
        L.set_language("fr", f)


def test_reopened_project_shows_answers_cost_and_excel(tmp_path):
    script = ScriptedClaude([
        _message([_tool_use("t1", "generate_base_case", {"description": "x"}),
                  _tool_use("t2", "export_excel", {"scenario": "base"})], "tool_use"),
        _message([{"type": "text", "text": "Raccomandazione."}], "end_turn"),
    ])
    a = L.Analysis.new(tmp_path, client=script.client(), llm=lambda d: proposal(provided={"revenue_at_entry"}))
    a.ask("testo lungo con istruzioni", "Analisi iniziale: valvole")
    reopened = L.Analysis.open(L.list_projects(tmp_path)[0]["path"], tmp_path)
    assert reopened.history == [dict(a.history[0])]
    assert reopened.history[0]["question"] == "Analisi iniziale: valvole"
    assert L.cost_label(reopened.session) == L.cost_label(a.session) is not None
    assert reopened.excel_files and reopened.excel_files[0].endswith("_base.xlsx")


def test_old_projects_say_cost_was_not_recorded(tmp_path):
    """Projects saved before usage tracking must say so instead of showing nothing."""
    import json as _json
    s = L.DealSession(output_dir=str(tmp_path), llm=lambda d: proposal(provided={"revenue_at_entry"}))
    s.generate_base_case("x")
    data = s.to_dict()
    del data["usage"]                                           # what an old deal file looks like
    (tmp_path / "Old_deal.json").write_text(_json.dumps(data))
    old = L.Analysis.open(str(tmp_path / "Old_deal.json"), tmp_path)
    assert "non registrato" in L.cost_label(old.session)
    assert L.list_projects(tmp_path)[0]["cost"] == "costo non registrato"
    data["usage"] = [{"model": "claude-opus-5", "input_tokens": 100000, "output_tokens": 10000,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}]
    (tmp_path / "Old_deal.json").write_text(_json.dumps(data))
    assert L.list_projects(tmp_path)[0]["cost"] == "circa 0,75 $"


def test_profiles_are_shown_as_a_sequence():
    assert L.fmt_value("ebitda_margin_by_year", (0.14, 0.12), "it") == "14,0% → 12,0%"
    assert L.progress_message("run_scenario", {"name": "downside", "overrides": {},
                                               "plan": {"ebitda_margin": [0.14, 0.12]}}, "en") == \
        "Running the «downside» scenario (margin by year 14.0% → 12.0%)"


@pytest.mark.parametrize("lang, expected", [
    ("it", "Covenant violato nell'anno 3: leva netta 6,10x contro un massimo di 5,75x · copertura interessi 1,90x contro un minimo di 2,00x"),
    ("en", "Covenant breached in year 3: net leverage 6.10x vs a maximum of 5.75x · interest cover 1.90x vs a minimum of 2.00x"),
])
def test_covenant_warning_in_plain_words(lang, expected):
    w = "Covenant breach in year 3: net leverage 6.10x vs max 5.75x and interest cover 1.90x vs min 2.00x"
    assert L.translate_warning(w, lang) == expected


def test_covenant_column():
    assert L.covenant_text(None) == "—"
    assert L.covenant_text({"first_breach_year": 2, "lowest_headroom": -0.1}) == "violati nell'anno 2"
    assert L.covenant_text({"first_breach_year": None, "lowest_headroom": 0.284}, "en") == "met (lowest headroom 28%)"
