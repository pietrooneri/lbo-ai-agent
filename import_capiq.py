"""
Turn Capital IQ screening exports into the comparables CSV used by calibrate_benchmarks.py.

Two exports are expected (see data/capiq/README.md for the exact screens):
  deals      European PE buyouts: target industry + implied EV/EBITDA   -> entry_ev_multiple
  companies  European mid-market private companies: industry, revenue, EBITDA margin,
             revenue CAGR, capex, D&A, net working capital, net debt / EBITDA
                                                                      -> operating ranges + leverage

The raw exports are licensed data: they stay in data/capiq/ (git-ignored). Only the
aggregated percentiles computed from them end up in data/sector_benchmarks.json.

Usage:
    uv run python import_capiq.py data/capiq/companies.xlsx data/capiq/deals.xlsx
    uv run python calibrate_benchmarks.py --csv data/capiq/comps.csv           # compare
"""

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

OUT = Path(__file__).parent / "data" / "capiq" / "comps.csv"
OUT_FIELDS = ["sector", "entry_ev_multiple", "ebitda_margin", "revenue_growth", "capex_pct_revenue",
              "da_pct_revenue", "nwc_pct_of_rev_growth", "total_leverage_x", "source_row"]

# Capital IQ / GICS-style industry names (lower case, matched as substrings, first hit wins).
INDUSTRY_TO_SECTOR = [
    ("software", "software_saas"), ("internet services", "software_saas"),
    ("health care providers", "healthcare_services"), ("health care services", "healthcare_services"),
    ("health care facilities", "healthcare_services"),
    ("education", "education"), ("diversified consumer services", "education"),
    ("hotels, restaurants", "leisure_fitness"), ("leisure", "leisure_fitness"), ("fitness", "leisure_fitness"),
    ("entertainment", "leisure_fitness"),
    ("chemicals", "chemicals"),
    ("construction materials", "building_materials"), ("building products", "building_materials"),
    ("construction and engineering", "building_materials"), ("construction & engineering", "building_materials"),
    ("air freight", "logistics_transport"), ("logistics", "logistics_transport"),
    ("road and rail", "logistics_transport"), ("ground transportation", "logistics_transport"),
    ("marine", "logistics_transport"), ("transportation infrastructure", "logistics_transport"),
    ("telecommunication", "telecom_infra"), ("wireless", "telecom_infra"),
    ("food products", "food_beverage"), ("beverages", "food_beverage"), ("food and staples", "food_beverage"),
    ("textiles, apparel", "consumer_brands_luxury"), ("apparel", "consumer_brands_luxury"),
    ("luxury", "consumer_brands_luxury"), ("household durables", "consumer_brands_luxury"),
    ("personal products", "consumer_brands_luxury"), ("personal care", "consumer_brands_luxury"),
    ("leisure products", "consumer_brands_luxury"),
    ("specialty retail", "consumer_retail"), ("retail", "consumer_retail"), ("distributors", "consumer_retail"),
    ("commercial services", "business_services"), ("professional services", "business_services"),
    ("it services", "business_services"), ("media", "business_services"),
    ("machinery", "industrials"), ("electrical equipment", "industrials"), ("auto components", "industrials"),
    ("automobile components", "industrials"), ("containers", "industrials"), ("packaging", "industrials"),
    ("electronic equipment", "industrials"), ("aerospace", "industrials"), ("industrial conglomerates", "industrials"),
    ("trading companies", "industrials"), ("metals", "industrials"),
]

# Column detection: first header matching the pattern (case-insensitive) is used.
COLUMNS = {
    "industry": r"primary industry|industry classification|target.*industry|^industry",
    "revenue": r"^(ltm )?total revenue(?!.*(cagr|growth|/|%))|^revenue(?!.*(cagr|growth|/))",
    "ebitda_margin": r"ebitda margin",
    "revenue_growth": r"revenue.*(cagr|growth)|(cagr|growth).*revenue",
    "capex": r"capital expenditure|capex",
    "da": r"d&a|depreciation",
    "nwc": r"net working capital|\bnwc\b",
    "net_leverage": r"net debt\s*/\s*ebitda",
    "ev_ebitda": r"(implied )?(enterprise value|tev|ev)\s*/\s*(ltm )?ebitda",
}
MISSING = {"", "-", "--", "nm", "n/a", "na", "n.m.", "none"}


