"""Deep-rinse battery: pins every fix from the 2026-11-09 full-code review.

Standalone runner: python tests/test_rinse.py
Runs against a throwaway LOCALAPPDATA so the real user tier is never touched.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_TMP = tempfile.mkdtemp(prefix="mssc-rinse-")
os.environ["LOCALAPPDATA"] = _TMP
os.environ["HERMES_HOME"] = _TMP  # the user tier is profile-scoped now (PR 153102)

import profile_store  # noqa: E402
import midi_core  # noqa: E402
from matrix_engine import MatrixEngine  # noqa: E402
import automation  # noqa: E402

FAIL = []


def check(name, fn):
    try:
        fn()
        print(f"  ok   {name}")
    except AssertionError as exc:
        FAIL.append(name)
        print(f"  FAIL {name}: {exc}")
    except Exception as exc:
        FAIL.append(name)
        print(f"  FAIL {name}: {type(exc).__name__}: {exc}")


def good_profile(pid="rinse-test"):
    return {
        "schema_version": 1,
        "profile_id": pid,
        "device": "Rinse Device",
        "controls": {
            "a": {"label": "A", "cc": 1, "widget": "knob", "x": 0, "y": 0},
            "lbl": {"label": "L", "cc": 9, "widget": "label", "x": 1, "y": 0},
        },
    }


# ---- profile_store ---------------------------------------------------------

def junk_scan_no_crash():
    d = profile_store.user_profiles_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "junk-list.json").write_text("[1, 2, 3]", encoding="utf-8")
    (d / "junk-num.json").write_text("123", encoding="utf-8")
    try:
        rows = profile_store.list_profiles()  # used to AttributeError -> 500
    except Exception as exc:
        raise AssertionError(f"scan crashed on junk JSON: {exc}")
    ids = {r["profile_id"] for r in rows}
    assert "junk-list" in ids and "junk-num" in ids, ids
    bad = [r for r in rows if r["profile_id"] == "junk-list"]
    assert bad and bad[0].get("errors"), bad
    # the flagship must still list fine alongside the junk
    assert any(r["profile_id"] == "microfreak" for r in rows), "flagship lost"
    (d / "junk-list.json").unlink()
    (d / "junk-num.json").unlink()


def bool_is_not_a_number():
    p = good_profile()
    p["channel"] = True
    assert any("channel" in e for e in profile_store.validate_profile(p)), p
    p = good_profile()
    p["controls"]["a"]["cc"] = True
    errs = profile_store.validate_profile(p)
    assert any("cc" in e for e in errs), errs
    p = good_profile()
    p["matrix"] = [{"source": "lfo", "target": "a", "depth": True}]
    assert any("depth" in e for e in profile_store.validate_profile(p))
    p = good_profile()
    p["sections"] = {"s": {"label": "S", "font_size": True}}
    assert any("font_size" in e for e in profile_store.validate_profile(p))


def windows_reserved_name_refused():
    for pid in ("con", "nul", "aux", "com1", "lpt9"):
        p = good_profile(pid)
        assert any("reserved" in e for e in profile_store.validate_profile(p)), pid
    p = good_profile("cone")  # must NOT refuse a normal slug that starts like one
    assert not any("reserved" in e for e in profile_store.validate_profile(p))


def settings_wrong_shape_survives():
    sp = profile_store.data_dir() / "settings.json"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text('["not", "a", "dict"]', encoding="utf-8")
    assert profile_store.get_active_profile_id() is None  # used to AttributeError
    sp.write_text('"bare string"', encoding="utf-8")
    assert profile_store.get_active_profile_id() is None
    sp.unlink()


def cc_map_excludes_labels():
    p = good_profile()
    profile_store.save_profile(p)
    m = profile_store.control_cc_map("rinse-test")
    assert m == {1: "a"}, m  # label cc 9 has no wire (matrix_inputs law parity)


# ---- midi_core Bus (FakeOut units, no ports) --------------------------------

class FakeOut:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    def send_short(self, msg):
        self.sent.append(msg)
        return self.ok


def fresh_bus(ok=True):
    b = midi_core.Bus()
    b.sendlog_on = False
    b._out = FakeOut(ok)
    return b


def failed_first_cc_send_leaves_no_phantom():
    b = fresh_bus(ok=False)
    r = b.send(kind="cc", cc=42, value=99, source="ui")
    assert r.get("error"), r
    assert (0, 42) not in b.cc_state, "phantom shadow entry after failed first send"
    # and a second failed send over an EXISTING value restores it
    b._out = FakeOut(ok=True)
    b.send(kind="cc", cc=42, value=10, source="ui")
    b._out = FakeOut(ok=False)
    b.send(kind="cc", cc=42, value=99, source="ui")
    assert b.cc_state[(0, 42)]["value"] == 10, "failed send did not roll back to prev"


def failed_note_off_keeps_ownership():
    b = fresh_bus()
    r = b.send(kind="note_on", note=60, velocity=100, source="agent")
    assert r.get("ok"), r
    assert (0, 60) in b._ours
    b._out = FakeOut(ok=False)
    r = b.send(kind="note_off", note=60, source="agent")
    assert r.get("error"), r
    assert (0, 60) in b.notes, "held note lost from shadow"
    assert (0, 60) in b._ours, "ownership lost -> disconnect would strand the note"


def junk_velocity_refused_not_crash():
    b = fresh_bus()
    r = b.send(kind="note_on", note=60, velocity="abc", source="agent")
    assert isinstance(r, dict) and "velocity" in str(r.get("error", "")), r
    r = b.send(kind="note_on", note=60, velocity=None, source="agent")
    assert r.get("ok") and r.get("velocity") == 100, r
    r = b.send(kind="note_on", note=60, velocity=0, source="agent")
    assert r.get("ok") and r.get("velocity") == 0, r  # 0 stays 0 (wire note-off semantics)


# ---- matrix engine fault path -----------------------------------------------

def faulted_engine_stop_restores_base():
    sent = []
    calls = {"n": 0}

    def wire(**kw):
        calls["n"] += 1
        sent.append(kw)
        if calls["n"] == 3:
            return {"error": "wire dead mid-run"}
        return {"ok": True}

    eng = MatrixEngine(wire, lambda cid: 64)
    r = eng.start([{"source": "lfo", "target": "a", "depth": 1.0}],
                  {"wave": "sine", "rate_hz": 8.0}, {"a": 2}, 0)
    assert r.get("ok"), r
    deadline = time.time() + 2.0
    while eng.status()["running"] and time.time() < deadline:
        time.sleep(0.05)
    assert eng.status()["fault"], "fault never latched"
    r = eng.stop()  # deep rinse: must restore, not "not running"
    assert r.get("ok") and r.get("restored"), r
    assert r["restored"][0]["value"] == 64, r  # base came back exactly


# ---- automation --------------------------------------------------------------

def pending_take_refuses_re_record():
    # A4 (2026-11-10): the old pending_overwritten flag only made
    # the data loss LOUD — it still lost the data. Starting while a take is
    # pending is now REFUSED by name; export/Discard are the only exits.
    rec = automation.Recorder()
    rec.start()
    rec.feed({"kind": "cc", "cc": 1, "value": 5, "channel": 0, "source": "ui"})
    r1 = rec.stop(120)
    assert r1.get("ok"), r1
    rs = rec.start()
    assert rs.get("error") and "export" in rs["error"], f"start while pending must refuse: {rs}"
    assert rec.pending() is not None, "refused start must keep the pending take intact"
    rec.discard()
    assert rec.start().get("ok"), "start after discard works"
    rec.stop(120)
    rec.discard()


def raw_bpm_named_error():
    rec = automation.Recorder()
    rec.start()
    rec.feed({"kind": "cc", "cc": 1, "value": 5, "channel": 0, "source": "ui"})
    r = rec.stop("abc")  # raw string, like the agent tool now passes
    assert not r.get("ok") and "bpm" in r.get("error", ""), r
    rec.discard()


def export_collision_never_eats():
    pat = {"bpm": 120.0, "name": "t", "notes": [],
           "cc_tracks": [{"cc": 1, "channel": 0, "points": [{"t_ms": 0, "v": 1}, {"t_ms": 5, "v": 2}]}]}
    p1 = automation.export_mid(pat, name="same")
    p2 = automation.export_mid(pat, name="same")
    assert p1 != p2, (p1, p2)
    assert Path(p1).is_file() and Path(p2).is_file()
    Path(p1).unlink()
    Path(p2).unlink()


# ---- review 2026-11-09 batch ---------------------------------------------------

def _load_agent_half():
    """Chat-parity harness (refactor-audit law): exec the agent half as a
    package module with a pinned parent so relative imports resolve AND the
    ONE-Bus law holds — the submodules are pre-seeded with the very instances
    this battery already uses, so midi_core.BUS here is the agent's BUS."""
    import importlib.util
    import types
    spec = importlib.util.spec_from_file_location(
        "zmc_harness", _ROOT / "__init__.py",
        submodule_search_locations=[str(_ROOT)])
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "zmc_harness"
    sys.modules["zmc_harness"] = mod
    sys.modules["zmc_harness.midi_core"] = midi_core
    sys.modules["zmc_harness.profile_store"] = profile_store
    sys.modules["zmc_harness.automation"] = automation
    spec.loader.exec_module(mod)
    return mod


