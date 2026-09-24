"""
Logic behind the desktop app (app.py), kept free of UI code so it can be tested.

Everything the user reads is produced here, in Italian or English (`lang`): the request
sent to the agent from the guided form, progress messages, warnings, scenario tables,
chart data, glossary and error messages. The API key lives in the macOS Keychain, never
on screen or on disk; the language choice lives in app_settings.json.
"""

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import keyring

from lbo_agent import DealSession, LBOAgent, usage_cost_usd
from lbo_engine import run_model

KEYRING_SERVICE = "lbo-agent"
KEYRING_USER = "anthropic-api-key"
OUTPUT_DIR = Path(__file__).parent / "output"
SETTINGS_FILE = Path(__file__).parent / "app_settings.json"
LANGUAGES = {"it": "Italiano", "en": "English"}

# ---------------------------------------------------------------------------
# Settings and API key
# ---------------------------------------------------------------------------

def get_language(settings_file: Path = SETTINGS_FILE) -> str:
    try:
        lang = json.loads(Path(settings_file).read_text())["language"]
        return lang if lang in LANGUAGES else "it"
    except (OSError, ValueError, KeyError):
        return "it"


def set_language(lang: str, settings_file: Path = SETTINGS_FILE) -> None:
    if lang not in LANGUAGES:
        raise ValueError(f"Unsupported language {lang!r}")
    Path(settings_file).write_text(json.dumps({"language": lang}))


def get_api_key() -> Optional[str]:
    try:
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    except keyring.errors.KeyringError:
        return None


def save_api_key(key: str, lang: str = "it") -> None:
    key = clean_key(key)
    if not looks_like_key(key):
        raise ValueError(TEXT[lang]["key_invalid"])
    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key)


def delete_api_key() -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
    except keyring.errors.PasswordDeleteError:
        pass


def clean_key(key: str) -> str:
    """Strip what copy-paste tends to add: spaces, newlines, straight or curly quotes."""
    return key.strip().strip("\"'“”‘’").strip()


def looks_like_key(key: str) -> bool:
    return key.startswith("sk-ant-") and not key.startswith("sk-ant-admin") and len(key) >= 80 and " " not in key


def mask_key(key: Optional[str], lang: str = "it") -> str:
    return f"{key[:13]}…{key[-4:]}" if key else TEXT[lang]["no_key"]


def activate_key(key: str) -> None:
    """The agent and the generator build their Anthropic clients from the environment:
    set it for this process only (never for the shell, never shown)."""
    os.environ["ANTHROPIC_API_KEY"] = key


# ---------------------------------------------------------------------------
# Number formatting
# ---------------------------------------------------------------------------

def num(v: float, decimals: int, lang: str) -> str:
    s = f"{v:,.{decimals}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".") if lang == "it" else s


def pct(v: float, lang: str, decimals: int = 1) -> str:
    return f"{num(v * 100, decimals, lang)}%"


def mult(v: float, lang: str) -> str:
    return f"{num(v, 2, lang)}x"


def money(v: float, lang: str) -> str:
    return f"{num(v, 1, lang)} mln" if lang == "it" else f"€{num(v, 1, lang)}m"


def _short(v: float, lang: str) -> str:
    return f"{v:g}".replace(".", ",") if lang == "it" else f"{v:g}"


def _num(v) -> Optional[float]:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


PCT_FIELDS = {"ebitda_margin", "revenue_growth", "capex_pct_revenue", "da_pct_revenue", "nwc_pct_of_rev_growth",
              "senior_rate", "senior_mandatory_amort_pct", "sub_rate", "cash_sweep_pct", "rcf_rate", "tax_rate",
              "transaction_fees_pct_ev", "financing_fees_pct_debt", "senior_oid_pct"}


def fmt_value(field_name: str, v, lang: str = "it") -> str:
    if field_name.endswith("_by_year"):                       # year-by-year profile: 14,0% → 12,0% → 11,0%
        return " → ".join(fmt_value(field_name[: -len("_by_year")], x, lang) for x in (v or ()))
    if field_name in PCT_FIELDS:
        return pct(v, lang)
    if field_name.endswith("_x") or "multiple" in field_name:
        return mult(v, lang)
    if field_name in ("hold_period_years", "fee_amortization_years"):
        return f"{v:g} {'anni' if lang == 'it' else 'years'}"
    return money(v, lang)


# ---------------------------------------------------------------------------
# Texts
# ---------------------------------------------------------------------------

