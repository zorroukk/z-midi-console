"""midi_core — zero-dependency MIDI engine (Windows winmm via ctypes).

Design laws inherited from the mf-visualizer lane:
- Every send funnels through Bus.send -> one choke point -> audit log (sendlog.jsonl).
- Incoming CC echo from hardware updates state with source "panel"; our sends carry their
  own provenance. State entries are {value, ts, source}.
- Windows MIDI ports are EXCLUSIVE: one Bus instance holds one device pair; close on
  disconnect; never auto-reopen a user-closed port.
- The plugin must LOAD on non-Windows hosts too: winmm is guarded, backend reports "none".
"""
from __future__ import annotations

import ctypes
import json
import os
import threading
import time
from collections import deque
from pathlib import Path

try:  # wire-remap law lives in profile_store (single source of the maps)
    from .profile_store import (wire_xform as _wire_x, unwire_xform as _unwire_x,
                                wire_maps as _wire_maps)
except ImportError:  # standalone (dashboard/tests): repo root on sys.path
    from profile_store import (wire_xform as _wire_x, unwire_xform as _unwire_x,
                               wire_maps as _wire_maps)

try:
    _winmm = ctypes.windll.winmm
    WINMM_OK = True
except AttributeError:  # non-Windows: plugin still loads
    _winmm = None
    WINMM_OK = False

# ctypes aliases used by BOTH branches (annotations, guards) — define them
# unconditionally so non-Windows imports never see a possibly-unbound name.
_VOID_P = ctypes.c_void_p
_UINT = ctypes.c_uint

if _winmm is not None:
    _winmm.midiInGetNumDevs.restype = _UINT
    _winmm.midiOutGetNumDevs.restype = _UINT
    _winmm.midiInGetDevCapsA.argtypes = [_UINT, _VOID_P, _UINT]
    _winmm.midiInGetDevCapsA.restype = _UINT
    _winmm.midiOutGetDevCapsA.argtypes = [_UINT, _VOID_P, _UINT]
    _winmm.midiOutGetDevCapsA.restype = _UINT
    _winmm.midiInOpen.argtypes = [ctypes.POINTER(_VOID_P), _UINT, _VOID_P, _VOID_P, _UINT]
    _winmm.midiInOpen.restype = _UINT
    _winmm.midiInStart.argtypes = [_VOID_P]
    _winmm.midiInStart.restype = _UINT
    _winmm.midiInStop.argtypes = [_VOID_P]
    _winmm.midiInStop.restype = _UINT
    _winmm.midiInClose.argtypes = [_VOID_P]
    _winmm.midiInClose.restype = _UINT
    _winmm.midiOutOpen.argtypes = [ctypes.POINTER(_VOID_P), _UINT, _VOID_P, _VOID_P, _UINT]
    _winmm.midiOutOpen.restype = _UINT
    _winmm.midiOutShortMsg.argtypes = [_VOID_P, _UINT]
    _winmm.midiOutShortMsg.restype = _UINT
    _winmm.midiOutClose.argtypes = [_VOID_P]
    _winmm.midiOutClose.restype = _UINT

MIM_DATA = 0x3C3
# Real mmsystem.h values (verified 2026-11-07 — do NOT trust the common 0x10000 guess:
# that is CALLBACK_WINDOW; passing a function pointer with it = MMSYSERR_INVALPARAM rc=11).
CALLBACK_FUNCTION = 0x30000

# HMIDIIN hmi, UINT wMsg, DWORD_PTR dwInstance, DWORD_PTR dwParam1, DWORD_PTR dwParam2
# WINFUNCTYPE does not exist off Windows — defining it unconditionally crashed
# import on mac/Linux (regression review 2026-11-07); guard it so the plugin loads clean
# everywhere and only winmm-backed features report "no backend".
_MIDIINPROC = (ctypes.WINFUNCTYPE(None, _VOID_P,
                                  ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_void_p) if WINMM_OK else None)


def _caps_name(idx: int, kind: str) -> str:
    """Device name from caps struct. Empirically (probed 2026-11-07 on py3.14/winmm):
    szPname lives at offset 8 in the buffer as actually written — parse raw bytes.
    Buffer MUST be >= sizeof(MIDIOUTCAPSA) (~56B): a 48B buffer made the driver
    overrun it and crash the plugin host with 0xC0000005 (observed 2026-11-07)."""
    if _winmm is None:  # list_devices gates on this; belt + type narrowing
        return ""
    buf = ctypes.create_string_buffer(64)
    fn = _winmm.midiInGetDevCapsA if kind == "in" else _winmm.midiOutGetDevCapsA
    if fn(idx, ctypes.cast(buf, _VOID_P), 64) != 0:
        return ""
    raw = bytes(buf.raw[8:40])
    return raw.split(b"\x00", 1)[0].decode("mbcs", errors="replace")


