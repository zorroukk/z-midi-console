"""automation — record a ride, export .mid.

Closes the automation loop: twist knobs / play notes while recording, stop, get
a Standard MIDI File (format 1) that FL Studio / Ableton / any DAW imports
natively. Refuse-closed: an empty take is a named error, never an empty file.

Laws: fed from the Bus audit choke point (records BOTH hardware echoes and our
sends — what you hear is what gets exported); stdlib only; exports land under
the plugin data dir (survives updates, same pattern as profiles).
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path

try:  # package context (agent half)
    from .profile_store import data_dir
    from .smf import format1
except ImportError:  # standalone (dashboard half / tests)
    # Review 2026-11-09: hoisted from per-call function bodies — the shim was
    # re-executed on every export/list call for no reason (automation ->
    # profile_store/smf is one-way, no import cycle to dodge).
    try:  # regression review debt sweep: a SIBLING plugin may own the bare alias in the
          # shared web-server process — prefer OUR plugin-unique aliases
          # (plugin_api._load_sibling registers them before executing this).
        from zmc_profile_store import data_dir  # type: ignore[import-not-found]
        from zmc_smf import format1  # type: ignore[import-not-found]
    except ImportError:
        from profile_store import data_dir
        from smf import format1


class Recorder:
    """One take at a time. Bus._audit feeds every audited event through feed().

    #9 (): the feed runs under the BUS lock while start/stop come
    from REST/tool threads — synchronization lives INSIDE the Recorder now;
    callers must not need the Bus lock to be safe.
    #11: events correlated as our own echo (source "echo") are NOT musical
    motion — they are our outgoing packets returning; capture policy ignores
    them so a take records moves, not our own send/echo pairs twice.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.on = False
        self._t0 = 0.0
        self._cc: dict[tuple, list] = {}         # (ch,cc) -> [{"t_ms","v"}] ascending
        self._pc: dict[int, list] = {}           # ch -> [{"t_ms","v"}] program changes
        self._pending: dict[tuple, tuple] = {}  # (ch,note) -> (on_ms, vel)
        self._notes: list = []
        self._final: dict | None = None         # #7: stopped take awaiting a successful export

    def start(self) -> dict:
        with self._lock:
            if self.on:
                return {"error": "already recording"}
            if self._final is not None:
                # A4 (2026-11-10): starting a new take used to
                # silently DESTROY the stopped-but-unexported one (repro: take
                # value 10, re-record, stop -> only pending is 99). The pending
                # take is precious — export retry and Discard are its exits.
                return {"error": "a recorded take is waiting to export — "
                                 "export it or press Discard before recording again"}
            self._cc, self._pc, self._pending, self._notes = {}, {}, {}, []
            self._t0 = time.time()
            self.on = True
            return {"ok": True}

    def feed(self, ev: dict) -> None:
        if ev.get("source") == "echo":
            return  # #11: our own returned packet is not a performance move
        with self._lock:
            if not self.on:
                return
            # an event can arrive stamped BEFORE
            # start() lands (bus lock contention) -> negative t_ms -> the
            # export dies on delta ticks and strands the take. Clamp at capture.
            t = max(0.0, round((ev.get("ts", time.time()) - self._t0) * 1000.0, 1))
            k = ev.get("kind")
            if k == "cc":
                # channel is part of the identity: CC 23 on ch0 and ch1 are two rides
                pts = self._cc.setdefault((int(ev.get("channel", 0)), int(ev["cc"])), [])
                # coalesce identical consecutive values (keep the earliest timestamp)
                if pts and pts[-1]["v"] == int(ev["value"]):
                    return
                pts.append({"t_ms": t, "v": int(ev["value"])})
            elif k == "program":
                # Review 2026-11-09 (device-agnostic): PC-bank devices (the
                # Cloudburst class) switch patches mid-ride; PCs were audited
                # but silently dropped, breaking "what you hear is what gets
                # exported" for that whole device class.
                pts = self._pc.setdefault(int(ev.get("channel", 0)), [])
                v = int(ev["program"])
                if pts and pts[-1]["v"] == v:
                    return
                pts.append({"t_ms": t, "v": v})
            elif k == "note":
                key = (int(ev.get("channel", 0)), int(ev["note"]))
                if ev.get("velocity"):
                    # Review: a retrigger without an intervening note-off used
                    # to overwrite the pending note — two ons, one off, a
                    # stuck note in the DAW. Close the earlier one first.
                    prev = self._pending.pop(key, None)
                    if prev is not None:
                        self._notes.append({"t_ms": prev[0], "dur": max(1.0, t - prev[0]),
                                            "note": key[1], "vel": prev[1],
                                            "channel": key[0]})
                    self._pending[key] = (t, int(ev["velocity"]))
                else:
                    on = self._pending.pop(key, None)
                    if on is not None:
                        self._notes.append({"t_ms": on[0], "dur": max(1.0, t - on[0]),
                                            "note": key[1], "vel": on[1],
                                            "channel": key[0]})

    def stop(self, bpm: float | str = 120.0) -> dict:
        """Returns {ok, pattern} or {error}. Hanging notes close at STOP time
        (#8: the old code closed them at the LAST EVENT timestamp — a note
        held into silence exported as a 1 ms stub). bpm is validated FIRST:
        a bad tempo must not consume the take. (str allowed: REST/agent
        layers pass raw JSON values through.)"""
        try:
            bpm = float(bpm)
        except (TypeError, ValueError):
            return {"error": "bpm must be a number"}
        if not (10.0 <= bpm <= 999.0):
            return {"error": "bpm must be 10-999"}
        with self._lock:
            if not self.on:
                return {"error": "not recording"}
            stop_ms = round((time.time() - self._t0) * 1000.0, 1)
            for (ch, n), (on, vel) in self._pending.items():
                self._notes.append({"t_ms": on, "dur": max(1.0, stop_ms - on),
                                    "note": n, "vel": vel, "channel": ch})
            self._pending = {}
            if not self._notes and not self._cc and not self._pc:
                self.on = False
                return {"error": "nothing recorded"}
            pattern = {"bpm": bpm, "name": "z-midi-console take",
                       "notes": self._notes,
                       "cc_tracks": [{"cc": cc, "channel": ch, "points": pts}
                                     for (ch, cc), pts in sorted(self._cc.items())],
                       "pc_tracks": [{"channel": ch, "points": pts}
                                     for ch, pts in sorted(self._pc.items())]}
            overwritten = self._final is not None
            self.on = False
            self._final = pattern  # #7: survives until an export succeeds
            return {"ok": True, "pattern": pattern, "notes": len(self._notes),
                    "cc_tracks": len(pattern["cc_tracks"]),
                    "pc_tracks": len(pattern["pc_tracks"]), "duration_ms": stop_ms,
                    # Deep rinse: #7 guarded a pending take against EXPORT
                    # failure — but the next normal record->stop cycle ate it
                    # silently. Same law, said out loud.
                    **({"pending_overwritten": True} if overwritten else {})}

    def pending(self) -> dict | None:
        """#7: the stopped take that has not been exported yet."""
        with self._lock:
            return self._final

    def discard(self) -> dict:
        with self._lock:
            # regression guard UI4: the tooltip promised "Stop recording WITHOUT
            # producing a .mid" — but discard only ever cleared a STOPPED
            # pending take: pressing it mid-recording answered "no pending
            # take" and the tape kept rolling. It now kills BOTH halves: an
            # in-flight take (stop, nothing exported) and any held pending.
            was_on = self.on
            had = self._final is not None
            if not was_on and not had:
                return {"error": "nothing to discard — not recording and no pending take"}
            self.on = False
            self._final = None
            self._cc, self._pc, self._pending, self._notes = {}, {}, {}, []
            return {"ok": True, "stopped_recording": was_on, "dropped_pending": had}

    def clear_pending(self) -> None:
        with self._lock:
            self._final = None

    def status(self) -> dict:
        with self._lock:
            if not self.on:
                return {"recording": False,
                        **({"pending_take": True} if self._final else {})}
            return {"recording": True, "elapsed_ms": round((time.time() - self._t0) * 1000),
                    "cc_tracks": len(self._cc), "pc_tracks": len(self._pc),
                    "notes": len(self._notes)}


