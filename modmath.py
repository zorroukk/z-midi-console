"""modmath — pure modulation math (curves + BPM/tick grid).

Stdlib-only, deterministic, no I/O. Property-tested (test_modmath.py). All curve
functions map 0.0 -> 0.0 and 1.0 -> 1.0 unless inherently discrete (step/snapping
preserve endpoint exactness by design).
"""
from __future__ import annotations

import math

# ---------------------------------------------------------------- curves ----

CURVES = ("lin", "exp", "log", "s", "step4", "snap12")
# 'sh' (sample-and-hold, borrowed from the LMMS 1.3 LFO controller and rewritten
# here): a new deterministic pseudo-random level each cycle — the classic
# random-stepped filter. Deterministic per cycle index so runs are reproducible
# and testable (no RNG state, no seeding).
LFO_WAVES = ("sine", "tri", "saw", "sh")


def curve(name: str, x: float) -> float:
    """Normalize x in [0,1] through the named shaping curve -> [0,1]."""
    if not (0.0 <= x <= 1.0):  # callers must clamp upstream; refuse loud
        raise ValueError(f"curve input out of range: {x}")
    if name == "lin":
        return x
    if name == "exp":
        return x * x
    if name == "log":
        return 1.0 - (1.0 - x) * (1.0 - x)
    if name == "s":  # smoothstep
        return x * x * (3.0 - 2.0 * x)
    if name == "step4":
        return math.floor(x * 4.0) / 3.0 if x < 1.0 else 1.0
    if name == "snap12":  # 12 musical detents, endpoints exact
        return round(x * 12.0) / 12.0
    raise ValueError(f"unknown curve: {name}")


# ------------------------------------------------------------ grid/BPM ------

TPQ = 1920  # MIDI ticks per quarter note for export


def ms_to_ticks(t_ms: float, bpm: float) -> int:
    return int(round(t_ms / 60000.0 * bpm * TPQ))
