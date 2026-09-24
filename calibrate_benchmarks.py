"""
Calibrate the sector guardrail ranges in sector_benchmarks.py from data.

Two sources:
  1. Damodaran, Europe industry datasets (January 2026), in data/damodaran_2026_01/.
     Public, but one aggregate per industry: a sector's range is the 25th-75th
     percentile ACROSS the Damodaran industries mapped to it, not across companies.
  2. A CSV of your own comparables (--csv): one row per company or deal, true
     25th-75th percentiles per sector (used instead of Damodaran where available).

What Damodaran is used for, and what not (checked on the data, see comparison()):
  - EBITDA margin, capex, D&A, NWC: yes. Capex and D&A as % of sales use sales rebuilt
    from Damodaran's own definition, Net CapEx = CapEx - D&A + Acquisitions + Net R&D;
    the shortcut "EBITDA margin - EBIT margin" gave negative D&A for software.
  - Entry multiples: no. Listed large-cap multiples carry sector re-ratings (listed
    electrical equipment at ~19x) that mid-market deals do not: the Argos Index Q1 2026
    has a 10.0x median for fund deals and only 6% of deals above 15x.
  - Revenue growth: no. Damodaran's is analysts' 5-year consensus for large caps in USD
    (~13% for machinery), far above a mid-market base case.
  - Leverage: no public sector breakdown (LCD reports ~4.6-5.3x for European LBOs overall).
These stay as set in sector_benchmarks.py unless your CSV provides them.

Usage:
    uv run python calibrate_benchmarks.py --download      # fetch the raw Damodaran files (not redistributed)
    uv run python calibrate_benchmarks.py                 # print old vs proposed, change nothing
    uv run python calibrate_benchmarks.py --write         # save data/sector_benchmarks.json
    uv run python calibrate_benchmarks.py --csv comps.csv --write
"""

import argparse
import csv
import json
import statistics
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import xlrd

from sector_benchmarks import HAND_SET as SECTORS   # always compare to the hand-set values

DATA_DIR = Path(__file__).parent / "data" / "damodaran_2026_01"
DAMODARAN_URL = "https://pages.stern.nyu.edu/~adamodar/pc/datasets/{name}.xls"
DAMODARAN_FILES = ["vebitdaEurope", "marginEurope", "capexEurope", "wcdataEurope", "histgrEurope"]
OUTPUT = Path(__file__).parent / "data" / "sector_benchmarks.json"

ARGOS_PE_MEDIAN = 10.0
ARGOS_SOURCE = ("Argos Index mid-market Q1 2026 (published 20 May 2026): median EV/EBITDA paid by "
                "investment funds 10.0x, eurozone unlisted SMEs - argos.fund")
DAMODARAN_SOURCE = "Damodaran Online, Europe industry datasets, updated 9 Jan 2026 - pages.stern.nyu.edu/~adamodar"

# Our sectors -> Damodaran Europe industries. Deliberately left out: industries whose
# listed profile is far from a mid-market LBO target (e.g. Aerospace/Defense at ~21x).
SECTOR_MAP: Dict[str, List[str]] = {
    "industrials": ["Machinery", "Electrical Equipment", "Auto Parts", "Packaging & Container",
                    "Electronics (General)"],
    "software_saas": ["Software (System & Application)", "Software (Internet)"],
    "healthcare_services": ["Healthcare Support Services", "Hospitals/Healthcare Facilities"],
    "business_services": ["Business & Consumer Services", "Computer Services", "Information Services",
                          "Environmental & Waste Services", "Office Equipment & Services", "Advertising"],
    "consumer_retail": ["Retail (General)", "Retail (Special Lines)", "Retail (Grocery and Food)",
                        "Retail (Distributors)", "Retail (Building Supply)", "Restaurant/Dining"],
    "consumer_brands_luxury": ["Apparel", "Shoe", "Household Products", "Furn/Home Furnishings", "Recreation"],
    # Food Wholesalers left out: negative NWC and ~2% margin are a distributor's, not a producer's.
    "food_beverage": ["Food Processing", "Beverage (Alcoholic)", "Beverage (Soft)"],
    "chemicals": ["Chemical (Specialty)", "Chemical (Diversified)", "Chemical (Basic)"],
    "logistics_transport": ["Transportation", "Trucking"],
    "building_materials": ["Building Materials", "Construction Supplies", "Engineering/Construction"],
    "telecom_infra": ["Telecom. Services", "Telecom (Wireless)"],   # Cable TV: 2 firms, D&A ~50% of sales
    "education": ["Education"],
    "other": ["Total Market (without financials)"],
}