FIELD_LABELS = {
    "it": {"revenue_at_entry": "fatturato", "entry_ebitda": "EBITDA d'ingresso", "ebitda_margin": "margine",
           "entry_ev_multiple": "multiplo d'ingresso", "revenue_growth": "crescita annua",
           "capex_pct_revenue": "investimenti", "da_pct_revenue": "ammortamenti",
           "nwc_pct_of_rev_growth": "capitale circolante", "total_leverage_x": "debito totale",
           "senior_leverage_x": "debito senior", "senior_rate": "tasso senior",
           "senior_mandatory_amort_pct": "rimborso obbligatorio", "sub_rate": "tasso subordinato",
           "cash_sweep_pct": "cash sweep", "rcf_commitment": "linea RCF", "rcf_rate": "tasso RCF",
           "tax_rate": "tasse", "min_cash": "cassa minima", "hold_period_years": "anni di detenzione",
           "exit_ev_multiple": "multiplo d'uscita", "transaction_fees_pct_ev": "fee M&A",
           "financing_fees_pct_debt": "fee di finanziamento", "senior_oid_pct": "OID sul Term Loan",
           "fee_amortization_years": "anni di ammortamento fee"},
    "en": {"revenue_at_entry": "revenue", "entry_ebitda": "entry EBITDA", "ebitda_margin": "margin",
           "entry_ev_multiple": "entry multiple", "revenue_growth": "annual growth", "capex_pct_revenue": "capex",
           "da_pct_revenue": "D&A", "nwc_pct_of_rev_growth": "working capital", "total_leverage_x": "total debt",
           "senior_leverage_x": "senior debt", "senior_rate": "senior rate",
           "senior_mandatory_amort_pct": "mandatory repayment", "sub_rate": "subordinated rate",
           "cash_sweep_pct": "cash sweep", "rcf_commitment": "RCF", "rcf_rate": "RCF rate", "tax_rate": "tax",
           "min_cash": "minimum cash", "hold_period_years": "holding years", "exit_ev_multiple": "exit multiple",
           "transaction_fees_pct_ev": "M&A fees", "financing_fees_pct_debt": "financing fees",
           "senior_oid_pct": "Term Loan OID", "fee_amortization_years": "fee amortisation years"},
}

