"""Automation battery: Recorder take -> pattern -> .mid bytes. No MIDI device
needed (pure event feeding). Run: python tests/test_automation.py"""
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation import Recorder, export_mid  # noqa: E402

fails = []
def ok(cond, msg):
    global fails
    if not cond:
        fails.append(msg)

# --- refuse-closed ----------------------------------------------------------
r = Recorder()
ok(r.stop().get("error") == "not recording", "stop without start must refuse")
r.start()
ok(r.start().get("error") == "already recording", "double start must refuse")
ok(r.stop().get("error") == "nothing recorded", "empty take must refuse, not write")

# --- cc ride capture --------------------------------------------------------
r = Recorder()
r.start()
t0 = time.time()
for i, v in enumerate((0, 30, 64, 100, 127)):
    r.feed({"kind": "cc", "cc": 23, "value": v, "channel": 0, "ts": t0 + i * 0.25})
r.feed({"kind": "cc", "cc": 23, "value": 127, "channel": 0, "ts": t0 + 1.0})  # dup -> coalesced
r.feed({"kind": "cc", "cc": 83, "value": 40, "channel": 0, "ts": t0 + 0.5})
r.feed({"kind": "cc", "cc": 23, "value": 9, "channel": 1, "ts": t0 + 0.6})    # same CC, other ch = own ride
res = r.stop(bpm=120.0)
ok(res.get("ok"), f"stop failed: {res}")
pat = res.get("pattern", {})
ok(len(pat.get("cc_tracks", [])) == 3, f"want 3 cc tracks (ch identity counts), got {pat.get('cc_tracks')}")
cc23 = next(t for t in pat["cc_tracks"] if t["cc"] == 23 and t["channel"] == 0)
ok(len(cc23["points"]) == 5, f"dup must coalesce: {cc23['points']}")
ok(cc23["points"][0]["t_ms"] == 0.0, "first point at t=0")
cc23b = next(t for t in pat["cc_tracks"] if t["cc"] == 23 and t["channel"] == 1)
ok(len(cc23b["points"]) == 1, f"ch1 ride separate: {cc23b['points']}")

# --- notes with velocity + hanging-note close -------------------------------
r = Recorder()
r.start()
r.feed({"kind": "note", "note": 60, "velocity": 100, "channel": 3, "ts": t0})
r.feed({"kind": "note", "note": 60, "velocity": 0, "channel": 3, "ts": t0 + 0.5})
r.feed({"kind": "note", "note": 64, "velocity": 90, "channel": 3, "ts": t0 + 0.7})  # hangs
res = r.stop(bpm=120.0)
ok(res.get("notes") == 2, f"want 2 notes (one closed, one hung): {res}")
n0 = res["pattern"]["notes"][0]
ok(n0["note"] == 60 and n0["vel"] == 100 and abs(n0["dur"] - 500) < 1, f"note pairing: {n0}")
ok(n0.get("channel") == 3, f"note must carry its channel: {n0}")
ok(res["pattern"]["notes"][1]["dur"] >= 1, "hanging note gets a real duration")
ok(res["pattern"]["notes"][1].get("channel") == 3,
   f"hanging note must keep its channel: {res['pattern']['notes'][1]}")

# --- export: real SMF bytes -------------------------------------------------
path = export_mid(res["pattern"], "battery take")
data = Path(path).read_bytes()
ok(data[:4] == b"MThd", "missing MThd header")
fmt = struct.unpack(">HHH", data[6:12])[1]
ok(fmt == 1, f"format must be 1, got {fmt}")
ok(data[14:18] == b"MTrk", "missing first MTrk")
ok(b"take" in data, "track name missing")
Path(path).unlink(missing_ok=True)

# --- bad bpm refused BEFORE the take is consumed ----------------------------
r = Recorder()
r.start()
r.feed({"kind": "cc", "cc": 1, "value": 5, "ts": time.time()})
ok(r.stop(bpm=5).get("error") == "bpm must be 10-999", "bpm range guard")
ok(r.status().get("recording"), "bad bpm must NOT consume the take (still recording)")
ok(r.stop(bpm="abc").get("error") == "bpm must be a number", "junk bpm named error")
res = r.stop(bpm=120.0)
ok(res.get("ok") and res.get("cc_tracks") == 1, f"take survives bad bpm: {res}")

# --- take library: list / get / delete / containment ------------------------
from automation import delete_take, list_takes, read_take, take_path  # noqa: E402