METRICS = ["entry_ev_multiple", "ebitda_margin", "revenue_growth", "capex_pct_revenue", "da_pct_revenue",
           "nwc_pct_of_rev_growth"]
DAMODARAN_METRICS = ["ebitda_margin", "capex_pct_revenue", "da_pct_revenue", "nwc_pct_of_rev_growth"]
# Which Damodaran metrics are applied per sector, and why the others are not (reviewed with
# the user on 2026-09-23). Sectors not listed keep every hand-set range.
_ALL = ["ebitda_margin", "capex_pct_revenue", "da_pct_revenue", "nwc_pct_of_rev_growth"]
APPLY: Dict[str, List[str]] = {
    "industrials": _ALL,
    "food_beverage": ["ebitda_margin", "capex_pct_revenue", "da_pct_revenue"],
    "business_services": ["capex_pct_revenue", "nwc_pct_of_rev_growth"],
    "building_materials": _ALL, "consumer_brands_luxury": _ALL,
    "consumer_retail": ["nwc_pct_of_rev_growth"],
    "logistics_transport": ["ebitda_margin", "capex_pct_revenue", "nwc_pct_of_rev_growth"],
    "chemicals": ["capex_pct_revenue", "da_pct_revenue", "nwc_pct_of_rev_growth"],
    "healthcare_services": ["capex_pct_revenue", "nwc_pct_of_rev_growth"],
    "telecom_infra": ["capex_pct_revenue", "nwc_pct_of_rev_growth"],
}
NOT_APPLIED_BECAUSE = {
    "consumer_retail": "capex/D&A: listed D&A includes IFRS 16 right-of-use depreciation; margin: the "
                       "listed mix is dragged down by grocery (~5%) and clamped a 12% specialty retailer "
                       "and an 18% gym chain in real runs",
    "food_beverage": "NWC: soft drinks run negative working capital; a producer with inventory and export "
                     "(e.g. pasta, ~18%) would be clamped well below its real cash absorption",
    "logistics_transport": "D&A: IFRS 16 right-of-use depreciation",
    "chemicals": "margin: 2025 is the trough of the European chemicals cycle; a 5-year base case needs mid-cycle",
    "healthcare_services": "margin/D&A: listed hospitals are not clinic chains; IFRS 16",
    "telecom_infra": "margin: listed telcos are not digital infrastructure (towers, fibre); D&A: above the "
                     "capex range (spectrum amortisation, IFRS 16), would flag every consistent estimate",
    "business_services": "margin: listed mix includes low-margin staffing/outsourcing (flagged a normal 14% IT "
                         "services margin); D&A: IFRS 16 and acquired intangibles put it above the capex range",
    "software_saas": "all: listed large-cap aggregate (17-22% margin) is far from PE-owned SaaS; D&A includes "
                     "acquired-intangible amortisation and capitalised R&D",
    "education": "all: one industry of 18 listed firms, NWC sign opposite to sector economics",
    "other": "all: must stay wide by design (unclassified companies)",
}

# A range built from 1-2 industries (or tightly clustered ones) would be too narrow to use as a
# guardrail: never narrower than this, widened symmetrically around the data centre.
MIN_WIDTH = {"entry_ev_multiple": 2.0, "ebitda_margin": 0.05, "revenue_growth": 0.03,
             "capex_pct_revenue": 0.015, "da_pct_revenue": 0.015, "nwc_pct_of_rev_growth": 0.10}
ROUND_TO = {"entry_ev_multiple": 0.25, "ebitda_margin": 0.005, "revenue_growth": 0.005,
            "capex_pct_revenue": 0.0025, "da_pct_revenue": 0.0025, "nwc_pct_of_rev_growth": 0.01}


def download_damodaran() -> None:
    """The raw files are Damodaran's: they are downloaded from his site, not shipped with the repo.
    Note: he updates them every January, so a later download may differ from the 2026-01 data
    the saved ranges were built on."""
    import urllib.request
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for name in DAMODARAN_FILES:
        urllib.request.urlretrieve(DAMODARAN_URL.format(name=name), DATA_DIR / f"{name}.xls")
        print(f"downloaded {name}.xls")


