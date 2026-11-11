"""Software modulation matrix — profile `matrix:` edges evaluated in OUR layer.

Honest split (owner ruling 2026-11-07): a synth's INTERNAL mod matrix is not
CC-addressable; what we build is modulation in the surface layer — sources mixed
into a target control's value and driven out as that control's CC.

Edge kinds (vocabulary kept deliberately small — everything composable from
these; shape borrowed from LMMS's automation clips / LFO controllers, rewritten
in our own idiom, MIT-clean):
- 'lfo'   (default): source 'lfo' or any control -> mix into target around the
         captured base. Per-edge wave/rate_hz/phase; optional center/amp pin the
         swing to an explicit range instead of around the base.
- 'move'  one-shot envelope: target glides from..to over duration_ms (curve
         shaped), then holds (or returns to base when hold:false). This is the
         "ramp wave 0->100 over 5s" primitive the chat box writes.
- 'when'  threshold trigger: when source crosses level (up/down), run the
         `then` edges (move/lfo). once:true (default) fires once per start;
         once:false re-arms when the source falls back below / rises above.
- 'watch' change trigger: fires the `then` edges the moment the source control
         value changes at all (a physical twist) — "after I turn timbre...".

Laws inherited from the mf-visualizer line:
- One port owner: every send goes through the injected send_fn (the Bus).
- Coalescer: send only values that CHANGED.
- Base capture: every target's own value is captured at start; the matrix
  modulates AROUND it and restores it at stop — no feedback drift, and a held
  move is undone by stop (panic story: stop returns the synth's values).
- stdlib only.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable, Dict, List, Optional

try:  # package context (agent half: hermes_plugins.<plugin>.matrix_engine)
    from .modmath import curve, LFO_WAVES
except ImportError:  # standalone (dashboard half / tests): repo root on sys.path
    from modmath import curve, LFO_WAVES  # single source, no mirroring

TICK_S = 1.0 / 30.0  # 30 Hz evaluation; winmm short messages handle this easily


def lfo_value(wave: str, phase: float) -> float:
    """LFO at *phase* cycles -> 0..1 (unipolar; depth carries the bipolarity)."""
    f = phase - math.floor(phase)
    if wave == "sine":
        return (math.sin(2.0 * math.pi * f) + 1.0) / 2.0
    if wave == "tri":
        return 1.0 - abs(2.0 * f - 1.0)
    if wave == "saw":
        return f
    if wave == "sh":
        # Sample-and-hold: deterministic per cycle index (integer hash, no RNG
        # state) — same run twice gives the same steps; tests can pin it.
        idx = math.floor(phase)
        h = (idx * 2654435761 ^ (idx >> 7) ^ 0x5BF03635) & 0x7FFFFFFF
        return (h % 1001) / 1000.0
    raise ValueError(f"unknown lfo wave {wave!r}")


def mix(base: float, src: float, depth: float, mode: str) -> float:
    """One edge's contribution. base 0..1. src: signed modulation for 'add'
    (-1..1 — LFOs swing around the base), unipolar 0..1 for 'mul' (centered
    internally) and 'set'. depth -2..2. Returns 0..1 (clamped)."""
    if mode == "add":
        v = base + depth * src
    elif mode == "mul":
        v = base * (1.0 + depth * (src - 0.5) * 2.0)
    elif mode == "set":
        w = min(1.0, abs(depth))
        v = base * (1.0 - w) + src * w
    else:
        raise ValueError(f"unknown mode {mode!r}")
    return max(0.0, min(1.0, v))


class MatrixEngine:
    """Runs the ACTIVE profile's matrix edges against the live Bus.

    send_fn(kind="cc", cc=..., value=..., channel=..., source="matrix") -> dict
    get_value(cid) -> current shadow value 0..127 or None
    """

    def __init__(self, send_fn: Callable[..., dict],
                 get_value: Callable[[str], Optional[int]],
                 log: Callable[..., None] = lambda *a: None) -> None:
        self.send, self.get_value, self.log = send_fn, get_value, log
        # RLock: the engine calls send_fn/get_value while holding it; anything
        # those reach back into (Bus.status -> matrix_status) re-enters this
        # lock on the SAME thread. Plain Lock would self-deadlock on reentry.
        self.lock = threading.RLock()
        self.running = False
        self.edges: List[dict] = []
        self._lfo_edges: List[dict] = []    # compiled + activated continuous edges
        self._moves: List[dict] = []        # compiled + activated one-shots
        self._trigs: List[dict] = []       # when/watch triggers
        self.lfo: Dict[str, Any] = {"wave": "sine", "rate_hz": 0.5}
        self._thread: Optional[threading.Thread] = None
        self._t0 = 0.0
        self._base: Dict[str, float] = {}      # target cid -> own value 0..1 (captured)
        self._cur: Dict[str, float] = {}       # target cid -> smoothed output 0..1
        self._pending: set = set()             # lazy-arm law: targets with no observed value yet
        self._unrestorable: set = set()        # modulated WITHOUT a base — stop must not fake a restore
        self._last_sent: Dict[int, int] = {}   # cc -> last matrix-sent value (coalescer)
        self._cc_of: Dict[str, int] = {}       # cid -> cc (from profile at start)
        self._ch = 0
        self.fault: Optional[str] = None       # #4: last wire failure ("device gone?" etc.)

    # -- control ----------------------------------------------------------
    def _then_specs(self, e: dict, errs: List[str], tag: str) -> List[dict]:
        """Compile a trigger's `then` list (move/lfo edges only — no nesting
        triggers inside triggers: the vocabulary stays small and readable)."""
        out: List[dict] = []
        for j, t in enumerate(e.get("then") or []):
            k = t.get("kind", "move")
            if k == "move":
                try:
                    out.append({"kind": "move", "target": str(t["target"]),
                                "from": float(t["from"]) / 127.0, "to": float(t["to"]) / 127.0,
                                "dur": float(t["duration_ms"]) / 1000.0,
                                "curve": str(t.get("curve", "lin")),
                                "hold": bool(t.get("hold", True)), "t0": None})
                except (KeyError, TypeError, ValueError) as exc:
                    errs.append(f"{tag}[{j}]: move needs numeric target/from/to/duration_ms ({exc})")
            elif k == "lfo":
                try:
                    out.append({"kind": "lfo", "source": "lfo", "target": str(t["target"]),
                                "depth": float(t.get("depth", 1.0)),
                                "curve": str(t.get("curve", "lin")),
                                "mode": str(t.get("mode", "add")),
                                "slew": float(t.get("slew") or 0.0),
                                "wave": t.get("wave"),
                                "rate": (float(t["rate_hz"]) if t.get("rate_hz") is not None else None),
                                "phase": 0.0,
                                "center": (float(t["center"]) / 127.0) if t.get("center") is not None else None,
                                "amp": (float(t["amp"]) / 127.0) if t.get("amp") is not None else None,
                                "t0": None})
                except (KeyError, TypeError, ValueError) as exc:
                    errs.append(f"{tag}[{j}]: lfo needs numeric target ({exc})")
            else:
                errs.append(f"{tag}[{j}]: kind {k!r} cannot nest inside 'then' (move/lfo only)")
        return out

    def start(self, edges: List[dict], lfo: Optional[dict],
              cc_of_cid: Dict[str, int], channel: int = 0) -> Dict[str, Any]:
        with self.lock:
            if self.running:
                return {"ok": False, "error": "matrix already running"}
            if not edges:
                return {"ok": False, "error": "no edges in active profile"}
            # kind-aware reference check: lfo needs source+target; move has no
            # source; when/watch carry their targets inside `then`.
            bad = []
            for e in edges:
                k = e.get("kind", "lfo")
                if k == "lfo":
                    if e.get("target") not in cc_of_cid or (e.get("source") != "lfo" and e.get("source") not in cc_of_cid):
                        bad.append(e)
                elif k == "move":
                    if e.get("target") not in cc_of_cid:
                        bad.append(e)
                elif k in ("when", "watch"):
                    src = e.get("source")
                    if (k == "when" and src != "lfo" and src not in cc_of_cid) or (k == "watch" and src not in cc_of_cid):
                        bad.append(e)
                    if not e.get("then"):
                        bad.append(e)
                else:
                    bad.append(e)
            if bad:
                return {"ok": False, "error": f"edge references unknown control or empty then: {bad[0]}"}
            self.edges = edges
            self.lfo = dict(lfo or {"wave": "sine", "rate_hz": 0.5})
            if self.lfo.get("wave") not in LFO_WAVES:
                return {"ok": False, "error": f"lfo wave must be one of {sorted(LFO_WAVES)}"}
            # Review 2026-11-09: compile edges ONCE. profile_store validated
            # these numbers refuse-closed long ago; re-parsing raw dicts with
            # float()/dict.get at 30 Hz was pure churn and hid a bad numeric
            # until mid-run. A direct (non-validated) caller now gets a named
            # refusal at start instead.
            lfo_edges: list[dict] = []
            moves: list[dict] = []
            trigs, errs = [], []
            for i, e in enumerate(edges):
                k = e.get("kind", "lfo")
                if k == "lfo":
                    try:
                        depth = float(e.get("depth", 1.0))
                        slew = float(e.get("slew") or 0.0)
                        rt = e.get("rate_hz")
                        rate = None if rt is None else float(rt)
                        ph = float(e.get("phase") or 0.0)
                        ctr = None if e.get("center") is None else float(e["center"]) / 127.0
                        amp = None if e.get("amp") is None else float(e["amp"]) / 127.0
                    except (TypeError, ValueError) as exc:
                        return {"ok": False, "error": f"edge numbers must be numeric: {exc}"}
                    lfo_edges.append({"kind": "lfo", "source": str(e.get("source", "")),
                                      "target": str(e["target"]), "depth": depth,
                                      "curve": str(e.get("curve", "lin")), "mode": str(e.get("mode", "add")),
                                      "slew": slew, "wave": e.get("wave"), "rate": rate,
                                      "phase": ph, "center": ctr, "amp": amp, "t0": 0.0})
                elif k == "move":
                    try:
                        moves.append({"kind": "move", "target": str(e["target"]),
                                      "from": float(e["from"]) / 127.0, "to": float(e["to"]) / 127.0,
                                      "dur": float(e["duration_ms"]) / 1000.0,
                                      "curve": str(e.get("curve", "lin")),
                                      "hold": bool(e.get("hold", True)), "t0": 0.0})
                    except (KeyError, TypeError, ValueError) as exc:
                        return {"ok": False, "error": f"move edge needs numeric from/to/duration_ms: {exc}"}
                elif k in ("when", "watch"):
                    try:
                        level = float(e.get("level", 64)) / 127.0
                    except (TypeError, ValueError) as exc:
                        return {"ok": False, "error": f"when level must be numeric: {exc}"}
                    then = self._then_specs(e, errs, f"matrix[{i}].then")
                    trigs.append({"kind": k, "source": str(e["source"]),
                                  "level": level, "crosses": str(e.get("crosses", "up")),
                                  "once": bool(e.get("once", True)), "armed": True,
                                  "prev": None, "then": then, "tid": i})
            if errs:
                return {"ok": False, "error": errs[0]}
            self._lfo_edges, self._moves, self._trigs = lfo_edges, moves, trigs
            self._cc_of = dict(cc_of_cid)
            self._ch = channel
            self._base, self._cur, self._last_sent = {}, {}, {}
            # Base capture covers EVERY target — including trigger `then`
            # targets: the base law says stop restores the synth's values, so
            # a target that only ever moves after a trigger must still be
            # captured (and must still have a known value) at start.
            all_targets = ([ed["target"] for ed in lfo_edges] + [m["target"] for m in moves]
                           + [t["target"] for tr in trigs for t in tr["then"]])
            unknown = []
            for t in all_targets:
                if t not in self._base:
                    v = self.get_value(t)
                    if v is None:
                        unknown.append(t)
                    else:
                        self._base[t] = v / 127.0
                        self._cur[t] = self._base[t]
            # Lazy-arm law (owner bug 2026-11-10, chat box demo): a start used to
            # REFUSE entirely when ANY target had no observed value — one stale
            # lane (never echoed since connect) blocked every fresh command.
            # Now: unobserved targets go PENDING. Base-mixing edges to a pending
            # target stay silent until a real value appears (one twist, one
            # send) and then arm with the CAPTURED base; absolute `move` edges
            # run immediately (they name their own from->to) and their targets
            # land in _unrestorable — stop() never writes a value it never
            # observed (the 64-centre masquerade stays dead, #3).
            self._pending = set(unknown)
            self._unrestorable = set()
            self.running = True
            self.fault = None
            self._t0 = time.monotonic()
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="midi-matrix")
            self._thread.start()
            r = {"ok": True, "edges": len(edges), "targets": sorted(self._base)}
            if unknown:
                r["pending"] = sorted(unknown)
            return r

    def stop(self) -> Dict[str, Any]:
        with self.lock:
            # Deep rinse: a FAULTED engine (running=False but bases still
            # captured) must still restore — the base law says the synth gets
            # its values back; "not running" is only honest when nothing is
            # held. Also capture the thread under the lock: a quick stop->start
            # could otherwise join the NEW thread and orphan the old one.
            if not self.running and not self._base:
                return {"ok": False, "error": "matrix not running"}
            self.running = False
            th, self._thread = self._thread, None
        if th is not None:
            th.join(timeout=1.0)
        restored, failed = [], []
        with self.lock:
            for cid, base in self._base.items():
                cc = self._cc_of.get(cid)
                if cc is None:
                    continue
                v = max(0, min(127, int(round(base * 127))))
                res = self.send(kind="cc", cc=cc, value=v, channel=self._ch,
                                source="matrix")
                # #4: a restore that the wire refused must be REPORTED, not
                # folded into a success story. ok:false + failed[] is the law.
                if isinstance(res, dict) and res.get("error"):
                    failed.append({"cid": cid, "cc": cc, "value": v,
                                   "error": res["error"]})
                    continue
                self._last_sent[cc] = v
                restored.append({"cid": cid, "cc": cc, "value": v})
            self.edges, self._lfo_edges, self._moves, self._trigs = [], [], [], []
            self._base, self._cur = {}, {}
            unres = sorted(self._unrestorable)
            self._pending, self._unrestorable = set(), set()
        out: Dict[str, Any] = {"ok": not failed, "restored": restored}
        if unres:
            # Lazy-arm law: these targets were modulated WITHOUT a captured base
            # (absolute moves on a fresh connect) — we never observed their real
            # values, so stop() refuses to fake a restore; they stay where the
            # last modulation left them, and the caller is told by name.
            out["not_restored"] = unres
        if failed:
            out["error"] = "restore sends failed: " + failed[0]["error"]
            out["failed"] = failed
        return out

    def status(self) -> Dict[str, Any]:
        with self.lock:
            return {"running": self.running,
                    "edges": len(self.edges),
                    "lfo": dict(self.lfo),
                    "fault": self.fault,
                    "pending": sorted(self._pending),
                    "fired": sum(1 for tr in self._trigs if not tr["armed"]),
                    "targets": {cid: round(c * 127) for cid, c in self._cur.items()}}

    # -- evaluation --------------------------------------------------------
    def _loop(self) -> None:
        while True:
            with self.lock:
                if not self.running:
                    return
                try:
                    self._tick(time.monotonic() - self._t0)
                except Exception as exc:  # one bad edge must not silence the engine
                    self.log("matrix tick failed:", exc)
            time.sleep(TICK_S)

    def _src01(self, src_cid: str, t: float) -> float:
        if src_cid == "lfo":
            return lfo_value(self.lfo.get("wave", "sine"),
                             t * float(self.lfo.get("rate_hz", 0.5)))
        v = self.get_value(src_cid)
        return (v if v is not None else 0) / 127.0

    def _fire(self, tr: dict, t: float) -> None:
        """Activate a trigger's then-edges at time t (moves get t0; lfos join
        the continuous set with their own start time). RESTART law (regression review B16,
        owner ruling 2026-11-10): one active child instance per trigger —
        re-firing REPLACES the previous child (re-hitting the same wobble)
        instead of stacking a new oscillator per twist. Top-level edges (no
        _trig tag) are never replaced by a trigger."""
        tid = tr.get("tid")
        if tid is not None:
            self._lfo_edges = [e for e in self._lfo_edges if e.get("_trig") != tid]
            self._moves = [m for m in self._moves if m.get("_trig") != tid]
        for spec in tr["then"]:
            s = dict(spec)
            s["t0"] = t
            s["_trig"] = tid
            if s["kind"] == "move":
                self._moves.append(s)
            else:
                self._lfo_edges.append(s)

    def _tick(self, t: float) -> None:
        # Lazy-arm law: a pending target arms the moment a REAL value appears
        # (physical twist echo, panel send — never our own matrix send: a
        # pending target receives no base-mixing sends, and move-run targets
        # left _pending for _unrestorable before their first send).
        if self._pending:
            for tg in list(self._pending):
                v = self.get_value(tg)
                if v is not None:
                    self._base[tg] = v / 127.0
                    self._cur[tg] = self._base[tg]
                    self._pending.discard(tg)
        # 1) triggers: when = level crossing (re-arm on the opposite side when
        # once:false), watch = ANY change of the source value (a physical
        # twist — the sensor is the shadow state the Bus already keeps).
        for tr in self._trigs:
            cur = self._src01(tr["source"], t)
            prev = tr["prev"]
            if tr["kind"] == "when":
                lv = tr["level"]
                if tr["armed"] and prev is not None:
                    if (tr["crosses"] == "up" and prev < lv <= cur) or (tr["crosses"] == "down" and prev > lv >= cur):
                        self._fire(tr, t)
                        if tr["once"]:
                            tr["armed"] = False
                if not tr["armed"] and not tr["once"]:
                    if (tr["crosses"] == "up" and cur < lv) or (tr["crosses"] == "down" and cur > lv):
                        tr["armed"] = True
            else:  # watch
                if tr["armed"] and prev is not None and cur != prev:
                    self._fire(tr, t)
                    if tr["once"]:
                        tr["armed"] = False
            tr["prev"] = cur

        # 2) contributions per target: continuous edges mix sequentially (add
        # sums, mul compounds, set takes the last full-strength edge); move
        # one-shots are ABSOLUTE (they set the value they envelope toward).
        out: Dict[str, float] = {}
        for ed in self._lfo_edges:
            if ed["target"] in self._pending:
                if ed["center"] is None:
                    continue  # base-mixing edge: silent until the target arms with a captured base
                # center/amp edges orbit the NAMED range — no base needed; they
                # run now and their target is flagged unrestorable for stop().
                self._pending.discard(ed["target"])
                self._unrestorable.add(ed["target"])
            if ed["source"] == "lfo":
                # Multi-matrix law: each LFO lane runs its OWN oscillator
                # (edge wave/rate_hz override the profile-level default).
                # N lanes = N independent modulations, any device's CCs.
                # phase (LMMS's PHS knob, rewritten here) de-syncs same-rate
                # lanes on one CC.
                rate = ed["rate"] if ed["rate"] is not None else float(self.lfo.get("rate_hz", 0.5))
                src = lfo_value(ed["wave"] or self.lfo.get("wave", "sine"),
                                (t - (ed["t0"] or 0.0)) * rate + ed["phase"])
            else:
                v = self.get_value(ed["source"])
                src = (v if v is not None else 0) / 127.0
            src = curve(ed["curve"], src)
            if ed["center"] is not None:
                # center/amp pin the swing to an explicit range (a triggered
                # oscillation "30% <-> 70% every 2s" does NOT orbit the
                # captured base — it orbits the center the command named).
                want = ed["center"] + (ed["amp"] if ed["amp"] is not None else 0.5) * (src * 2.0 - 1.0)
                out[ed["target"]] = max(0.0, min(1.0, want))
                continue
            if ed["source"] == "lfo" and ed["mode"] == "add":
                src = src * 2.0 - 1.0  # LFO add-edges swing AROUND the base
            base = out.get(ed["target"], self._base.get(ed["target"], 0.0))
            out[ed["target"]] = mix(base, src, ed["depth"], ed["mode"])
        for m in self._moves:
            p = (t - m["t0"]) / m["dur"] if m["dur"] > 0 else 1.0
            if p < 0.0:
                continue  # not started yet (activated on a later tick)
            if m["target"] in self._pending:
                # ABSOLUTE edge — names its own from->to, runs without a base;
                # stop() will report the target as not_restored (lazy-arm law).
                self._pending.discard(m["target"])
                self._unrestorable.add(m["target"])
            if p >= 1.0:
                # float() is a no-op on the compiled floats (moves are built
                # with float()/127 at start) — it names the type for the
                # checker: dict[str] values are Unknown without it.
                to_v = float(m["to"])
                want = to_v if m["hold"] else float(self._base.get(m["target"], to_v))
            else:
                want = m["from"] + (m["to"] - m["from"]) * curve(m["curve"], p)
            out[m["target"]] = max(0.0, min(1.0, want))
        for cid, want in out.items():
            # per-edge slew: smallest nonzero slew among edges to this target
            sl = [e["slew"] for e in self._lfo_edges if e["target"] == cid and e["slew"]]
            slew = min(sl) if sl else 0.0
            if slew:
                prev = self._cur.get(cid, want)
                want = prev + (want - prev) * min(1.0, slew)
            self._cur[cid] = want
            v = max(0, min(127, int(round(want * 127))))
            cc = self._cc_of.get(cid)
            if cc is None or self._last_sent.get(cc) == v:  # coalescer
                continue
            try:
                res = self.send(kind="cc", cc=cc, value=v,
                                channel=self._ch, source="matrix")
            except Exception as exc:  # never kill the thread on one send
                self.fault = str(exc)
                self.running = False  # #4: a throwing wire is a fault, not a shrug
                self.log("matrix send failed:", exc)
                return  # review: stop modulation against a dead device NOW
            if isinstance(res, dict) and res.get("error"):
                # #4: the coalescer advances ONLY on success — a value the
                # wire refused may be retried next tick; a repeated failure
                # latches a fault and stops modulation against a dead device.
                self.fault = res["error"]
                self.running = False
                self.log("matrix send refused:", res["error"])
                return  # review: no further sends into the dead wire this tick
            self._last_sent[cc] = v
