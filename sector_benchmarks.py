"""
Sector benchmark ranges used as guardrails on generated assumptions.

The ranges below are hand-set heuristics for a European mid-market LBO base case.
Where public data is representative, calibrate_benchmarks.py replaces them field by
field with ranges from Damodaran's Europe datasets (saved, with sources, in
data/sector_benchmarks.json); SOURCES says which is which for every sector. Entry
multiples, growth and leverage stay hand-set (no representative public sector data).
They exist to catch implausible LLM output, not to price a real deal.

All percentages are decimals (0.04 = 4%). Leverage and multiples are x EBITDA.
"""

import dataclasses
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple

Range = Tuple[float, float]

# Reference base rate (3M EURIBOR) the financing ranges are built around.
# Update when rates move; senior/sub/RCF ranges below assume ~2%.
REFERENCE_BASE_RATE = 0.02


@dataclass(frozen=True)
class SectorBenchmark:
    label: str
    entry_ev_multiple: Range
    ebitda_margin: Range
    revenue_growth: Range
    capex_pct_revenue: Range
    da_pct_revenue: Range
    nwc_pct_of_rev_growth: Range
    total_leverage_x: Range


SECTORS: Dict[str, SectorBenchmark] = {
    "industrials": SectorBenchmark(
        "Industrials / manufacturing", (7.0, 10.0), (0.10, 0.20), (0.02, 0.06),
        (0.025, 0.05), (0.025, 0.045), (0.10, 0.25), (4.0, 5.5)),
    "software_saas": SectorBenchmark(
        "Software / SaaS", (12.0, 20.0), (0.25, 0.45), (0.08, 0.20),
        (0.01, 0.04), (0.01, 0.04), (-0.10, 0.05), (5.0, 7.0)),
    "healthcare_services": SectorBenchmark(
        "Healthcare services", (10.0, 14.0), (0.12, 0.22), (0.04, 0.08),
        (0.02, 0.05), (0.02, 0.045), (0.05, 0.15), (5.0, 6.5)),
    "business_services": SectorBenchmark(
        "Business services", (9.0, 13.0), (0.12, 0.25), (0.04, 0.08),
        (0.01, 0.03), (0.01, 0.03), (0.05, 0.15), (4.5, 6.0)),
    "consumer_retail": SectorBenchmark(
        "Consumer / retail", (7.0, 10.0), (0.08, 0.16), (0.02, 0.06),
        (0.03, 0.06), (0.025, 0.05), (0.05, 0.15), (3.5, 5.0)),
    "consumer_brands_luxury": SectorBenchmark(
        "Consumer brands / luxury / fashion", (10.0, 15.0), (0.15, 0.30), (0.04, 0.10),
        (0.03, 0.06), (0.025, 0.05), (0.15, 0.30), (4.0, 5.5)),
    "food_beverage": SectorBenchmark(
        "Food & beverage", (9.0, 13.0), (0.10, 0.20), (0.02, 0.05),
        (0.03, 0.06), (0.025, 0.045), (0.08, 0.20), (4.5, 6.0)),
    "chemicals": SectorBenchmark(
        "Specialty chemicals", (7.0, 10.0), (0.12, 0.22), (0.02, 0.05),
        (0.04, 0.07), (0.035, 0.06), (0.12, 0.25), (4.0, 5.0)),
    "logistics_transport": SectorBenchmark(
        "Logistics / transport", (7.0, 10.0), (0.06, 0.14), (0.03, 0.06),
        (0.03, 0.08), (0.03, 0.07), (0.05, 0.15), (3.5, 5.0)),
    "building_materials": SectorBenchmark(
        "Building materials / construction", (6.0, 9.0), (0.12, 0.22), (0.02, 0.05),
        (0.04, 0.07), (0.03, 0.06), (0.10, 0.20), (3.5, 5.0)),
    "telecom_infra": SectorBenchmark(
        "Telecom / digital infrastructure", (9.0, 14.0), (0.30, 0.50), (0.02, 0.06),
        (0.12, 0.22), (0.10, 0.20), (0.00, 0.10), (5.0, 7.0)),
    "education": SectorBenchmark(
        "Education / training", (10.0, 14.0), (0.15, 0.30), (0.04, 0.10),
        (0.03, 0.07), (0.03, 0.06), (-0.10, 0.05), (4.5, 6.0)),
    # Added after a real run (a premium gym chain) was classified as retail and had its 18%
    # margin clamped to retail levels. Hand-set: no representative public dataset.
    "leisure_fitness": SectorBenchmark(
        "Leisure & fitness (gyms, wellness, attractions, cinemas)", (8.0, 12.0), (0.15, 0.30), (0.03, 0.08),
        (0.06, 0.12), (0.05, 0.10), (-0.15, 0.00), (4.0, 5.5)),
    "other": SectorBenchmark(
        "Other / unclear (wide ranges)", (6.0, 14.0), (0.05, 0.40), (0.00, 0.12),
        (0.01, 0.12), (0.01, 0.10), (-0.10, 0.30), (3.0, 6.5)),
}

HAND_SET = dict(SECTORS)   # before calibration: what calibrate_benchmarks.py compares against

# Hand-set ranges above are the fallback. Ranges calibrated from data (calibrate_benchmarks.py)
# live in data/sector_benchmarks.json with their sources and replace them field by field.
# LBO_BENCHMARKS_FILE=none disables the file (the test suite pins the hand-set values).
BENCHMARKS_FILE = Path(os.environ.get("LBO_BENCHMARKS_FILE",
                                      Path(__file__).parent / "data" / "sector_benchmarks.json"))
