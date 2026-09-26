"""
LBO Agent — desktop app (personal prototype, macOS).

A window around the agent for someone who knows neither code nor LBOs: a guided
form, progress in plain words, results as cards / table / charts, the agent's answers
(saved with the project), the Excel files one click away, saved projects, a glossary,
Italian or English, and the API key kept in the Keychain.

    uv run python app.py              # desktop window
    uv run python app.py --browser    # same app in the browser (development)
    packaging/build_app.sh            # standalone "LBO Agent.app" (see packaging/README.md)
"""

import argparse
import multiprocessing
import os
import subprocess
import sys
import time

import faulthandler
import signal

if sys.stdout is None or sys.stderr is None:     # packaged app without a console: log to nowhere
    sys.stdout = sys.stdout or open(os.devnull, "w")
    sys.stderr = sys.stderr or open(os.devnull, "w")

from nicegui import run, ui

import app_logic as L

# Injection points for tests / demos: a fake Anthropic client and a fake proposal step.
CLIENT_FACTORY = None
LLM = None

TONE = {"positive": "text-green-8", "warning": "text-orange-9", "negative": "text-red-8"}


def main_page():
    # LBO_APP_LANGUAGE / LBO_APP_OPEN_PROJECT: demo and screenshot hooks (the saved preference is untouched).
    lang = os.environ.get("LBO_APP_LANGUAGE") or L.get_language()
    T = lambda key, **kw: L.t(lang, key, **kw)
    state = {"analysis": None}

    ui.query("body").style("background-color: #f5f6f8")
    ui.add_css(".answer h1 {font-size: 1.4rem; margin: 0.8rem 0 0.4rem} "
               ".answer h2 {font-size: 1.2rem; margin: 0.8rem 0 0.4rem} "
               ".answer h3 {font-size: 1.05rem} .answer table {border-collapse: collapse; margin: 0.5rem 0} "
               ".answer th, .answer td {border: 1px solid #ddd; padding: 4px 8px}")

    with ui.header().classes("items-center bg-blue-10 q-px-lg"):
        ui.icon("insights", size="md")
        ui.label("LBO Agent").classes("text-h6")
        ui.space()
        ui.label(T("app_disclaimer")).classes("text-caption text-blue-2")

    with ui.tabs().classes("w-full bg-white text-blue-10") as tabs:
        tab_analysis = ui.tab(T("tab_analysis"), icon="query_stats")
        tab_projects = ui.tab(T("tab_projects"), icon="folder_open")
        tab_glossary = ui.tab(T("tab_glossary"), icon="menu_book")
        tab_settings = ui.tab(T("tab_settings"), icon="settings")

    def client():
        return CLIENT_FACTORY() if CLIENT_FACTORY else None

    # ---------------------------------------------------------------- analysis
    async def run_question(text: str, display: str):
        analysis = state["analysis"]
        start_button.disable()
        progress_card.set_visibility(True)
        progress_view.refresh()
        try:
            await run.io_bound(analysis.ask, text, display)
        except Exception as exc:  # shown in plain words, never a traceback
            ui.notify(L.friendly_error(exc, lang), type="negative", multi_line=True, close_button="OK", timeout=0)
        finally:
            progress_card.set_visibility(False)
            start_button.enable()
            results_view.refresh()
            projects_view.refresh()

    async def start_new():
        key = L.get_api_key()
        if not key:
            ui.notify(T("save_key_first"), type="warning")
            tabs.set_value(tab_settings)
            return
        L.activate_key(key)
        try:
            text = L.build_request(description.value or "", revenue.value, ebitda.value, asking.value,
                                   downside.value, irr_target.value if want_irr.value else None,
                                   moic_target.value if want_moic.value else None, other.value or "", lang=lang)
        except ValueError as exc:
            ui.notify(str(exc), type="warning")
            return
        state["analysis"] = L.Analysis.new(client=client(), llm=LLM, lang=lang)
        form.value = False
        results_view.refresh()
        short = (description.value or "").strip()
        await run_question(text, T("first_question", d=short[:90] + ("…" if len(short) > 90 else "")))

    async def ask_follow_up(text: str):
        if not text.strip():
            return
        key = L.get_api_key()
        if not key:
            ui.notify(T("save_key_first"), type="warning")
            return
        L.activate_key(key)
        await run_question(f"{text.strip()} {T('plain_follow')}", text.strip())

    @ui.refreshable
    def key_banner():
        if not L.get_api_key():
            with ui.card().classes("w-full bg-orange-1"):
                with ui.row().classes("items-center"):
                    ui.icon("vpn_key", color="orange-9")
                    ui.label(T("key_needed"))
                    ui.button(T("go_settings"), on_click=lambda: tabs.set_value(tab_settings)).props("flat")

    @ui.refreshable
    def progress_view():
        analysis = state["analysis"]
        steps = analysis.progress() if analysis else []
        elapsed = int(time.monotonic() - analysis.started_at) if analysis and analysis.running else 0
        with ui.row().classes("items-center"):
            ui.spinner(size="lg")
            ui.label(T("working", s=elapsed)).classes("text-subtitle1")
        if not steps:
            ui.label(T("thinking")).classes("text-grey-8")
        for i, step in enumerate(steps):
            last = i == len(steps) - 1
            with ui.row().classes("items-center no-wrap"):
                ui.icon("autorenew" if last else "check_circle", color="blue-8" if last else "green-7")
                ui.label(step)

    @ui.refreshable
    def results_view():
        analysis = state["analysis"]
        if not analysis or not analysis.has_results:
            return
        session, gen = analysis.session, analysis.session.generation
        ui.separator()
        with ui.row().classes("items-baseline"):
            ui.label(gen.assumptions.company_name).classes("text-h5")
            ui.badge(L.sector_label(gen.sector)).props("outline")

        with ui.row().classes("w-full q-gutter-md"):
            for k in L.kpis(session, lang):
                with ui.card().classes("col"):
                    ui.label(k["label"]).classes("text-caption text-grey-8")
                    ui.label(k["value"]).classes(f"text-h5 {TONE.get(k.get('tone'), '')}")
                    ui.label(k["hint"]).classes("text-caption text-grey-7")

        checks = [L.translate_warning(w, lang) for w in gen.warnings]
        if checks:
            with ui.card().classes("w-full bg-orange-1"):
                ui.label(T("to_check")).classes("text-subtitle2")
                for c in checks:
                    with ui.row().classes("items-start no-wrap"):
                        ui.icon("warning", color="orange-9")
                        ui.label(c)

        ui.label(T("scenarios")).classes("text-h6 q-mt-md")
        ui.table(rows=L.table_rows(session, lang), columns=L.table_columns(lang), row_key="id").classes(
            "w-full").props("wrap-cells flat bordered")
        with ui.row().classes("w-full no-wrap q-gutter-md"):
            with ui.card().classes("col"):
                ui.echart(L.irr_chart(session, lang)).classes("w-full h-80")
            with ui.card().classes("col"):
                ui.echart(L.debt_chart(session, lang)).classes("w-full h-80")

        ui.label(T("explanation")).classes("text-h6 q-mt-md")
        if not analysis.history:
            ui.label(T("loaded_hint")).classes("text-grey-8")
        for i, qa in enumerate(analysis.history):
            latest = i == len(analysis.history) - 1
            with ui.expansion(qa["question"], icon="chat", value=latest).classes("w-full bg-white"):
                ui.markdown(qa["answer"]).classes("q-pa-md answer")

        with ui.card().classes("w-full"):
            ui.label(T("follow_title")).classes("text-subtitle2")
            with ui.row().classes("w-full items-center no-wrap"):
                follow = ui.input(placeholder=T("follow_ph")).classes("col")
                ui.button(T("ask"), icon="send", on_click=lambda: ask_follow_up(follow.value or "")).props(
                    "unelevated")

        with ui.row().classes("items-center q-gutter-sm"):
            for path in analysis.excel_files:
                ui.button(f"{T('open_excel')}: {path.split('/')[-1]}", icon="table_view",
                          on_click=lambda p=path: subprocess.run(["open", p], check=False)).props("outline")
            cost = L.cost_label(session, lang)
            if cost:
                ui.label(cost).classes("text-caption text-grey-7")

    with ui.tab_panels(tabs, value=tab_analysis).classes("w-full bg-transparent"):
        with ui.tab_panel(tab_analysis).classes("q-gutter-md"):
            key_banner()
            form = ui.expansion(T("new_analysis"), icon="add_circle", value=True).classes(
                "w-full bg-white").props("header-class=text-h6")
            with form, ui.column().classes("w-full q-pa-md"):
                ui.label(T("form_intro")).classes("text-grey-8")
                description = ui.textarea(T("description"), placeholder=T("description_ph")).classes(
                    "w-full").props("outlined rows=3")
                with ui.row().classes("w-full q-gutter-md"):
                    revenue = ui.number(T("revenue"), min=0, format="%.1f").classes("w-64").props("outlined clearable")
                    ebitda = ui.number(T("ebitda"), min=0, format="%.1f").classes("w-64").props("outlined clearable")
                    asking = ui.number(T("asking"), min=0, format="%.1f").classes("w-64").props("outlined clearable")
                ui.label(T("what_to_know")).classes("text-subtitle2 q-mt-sm")
                downside = ui.checkbox(T("q_downside"), value=True)
                with ui.row().classes("items-center"):
                    want_irr = ui.checkbox(T("q_irr"), value=True)
                    irr_target = ui.number(value=20, min=1, max=60, suffix="%").classes("w-24").props("dense outlined")
                with ui.row().classes("items-center"):
                    want_moic = ui.checkbox(T("q_moic"))
                    moic_target = ui.number(value=2.5, min=1, max=10, step=0.5, suffix="x").classes("w-24").props(
                        "dense outlined")
                other = ui.textarea(T("other")).classes("w-full").props("outlined rows=2")
                with ui.row().classes("items-center"):
                    start_button = ui.button(T("start"), icon="play_arrow", on_click=start_new).props(
                        "unelevated size=lg")
                    ui.label(T("start_hint")).classes("text-caption text-grey-7")

            progress_card = ui.card().classes("w-full")
            with progress_card:
                progress_view()
            progress_card.set_visibility(False)
            ui.timer(0.7, lambda: progress_view.refresh() if progress_card.visible else None)

            if os.environ.get("LBO_APP_OPEN_PROJECT"):
                state["analysis"] = L.Analysis.open(os.environ["LBO_APP_OPEN_PROJECT"], lang=lang)
                form.value = False
            results_view()

        # ------------------------------------------------------------ projects
        with ui.tab_panel(tab_projects):
            def open_project(path: str):
                try:
                    state["analysis"] = L.Analysis.open(path, client=client(), lang=lang)
                except Exception as exc:
                    ui.notify(T("open_failed", e=exc), type="negative")
                    return
                tabs.set_value(tab_analysis)
                form.value = False
                results_view.refresh()

            @ui.refreshable
            def projects_view():
                projects = L.list_projects(lang=lang)
                if not projects:
                    ui.label(T("no_projects")).classes("text-grey-8")
                for p in projects:
                    with ui.card().classes("w-full"):
                        with ui.row().classes("w-full items-center"):
                            with ui.column().classes("gap-0"):
                                ui.label(p["company"]).classes("text-subtitle1")
                                ui.label(T("project_meta", sector=p["sector"], n=p["scenarios"],
                                           saved=p["saved"], cost=p["cost"])).classes("text-caption text-grey-7")
                            ui.space()
                            ui.button(T("open"), icon="open_in_new",
                                      on_click=lambda path=p["path"]: open_project(path))

            projects_view()

        # ------------------------------------------------------------ glossary
        with ui.tab_panel(tab_glossary):
            ui.label(T("glossary_title")).classes("text-h6")
            for term, text in L.GLOSSARY[lang].items():
                with ui.expansion(term).classes("w-full bg-white"):
                    ui.label(text).classes("q-pa-sm")

        # ------------------------------------------------------------ settings
        with ui.tab_panel(tab_settings).classes("q-gutter-md"):
            def change_language(event):
                if event.value != lang:
                    L.set_language(event.value)
                    ui.navigate.reload()

            with ui.card().classes("w-full"):
                ui.label(T("language_title")).classes("text-h6")
                ui.label(T("language_intro")).classes("text-grey-8")
                ui.toggle(L.LANGUAGES, value=lang, on_change=change_language)

            @ui.refreshable
            def key_status():
                key = L.get_api_key()
                with ui.row().classes("items-center"):
                    ui.icon("check_circle" if key else "cancel", color="green-7" if key else "red-7")
                    ui.label(T("key_label", k=L.mask_key(key, lang)))

            def save_key():
                try:
                    L.save_api_key(key_input.value or "", lang)
                except ValueError as exc:
                    ui.notify(str(exc), type="warning", multi_line=True)
                    return
                key_input.value = ""
                ui.notify(T("key_saved"), type="positive")
                key_status.refresh()
                key_banner.refresh()

            async def verify_key():
                key = L.get_api_key()
                if not key:
                    ui.notify(T("save_key_first"), type="warning")
                    return
                import anthropic
                try:
                    await run.io_bound(lambda: anthropic.Anthropic(api_key=key).models.list(limit=1))
                    ui.notify(T("key_works"), type="positive")
                except Exception as exc:
                    ui.notify(L.friendly_error(exc, lang), type="negative", multi_line=True)

            def delete_key():
                L.delete_api_key()
                ui.notify(T("key_removed"))
                key_status.refresh()
                key_banner.refresh()

            with ui.card().classes("w-full"):
                ui.label(T("key_title")).classes("text-h6")
                ui.label(T("key_intro")).classes("text-grey-8")
                key_status()
                key_input = ui.input(T("key_paste"), password=True).classes("w-full").props("outlined")
                with ui.row():
                    ui.button(T("save"), icon="save", on_click=save_key).props("unelevated")
                    ui.button(T("verify"), icon="verified", on_click=verify_key).props("outline")
                    ui.button(T("remove"), icon="delete", on_click=delete_key).props("flat color=red")
            with ui.card().classes("w-full"):
                ui.label(T("files_title")).classes("text-h6")
                ui.label(T("files_intro", d=L.OUTPUT_DIR)).classes("text-grey-8")
                ui.button(T("open_folder"), icon="folder",
                          on_click=lambda: subprocess.run(["open", str(L.OUTPUT_DIR)], check=False)).props("outline")


def main(argv=None):
    ap = argparse.ArgumentParser(description="LBO Agent desktop app")
    ap.add_argument("--browser", action="store_true", help="Open in the browser instead of a desktop window")
    ap.add_argument("--port", type=int, default=None)
    args, _ = ap.parse_known_args(argv)          # macOS may pass -psn_... to a bundled app
    L.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ui.run(root=main_page, title="LBO Agent", language="it-IT" if L.get_language() == "it" else "en-US",
           native=not args.browser, reload=False, window_size=None if args.browser else (1320, 920),
           port=args.port or (8765 if args.browser else None), show=False, favicon="📈",
           show_welcome_message=False)


if __name__ in {"__main__", "__mp_main__"}:
    multiprocessing.freeze_support()             # required by the native window in a packaged app
    faulthandler.register(signal.SIGUSR1)        # `kill -USR1 <pid>` prints every thread's stack (debugging)
    main()
