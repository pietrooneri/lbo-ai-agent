"""
Excel export — step 3 of the LBO agent.

Writes the LBO as a LIVE workbook: every number on the LBO sheet is an Excel
formula that traces back to the blue input cells on the Assumptions sheet, so
the model can be flexed in Excel without Python. The formulas replicate
lbo_engine.py line by line (tests/test_excel_export.py evaluates the workbook
and checks it against the engine, year by year).

Sheets:
  Assumptions  inputs (blue) with source and rationale from the generator
  LBO          S&U, income statement, FCF, cash waterfall, debt schedule,
               credit stats, checks, returns, IRR sensitivity
  Audit        sector call, guardrail adjustments, warnings, key risks,
               engine snapshot vs live model, modelling conventions

Two deliberate differences from a static dump:
- The model always projects max(10, hold) years; the holding period is an
  input and exit values are picked with INDEX, so changing it in Excel works.
- Entry terms and operating plan are separate inputs: LTM EBITDA sizes the
  price and debt, the projected EBITDA margin drives years 1..N (the LTM
  margin is shown as a memo). Flex the margin to run a post-closing downside.

Usage:
    uv run python excel_export.py -o lbo.xlsx                  # default assumptions
    uv run python excel_export.py --from-json out.json -o lbo.xlsx
"""

import argparse
import dataclasses
import datetime as dt
import json
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Union

from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule, FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from lbo_engine import PLAN_DRIVERS, Assumptions, run_model

# --- styling -----------------------------------------------------------------
FONT = "Arial"
BLUE, BLACK, GREEN, GREY, WHITE, RED = "0000FF", "000000", "008000", "7F7F7F", "FFFFFF", "C00000"
YELLOW_FILL = PatternFill("solid", fgColor="FFFF00")
TITLE_FILL = PatternFill("solid", fgColor="1F3864")
SECTION_FILL = PatternFill("solid", fgColor="D9E1F2")
THIN_TOP = Border(top=Side(style="thin"))

FMT_MM = '#,##0.0;(#,##0.0);"-"'
FMT_PCT = '0.0%;(0.0%);"-"'
FMT_X = '0.00"x";(0.00"x");"-"'
FMT_INT = '0'
FMT_YEAR = '"Year "0;;"Entry"'
FMT_FLAG = '"Yes";;"No"'
FMT_CHECK = '0.000000;0.000000;"OK"'

PURE_LINK = re.compile(r"^=('?[A-Za-z ]+'?)!\$?[A-Z]+\$?\d+$")  # cross-sheet link -> green

ENTRY_COL = 3            # column C = entry / year 0
MIN_PROJECTION_YEARS = 10


def _font(color=BLACK, bold=False, italic=False, size=10):
    return Font(name=FONT, color=color, bold=bold, italic=italic, size=size)


def _put(ws, ref, value, fmt=None, color=None, bold=False, italic=False, fill=None, align=None):
    cell = ws[ref]
    cell.value = value
    if color is None:
        is_formula = isinstance(value, str) and value.startswith("=")
        color = (GREEN if PURE_LINK.match(value) else BLACK) if is_formula else (
            BLUE if isinstance(value, (int, float)) else BLACK)
    cell.font = _font(color, bold, italic)
    if fmt:
        cell.number_format = fmt
    if fill:
        cell.fill = fill
    if align:
        cell.alignment = Alignment(horizontal=align)
    return cell


# --- Assumptions sheet ---------------------------------------------------------

# (field, label, unit, number format, key assumption?)  — None entries are section titles.
ASSUMPTION_LAYOUT = [
    ("Transaction", None, None, None, None),
    ("revenue_at_entry", "LTM revenue at closing", "{cur} mm", FMT_MM, False),
    ("entry_ebitda", "LTM EBITDA at closing (sizes price and debt)", "{cur} mm", FMT_MM, True),
    ("ltm_margin", "LTM EBITDA margin (memo)", "% of revenue", FMT_PCT, False),
    ("entry_ev_multiple", "Entry EV / LTM EBITDA", "x", FMT_X, True),
    ("Operating plan: growth, EBITDA margin and capex year by year on the Plan sheet", None, None, None, None),
    ("da_pct_revenue", "D&A", "% of revenue", FMT_PCT, False),
    ("nwc_pct_of_rev_growth", "Increase in NWC", "% of revenue growth", FMT_PCT, False),
    ("Financing", None, None, None, None),
    ("total_leverage_x", "Total debt at entry", "x EBITDA", FMT_X, True),
    ("senior_leverage_x", "of which Senior Term Loan", "x EBITDA", FMT_X, False),
    ("senior_rate", "Term Loan interest rate", "%", FMT_PCT, False),
    ("senior_mandatory_amort_pct", "Term Loan mandatory amortization", "% of original p.a.", FMT_PCT, False),
    ("cash_sweep_pct", "Cash sweep on Term Loan", "% of surplus cash", FMT_PCT, False),
    ("sub_rate", "Subordinated Notes interest rate (bullet)", "%", FMT_PCT, False),
    ("rcf_commitment", "RCF commitment (undrawn at close)", "{cur} mm", FMT_MM, False),
    ("rcf_rate", "RCF interest rate", "%", FMT_PCT, False),
    ("Tax and cash", None, None, None, None),
    ("tax_rate", "Tax rate (no loss carry-forward)", "%", FMT_PCT, False),
    ("min_cash", "Minimum cash (funded at close, never swept)", "{cur} mm", FMT_MM, False),
    ("Transaction costs (paid at close)", None, None, None, None),
    ("transaction_fees_pct_ev", "M&A fees (advisory, legal, due diligence)", "% of EV", FMT_PCT, False),
    ("financing_fees_pct_debt", "Financing fees (arrangement, underwriting)", "% of funded debt", FMT_PCT, False),
    ("senior_oid_pct", "OID on Term Loan (funded below par, repaid at par)", "% of face value", FMT_PCT, False),
    ("fee_amortization_years", "Amortisation of financing fees + OID (non-cash)", "years", FMT_INT, False),
    ("Exit", None, None, None, None),
    ("hold_period_years", "Holding period", "years", FMT_INT, True),
    ("exit_ev_multiple", "Exit EV / EBITDA", "x", FMT_X, True),
]