def damodaran_available() -> bool:
    return all((DATA_DIR / f"{n}.xls").exists() for n in DAMODARAN_FILES)


def _sheet(name: str):
    return xlrd.open_workbook(str(DATA_DIR / f"{name}.xls")).sheet_by_name("Industry Averages")


def _table(sheet, header_row: int) -> Dict[str, Dict[str, float]]:
    headers = [str(c.value).strip() for c in sheet.row(header_row)]
    out = {}
    for r in range(header_row + 1, sheet.nrows):
        name = str(sheet.cell(r, 0).value).strip()
        if name:
            out[name] = {h: sheet.cell(r, i).value for i, h in enumerate(headers) if i}
    return out


def load_damodaran() -> Dict[str, Dict[str, float]]:
    """Industry -> our metric names, derived from the five Damodaran files."""
    margin = _table(_sheet("marginEurope"), 8)
    capex = _table(_sheet("capexEurope"), 7)
    wc = _table(_sheet("wcdataEurope"), 7)
    growth = _table(_sheet("histgrEurope"), 7)
    multiples_sheet = _sheet("vebitdaEurope")
    # Two blocks (positive-EBITDA firms / all firms) share header names: take the first EV/EBITDA.
    headers = [str(c.value).strip() for c in multiples_sheet.row(8)]
    ev_col = headers.index("EV/EBITDA")
    multiples = {str(multiples_sheet.cell(r, 0).value).strip(): multiples_sheet.cell(r, ev_col).value
                 for r in range(9, multiples_sheet.nrows) if multiples_sheet.cell(r, 0).value}

    out = {}
    for name, m in margin.items():
        try:
            c = capex[name]
            cx, da = float(c["Capital Expenditures (US $ millions)"]), float(c["Depreciation & Amort ((US $ millions)"])
            net_capex = cx - da + float(c["Acquisitions (US $ millions)"]) + float(c["Net R&D (US $ millions)"])
            sales = net_capex / float(c["Net Cap Ex/Sales"])
            if sales <= 0:
                raise ValueError("cannot rebuild sales")
            out[name] = {
                "ebitda_margin": float(m["EBITDA/Sales"]),
                "da_pct_revenue": da / sales,
                "capex_pct_revenue": cx / sales,
                "nwc_pct_of_rev_growth": float(wc[name]["Non-cash WC/ Sales"]),
                "revenue_growth": float(growth[name]["Expected Growth in Revenues - Next 5 years"]),
                "listed_ev_ebitda": float(multiples[name]),
                "firms": int(float(m["Number of firms"])),
            }
        except (KeyError, ValueError, TypeError, ZeroDivisionError):
            continue            # an industry missing from one file, or '' cells: skip it
    return out


def _percentile(values: List[float], q: float) -> float:
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[int(q) - 1]


def _to_range(metric: str, values: List[float], lo_q: float, hi_q: float) -> Tuple[float, float]:
    lo, hi = _percentile(values, lo_q), _percentile(values, hi_q)
    if hi - lo < MIN_WIDTH[metric]:
        mid = (lo + hi) / 2
        lo, hi = mid - MIN_WIDTH[metric] / 2, mid + MIN_WIDTH[metric] / 2
    step = ROUND_TO[metric]
    lo, hi = round(lo / step) * step, round(hi / step) * step
    if metric in ("ebitda_margin", "capex_pct_revenue", "da_pct_revenue"):
        lo = max(lo, step)      # cannot be zero or negative
    return round(lo, 4), round(hi, 4)


def from_damodaran(lo_q: float = 25, hi_q: float = 75) -> Dict[str, dict]:
    data = load_damodaran()
    result = {}
    for sector, industries in SECTOR_MAP.items():
        rows = [data[i] for i in industries if i in data]
        missing = [i for i in industries if i not in data]
        ranges = {metric: _to_range(metric, [r[metric] for r in rows], lo_q, hi_q)
                  for metric in APPLY.get(sector, [])}
        if not ranges:
            continue
        result[sector] = {
            "ranges": ranges,
            "source": (f"{DAMODARAN_SOURCE}; {len(rows)} industries "
                       f"({sum(r['firms'] for r in rows)} listed firms): {', '.join(i for i in industries if i in data)}"
                       + (f"; missing: {missing}" if missing else "")),
            "method": f"P{lo_q:g}-P{hi_q:g} across mapped industries, min widths {MIN_WIDTH}",
            **({"not_applied": NOT_APPLIED_BECAUSE[sector]} if sector in NOT_APPLIED_BECAUSE else {}),
        }
    return result