AGENT = None


def agent_half_bpm_parity():
    # The old bpm=120 DEFAULT silently re-tempoed every export retry, and the
    # agent twin CLAMPED while REST refuses. Pin both halves of the fix.
    global AGENT
    AGENT = _load_agent_half()
    rec = midi_core.BUS.recorder
    rec.discard()
    assert rec.start().get("ok")
    rec.feed({"kind": "cc", "cc": 1, "value": 5, "channel": 0, "source": "ui"})
    rs = rec.stop(90.0)  # Recorder directly: leaves the take pending
    assert rs.get("ok"), rs
    bad = json.loads(AGENT._recorder_handler(action="export", bpm=5000, name="parity"))
    assert "refused" in bad.get("error", ""), bad  # refuse-don't-clamp parity
    assert rec.pending() is not None, "refused export ate the take"
    r2 = json.loads(AGENT._recorder_handler(action="export", name="parity"))
    assert r2.get("ok"), r2
    data = Path(r2["path"]).read_bytes()
    i = data.find(b"\xff\x51\x03")
    assert i > 0, data[:64]
    tempo = int.from_bytes(data[i + 3:i + 6], "big")
    assert abs(tempo - 666667) <= 1, f"tempo {tempo}: take re-tempoed, bpm lost"
    Path(r2["path"]).unlink()


def agent_activate_needs_id():
    r = json.loads(AGENT._profile_handler(action="activate"))
    assert "id required" in r.get("error", ""), r  # was: silent deactivate + ok


def agent_take_get_returns_base64():
    pat = {"bpm": 120.0, "name": "b64", "notes": [],
           "cc_tracks": [{"cc": 1, "channel": 0, "points": [{"t_ms": 0, "v": 1}]}]}
    p = automation.export_mid(pat, name="b64")
    r = json.loads(AGENT._recorder_handler(action="take_get", name=Path(p).name))
    assert r.get("mid_b64"), r  # description advertised base64; path-only lied
    import base64 as _b64
    assert _b64.b64decode(r["mid_b64"])[:4] == b"MThd", "mid_b64 not the file"
    Path(p).unlink()


def pc_recorded_and_exported():
    rec = automation.Recorder()
    rec.start()
    rec.feed({"kind": "program", "program": 3, "channel": 0, "source": "ui"})
    rec.feed({"kind": "program", "program": 3, "channel": 0, "source": "ui"})  # coalesce
    rec.feed({"kind": "program", "program": 7, "channel": 1, "source": "ui"})
    r = rec.stop(120)
    assert r.get("ok"), r
    assert len(r["pattern"]["pc_tracks"]) == 2, r["pattern"]
    p = automation.export_mid(r["pattern"], name="pc")
    data = Path(p).read_bytes()
    assert bytes([0xC0, 3]) in data and bytes([0xC1, 7]) in data, "PC events missing from .mid"
    Path(p).unlink()
    rec.discard()


def retrigger_closes_previous_note():
    rec = automation.Recorder()
    rec.start()
    t = time.time()
    for vel, dt in ((100, 0.0), (90, 0.5)):
        rec.feed({"kind": "note", "note": 60, "velocity": vel, "channel": 0,
                  "source": "panel", "ts": t + dt})
    rec.feed({"kind": "note", "note": 60, "velocity": 0, "channel": 0,
              "source": "panel", "ts": t + 1.0})
    r = rec.stop(120)
    assert r.get("ok") and r["notes"] == 2, f"retrigger ate a note: {r}"
    ns = sorted(r["pattern"]["notes"], key=lambda n: n["t_ms"])
    assert ns[0]["vel"] == 100 and ns[1]["vel"] == 90, ns
    rec.discard()


def export_leaves_no_junk():
    pat = {"bpm": 120.0, "name": "clean", "notes": [],
           "cc_tracks": [{"cc": 1, "channel": 0, "points": [{"t_ms": 0, "v": 1}]}]}
    p = automation.export_mid(pat, name="clean")
    assert Path(p).stat().st_size > 0, "0-byte take"
    assert not list(automation.exports_dir().glob("*.mid.lock")), "lock litter"
    Path(p).unlink()


