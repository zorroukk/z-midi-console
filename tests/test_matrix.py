"""Matrix battery: LFO shapes, mix math, validator refuse-closed, live loopMIDI run.

Run: PYTHONPATH=dashboard:. <gateway-py3.14> tests/test_matrix.py
"""
import math
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matrix_engine as me  # noqa: E402
from matrix_engine import lfo_value, mix, MatrixEngine  # noqa: E402
import modmath  # noqa: E402
import profile_store  # noqa: E402


def main() -> int:
    # 1. mirror law: LFO_WAVES identical in both modules
    assert set(profile_store.LFO_WAVES) == set(me.LFO_WAVES) == set(modmath.LFO_WAVES), \
        "LFO_WAVES must all derive from modmath (single source)"

    # 2. LFO shapes: known points + range over a full cycle
    assert abs(lfo_value("sine", 0.0) - 0.5) < 1e-9
    assert abs(lfo_value("sine", 0.25) - 1.0) < 1e-9
    assert abs(lfo_value("sine", 0.75) - 0.0) < 1e-9
    assert abs(lfo_value("tri", 0.0) - 0.0) < 1e-9 and abs(lfo_value("tri", 0.25) - 0.5) < 1e-9
    assert abs(lfo_value("saw", 0.0)) < 1e-9 and abs(lfo_value("saw", 0.999) - 0.999) < 1e-6
    for wave in ("sine", "tri", "saw"):
        vals = [lfo_value(wave, i / 997.0) for i in range(997)]
        assert all(0.0 <= v <= 1.0 for v in vals), f"{wave} out of 0..1"
        assert max(vals) - min(vals) > 0.9, f"{wave} barely moves"
    try:
        lfo_value("square", 0.5)
        raise AssertionError("unknown wave not refused")
    except ValueError:
        pass

    # 3. mix math: add/mul/set + clamping
    assert abs(mix(0.5, 1.0, 0.5, "add") - 1.0) < 1e-9
    assert mix(0.9, 1.0, 1.0, "add") == 1.0, "add must clamp"
    assert abs(mix(0.5, 1.0, 0.5, "mul") - 0.75) < 1e-9
    assert abs(mix(0.5, 0.0, 0.5, "mul") - 0.25) < 1e-9
    assert abs(mix(0.2, 0.8, 1.0, "set") - 0.8) < 1e-9
    assert abs(mix(0.2, 0.8, 0.0, "set") - 0.2) < 1e-9, "depth 0 set = passthrough"
    assert mix(0.1, 1.0, -1.0, "add") == 0.0, "negative depth must clamp"
    try:
        mix(0.5, 0.5, 1.0, "bogus")
        raise AssertionError("unknown mode not refused")
    except ValueError:
        pass

    # 4. validator: lfo source allowed, lfo block + slew refuse-closed
    def good():
        return {
            "schema_version": 1, "profile_id": "mt", "device": "MT",
            "controls": {"a": {"label": "A", "cc": 1, "x": 0, "y": 0},
                         "b": {"label": "B", "cc": 2, "x": 1, "y": 0}},
            "matrix": [{"source": "lfo", "target": "b", "depth": 0.5, "slew": 0.2}],
            "lfo": {"wave": "tri", "rate_hz": 4.0},
        }
    assert profile_store.validate_profile(good()) == [], profile_store.validate_profile(good())
    p = good(); p["lfo"]["wave"] = "square"
    errs = profile_store.validate_profile(p)
    assert errs and any("lfo.wave" in e for e in errs), errs
    p = good(); p["lfo"]["rate_hz"] = 999
    assert any("rate_hz" in e for e in profile_store.validate_profile(p))
    p = good(); p["matrix"][0]["slew"] = 5
    assert any("slew" in e for e in profile_store.validate_profile(p))
    p = good(); p["matrix"][0]["source"] = "ghost"
    assert any("not a control or 'lfo'" in e for e in profile_store.validate_profile(p))
    # label controls have no wire — edges must not touch them (named error here,
    # not a vague engine-start refusal later)
    p = good(); p["controls"]["lbl"] = {"label": "L", "widget": "label", "x": 2, "y": 0}
    p["matrix"][0]["target"] = "lbl"
    assert any("label (no wire)" in e for e in profile_store.validate_profile(p)), \
        "label target must be refused by the validator"
    p = good(); p["controls"]["lbl"] = {"label": "L", "widget": "label", "x": 2, "y": 0}
    p["matrix"][0]["source"] = "lbl"
    assert any("label (no wire)" in e for e in profile_store.validate_profile(p)), \
        "label source must be refused by the validator"

    # 4b. Lazy-arm law (owner bug 2026-11-10, chat box demo): an unobserved
    # target no longer REFUSES the whole start — one stale lane (never echoed
    # since connect) used to block every fresh command. Base-mixing lanes stay
    # silent until the target arms with a CAPTURED value, then modulate around
    # it. The "invent 64" masquerade stays DEAD (#3): nothing is
    # ever restored that was never observed.
    lvals: dict = {"b": None}
    sent: list = []
    eng = MatrixEngine(lambda **kw: sent.append(kw) or {}, lambda cid: lvals.get(cid))
    r = eng.start([{"source": "lfo", "target": "b", "depth": 1.0}],
                  {"wave": "sine", "rate_hz": 2.0}, {"b": 2}, 0)
    assert r.get("ok") and r.get("pending") == ["b"], r
    time.sleep(0.15)
    assert not sent, "a pending target must receive no base-mixing sends"
    lvals["b"] = 40                     # one twist: the value becomes known
    time.sleep(0.2)
    assert sent, "arming after a real value did not start modulation"
    st = eng.stop()
    assert st.get("ok") and st["restored"] and st["restored"][0]["value"] == 40, st
    assert "not_restored" not in st, st  # armed base -> honest restore

    # 4b''. ABSOLUTE moves run immediately even with no base (they name their
    # own from->to); stop() refuses to fake a restore and says so by name.
    sent2 = []
    eng = MatrixEngine(lambda **kw: sent2.append(kw) or {}, lambda cid: None)
    r = eng.start([{"kind": "move", "target": "b", "from": 0, "to": 127,
                    "duration_ms": 100, "curve": "lin", "hold": True}],
                  {"wave": "sine", "rate_hz": 0.5}, {"b": 2}, 0)
    assert r.get("ok") and r.get("pending") == ["b"], r
    time.sleep(0.25)
    assert sent2 and sent2[-1]["value"] == 127, sent2
    st = eng.stop()
    assert st.get("ok") and st.get("not_restored") == ["b"], st
    assert st["restored"] == [], "stop must not send a fake restore for an unobserved base"

    # 4b'. #4: a refused send mid-run latches a fault and stops modulation.
    def dead_wire(**kw):
        return {"ok": False, "error": "wire dead"}
    eng = MatrixEngine(dead_wire, lambda cid: 64)
    r = eng.start([{"source": "lfo", "target": "b", "depth": 1.0}],
                  {"wave": "sine", "rate_hz": 8.0}, {"b": 2}, 0)
    assert r.get("ok"), r
    time.sleep(0.3)
    st = eng.status()
    assert not st["running"], "engine kept modulating against a dead wire"
    assert st["fault"] and "wire dead" in st["fault"], st
    # Deep rinse (law change): a FAULTED engine still holds its captured
    # bases — stop() must ATTEMPT the restore (base law), not shrug "not
    # running". On a dead wire the restore send is refused and REPORTED
    # (failed[] carrying the base value 64), never silently skipped.
    r = eng.stop()
    assert r.get("failed") and r["failed"][0]["value"] == 64, r

    # 4b''. #4 second half: a restore the wire REFUSES is reported (failed[]),
    # never folded into ok. depth 0 keeps the coalescer quiet, so the only
    # send is the restore — deterministic.
    eng = MatrixEngine(lambda **kw: {"ok": True}, lambda cid: 64)
    r = eng.start([{"source": "lfo", "target": "b", "depth": 0.0}],
                  {"wave": "sine", "rate_hz": 8.0}, {"b": 2}, 0)
    assert r.get("ok"), r
    time.sleep(0.15)
    eng.send = dead_wire
    r = eng.stop()
    assert not r.get("ok") and r.get("failed"), r

    # 4b'''. #1: double start refuses (no orphan engine), stop is idempotent
    eng = MatrixEngine(lambda **kw: {}, lambda cid: 64)
    assert eng.start([{"source": "lfo", "target": "b", "depth": 0.5}],
                     {"wave": "sine", "rate_hz": 2.0}, {"b": 2}, 0).get("ok")
    r2 = eng.start([{"source": "lfo", "target": "b", "depth": 0.5}],
                   {"wave": "sine", "rate_hz": 2.0}, {"b": 2}, 0)
    assert not r2.get("ok") and "already running" in r2.get("error", ""), r2
    eng.stop()
    r = eng.stop()
    assert not r.get("ok") and r.get("error") == "matrix not running", \
        f"second engine stop must refuse honestly: {r}"

    # 4b5. LMMS-borrow batch (2026-11-10): sh wave, phase, move envelopes,
    # when/watch triggers, wire-remap math. Deterministic: where a thread
    # would race the clock, stop it and drive _tick(t) by hand.
    v0 = lfo_value("sh", 0.3)
    assert 0.0 <= v0 <= 1.0, v0
    assert abs(v0 - lfo_value("sh", 0.9)) < 1e-9, "sh must hold within one cycle"
    assert abs(v0 - lfo_value("sh", 1.0)) > 1e-9, "sh must change between cycles"
    assert v0 == lfo_value("sh", 0.3), "sh must be reproducible for the same cycle index"
    assert lfo_value("sh", 100.3) == lfo_value("sh", 100.9), "sh must be reproducible for the same cycle index"
    spread = [lfo_value("sh", i) for i in range(20)]
    assert len(set(spread)) > 10 and max(spread) - min(spread) > 0.5, "sh barely moves"

    # phase (LMMS PHS knob, rewritten): an offset edge starts mid-cycle.
    sent = []
    eng = MatrixEngine(lambda **kw: sent.append(kw) or {}, lambda cid: 64)
    r = eng.start([{"source": "lfo", "target": "b", "depth": 1.0,
                    "wave": "saw", "rate_hz": 1.0, "phase": 0.25}],
                  {"wave": "sine", "rate_hz": 1.0}, {"b": 2}, 0)
    assert r.get("ok"), r
    eng.running = False
    eng._thread.join(timeout=1.0)
    sent.clear()  # freeze first: a real-time tick may have fired before join
    eng._tick(0.0)  # saw(0.25)=0.25 -> signed -0.5 -> base .5 -.5 = 0
    assert sent and sent[-1]["cc"] == 2 and sent[-1]["value"] == 0, sent

    # move: one-shot envelope, holds at the end, stop restores the CAPTURED
    # base (base law — a held move is undone by stop/panic).
    sent = []
    eng = MatrixEngine(lambda **kw: sent.append(kw) or {}, lambda cid: 40)
    r = eng.start([{"kind": "move", "target": "b", "from": 0, "to": 127,
                    "duration_ms": 1000, "curve": "lin"}],
                  {"wave": "sine", "rate_hz": 1.0}, {"b": 2}, 0)
    assert r.get("ok") and r["targets"] == ["b"], r
    eng.running = False
    eng._thread.join(timeout=1.0)
    sent.clear()          # freeze first: a real-time tick may have advanced
    eng._last_sent.clear()  # the move slightly before the clock hand-off
    eng._moves[0]["t0"] = 10.0
    eng._tick(0.5)   # thread-clock time: p<0, move not started — no sends
    assert not sent, sent
    eng._tick(10.5)  # half-way
    assert sent and sent[-1]["value"] in (62, 63, 64), sent[-1]
    eng._tick(11.2)  # past the end -> holds 100%
    assert sent[-1]["value"] == 127, sent[-1]
    r = eng.stop()
    assert r.get("ok") and r["restored"] == [{"cid": "b", "cc": 2, "value": 40}], r

    # when: up-crossing fires the then-move; once:true never re-arms.
    vals = {"a": 0, "b": 40}
    sent = []
    eng = MatrixEngine(lambda **kw: sent.append(kw) or {}, lambda cid: vals.get(cid))
    r = eng.start([{"kind": "when", "source": "a", "level": 64, "crosses": "up",
                    "then": [{"kind": "move", "target": "b", "from": 40,
                              "to": 127, "duration_ms": 500}]}],
                  {"wave": "sine", "rate_hz": 1.0}, {"a": 1, "b": 2}, 0)
    assert r.get("ok"), r
    eng.running = False
    eng._thread.join(timeout=1.0)
    eng._tick(0.05)
    assert eng.status()["fired"] == 0
    vals["a"] = 70
    eng._tick(0.15)  # crosses 64 up -> FIRE, then-move t0=0.15
    assert eng.status()["fired"] == 1
    eng._tick(0.4)   # p=0.5 -> b mid-swing
    vb = [s for s in sent if s["cc"] == 2][-1]["value"]
    assert 75 <= vb <= 90, sent
    eng._tick(0.7)   # p>=1 -> holds 127
    assert [s for s in sent if s["cc"] == 2][-1]["value"] == 127
    vals["a"] = 10
    eng._tick(0.9)
    vals["a"] = 100
    eng._tick(1.1)   # once:true: re-crossing must NOT fire again
    assert eng.status()["fired"] == 1, "when re-fired despite once:true"

    # watch: ANY twist fires the then-lfo; center/amp orbit the NAMED range,
    # not the captured base ("after I turn timbre, oscillate resonance").
    vals = {"a": 10, "b": 64}
    sent = []
    eng = MatrixEngine(lambda **kw: sent.append(kw) or {}, lambda cid: vals.get(cid))
    r = eng.start([{"kind": "watch", "source": "a",
                    "then": [{"kind": "lfo", "target": "b", "wave": "tri",
                              "rate_hz": 0.5, "center": 64, "amp": 25}]}],
                  {"wave": "sine", "rate_hz": 1.0}, {"a": 1, "b": 2}, 0)
    assert r.get("ok"), r
    eng.running = False
    eng._thread.join(timeout=1.0)
    eng._tick(0.05)  # prev None: just records
    assert not [s for s in sent if s["cc"] == 2], "watch fired without a twist"
    vals["a"] = 11
    eng._tick(0.1)   # FIRE -> lfo t0=0.1
    eng._tick(0.35)  # 0.125 cycles of tri = 0.25 -> signed -0.5 -> ~51
    vb = [s for s in sent if s["cc"] == 2][-1]["value"]
    assert 50 <= vb <= 52, sent

    # B16 RESTART law (owner ruling 2026-11-10): re-firing a trigger REPLACES
    # its then-child — one active instance per trigger, never a stack of
    # oscillators (three twists of the watch source must NOT triple the swing).
    vals = {"a": 10, "b": 64}
    sent = []
    eng = MatrixEngine(lambda **kw: sent.append(kw) or {}, lambda cid: vals.get(cid))
    r = eng.start([{"kind": "watch", "source": "a", "once": False,
                    "then": [{"kind": "lfo", "target": "b", "wave": "sine",
                              "rate_hz": 1.0, "depth": 0.5}]}],
                  {"wave": "sine", "rate_hz": 1.0}, {"a": 1, "b": 2}, 0)
    assert r.get("ok"), r
    eng.running = False
    eng._thread.join(timeout=1.0)
    sent.clear(); eng._last_sent.clear()  # frozen-clock law: kill pre-join sends
    for t, v in ((0.1, 11), (0.2, 12), (0.3, 13)):  # three twists -> three fires
        vals["a"] = v
        eng._tick(t)
    assert len(eng._lfo_edges) == 1, \
        f"re-fire must REPLACE the child, got {len(eng._lfo_edges)} stacked"
    # The SURVIVING child restarted at the LAST fire (t0=0.3): at t=0.425
    # that is 0.125 cycles of sine -> 0.707 -> 64 + 63.5*0.707 ~ 109.
    # Stacked children (the old bug) would mix sequentially and land ~127.
    eng._tick(0.425)
    vb = [s for s in sent if s["cc"] == 2][-1]["value"]
    assert 107 <= vb <= 111, f"restart law broken (t0 or stacking): {vb} in {sent}"
    eng._tick(0.55)  # 0.25 cycles -> sine peak -> full depth 127
    vb = [s for s in sent if s["cc"] == 2][-1]["value"]
    assert vb == 127, f"surviving child should be at its peak: {vb} in {sent}"

    # validator: kind-aware refuse-closed.
    def with_edge(edge):
        p = good()
        p["matrix"] = [edge]
        return profile_store.validate_profile(p)

    assert with_edge({"kind": "move", "target": "b", "from": 0, "to": 127,
                      "duration_ms": 5000}) == []
    assert any("to" in e and "int 0..127" in e for e in
               with_edge({"kind": "move", "target": "b", "from": 0, "to": 200,
                          "duration_ms": 5000}))
    assert any("duration_ms" in e for e in
               with_edge({"kind": "move", "target": "b", "from": 0, "to": 10,
                          "duration_ms": 5}))
    assert any("move edges have no source" in e for e in
               with_edge({"kind": "move", "target": "b", "from": 0, "to": 10,
                          "duration_ms": 1000, "source": "lfo"}))
    assert any("crosses" in e for e in
               with_edge({"kind": "when", "source": "a", "level": 64,
                          "crosses": "sideways",
                          "then": [{"kind": "move", "target": "b", "from": 0,
                                    "to": 127, "duration_ms": 1000}]}))
    assert any("non-empty" in e for e in
               with_edge({"kind": "when", "source": "a", "level": 64, "then": []}))
    assert any("cannot nest" in e for e in
               with_edge({"kind": "when", "source": "a", "level": 64, "then":
                          [{"kind": "when", "source": "a", "level": 10, "then":
                            [{"kind": "move", "target": "b", "from": 0, "to": 1,
                              "duration_ms": 100}]}]}))
    assert any("watch senses physical twists" in e for e in
               with_edge({"kind": "watch", "source": "lfo", "then":
                          [{"kind": "move", "target": "b", "from": 0, "to": 1,
                            "duration_ms": 100}]}))
    assert with_edge({"kind": "watch", "source": "a", "then":
                      [{"kind": "lfo", "target": "b", "wave": "tri",
                        "rate_hz": 0.5, "center": 64, "amp": 25}]}) == []
    assert any("phase" in e for e in
               with_edge({"source": "lfo", "target": "b", "phase": 2.0}))

    # control wire-remap keys validate; math pinned against a seeded map.
    p = good()
    p["controls"]["b"].update({"invert": True, "out_min": 20, "out_max": 100})
    assert profile_store.validate_profile(p) == [], profile_store.validate_profile(p)
    p = good()
    p["controls"]["b"].update({"out_min": 100, "out_max": 20})
    assert any("out_min" in e for e in profile_store.validate_profile(p))
    p = good()
    p["controls"]["b"]["invert"] = "yes"
    assert any("invert must be a bool" in e for e in profile_store.validate_profile(p))
    profile_store._WIRE_CACHE["m"] = {"primary": {7: (True, 0, 127),
                                                  8: (False, 20, 100)}}
    try:
        assert profile_store.wire_xform("primary", 5, 90) == 90
        assert profile_store.wire_xform("primary", 7, 10) == 117
        assert abs(profile_store.unwire_xform("primary", 7, 117) - 10) <= 1
        assert profile_store.wire_xform("primary", 8, 0) == 20
        assert profile_store.wire_xform("primary", 8, 127) == 100
        assert abs(profile_store.unwire_xform("primary", 8, 60) - 64) <= 1
        assert profile_store.wire_xform("other-bus", 7, 10) == 10
    finally:
        profile_store.invalidate_wire_maps()

    # 4b''''. Multi-matrix law: each LFO lane runs its OWN oscillator. Two
    # lanes at different rates must swing independently, never share a clock.
    sent = []
    eng = MatrixEngine(lambda **kw: sent.append(kw) or {}, lambda cid: 64)
    r = eng.start([{"source": "lfo", "target": "a", "depth": 1.0,
                    "wave": "sine", "rate_hz": 1.0},
                   {"source": "lfo", "target": "b", "depth": 1.0,
                    "wave": "sine", "rate_hz": 3.0}],
                  {"wave": "sine", "rate_hz": 0.5}, {"a": 1, "b": 2}, 0)
    assert r.get("ok"), r
    sent.clear()
    eng._tick(0.25)  # lane a phase .25 -> sine peak; lane b phase .75 -> trough
    va = [s for s in sent if s.get("cc") == 1][-1]["value"]
    vb = [s for s in sent if s.get("cc") == 2][-1]["value"]
    assert va >= 120 and vb <= 8, f"lanes not independent: a={va} b={vb}"
    eng.stop()

    # 4c. reentry law: engine calls send_fn while holding its lock; if send_fn
    # reaches back into engine.status(), a plain Lock would self-deadlock the
    # engine thread. RLock must let it through and the thread must exit clean.
    eng = MatrixEngine(lambda **kw: eng_st.append(eng2.status()) or {}, lambda cid: 64)
    eng_st = []
    eng2 = eng
    r = eng.start([{"source": "lfo", "target": "b", "depth": 0.5}],
                  {"wave": "sine", "rate_hz": 8.0}, {"b": 2}, 0)
    assert r.get("ok"), r
    th = eng._thread
    time.sleep(0.3)
    eng.stop()
    assert not th.is_alive(), "engine thread deadlocked on status() reentry"
    assert eng_st, "reentrant status() saw nothing"
    assert eng_st[0]["running"], "reentrant status must see the running engine"

    # 5. LIVE: lfo -> cutoff on the loopMIDI bench, base capture + restore
    import midi_core
    r = midi_core.BUS.connect("loopMIDI Port")
    assert r.get("ok"), r
    try:
        prof = good()
        prof["profile_id"] = "matrix-bench"
        profile_store.save_profile(prof)
        profile_store.set_active_profile_id("matrix-bench")
        # set base: cutoff-equivalent cc2 = 64 (shadow + wire)
        midi_core.BUS.send(kind="cc", cc=2, value=64, channel=0, source="probe")
        time.sleep(0.4)

        cc_of = {"a": 1, "b": 2}
        sent = []

        def send_fn(**kw):
            sent.append(kw["value"])
            return midi_core.BUS.send(**kw)

        def getv(cid):
            ent = midi_core.BUS.cc_state.get((0, cc_of[cid]))
            return ent["value"] if ent else None

        eng = MatrixEngine(send_fn, getv)
        r = eng.start(prof["matrix"], prof["lfo"], cc_of, 0)
        assert r.get("ok"), r
        time.sleep(0.9)  # ~3.6 cycles at 4 Hz
        st = eng.status()
        assert st["running"] and len(sent) >= 8, f"too few sends: {len(sent)}"
        assert min(sent) < 64 < max(sent) or (min(sent) < 64 and max(sent) <= 127), \
            f"lfo did not modulate around base: min={min(sent)} max={max(sent)}"
        assert all(0 <= v <= 127 for v in sent)
        assert max(sent) - min(sent) > 20, f"modulation too shallow: {max(sent)-min(sent)}"
        r = eng.stop()
        assert r.get("ok") and r["restored"][0]["value"] == 64, r
        time.sleep(0.4)
        ent = midi_core.BUS.cc_state.get((0, 2))
        assert ent and ent["value"] == 64, f"base not restored on wire: {ent}"
        # engine refuses empty edges
        e2 = MatrixEngine(send_fn, getv)
        assert not e2.start([], prof["lfo"], cc_of, 0).get("ok"), "empty edges accepted"
        # BUS-level matrix: auto-stop on disconnect must not deadlock
        # (midiInClose waits for the restore-echo callback, which waits on _lock)
        r = midi_core.BUS.matrix_start(prof["matrix"], prof["lfo"], cc_of, 0)
        assert r.get("ok"), r
        time.sleep(0.3)
        # AB-BA regression: the pane polls /state (BUS.status) every 600ms while
        # the matrix loop sends. status() must never hold Bus._lock while taking
        # engine.lock — the engine loop holds engine.lock while taking _lock.
        poll = threading.Thread(
            target=lambda: [midi_core.BUS.status() for _ in range(30)])
        poll.start()
        poll.join(5.0)
        assert not poll.is_alive(), "BUS.status() vs running matrix deadlocked"
        t0 = time.monotonic()
        midi_core.BUS.disconnect()
        assert time.monotonic() - t0 < 3.0, "disconnect with running matrix hung"
        assert not midi_core.BUS.matrix_status().get("running"), "zombie matrix after disconnect"
        # BUS-level stop is idempotent (the pane's Stop button may double-fire)
        assert midi_core.BUS.matrix_stop().get("ok") and \
               midi_core.BUS.matrix_stop().get("restored") == [], \
            "BUS.matrix_stop must be idempotent"
        assert midi_core.BUS.matrix_start(prof["matrix"], prof["lfo"], cc_of, 0).get("error") \
            == "not connected — connect a device first", "matrix must refuse when offline"
        assert midi_core.BUS.connect("loopMIDI Port").get("ok")
        profile_store.delete_profile("matrix-bench")
        profile_store.set_active_profile_id(None)
    finally:
        midi_core.BUS.disconnect()
    print("ALL OK — matrix battery passed (mirror, lfo shapes, mix math, refuse-closed, live modulate+restore)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