TEXT: Dict[str, Dict[str, str]] = {
    "it": {
        "app_disclaimer": "Stime illustrative a scopo di studio, non una valutazione professionale",
        "tab_analysis": "Analisi", "tab_projects": "Progetti salvati", "tab_glossary": "Glossario",
        "tab_settings": "Impostazioni",
        "key_needed": "Per usare l'agente serve una chiave API di Anthropic. Si inserisce una volta sola.",
        "go_settings": "Vai a Impostazioni", "save_key_first": "Prima salva la tua chiave API in Impostazioni.",
        "new_analysis": "Nuova analisi",
        "form_intro": "Descrivi l'azienda come la racconteresti a un collega: cosa fa, dove, per chi. "
                      "I numeri che non conosci lasciali vuoti, li stima l'agente.",
        "description": "Descrizione dell'azienda",
        "description_ph": "Es.: produttore italiano di valvole industriali, 4 stabilimenti, clienti nel settore "
                          "oil&gas e utilities",
        "revenue": "Fatturato (milioni €)", "ebitda": "EBITDA (milioni €)",
        "asking": "Prezzo chiesto (multiplo EBITDA)", "what_to_know": "Cosa vuoi sapere",
        "q_downside": "Cosa succede se le cose vanno peggio del previsto (downside)",
        "q_irr": "Prezzo massimo per ottenere un rendimento (IRR) del",
        "q_moic": "Quanto debito serve per moltiplicare l'investimento (MOIC) per",
        "other": "Altre domande (facoltativo)", "start": "Avvia analisi",
        "start_hint": "Circa 1-2 minuti e 0,25-0,35 $ di costo API",
        "working": "L'agente sta lavorando… {s} s (di solito 1-2 minuti)",
        "thinking": "Leggo la descrizione e decido cosa calcolare…",
        "to_check": "Da controllare", "scenarios": "Scenari",
        "explanation": "Risposte dell'agente", "loaded_hint": "Progetto aperto dal salvataggio.",
        "follow_title": "Fai un'altra domanda sullo stesso deal",
        "follow_ph": "Es.: e se il debito fosse 3,5 volte l'EBITDA?", "ask": "Chiedi",
        "open_excel": "Apri Excel", "cost": "Costo API totale del progetto: {c}", "cost_small": "meno di 0,01 $",
        "cost_untracked": "Costo non registrato: progetto salvato prima che l'app tenesse traccia dei costi "
                          "(il dato reale è nella Console Anthropic → Usage)",
        "cost_short_untracked": "costo non registrato", "cost_short_none": "nessun costo ancora",
        "no_projects": "Nessun progetto salvato: ogni analisi viene salvata qui automaticamente.",
        "project_meta": "{sector} · {n} scenari · salvato il {saved} · {cost}", "open": "Apri",
        "open_failed": "Impossibile aprire il progetto: {e}",
        "glossary_title": "Le parole che trovi nei risultati, spiegate semplice",
        "key_title": "Chiave API Anthropic",
        "key_intro": "Si crea su console.anthropic.com → API Keys. Viene salvata nel Portachiavi del Mac: non "
                     "compare mai a schermo e non finisce in nessun file.",
        "key_label": "Chiave API: {k}", "key_paste": "Incolla qui la chiave", "save": "Salva",
        "verify": "Verifica (gratis)", "remove": "Rimuovi", "key_saved": "Chiave salvata nel Portachiavi del Mac.",
        "key_works": "La chiave funziona.", "key_removed": "Chiave rimossa dal Portachiavi.",
        "no_key": "nessuna chiave salvata",
        "key_invalid": "Non sembra una chiave API Anthropic: deve iniziare con 'sk-ant-' ed essere lunga circa "
                       "100 caratteri.",
        "files_title": "Dove finiscono i file", "files_intro": "Excel e progetti vengono salvati in: {d}",
        "open_folder": "Apri la cartella", "language_title": "Lingua",
        "language_intro": "Lingua dell'app e delle risposte dell'agente.",
        "need_description": "Scrivi almeno una riga che descriva l'azienda.",
        "known": "Dati noti", "revenue_fact": "fatturato {v} milioni di euro", "ebitda_fact": "EBITDA {v} milioni di euro",
        "asking_fact": "il venditore chiede {v}x EBITDA",
        "q_irr_text": "Qual è il multiplo massimo che possiamo pagare per un IRR del {v}%?",
        "q_moic_text": "Quanta leva servirebbe per arrivare a un MOIC di {v}x?",
        "q_downside_text": "Com'è lo scenario negativo (downside) dopo l'acquisto?",
        "plain": "Spiega i risultati in modo semplice, per chi non è esperto di LBO.",
        "plain_follow": "Rispondi in modo semplice, per chi non è esperto di LBO.",
        "first_question": "Analisi iniziale: {d}",
        "p_base": "Stimo le ipotesi dell'azienda: settore, margini, prezzo e debito",
        "p_scenario": "Simulo lo scenario «{n}»", "p_solve": "Cerco il {w} che dà {g}",
        "p_goal_irr": "un IRR del {v}%", "p_goal_moic": "un MOIC di {v}x",
        "p_compare": "Confronto gli scenari", "p_excel": "Preparo il file Excel", "p_other": "Eseguo {t}",
        "base_case": "Caso base",
        "k_ev": "Prezzo (valore d'impresa)", "k_ev_h": "Quanto si paga per l'intera azienda, debito compreso",
        "k_eq": "Equity del fondo", "k_eq_h": "I soldi che il fondo mette di tasca propria",
        "k_moic_h": "Quante volte il fondo moltiplica i soldi investiti",
        "k_irr_h": "Rendimento annuo; per un fondo di private equity di solito si punta ad almeno 20%",
        "c_scenario": "Scenario", "c_changes": "Cosa cambia rispetto al caso base", "c_ev": "Prezzo",
        "c_lev": "Debito (x EBITDA)", "c_flags": "Avvisi",
        "ch_irr": "Rendimento annuo (IRR) per scenario", "ch_target": "obiettivo tipico 20%",
        "ch_debt": "Quanto pesa il debito: debito netto / EBITDA",
        "ch_debt_sub": "Più in basso = più sicuro. Scenari con lo stesso debito si sovrappongono.",
        "year": "Anno",
        "w_liquidity": "La cassa non basta: servirebbero {a} di linea di credito, ma ne sono disponibili solo {b}",
        "w_rcf": "In alcuni anni la cassa non basta e si usa la linea di credito (fino a {a})",
        "w_cov": "Margine di sicurezza sugli interessi basso: l'EBITDA copre gli interessi solo {a} volte (sotto 2)",
        "w_equity": "All'uscita il valore dell'equity è zero: l'investimento andrebbe perso",
        "w_provided": "Il dato che hai indicato ({f} {v}) è fuori dai valori tipici del settore ({r}): l'ho "
                      "tenuto, ma verificalo",
        "e_auth": "La chiave API non è valida o è stata revocata. Aggiornala in Impostazioni.",
        "e_perm": "La chiave API non ha i permessi per questa richiesta.",
        "e_credit": "Credito API esaurito: ricarica su console.anthropic.com → Billing.",
        "e_rate": "Troppe richieste in poco tempo: aspetta un minuto e riprova.",
        "e_conn": "Impossibile raggiungere Anthropic: controlla la connessione a internet.",
        "e_other": "Qualcosa è andato storto: {e}",
    },
    "en": {
        "app_disclaimer": "Illustrative estimates for learning purposes, not a professional valuation",
        "tab_analysis": "Analysis", "tab_projects": "Saved projects", "tab_glossary": "Glossary",
        "tab_settings": "Settings",
        "key_needed": "The agent needs an Anthropic API key. You only enter it once.",
        "go_settings": "Go to Settings", "save_key_first": "Save your API key in Settings first.",
        "new_analysis": "New analysis",
        "form_intro": "Describe the company as you would to a colleague: what it does, where, for whom. "
                      "Leave blank the figures you don't know; the agent estimates them.",
        "description": "Company description",
        "description_ph": "E.g.: Italian maker of industrial valves, 4 plants, clients in oil & gas and utilities",
        "revenue": "Revenue (€ millions)", "ebitda": "EBITDA (€ millions)",
        "asking": "Asking price (EBITDA multiple)", "what_to_know": "What do you want to know",
        "q_downside": "What happens if things go worse than planned (downside)",
        "q_irr": "Maximum price for an annual return (IRR) of",
        "q_moic": "How much debt is needed to multiply the investment (MOIC) by",
        "other": "Other questions (optional)", "start": "Start analysis",
        "start_hint": "About 1-2 minutes and $0.25-0.35 of API cost",
        "working": "The agent is working… {s} s (usually 1-2 minutes)",
        "thinking": "Reading the description and deciding what to compute…",
        "to_check": "Worth checking", "scenarios": "Scenarios",
        "explanation": "Agent answers", "loaded_hint": "Project opened from a save.",
        "follow_title": "Ask another question about this deal",
        "follow_ph": "E.g.: what if debt were 3.5 times EBITDA?", "ask": "Ask",
        "open_excel": "Open Excel", "cost": "Total API cost of this project: {c}", "cost_small": "less than $0.01",
        "cost_untracked": "Cost not recorded: project saved before the app tracked costs "
                          "(the real figure is in the Anthropic Console → Usage)",
        "cost_short_untracked": "cost not recorded", "cost_short_none": "no cost yet",
        "no_projects": "No saved projects yet: every analysis is saved here automatically.",
        "project_meta": "{sector} · {n} scenarios · saved {saved} · {cost}", "open": "Open",
        "open_failed": "Could not open the project: {e}",
        "glossary_title": "The terms you see in the results, in plain words",
        "key_title": "Anthropic API key",
        "key_intro": "Create it at console.anthropic.com → API Keys. It is stored in the macOS Keychain: never "
                     "shown on screen and never written to a file.",
        "key_label": "API key: {k}", "key_paste": "Paste the key here", "save": "Save",
        "verify": "Check (free)", "remove": "Remove", "key_saved": "Key saved in the macOS Keychain.",
        "key_works": "The key works.", "key_removed": "Key removed from the Keychain.",
        "no_key": "no key saved",
        "key_invalid": "This does not look like an Anthropic API key: it should start with 'sk-ant-' and be "
                       "about 100 characters long.",
        "files_title": "Where files go", "files_intro": "Excel files and projects are saved in: {d}",
        "open_folder": "Open the folder", "language_title": "Language",
        "language_intro": "Language of the app and of the agent's answers.",
        "need_description": "Write at least one line describing the company.",
        "known": "Known figures", "revenue_fact": "revenue €{v} million", "ebitda_fact": "EBITDA €{v} million",
        "asking_fact": "the seller asks {v}x EBITDA",
        "q_irr_text": "What is the maximum multiple we can pay for a {v}% IRR?",
        "q_moic_text": "How much leverage would we need for a {v}x MOIC?",
        "q_downside_text": "What does the downside look like after we buy it?",
        "plain": "Explain the results in plain words, for someone who is not an LBO expert.",
        "plain_follow": "Answer in plain words, for someone who is not an LBO expert.",
        "first_question": "Initial analysis: {d}",
        "p_base": "Estimating the company's assumptions: sector, margins, price and debt",
        "p_scenario": "Running the «{n}» scenario", "p_solve": "Finding the {w} that gives {g}",
        "p_goal_irr": "a {v}% IRR", "p_goal_moic": "a {v}x MOIC",
        "p_compare": "Comparing scenarios", "p_excel": "Preparing the Excel file", "p_other": "Running {t}",
        "base_case": "Base case",
        "k_ev": "Price (enterprise value)", "k_ev_h": "What is paid for the whole company, debt included",
        "k_eq": "Fund equity", "k_eq_h": "The money the fund puts in itself",
        "k_moic_h": "How many times the fund multiplies the money invested",
        "k_irr_h": "Annual return; private equity funds usually aim for at least 20%",
        "c_scenario": "Scenario", "c_changes": "What changes vs the base case", "c_ev": "Price",
        "c_lev": "Debt (x EBITDA)", "c_flags": "Warnings",
        "ch_irr": "Annual return (IRR) by scenario", "ch_target": "typical target 20%",
        "ch_debt": "How heavy the debt is: net debt / EBITDA",
        "ch_debt_sub": "Lower = safer. Scenarios with the same debt overlap.",
        "year": "Year",
        "w_liquidity": "Cash falls short: it would need {a} of credit line, but only {b} is available",
        "w_rcf": "In some years cash falls short and the credit line is used (up to {a})",
        "w_cov": "Thin safety margin on interest: EBITDA covers interest only {a} times (below 2)",
        "w_equity": "At exit the equity is worth nothing: the investment would be lost",
        "w_provided": "The figure you gave ({f} {v}) is outside the sector's usual range ({r}): I kept it, "
                      "but double-check it",
        "e_auth": "The API key is invalid or was revoked. Update it in Settings.",
        "e_perm": "The API key is not allowed to make this request.",
        "e_credit": "API credit exhausted: top up at console.anthropic.com → Billing.",
        "e_rate": "Too many requests in a short time: wait a minute and try again.",
        "e_conn": "Cannot reach Anthropic: check your internet connection.",
        "e_other": "Something went wrong: {e}",
    },
}