def _write_assumptions(ws, a: Assumptions, audit: Optional[dict], cur: str, n_years: int) -> Dict[str, str]:
    ws.sheet_view.showGridLines = False
    for col, width in zip("ABCDE", (46, 13, 20, 12, 110)):
        ws.column_dimensions[col].width = width

    _put(ws, "A1", f"{a.company_name} — LBO assumptions", bold=True, color=WHITE, fill=TITLE_FILL)
    for col in "BCDE":
        ws[f"{col}1"].fill = TITLE_FILL
    _put(ws, "A2", "Legend: blue = input (change freely, the LBO sheet recalculates) · black = formula · "
                   "yellow = key assumption. Source: provided = stated in the description · estimated = "
                   "Claude's estimate · derived = computed from other inputs · adjusted = changed by a guardrail · scenario = changed by the agent for this scenario.",
         italic=True, color=GREY)
    for col, text in zip("ABCDE", ("Assumption", "Value", "Unit", "Source", "Rationale / guardrail notes")):
        _put(ws, f"{col}4", text, bold=True, fill=SECTION_FILL)

    trace = (audit or {}).get("trace", {})
    refs: Dict[str, str] = {}
    row = 5
    rows: Dict[str, int] = {}
    for name, label, unit, fmt, key in ASSUMPTION_LAYOUT:
        if label is None:
            row += 1
            _put(ws, f"A{row}", name, bold=True)
            row += 1
            continue
        rows[name] = row
        refs[name] = f"Assumptions!$B${row}"
        _put(ws, f"A{row}", label)
        _put(ws, f"C{row}", unit.format(cur=cur), color=GREY)
        t = trace.get(name)
        if name == "ltm_margin":
            value = f"=B{rows['entry_ebitda']}/B{rows['revenue_at_entry']}"
        else:
            value = getattr(a, name)
        _put(ws, f"B{row}", value, fmt=fmt, fill=YELLOW_FILL if key else None)
        if t:
            _put(ws, f"D{row}", t["source"])
            note = t["rationale"]
            if t["notes"]:
                note += f"  [Guardrail: {'; '.join(t['notes'])}; Claude proposed {t['llm_value']:g}]"
            _put(ws, f"E{row}", note)
        else:
            _put(ws, f"D{row}", "derived" if name == "ltm_margin" else "input")
        row += 1

    hold = DataValidation(type="whole", operator="between", formula1="1", formula2=str(n_years),
                          showErrorMessage=True, errorTitle="Holding period",
                          error=f"Whole number of years between 1 and {n_years} (projection length).")
    ws.add_data_validation(hold)
    hold.add(f"B{rows['hold_period_years']}")
    ws.freeze_panes = "A5"
    return refs


# --- Plan sheet ----------------------------------------------------------------

PLAN_LABELS = {"revenue_growth": ("Revenue growth", "% vs prior year"),
               "ebitda_margin": ("EBITDA margin", "% of revenue"),
               "capex_pct_revenue": ("Capex", "% of revenue")}
PLAN_FIRST_ROW = 5


def _write_plan(ws, a: Assumptions, audit: Optional[dict], n_years: int) -> Dict[str, int]:
    """One blue input per driver and year, in the same columns as the LBO sheet (D = year 1)."""
    ws.sheet_view.showGridLines = False
    year_cols = [get_column_letter(ENTRY_COL + t) for t in range(1, n_years + 1)]
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 18
    ws.column_dimensions["C"].width = 60
    for col in year_cols:
        ws.column_dimensions[col].width = 10
    _put(ws, "A1", f"{a.company_name} — operating plan", bold=True, color=WHITE, fill=TITLE_FILL)
    for col in ["B", "C"] + year_cols:
        ws[f"{col}1"].fill = TITLE_FILL
    _put(ws, "A2", "One input per year (blue): edit any year and the LBO sheet recalculates. Years after the "
                   "last planned year repeat its value.", italic=True, color=GREY)
    for col, text in (("A", "Driver"), ("B", "Unit"), ("C", "Source / rationale")):
        _put(ws, f"{col}4", text, bold=True, fill=SECTION_FILL)
    for t, col in enumerate(year_cols, start=1):
        _put(ws, f"{col}4", t, fmt=FMT_YEAR, bold=True, fill=SECTION_FILL, color=BLACK, align="right")

    trace = (audit or {}).get("trace", {})
    rows = {}
    for i, driver in enumerate(PLAN_DRIVERS):
        r = PLAN_FIRST_ROW + i
        rows[f"plan_row:{driver}"] = r
        label, unit = PLAN_LABELS[driver]
        _put(ws, f"A{r}", label)
        _put(ws, f"B{r}", unit, color=GREY)
        t = trace.get(f"{driver}_by_year") or trace.get(driver)
        shape = "year by year" if getattr(a, f"{driver}_by_year") else "flat"
        note = f"{t['source']} ({shape}): {t['rationale']}" if t else f"input ({shape})"
        if t and t.get("notes"):
            note += f"  [Guardrail: {'; '.join(t['notes'])}]"
        _put(ws, f"C{r}", note, color=GREY)
        ws[f"C{r}"].alignment = Alignment(wrap_text=True, vertical="top")
        for year, col in enumerate(year_cols, start=1):
            _put(ws, f"{col}{r}", a.plan_value(driver, year), fmt=FMT_PCT,
                 fill=YELLOW_FILL if driver != "capex_pct_revenue" else None)
    ws.freeze_panes = "D5"
    return rows