def list_devices():
    if _winmm is None:
        return []
    devs = []
    for i in range(_winmm.midiInGetNumDevs()):
        devs.append({"dir": "in", "index": i, "name": _caps_name(i, "in")})
    for i in range(_winmm.midiOutGetNumDevs()):
        devs.append({"dir": "out", "index": i, "name": _caps_name(i, "out")})
    return devs


class _MidiIn:
    def __init__(self, idx: int, on_data):
        self._idx = idx
        self._on_data = on_data
        self._h = ctypes.c_void_p()
        if _MIDIINPROC is None:  # only ever built behind connect()'s winmm gate
            raise RuntimeError("no MIDI backend on this OS (winmm is Windows-only)")
        self._proc = _MIDIINPROC(self._callback)  # reference MUST be kept alive

    def _callback(self, hmi, wmsg, inst, p1, p2):
        # Runs on the winmm driver thread: an escaping exception = process death,
        # so this body must never raise.
        try:
            if wmsg == MIM_DATA:
                m = int(p1) & 0xFFFFFF
                self._on_data(m & 0xFF, (m >> 8) & 0xFF, (m >> 16) & 0xFF)
        except Exception:
            pass

    def open(self):
        if _winmm is None:  # connect() gates on WINMM_OK; this is the belt (and the type narrowing)
            raise RuntimeError("no MIDI backend on this OS (winmm is Windows-only)")
        rc = _winmm.midiInOpen(ctypes.byref(self._h), self._idx, self._proc, None,
                               CALLBACK_FUNCTION)
        if rc != 0:
            raise RuntimeError(f"midiInOpen failed rc={rc} (device busy? another app holds it)")
        rc = _winmm.midiInStart(self._h)
        if rc != 0:
            _winmm.midiInClose(self._h)
            self._h = ctypes.c_void_p()
            raise RuntimeError(f"midiInStart failed rc={rc}")

    def close(self):
        if self._h:
            if _winmm is None:
                raise RuntimeError("no MIDI backend on this OS (winmm is Windows-only)")
            _winmm.midiInStop(self._h)
            _winmm.midiInClose(self._h)
            self._h = ctypes.c_void_p()


class _MidiOut:
    def __init__(self, idx: int):
        self._idx = idx
        self._h = ctypes.c_void_p()

    def open(self):
        if _winmm is None:
            raise RuntimeError("no MIDI backend on this OS (winmm is Windows-only)")
        rc = _winmm.midiOutOpen(ctypes.byref(self._h), self._idx, None, None, 0)
        if rc != 0:
            raise RuntimeError(f"midiOutOpen failed rc={rc} (device busy?)")

    def send_short(self, msg: int) -> bool:
        if _winmm is None:
            return False
        return _winmm.midiOutShortMsg(self._h, msg & 0x00FFFFFF) == 0

    def close(self):
        if self._h:
            if _winmm is None:
                raise RuntimeError("no MIDI backend on this OS (winmm is Windows-only)")
            _winmm.midiOutClose(self._h)
            self._h = ctypes.c_void_p()


_SENDLOG_IO = threading.Lock()  # the ONE lock serializing sendlog appends AND rotation across every bus (shared journal file; per-bus locks protected nothing)