def t(lang: str, key: str, **kw) -> str:
    return TEXT[lang][key].format(**kw)


def field_label(name: str, lang: str = "it") -> str:
    labels = FIELD_LABELS[lang]
    if name.endswith("_by_year"):
        base = labels.get(name[: -len("_by_year")], name)
        return f"{base} {'anno per anno' if lang == 'it' else 'by year'}"
    return labels.get(name, name)


# ---------------------------------------------------------------------------
# Guided form -> request text
# ---------------------------------------------------------------------------

def build_request(description: str, revenue=None, ebitda=None, asking_multiple=None,
                  downside: bool = True, irr_target=None, moic_target=None, other_questions: str = "",
                  lang: str = "it") -> str:
    """Turn the form into the kind of request the agent handles best: the description with
    every figure the user knows, then explicit questions. The agent answers in the language
    the request is written in."""
    description = description.strip()
    if not description:
        raise ValueError(t(lang, "need_description"))
    facts = []
    if _num(revenue):
        facts.append(t(lang, "revenue_fact", v=_short(_num(revenue), lang)))
    if _num(ebitda):
        facts.append(t(lang, "ebitda_fact", v=_short(_num(ebitda), lang)))
    if _num(asking_multiple):
        facts.append(t(lang, "asking_fact", v=_short(_num(asking_multiple), lang)))
    text = description.rstrip(".") + "." + (f" {t(lang, 'known')}: " + ", ".join(facts) + "." if facts else "")

    questions = []
    if _num(irr_target):
        questions.append(t(lang, "q_irr_text", v=_short(_num(irr_target), lang)))
    if _num(moic_target):
        questions.append(t(lang, "q_moic_text", v=_short(_num(moic_target), lang)))
    if downside:
        questions.append(t(lang, "q_downside_text"))
    if other_questions.strip():
        questions.append(other_questions.strip())
    if questions:
        text += " " + " ".join(questions)
    return f"{text} {t(lang, 'plain')}"