# --- LBO sheet -----------------------------------------------------------------

Formula = Union[str, float, int, None, Callable[..., str]]


@dataclass
class Line:
    key: Optional[str]
    label: str = ""
    entry: Formula = None           # column C: value or formula (callable gets the row map)
    year: Formula = None            # every projection column: callable(c, p, R) with c/p = this/prior column
    fmt: str = FMT_MM
    bold: bool = False
    memo: bool = False              # grey italic helper / memo line
    kind: str = "line"              # line | section | blank | header
    extra: Dict[str, Callable] = field(default_factory=dict)   # other single cells, e.g. {"D": fn}
    highlight: bool = False


def _lbo_layout(A: Dict[str, str], n_years: int, last: str) -> List[Line]:
    yr = lambda R, key: f"$D${R[key]}:${last}${R[key]}"   # projection-year range of one line
    pct_of_total = lambda R, r: f"=C{r}/$C${R['su_uses']}"
    x_ebitda = lambda R, r: f"=C{r}/$C${R['su_ebitda']}"
    L = Line
    return [
        L(None, "Sources & Uses", kind="header", extra={"C": "{cur} mm", "D": "x EBITDA", "E": "% of total"}),
        L("su_ebitda", "LTM EBITDA at entry", entry=f"={A['entry_ebitda']}"),
        L("su_mult", "Entry EV / EBITDA", entry=f"={A['entry_ev_multiple']}", fmt=FMT_X),
        L("su_ev", "Purchase of enterprise value", entry=lambda R: f"=C{R['su_ebitda']}*C{R['su_mult']}",
          extra={"D": x_ebitda, "E": pct_of_total}),
        L("su_mincash", "Minimum cash funding", entry=f"={A['min_cash']}",
          extra={"D": x_ebitda, "E": pct_of_total}),
        L("su_txfees", "M&A fees", entry=lambda R: f"=C{R['su_ev']}*{A['transaction_fees_pct_ev']}",
          extra={"D": x_ebitda, "E": pct_of_total}),
        L("su_finfees", "Financing fees",
          entry=lambda R: f"=C{R['su_ebitda']}*{A['total_leverage_x']}*{A['financing_fees_pct_debt']}",
          extra={"D": x_ebitda, "E": pct_of_total}),
        L("su_oid", "OID on Term Loan",
          entry=lambda R: f"=C{R['su_ebitda']}*{A['senior_leverage_x']}*{A['senior_oid_pct']}",
          extra={"D": x_ebitda, "E": pct_of_total}),
        L("su_uses", "Total uses", entry=lambda R: f"=SUM(C{R['su_ev']}:C{R['su_oid']})", bold=True,
          extra={"D": x_ebitda, "E": pct_of_total}),
        L(None, kind="blank"),
        L("su_senior", "Senior Term Loan", entry=lambda R: f"=C{R['su_ebitda']}*{A['senior_leverage_x']}",
          extra={"D": x_ebitda, "E": pct_of_total}),
        L("su_sub", "Subordinated Notes",
          entry=lambda R: f"=C{R['su_ebitda']}*({A['total_leverage_x']}-{A['senior_leverage_x']})",
          extra={"D": x_ebitda, "E": pct_of_total}),
        L("su_equity", "Sponsor equity (plug)",
          entry=lambda R: f"=C{R['su_uses']}-C{R['su_senior']}-C{R['su_sub']}",
          extra={"D": x_ebitda, "E": pct_of_total}),
        L("su_sources", "Total sources", entry=lambda R: f"=SUM(C{R['su_senior']}:C{R['su_equity']})", bold=True,
          extra={"D": x_ebitda, "E": pct_of_total}),
        L("su_check", "Check: sources - uses", entry=lambda R: f"=C{R['su_sources']}-C{R['su_uses']}",
          fmt=FMT_CHECK, memo=True),
        L(None, kind="blank"),

        L("years", "{cur} mm", entry=0, year=lambda c, p, R: f"={p}{R['years']}+1", fmt=FMT_YEAR,
          kind="timeline"),
        L("in_hold", "In holding period", year=lambda c, p, R: f"=IF({c}{R['years']}<={A['hold_period_years']},1,0)",
          fmt=FMT_FLAG, memo=True),

        L(None, "Income statement", kind="section"),
        L("revenue", "Revenue", entry=f"={A['revenue_at_entry']}",
          year=lambda c, p, R: f"={p}{R['revenue']}*(1+Plan!{c}${A['plan_row:revenue_growth']})", bold=True),
        L("rev_g", "  growth", year=lambda c, p, R: f"={c}{R['revenue']}/{p}{R['revenue']}-1", fmt=FMT_PCT, memo=True),
        L("ebitda", "EBITDA", entry=f"={A['entry_ebitda']}",
          year=lambda c, p, R: f"={c}{R['revenue']}*Plan!{c}${A['plan_row:ebitda_margin']}", bold=True),
        L("margin", "  margin", entry=lambda R: f"=C{R['ebitda']}/C{R['revenue']}",
          year=lambda c, p, R: f"={c}{R['ebitda']}/{c}{R['revenue']}", fmt=FMT_PCT, memo=True),
        L("da", "Less: D&A", year=lambda c, p, R: f"={c}{R['revenue']}*{A['da_pct_revenue']}"),
        L("ebit", "EBIT", year=lambda c, p, R: f"={c}{R['ebitda']}-{c}{R['da']}", bold=True),
        L("int_senior", "Less: interest on Term Loan (opening balance)",
          year=lambda c, p, R: f"={p}{R['tl_end']}*{A['senior_rate']}"),
        L("int_sub", "Less: interest on Sub Notes (opening balance)",
          year=lambda c, p, R: f"={p}{R['sub_end']}*{A['sub_rate']}"),
        L("int_rcf", "Less: interest on RCF (opening balance)",
          year=lambda c, p, R: f"={p}{R['rcf_end']}*{A['rcf_rate']}"),
        L("interest", "Total interest expense", year=lambda c, p, R: f"=SUM({c}{R['int_senior']}:{c}{R['int_rcf']})"),
        L("fee_amort", "Less: amortisation of financing fees + OID (non-cash)",
          year=lambda c, p, R: (f"=IF({c}{R['years']}<={A['fee_amortization_years']},"
                                f"($C${R['su_finfees']}+$C${R['su_oid']})/{A['fee_amortization_years']},0)")),
        L("ebt", "EBT", year=lambda c, p, R: f"={c}{R['ebit']}-{c}{R['interest']}-{c}{R['fee_amort']}", bold=True),
        L("tax", "Less: tax (none on losses)", year=lambda c, p, R: f"=MAX({c}{R['ebt']},0)*{A['tax_rate']}"),
        L("ni", "Net income", year=lambda c, p, R: f"={c}{R['ebt']}-{c}{R['tax']}", bold=True),

        L(None, "Free cash flow", kind="section"),
        L("cf_ni", "Net income", year=lambda c, p, R: f"={c}{R['ni']}"),
        L("cf_da", "Plus: D&A", year=lambda c, p, R: f"={c}{R['da']}"),
        L("cf_amort", "Plus: fee + OID amortisation (non-cash)", year=lambda c, p, R: f"={c}{R['fee_amort']}"),
        L("capex", "Less: capex",
          year=lambda c, p, R: f"={c}{R['revenue']}*Plan!{c}${A['plan_row:capex_pct_revenue']}"),
        L("nwc", "Less: increase in NWC",
          year=lambda c, p, R: f"=({c}{R['revenue']}-{p}{R['revenue']})*{A['nwc_pct_of_rev_growth']}"),
        L("fcf", "Free cash flow",
          year=lambda c, p, R: f"={c}{R['cf_ni']}+{c}{R['cf_da']}+{c}{R['cf_amort']}-{c}{R['capex']}-{c}{R['nwc']}",
          bold=True),

        L(None, "Cash waterfall", kind="section"),
        L("beg_excess", "Cash above minimum, opening", year=lambda c, p, R: f"={p}{R['cash_end']}-{A['min_cash']}"),
        L("wf_fcf", "Plus: free cash flow", year=lambda c, p, R: f"={c}{R['fcf']}"),
        L("mand", "Less: Term Loan mandatory amortization",
          year=lambda c, p, R: f"=MIN($C${R['su_senior']}*{A['senior_mandatory_amort_pct']},{p}{R['tl_end']})"),
        L("avail", "Cash available after mandatory debt service",
          year=lambda c, p, R: f"={c}{R['beg_excess']}+{c}{R['wf_fcf']}-{c}{R['mand']}", bold=True),
        L("rcf_draw", "RCF draw (covers a shortfall)", year=lambda c, p, R: f"=MAX(-{c}{R['avail']},0)"),
        L("rcf_repay", "RCF repayment (first use of surplus)",
          year=lambda c, p, R: f"=MIN(MAX({c}{R['avail']},0),{p}{R['rcf_end']})"),
        L("sweep", "Term Loan cash sweep",
          year=lambda c, p, R: (f"=MIN((MAX({c}{R['avail']},0)-{c}{R['rcf_repay']})*{A['cash_sweep_pct']},"
                                f"{p}{R['tl_end']}-{c}{R['mand']})")),
        L("retained", "Surplus cash retained on balance sheet",
          year=lambda c, p, R: f"=MAX({c}{R['avail']},0)-{c}{R['rcf_repay']}-{c}{R['sweep']}"),

        L(None, "Debt schedule", kind="section"),
        L("tl_beg", "Term Loan — opening", year=lambda c, p, R: f"={p}{R['tl_end']}"),
        L("tl_mand", "  mandatory amortization", year=lambda c, p, R: f"=-{c}{R['mand']}"),
        L("tl_sweep", "  cash sweep", year=lambda c, p, R: f"=-{c}{R['sweep']}"),
        L("tl_end", "Term Loan — closing", entry=lambda R: f"=C{R['su_senior']}",
          year=lambda c, p, R: f"=SUM({c}{R['tl_beg']}:{c}{R['tl_sweep']})", bold=True),
        L("sub_end", "Subordinated Notes — closing (bullet)", entry=lambda R: f"=C{R['su_sub']}",
          year=lambda c, p, R: f"={p}{R['sub_end']}", bold=True),
        L("rcf_beg", "RCF — opening", year=lambda c, p, R: f"={p}{R['rcf_end']}"),
        L("rcf_d", "  draws", year=lambda c, p, R: f"={c}{R['rcf_draw']}"),
        L("rcf_r", "  repayments", year=lambda c, p, R: f"=-{c}{R['rcf_repay']}"),
        L("rcf_end", "RCF — closing", entry=0, year=lambda c, p, R: f"=SUM({c}{R['rcf_beg']}:{c}{R['rcf_r']})",
          bold=True),
        L("rcf_headroom", "  undrawn RCF headroom", year=lambda c, p, R: f"={A['rcf_commitment']}-{c}{R['rcf_end']}",
          memo=True),
        L("cash_end", "Cash — closing", entry=f"={A['min_cash']}",
          year=lambda c, p, R: f"={A['min_cash']}+{c}{R['retained']}", bold=True),
        L("total_debt", "Total debt", entry=lambda R: f"=C{R['tl_end']}+C{R['sub_end']}+C{R['rcf_end']}",
          year=lambda c, p, R: f"={c}{R['tl_end']}+{c}{R['sub_end']}+{c}{R['rcf_end']}"),
        L("net_debt", "Net debt", entry=lambda R: f"=C{R['total_debt']}-C{R['cash_end']}",
          year=lambda c, p, R: f"={c}{R['total_debt']}-{c}{R['cash_end']}", bold=True),

        L(None, "Credit statistics", kind="section"),
        L("lev", "Total debt / EBITDA", entry=lambda R: f"=C{R['total_debt']}/C{R['ebitda']}",
          year=lambda c, p, R: f"={c}{R['total_debt']}/{c}{R['ebitda']}", fmt=FMT_X),
        L("cov", "EBITDA / interest",
          year=lambda c, p, R: f'=IF({c}{R["interest"]}>0,{c}{R["ebitda"]}/{c}{R["interest"]},"n.m.")', fmt=FMT_X),

        L(None, "Checks and helpers", kind="section"),
        L("chk_cash", "Cash conservation |change in net debt - FCF| (must be 0)",
          year=lambda c, p, R: f"=ABS(({p}{R['net_debt']}-{c}{R['net_debt']})-{c}{R['fcf']})",
          fmt=FMT_CHECK, memo=True),
        L("cov_hold", "EBITDA / interest, holding period only",
          year=lambda c, p, R: (f'=IF(AND({c}{R["in_hold"]}=1,{c}{R["interest"]}>0),'
                                f'{c}{R["ebitda"]}/{c}{R["interest"]},"")'), fmt=FMT_X, memo=True),
        L("rcf_hold", "RCF balance, holding period only",
          year=lambda c, p, R: f"={c}{R['rcf_end']}*{c}{R['in_hold']}", memo=True),
        L("eq_cf", "Sponsor equity cash flows", entry=lambda R: f"=-C{R['su_equity']}",
          year=lambda c, p, R: f"=IF({c}{R['years']}={A['hold_period_years']},$C${R['exit_equity']},0)", bold=True),
        L(None, kind="blank"),

        L(None, "Returns", kind="header", extra={"C": "{cur} mm"}),
        L("hold", "Holding period (years)", entry=f"={A['hold_period_years']}", fmt=FMT_INT),
        L("exit_ebitda", "Exit EBITDA (final year of hold)", entry=lambda R: f"=INDEX({yr(R, 'ebitda')},C{R['hold']})"),
        L("exit_mult", "Exit EV / EBITDA", entry=f"={A['exit_ev_multiple']}", fmt=FMT_X),
        L("exit_ev", "Exit enterprise value", entry=lambda R: f"=C{R['exit_ebitda']}*C{R['exit_mult']}"),
        L("exit_nd", "Less: net debt at exit", entry=lambda R: f"=INDEX({yr(R, 'net_debt')},C{R['hold']})"),
        L("exit_equity", "Exit equity value", entry=lambda R: f"=C{R['exit_ev']}-C{R['exit_nd']}", bold=True),
        L("entry_equity", "Entry sponsor equity", entry=lambda R: f"=C{R['su_equity']}"),
        L("moic", "MOIC", entry=lambda R: f"=C{R['exit_equity']}/C{R['entry_equity']}", fmt=FMT_X, bold=True,
          highlight=True),
        L("irr", "IRR  (= MOIC ^ (1 / years) - 1: no interim dividends)",
          entry=lambda R: f"=IF(C{R['moic']}>0,C{R['moic']}^(1/C{R['hold']})-1,-1)", fmt=FMT_PCT, bold=True,
          highlight=True),
        L("irr_chk", "IRR cross-check: Excel IRR() on equity cash flows",
          entry=lambda R: f'=IFERROR(IRR(C{R["eq_cf"]}:{last}{R["eq_cf"]}),"n.m.")', fmt=FMT_PCT, memo=True),
        L("peak_rcf", "Peak RCF draw during hold", entry=lambda R: f"=MAX({yr(R, 'rcf_hold')})"),
        L("min_cov", "Minimum EBITDA / interest during hold",
          entry=lambda R: f'=IF(COUNT({yr(R, "cov_hold")})>0,MIN({yr(R, "cov_hold")}),"n.m.")', fmt=FMT_X),
        L("flag_rcf", "Liquidity flag",
          entry=lambda R: (f'=IF(C{R["peak_rcf"]}>{A["rcf_commitment"]}+0.000001,'
                           f'"RCF over commitment: structure does not fund itself",'
                           f'IF(C{R["peak_rcf"]}>0,"RCF drawn to cover a shortfall","-"))')),
        L("flag_cov", "Coverage flag",
          entry=lambda R: f'=IF(AND(ISNUMBER(C{R["min_cov"]}),C{R["min_cov"]}<2),"Coverage below 2.0x","-")'),
        L("flag_eq", "Equity flag", entry=lambda R: f'=IF(C{R["exit_equity"]}<=0,"Exit equity <= 0","-")'),
    ]