def matrix_fault_stops_tick_at_first_refusal():
    sent = []

    def wire(**kw):
        sent.append(kw.get("cc"))
        return {"error": "dead wire"}

    eng = MatrixEngine(wire, lambda cid: 64)
    r = eng.start([{"source": "lfo", "target": "a", "depth": 1.0},
                   {"source": "lfo", "target": "b", "depth": 1.0}],
                  {"wave": "sine", "rate_hz": 8.0}, {"a": 2, "b": 3}, 0)
    assert r.get("ok"), r
    time.sleep(0.4)          # let the engine thread latch its own fault and exit
    eng.running = False
    time.sleep(0.05)
    with eng.lock:
        n0 = len(sent)
        eng._tick(0.5)       # one manual tick: first refusal must END the tick
    assert len(sent) == n0 + 1, f"faulty tick fired {len(sent) - n0} sends (must stop at first)"
    assert eng.fault and not eng.running
    eng.stop()


def matrix_compiled_edges_refuse_junk():
    eng = MatrixEngine(lambda **k: {"ok": True}, lambda cid: 64)
    r = eng.start([{"source": "lfo", "target": "a", "depth": "x"}], None, {"a": 1}, 0)
    assert not r.get("ok") and "numeric" in r.get("error", ""), r
    assert not r.get("running", False) and not eng.running


# ---- deck feature (2026-11-09) ------------------------------------------------

def deck_registry_one_primary():
    # one-Bus law survives the registry: 'primary' IS the historical singleton
    assert midi_core.get_bus("primary") is midi_core.BUS
    assert midi_core.BUSES["primary"] is midi_core.BUS
    b = midi_core.get_bus("decktest")
    assert b is not midi_core.BUS and b.bus_id == "decktest"
    assert midi_core.get_bus("decktest") is b  # stable registry, no second Bus


def deck_buses_state_isolated():
    a = midi_core.get_bus("busA")
    z = midi_core.get_bus("busZ")
    a.sendlog_on = z.sendlog_on = False
    a._audit({"kind": "cc", "cc": 7, "value": 42, "channel": 0, "source": "ui"})
    a.cc_state[(0, 7)] = {"value": 42, "ts": 0, "source": "ui"}
    sa, sz = a.status(), z.status()
    assert sa["bus"] == "busA" and any(c["cc"] == 7 for c in sa["cc"])
    assert not sz["cc"], "busZ saw busA's CC state"
    assert sa["events"][-1].get("bus") == "busA", "audit rows carry the device axis"


def deck_profile_bus_key():
    good = {"schema_version": 1, "profile_id": "buskey-ok", "device": "d",
            "controls": {}, "bus": "cloudburst"}
    assert profile_store.validate_profile(good) == []
    bad = dict(good, bus="Bad Bus!")
    assert any("bus" in e for e in profile_store.validate_profile(bad))


def deck_settings_roundtrip():
    mf = "microfreak"
    profile_store.set_deck([mf])
    assert profile_store.get_deck() == [mf]
    assert profile_store.get_active_profile_id() == mf
    try:  # refuse-closed: a junk deck id is refused, deck unchanged
        profile_store.set_deck([mf, "nope-missing"])
        raise AssertionError("junk deck id accepted")
    except (KeyError, ValueError):
        pass
    assert profile_store.get_deck() == [mf]
    profile_store.set_active_profile_id(mf)  # activate keeps the deck, front
    assert profile_store.get_deck() == [mf]
    sp = profile_store._settings_path()
    sp.write_text('{"active_profile": 5, "deck": [7, "microfreak"]}', encoding="utf-8")
    assert profile_store.get_deck() == ["microfreak"]  # junk entries dropped


def deck_presets_upsert():
    # Owner 'select preset' menu 2026-11-09: every deck layout is remembered,
    # deduped by exact id order; the live deck is always among the presets.
    mf, cb = "microfreak", "cloudburst"
    profile_store.set_deck([mf])
    profile_store.set_deck([mf, cb])
    profile_store.set_deck([mf])  # back to single — both layouts kept
    pres = profile_store.get_deck_presets()
    assert [mf] in pres and [mf, cb] in pres
    assert sum(1 for p in pres if p == [mf]) == 1  # repeat upsert never duplicates
    profile_store.set_deck([mf])
    assert profile_store.get_deck_presets() == pres  # no growth on repeat
    sp = profile_store._settings_path()
    sp.write_text('{"deck_presets": [3, ["microfreak"], "junk", ["a", 9]]}', encoding="utf-8")
    assert profile_store.get_deck_presets() == [["microfreak"]]  # junk layouts dropped


def keyboard_decl():
    # Piano law (2026-11-09): 'keyboard' is optional but validated when
    # present; the MicroFreak preset declares its real 25-key bed (C3 up).
    mf = profile_store.load_profile("microfreak")
    assert mf.get("keyboard") == {"keys": 25, "first": 48}
    bad = json.loads(json.dumps(mf)); bad["keyboard"] = {"keys": 5, "first": 48}
    assert any("keyboard" in e for e in profile_store.validate_profile(bad)), "tiny keyboard accepted"
    bad2 = json.loads(json.dumps(mf)); bad2["keyboard"] = {"keys": 25}
    assert any("keyboard" in e for e in profile_store.validate_profile(bad2)), "half keyboard accepted"
    bad3 = json.loads(json.dumps(mf)); bad3["keyboard"] = {"keys": 25, "first": 48, "extra": 1}
    assert any("keyboard" in e for e in profile_store.validate_profile(bad3)), "unknown keyboard key accepted"


def manage_copy_rename_delete():
    # owner manage feature 2026-11-09: copy/rename/delete laws.
    # copy: either-tier source -> user tier, id rewritten, never overwrites.
    profile_store.copy_profile("microfreak", "mf-copy")
    c = profile_store.load_profile("mf-copy")
    assert c["profile_id"] == "mf-copy" and c["controls"]
    try:
        profile_store.copy_profile("microfreak", "mf-copy")
        raise AssertionError("copy overwrote an existing profile")
    except ValueError:
        pass
    # rename: user tier only; deck order and active marker follow in place.
    profile_store.set_deck(["mf-copy", "microfreak"])
    profile_store.rename_profile("mf-copy", "mf-two")
    assert not (profile_store.user_profiles_dir() / "mf-copy.json").is_file()
    assert profile_store.load_profile("mf-two")["profile_id"] == "mf-two"
    assert profile_store.get_deck() == ["mf-two", "microfreak"]
    assert profile_store.get_active_profile_id() == "mf-two"
    try:  # bundled profiles are factory — rename refuses, copy is the path
        profile_store.rename_profile("microfreak", "microfreak-2")
        raise AssertionError("renamed a bundled profile")
    except KeyError:
        pass
    # delete: a deleted id must not linger in the deck (later set_deck refuses)
    profile_store.delete_profile("mf-two")
    assert profile_store.get_deck() == ["microfreak"]
    profile_store.set_deck(["microfreak"])  # no phantom refusal from a dead id
    assert profile_store.delete_profile("mf-two") is False


