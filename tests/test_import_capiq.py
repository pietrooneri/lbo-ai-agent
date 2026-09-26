"""Capital IQ import on synthetic exports shaped like the real ones (title rows, bracketed
headers, % strings, negative capex, 'NM' cells)."""

import csv

import openpyxl
import pytest

import calibrate_benchmarks as cb
import import_capiq as ic


def _xlsx(path, rows):
    wb = openpyxl.Workbook()
    for r in rows:
        wb.active.append(r)
    wb.save(path)
    return str(path)


COMPANY_HEADER = ["Company Name", "Primary Industry", "Total Revenue [LTM] (€EURmm)", "EBITDA Margin % [LTM]",
                  "Total Revenue, 3 Yr CAGR % [LTM]", "Capital Expenditure [LTM] (€EURmm)",
                  "D&A [LTM] (€EURmm)", "Net Working Capital [LTM] (€EURmm)", "Net Debt/EBITDA [LTM] (x)"]


def companies(tmp_path):
    rows = [["S&P Capital IQ - Company Screening"], ["Screening criteria: Western Europe"], [], COMPANY_HEADER]
    for i in range(6):
        rows.append([f"Valve Co {i}", "Industrials > Machinery", 200 + 20 * i, f"{12 + i}%", 4 + i,
                     -(6 + i), 7 + i, 30 + i, 3 + 0.5 * i])
    rows.append(["Odd Co", "Industrials > Machinery", 100, "NM", "-", "-", "-", "-", "NM"])
    rows.append(["Bank Co", "Financials > Banks", 900, "30%", 5, -10, 10, 0, 1])
    return _xlsx(tmp_path / "companies.xlsx", rows)


def deals(tmp_path):
    rows = [["Transaction Screening"], [], ["Target Name", "Target/Issuer Primary Industry", "Announced Date",
                                            "Implied Enterprise Value/EBITDA (x)"]]
    for i, m in enumerate([7.5, 8.0, 9.0, 10.0, 11.0, -3.0, "NM"]):
        rows.append([f"Deal {i}", "Industrials > Machinery", "2024-05-01", m])
    return _xlsx(tmp_path / "deals.xlsx", rows)


def test_company_export_is_recognised_and_normalised(tmp_path):
    res = ic.convert(companies(tmp_path))
    assert set(res["columns"]) >= {"industry", "revenue", "ebitda_margin", "revenue_growth", "capex", "da",
                                   "nwc", "net_leverage"}
    rows = [r for r in res["rows"] if r["source_row"].endswith((":5", ":6", ":7", ":8", ":9", ":10"))]
    first = rows[0]
    assert first["sector"] == "industrials"
    assert first["ebitda_margin"] == pytest.approx(0.12)                   # "12%"
    assert first["revenue_growth"] == pytest.approx(0.04)                  # 4 -> 4%
    assert first["capex_pct_revenue"] == pytest.approx(6 / 200)            # negative capex -> positive share
    assert first["da_pct_revenue"] == pytest.approx(7 / 200)
    assert first["total_leverage_x"] == 3
    assert res["unmapped"] == {}                                            # banks are skipped on purpose
    assert len(res["rows"]) == 6                                           # 'Odd Co' has nothing usable


def test_deal_export_drops_negative_and_missing_multiples(tmp_path):
    res = ic.convert(deals(tmp_path))
    assert [r["entry_ev_multiple"] for r in res["rows"]] == [7.5, 8.0, 9.0, 10.0, 11.0]


def test_end_to_end_into_calibration(tmp_path):
    out = tmp_path / "comps.csv"
    ic.main([companies(tmp_path), deals(tmp_path), "-o", str(out)])
    assert len(list(csv.DictReader(open(out)))) == 11
    ranges = cb.from_csv(str(out))["industrials"]["ranges"]
    assert ranges["entry_ev_multiple"] == (8.0, 10.0)                       # P25-P75 of 7.5..11
    assert 0.12 <= ranges["ebitda_margin"][0] < ranges["ebitda_margin"][1] <= 0.18      # P25-P75, widened to min width
    assert "total_leverage_x" in ranges


@pytest.mark.parametrize("industry, sector", [
    ("Information Technology > Software > Application Software", "software_saas"),
    ("Health Care > Health Care Providers and Services", "healthcare_services"),
    ("Consumer Discretionary > Hotels, Restaurants and Leisure", "leisure_fitness"),
    ("Materials > Chemicals > Specialty Chemicals", "chemicals"),
    ("Consumer Discretionary > Specialty Retail", "consumer_retail"),
])
def test_industry_mapping(industry, sector):
    assert ic.sector_for(industry) == sector


def test_currency_factor_converts_usd_columns_to_eur():
    from import_capiq import currency_factor
    assert currency_factor("Total Revenue [LTM] (€EURmm, Historical rate)") == 1.0
    assert currency_factor("Depreciation & Amort. [LTM] ($USDmm, Historical rate)") < 1.0
    assert currency_factor("EBITDA Margin % [LTM]") == 1.0
