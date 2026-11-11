"""v0.5 SMF export battery: byte-level truths + MIDO PARSE ROUNDTRIP (the real
proof: a third-party parser must see exactly the notes/CCs/ticks we wrote).
mido delivers DELTA TICKS for extra type-1 tracks here - this battery
accumulates in ticks and asserts tick-exact (stricter than seconds)."""
import io
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from smf import varlen, format1, TPQ  # noqa: E402
from modmath import ms_to_ticks  # noqa: E402
import mido  # noqa: E402

fails = []
def ok(c, m):
    if not c: fails.append(m)

# ---- varlen edges (decode-verified, not memorized) ----
def decode_vl(b):
    v = 0
    for x in b:
        v = (v << 7) | (x & 0x7F)
    return v
for n in (0, 0x40, 0x7F, 0x80, 0x3FFF, 0x200000, 12345678):
    enc = varlen(n)
    ok(decode_vl(enc) == n and enc[-1] & 0x80 == 0 and all(c & 0x80 for c in enc[:-1]),
       f"varlen roundtrip {n}: {enc.hex()}")

PAT = {"schema": "mfpat/1", "id": "L00001", "name": "teeter test", "bpm": 120.0, "spb": 4,
       "bars": 2,
       "notes": [{"t_ms": 0.0, "dur": 250.0, "note": 60, "vel": 96, "channel": 3},
                 {"t_ms": 250.0, "dur": 250.0, "note": 63, "vel": 100, "channel": 3},
                 {"t_ms": 1000.0, "dur": 2000.0, "note": 67, "vel": 80, "channel": 3},
                 {"t_ms": 2000.0, "dur": 100.0, "note": 127, "vel": 127, "channel": 3}],
       "cc_tracks": [{"cc": 23, "channel": 1, "points": [{"t_ms": 0.0, "v": 10, "curve": "lin"},
                                                          {"t_ms": 1000.0, "v": 90, "curve": "lin"}]}]}

blob = format1(PAT)
ok(blob[:4] == b"MThd", "MThd")
hlen = struct.unpack(">L", blob[4:8])[0]
fmt, ntrk, res = struct.unpack(">HHH", blob[8:14])
ok(hlen == 6 and fmt == 1 and ntrk == 2 and res == TPQ, f"header {hlen} {fmt} {ntrk} {res}")
ok(blob[14:18] == b"MTrk", "track0 MTrk")   # 4 tag + 4 len + 6 payload

mf = mido.MidiFile(file=io.BytesIO(blob))
ok(mf.type == 1 and mf.ticks_per_beat == TPQ, "mido: type1/tpq")
ok(mf.length and abs(mf.length - 3.0) < 1e-6, f"tempo math: {mf.length}s (want 3.0 = last note-off @120bpm)")
notes, ccs = {}, []
bad_ch = []
t = 0                                    # accumulated TICKS
for msg in mf.tracks[1]:
    t += int(round(float(msg.time)))      # mido delivers delta TICKS for extra tracks
    if msg.type == "note_on":
        notes.setdefault(msg.note, []).append((t, msg.velocity))
        if msg.channel != 3: bad_ch.append(("note", msg.note, msg.channel))
    elif msg.type == "note_off":
        notes.setdefault(msg.note, []).append((t, 0))
        if msg.channel != 3: bad_ch.append(("off", msg.note, msg.channel))
    elif msg.type == "control_change":
        ccs.append((t, msg.control, msg.value))
        if msg.channel != 1: bad_ch.append(("cc", msg.control, msg.channel))
ok(not bad_ch, f"channels must survive the roundtrip (notes ch3, CC ch1): {bad_ch}")
ok(len(ccs) == 2, f"CC points written EXACTLY (no grid resample): {ccs}")
for nt in PAT["notes"]:
    tk_on = ms_to_ticks(nt["t_ms"], 120.0)
    tk_off = ms_to_ticks(nt["t_ms"] + nt["dur"], 120.0)
    got = notes.get(nt["note"], [])
    ok(any(g[1] > 0 and g[0] == tk_on for g in got), f"note {nt['note']} on @{tk_on}: {got}")
    ok(any(g[1] == 0 and g[0] == tk_off for g in got), f"note {nt['note']} off @{tk_off}: {got}")
ok(ccs[0][1:] == (23, 10) and ccs[0][0] == 0, f"CC first point: {ccs[:2]}")
ok(ccs[1] == (ms_to_ticks(1000.0, 120.0), 23, 90),
   f"CC ride point at EXACT tick (live fidelity): {ccs[1]}")
ok(all(c[1] == 23 for c in ccs), "only CC23 present")

# seam: same-tick OFF must precede ON (legato retrigger must not die)
seam = format1({"schema": "mfpat/1", "id": "s", "name": "seam", "bpm": 120.0, "spb": 4, "bars": 1,
                "notes": [{"t_ms": 0.0, "dur": 500.0, "note": 60, "vel": 90},
                          {"t_ms": 500.0, "dur": 250.0, "note": 60, "vel": 90}]})
seq = [m for m in mido.MidiFile(file=io.BytesIO(seam)).tracks[1] if m.type.startswith("note")]
ok([m.type for m in seq] == ["note_on", "note_off", "note_on", "note_off"],
   f"seam order: {[m.type for m in seq]}")
ok(float(seq[2].time) == 0.0 and float(seq[1].time) > 0.0, "off lands, same-tick on follows with zero delta")

# refuse-don't-mask: bad channel / note / cc raise, never wrap
base = {"bpm": 120.0, "bars": 1, "notes": [], "cc_tracks": []}
for bad, why in (({"notes": [{"t_ms": 0.0, "dur": 10.0, "note": 128, "vel": 9}]}, "note 128"),
                 ({"notes": [{"t_ms": 0.0, "dur": 10.0, "note": 60, "vel": 9, "channel": 16}]}, "ch 16"),
                 ({"cc_tracks": [{"cc": 129, "points": [{"t_ms": 0.0, "v": 5}]}]}, "cc 129"),
                 ({"cc_tracks": [{"cc": 23, "channel": -1, "points": [{"t_ms": 0.0, "v": 5}]}]}, "ch -1")):
    try:
        format1({**base, **bad})
        fails.append(f"{why} must raise, not mask")
    except ValueError:
        pass

if fails:
    print("FAILURES:"); [print(" -", f) for f in fails]; raise SystemExit(1)
print("ALL OK — smf battery (varlen decode-verified, header, tick-exact mido note/CC roundtrip, grid density, seam order)")