def _num(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower().replace(",", "").replace("€", "").replace("$", "").replace("x", "")
    if s in MISSING:
        return None
    pct = s.endswith("%")
    s = s.rstrip("%").strip()
    if s.startswith("(") and s.endswith(")"):                    # accounting negatives: (12.3)
        s = "-" + s[1:-1]
    try:
        return float(s) / (100 if pct else 1)
    except ValueError:
        return None


def _as_share(v: Optional[float]) -> Optional[float]:
    """Percent columns come as 12.5 or 0.125 or '12.5%': normalise to 0.125."""
    return None if v is None else (v / 100 if abs(v) > 1.5 else v)


def read_rows(path: str) -> List[List]:
    p = Path(path)
    if p.suffix.lower() == ".csv":
        return list(csv.reader(open(p, encoding="utf-8-sig")))
    if p.suffix.lower() == ".xls":
        import xlrd
        sh = xlrd.open_workbook(str(p)).sheet_by_index(0)
        return [sh.row_values(r) for r in range(sh.nrows)]
    import openpyxl
    ws = openpyxl.load_workbook(p, read_only=True, data_only=True).worksheets[0]
    return [list(r) for r in ws.iter_rows(values_only=True)]


def find_header(rows: List[List]) -> int:
    """Capital IQ exports start with title rows: the header is the first row naming an industry
    column and at least two other non-empty cells."""
    for i, row in enumerate(rows[:60]):
        cells = [str(c).strip().lower() for c in row if c not in (None, "")]
        if len(cells) >= 3 and any(re.search(COLUMNS["industry"], c) for c in cells):
            return i
    raise ValueError("No header row with an industry column found in the first 60 rows")


def map_columns(header: List) -> Dict[str, int]:
    found = {}
    for key, pattern in COLUMNS.items():
        for j, h in enumerate(header):
            if h not in (None, "") and re.search(pattern, str(h).strip().lower()) and j not in found.values():
                found[key] = j
                break
    return found


def sector_for(industry: str) -> Optional[str]:
    text = (industry or "").lower()
    return next((sector for key, sector in INDUSTRY_TO_SECTOR if key in text), None)


def convert(path: str) -> Dict:
    rows = read_rows(path)
    h = find_header(rows)
    cols = map_columns(rows[h])
    if "industry" not in cols:
        raise ValueError(f"{path}: no industry column; headers are {rows[h]}")
    out, unmapped = [], {}
    for i, row in enumerate(rows[h + 1:], start=h + 2):
        get = lambda k: row[cols[k]] if k in cols and cols[k] < len(row) else None
        industry = str(get("industry") or "").strip()
        if not industry:
            continue
        sector = sector_for(industry)
        if not sector:
            unmapped[industry] = unmapped.get(industry, 0) + 1
            continue
        revenue = _num(get("revenue"))
        rec = {"sector": sector, "source_row": f"{Path(path).name}:{i}"}
        margin = _as_share(_num(get("ebitda_margin")))
        if margin is not None and 0 < margin < 1:
            rec["ebitda_margin"] = margin
        growth = _as_share(_num(get("revenue_growth")))
        if growth is not None and -0.5 < growth < 1:
            rec["revenue_growth"] = growth
        for key, field in (("capex", "capex_pct_revenue"), ("da", "da_pct_revenue"), ("nwc", "nwc_pct_of_rev_growth")):
            v = _num(get(key))
            if v is not None and revenue and revenue > 0:
                share = abs(v) / revenue if key != "nwc" else v / revenue   # capex is often negative in exports
                if -1 < share < 1:
                    rec[field] = share
        lev = _num(get("net_leverage"))
        if lev is not None and 0 <= lev < 15:
            rec["total_leverage_x"] = lev
        mult = _num(get("ev_ebitda"))
        if mult is not None and 2 < mult < 40:                  # drop negative-EBITDA and outlier multiples
            rec["entry_ev_multiple"] = mult
        if len(rec) > 2:
            out.append(rec)
    return {"rows": out, "columns": {k: rows[h][j] for k, j in cols.items()}, "unmapped": unmapped}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Convert Capital IQ exports into comps.csv for calibration")
    ap.add_argument("files", nargs="+", help="Capital IQ exports (.xlsx, .xls or .csv)")
    ap.add_argument("-o", "--output", default=str(OUT))
    args = ap.parse_args(argv)

    all_rows = []
    for f in args.files:
        res = convert(f)
        print(f"\n{f}: {len(res['rows'])} usable rows")
        print("  columns used: " + "; ".join(f"{k} <- '{v}'" for k, v in res["columns"].items()))
        if res["unmapped"]:
            top = sorted(res["unmapped"].items(), key=lambda kv: -kv[1])[:12]
            print("  industries not mapped (add them to INDUSTRY_TO_SECTOR if relevant): " +
                  "; ".join(f"{k} ({n})" for k, n in top))
        all_rows += res["rows"]
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=OUT_FIELDS)
        w.writeheader()
        w.writerows(all_rows)
    counts = {}
    for r in all_rows:
        for k in OUT_FIELDS[1:-1]:
            if k in r:
                counts.setdefault(r["sector"], {}).setdefault(k, 0)
                counts[r["sector"]][k] += 1
    print(f"\nWrote {len(all_rows)} rows to {args.output}. Observations per sector (5+ needed per metric):")
    for sector, c in sorted(counts.items()):
        print(f"  {sector:24} " + ", ".join(f"{k}={n}" for k, n in c.items()))


if __name__ == "__main__":
    sys.exit(main())