class Bus:
    """One shared MIDI session: device pair + state + audit trail.
    Multi-device law (deck feature 2026-11-09): one Bus per on-screen device;
    winmm ports are exclusive PER PORT, so two Buses on two DIFFERENT device
    pairs are legal, and two handles on ONE pair fail with rc!=0. Buses are
    reached through the BUSES registry keyed by bus id (profiles bind to a
    bus id; 'primary' is the historical singleton)."""

    def __init__(self, bus_id: str = "primary"):
        self.bus_id = bus_id
        self._lock = threading.RLock()
        # Lifecycle lock (#1, 2026-11-07): connect/disconnect/
        # matrix_start/matrix_stop are serialized here — check-then-assign on
        # self._matrix raced and orphaned a second engine. Kept SEPARATE from
        # _lock: never hold _lock across thread joins or port closes (the
        # deadlock law), and the engine loop never takes _life.
        self._life = threading.RLock()
        self._out = None
        self._inp = None
        self.device = None
        self.cc_state = {}   # (channel, cc) -> {"value", "ts", "source"}
        self.notes = {}      # (channel, note) -> ts
        self._ours = set()   # (channel, note) started BY US — released on disconnect (#5)
        self._recent_sends = deque(maxlen=200)  # (kind, ch, num, val, ts) — echo correlator (#11)
        self._recent_in = deque(maxlen=200)     # (kind, ch, num, val, ts) — PHYSICAL motion (device→host, non-echo), analyzer feed only
        self.events = deque(maxlen=300)
        self._seq = 0        # monotonic event cursor (rebind/learn filtering, #2)
        self._audit_n = 0    # sendlog rotation counter (#17)
        self._rot_lock = threading.Lock()  # deep rinse: serialize sendlog rotation
        self.sendlog_on = True
        self._matrix = None  # live MatrixEngine while running (one at a time)
        try:  # package context (agent half)
            from .automation import Recorder
        except ImportError:  # standalone (dashboard half / tests)
            from automation import Recorder
        self.recorder = Recorder()

    # --- audit ----------------------------------------------------------
    def sendlog_path(self) -> Path:
        # Single-source law: the plugin data dir is profile_store.data_dir()
        # (profiles, exports, settings, sendlog all live together).
        try:  # package context (agent half)
            from .profile_store import data_dir
        except ImportError:  # standalone (dashboard half / tests)
            from profile_store import data_dir
        d = data_dir()
        d.mkdir(parents=True, exist_ok=True)  # sendlog may be the FIRST write on a fresh install
        return d / "sendlog.jsonl"

    def _audit(self, ev: dict) -> None:
        ev.setdefault("ts", time.time())
        ev.setdefault("bus", self.bus_id)  # deck: audit rows carry the device axis
        with self._lock:
            self._seq += 1
            ev["seq"] = self._seq  # monotonic cursor: pane arms rebind/learn against this (#2)
            self.events.append(dict(ev))
            self.recorder.feed(ev)  # record-a-ride sees every audited event
            self._audit_n += 1      # deep rinse: counter under the lock (was a lost-update race)
            rotate = self._audit_n % 512 == 0
        if self.sendlog_on:
            # the journal is ONE shared file — every bus wrote
            # into it while rotation (read tail -> os.replace) held only a
            # PER-BUS lock. An append landing between the tail read and the
            # replace was silently destroyed, and two buses' rotators could
            # race each other. One module lock over append AND rotation.
            try:
                with _SENDLOG_IO:
                    with open(self.sendlog_path(), "a", encoding="utf-8") as f:
                        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                    if rotate:
                        self._rotate_sendlog()
            except OSError:
                pass

    def _rotate_sendlog(self) -> None:
        """#17: the audit journal never rotated — a running matrix writes
        ~30 rows/s forever. Past 8 MB, keep the last ~1 MB (recent history
        is what /sendlog and debugging ever need)."""
        with self._rot_lock:  # deep rinse: one rotator at a time; a failed
            try:              # replace must not leak the .tmp forever
                p = self.sendlog_path()
                if p.stat().st_size <= 8 * 1024 * 1024:
                    return
                with open(p, "rb") as f:
                    f.seek(0, 2)
                    start = max(0, f.tell() - 1024 * 1024)
                    f.seek(start)
                    if start:
                        # Deep rinse (off-by-one): only drop the first line when
                        # it is genuinely partial — a line-aligned boundary used
                        # to silently eat one intact record per rotation.
                        f.seek(start - 1)
                        if f.read(1) != b"\n":
                            f.readline()
                    tail = f.read()
                tmp = p.with_suffix(".jsonl.tmp")
                try:
                    with open(tmp, "wb") as f:
                        f.write(tail)
                    os.replace(tmp, p)
                except OSError:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
            except OSError:
                pass

    # --- incoming -------------------------------------------------------
    def _is_echo(self, kind: str, num: int, val: int, ch: int) -> bool:
        """#11: an incoming event that matches one WE recently sent is our own
        returned copy, not a physical move. Label it 'echo' — state still
        mirrors (mirror law), but recorder/learn must not treat it as motion."""
        now = time.time()
        with self._lock:
            for k, c, n, v, ts in reversed(self._recent_sends):
                if now - ts > 0.25:
                    break
                if k == kind and c == ch and n == num and v == val:
                    return True
        return False

    def _on_data(self, status: int, d1: int, d2: int) -> None:
        hi, ch = status & 0xF0, status & 0x0F
        if hi in (0x90, 0x80):
            vel = d2 if hi == 0x90 else 0
            src = "echo" if self._is_echo("note", d1, vel if hi == 0x90 else 0, ch) else "panel"
            with self._lock:
                # regression guard bug 7: appends join the lock so the /viz snapshot
                # (which reads under this same lock) sees whole frames.
                if src != "echo":
                    self._recent_in.append(("note", ch, d1, vel, time.time()))  # analyzer sees physical keys
                if hi == 0x90 and d2 > 0:
                    self.notes[(ch, d1)] = time.time()
                else:
                    self.notes.pop((ch, d1), None)
            self._audit({"kind": "note", "note": d1,
                         "velocity": vel,
                         "channel": ch, "source": src})
        elif hi == 0xB0:
            src = "echo" if self._is_echo("cc", d1, d2, ch) else "panel"
            # Wire-remap law: inbound WIRE values are inverse-mapped to the
            # SURFACE domain before touching shadow/audit (identity when the
            # cc declares no transform — the common case is untouched).
            d2s = _unwire_x(self.bus_id, d1, d2)
            with self._lock:
                if src != "echo":
                    self._recent_in.append(("cc", ch, d1, d2s, time.time()))  # analyzer sees physical knob twists
                # An echoed value on a TRANSFORMED cc must not overwrite our
                # exact shadow: double rounding (surface->wire->surface) can
                # land ±1 off, and a ±1 flap is a false 'physical twist' to a
                # watch lane. Untransformed cc: identity, write as before.
                if src != "echo" or _wire_maps().get(self.bus_id, {}).get(d1) is None:
                    self.cc_state[(ch, d1)] = {"value": d2s, "ts": time.time(), "source": src}
            self._audit({"kind": "cc", "cc": d1, "value": d2s, "channel": ch, "source": src})
        elif hi == 0xC0:
            # Physical program change (Cloudburst patch switch etc.): the
            # recorder's PC track was fed only by host-side sends — a patch
            # change made ON the hardware never reached audit (review 2026-11-09).
            src = "echo" if self._is_echo("pc", 0, d1, ch) else "panel"
            self._audit({"kind": "program", "program": d1, "channel": ch, "source": src})

    # --- session --------------------------------------------------------
    def connect(self, name_substr: str) -> dict:
        if _winmm is None:
            return {"error": "no MIDI backend on this OS (winmm is Windows-only)"}
        with self._life:  # lifecycle law (#1): one session transition at a time
            needle = name_substr.lower()
            ins = [d for d in list_devices() if d["dir"] == "in" and needle in d["name"].lower()]
            outs = [d for d in list_devices() if d["dir"] == "out" and needle in d["name"].lower()]
            if not outs:
                return {"error": f"no output device matching {name_substr!r}"}
            self.matrix_stop()  # never modulate blindly across a port change
            self.disconnect()
            out = _MidiOut(outs[0]["index"])
            out.open()
            inp = None
            if ins:
                inp = _MidiIn(ins[0]["index"], self._on_data)
                try:
                    inp.open()
                except RuntimeError:
                    out.close()
                    raise
            with self._lock:
                self._out, self._inp = out, inp
                self.device = {"out": outs[0]["name"], "in": ins[0]["name"] if ins else None}
                self.cc_state.clear()
                self.notes.clear()
                self._ours.clear()
            # Router settle: reopening a port right after closing it can leave the
            # driver/router half-primed and the first message undelivered (observed
            # intermittently with loopMIDI on re-connect). A short settle + the
            # caller's natural send cadence covers it.
            time.sleep(0.15)
            self._audit({"kind": "connect", "device": self.device, "source": "session"})
            return {"ok": True, "device": self.device, "mirror": bool(ins)}

    def _release_ours(self, out) -> bool:
        """#5: notes WE started must not be stranded by a lifecycle action.
        Sends note-offs straight on the still-open handle (Bus.send is already
        torn down by the time we close). Returns False if any release failed."""
        ok = True
        with self._lock:
            ours = sorted(self._ours)
        for (ch, n) in ours:
            if not out.send_short((0x80 | ch) | (n << 8)):
                ok = False
        return ok

    def disconnect(self) -> None:
        with self._life:  # lifecycle law (#1)
            self.matrix_stop()  # a running matrix must not outlive its port
            with self._lock:
                inp, out, self._inp, self._out = self._inp, self._out, None, None
                had = self.device
                self.device = None
            # Release plugin-started notes BEFORE closing the output (#5) —
            # a normal disconnect must not strand a held note on the synth.
            released = True
            with self._lock:
                ours = sorted(self._ours)  # snapshot BEFORE release (regression review bug 3)
            if out is not None:
                released = self._release_ours(out)
            # Law: close ports OUTSIDE the state lock. midiInClose waits for an
            # in-flight callback; the callback (mirror echo of our restore
            # sends) waits on _lock — holding it across close deadlocks
            # (observed: disconnect with a running matrix hung forever).
            if inp is not None:
                inp.close()
            if out is not None:
                out.close()
            with self._lock:
                # regression review bug 3 (2026-11-10): a NON-mirroring device never
                # echoes our note-offs back, so the shadow kept our held notes
                # forever — status()["notes"] lied after disconnect. The
                # release we just sent IS the truth.
                for key in ours:
                    self.notes.pop(key, None)
                self._ours.clear()
            if had:
                self._audit({"kind": "disconnect", "device": had, "source": "session",
                             "note_release_failed": not released})

    # --- outgoing (single choke point) -----------------------------------
    def send(self, kind: str = "cc", cc=None, value=None, note=None, velocity=None,
             channel: int = 0, source: str = "agent") -> dict:
        with self._lock:
            if self._out is None:
                return {"error": "no device connected"}
            try:
                ch = int(channel)
            except (TypeError, ValueError):
                return {"error": "channel must be an int"}
            if not 0 <= ch <= 15:
                return {"error": "channel must be 0-15"}
            if kind == "cc":
                if cc is None or value is None:
                    return {"error": "cc and value required"}
                try:
                    cc_i, v = int(cc), int(value)
                except (TypeError, ValueError):
                    return {"error": "cc and value must be ints"}
                if not 0 <= cc_i <= 127:
                    return {"error": "cc must be 0-127 (refused, not masked)"}
                v = max(0, min(127, v))
                # Wire-remap law (invert/out_min/out_max): shadow state and
                # everything above it speak the SURFACE domain; only the
                # physical wire carries the transform. The echo correlator
                # records the WIRE value — that is what comes back.
                w = _wire_x(self.bus_id, cc_i, v)
                # Ordering law: shadow state is written BEFORE the wire send. A
                # mirroring device echoes the CC back on the callback thread; if we
                # sent first, that echo could land before this write and then be
                # clobbered by our own provenance (observed as a flaky battery).
                prev = self.cc_state.get((ch, cc_i))
                self.cc_state[(ch, cc_i)] = {"value": v, "ts": time.time(), "source": source}
                # Signature-before-wire law (regression review bug 2, 2026-11-10): the
                # echo correlator must carry the signature BEFORE the message
                # physically leaves. winmm may fire the driver callback on the
                # calling thread before midiOutShortMsg returns; a signature
                # appended after the call let our own send classify as a
                # physical twist (false rebind prompts, double-recorded takes).
                sent_key = ("cc", ch, cc_i, w)  # WIRE value — the correlator must match what physically went out
                self._recent_sends.append((*sent_key, time.time()))
                ok = self._out.send_short((0xB0 | ch) | (cc_i << 8) | (w << 16))
                if not ok:
                    self._recent_sends.pop()  # no message, no echo — drop the signature (all appends hold _lock)
                    # #4: a failed wire send must not leave a convincing shadow —
                    # roll the mirror back COMPLETELY (no echo can arrive for a
                    # message that never went out): restore the previous entry,
                    # or REMOVE the fabricated one when there was none (deep
                    # rinse: the first-ever send to a dead wire used to leave a
                    # phantom value in /state forever).
                    if prev is None:
                        self.cc_state.pop((ch, cc_i), None)
                    else:
                        self.cc_state[(ch, cc_i)] = prev
                ev = {"kind": "cc", "cc": cc_i, "value": v, "channel": ch, "source": source}
            elif kind in ("note_on", "note_off"):
                if note is None:
                    return {"error": "note required"}
                try:
                    n = int(note)
                except (TypeError, ValueError):
                    return {"error": "note must be an int"}
                if not 0 <= n <= 127:
                    return {"error": "note must be 0-127 (refused, not masked)"}
                if kind == "note_off":
                    vel = 0
                else:
                    # Deep rinse: junk velocity used to raise ValueError straight
                    # through REST (500); and `velocity or 100` turned an
                    # explicit 0 into 100. 0 is legal note_on velocity (wire
                    # semantics: note-on with vel 0 IS a note-off).
                    try:
                        vel = 100 if velocity is None else max(0, min(127, int(velocity)))
                    except (TypeError, ValueError):
                        return {"error": "velocity must be an int"}
                # Same ordering law as the cc lane (shadow before wire).
                was_ours = (ch, n) in self._ours
                if vel:
                    prev_note = self.notes.get((ch, n))
                    self.notes[(ch, n)] = time.time()
                    self._ours.add((ch, n))  # #5: ours -> released on disconnect
                else:
                    prev_note = self.notes.get((ch, n))
                    self.notes.pop((ch, n), None)
                    self._ours.discard((ch, n))
                sent_key = ("note", ch, n, vel)  # signature-before-wire law (regression review bug 2)
                self._recent_sends.append((*sent_key, time.time()))
                ok = self._out.send_short(((0x90 if kind == "note_on" else 0x80) | ch)
                                          | (n << 8) | (vel << 16))
                if not ok:
                    self._recent_sends.pop()  # no message, no echo (regression review bug 2)
                    # #4 rollback for the note lane (see cc lane law above).
                    if vel:
                        self._ours.discard((ch, n))
                        if prev_note is None:
                            self.notes.pop((ch, n), None)
                        else:
                            self.notes[(ch, n)] = prev_note
                    else:
                        if prev_note is not None:
                            self.notes[(ch, n)] = prev_note
                            # Deep rinse: a failed note-off that restores the
                            # held note must ALSO restore who owns it — else the
                            # shadow says "held" while _ours says "nobody's job"
                            # and disconnect() strands it (#5 release law).
                            if was_ours:
                                self._ours.add((ch, n))
                ev = {"kind": "note", "note": n, "velocity": vel, "channel": ch,
                      "source": source}
            elif kind == "pc":
                if value is None:
                    return {"error": "value (program) required"}
                try:
                    p = int(value)
                except (TypeError, ValueError):
                    return {"error": "program must be an int"}
                if not 0 <= p <= 127:
                    return {"error": "program must be 0-127 (refused, not masked)"}
                sent_key = ("pc", ch, 0, p)  # echo correlator for the 0xC0 inbound branch (was None — our own PC came back as 'panel'); signature-before-wire (regression review bug 2)
                self._recent_sends.append((*sent_key, time.time()))
                ok = self._out.send_short((0xC0 | ch) | (p << 8))
                if not ok:
                    self._recent_sends.pop()
                ev = {"kind": "program", "program": p, "channel": ch, "source": source}
            else:
                return {"error": f"unknown kind {kind!r}"}
            if not ok:
                return {"error": "midiOutShortMsg failed (device gone?)"}
        # Deep rinse (lifecycle law): the audit append — open/write, and every
        # 512th event a ~1 MB rotation — is disk IO and must NOT run under
        # _lock: the winmm callback thread needs _lock for incoming events and
        # was queueing behind our writes at 30 Hz matrix rates.
        self._audit(ev)
        return {"ok": True, **ev}

    def panic(self) -> dict:
        """All notes off on every channel + release our held notes (a Panic
        button belongs on a hardware surface; #5)."""
        sent, errors = 0, []
        for ch in range(16):
            r = self.send(kind="cc", cc=123, value=0, channel=ch, source="panic")
            if r.get("ok"):
                sent += 1
            else:
                errors.append(r["error"])
                break
        with self._lock:
            # Releasing means the STATE changes too — a "panic" that leaves
            # held notes in our shadow is a lie the pane would keep rendering.
            # regression review bug 3 (2026-11-10): clearing only _ours left
            # HARDWARE-originated held notes lit forever — all-notes-off went
            # out on all 16 channels, so the shadow must agree with the wire.
            # (same day, own-goal fix): the wire must ACTUALLY
            # have carried the panic. If any CC 123 send failed, the notes on
            # the refused channels may still be SOUNDING — clearing _ours +
            # notes then strands them (disconnect has nothing left to release,
            # reproduced). On failure: state untouched, error surfaced, the
            # caller can retry while the synth is still ours to silence.
            if not errors:
                self.notes.clear()
                self._ours.clear()
        return {"ok": not errors, "all_notes_off_channels": sent,
                **({"error": errors[0]} if errors else {}),
                **({"detail": "held notes kept in shadow — the wire refused the panic, ownership intact so disconnect/release still covers them"}
                   if errors else {})}

    # --- software matrix (ONE engine, owned by the Bus: REST + agent tools
    # call the same methods; a zombie engine can never outlive the port) -----
    def matrix_start(self, edges, lfo, cc_of_cid, channel=0) -> dict:
        with self._life:  # lifecycle law (#1): no check-then-assign race, no orphans
            if self._out is None:
                return {"error": "not connected — connect a device first"}
            if self._matrix is not None:
                if self._matrix.status().get("running"):
                    return {"error": "matrix already running"}
                # Deep rinse: a faulted engine (wire refused mid-run, thread
                # exited) still holds its captured bases. Restore them BEFORE
                # a new engine captures fresh ones — otherwise the new base is
                # the polluted last-modulated value and "Stop restores" lies.
                self._matrix.stop()
                self._matrix = None
            try:  # package context (agent half)
                from .matrix_engine import MatrixEngine
            except ImportError:  # standalone (dashboard half / tests)
                from matrix_engine import MatrixEngine

            def getv(cid, _cc=cc_of_cid, _ch=channel):
                cc = _cc.get(cid)
                if cc is None:
                    return None
                ent = self.cc_state.get((_ch, cc))
                return ent["value"] if ent else None

            eng = MatrixEngine(self.send, getv)
            # Ownership is RESERVED before the worker can run (#1): a second
            # start() can no longer slip past the check and orphan an engine.
            self._matrix = eng
            r = eng.start(edges, lfo, cc_of_cid, channel)
            if not r.get("ok"):
                self._matrix = None
            return r

    def matrix_stop(self) -> dict:
        with self._life:  # lifecycle law (#1)
            if self._matrix is None:
                return {"ok": True, "restored": []}  # idempotent: nothing was running
            eng, self._matrix = self._matrix, None
            # The engine is released whether or not every restore send landed
            # (#4): the caller sees ok:false + failed[] and must not be told
            # values were restored when the wire refused them.
            return eng.stop()

    def matrix_status(self) -> dict:
        return self._matrix.status() if self._matrix else {"running": False}

    # --- state ----------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            base = {
                "bus": self.bus_id,
                "backend": "winmm" if WINMM_OK else "none",
                "connected": self.device,
                "event_seq": self._seq,  # #2: pane arms rebind/learn against this cursor
                "cc": [{"channel": ch, "cc": cc, **v}
                       for (ch, cc), v in sorted(self.cc_state.items())],
                "notes": [{"channel": ch, "note": n}
                          for (ch, n) in sorted(self.notes)],
                "events": list(self.events)[-40:],
            }
        # Law: matrix_status OUTSIDE _lock. The engine loop holds engine.lock
        # while calling send() (which takes _lock); taking _lock here and then
        # engine.lock is the other half of an AB-BA deadlock — hit in practice
        # when the pane polls /state every 600ms against a running matrix.
        base["matrix"] = self.matrix_status()
        base["recorder"] = self.recorder.status()
        return base


# --- bus registry (deck feature) ------------------------------------------
# 'primary' IS the historical module-global BUS — every pre-deck route, tool
# and test keeps hitting it unchanged. Extra buses are created on demand and
# share the one sendlog (rows carry a "bus" field).
BUSES: dict = {}
_BUSES_LOCK = threading.Lock()  # get_bus creation guard (review 2026-11-09): two threads racing on the same new id used to each build a Bus; the loser's orphan could win the exclusive winmm port and lock the device until process restart


def get_bus(bus_id: str = "primary") -> "Bus":
    bid = str(bus_id or "primary")
    b = BUSES.get(bid)
    if b is None:
        with _BUSES_LOCK:  # double-checked: re-read inside the lock, first creator wins, everyone else shares it
            b = BUSES.get(bid)
            if b is None:
                b = Bus(bid)
                b.sendlog_on = BUS.sendlog_on  # inherit the config switch (register())
                BUSES[bid] = b
    return b


BUS = Bus()
BUSES["primary"] = BUS  # the historical singleton IS the primary bus (one-Bus law)
