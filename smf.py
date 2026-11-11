"""smf — Standard MIDI File (format 1) writer for recorded takes.

Pure byte math, no device IO. Track 0: tempo + name + end. Track 1: notes
and CC points on their OWN channels (status byte carries ch; refused not
masked if out of domain), CC rides at their EXACT ticks (a live ride keeps
its fidelity — no quantize resample). Ticks are grid-relative (TPQ 1920 from
modmath). This is the answer to "get what I played INTO FL Studio": FL imports
type-1 .mid natively; clips.json/loops.json are our schema and FL cannot read
them.
"""
from __future__ import annotations

import struct
from typing import List, Tuple

try:  # package context (agent half)
    from .modmath import TPQ, ms_to_ticks
except ImportError:  # standalone (dashboard half / tests)
    from modmath import TPQ, ms_to_ticks

_META_TEMPO = b"\xff\x51\x03"


def varlen(n: int) -> bytes:
    """MIDI delta-time variable length quantity."""
    if n < 0:
        raise ValueError("delta ticks cannot be negative")
    out = bytearray([n & 0x7F])
    n >>= 7
    while n:
        out.insert(0, (n & 0x7F) | 0x80)
        n >>= 7
    return bytes(out)


def _track(events: List[Tuple[int, bytes]]) -> bytes:
    """events: (abs_tick, payload). Sorts, deltas, wraps MTrk."""
    body = bytearray()
    last = 0
    for tick, payload in sorted(events, key=lambda e: e[0]):
        # (sorting IS the never-rewind invariant — review: the old
        # `tick = max(tick, last)` clamp could never fire and read as a
        # needed guard when it was dead code.)
        body += varlen(tick - last) + payload
        last = tick
    body += varlen(0) + b"\xff\x2f\x00"           # end of track
    return b"MTrk" + struct.pack(">L", len(body)) + bytes(body)


def format1(pattern: dict) -> bytes:
    bpm = float(pattern["bpm"])
    if not (10.0 <= bpm <= 999.0):
        raise ValueError("bpm out of domain")
    meta = [(0, _META_TEMPO + struct.pack(">I", max(1, int(round(60000000.0 / bpm))))[1:]),
            (0, b"\xff\x06\x00")]                 # marker at bar 1
    name = str(pattern.get("name", ""))[:32].encode("ascii", "replace")
    meta.append((0, bytes([0xFF, 0x03, len(name)]) + name))
    track_notes = []
    for nt in pattern.get("notes", []):
        on = ms_to_ticks(float(nt["t_ms"]), bpm)
        off = max(on + 1, ms_to_ticks(float(nt["t_ms"]) + float(nt["dur"]), bpm))
        n = int(nt["note"])
        if not (0 <= n <= 127):
            raise ValueError(f"note {n} out of domain (refused, not masked)")
        ch = int(nt.get("channel", 0))
        if not (0 <= ch <= 15):
            raise ValueError(f"channel {ch} out of domain (refused, not masked)")
        track_notes.append((off, bytes([0x80 | ch, n, 0])))           # off first at ties
        # regression review debt sweep: vel was CLAMPED while note/ch refused — one file,
        # two twins. refuse-don't-mask law everywhere.
        vel = int(nt.get("vel", 96))
        if not (1 <= vel <= 127):
            raise ValueError(f"vel {vel} out of domain 1..127 (refused, not masked)")
        track_notes.append((on, bytes([0x90 | ch, n, vel])))
    for tr in pattern.get("cc_tracks", []):
        cc = int(tr["cc"])
        if not (0 <= cc <= 127):
            raise ValueError(f"cc {cc} out of domain (refused, not masked)")
        ch = int(tr.get("channel", 0))
        if not (0 <= ch <= 15):
            raise ValueError(f"channel {ch} out of domain (refused, not masked)")
        last_v = None
        # exact-tick points (a live ride is NOT quantized); coalesce identical runs
        for p in sorted(tr.get("points", []), key=lambda p: float(p["t_ms"])):
            # regression review debt sweep: CC values were clamped while cc/channel refused.
            v = int(p["v"])
            if not (0 <= v <= 127):
                raise ValueError(f"cc value {v} out of domain (refused, not masked)")
            if v == last_v:
                continue
            last_v = v
            track_notes.append((ms_to_ticks(float(p["t_ms"]), bpm), bytes([0xB0 | ch, cc, v])))
    for tr in pattern.get("pc_tracks", []):
        # Review 2026-11-09 (device-agnostic): program changes ride their own
        # tracks so PC-bank devices (Cloudburst class) export whole.
        ch = int(tr.get("channel", 0))
        if not (0 <= ch <= 15):
            raise ValueError(f"channel {ch} out of domain (refused, not masked)")
        last_p = None
        for p in sorted(tr.get("points", []), key=lambda p: float(p["t_ms"])):
            v = int(p["v"])
            if not (0 <= v <= 127):
                raise ValueError(f"program {v} out of domain (refused, not masked)")
            if v == last_p:
                continue
            last_p = v
            track_notes.append((ms_to_ticks(float(p["t_ms"]), bpm), bytes([0xC0 | ch, v])))
    return (b"MThd" + struct.pack(">LHHH", 6, 1, 2, TPQ)
            + _track(meta) + _track(track_notes))