SOURCES: Dict[str, str] = {k: "Hand-set heuristic ranges (no data source)" for k in SECTORS}


def load_overrides(path: Path, sectors: Dict[str, SectorBenchmark]):
    """Return (sectors with calibrated fields replaced, sources per sector)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out, sources = dict(sectors), {}
    for key, entry in data.get("sectors", {}).items():
        if key not in out:
            raise ValueError(f"{path}: unknown sector {key!r}")
        fields = {m: tuple(r) for m, r in entry["ranges"].items()}
        bad = [m for m, (lo, hi) in fields.items() if lo > hi]
        if bad:
            raise ValueError(f"{path}: {key} has low > high for {bad}")
        out[key] = dataclasses.replace(out[key], **fields)
        sources[key] = f"{entry['source']} [{', '.join(fields)}]" + (
            f"; kept hand-set: {entry['not_applied']}" if entry.get("not_applied") else "")
    return out, sources


if str(BENCHMARKS_FILE).lower() != "none" and BENCHMARKS_FILE.exists():
    SECTORS, _calibrated = load_overrides(BENCHMARKS_FILE, SECTORS)
    SOURCES.update(_calibrated)


# Sector-independent ranges (financing terms, tax, deal mechanics).
GLOBAL_RANGES: Dict[str, Range] = {
    "senior_rate": (0.05, 0.09),          # base + ~300-650bps (TLB / unitranche)
    "sub_rate": (0.08, 0.14),             # HY / mezzanine / 2nd lien
    "rcf_rate": (0.045, 0.08),
    "senior_mandatory_amort_pct": (0.0, 0.10),
    "cash_sweep_pct": (0.5, 1.0),
    "tax_rate": (0.15, 0.35),
    "hold_period_years": (3, 7),
    "transaction_fees_pct_ev": (0.01, 0.03),       # M&A advisory, legal, DD
    "financing_fees_pct_debt": (0.015, 0.035),     # arrangement / underwriting
    "senior_oid_pct": (0.0, 0.015),                # TLB typically issued at 98.5-100
    "fee_amortization_years": (5, 7),              # debt tenor
}

# Relative ranges, expressed against other assumptions.
SENIOR_SHARE_OF_TOTAL_LEVERAGE: Range = (0.5, 1.0)   # 1.0 = unitranche, no sub notes
MIN_SUB_SPREAD_OVER_SENIOR = 0.015                    # sub debt must price >= senior + 150bps
MIN_EQUITY_PCT_OF_USES = 0.30                         # typical floor on sponsor equity cheque
RCF_COMMITMENT_X_EBITDA: Range = (0.25, 1.5)
MIN_CASH_PCT_REVENUE: Range = (0.005, 0.05)


def benchmark_table_for_prompt() -> str:
    """Render all ranges as a compact text table for the LLM system prompt."""
    pct = lambda r: f"{r[0]*100:.1f}-{r[1]*100:.1f}%"
    mult = lambda r: f"{r[0]:.1f}-{r[1]:.1f}x"
    lines = ["sector | EV/EBITDA | EBITDA margin | rev growth | capex % rev | D&A % rev | NWC % of rev growth | total leverage"]
    for key, s in SECTORS.items():
        lines.append(f"{key} ({s.label}) | {mult(s.entry_ev_multiple)} | {pct(s.ebitda_margin)} | "
                     f"{pct(s.revenue_growth)} | {pct(s.capex_pct_revenue)} | {pct(s.da_pct_revenue)} | "
                     f"{pct(s.nwc_pct_of_rev_growth)} | {mult(s.total_leverage_x)}")
    g = GLOBAL_RANGES
    lines += [
        "",
        f"All sectors: senior rate {pct(g['senior_rate'])}, sub rate {pct(g['sub_rate'])} "
        f"(>= senior + {MIN_SUB_SPREAD_OVER_SENIOR*1e4:.0f}bps), RCF rate {pct(g['rcf_rate'])}, "
        f"TL amortization {pct(g['senior_mandatory_amort_pct'])} of original principal p.a., "
        f"cash sweep {pct(g['cash_sweep_pct'])}, tax {pct(g['tax_rate'])}, "
        f"hold {g['hold_period_years'][0]}-{g['hold_period_years'][1]} years, "
        f"senior = {SENIOR_SHARE_OF_TOTAL_LEVERAGE[0]*100:.0f}-100% of total leverage, "
        f"sponsor equity >= {MIN_EQUITY_PCT_OF_USES*100:.0f}% of uses, "
        f"RCF commitment {mult(RCF_COMMITMENT_X_EBITDA)} EBITDA, minimum cash {pct(MIN_CASH_PCT_REVENUE)} of revenue, "
        f"M&A fees {pct(g['transaction_fees_pct_ev'])} of EV, financing fees {pct(g['financing_fees_pct_debt'])} "
        f"of debt, Term Loan OID {pct(g['senior_oid_pct'])}, amortised over {g['fee_amortization_years'][0]}-"
        f"{g['fee_amortization_years'][1]} years.",
        f"Reference base rate (3M EURIBOR): {REFERENCE_BASE_RATE*100:.1f}%.",
    ]
    return "\n".join(lines)
