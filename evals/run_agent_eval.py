"""
Eval runner for the LBO agent (evals/agent_cases.json).

Each case goes through the real entry point (DealSession + LBOAgent, same model,
tools, prompts and guardrails as `lbo_agent.py`) and is graded with programmatic
checks on what the agent actually did: session state (base case, scenarios,
exports) plus the tool calls, and a grounding check on the numbers in its answer.

Layout (report-builder compatible), under .claude/hillclimb/lbo_agent/<variant>/:
  results.jsonl          one row per (case, rep), written as each case finishes
  traces/<id>_rep<k>.json  full conversation (user / assistant / tool_call / tool_result)
  errors.jsonl           failed attempts (timeout, API error, model mismatch): never scored
  out/<id>_rep<k>/       Excel files and deal file the agent wrote

Safety rails: --budget-usd stops before a case that could exceed the budget; resume
skips (case, rep) pairs already in results.jsonl; a harness-integrity gate refuses to
run if the agent code changed since the user last approved it (--approve-harness).

Usage:
    uv run python evals/run_agent_eval.py --only 01_valvole,02_saas --budget-usd 1.5 --approve-harness
    uv run python evals/run_agent_eval.py --budget-usd 4 --summary
"""

import argparse
import concurrent.futures
import hashlib
import json
import math
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from assumption_generator import DEFAULT_MODEL  # noqa: E402
from lbo_agent import SYSTEM_PROMPT, DealSession, LBOAgent, usage_cost_usd  # noqa: E402

FLOW_DIR = ROOT / ".claude" / "hillclimb" / "lbo_agent"
CASES_FILE = ROOT / "evals" / "agent_cases.json"
HARNESS_PATHS = ["evals/run_agent_eval.py", "evals/agent_cases.json", "lbo_agent.py",
                 "assumption_generator.py", "lbo_engine.py", "excel_export.py", "sector_benchmarks.py",
                 "data/sector_benchmarks.json"]
CHECKS = ["sector_ok", "provided_ok", "tools_ok", "scenarios_ok", "plan_ok", "covenant_ok", "guardrail_ok",
          "export_ok", "language_ok"]
# Text fields written by our code (not by the model): their figures count as tool output.
CODE_TEXT_KEYS = {"guardrail_warnings", "guardrail_adjustments", "engine_warnings", "benchmark_notes",
                  "entry_terms", "note"}
STOPWORDS = {
    "it": {"il", "la", "di", "e", "della", "che", "non", "per", "con", "una", "sono", "del", "nel", "sul", "è"},
    "en": {"the", "and", "of", "to", "is", "with", "for", "that", "are", "in", "at", "we"},
    "de": {"der", "die", "und", "das", "mit", "für", "nicht", "ist", "den", "auf", "bei", "wir"},
}
OPERATING_FIELDS = {"ebitda_margin", "revenue_growth", "capex_pct_revenue", "nwc_pct_of_rev_growth",
                    "exit_ev_multiple", "revenue_growth_by_year", "ebitda_margin_by_year",
                    "capex_pct_revenue_by_year"}


# ---------------------------------------------------------------------------
# Running one case
# ---------------------------------------------------------------------------

def _block(b, key, default=None):
    return b.get(key, default) if isinstance(b, dict) else getattr(b, key, default)


def build_trace(messages: List[dict]) -> List[dict]:
    """Agent history -> [{role, content, name?, thinking?}] in the report-viewer shape."""
    trace = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            trace.append({"role": m["role"], "content": content})
            continue
        pending_thinking = None
        for b in content:
            kind = _block(b, "type")
            if kind == "thinking":
                pending_thinking = (_block(b, "thinking") or "") or pending_thinking
            elif kind == "text":
                turn = {"role": "assistant", "content": _block(b, "text")}
                if pending_thinking:
                    turn["thinking"], pending_thinking = pending_thinking, None
                trace.append(turn)
            elif kind == "tool_use":
                turn = {"role": "tool_call", "name": _block(b, "name"),
                        "content": json.dumps(_block(b, "input"), indent=2, ensure_ascii=False)}
                if pending_thinking:
                    turn["thinking"], pending_thinking = pending_thinking, None
                trace.append(turn)
            elif kind == "tool_result":
                result = _block(b, "content")
                if not isinstance(result, str):
                    result = json.dumps(result, ensure_ascii=False)
                trace.append({"role": "tool_result", "content": result,
                              **({"is_error": True} if _block(b, "is_error") else {})})
    return trace


