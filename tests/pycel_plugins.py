"""Excel functions pycel lacks, used only to evaluate exported workbooks in tests."""
import numpy_financial as npf


def irr(values, guess=None):
    flat = [v for row in values for v in row if isinstance(v, (int, float))]
    return float(npf.irr(flat))