class _FakeMatrixBus:
    """B05 harness: a bus whose matrix can be 'running' without a port —
    the route transaction logic only talks to these three methods."""
    def __init__(self, bus_id="primary"):
        self.bus_id = bus_id
        self.running, self.stops, self.starts = True, 0, 0

    def matrix_status(self):
        return {"running": self.running}

    def matrix_stop(self):
        self.stops += 1
        self.running = False
        return {"ok": True, "restored": []}

    def matrix_start(self, edges, lfo, cc_of, channel):
        self.starts += 1
        self.running = True
        return {"ok": True, "edges": len(edges), "targets": []}


def _req(body):
    class Req:
        async def json(self):
            return body
    return Req()


def b05_save_matrix_transaction():
    # regression review B05 (owner 2026-11-10): saving the ACTIVE profile used to stop a
    # running matrix and leave it stopped (the pane's /matrix/reload no-oped
    # against 'not running' — the wah died silently mid-edit). Law now:
    # active save = stop -> persist -> restart iff it was running; a save of
    # a NON-active preset never touches the matrix; delete/rename of a
    # bystander never sweeps; delete/rename of the ACTIVE one does.
    import dashboard.plugin_api as api
    prev_deck = profile_store.get_deck()
    doc = good_profile("b05-active")
    doc["matrix"] = [{"source": "lfo", "target": "a", "depth": 0.5}]
    profile_store.save_profile(doc)
    profile_store.save_profile(good_profile("b05-bystander"))
    profile_store.set_deck(["b05-active"])
    profile_store.set_active_profile_id("b05-active")
    fake = _FakeMatrixBus("primary")     # the bus the active preset binds
    fake2 = _FakeMatrixBus("rinse-b05sec")  # a bystander bus: never touched
    api.midi_core.BUSES["rinse-b05"] = fake  # type: ignore[reportOptionalMemberAccess]
    api.midi_core.BUSES["rinse-b05sec"] = fake2  # type: ignore[reportOptionalMemberAccess]
    try:
        d = json.loads(json.dumps(doc))
        d["controls"]["a"]["label"] = "A2"
        r = asyncio.run(api.profile_save(_req({"doc": d})))  # type: ignore[arg-type]
        assert r.get("ok") and r.get("matrix_restarted"), r
        assert fake.stops == 1 and fake.starts == 1, \
            f"active save must stop+restart exactly once: {fake.stops}/{fake.starts}"
        assert profile_store.load_profile("b05-active")["controls"]["a"]["label"] == "A2"
        r = asyncio.run(api.profile_save(_req({"doc": good_profile("b05-bystander")})))  # type: ignore[arg-type]
        assert r.get("ok") and "matrix_restarted" not in r, r
        assert (fake.stops, fake.starts) == (1, 1), "non-active save disturbed the matrix"
        r = asyncio.run(api.profile_delete(_req({"id": "b05-bystander"})))  # type: ignore[arg-type]
        assert r.get("ok") and (fake.stops, fake.starts) == (1, 1), \
            "bystander delete swept a running matrix"
        r = asyncio.run(api.profile_delete(_req({"id": "b05-active"})))  # type: ignore[arg-type]
        assert r.get("ok") and fake.stops == 2, \
            "delete of the ACTIVE profile must sweep the matrix"
        # bus-scope law: the transaction touches ONLY the bus the active
        # profile binds — a secondary bus's engine is not this preset's
        # matrix (neither killed nor revived by a bystander's save).
        assert (fake2.stops, fake2.starts) == (0, 0), \
            f"active-profile save disturbed a secondary bus: {fake2.stops}/{fake2.starts}"
    finally:
        api.midi_core.BUSES.pop("rinse-b05", None)  # type: ignore[reportOptionalMemberAccess]
        api.midi_core.BUSES.pop("rinse-b05sec", None)  # type: ignore[reportOptionalMemberAccess]
        profile_store.delete_profile("b05-active")
        profile_store.delete_profile("b05-bystander")
        profile_store.set_deck(prev_deck or ["microfreak"])


def transition_validate_before_sweep():
    """/profile/activate with a dead id and /deck
    with junk used to sweep (kill) a running matrix on their way to a named
    refusal. Validate BEFORE the destructive stop; unchanged deck = no-op."""
    prev_deck = profile_store.get_deck()
    prev_active = profile_store.get_active_profile_id()
    import dashboard.plugin_api as api
    profile_store.set_deck(["microfreak"])
    profile_store.set_active_profile_id("microfreak")
    fake = _FakeMatrixBus("primary")
    api.midi_core.BUSES["rinse-tr"] = fake  # type: ignore[reportOptionalMemberAccess]
    try:
        r = asyncio.run(api.profile_activate(_req({"id": "no-such-preset"})))  # type: ignore[arg-type]
        assert not r.get("ok") and r.get("error"), r
        assert fake.stops == 0, "dead activate killed a running matrix"
        r = asyncio.run(api.post_deck(_req({"ids": ["microfreak", 42]})))  # type: ignore[arg-type]
        assert not r.get("ok") and "refused" in (r.get("error") or ""), r
        assert fake.stops == 0, "junk deck id killed a running matrix"
        r = asyncio.run(api.post_deck(_req({"ids": ["microfreak"]})))  # type: ignore[arg-type]
        assert r.get("ok") and fake.stops == 0, "unchanged deck swept the matrix"
    finally:
        api.midi_core.BUSES.pop("rinse-tr", None)  # type: ignore[reportOptionalMemberAccess]
        profile_store.set_deck(prev_deck)
        profile_store.set_active_profile_id(prev_active)


def b05_agent_save_parity():
    # The midi_profile tool must obey the same B05 transaction as REST —
    # the chat path editing the active preset used to kill the wah too.
    import dashboard.plugin_api as api
    prev_deck = profile_store.get_deck()
    doc = good_profile("b05-agent")
    profile_store.save_profile(doc)
    profile_store.set_deck(["b05-agent"])
    profile_store.set_active_profile_id("b05-agent")
    fake = _FakeMatrixBus()
    # BOTH registries: the agent half shares the battery's flat-imported
    # midi_core, the dashboard half may hold its own path-loaded instance
    # (package-vs-flat fork) — inject where each half actually looks.
    api.midi_core.BUSES["rinse-b05b"] = fake  # type: ignore[reportOptionalMemberAccess]
    midi_core.BUSES["rinse-b05b"] = fake
    try:
        agent = _load_agent_half()
        d = json.loads(json.dumps(doc))
        d["controls"]["a"]["label"] = "A3"
        r = json.loads(agent._profile_handler(action="save", profile=d))
        assert r.get("ok") and r.get("matrix_restarted"), r
        assert fake.stops == 1 and fake.starts == 1, \
            f"agent active-save must stop+restart once: {fake.stops}/{fake.starts}"
        # matrix is running again after the restart — another active save
        # must run the SAME transaction (stop+restart, not stop-and-leave):
        r = json.loads(agent._profile_handler(action="save", profile=good_profile("b05-agent")))
        assert r.get("ok") and r.get("matrix_restarted"), r
        assert (fake.stops, fake.starts) == (2, 2), \
            f"second active save must stop+restart again: {fake.stops}/{fake.starts}"
        # nothing running -> a save must not mint a matrix out of thin air:
        fake.running = False
        r = json.loads(agent._profile_handler(action="save", profile=good_profile("b05-agent")))
        assert r.get("ok") and "matrix_restarted" not in r, r
        assert (fake.stops, fake.starts) == (2, 2), \
            "agent save started a matrix that was never running"
    finally:
        api.midi_core.BUSES.pop("rinse-b05b", None)  # type: ignore[reportOptionalMemberAccess]
        midi_core.BUSES.pop("rinse-b05b", None)
        profile_store.delete_profile("b05-agent")
        profile_store.set_deck(prev_deck or ["microfreak"])