def tool_calls_from_trace(trace: List[dict]) -> List[dict]:
    calls, pending = [], []
    for t in trace:
        if t["role"] == "tool_call":
            pending.append({"name": t["name"], "input": json.loads(t["content"])})
        elif t["role"] == "tool_result" and pending:
            call = pending.pop(0)
            call["result"], call["is_error"] = t["content"], t.get("is_error", False)
            calls.append(call)
    return calls


def run_case(case: dict, out_dir: Path, *, model: str, client=None, llm=None) -> dict:
    session = DealSession(output_dir=str(out_dir), model=model, llm=llm)
    agent = LBOAgent(session, model=model, client=client)
    answers = [agent.ask(turn) for turn in case["turns"]]
    return {"session": session, "agent": agent, "answers": answers, "trace": build_trace(agent.messages)}


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------

def _close(a, b, rel=1e-6):
    return a is not None and b is not None and abs(a - b) <= rel * max(1.0, abs(a), abs(b))


_NUM = re.compile(r"(?<![\w/])[-−]?\d+(?:[.,]\d+)?")


def _numbers_in(value, strings: bool = True) -> List[float]:
    """Numbers inside a JSON-like value. strings=False keeps only numeric fields (plus the
    code-written texts in CODE_TEXT_KEYS), so figures quoted inside model-written prose
    (rationales) do not count as tool-computed values."""
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, dict):
        return [n for k, v in value.items()
                for n in _numbers_in(v, strings or k in CODE_TEXT_KEYS)]
    if isinstance(value, list):
        return [n for v in value for n in _numbers_in(v, strings)]
    if isinstance(value, str) and strings:
        return [float(m.replace(",", ".").replace("−", "-")) for m in _NUM.findall(value)]
    return []


def grounding(answer: str, references: List[float]) -> Dict:
    """Share of the numbers in the answer that match a tool result, a tool argument or the
    request, up to the rounding shown (10,3% matches 0.10254). Small integers (counts,
    years of hold, list numbering) and calendar years are ignored."""
    text = re.sub(r"`[^`]*`", " ", answer)                 # file paths, code
    text = re.sub(r"\b(?:Y|Year|Anno)\s?\d+\b", " ", text)  # Y1, Year 5
    refs = [abs(r) for r in references] + [abs(r) * 100 for r in references]
    checked, missing = 0, []
    for m in _NUM.finditer(text):
        raw = m.group(0)
        n = abs(float(raw.replace(",", ".").replace("−", "-")))
        decimals = len(re.split(r"[.,]", raw)[1]) if re.search(r"[.,]", raw) else 0
        if decimals == 0 and (n <= 10 or 1990 <= n <= 2100):
            continue
        checked += 1
        tol = 0.5 * 10 ** (-decimals) + 1e-9
        if not any(abs(n - r) <= tol for r in refs):
            missing.append(text[max(0, m.start() - 30): m.end() + 10].replace("\n", " ").strip())
    return {"score": 1.0 if checked == 0 else (checked - len(missing)) / checked,
            "checked": checked, "ungrounded": missing}


def detect_language(text: str) -> Optional[str]:
    words = re.findall(r"[a-zàèéìòùäöüß]+", text.lower())
    counts = {lang: sum(w in sw for w in words) for lang, sw in STOPWORDS.items()}
    best = max(counts, key=counts.get)
    return best if counts[best] else None