FIRST_LBO_ROW = 5


def _assign_rows(layout: List[Line]) -> Dict[str, int]:
    return {line.key: FIRST_LBO_ROW + i for i, line in enumerate(layout) if line.key}


def lbo_rows(a: Assumptions) -> Dict[str, int]:
    """Row number of every keyed line on the LBO sheet (for tests and downstream readers)."""
    n_years = max(MIN_PROJECTION_YEARS, a.hold_period_years)
    last = get_column_letter(ENTRY_COL + n_years)
    dummy = {f.name: "X" for f in dataclasses.fields(Assumptions)} | {f"plan_row:{d}": 0 for d in PLAN_DRIVERS}
    return _assign_rows(_lbo_layout(dummy, n_years, last))


def _write_lbo(ws, a: Assumptions, A: Dict[str, str], cur: str, n_years: int) -> Dict[str, int]:
    ws.sheet_view.showGridLines = False
    last = get_column_letter(ENTRY_COL + n_years)
    year_cols = [get_column_letter(ENTRY_COL + t) for t in range(1, n_years + 1)]
    ws.column_dimensions["A"].width = 52
    ws.column_dimensions["B"].width = 9
    for col in ["C"] + year_cols:
        ws.column_dimensions[col].width = 11

    layout = _lbo_layout(A, n_years, last)
    R = _assign_rows(layout)                 # pass 1: rows first, so formulas can point forward

    def resolve(f, *args):
        return f(*args) if callable(f) else f

    row = FIRST_LBO_ROW
    for line in layout:                      # pass 2: write
        label = line.label.format(cur=cur)
        if line.kind == "blank":
            row += 1
            continue
        if line.kind in ("section", "header"):
            _put(ws, f"A{row}", label, bold=True, fill=SECTION_FILL)
            for col in ["B", "C"] + year_cols:
                ws[f"{col}{row}"].fill = SECTION_FILL
            for col, text in line.extra.items():
                _put(ws, f"{col}{row}", text.format(cur=cur), bold=True, fill=SECTION_FILL, align="right")
            row += 1
            continue

        color = GREY if line.memo else None
        timeline = line.kind == "timeline"
        _put(ws, f"A{row}", label, bold=line.bold or timeline, italic=line.memo, color=GREY if line.memo else BLACK,
             fill=TITLE_FILL if timeline else None)
        if timeline:
            ws[f"A{row}"].font = _font(WHITE, bold=True)
            ws[f"B{row}"].fill = TITLE_FILL
        if line.entry is not None:
            value = resolve(line.entry, R)
            entry_color = WHITE if timeline else color or (BLACK if isinstance(value, (int, float)) else None)
            _put(ws, f"C{row}", value, fmt=line.fmt, bold=line.bold,
                 italic=line.memo, color=entry_color,
                 fill=YELLOW_FILL if line.highlight else (TITLE_FILL if timeline else None))
        if line.year is not None:
            prev = "C"
            for col in year_cols:
                _put(ws, f"{col}{row}", line.year(col, prev, R), fmt=line.fmt, bold=line.bold,
                     italic=line.memo, color=WHITE if timeline else color, fill=TITLE_FILL if timeline else None)
                prev = col
        for col, fn in line.extra.items():
            _put(ws, f"{col}{row}", fn(R, row), fmt=FMT_X if col == "D" else FMT_PCT, italic=True, color=GREY)
        if line.bold and line.key not in ("revenue", "ebitda", "su_ebitda") and not timeline:
            bordered = (["C"] if line.entry is not None else []) + (year_cols if line.year is not None else [])
            for col in bordered:
                ws[f"{col}{row}"].border = THIN_TOP
        row += 1
    end_of_model = row

    # Header block
    _put(ws, "A1", f"{a.company_name} — LBO model ({cur} mm)", bold=True, color=WHITE, fill=TITLE_FILL)
    for col in ["B", "C"] + year_cols:
        ws[f"{col}1"].fill = TITLE_FILL
    _put(ws, "A2", "Every figure is a live formula driven by the Assumptions sheet. Interest on opening balances "
                   "(no circularity). Greyed columns fall outside the holding period.", italic=True, color=GREY)
    _put(ws, "A3", "Model checks", bold=True)
    _put(ws, "C3", f'=IF(AND(ABS(C{R["su_check"]})<0.001,MAX($D${R["chk_cash"]}:${last}${R["chk_cash"]})<0.001),'
                   f'"OK","ERROR")', bold=True, align="right")
    _put(ws, "E3", "MOIC", bold=True, align="right")
    _put(ws, "F3", f"=C{R['moic']}", fmt=FMT_X, bold=True)
    _put(ws, "G3", "IRR", bold=True, align="right")
    _put(ws, "H3", f"=C{R['irr']}", fmt=FMT_PCT, bold=True)
    ws.conditional_formatting.add("C3", CellIsRule(operator="equal", formula=['"ERROR"'],
                                                   font=Font(name=FONT, color=WHITE, bold=True),
                                                   fill=PatternFill("solid", fgColor=RED)))
    ws.conditional_formatting.add("C3", CellIsRule(operator="equal", formula=['"OK"'],
                                                   font=Font(name=FONT, color=GREEN, bold=True)))
    # Grey out projection years beyond the holding period.
    ws.conditional_formatting.add(
        f"D{R['years'] + 1}:{last}{R['eq_cf']}",
        FormulaRule(formula=[f"D${R['years']}>$C${R['hold']}"], font=Font(name=FONT, color="BFBFBF")))

    _write_sensitivity(ws, R, end_of_model + 1, n_years, last)
    ws.freeze_panes = "D1"
    return R