r = Recorder()
r.start()
r.feed({"kind": "cc", "cc": 7, "value": 11, "ts": time.time()})
res = r.stop(bpm=120.0)
p1 = Path(export_mid(res["pattern"], "battery A"))
time.sleep(1.1)  # second-precision filenames must not collide
p2 = Path(export_mid(res["pattern"], "battery B"))
names = [t["name"] for t in list_takes()]
ok(p1.name in names and p2.name in names, f"takes must list: {names[:5]}")
rows = [t for t in list_takes() if t["name"] in (p1.name, p2.name)]
ok(rows[0]["name"] == p2.name, "list must be newest-first")
ok(all(t["size"] > 0 and t["mtime"] > 0 for t in rows), "take rows carry size/mtime")
ok(read_take(p2.name) == p2.read_bytes(), "read_take bytes match")
# containment law: one .mid inside exports dir, nothing else
for bad in ("", "../secret.mid", "sub/x.mid", "notes.txt", "nope.mid", "a\\b.mid"):
    try:
        take_path(bad)
        fails.append(f"take_path must refuse {bad!r}")
    except ValueError:
        pass
evil = Path(export_mid(res["pattern"], name="../../evil name"))
ok("/" not in evil.name and "\\" not in evil.name and evil.parent == p1.parent,
   f"export name must sanitize to one file: {evil.name}")
delete_take(evil.name)
ok(not evil.exists(), "delete_take removes the file")
delete_take(p1.name)
delete_take(p2.name)
left = [t["name"] for t in list_takes(500)]
ok(p1.name not in left and p2.name not in left, "deleted takes leave the list")
try:
    delete_take(p1.name)
    fails.append("double delete must refuse, not silently pass")
except ValueError:
    pass

# --- #6: same-second same-name export never replaces (gets -2, -3) ----------
r = Recorder()
r.start()
r.feed({"kind": "cc", "cc": 7, "value": 11, "ts": time.time()})
res = r.stop(bpm=120.0)
pa = Path(export_mid(res["pattern"], "battery collide"))
pb = Path(export_mid(res["pattern"], "battery collide"))   # same second, same name
pc = Path(export_mid(res["pattern"], "battery collide"))
ok(pa.exists() and pb.exists() and pc.exists(), "colliding exports must all survive")
ok(len({pa.name, pb.name, pc.name}) == 3, f"names must differ: {pa.name} {pb.name} {pc.name}")
ok("-2" in pb.name or "-3" in pc.name, f"collision suffix expected: {pb.name} {pc.name}")
for x in (pa, pb, pc):
    delete_take(x.name)

# --- #7: a stopped take stays PENDING until exported (failure loses nothing) -
r = Recorder()
r.start()
r.feed({"kind": "cc", "cc": 7, "value": 3, "ts": time.time()})
res = r.stop(bpm=120.0)
ok(r.pending() is not None, "stopped take must remain pending until exported")
pend = r.pending() or {}
ok(len(pend.get("cc_tracks", [])) == 1, "pending take carries the ride")
p = Path(export_mid(pend, "battery pending"))
r.clear_pending()
ok(r.pending() is None, "clear_pending releases the slot")
delete_take(p.name)

# --- A4 (): start while pending REFUSES, never clobbers
r = Recorder()
r.start()
r.feed({"kind": "cc", "cc": 7, "value": 10, "ts": time.time()})
r.stop(bpm=120.0)
rs = r.start()
ok(rs.get("error") and "export" in rs["error"], f"start while pending must refuse by name: {rs}")
ok(r.pending() is not None, "refused start must leave the pending take intact")
r.clear_pending()
ok(r.start().get("ok"), "start after clear_pending works")
r.stop(bpm=120.0)
r.clear_pending()
r.start()
r.feed({"kind": "cc", "cc": 7, "value": 4, "ts": time.time()})
r.stop(bpm=120.0)
ok(r.discard().get("ok"), "discard works")
ok(r.pending() is None, "discard empties the slot")

# --- regression guard UI4: discard must ALSO stop a LIVE take (the tooltip always
# promised it; it used to answer "no pending take" and keep rolling) ---
r = Recorder()
r.start()
r.feed({"kind": "cc", "cc": 7, "value": 3, "ts": time.time()})
d = r.discard()
ok(d.get("ok") and d.get("stopped_recording"), f"discard must stop a live take: {d}")
ok(r.status().get("recording") is False and r.pending() is None, "discarded live take leaves nothing behind")
ok(r.discard().get("error"), "discarding empty air must refuse by name")

# --- #11: echo events never double-record ------------------------------------
r = Recorder()
r.start()
r.feed({"kind": "cc", "cc": 9, "value": 50, "channel": 0, "ts": time.time(), "source": "ui"})
r.feed({"kind": "cc", "cc": 9, "value": 50, "channel": 0, "ts": time.time(), "source": "echo"})
res = r.stop(bpm=120.0)
ok(res.get("cc_tracks") == 1, f"echo must be ignored by the recorder: {res}")
cc9 = res["pattern"]["cc_tracks"][0]
ok(len(cc9["points"]) == 1, f"echo must not add a point: {cc9['points']}")

print("FAILURES:", fails if fails else "none")
raise SystemExit(1 if fails else 0)