def keyboard_inherits_from_bundle():
    # Piano law 2nd half (owner bug 2026-11-09): a user-tier override that
    # omits 'keyboard' (e.g. saved before the key existed) inherits the
    # bundled DEVICE fact, so the on-screen piano still matches the synth.
    mf = json.loads(json.dumps(profile_store.load_profile("microfreak")))
    mf.pop("keyboard")
    profile_store.save_profile(mf)                      # user tier, no keyboard
    assert profile_store.load_profile("microfreak").get("keyboard") == {"keys": 25, "first": 48}, "inheritance missed"
    mf["keyboard"] = {"keys": 61, "first": 24}          # explicit user value wins
    profile_store.save_profile(mf)
    assert profile_store.load_profile("microfreak").get("keyboard") == {"keys": 61, "first": 24}, "user keyboard overridden by bundle"
    profile_store.delete_profile("microfreak")          # back to factory for later tests


def viz_bus_scoped_and_merged():
    # Analyzer sources (owner batch 2026-11-09): /viz?bus=<name> is that bus
    # only; bus='*' merges every bus; no cross-bus leak.
    import dashboard.plugin_api as api
    mc = api.midi_core  # the SAME module instance the routes use (package vs flat import can fork)
    b2 = mc.get_bus("rinse-b")
    b2._recent_sends.append(("cc", 0, 42, 99, time.time()))
    b2._recent_in.append(("cc", 0, 43, 77, time.time()))    # physical twist
    assert any(s["num"] == 42 for s in api.get_viz(bus="rinse-b")["sends"]), "named bus missed"
    assert any(s["num"] == 42 for s in api.get_viz(bus="*")["sends"]), "wildcard merge missed"
    assert not any(s["num"] == 42 for s in api.get_viz(bus="primary")["sends"]), "bus leak into primary"
    kinds = {s["kind"] for s in api.get_viz(bus="rinse-b")["sends"]}
    assert "cc-in" in kinds, "physical inbound motion missing from /viz"


def audio_bridge_refuses_honestly():
    # /audio reads audio-strip's published state.json; in this throwaway
    # LOCALAPPDATA it must refuse with a NAMED error, never crash.
    import dashboard.plugin_api as api
    r = api.get_audio()
    assert isinstance(r, dict) and "error" in r and "audio-strip" in r["error"], f"bad audio bridge: {r}"


def failed_transition_restarts_the_matrix():
    """A12 (2026-11-10): a REST transition that stops the engine
    and THEN fails to persist must re-arm it — activate used to return with
    the matrix dead, and /deck did not even catch OSError (HTTP 500 + a dead
    wah with no undo)."""
    prev_deck = profile_store.get_deck()
    prev_active = profile_store.get_active_profile_id()
    import dashboard.plugin_api as api
    ps = api.profile_store  # the module the ROUTES call — package-vs-flat import can fork it
    profile_store.set_deck(["microfreak"])
    profile_store.set_active_profile_id("microfreak")
    fake = _FakeMatrixBus("primary")
    api.midi_core.BUSES["rinse-a12"] = fake  # type: ignore[reportOptionalMemberAccess]
    orig_set = ps.set_active_profile_id
    orig_set_deck = ps.set_deck
    try:
        def boom_active(pid):
            raise OSError("disk on fire (A12 pin)")
        ps.set_active_profile_id = boom_active
        fake.running = True
        r = asyncio.run(api.profile_activate(_req({"id": "microfreak"})))  # type: ignore[arg-type]
        assert r.get("error") and not r.get("ok"), r
        assert fake.stops >= 1 and fake.starts >= 1 and fake.running, \
            f"failed activate left the matrix dead: {r} stops={fake.stops} starts={fake.starts}"
        ps.set_active_profile_id = orig_set

        def boom_deck(ids):
            raise OSError("deck persist failed (A12 pin)")
        ps.set_deck = boom_deck
        fake.running = True
        s0 = fake.starts
        r = asyncio.run(api.post_deck(_req({"ids": ["microfreak", "behringer-bcr2000"]})))  # type: ignore[arg-type]
        assert not r.get("ok") and "refused" in (r.get("error") or ""), r
        assert fake.starts > s0 and fake.running, \
            f"failed deck left the matrix dead: {r} {s0} -> {fake.starts}"
    finally:
        ps.set_active_profile_id = orig_set
        ps.set_deck = orig_set_deck
        api.midi_core.BUSES.pop("rinse-a12", None)  # type: ignore[reportOptionalMemberAccess]
        profile_store.set_deck(prev_deck)
        if prev_active:
            profile_store.set_active_profile_id(prev_active)


def agent_failed_delete_and_activate_restore_matrix():
    """A12 agent half: the delete/rename rollback was gated on
    `warn` — None after a SUCCESSFUL stop, so it never ran. And activate was
    not wrapped at all: a failed persist left the engine dead."""
    import dashboard.plugin_api as api
    prev_deck = profile_store.get_deck()
    doc = good_profile("a12-agent")
    profile_store.save_profile(doc)
    doc3 = good_profile("a12-agent2")
    profile_store.save_profile(doc3)
    profile_store.set_deck(["a12-agent", "a12-agent2"])
    profile_store.set_active_profile_id("a12-agent")
    # bind the ACTIVE preset to the fake bus (B05: only active-profile edits
    # are transitions) — save BEFORE the boom patches
    d2 = profile_store.load_profile("a12-agent")
    d2["bus"] = "rinse-a12b"
    profile_store.save_profile(d2)
    agent = _load_agent_half()
    fake = _FakeMatrixBus("rinse-a12b")
    api.midi_core.BUSES["rinse-a12b"] = fake  # type: ignore[reportOptionalMemberAccess]
    midi_core.BUSES["rinse-a12b"] = fake
    orig_del = profile_store.delete_profile
    orig_act = profile_store.set_active_profile_id
    try:
        def boom_del(pid):
            raise OSError("delete failed (A12 pin)")
        profile_store.delete_profile = boom_del
        r = json.loads(agent._profile_handler(action="delete", id="a12-agent"))
        assert r.get("error") and not r.get("ok"), r
        assert fake.stops >= 1 and fake.starts >= 1 and fake.running, \
            f"failed agent delete must stop AND re-arm: stops={fake.stops} starts={fake.starts}"
        profile_store.delete_profile = orig_del

        def boom_act(pid):
            raise OSError("activate persist failed (A12 pin)")
        profile_store.set_active_profile_id = boom_act
        fake.running = True
        r = json.loads(agent._profile_handler(action="activate", id="a12-agent2"))
        assert r.get("error") and not r.get("ok"), r
        assert fake.running and fake.starts >= 2, \
            f"failed agent activate left the matrix dead: {r} starts={fake.starts}"
    finally:
        profile_store.delete_profile = orig_del
        profile_store.set_active_profile_id = orig_act
        api.midi_core.BUSES.pop("rinse-a12b", None)  # type: ignore[reportOptionalMemberAccess]
        midi_core.BUSES.pop("rinse-a12b", None)
        try:
            profile_store.delete_profile("a12-agent")
            profile_store.delete_profile("a12-agent2")
        except Exception:
            pass
        profile_store.set_deck(prev_deck)