# ---------------------------------------------------------------------------
# Progress and warnings
# ---------------------------------------------------------------------------

def progress_message(tool: str, args: dict, lang: str = "it") -> str:
    labels = FIELD_LABELS[lang]
    if tool == "generate_base_case":
        return t(lang, "p_base")
    if tool == "run_scenario":
        name = str(args.get("name", "")).replace("_", " ")
        items = list((args.get("overrides") or {}).items()) + [
            (f"{d}_by_year", v) for d, v in (args.get("plan") or {}).items()]
        changes = ", ".join(f"{field_label(k, lang)} {fmt_value(k, v, lang)}" for k, v in items)
        return t(lang, "p_scenario", n=name) + (f" ({changes})" if changes else "")
    if tool == "solve_for_target":
        what = labels.get(args.get("variable", ""), args.get("variable", ""))
        target = args.get("target", 0)
        goal = (t(lang, "p_goal_irr", v=_short(round(target * 100, 1), lang)) if args.get("metric") == "irr"
                else t(lang, "p_goal_moic", v=_short(target, lang)))
        return t(lang, "p_solve", w=what, g=goal)
    if tool == "compare_scenarios":
        return t(lang, "p_compare")
    if tool == "export_excel":
        return t(lang, "p_excel")
    return t(lang, "p_other", t=tool)


