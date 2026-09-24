"""The exported workbook is a second implementation of the engine, in Excel formulas.
These tests evaluate it (pycel) and require it to match lbo_engine.py line by line."""

import dataclasses
import math

import openpyxl
import pytest
from openpyxl.utils import get_column_letter
from pycel import ExcelCompiler

from assumption_generator import apply_guardrails
from excel_export import ASSUMPTION_LAYOUT, export_to_excel, lbo_rows
from lbo_engine import Assumptions, run_model
from test_assumption_generator import proposal
from test_lbo_engine import SCENARIOS

YEAR_LINES = {  # LBO row key -> YearResult attribute
    "revenue": "revenue", "ebitda": "ebitda", "da": "da", "ebit": "ebit", "interest": "interest_expense",
    "ebt": "ebt", "tax": "tax", "ni": "net_income", "capex": "capex", "nwc": "nwc_change",
    "fcf": "fcf_pre_sweep", "mand": "mandatory_amort", "sweep": "cash_sweep",
    "rcf_draw": "rcf_draw", "rcf_repay": "rcf_repayment", "tl_end": "senior_end_balance",
    "sub_end": "sub_end_balance", "rcf_end": "rcf_end_balance", "cash_end": "cash_end_balance",
}


def _assumption_cell(name):
    row = 5
    for field, label, *_ in ASSUMPTION_LAYOUT:
        if label is None:
            row += 2
            continue
        if field == name:
            return f"Assumptions!B{row}"
        row += 1
    raise KeyError(name)


class Book:
    def __init__(self, path, a):
        self.xl = ExcelCompiler(filename=str(path), plugins=("pycel_plugins",))
        self.R = lbo_rows(a)

    def lbo(self, key, col="C"):
        return self.xl.evaluate(f"LBO!{col}{self.R[key]}")

    def year(self, key, t):
        return self.lbo(key, get_column_letter(3 + t))

    def set(self, name, value):
        self.xl.evaluate("Audit!C10")  # pycel only lets you edit cells it has already loaded
        self.xl.set_value(_assumption_cell(name), value)


def _export(tmp_path, a, audit=None):
    path = tmp_path / "lbo.xlsx"
    export_to_excel(a, str(path), audit)
    return Book(path, a)


def _assert_matches_engine(book, a):
    out = run_model(a)
    for t, yr in enumerate(out["years"], start=1):
        for key, attr in YEAR_LINES.items():
            assert book.year(key, t) == pytest.approx(getattr(yr, attr), abs=1e-6), f"{key} year {t}"
    ret = out["returns"]
    assert book.lbo("su_equity") == pytest.approx(out["sources_uses"]["sources"]["sponsor_equity"])
    assert book.lbo("exit_equity") == pytest.approx(ret["exit_equity_value"], abs=1e-6)
    assert book.lbo("moic") == pytest.approx(ret["moic"], abs=1e-9)
    assert book.lbo("irr") == pytest.approx(ret["irr"], abs=1e-9)
    assert book.lbo("irr_chk") == pytest.approx(ret["irr"], abs=1e-7)
    assert book.lbo("peak_rcf") == pytest.approx(ret["peak_rcf_draw"], abs=1e-6)
    if math.isfinite(ret["min_interest_coverage"]):
        assert book.lbo("min_cov") == pytest.approx(ret["min_interest_coverage"], abs=1e-9)
    assert (book.lbo("flag_rcf") != "-") == any("RCF" in w for w in ret["warnings"])
    assert (book.lbo("flag_cov") != "-") == any("coverage" in w for w in ret["warnings"])
    assert book.xl.evaluate("LBO!C3") == "OK"


@pytest.mark.parametrize("name", SCENARIOS)
def test_workbook_matches_engine(tmp_path, name):
    a = SCENARIOS[name]
    _assert_matches_engine(_export(tmp_path, a), a)


def test_workbook_from_generator_output(tmp_path):
    res = apply_guardrails(proposal(provided={"revenue_at_entry"}, ebitda_margin=0.35))
    book = _export(tmp_path, res.assumptions, res.to_dict())
    _assert_matches_engine(book, res.assumptions)
    assert book.xl.evaluate("Audit!C10").startswith("Matches the Python engine")
    wb = openpyxl.load_workbook(tmp_path / "lbo.xlsx")
    notes = [c.value for c in wb["Assumptions"]["E"] if c.value]
    assert any("Guardrail" in n and "Claude proposed 0.35" in n for n in notes)


@pytest.mark.parametrize("name, value", [
    ("hold_period_years", 7), ("revenue_growth", 0.08), ("ebitda_margin", 0.12), ("entry_ebitda", 140.0),
    ("total_leverage_x", 6.0), ("exit_ev_multiple", 9.5), ("cash_sweep_pct", 0.5),
])
def test_editing_an_input_in_excel_reprices_the_model(tmp_path, name, value):
    """The point of 'live formulas': change one blue cell, get the engine's answer for it."""
    base = SCENARIOS["base"]
    book = _export(tmp_path, base)
    book.set(name, value)
    flexed = dataclasses.replace(base, **{name: value})
    ret = run_model(flexed)["returns"]
    assert book.lbo("moic") == pytest.approx(ret["moic"], abs=1e-9)
    assert book.lbo("irr") == pytest.approx(ret["irr"], abs=1e-9)
    assert book.xl.evaluate("Audit!C10").startswith("Inputs changed")


def test_sensitivity_table_matches_engine(tmp_path):
    base = SCENARIOS["base"]
    book = _export(tmp_path, base)
    R = book.R
    sens_hdr = max(R.values()) + 3            # section title, then header row
    for i, off in enumerate([-1.0, -0.5, 0.0, 0.5, 1.0]):
        for j, hold in enumerate([3, 4, 5, 6, 7]):
            cell = f"LBO!{get_column_letter(4 + j)}{sens_hdr + 1 + i}"
            flexed = dataclasses.replace(base, exit_ev_multiple=base.exit_ev_multiple + off, hold_period_years=hold)
            assert book.xl.evaluate(cell) == pytest.approx(run_model(flexed)["returns"]["irr"], abs=1e-9), cell


def test_lbo_sheet_has_no_hardcoded_numbers(tmp_path):
    """Only structural zeros (entry year index, opening RCF) and the sensitivity axis are constants."""
    a = SCENARIOS["base"]
    export_to_excel(a, str(tmp_path / "lbo.xlsx"))
    ws = openpyxl.load_workbook(tmp_path / "lbo.xlsx")["LBO"]
    R = lbo_rows(a)
    sens_rows = range(max(R.values()) + 3, ws.max_row + 1)
    constants = [c.coordinate for row in ws.iter_rows() for c in row
                 if isinstance(c.value, (int, float)) and not isinstance(c.value, bool)
                 and c.row not in sens_rows]
    assert sorted(constants) == sorted([f"C{R['years']}", f"C{R['rcf_end']}"])