def grade(case: dict, session: DealSession, calls: List[dict], answers: List[str]) -> Dict:
    exp = case["expect"]
    g, why = {}, {}
    base_sc = session.scenarios.get("base")
    base = base_sc.assumptions if base_sc else None
    gen = session.generation

    g["sector_ok"] = float(bool(gen) and gen.sector in exp["sector"])
    why["sector_ok"] = f"sector={gen.sector if gen else None}, expected one of {exp['sector']}"

    problems = []
    if not base:
        problems.append("no base case")
    else:
        for field, value in exp.get("provided", {}).items():
            got, src = getattr(base, field), gen.trace[field].source
            if not _close(got, value) or src not in ("provided", "derived"):
                problems.append(f"{field}: {got:g} ({src}) vs {value:g}")
        if "currency" in exp and gen.currency != exp["currency"]:
            problems.append(f"currency {gen.currency} vs {exp['currency']}")
        if "entry_ebitda_used" in exp and not any(
                _close(sc.assumptions.entry_ebitda, exp["entry_ebitda_used"]) for sc in session.scenarios.values()):
            problems.append(f"no scenario with entry EBITDA {exp['entry_ebitda_used']}")
    g["provided_ok"] = float(not problems)
    why["provided_ok"] = "; ".join(problems) or "all given figures kept"

    if exp.get("tool_calls"):
        missing = []
        for want in exp["tool_calls"]:
            if not any(c["name"] == want["tool"] and not c["is_error"] and all(
                    (_close(c["input"].get(k), v) if isinstance(v, (int, float)) else c["input"].get(k) == v)
                    for k, v in want["args"].items()) for c in calls):
                missing.append(f"{want['tool']}{want['args']}")
        g["tools_ok"] = float(not missing)
        why["tools_ok"] = "missing: " + "; ".join(missing) if missing else "expected calls made"
    else:
        g["tools_ok"], why["tools_ok"] = None, "n/a"

    others = [sc for n, sc in session.scenarios.items() if n != "base"]
    scen_checks = []
    for want in exp.get("scenario_overrides", []):
        fields = {k: v for k, v in want.items() if k != "reprice_entry"}
        ok = any(all(_close(getattr(sc.assumptions, k), v) for k, v in fields.items())
                 and (want.get("reprice_entry") is not False or _close(sc.assumptions.entry_ebitda, base.entry_ebitda))
                 for sc in others) if base else False
        scen_checks.append((f"scenario with {want}", ok))
    if exp.get("post_close_downside"):
        base_moic = session._summary("base", base)["moic"] if base else None
        ok = base is not None and any(
            OPERATING_FIELDS & set(sc.overrides)
            and _close(sc.assumptions.entry_ebitda, base.entry_ebitda)
            and _close(sc.assumptions.entry_ev_multiple, base.entry_ev_multiple)
            and _close(sc.assumptions.total_leverage_x, base.total_leverage_x)
            and session._summary(sc.name, sc.assumptions)["moic"] < base_moic
            for sc in others)
        scen_checks.append(("post-closing downside (same price and debt, lower MOIC)", ok))
    if "min_scenarios" in exp:
        scen_checks.append((f">= {exp['min_scenarios']} scenarios (got {len(others)})", len(others) >= exp["min_scenarios"]))
    if scen_checks:
        g["scenarios_ok"] = float(all(ok for _, ok in scen_checks))
        why["scenarios_ok"] = "; ".join(f"{'OK' if ok else 'FAIL'} {name}" for name, ok in scen_checks)
    else:
        g["scenarios_ok"], why["scenarios_ok"] = None, "n/a"

    plan_checks = []
    if "plan_shape" in exp and base is not None:
        want = exp["plan_shape"]
        seq = getattr(base, want["field"]) or ()
        ok = len(seq) >= 2 and (not want.get("increasing") or all(b >= a for a, b in zip(seq, seq[1:]))
                                and seq[-1] > seq[0])
        plan_checks.append((f"base {want['field']} = {list(seq)} (increasing profile expected)", ok))
    for want in exp.get("scenario_plan", []):
        ok = any((getattr(sc.assumptions, want["field"]) or ()) and
                 _close(getattr(sc.assumptions, want["field"])[-1], want["last"], rel=1e-3) for sc in others)
        plan_checks.append((f"scenario with {want['field']} ending at {want['last']}", ok))
    if plan_checks:
        g["plan_ok"] = float(all(ok for _, ok in plan_checks))
        why["plan_ok"] = "; ".join(f"{'OK' if ok else 'FAIL'} {name}" for name, ok in plan_checks)
    else:
        g["plan_ok"], why["plan_ok"] = None, "n/a"

    if "covenant_steps" in exp:
        want = exp["covenant_steps"]
        found = [sc.name for sc in session.scenarios.values()
                 if [sc.assumptions.plan_value("max_net_leverage", t) for t in range(1, len(want) + 1)] == want]
        tested = base is not None and base.has_covenants
        g["covenant_ok"] = float(bool(found) and tested)
        why["covenant_ok"] = (f"step-downs {want} in {found}" if found else f"no scenario with step-downs {want}") + \
            ("" if tested else "; base case has no covenants")
    else:
        g["covenant_ok"], why["covenant_ok"] = None, "n/a"

    if exp.get("guardrail_warning"):
        g["guardrail_ok"] = float(bool(gen and gen.warnings))
        why["guardrail_ok"] = "; ".join(gen.warnings) if gen and gen.warnings else "no guardrail warning raised"
    else:
        g["guardrail_ok"], why["guardrail_ok"] = None, "n/a"

    exported = [p for p in session.exports if Path(p).exists()]
    g["export_ok"] = float(any(p.endswith("_base.xlsx") for p in exported) or
                           any(c["name"] == "export_excel" and c["input"].get("scenario", "base") == "base"
                               and not c["is_error"] for c in calls))
    why["export_ok"] = ", ".join(Path(p).name for p in exported) or "no Excel exported"

    references = _numbers_in(case["turns"])                   # what the user said
    for c in calls:
        references += _numbers_in(c["input"], strings=False)   # what the agent chose to run
        try:
            references += _numbers_in(json.loads(c["result"]), strings=False)  # what the tools computed
        except (json.JSONDecodeError, TypeError):
            pass                                                # error text: not a source of figures
    gr = grounding("\n".join(answers), references)
    g["grounding"] = round(gr["score"], 4)
    why["grounding"] = (f"{gr['checked'] - len(gr['ungrounded'])}/{gr['checked']} numbers traceable"
                        + ("; not traceable: " + " | ".join(gr["ungrounded"]) if gr["ungrounded"] else ""))

    want_lang = next((t for t in case["tags"] if t in STOPWORDS), None)
    got_lang = detect_language("\n".join(answers))
    g["language_ok"] = float(got_lang == want_lang) if want_lang and got_lang else None
    why["language_ok"] = f"answer looks {got_lang}, request is {want_lang}"

    applicable = [g[k] for k in CHECKS if g[k] is not None]
    g = {"all_checks": float(all(v == 1.0 for v in applicable)), **g}
    return {"grade": g, "explanation": why}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def harness_sha() -> str:
    h = hashlib.sha256()
    for rel in HARNESS_PATHS:
        h.update(rel.encode())
        if (ROOT / rel).exists():
            h.update((ROOT / rel).read_bytes())
    return h.hexdigest()