def _write_sensitivity(ws, R: Dict[str, int], top: int, n_years: int, last: str):
    """IRR by exit multiple x holding period. Fully live and exact: the projection path does not
    depend on the exit multiple or the holding period, so each cell only re-prices the exit."""
    yr = lambda key: f"$D${R[key]}:${last}${R[key]}"
    holds = [3, 4, 5, 6, 7]  # always within the projection (>= MIN_PROJECTION_YEARS columns)
    offsets = [-1.0, -0.5, 0.0, 0.5, 1.0]
    cols = [get_column_letter(4 + i) for i in range(len(holds))]

    _put(ws, f"A{top}", "IRR sensitivity: exit multiple (rows) x holding period (columns)", bold=True,
         fill=SECTION_FILL)
    for col in ["B", "C"] + [get_column_letter(ENTRY_COL + t) for t in range(1, n_years + 1)]:
        ws[f"{col}{top}"].fill = SECTION_FILL
    hdr = top + 1
    _put(ws, f"A{hdr}", "Change vs base exit multiple  |  exit multiple  |  holding period (years):",
         italic=True, color=GREY)
    for col, h in zip(cols, holds):
        _put(ws, f"{col}{hdr}", h, fmt='0" yrs"', bold=True, align="right")
    for i, off in enumerate(offsets):
        r = hdr + 1 + i
        _put(ws, f"B{r}", off, fmt='+0.0"x";-0.0"x";"base"')
        _put(ws, f"C{r}", f"=$C${R['exit_mult']}+B{r}", fmt=FMT_X, bold=True)
        for col in cols:
            equity = f"(INDEX({yr('ebitda')},{col}${hdr})*$C{r}-INDEX({yr('net_debt')},{col}${hdr}))"
            _put(ws, f"{col}{r}",
                 f'=IFERROR(IF({equity}>0,({equity}/$C${R["entry_equity"]})^(1/{col}${hdr})-1,-1),"n.a.")',
                 fmt=FMT_PCT, align="right")
    first, lastrow = hdr + 1, hdr + len(offsets)
    ws.conditional_formatting.add(
        f"{cols[0]}{first}:{cols[-1]}{lastrow}",
        FormulaRule(formula=[f"AND($B{first}=0,{cols[0]}${hdr}=$C${R['hold']})"], fill=YELLOW_FILL))