def translate_warning(text: str, lang: str = "it") -> str:
    """Engine / guardrail warnings (written by code, in English) -> plain words."""
    m_money = lambda x: money(float(x), lang)
    rules = [
        (r"^Liquidity: RCF drawn up to ([\d.]+) vs ([\d.]+)",
         lambda m: t(lang, "w_liquidity", a=m_money(m[1]), b=m_money(m[2]))),
        (r"^RCF drawn \(peak ([\d.]+)\)", lambda m: t(lang, "w_rcf", a=m_money(m[1]))),
        (r"^Minimum EBITDA / interest coverage ([\d.]+)x",
         lambda m: t(lang, "w_cov", a=num(float(m[1]), 2, lang))),
        (r"^Exit equity value <= 0", lambda m: t(lang, "w_equity")),
        (r"^(\w+) = ([\d.]+)(%|x)? \(provided\) is outside \w+ (?:margin )?range (.+?); kept as given",
         lambda m: t(lang, "w_provided", f=FIELD_LABELS[lang].get(m[1], m[1]),
                     v=num(float(m[2]), 2, lang) + (m[3] or ""),
                     r=m[4].replace(".", ",") if lang == "it" else m[4])),
    ]
    for pattern, render in rules:
        m = re.search(pattern, text)
        if m:
            return render(m)
    return text


# ---------------------------------------------------------------------------
# Results: KPIs, table rows, charts
# ---------------------------------------------------------------------------

def sector_label(key: str) -> str:
    from sector_benchmarks import SECTORS
    return SECTORS[key].label if key in SECTORS else key


def pretty_scenario(name: str, lang: str = "it") -> str:
    return t(lang, "base_case") if name == "base" else name.replace("_", " ").capitalize()


def scenario_summaries(session: DealSession) -> List[dict]:
    """Agent summaries round IRR/MOIC for the model; the UI shows exact values (as Excel does)."""
    out = []
    for name, sc in session.scenarios.items():
        exact = run_model(sc.assumptions)["returns"]
        out.append(session._summary(name, sc.assumptions) | {
            "overrides": sc.overrides, "rationale": sc.rationale, "irr": exact["irr"], "moic": exact["moic"]})
    return out


def kpis(session: DealSession, lang: str = "it") -> List[dict]:
    s = scenario_summaries(session)[0]
    irr = s["irr"]
    return [
        {"label": t(lang, "k_ev"), "value": money(s["entry_ev"], lang), "hint": t(lang, "k_ev_h")},
        {"label": t(lang, "k_eq"), "value": money(s["entry_equity"], lang), "hint": t(lang, "k_eq_h")},
        {"label": "MOIC", "value": mult(s["moic"], lang), "hint": t(lang, "k_moic_h")},
        {"label": "IRR", "value": pct(irr, lang), "hint": t(lang, "k_irr_h"),
         "tone": "positive" if irr >= 0.20 else "warning" if irr >= 0.12 else "negative"},
    ]


def table_rows(session: DealSession, lang: str = "it") -> List[dict]:
    labels = FIELD_LABELS[lang]
    rows = []
    for s in scenario_summaries(session):
        changes = "—" if s["scenario"] == "base" else ", ".join(
            f"{field_label(k, lang)} {fmt_value(k, v, lang)}" for k, v in s["overrides"].items()
            if k not in ("entry_ebitda", "senior_leverage_x")) or "—"
        rows.append({
            "id": s["scenario"], "scenario": pretty_scenario(s["scenario"], lang), "changes": changes,
            "moic": mult(s["moic"], lang), "irr": pct(s["irr"], lang), "ev": money(s["entry_ev"], lang),
            "leverage": mult(s["entry_total_leverage_x"], lang),
            "flags": "; ".join(translate_warning(w, lang) for w in s["engine_warnings"]) or "—",
            "irr_value": s["irr"],
        })
    return rows


def table_columns(lang: str = "it") -> List[dict]:
    return [
        {"name": "scenario", "label": t(lang, "c_scenario"), "field": "scenario", "align": "left"},
        {"name": "changes", "label": t(lang, "c_changes"), "field": "changes", "align": "left"},
        {"name": "ev", "label": t(lang, "c_ev"), "field": "ev"},
        {"name": "leverage", "label": t(lang, "c_lev"), "field": "leverage"},
        {"name": "moic", "label": "MOIC", "field": "moic"},
        {"name": "irr", "label": "IRR", "field": "irr"},
        {"name": "flags", "label": t(lang, "c_flags"), "field": "flags", "align": "left"},
    ]


def irr_chart(session: DealSession, lang: str = "it") -> dict:
    rows = table_rows(session, lang)
    return {
        "title": {"text": t(lang, "ch_irr"), "textStyle": {"fontSize": 14}},
        "tooltip": {"trigger": "axis"},
        "grid": {"left": 50, "right": 20, "bottom": 70},
        "xAxis": {"type": "category", "data": [r["scenario"] for r in rows],
                  "axisLabel": {"interval": 0, "rotate": 20}},
        "yAxis": {"type": "value", "axisLabel": {"formatter": "{value}%"}},
        "series": [{"type": "bar", "data": [
            {"value": round(r["irr_value"] * 100, 1),
             "itemStyle": {"color": "#2e7d32" if r["irr_value"] >= 0.20 else "#f9a825" if r["irr_value"] >= 0.12
                           else "#c62828"}} for r in rows],
            "markLine": {"silent": True, "symbol": "none", "lineStyle": {"type": "dashed", "color": "#555"},
                         "data": [{"yAxis": 20, "label": {"formatter": t(lang, "ch_target"),
                                                          "position": "insideStartTop"}}]}}],
    }