print("deep-rinse battery:")
check("junk scan shows error rows, never crashes", junk_scan_no_crash)
check("bool is not a number (channel/cc/depth/font_size)", bool_is_not_a_number)
check("Windows reserved profile_id refused", windows_reserved_name_refused)
check("settings.json wrong shape tolerated", settings_wrong_shape_survives)
check("control_cc_map excludes label wire", cc_map_excludes_labels)
check("failed first cc send leaves no phantom shadow", failed_first_cc_send_leaves_no_phantom)
check("failed note-off keeps note AND ownership", failed_note_off_keeps_ownership)
check("junk velocity refused, 0 stays 0, None -> 100", junk_velocity_refused_not_crash)
check("faulted engine stop() restores the base", faulted_engine_stop_restores_base)
check("pending take refuses re-record (A4)", pending_take_refuses_re_record)
check("raw bpm string -> named error", raw_bpm_named_error)
check("same-second exports never overwrite", export_collision_never_eats)
check("agent half: export keeps take bpm, refuses junk (REST parity)", agent_half_bpm_parity)
check("agent half: activate without id is a named error", agent_activate_needs_id)
check("agent half: take_get returns real base64 .mid", agent_take_get_returns_base64)
check("PC rides recorded and exported (0xC0 in .mid)", pc_recorded_and_exported)
check("note retrigger closes the earlier note", retrigger_closes_previous_note)
check("export leaves no 0-byte take and no lock litter", export_leaves_no_junk)
check("matrix fault ends the tick at the first refusal", matrix_fault_stops_tick_at_first_refusal)
check("matrix start refuses non-numeric edge numbers", matrix_compiled_edges_refuse_junk)
check("deck: registry keeps ONE primary bus", deck_registry_one_primary)
check("deck: bus state + audit rows are device-scoped", deck_buses_state_isolated)
def analyzer_widget_law():
    # Analyzer-as-control law (owner 2026-11-09): widget 'analyzer' needs no
    # cc; w is a real span 1-8; source/type must be non-empty strings.
    import copy
    base = {"schema_version": 1, "profile_id": "anz", "device": "anz", "channel": 0,
            "sections": {"s": {"label": "S"}},
            "controls": {"a1": {"label": "spectrum", "widget": "analyzer",
                                "source": "pc", "type": "bars", "w": 4,
                                "section": "s", "x": 0, "y": 0}}}
    assert not profile_store.validate_profile(base), profile_store.validate_profile(base)
    b = copy.deepcopy(base); b["controls"]["a1"]["w"] = 99
    assert any("w must" in e for e in profile_store.validate_profile(b)), b
    b = copy.deepcopy(base); del b["controls"]["a1"]["source"]
    assert any("source" in e for e in profile_store.validate_profile(b)), b
    b = copy.deepcopy(base); b["controls"]["a1"]["type"] = 12
    assert any("type" in e for e in profile_store.validate_profile(b)), b
    b = copy.deepcopy(base); b["controls"]["a1"]["cc"] = 5  # harmless if present
    assert not profile_store.validate_profile(b), profile_store.validate_profile(b)


# ---- two-agent review sweep 2026-11-09 — pins for every fix in the batch ----

def physical_pc_reaches_recorder():
    # _on_data 0xC0 branch: a patch change made ON THE HARDWARE must reach
    # the recorder. The old branches fed notes/CCs only; the passing test fed
    # synthetic host-side events and hid the gap.
    bus = fresh_bus()
    bus.recorder.start()
    bus._on_data(0xC0, 5, 0)
    r = bus.recorder.stop(120)
    assert r.get("ok"), r
    assert any(pt["v"] == 5 for t in r["pattern"]["pc_tracks"] for pt in t["points"]), r["pattern"]
    bus.recorder.discard()


def keyboard_wire_domain():
    # first+keys must fit the 0-127 wire or the piano renders dead keys.
    mf = profile_store.load_profile("microfreak")
    bad = json.loads(json.dumps(mf))
    bad["keyboard"] = {"keys": 88, "first": 108}
    assert any("keyboard" in e for e in profile_store.validate_profile(bad)), \
        "88 keys from 108 runs off the wire and was accepted"


def analyzer_not_a_matrix_endpoint():
    # Edges to/from an analyzer refused at SAVE (was saveable, then
    # /matrix/start refused the WHOLE matrix). Wire views ignore a stray cc.
    base = good_profile()
    base["controls"]["an"] = {"label": "An", "widget": "analyzer",
                              "source": "pc", "type": "bars", "x": 2, "y": 0}
    base["matrix"] = [{"source": "a", "target": "an"}]
    assert any("analyzer" in e for e in profile_store.validate_profile(base)), base
    base["matrix"] = [{"source": "an", "target": "a"}]
    assert any("analyzer" in e for e in profile_store.validate_profile(base)), base
    b2 = json.loads(json.dumps(base))
    b2["matrix"] = []
    b2["controls"]["an"]["cc"] = 77  # stray cc on a wireless widget: harmless now
    errs = profile_store.validate_profile(b2)
    assert not errs, errs
    profile_store.save_profile(b2)
    assert 77 not in profile_store.control_cc_map("rinse-test"), \
        "stray analyzer cc entered the wire map"


def deactivate_promotes_next():
    # deck[0]==active law: deactivating drops the front and promotes the
    # survivor — the old path left active:null over a live deck[0].
    profile_store.set_deck(["microfreak", "cloudburst"])
    profile_store.set_active_profile_id(None)
    assert profile_store.get_deck() == ["cloudburst"], profile_store.get_deck()
    assert profile_store.get_active_profile_id() == "cloudburst"


def delete_prunes_presets():
    # A deleted id must not haunt the 'select preset' menu forever.
    profile_store.copy_profile("microfreak", "doomed")
    profile_store.set_deck(["doomed", "microfreak"])
    assert any("doomed" in lay for lay in profile_store.get_deck_presets())
    profile_store.delete_profile("doomed")
    assert not any("doomed" in lay for lay in profile_store.get_deck_presets()), \
        profile_store.get_deck_presets()