# --- Audit sheet ---------------------------------------------------------------

CONVENTIONS = [
    "Interest on opening balances of every tranche: no circular reference, no iterative calculation.",
    "Cash waterfall: FCF + opening surplus cash -> Term Loan mandatory amortization -> shortfall drawn on "
    "RCF / surplus repays RCF first, then sweeps the Term Loan -> remainder kept as cash.",
    "Subordinated Notes are bullet and non-call: repaid at exit out of the equity value.",
    "Exit: final-year EBITDA x exit multiple, less net debt (Term Loan + Sub Notes + RCF - cash).",
    "Transaction costs: M&A fees, financing fees and Term Loan OID are Uses paid at close (they raise the "
    "equity cheque); financing fees + OID are amortised straight-line, non-cash and tax-deductible, and "
    "added back in FCF. Debt is repaid at face value.",
    "Not modelled: interest income on cash, RCF commitment fee, tax-loss carry-forwards, dividends / "
    "recaps, management equity.",
    "Only two equity cash flows, so IRR = MOIC ^ (1 / years) - 1; Excel IRR() is shown as a cross-check.",
]


def _write_audit(ws, a: Assumptions, audit: Optional[dict], R: Dict[str, int], cur: str):
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 110
    _put(ws, "A1", f"{a.company_name} — audit trail", bold=True, color=WHITE, fill=TITLE_FILL)
    for col in "BC":
        ws[f"{col}1"].fill = TITLE_FILL
    _put(ws, "A2", f"Exported {dt.date.today().isoformat()} · figures in {cur} mm", italic=True, color=GREY)

    row = 4

    def section(title):
        nonlocal row
        row += 1
        _put(ws, f"A{row}", title, bold=True, fill=SECTION_FILL)
        for col in "BC":
            ws[f"{col}{row}"].fill = SECTION_FILL
        row += 1

    def text(label, value, color=BLACK):
        nonlocal row
        _put(ws, f"A{row}", label)
        _put(ws, f"C{row}", value, color=color)
        ws[f"C{row}"].alignment = Alignment(wrap_text=True, vertical="top")
        row += 1

    snap = run_model(a)["returns"]  # engine snapshot on exactly the workbook's inputs
    section("Python engine snapshot at export vs live workbook")
    _put(ws, f"A{row}", "Metric", bold=True)
    _put(ws, f"B{row}", "Engine (static)", bold=True, align="right")
    _put(ws, f"C{row}", "Live workbook  |  status", bold=True)
    row += 1
    for label, engine_key, lbo_key, number_fmt in (
            ("Exit equity value", "exit_equity_value", "exit_equity", FMT_MM),
            ("MOIC", "moic", "moic", FMT_X), ("IRR", "irr", "irr", FMT_PCT)):
        _put(ws, f"A{row}", label)
        _put(ws, f"B{row}", snap[engine_key], fmt=number_fmt, color=GREY)
        _put(ws, f"C{row}", f"=LBO!$C${R[lbo_key]}", fmt=number_fmt)
        ws[f"C{row}"].alignment = Alignment(horizontal="left")
        row += 1
    moic_row = row - 2
    _put(ws, f"A{row}", "Status")
    _put(ws, f"C{row}", f'=IF(ABS(C{moic_row}-B{moic_row})<0.000001,"Matches the Python engine",'
                        f'"Inputs changed since export: engine snapshot no longer applies")', bold=True)
    row += 1
    for w in snap["warnings"]:
        text("Engine warning", w, RED)

    if audit and audit.get("scenario"):
        sc = audit["scenario"]
        section(f"Scenario: {sc['name']} (built on '{sc['based_on']}')")
        text("Rationale", sc["rationale"])
        for name, value in sc["overrides"].items():
            shown = "/".join(f"{x:g}" for x in value) if isinstance(value, (list, tuple)) else f"{value:g}"
            text("Changed vs base", f"{name} = {shown}")

    if audit:
        section("Sector call (Claude)")
        if audit.get("provenance"):
            text("Base case source", audit["provenance"])
        text("Sector", audit.get("sector_label", audit.get("sector", "")))
        text("Rationale", audit.get("sector_rationale", ""))
        section("Guardrail adjustments to Claude's proposal")
        for item in audit.get("adjustments") or ["None: every estimate was inside the benchmark ranges"]:
            text("", item)
        section("Guardrail warnings")
        for item in audit.get("warnings") or ["None"]:
            text("", item, RED if audit.get("warnings") else BLACK)
        section("Key risks to underwrite")
        for item in audit.get("key_risks") or ["-"]:
            text("", item)
    else:
        section("Source of assumptions")
        text("", "Entered manually / engine defaults (not generated by Claude).")

    section("Modelling conventions")
    for item in CONVENTIONS:
        text("", item)


