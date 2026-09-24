import os

# Guardrail tests assert hand-set range values: keep calibrated data out of the unit suite.
# The calibrated file itself is tested in test_calibration.py.
os.environ.setdefault("LBO_BENCHMARKS_FILE", "none")
