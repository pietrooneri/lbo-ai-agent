"""Calibrated benchmark ranges: the saved file, its loader, and the selection rules."""

import json

import pytest

import calibrate_benchmarks as cb
from sector_benchmarks import HAND_SET, load_overrides

SAVED = cb.OUTPUT
needs_raw_data = pytest.mark.skipif(not cb.damodaran_available(),
                                    reason="raw Damodaran files not downloaded (calibrate_benchmarks.py --download)")


@pytest.fixture(scope="module")
def calibrated():
    return load_overrides(SAVED, HAND_SET)


@needs_raw_data
def test_saved_file_matches_a_fresh_calibration():
    """data/sector_benchmarks.json is exactly what the script produces from the raw files."""
    assert json.loads(SAVED.read_text())["sectors"] == json.loads(json.dumps(cb.from_damodaran()))


def test_only_approved_fields_change(calibrated):
    sectors, _ = calibrated
    for key, hand in HAND_SET.items():
        applied = set(cb.APPLY.get(key, []))
        for field in ("entry_ev_multiple", "ebitda_margin", "revenue_growth", "capex_pct_revenue",
                      "da_pct_revenue", "nwc_pct_of_rev_growth", "total_leverage_x"):
            if field not in applied:
                assert getattr(sectors[key], field) == getattr(hand, field), (key, field)


def test_calibrated_values_are_sane(calibrated):
    sectors, sources = calibrated
    assert sectors["industrials"].ebitda_margin == (0.10, 0.15)
    assert sectors["software_saas"] == HAND_SET["software_saas"]           # rejected: listed large caps
    assert sectors["chemicals"].ebitda_margin == HAND_SET["chemicals"].ebitda_margin   # cycle trough
    for key, s in sectors.items():
        for field in ("ebitda_margin", "capex_pct_revenue", "da_pct_revenue"):
            lo, hi = getattr(s, field)
            assert 0 < lo < hi < 0.6, (key, field)
    assert "Damodaran" in sources["industrials"] and "IFRS 16" in sources["consumer_retail"]


@needs_raw_data
def test_capex_and_da_rebuild_is_plausible():
    """Sales are rebuilt from Net CapEx = CapEx - D&A + Acquisitions + Net R&D; the old shortcut
    (EBITDA margin - EBIT margin) gave negative D&A for software."""
    data = cb.load_damodaran()
    for name in ("Machinery", "Software (System & Application)", "Food Processing", "Chemical (Specialty)"):
        assert 0.01 < data[name]["da_pct_revenue"] < 0.12
        assert 0.01 < data[name]["capex_pct_revenue"] < 0.12


def test_csv_comparables_give_true_percentiles(tmp_path):
    rows = ["sector,ebitda_margin,total_leverage_x"] + [f"industrials,{m},{l}" for m, l in
                                                         [(0.08, 4.0), (0.12, 4.5), (0.16, 5.0), (0.20, 5.5), (0.24, 6.0)]]
    path = tmp_path / "comps.csv"
    path.write_text("\n".join(rows))
    out = cb.from_csv(str(path))["industrials"]["ranges"]
    assert out["ebitda_margin"] == (0.12, 0.20)          # P25-P75
    assert out["total_leverage_x"] == (4.5, 5.5)


def test_narrow_ranges_are_widened_to_the_minimum():
    assert cb._to_range("ebitda_margin", [0.12, 0.13, 0.14, 0.15, 0.16], 25, 75) == (0.115, 0.165)


@pytest.mark.parametrize("content, message", [
    ({"sectors": {"crypto": {"ranges": {}, "source": "x"}}}, "unknown sector"),
    ({"sectors": {"industrials": {"ranges": {"ebitda_margin": [0.2, 0.1]}, "source": "x"}}}, "low > high"),
])
def test_bad_benchmark_file_is_rejected(tmp_path, content, message):
    path = tmp_path / "b.json"
    path.write_text(json.dumps(content))
    with pytest.raises(ValueError, match=message):
        load_overrides(path, HAND_SET)
