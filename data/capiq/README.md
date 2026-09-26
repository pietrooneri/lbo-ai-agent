# Capital IQ exports for benchmark calibration

Save the two exports in this folder. Everything here except this file is git-ignored:
the raw data is licensed (Warwick academic access) and must not be published. Only
the aggregated ranges (25th–75th percentiles per sector) go into `data/sector_benchmarks.json`.
Check that this use is allowed by the university's licence.

Menu names are indicative: they may differ slightly depending on the Capital IQ version.

## Export 1: PE buyout deals (entry multiples) → `deals.xlsx`

**Screening → Transactions → M&A**

| Criterion | Value |
|---|---|
| Transaction status | Closed |
| Announced / closed date | 01/01/2021 – today |
| Target / issuer geography | Europe (Western Europe; include Italy, DACH, France, Iberia, Benelux, Nordics, UK) |
| Transaction features / buyer type | Leveraged Buyout (LBO) **or** Buyer type = Private Equity Firm |
| Implied enterprise value | €50m – €2,000m |
| Majority stake | Percent sought ≥ 50% |

**Columns to add:** Target/Issuer primary industry · Announced date · Implied Enterprise Value (€m) · **Implied Enterprise Value / EBITDA (x)** · Implied EV / Revenue (x) · Target LTM Total Revenue · Target LTM EBITDA.

Many deals do not disclose a multiple: that is expected. A few hundred rows are enough.

## Export 2: mid-market private companies (operating metrics, leverage) → `companies.xlsx`

**Screening → Companies**

| Criterion | Value |
|---|---|
| Geography | Western Europe (as above) |
| Company status | Operating |
| Company type | Private company (optionally: private-equity backed / investor type = PE) |
| LTM Total Revenue | €50m – €1,000m |
| Financial data | LTM EBITDA and LTM Total Revenue available |

**Columns to add (LTM, € millions where applicable):** Primary industry · Total Revenue · EBITDA Margin % · Total Revenue 3-year CAGR % · Capital Expenditure · Depreciation & Amortization (D&A) · Net Working Capital · Net Debt / EBITDA (x).

Export up to the row limit (e.g. the largest 2,000–5,000 companies by revenue).

## Then

```bash
uv run python import_capiq.py "data/capiq/Company Screening Report.xls" data/capiq/deals.xls
uv run python calibrate_benchmarks.py --capiq data/capiq/comps.csv          # compare
uv run python calibrate_benchmarks.py --capiq data/capiq/comps.csv --write  # apply
```

Pick the **"Total Revenue"** data item for the revenue filter (not "Other Revenues, Total").
Columns in another currency (e.g. D&A in $USDmm) are converted to EUR by the importer.

What was applied (Sep 2026): only the EBITDA margin floors, lowered to the P25 of the
sponsor-backed companies in 7 sectors with 40+ observations (software and luxury excluded).
Growth, leverage, D&A, capex, NWC and deal multiples were not applied; the reasons are in
`CAPIQ_NOT_APPLIED` in `calibrate_benchmarks.py` and in `data/sector_benchmarks.json`.

The first command reports which columns it recognised, industries it could not map,
and how many observations each sector has (at least 5 per metric are needed). The
second prints current vs proposed ranges without changing anything; `--write` saves them.
