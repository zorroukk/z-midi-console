"""Property battery for modmath (the shared math core: curves + BPM/tick grid).
Run: python tests/test_modmath.py"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modmath import CURVES, curve, ms_to_ticks, TPQ

fails = []
def ok(cond, msg):
    global fails
    if not cond:
        fails.append(msg)

# --- curves ---------------------------------------------------------------
ok(set(CURVES) == {"lin", "exp", "log", "s", "step4", "snap12"},
   f"curve keys wrong: {sorted(CURVES)}")
for name in CURVES:
    for x in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = curve(name, x)
        ok(0.0 <= y <= 1.0, f"{name}({x})={y} out of range")
ok(abs(curve("lin", 0.37) - 0.37) < 1e-9, "lin not identity")
ok(curve("exp", 0.5) < 0.5 and curve("log", 0.5) > 0.5, "exp/log asymmetry broken")
ok(abs(curve("s", 0.5) - 0.5) < 1e-9, "s(0.5) must be 0.5")
ok(abs(curve("step4", 0.24) - 0.0 / 3.0) < 1e-9 and abs(curve("step4", 0.26) - 1.0 / 3.0) < 1e-9,
   f"step4 floor grid: {curve('step4', 0.24)} {curve('step4', 0.26)}")
ok(abs(curve("snap12", 0.52) - 6.0 / 12.0) < 1e-9, "snap12 nearest twelfth")
for bad in (("nope", 0.4), ("lin", 2.0), ("lin", -0.1)):
    try:
        curve(*bad)
        fails.append(f"curve{bad} must raise (refuse-closed), not guess")
    except ValueError:
        pass
ok(curve("lin", 0.0) == 0.0 and curve("lin", 1.0) == 1.0, "endpoints exact")

# --- BPM / tick grid ------------------------------------------------------
ok(TPQ == 1920, f"TPQ must be 1920, got {TPQ}")
ok(ms_to_ticks(500.0, 120.0) == 1920, "one beat at 120bpm = 1920 ticks")
ok(ms_to_ticks(0.0, 120.0) == 0, "0ms = 0 ticks")
ok(ms_to_ticks(1000.0, 60.0) == 1920, "one beat at 60bpm = 1920 ticks")

print("FAILURES:", fails if fails else "none")
raise SystemExit(1 if fails else 0)