def debt_chart(session: DealSession, lang: str = "it") -> dict:
    sums = scenario_summaries(session)
    years = max(len(s["net_debt_to_ebitda_by_year"]) for s in sums)
    return {
        "title": {"text": t(lang, "ch_debt"), "textStyle": {"fontSize": 14}, "subtext": t(lang, "ch_debt_sub")},
        "tooltip": {"trigger": "axis"},
        "legend": {"bottom": 0, "type": "scroll"},
        "grid": {"left": 50, "right": 20, "bottom": 60, "top": 70},
        "xAxis": {"type": "category", "data": [f"{t(lang, 'year')} {i}" for i in range(1, years + 1)]},
        "yAxis": {"type": "value", "axisLabel": {"formatter": "{value}x"}},
        "series": [{"type": "line", "name": pretty_scenario(s["scenario"], lang),
                    "data": s["net_debt_to_ebitda_by_year"], "smooth": True} for s in sums],
    }


def _cost_text(usage_log: List[dict], lang: str) -> Optional[str]:
    cost = usage_cost_usd(usage_log)
    if cost is None:
        return None
    return t(lang, "cost_small") if cost < 0.01 else (
        f"circa {num(cost, 2, lang)} $" if lang == "it" else f"about ${num(cost, 2, lang)}")


def cost_label(session: DealSession, lang: str = "it") -> Optional[str]:
    if not getattr(session, "usage_tracked", True):
        return t(lang, "cost_untracked")
    if not session.usage_log:
        return None
    shown = _cost_text(session.usage_log, lang)
    return t(lang, "cost", c=shown) if shown else None


def project_cost(data: dict, lang: str = "it") -> str:
    """Short cost text for the saved-projects list."""
    if "usage" not in data:
        return t(lang, "cost_short_untracked")
    return (_cost_text(data["usage"], lang) if data["usage"] else None) or t(lang, "cost_short_none")


# ---------------------------------------------------------------------------
# Saved projects
# ---------------------------------------------------------------------------