# --- entry point -----------------------------------------------------------------

def export_to_excel(a: Assumptions, path: str, audit: Optional[dict] = None) -> str:
    """Write the live LBO workbook. `audit` is GenerationResult.to_dict() (optional)."""
    cur = (audit or {}).get("currency") or "EUR"
    n_years = max(MIN_PROJECTION_YEARS, a.hold_period_years)

    wb = Workbook()
    ws_a = wb.active
    ws_a.title = "Assumptions"
    ws_plan = wb.create_sheet("Plan")
    ws_lbo = wb.create_sheet("LBO")
    ws_audit = wb.create_sheet("Audit")

    A = _write_assumptions(ws_a, a, audit, cur, n_years)
    A |= _write_plan(ws_plan, a, audit, n_years)
    R = _write_lbo(ws_lbo, a, A, cur, n_years)
    _write_audit(ws_audit, a, audit, R, cur)

    for ws in wb.worksheets:              # print: landscape, one page wide
        ws.page_setup.orientation = "landscape"
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True

    wb.active = 2                        # open on the LBO sheet
    wb.calculation.fullCalcOnLoad = True  # openpyxl stores no cached values: force Excel to compute
    wb.save(path)
    return path


def assumptions_from_json(data: dict) -> Assumptions:
    return Assumptions(**data["assumptions"])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Export an LBO to Excel with live formulas.")
    ap.add_argument("--from-json", help="JSON written by assumption_generator.py --json")
    ap.add_argument("-o", "--output", default="lbo_model.xlsx")
    args = ap.parse_args(argv)

    if args.from_json:
        with open(args.from_json, encoding="utf-8") as f:
            audit = json.load(f)
        a = assumptions_from_json(audit)
    else:
        audit, a = None, Assumptions()
    print(f"Saved {export_to_excel(a, args.output, audit)}")


if __name__ == "__main__":
    main()