def check_harness(approve: bool) -> None:
    approval = FLOW_DIR / "harness_approval.json"
    sha = harness_sha()
    if approve:
        FLOW_DIR.mkdir(parents=True, exist_ok=True)
        approval.write_text(json.dumps({"sha": sha, "paths": HARNESS_PATHS, "approved_at": time.ctime()}, indent=2))
        return
    if not approval.exists() or json.loads(approval.read_text())["sha"] != sha:
        sys.exit("The agent or the eval changed since the last approved run. Review the change, then rerun "
                 "with --approve-harness to accept it.")


def _jsonl(path: Path) -> List[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def _usage_totals(usage_log: List[dict]) -> dict:
    keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    return {k: sum(u[k] for u in usage_log) for k in keys}


def run(args, client=None, llm=None) -> List[dict]:
    variant_dir = FLOW_DIR / args.variant if not args.flow_dir else Path(args.flow_dir) / args.variant
    (variant_dir / "traces").mkdir(parents=True, exist_ok=True)
    results_path, errors_path = variant_dir / "results.jsonl", variant_dir / "errors.jsonl"
    cases = json.loads(Path(args.cases).read_text())
    if args.only:
        wanted = args.only.split(",")
        cases = [c for c in cases if c["id"] in wanted]
    if args.limit:
        cases = cases[:args.limit]

    done = {(r["prompt_id"], r["rep"]) for r in _jsonl(results_path)}
    spent = sum(r["meta"].get("cost_usd") or 0 for r in _jsonl(results_path)) + \
        sum(e.get("cost_usd") or 0 for e in _jsonl(errors_path))
    per_case = [r["meta"]["cost_usd"] for r in _jsonl(results_path) if r["meta"].get("cost_usd")]
    new_rows = []

    for case in cases:
        for rep in range(args.reps):
            if (case["id"], rep) in done:
                continue
            estimate = max(per_case) if per_case else args.first_case_estimate
            if args.budget_usd is not None and spent + estimate > args.budget_usd:
                print(f"Budget stop before {case['id']}: spent ${spent:.2f}, next case up to ~${estimate:.2f}, "
                      f"budget ${args.budget_usd:.2f}", file=sys.stderr)
                return new_rows
            print(f"[{case['id']} rep {rep}] running...", file=sys.stderr, flush=True)
            out_dir = variant_dir / "out" / f"{case['id']}_rep{rep}"
            started = time.monotonic()   # on macOS excludes time asleep
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = pool.submit(run_case, case, out_dir, model=args.model, client=client, llm=llm)
            error = None
            try:
                outcome = future.result(timeout=args.timeout_s)
            except concurrent.futures.TimeoutError:
                error, outcome = ("timeout", f"no answer within {args.timeout_s}s"), None
            except Exception as exc:  # API errors, refusals, truncation: recorded, never scored
                error, outcome = (type(exc).__name__, str(exc)[:500]), None
            pool.shutdown(wait=False)
            latency = round(time.monotonic() - started, 1)

            if outcome:
                session = outcome["session"]
                served = {u["model"] for u in session.usage_log}
                if served - {args.model}:
                    error = ("model_mismatch", f"requested {args.model}, served {sorted(served)}")
            if error:
                usage_log = outcome["session"].usage_log if outcome else []
                cost = usage_cost_usd(usage_log) if usage_log else None
                spent += cost or 0
                with errors_path.open("a") as f:
                    f.write(json.dumps({"prompt_id": case["id"], "rep": rep, "failure_class": error[0],
                                        "detail": error[1], "latency_s": latency, "cost_usd": cost,
                                        "usage": _usage_totals(usage_log) if usage_log else None}) + "\n")
                print(f"  ERROR {error[0]}: {error[1][:200]}", file=sys.stderr)
                continue

            session, trace = outcome["session"], outcome["trace"]
            (variant_dir / "traces" / f"{case['id']}_rep{rep}.json").write_text(
                json.dumps(trace, indent=2, ensure_ascii=False))
            calls = tool_calls_from_trace(trace)
            graded = grade(case, session, calls, outcome["answers"])
            cost = usage_cost_usd(session.usage_log)
            spent += cost or 0
            if cost:
                per_case.append(cost)
            row = {
                "prompt_id": case["id"], "prompt": "\n---\n".join(case["turns"]), "tags": case["tags"],
                "rep": rep, "status": "ok", "stop_reason": "end_turn", "model": args.model,
                **graded, "latency_s": latency, "tool_calls": len(calls),
                "usage": _usage_totals(session.usage_log),
                "meta": {"cost_usd": cost, "api_calls": len(session.usage_log),
                         "answer": "\n\n---\n\n".join(outcome["answers"]),
                         "scenarios": {n: sc.overrides for n, sc in session.scenarios.items()},
                         "exports": session.exports},
            }
            with results_path.open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            new_rows.append(row)
            g = row["grade"]
            print(f"  all_checks={g['all_checks']:.0f} grounding={g['grounding']:.2f} "
                  f"cost=${cost or 0:.2f} time={latency}s calls={len(calls)}", file=sys.stderr)
    return new_rows


def wilson_interval(k: float, n: int, z: float = 1.96):
    """95% interval for a pass rate that stays honest at small n (2/2 is not '100% ± 0%')."""
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def summary(variant_dir: Path) -> str:
    rows, errors = _jsonl(variant_dir / "results.jsonl"), _jsonl(variant_dir / "errors.jsonl")
    if not rows and not errors:
        return "No results yet."
    cols = ["all_checks"] + CHECKS + ["grounding"]
    rows = [{**r, "grade": {c: r["grade"].get(c) for c in cols}} for r in rows]   # older rows lack new checks
    lines = ["| case | " + " | ".join(cols) + " | cost $ | time s |",
             "|" + "---|" * (len(cols) + 3)]
    fmt = lambda v: "n/a" if v is None else ("✓" if v == 1.0 else "✗") if v in (0.0, 1.0) else f"{v:.2f}"
    for r in rows:
        lines.append(f"| {r['prompt_id']} | " + " | ".join(fmt(r["grade"][c]) for c in cols)
                     + f" | {r['meta']['cost_usd'] or 0:.2f} | {r['latency_s']:.0f} |")
    n = len(rows)
    done = {(r["prompt_id"], r["rep"]) for r in rows}
    open_errors = [e for e in errors if (e["prompt_id"], e["rep"]) not in done]
    if n:
        passed = sum(r["grade"]["all_checks"] for r in rows)
        lo, hi = wilson_interval(passed, n)
        cost = sum(r["meta"]["cost_usd"] or 0 for r in rows) + sum(e.get("cost_usd") or 0 for e in errors)
        lines += ["", f"all_checks: {passed:.0f}/{n} ({passed / n:.0%}; 95% CI {lo:.0%}-{hi:.0%}) · "
                      f"mean grounding {sum(r['grade']['grounding'] for r in rows) / n:.2f} · "
                      f"open errors {len(open_errors)} (+{len(errors) - len(open_errors)} retried OK) · "
                      f"total cost ${cost:.2f}"]
    for e in open_errors:
        lines.append(f"ERROR {e['prompt_id']} rep {e['rep']}: {e['failure_class']} — {e['detail'][:150]}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run the LBO agent eval.")
    ap.add_argument("--cases", default=str(CASES_FILE))
    ap.add_argument("--variant", default="baseline")
    ap.add_argument("--flow-dir", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--only", help="Comma-separated case ids")
    ap.add_argument("--limit", type=int, help="Run only the first N cases (priority order)")
    ap.add_argument("--budget-usd", type=float, help="Stop before a case that could push spend past this")
    ap.add_argument("--first-case-estimate", type=float, default=1.0,
                    help="Assumed cost of a case before any has been measured (USD)")
    ap.add_argument("--timeout-s", type=int, default=900)
    ap.add_argument("--approve-harness", action="store_true")
    ap.add_argument("--summary", action="store_true", help="Print the results table (no API calls if all done)")
    args = ap.parse_args(argv)
    if args.variant != "baseline" and not re.fullmatch(r"v\d+", args.variant):
        ap.error("variant must be 'baseline' or v<N>")

    check_harness(args.approve_harness)
    run(args)
    if args.summary:
        print(summary((Path(args.flow_dir) if args.flow_dir else FLOW_DIR) / args.variant))


if __name__ == "__main__":
    main()