def get_bus_race_single_winner():
    # Double-checked creation: four threads on one new id must share ONE Bus
    # (the orphan that won the exclusive winmm port used to lock the device).
    import threading as _th
    barrier = _th.Barrier(4)
    won = []

    def grab():
        barrier.wait()
        won.append(midi_core.get_bus("rinse-race"))

    ts = [_th.Thread(target=grab) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len({id(b) for b in won}) == 1, "get_bus minted rival buses"


def viz_refuses_phantom_bus_names():
    # GET input must not mint buses: a typo'd name is a named error, not a
    # permanent phantom listed by /state and swept by /panic.
    import dashboard.plugin_api as api
    r = api.get_viz(bus="OOPS NOT A SLUG")
    assert isinstance(r, dict) and r.get("error"), r
    assert "OOPS NOT A SLUG" not in api.midi_core.BUSES, "GET minted a phantom bus"


check("deck: profile 'bus' key validated", deck_profile_bus_key)
check("deck: settings roundtrip, refuse-closed, junk-tolerant", deck_settings_roundtrip)
check("deck: presets upsert/dedupe/junk-tolerant (select preset)", deck_presets_upsert)
check("piano: keyboard declaration validated (25-key MicroFreak bed)", keyboard_decl)
check("piano: user-tier override inherits bundled keyboard", keyboard_inherits_from_bundle)
check("analyzer: /viz bus-scoped and wildcard-merged", viz_bus_scoped_and_merged)
check("analyzer: /audio bridge refuses honestly without audio-strip", audio_bridge_refuses_honestly)
check("manage: copy/rename/delete laws (deck follows, factory refused)", manage_copy_rename_delete)
check("regression review B05: active-profile save = stop->persist->restart transaction", b05_save_matrix_transaction)
check("regression review B05: agent midi_profile save obeys the same transaction", b05_agent_save_parity)
check("analyzer: widget 'analyzer' validation law", analyzer_widget_law)
check("review: physical 0xC0 reaches the recorder", physical_pc_reaches_recorder)
check("review: keyboard fits the 0-127 wire", keyboard_wire_domain)
check("review: analyzer refused as matrix endpoint + wire view", analyzer_not_a_matrix_endpoint)
check("review: deactivate promotes deck survivor (law)", deactivate_promotes_next)
check("review: delete prunes dead id from saved presets", delete_prunes_presets)
check("review: get_bus race mints exactly one Bus", get_bus_race_single_winner)
def echo_sig_before_wire():
    # regression review bug 2 (2026-11-10): the echo correlator must carry the
    # signature BEFORE midiOutShortMsg is called — winmm may fire the driver
    # callback on the calling thread before the call returns, and a signature
    # appended after it lets our own send classify as a physical twist.
    import dashboard.plugin_api as api
    mc = api.midi_core
    assert mc is not None
    bus = mc.get_bus("rinse-sig")

    class _SigOut:
        def __init__(self):
            self.sig_at_send = None

        def send_short(self, msg):
            self.sig_at_send = any(tuple(k[:4]) == ("cc", 0, 42, 77)
                                   for k in bus._recent_sends)
            return True

        def close(self):
            pass

    out = _SigOut()
    bus._out = out
    try:
        r = bus.send(kind="cc", cc=42, value=77, channel=0, source="panel")
        assert r.get("ok")
        assert out.sig_at_send is True, "signature must exist BEFORE the wire send"
        # failed send -> signature must be dropped (no message, no echo)
        class _DeadOut:
            def send_short(self, m): return False
            def close(self): pass
        bus._out = _DeadOut()
        n_before = len(bus._recent_sends)
        r2 = bus.send(kind="cc", cc=43, value=9, channel=0, source="panel")
        assert r2.get("error")
        assert len(bus._recent_sends) == n_before, "failed send left a phantom echo signature"
    finally:
        bus._out = None


def panic_clears_all_notes():
    # regression review bug 3a: panic sends all-notes-off on all 16 channels, so the
    # shadow must clear HARDWARE-held notes too — the pane must not keep
    # rendering lit keys that the wire just silenced.
    import dashboard.plugin_api as api
    mc = api.midi_core
    assert mc is not None
    bus = mc.get_bus("rinse-panic")

    class _Out:
        def send_short(self, m): return True
        def close(self): pass

    bus._out = _Out()
    try:
        bus.send(kind="note_on", note=60, velocity=90, channel=0, source="panel")  # ours
        bus._on_data(0x90, 64, 100)  # hardware-originated held note (not ours)
        assert (0, 60) in bus.notes and (0, 64) in bus.notes, bus.notes
        r = bus.panic()
        assert r.get("ok")
        assert not bus.notes, f"panic must clear ALL held notes: {bus.notes}"
        assert not bus._ours
        # (my pass-1 fix's own edge): when the wire REFUSES the
        # panic, notes may still be SOUNDING — state must NOT be cleared, or
        # disconnect strands them with no owner to release.
        class _CcReject:
            def send_short(self, m):
                return (m & 0xF0) != 0xB0  # notes pass, CC dies
            def close(self): pass
        bus._out = _CcReject()
        bus.send(kind="note_on", note=70, velocity=88, channel=1, source="panel")
        r2 = bus.panic()
        assert not r2.get("ok") and r2.get("error"), r2
        assert (1, 70) in bus.notes and (1, 70) in bus._ours, \
            f"failed panic dropped ownership of sounding notes: {bus.notes}"
    finally:
        bus._out = None


def disconnect_clears_held_notes():
    # regression review bug 3b: a NON-mirroring device never echoes our note-offs
    # back, so disconnect must clear our held notes from the shadow itself —
    # status()["notes"] must not lie after disconnect.
    import dashboard.plugin_api as api
    mc = api.midi_core
    assert mc is not None
    bus = mc.get_bus("rinse-disc")

    class _Out:
        def send_short(self, m): return True
        def close(self): pass

    bus._out = _Out()
    bus.send(kind="note_on", note=48, velocity=90, channel=2, source="panel")
    assert (2, 48) in bus.notes
    bus.disconnect()  # releases ours, then clears the shadow (no echo needed)
    assert not bus.notes, f"disconnect left stale held notes: {bus.notes}"
    assert not bus._ours


def sol2_agent_validate_before_sweep():
    # regression guard bug 1: the agent's _profile_handler swept the matrix in a
    # SHARED block before ANY validation — activate-without-id, dead ids,
    # doomed deletes/renames all killed a running wah on the way to their
    # refusal, and COPY swept the active profile's matrix over an edit that
    # never touches the active doc. Validate (refuse) first; copy NEVER sweeps.
    prev_deck = profile_store.get_deck()
    prev_active = profile_store.get_active_profile_id()
    import dashboard.plugin_api as api
    profile_store.set_deck(["microfreak"])
    profile_store.set_active_profile_id("microfreak")
    fake = _FakeMatrixBus("primary")
    # BOTH registries (b05_agent_save_parity law): the agent half sweeps the
    # battery's flat-imported midi_core; the dashboard half may hold its own.
    api.midi_core.BUSES["rinse-tr"] = fake  # type: ignore[reportOptionalMemberAccess]
    midi_core.BUSES["rinse-tr"] = fake
    agent = _load_agent_half()
    try:
        r = json.loads(agent._profile_handler(action="activate"))
        assert r.get("error") and fake.stops == 0, f"activate-no-id swept: {r} {fake.stops}"
        r = json.loads(agent._profile_handler(action="activate", id="no-such-preset"))
        assert r.get("error") and fake.stops == 0, f"dead activate swept: {r} {fake.stops}"
        r = json.loads(agent._profile_handler(action="activate", id="microfreak"))
        assert r.get("ok") and fake.stops == 0, f"re-activating the SAME surface swept: {r} {fake.stops}"
        r = json.loads(agent._profile_handler(action="copy", id="microfreak", new_id="sol2-copy"))
        assert r.get("ok") and fake.stops == 0, f"copy of ACTIVE swept the matrix: {r} {fake.stops}"
        profile_store.delete_profile("sol2-copy")
        r = json.loads(agent._profile_handler(action="delete", id="microfreak"))
        assert not r.get("ok") and r.get("error") and fake.stops == 0, \
            f"delete of bundled ACTIVE swept: {r} {fake.stops}"
        r = json.loads(agent._profile_handler(action="rename", id="microfreak", new_id="microfreak"))
        assert r.get("error") and fake.stops == 0, f"src==dst rename swept: {r} {fake.stops}"
        # REST parity: doomed delete/rename refuse BEFORE the sweep now too.
        r = asyncio.run(api.profile_delete(_req({"id": "microfreak"})))  # type: ignore[arg-type]
        assert not r.get("ok") and fake.stops == 0, f"REST bundled delete swept: {r} {fake.stops}"
        r = asyncio.run(api.profile_rename(_req({"from": "microfreak", "to": "microfreak"})))  # type: ignore[arg-type]
        assert r.get("error") and fake.stops == 0, f"REST same-id rename swept: {r} {fake.stops}"
        # A REAL transition still sweeps (the law keeps its teeth):
        r = json.loads(agent._profile_handler(action="activate", id=""))
        assert r.get("ok") and fake.stops == 1, f"deactivation must still sweep: {r} {fake.stops}"
    finally:
        api.midi_core.BUSES.pop("rinse-tr", None)  # type: ignore[reportOptionalMemberAccess]
        midi_core.BUSES.pop("rinse-tr", None)
        profile_store.set_deck(prev_deck)
        profile_store.set_active_profile_id(prev_active)


def sol2_delete_rename_atomicity():
    # regression guard bug 2: delete unlinked the file BEFORE committing settings;
    # a failed settings write deleted the profile while the deck still
    # pointed at the dead id. rename committed settings LAST too. Now the
    # settings transaction is the commit point: if it fails, NOTHING is
    # lost; a failed file unlink only leaves an unreferenced orphan.
    doc = good_profile("sol2-atom")
    profile_store.save_profile(doc)
    profile_store.set_deck(["sol2-atom", "microfreak"])
    boom = OSError("simulated AV lock on settings.json")
    _real_dump = profile_store._dump_settings
    try:
        profile_store._dump_settings = lambda *a, **k: (_ for _ in ()).throw(boom)  # type: ignore[assignment]
        try:
            profile_store.delete_profile("sol2-atom")
            raise AssertionError("delete should propagate the settings failure")
        except OSError:
            pass
        f = profile_store.user_profiles_dir() / "sol2-atom.json"
        assert f.is_file(), "settings-write failure DELETED the profile (dangling deck)"
        assert "sol2-atom" in profile_store.get_deck(), "settings-write failure mutated the deck"
        try:
            profile_store.rename_profile("sol2-atom", "sol2-atom2")
            raise AssertionError("rename should propagate the settings failure")
        except OSError:
            pass
        assert f.is_file(), "failed rename lost the source profile"
        assert not (profile_store.user_profiles_dir() / "sol2-atom2.json").is_file(), \
            "failed rename left an orphan target file (rollback missing)"
        assert profile_store.get_deck() == ["sol2-atom", "microfreak"], profile_store.get_deck()
    finally:
        profile_store._dump_settings = _real_dump  # type: ignore[attr-defined]
    # the real delete still works after the storm:
    assert profile_store.delete_profile("sol2-atom")
    assert "sol2-atom" not in profile_store.get_deck()


def sol2_rename_rewrites_presets():
    # regression guard bug 6: rename mapped the live deck but left deck_presets
    # pointing at the dead id — selecting the remembered layout then refused
    # forever ("deck refused: no profile 'old'").
    doc = good_profile("sol2-pre")
    profile_store.save_profile(doc)
    profile_store.set_deck(["sol2-pre", "microfreak"])
    presets = profile_store.get_deck_presets()
    assert ["sol2-pre", "microfreak"] in [list(p) for p in presets], presets
    profile_store.rename_profile("sol2-pre", "sol2-pre2")
    presets = profile_store.get_deck_presets()
    assert not any("sol2-pre" in p for p in presets), \
        f"presets still reference the dead id: {presets}"
    assert ["sol2-pre2", "microfreak"] in [list(p) for p in presets], presets
    assert profile_store.get_deck() == ["sol2-pre2", "microfreak"], profile_store.get_deck()
    # and the mapped preset is selectable (set_deck refuses dead ids):
    profile_store.set_deck(["sol2-pre2", "microfreak"])
    profile_store.delete_profile("sol2-pre2")


check("sol2 bug 1: agent/REST profile actions validate BEFORE sweeping; copy never sweeps", sol2_agent_validate_before_sweep)
check("sol2 bug 2: delete/rename commit settings before touching files", sol2_delete_rename_atomicity)
check("sol2 bug 6: rename rewrites deck_presets (no dead-id layouts)", sol2_rename_rewrites_presets)
check("echo signature recorded BEFORE the wire send", echo_sig_before_wire)
check("panic clears ALL held notes (hw + ours)", panic_clears_all_notes)
check("disconnect clears plugin-held notes", disconnect_clears_held_notes)
check("review: /viz refuses phantom bus names, mints none", viz_refuses_phantom_bus_names)
check("regression review: dead activate/junk deck refuse WITHOUT killing a running matrix", transition_validate_before_sweep)
check("A12: failed REST transitions re-arm the matrix (activate/deck)", failed_transition_restarts_the_matrix)
check("A12: failed agent delete/activate re-arm the matrix", agent_failed_delete_and_activate_restore_matrix)

# tidy the throwaway tier
import shutil
shutil.rmtree(_TMP, ignore_errors=True)

print("FAILURES:", ", ".join(FAIL) if FAIL else "none")
sys.exit(1 if FAIL else 0)