def list_projects(output_dir: Path = OUTPUT_DIR, lang: str = "it") -> List[dict]:
    projects = []
    for path in sorted(Path(output_dir).glob("*_deal.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            base = data["base"]
        except (OSError, ValueError, KeyError):
            continue
        projects.append({
            "path": str(path), "company": base.get("company_name", path.stem),
            "sector": base.get("sector_label", base.get("sector", "")),
            "saved": time.strftime("%d/%m/%Y %H:%M", time.localtime(path.stat().st_mtime)),
            "scenarios": len(data.get("scenarios", [])) + 1,
            "cost": project_cost(data, lang),
        })
    return projects


# ---------------------------------------------------------------------------
# Running the agent in the background
# ---------------------------------------------------------------------------

def friendly_error(exc: Exception, lang: str = "it") -> str:
    import anthropic
    if isinstance(exc, anthropic.AuthenticationError):
        return t(lang, "e_auth")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return t(lang, "e_perm")
    if isinstance(exc, anthropic.BadRequestError) and "credit" in str(exc).lower():
        return t(lang, "e_credit")
    if isinstance(exc, anthropic.RateLimitError):
        return t(lang, "e_rate")
    if isinstance(exc, anthropic.APIConnectionError):
        return t(lang, "e_conn")
    return t(lang, "e_other", e=exc)


@dataclass
class Analysis:
    """One deal in the app: the agent session plus a thread-safe progress log the UI polls.
    Answers and API usage live in the session, so they are saved with the project."""
    session: DealSession
    agent: LBOAgent
    lang: str = "it"
    steps: List[str] = field(default_factory=list)
    running: bool = False
    started_at: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def new(cls, output_dir: Path = OUTPUT_DIR, client=None, llm=None, lang: str = "it") -> "Analysis":
        return cls._wrap(DealSession(output_dir=str(output_dir), llm=llm), client, lang)

    @classmethod
    def open(cls, path: str, output_dir: Path = OUTPUT_DIR, client=None, lang: str = "it") -> "Analysis":
        return cls._wrap(DealSession.load(path, str(output_dir)), client, lang)

    @classmethod
    def _wrap(cls, session: DealSession, client, lang: str) -> "Analysis":
        holder = {}
        agent = LBOAgent(session, client=client, on_tool_call=lambda n, a: holder["self"]._step(n, a))
        analysis = cls(session=session, agent=agent, lang=lang)
        holder["self"] = analysis
        return analysis

    def _step(self, tool: str, args: dict) -> None:
        with self._lock:
            self.steps.append(progress_message(tool, args, self.lang))

    def progress(self) -> List[str]:
        with self._lock:
            return list(self.steps)

    def ask(self, text: str, display_question: Optional[str] = None) -> str:
        """Blocking: run from a worker thread (nicegui.run.io_bound)."""
        with self._lock:
            self.steps, self.running, self.started_at = [], True, time.monotonic()
        try:
            answer = self.agent.ask(text, display_question=display_question)
            if self.session.generation:
                self.session.save()
            return answer
        finally:
            self.running = False

    @property
    def history(self) -> List[dict]:
        return self.session.history

    @property
    def has_results(self) -> bool:
        return "base" in self.session.scenarios

    @property
    def excel_files(self) -> List[str]:
        paths = list(self.session.exports)
        # Reopened project: the exports list is empty, but the workbooks are still on disk.
        stem = self.session.company_stem() if self.session.generation else None
        if stem:
            paths += [str(p) for p in sorted(self.session.output_dir.glob(f"{stem}_*.xlsx"))]
        return list(dict.fromkeys(p for p in paths if Path(p).exists()))


# ---------------------------------------------------------------------------
# Glossary
# ---------------------------------------------------------------------------

GLOSSARY: Dict[str, Dict[str, str]] = {
    "it": {
        "LBO (leveraged buyout)": "Acquisto di un'azienda pagato in gran parte con debito. Il debito viene poi "
                                  "ripagato con la cassa che l'azienda stessa produce.",
        "EBITDA": "Il profitto operativo prima di interessi, tasse e ammortamenti. È la misura di quanto "
                  "l'azienda 'rende' con la sua attività: su questo si calcolano prezzo e debito.",
        "Valore d'impresa (EV) e multiplo": "Il prezzo dell'intera azienda. Si esprime come multiplo dell'EBITDA: "
                                             "8x vuol dire pagare otto anni di EBITDA.",
        "Debito (leva) x EBITDA": "Quanto debito si usa, in anni di EBITDA. 4,5x è un livello tipico; oltre 6x "
                                  "diventa rischioso.",
        "Equity": "La parte del prezzo che il fondo paga con soldi propri (il resto è debito).",
        "MOIC": "Quante volte il fondo moltiplica i soldi investiti. 2x = raddoppia, 1x = riprende solo quanto ha messo.",
        "IRR": "Il rendimento annuo dell'investimento. I fondi di private equity di solito cercano almeno il 20%.",
        "Caso base": "Lo scenario con le ipotesi più probabili, senza ottimismi.",
        "Downside post-closing": "Cosa succede se, dopo aver comprato (prezzo e debito già fissati), l'azienda va "
                                 "peggio del previsto.",
        "Cash sweep": "L'uso della cassa in eccesso per ripagare il debito in anticipo.",
        "RCF (linea di credito)": "Una linea di credito di scorta, usata solo se in un anno la cassa non basta.",
        "Copertura degli interessi": "Quante volte l'EBITDA copre gli interessi da pagare. Sotto 2 volte è un "
                                     "campanello d'allarme.",
        "Guardrail": "Controlli automatici che riportano le stime dell'AI dentro i valori tipici del settore e "
                     "segnalano i dati fuori norma.",
    },
    "en": {
        "LBO (leveraged buyout)": "Buying a company mostly with borrowed money. The debt is then repaid with the "
                                  "cash the company itself generates.",
        "EBITDA": "Operating profit before interest, tax, depreciation and amortisation: how much the business "
                  "earns from its activity. Price and debt are set as multiples of it.",
        "Enterprise value (EV) and multiple": "The price of the whole company, expressed as a multiple of EBITDA: "
                                              "8x means paying eight years of EBITDA.",
        "Debt (leverage) x EBITDA": "How much debt is used, in years of EBITDA. 4.5x is typical; above 6x it "
                                    "gets risky.",
        "Equity": "The part of the price the fund pays with its own money (the rest is debt).",
        "MOIC": "How many times the fund multiplies the money invested. 2x = doubles it, 1x = only gets it back.",
        "IRR": "The annual return on the investment. Private equity funds usually look for at least 20%.",
        "Base case": "The scenario with the most likely assumptions, without optimism.",
        "Post-closing downside": "What happens if, after buying (price and debt already fixed), the company does "
                                 "worse than planned.",
        "Cash sweep": "Using surplus cash to repay debt early.",
        "RCF (credit line)": "A back-up credit line, used only if cash falls short in a year.",
        "Interest coverage": "How many times EBITDA covers the interest due. Below 2 times is a warning sign.",
        "Guardrail": "Automatic checks that bring the AI's estimates back within the sector's usual values and "
                     "flag unusual figures.",
    },
}