def exports_dir() -> Path:
    d = data_dir() / "exports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def export_mid(pattern: dict, name: str = "take") -> str:
    """pattern dict -> .mid file (atomic write). Returns the path.
    #6: an existing destination is NEVER replaced — same-name exports in the
    same second get -2, -3... suffixes (os.replace alone silently ate takes)."""
    data = format1(pattern)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:40] or "take"
    stamp = time.strftime('%Y%m%d-%H%M%S')
    d = exports_dir()
    target = d / f"{stamp}-{safe}.mid"
    # Review 2026-11-09: the reservation used to be the FINAL .mid name — a
    # crash between reserve and write left a 0-byte .mid that the take library
    # indexed as real. Reserve a .mid.lock sibling instead: the .mid appears
    # only when complete, and list_takes (glob *.mid) never sees the lock.
    lock = d / f"{stamp}-{safe}.mid.lock"
    n = 1
    while True:
        # Review: bump on EITHER an existing complete take (the released lock
        # records no history — without this, same-second exports silently
        # re-collided onto one name, the exact #6 take-eater) OR a live
        # sibling lock (a concurrent export mid-write).
        if target.exists():
            n += 1
            target = d / f"{stamp}-{safe}-{n}.mid"
            lock = d / f"{stamp}-{safe}-{n}.mid.lock"
            continue
        try:
            _rfd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(_rfd)
            # double-check under the lock — a
            # concurrent writer can complete between our exists() read and
            # our O_EXCL, and we would then replace their take silently.
            if target.exists():
                n += 1
                target = d / f"{stamp}-{safe}-{n}.mid"
                lock = d / f"{stamp}-{safe}-{n}.mid.lock"
                try:
                    os.unlink(str(lock.parent / f"{stamp}-{safe}-{n - 1}.mid.lock"))
                except OSError:
                    pass
                continue
            break
        except FileExistsError:
            n += 1
            target = d / f"{stamp}-{safe}-{n}.mid"
            lock = d / f"{stamp}-{safe}-{n}.mid.lock"
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass
    return str(target)


# ---- take library (exports dir is the index — no extra state to corrupt) ----

def list_takes(limit: int = 100) -> list:
    """Saved takes, newest first: [{name,size,mtime}]."""
    d = exports_dir()
    rows = []
    for p in d.glob("*.mid"):
        try:
            st = p.stat()
        except OSError:
            continue
        rows.append({"name": p.name, "size": st.st_size, "mtime": int(st.st_mtime)})
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows[:max(1, min(500, int(limit)))]


def take_path(name: str) -> Path:
    """Containment law: a take is one .mid file INSIDE the exports dir.
    Rejects separators, '..', anything not .mid, and symlink escapes."""
    n = str(name or "")
    if not n or "/" in n or "\\" in n or ".." in n or not n.endswith(".mid"):
        raise ValueError(f"bad take name {n!r}")
    d = exports_dir().resolve()
    p = (d / n).resolve()
    if p.parent != d or not p.is_file():
        raise ValueError(f"no such take: {n!r}")
    return p


def read_take(name: str) -> bytes:
    return take_path(name).read_bytes()


def delete_take(name: str) -> bool:
    take_path(name).unlink()
    return True