def from_csv(path: str, lo_q: float = 25, hi_q: float = 75, min_obs: int = 5) -> Dict[str, dict]:
    """Your comparables: columns `sector` plus any of METRICS (and `total_leverage_x`).
    Percentages as decimals. Sectors with fewer than `min_obs` values for a metric are skipped."""
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    result: Dict[str, dict] = {}
    for sector in sorted({r["sector"] for r in rows}):
        if sector not in SECTORS:
            raise ValueError(f"Unknown sector {sector!r} in {path}; valid: {sorted(SECTORS)}")
        ranges = {}
        for metric in METRICS + ["total_leverage_x"]:
            values = [float(r[metric]) for r in rows if r["sector"] == sector and r.get(metric, "").strip()]
            if len(values) >= min_obs:
                if metric == "total_leverage_x":
                    lo, hi = _percentile(values, lo_q), _percentile(values, hi_q)
                    ranges[metric] = (round(lo * 4) / 4, round(hi * 4) / 4)
                else:
                    ranges[metric] = _to_range(metric, values, lo_q, hi_q)
        if ranges:
            n = sum(1 for r in rows if r["sector"] == sector)
            result[sector] = {"ranges": ranges, "source": f"User comparables {Path(path).name} ({n} rows)",
                              "method": f"P{lo_q:g}-P{hi_q:g} across companies/deals"}
    return result


def merge(base: Dict[str, dict], override: Dict[str, dict]) -> Dict[str, dict]:
    out = {k: {**v, "ranges": dict(v["ranges"])} for k, v in base.items()}
    for sector, v in override.items():
        entry = out.setdefault(sector, {"ranges": {}, "source": "", "method": ""})
        entry["ranges"].update(v["ranges"])
        entry["source"] = f"{v['source']} (for {', '.join(v['ranges'])}); otherwise {entry['source']}"
    return out


def comparison(proposed: Dict[str, dict]) -> str:
    fmt = lambda m, r: (f"{r[0]:.2f}-{r[1]:.2f}x" if m in ("entry_ev_multiple", "total_leverage_x")
                        else f"{r[0] * 100:.1f}-{r[1] * 100:.1f}%")
    lines = []
    for sector, v in proposed.items():
        lines.append(f"\n{sector}")
        for metric, new in v["ranges"].items():
            old = getattr(SECTORS[sector], metric)
            lines.append(f"  {metric:24} {fmt(metric, old):>16}  ->  {fmt(metric, new):<16}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Calibrate sector benchmark ranges from data.")
    ap.add_argument("--csv", help="Your comparables CSV (overrides Damodaran where it has enough rows)")
    ap.add_argument("--low", type=float, default=25)
    ap.add_argument("--high", type=float, default=75)
    ap.add_argument("--download", action="store_true", help="Download the raw Damodaran Europe files first")
    ap.add_argument("--write", action="store_true", help=f"Save to {OUTPUT.relative_to(Path.cwd()) if OUTPUT.is_relative_to(Path.cwd()) else OUTPUT}")
    args = ap.parse_args(argv)

    if args.download:
        download_damodaran()
    if not damodaran_available():
        ap.error(f"raw Damodaran files missing in {DATA_DIR}: run with --download")
    proposed = from_damodaran(args.low, args.high)
    if args.csv:
        proposed = merge(proposed, from_csv(args.csv, args.low, args.high))
    print(comparison(proposed))
    if args.write:
        kept = {k: v for k, v in NOT_APPLIED_BECAUSE.items() if k not in proposed}
        OUTPUT.write_text(json.dumps({"sectors": proposed, "kept_hand_set": kept,
                                      "not_calibrated_anywhere": {
                                          "entry_ev_multiple": "listed multiples not representative; hand-set "
                                                               "ranges checked against " + ARGOS_SOURCE,
                                          "revenue_growth": "only large-cap analyst consensus available",
                                          "total_leverage_x": "no public sector data; LCD ~4.6-5.3x overall"}},
                                     indent=2, ensure_ascii=False))
        print(f"\nSaved {OUTPUT}")


if __name__ == "__main__":
    main()
