"""Dashboard half: REST surface for the pane + builder.

Exec'd STANDALONE by the web server (no package context) — sibling modules load via
sys.path insert (mf-visualizer pattern). The MIDI session state is shared with the agent
half: prefer importing the already-loaded agent package module so ONE Bus instance owns
the exclusive MIDI ports; fall back to path-loading only when the agent half is absent
(dev/test).

Async law (#9): every handler that touches winmm or the filesystem
runs OFF the event loop — body-less handlers are plain def (FastAPI runs them in
its threadpool), body-readers await run_in_threadpool around the blocking part.
A 150 ms connect settle or a matrix stop join must never stall the loop that
serves every other route.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # root siblings for the fallback path-load

from fastapi import APIRouter, Request  # noqa: E402
from fastapi.concurrency import run_in_threadpool  # noqa: E402

midi_core: ModuleType | None = None
_import_note = ""
try:
    from hermes_plugins.z_midi_console import midi_core  # type: ignore
    _import_note = "shared-agent-module"
except ImportError as exc:  # agent half not loaded (dev/test): path-load fallback.
    # Deep rinse: `except Exception` treated a BROKEN agent half (any error
    # raised inside the package import) as "absent" and silently path-loaded a
    # SECOND Bus into the same process — two owners of one exclusive port.
    # Only a genuinely missing module falls back; anything else fails loudly.
    import importlib.util

    if "zmc_midi_core" in sys.modules:  # deep rinse: never exec the module twice
        midi_core = sys.modules["zmc_midi_core"]
    else:
        _p = Path(__file__).resolve().parent.parent / "midi_core.py"
        _spec = importlib.util.spec_from_file_location("zmc_midi_core", _p)
        if _spec is None or _spec.loader is None:
            raise RuntimeError(f"z-midi-console: midi_core.py not loadable at {_p}")
        midi_core = importlib.util.module_from_spec(_spec)
        sys.modules["zmc_midi_core"] = midi_core
        _spec.loader.exec_module(midi_core)
    _import_note = f"path-loaded-fallback ({exc.__class__.__name__})"

if midi_core is None:  # both import paths failed — fail loud at mount, not at first route call
    raise RuntimeError("z-midi-console: midi_core could not be loaded (shared import AND path fallback both failed)")


def _mc() -> ModuleType:
    """Typed accessor for the midi_core module: the module-level guard above
    proves non-None at mount, but Pyright won't narrow a module global inside
    functions — this is the one honest narrowing point."""
    if midi_core is None:
        raise RuntimeError("z-midi-console: midi_core not loaded")
    return midi_core


BUS = midi_core.BUS
router = APIRouter()

import importlib.util as _ilu  # noqa: E402


def _load_sibling(name: str):
    """Deep rinse: the web-server process is SHARED and this router is exec'd
    standalone — a bare `import profile_store` into sys.modules races with any
    other plugin shipping the same filename (mf-visualizer literally does):
    whichever imports first wins for BOTH, wrong schema, wrong data_dir. Load
    under a plugin-UNIQUE name; alias the generic name only if free, so the
    siblings' own standalone fallback imports resolve to OURS."""
    mod_name = f"zmc_{name}"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    p = Path(__file__).resolve().parent.parent / f"{name}.py"
    spec = _ilu.spec_from_file_location(mod_name, p)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"z-midi-console: sibling module {name}.py not loadable at {p}")
    mod = _ilu.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    sys.modules.setdefault(name, mod)
    return mod


profile_store = _load_sibling("profile_store")
automation = _load_sibling("automation")


async def _json_body(request: Request) -> dict:
    """Deep rinse: a malformed/empty body used to raise inside the coroutine
    and surface as a bare 500; routes now see {} and refuse by name."""
    try:
        b = await request.json()
    except Exception:
        return {}
    return b if isinstance(b, dict) else {}


# ---- software matrix --------------------------------------------------------
# plain def = FastAPI threadpool: matrix stop joins the engine thread (up to
# 1 s), start/stop do winmm sends — none of it belongs on the event loop.

@router.post("/matrix/start")
def post_matrix_start():
    """Start the ACTIVE profile's matrix — engine is owned by the profile's
    Bus (deck: each profile binds one), so the agent tools and the pane can
    never end up with two engines."""
    try:
        pid = profile_store.get_active_profile_id()
        if not pid:
            return {"error": "no active profile"}
        edges, lfo, cc_of, ch = profile_store.matrix_inputs(
            profile_store.load_profile(pid))
        return _active_bus().matrix_start(edges, lfo, cc_of, ch)
    except Exception as exc:
        return {"error": str(exc)}


@router.post("/matrix/stop")
def post_matrix_stop():
    return _active_bus().matrix_stop()


@router.post("/matrix/reload")
def post_matrix_reload():
    """Apply edited edges/LFO to a RUNNING matrix (stop restores base, start
    re-captures it — a clean restart the pane can fire after any edge edit)."""
    bus = _active_bus()
    if not bus.matrix_status().get("running"):
        return {"ok": True, "running": False}
    pid = profile_store.get_active_profile_id()
    if not pid:
        return {"error": "no active profile"}
    try:
        # Deep rinse: validate BEFORE the destructive stop — a reload that
        # fails (profile edited invalid meanwhile, base-capture refusal) must
        # not strand a previously-running matrix. And carry restore failures
        # instead of discarding the stop result.
        edges, lfo, cc_of, ch = profile_store.matrix_inputs(
            profile_store.load_profile(pid))
        stop_r = bus.matrix_stop()
        if not stop_r.get("ok") and stop_r.get("error") != "matrix not running":
            return {"error": f"reload: stop failed: {stop_r['error']}",
                    "failed": stop_r.get("failed", []), "reloaded": False}
        return bus.matrix_start(edges, lfo, cc_of, ch)
    except Exception as exc:
        return {"error": str(exc)}


@router.get("/matrix/status")
def get_matrix_status():
    return _active_bus().matrix_status()


# ---- record-a-ride -> .mid export -------------------------------------------

@router.post("/rec/start")
def post_rec_start():
    return _active_bus().recorder.start()


@router.get("/rec/status")
def get_rec_status():
    return _active_bus().recorder.status()


@router.post("/rec/stop")
async def post_rec_stop(request: Request):
    body = await _json_body(request)
    rec = _active_bus().recorder
    # pass bpm RAW: Recorder.stop validates and returns a named error —
    # float() here would 500 on junk instead of refusing politely.
    r = await run_in_threadpool(rec.stop, body.get("bpm", 120.0))
    if not r.get("ok"):
        return r
    import base64

    def _export():
        # #7: the take is NOT consumed by a failed export — it stays pending
        # and /rec/export can retry it. Never silently eat a performance.
        try:
            path = automation.export_mid(r.pop("pattern"), name=str(body.get("name") or "take"))
        except Exception as exc:
            return {"error": f"export failed: {exc}", "pending": True}
        rec.clear_pending()
        r["path"] = path
        r["filename"] = Path(path).name
        # the take is already consumed at this point —
        # an AV lock on read_bytes must not 500 and strand the exported file
        # (retry would say "no pending take" while the .mid sits on disk).
        try:
            r["mid_b64"] = base64.b64encode(Path(path).read_bytes()).decode()
        except OSError as exc:
            r["error"] = f"exported but unread ({exc.__class__.__name__}) — open the file directly"
        return r
    return await run_in_threadpool(_export)


@router.post("/rec/export")
async def post_rec_export(request: Request):
    """#7: retry the export of the stopped (pending) take."""
    body = await _json_body(request)
    rec = _active_bus().recorder
    import base64

    def _export():
        pattern = rec.pending()
        if pattern is None:
            return {"error": "no pending take"}
        try:
            if body.get("bpm") is not None:
                try:
                    nb = float(body["bpm"])
                except (TypeError, ValueError):
                    return {"error": "bpm must be a number"}
                # Deep rinse: this used to CLAMP (bpm 5000 quietly became 999)
                # — refuse-don't-mask law: out of domain is a named refusal.
                if not 10.0 <= nb <= 999.0:
                    return {"error": "bpm must be 10-999 (refused, not clamped)"}
                pattern["bpm"] = nb
            path = automation.export_mid(pattern, name=str(body.get("name") or "take"))
        except Exception as exc:
            return {"error": f"export failed: {exc}", "pending": True}
        rec.clear_pending()
        # never clear-then-500 (see /rec/stop twin).
        try:
            b64 = base64.b64encode(Path(path).read_bytes()).decode()
        except OSError as exc:
            return {"ok": True, "path": path, "filename": Path(path).name,
                    "error": f"exported but unread ({exc.__class__.__name__}) — open the file directly"}
        return {"ok": True, "path": path, "mid_b64": b64,
                "filename": Path(path).name}
    return await run_in_threadpool(_export)


@router.post("/rec/discard")
def post_rec_discard():
    return _active_bus().recorder.discard()  # active-bus parity with /rec/start + the agent twin (was silently discarding PRIMARY's take on a deck rig)


@router.post("/rec/open")
def post_rec_open():
    """Open the takes folder in the OS file browser. Fixed plugin data path —
    no user input crosses into the shell."""
    import subprocess
    import sys
    d = automation.exports_dir()
    try:
        if sys.platform == "win32":
            os.startfile(str(d))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(d)])
        else:
            subprocess.Popen(["xdg-open", str(d)])
        return {"ok": True, "path": str(d)}
    except Exception as exc:  # deep rinse: os.startfile can raise non-OSError
        return {"error": str(exc)}


@router.get("/rec/takes")
def get_rec_takes(limit: int = 100):
    return {"takes": automation.list_takes(int(limit))}


@router.get("/rec/take/get")
def get_rec_take(name: str = ""):
    import base64
    try:
        p = automation.take_path(name)
        return {"name": p.name, "size": p.stat().st_size,
                "mid_b64": base64.b64encode(automation.read_take(name)).decode()}
    except (ValueError, OSError) as exc:  # OSError: deleted between stat and read
        return {"error": str(exc)}


@router.post("/rec/take/delete")
async def post_rec_take_delete(request: Request):
    body = await _json_body(request)
    try:
        return await run_in_threadpool(
            lambda: {"ok": automation.delete_take(str(body.get("name", "")))})
    except (ValueError, OSError) as exc:  # OSError: already gone (double delete race)
        return {"error": str(exc)}


def _profile_err(exc: Exception) -> dict:
    return {"error": str(exc)}


def _stop_matrix_for_transition() -> dict | None:
    """#10 (): profile activation/deletion/edits while a matrix
    runs would leave the engine modulating targets from a profile that is no
    longer (or not yet) on screen — and the pane's Stop button can even vanish
    with the old panel. Transitions are backend-owned: restore bases FIRST,
    then change. Returns the stop result when a matrix was running.
    Deck law: every registered bus is swept, not just primary."""
    stopped = None
    for b in list(_mc().BUSES.values()):
        if b.matrix_status().get("running"):
            r = b.matrix_stop()
            if not r.get("ok"):
                stopped = r
            else:
                stopped = stopped or {"stopped": True}
    return stopped


def _active_bus_id() -> str:
    """Bus id the ACTIVE profile binds (deck law: omitted 'bus' == 'primary')."""
    pid = profile_store.get_active_profile_id()
    if pid:
        try:
            return str(profile_store.load_profile(pid).get("bus") or "primary")
        except Exception:
            pass
    return "primary"


def _stop_matrix_on_active_bus() -> dict | None:
    """B05 transaction sweep half: a matrix compiles ONLY from the active
    profile, so the save/delete/rename transaction stops the engine on the
    bus the active profile binds — a secondary bus's engine (if any) is not
    this preset's matrix and must not be silently killed-and-not-revived."""
    stopped = None
    bid = _active_bus_id()
    for b in list(_mc().BUSES.values()):
        if b.bus_id == bid and b.matrix_status().get("running"):
            r = b.matrix_stop()
            if not r.get("ok"):
                stopped = r
            else:
                stopped = stopped or {"stopped": True}
    return stopped


def _restart_matrix(bus) -> dict:
    """B05 restart half: re-start one bus's engine from the ACTIVE profile
    doc on disk (agent _matrix_handler 'reload' parity). matrix_start needs
    the wire view — calling it bare is a TypeError (caught pre-ship 2026-11-10)."""
    pid = profile_store.get_active_profile_id()
    if not pid:
        return {"ok": False, "error": "no active profile"}
    edges, lfo, cc_of, ch = profile_store.matrix_inputs(
        profile_store.load_profile(pid))
    return bus.matrix_start(edges, lfo, cc_of, ch)


def _stop_matrix_if_active(pid: str | None) -> dict | None:
    """B05 audit (2026-11-10): a matrix compiles ONLY from the active profile,
    so transitions touching a NON-active preset (delete/rename/copy of a
    bystander) must not stop a running wah — the old blanket sweep silently
    killed it. When the touched id IS the active profile, stop the engine on
    the bus that profile binds (not every bus — a secondary engine is not
    this preset's matrix)."""
    active = profile_store.get_active_profile_id()
    if pid and pid == active:
        return _stop_matrix_on_active_bus()
    return None


def _active_bus():
    """The Bus of the ACTIVE profile (deck law: each profile binds one bus;
    omitted 'bus' == 'primary' == the historical singleton)."""
    pid = profile_store.get_active_profile_id()
    bus = "primary"
    if pid:
        try:
            bus = str(profile_store.load_profile(pid).get("bus") or "primary")
        except Exception:
            pass
    return _mc().get_bus(bus)


@router.get("/state")
def get_state():
    # Flat top-level keys follow the ACTIVE profile's bus. Pre-deck that was
    # always 'primary' — zero behavior change — and with a deck it means the
    # learn cursor, connected chip and CC shadow track the surface you edit.
    st = _active_bus().status()
    st["import"] = _import_note
    st["pid"] = os.getpid()
    st["active_profile"] = profile_store.get_active_profile_id()
    # Deck feed (2026-11-09): one status blob per registered bus + the deck
    # manifest. Flat top-level keys stay the PRIMARY bus (pre-deck contract).
    st["buses"] = [b.status() for b in list(_mc().BUSES.values())]  # snapshot: get_bus may insert during a 600 ms poll loop (RuntimeError race, review 2026-11-09)
    deck = []
    for pid in profile_store.get_deck():
        row = {"profile_id": pid}
        try:
            p = profile_store.load_profile(pid)
            row.update(device=p.get("device"), bus=str(p.get("bus") or "primary"),
                       channel=p.get("channel", 0), valid=True)
        except Exception:
            row.update(valid=False)
        deck.append(row)
    st["deck"] = deck
    st["deck_presets"] = profile_store.get_deck_presets()
    ccmap = profile_store.control_cc_map()
    st["bound_ccs"] = sorted(ccmap)
    # Passive-learn feed: incoming CCs the active profile has no control for.
    # #11: only genuine hardware motion ("panel") prompts — our own echoes
    # (incl. panic CC 123s) must never offer to create a control.
    st["unmapped_cc"] = [c for c in st.get("cc", [])
                         if c["cc"] not in ccmap and c.get("source") == "panel"]
    return st


@router.get("/devices")
def get_devices():
    # caps enumeration touches every winmm device — threadpool, not the loop
    return {"devices": _mc().list_devices()}


@router.post("/connect")
async def post_connect(request: Request):
    body = await _json_body(request)
    name = str(body.get("name", ""))
    if not name:
        return {"error": "name required"}
    # Deck: each bus owns its own exclusive port pair; 'primary' keeps the
    # historical behavior exactly.
    bus = _mc().get_bus(str(body.get("bus") or "primary"))
    try:
        # connect settles 150 ms after port open (router first-message law) —
        # that sleep belongs in a worker thread, never on the event loop.
        r = await run_in_threadpool(bus.connect, name)
        await run_in_threadpool(_auto_match_profile, r, bus.bus_id)
        return r
    except Exception as exc:
        return {"error": str(exc)}


def _auto_match_profile(connect_result: dict, bus_id: str = "primary") -> None:
    """Seamlessness law: plug in -> the surface appears. If no active profile
    matches the connected port name, activate the first profile whose
    match.name_contains hits it (user tier wins over bundled).
    Deck law: a profile only auto-matches the bus it is BOUND to (profile
    'bus' key, omitted == primary). Connecting a secondary device ADDS its
    profile to the deck tail — it never steals the active/edit focus."""
    try:
        dev = str(((connect_result or {}).get("device") or {}).get("out") or "").lower()
        if not connect_result.get("ok") or not dev:
            return
        act = profile_store.get_active_profile_id()
        if act:
            try:
                a = profile_store.load_profile(act)
                m = str((a.get("match") or {}).get("name_contains") or "")
                ab = str(a.get("bus") or "primary")
            except Exception:
                m, ab = "", "primary"
            if m and m.lower() in dev and ab == bus_id:
                return
        for row in profile_store.list_profiles():
            try:
                p = profile_store.load_profile(row["profile_id"])
            except Exception:
                continue
            m = str((p.get("match") or {}).get("name_contains") or "")
            if str(p.get("bus") or "primary") != bus_id:
                continue
            if m and m.lower() in dev:
                deck = profile_store.get_deck()
                if bus_id == "primary" or row["profile_id"] not in deck:
                    if bus_id == "primary":
                        profile_store.set_active_profile_id(row["profile_id"])
                    else:
                        # secondary device joins the deck tail; active stays
                        profile_store.set_deck(deck + [row["profile_id"]])
                return
    except Exception:
        pass


@router.post("/disconnect")
async def post_disconnect(request: Request):
    body = await _json_body(request)
    bus = _mc().get_bus(str(body.get("bus") or "primary"))
    # close waits for in-flight callbacks (deadlock law) — threadpool
    await run_in_threadpool(bus.disconnect)
    return {"ok": True}


@router.post("/panic")
def post_panic():
    """All notes off on every channel + release plugin-held notes (#5).
    Deck: panic is a safety action — every connected bus gets it."""
    results = [b.panic() for b in list(_mc().BUSES.values()) if b.device]
    # Honest confirmation (review 2026-11-09): one dead-wire bus used to hide
    # behind ok:true — aggregate and surface the first failure.
    bad = next((r for r in results if not r.get("ok")), None)
    if results:
        return {"ok": bad is None, "buses": results,
                **({"error": bad.get("error") or "bus panic failed"} if bad else {})}
    return {"ok": True, "buses": [], "error": None}


@router.post("/cc")
async def post_cc(request: Request):
    body = await _json_body(request)
    bus = _mc().get_bus(str(body.get("bus") or "primary"))
    return await run_in_threadpool(
        lambda: bus.send(kind="cc", cc=body.get("cc"), value=body.get("value"),
                         channel=body.get("channel", 0),
                         source=str(body.get("source", "ui"))))


@router.post("/note")
async def post_note(request: Request):
    body = await _json_body(request)
    bus = _mc().get_bus(str(body.get("bus") or "primary"))
    kind = "note_on" if body.get("velocity", 1) else "note_off"
    return await run_in_threadpool(
        lambda: bus.send(kind=kind, note=body.get("note"), velocity=body.get("velocity"),
                         channel=body.get("channel", 0),
                         source=str(body.get("source", "ui"))))


# ---- deck (multi-profile surface, 2026-11-09) --------------------------------

@router.get("/deck")
def get_deck_route():
    return {"deck": profile_store.get_deck()}


@router.post("/deck")
async def post_deck(request: Request):
    """Replace the on-screen deck (active profile = deck[0]). Matrix
    transitions are backend-owned: every bus's engine stops + restores bases
    before the deck changes (#10 law)."""
    body = await _json_body(request)
    ids = body.get("ids")
    if not isinstance(ids, list):
        return {"error": "ids must be a list of profile ids"}
    # validate BEFORE the destructive sweep (a bad
    # deck used to silently kill a running wah and leave it dead), and skip
    # the sweep entirely when the deck is unchanged (no-op poll).
    _seen: list[str] = []
    for d in ids:
        if not (isinstance(d, str) and d):
            return {"error": f"deck refused: ids must be non-empty strings, got {type(d).__name__}"}
        if d in _seen:
            continue  # duplicates collapse (set_deck law) — compare the EFFECTIVE deck
        _seen.append(d)
        try:
            profile_store.load_profile(d)
        except (KeyError, ValueError) as exc:
            return {"error": f"deck refused: {exc}"}
    if _seen == profile_store.get_deck():
        return {"ok": True, "deck": profile_store.get_deck()}
    running = [b for b in list(_mc().BUSES.values()) if b.matrix_status().get("running")]
    warn = _stop_matrix_for_transition()
    try:
        deck = profile_store.set_deck(ids)
    except (KeyError, ValueError, OSError) as exc:
        # A12: OSError used to 500 with the matrix left stopped.
        # Restore-on-failure law — the deck is unchanged, re-arm the engines.
        for b in running:
            _restart_matrix(b)
        return {"error": f"deck refused: {exc}"}
    r = {"ok": True, "deck": deck}
    if warn:
        r["matrix_warning"] = "matrix stopped for deck change"
    return r


@router.get("/viz")
def get_viz(bus: str = ""):
    """Fast feed for the spectrum analyzer (pane polls ~150 ms while on):
    recent wire traffic, ages in seconds. bus='' -> active bus; bus='*' ->
    every bus merged (all-MIDI source); otherwise the named bus. In-memory
    deques — no file IO, cheap enough for a fast poll (sendlog stays audit)."""
    import time as _t
    now = _t.time()
    def rows_for(b, inbound):
        # regression guard bug 7 (hygiene — diagnosis corrected: a deque does NOT
        # raise "mutated during iteration" under CPython, so this never threw;
        # but an unlocked walk could skip/dup rows and paired sends+ins came
        # from different instants. Snapshot under the bus lock = one atomic
        # frame per bus.)
        with b._lock:
            sends = list(b._recent_sends)
            ins = list(b._recent_in) if inbound else []
        out = list(sends)
        out.extend((k + "-in", c, n, v, ts) for (k, c, n, v, ts) in ins)
        return out
    if bus == "*":
        rows = []
        for b in list(_mc().BUSES.values()):
            rows.extend(rows_for(b, True))
    else:
        # GET input must not mint buses (review 2026-11-09): every unique typo'd
        # string used to create a permanent phantom Bus listed by /state + swept by /panic.
        if bus and not profile_store._BUS_SLUG.match(bus):
            return {"error": f"bus {bus!r} is not a valid bus id"}
        # the slug guard only rejected SYNTAX junk —
        # get_bus INSERTS on miss, so every valid-slug typo on this 150 ms-
        # polled GET minted a permanent phantom Bus. Lookup, never create.
        b = _mc().BUSES.get(bus) if bus else _active_bus()
        if b is None:
            return {"error": f"no such bus {bus!r}"}
        rows = rows_for(b, True)
    return {"sends": [{"kind": k, "ch": c, "num": n, "val": v, "age": round(now - ts, 3)}
                      for (k, c, n, v, ts) in sorted(rows, key=lambda r: r[4], reverse=True)]}


@router.get("/audio")
def get_audio():
    """Desktop-audio + microphone spectrum bridge: audio-strip publishes its
    live FFT payload to state.json (atomic tmp+replace by that plugin); we
    just read it — no PortAudio, no HTTP, stdlib only. Refuse-honest: if
    audio-strip isn't installed/running, the pane gets a named error."""
    import json as _json
    # Fallback law (review 2026-11-09): no LOCALAPPDATA (POSIX dev hosts) used to
    # make the path CWD-relative and read the wrong file. Same chain as data_dir().
    # Catalog review (PR 153102): the base is now the ACTIVE PROFILE's Hermes home,
    # so a second profile reads its own audio-strip, not the default profile's.
    hermes_root = profile_store.hermes_home()
    candidates = [hermes_root / "plugins" / "audio-strip" / "state.json"]
    base = os.environ.get("LOCALAPPDATA")
    if base:
        alt = Path(base) / "hermes" / "plugins" / "audio-strip" / "state.json"
        if alt not in candidates:
            candidates.append(alt)
    last: Exception | None = None
    for p in candidates:
        try:
            return _json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            last = exc
    return {"error": f"audio-strip unavailable: {last}"}


@router.get("/sendlog")
def get_sendlog(limit: int = 50):
    p = BUS.sendlog_path()
    rows = []
    try:
        # tail-read (last 64 KB): the audit log grows without bound (and now
        # rotates at 8 MB, #17); never readlines() the whole file for a view.
        with open(p, "rb") as f:
            f.seek(0, 2)
            start = max(0, f.tell() - 65536)
            f.seek(start)
            if start:
                # Deep rinse (off-by-one): only drop the first line when it is
                # genuinely partial — a line-aligned boundary used to silently
                # eat one intact audit row per call.
                f.seek(start - 1)
                if f.read(1) != b"\n":
                    f.readline()
            lines = f.read().decode("utf-8", errors="replace").splitlines()
        import json as _json
        # one corrupt/rotated line must not 500 every
        # later poll — skip malformed rows, keep the readable tail.
        rows = []
        for x in lines[-max(1, min(500, int(limit))):]:
            if not x.strip():
                continue
            try:
                rows.append(_json.loads(x))
            except ValueError:
                continue
    except OSError:
        pass
    return {"path": str(p), "rows": rows}


# ---- profile store (every route does file IO — all plain def / threadpool) ---

@router.get("/profile/list")
def profile_list():
    return {"profiles": profile_store.list_profiles()}


@router.get("/profile/get")
def profile_get(id: str = ""):
    try:
        return profile_store.load_profile(id)
    except (KeyError, ValueError, OSError) as exc:  # deep rinse: OSError = disk full / AV lock / sharing violation, refuse by name not 500
        return _profile_err(exc)


@router.get("/profile/active")
def profile_active():
    pid = profile_store.get_active_profile_id()
    out = {"active": pid, "profile": None}
    if pid:
        try:
            out["profile"] = profile_store.load_profile(pid)
        except (KeyError, ValueError, OSError) as exc:  # deep rinse: OSError = disk full / AV lock / sharing violation, refuse by name not 500
            out["error"] = str(exc)
    return out


@router.post("/profile/activate")
async def profile_activate(request: Request):
    body = await _json_body(request)

    if "id" not in body:
        return {"error": "id required (id: null deactivates explicitly)"}  # a typo'd call silently DEACTIVATED
    aid = body.get("id")
    if aid is not None:
        aid = str(aid)
    def _do():
        # resolve BEFORE the sweep — a dead id must
        # not kill a running wah on its way to a named refusal (B05 law).
        if aid is not None:
            try:
                profile_store.load_profile(aid)
            except (KeyError, ValueError) as exc:
                return _profile_err(exc)
        running = [b for b in list(_mc().BUSES.values()) if b.matrix_status().get("running")]
        warn = _stop_matrix_for_transition()  # #10: transitions restore bases first
        try:
            profile_store.set_active_profile_id(aid)
            out = {"ok": True, "active": aid}
            if warn and warn.get("error"):
                out["matrix_warning"] = warn["error"]
            return out
        except (KeyError, ValueError, OSError) as exc:  # deep rinse: OSError = disk full / AV lock / sharing violation, refuse by name not 500
            # A12 (2026-11-10): the persist failed — the active
            # preset is unchanged, so the matrix the transition stopped must
            # not stay dead (restore-on-failure law, save-branch parity).
            for b in running:
                _restart_matrix(b)
            return _profile_err(exc)
    return await run_in_threadpool(_do)


@router.post("/profile/save")
async def profile_save(request: Request):
    # Body key is 'doc', NOT 'profile': the web-server auth gate
    # (dashboard_auth/native_http._check_profile_scope) inspects body['profile']
    # on EVERY request as a Hermes-profile selector and 403s anything that
    # isn't a string — a profile DOCUMENT under that key never reaches us.
    body = await _json_body(request)

    def _do():
        doc = body.get("doc")
        pid = (doc or {}).get("profile_id") if isinstance(doc, dict) else None
        active = profile_store.get_active_profile_id()
        # regression review B05 (owner 2026-11-10): saving the ACTIVE profile used to stop
        # the running matrix and leave it stopped — the pane's follow-up
        # /matrix/reload saw "not running" and no-oped, silently killing the
        # wah mid-edit. Now: a NON-active save cannot disturb the matrix at
        # all (edges compile from the active doc), and an active save is one
        # transaction — stop (restore bases), persist, restart iff it was
        # running. save_profile validates before writing, so an invalid doc
        # never even reaches the stop.
        if not pid or pid != active:
            try:
                return {"ok": True, "path": profile_store.save_profile(doc)}
            except (ValueError, OSError) as exc:  # OSError: disk full / AV lock on os.replace
                return _profile_err(exc)
        was_running = [b for b in list(_mc().BUSES.values())
                       if b.bus_id == _active_bus_id() and b.matrix_status().get("running")]
        stopped = []
        for b in was_running:
            r = b.matrix_stop()
            if not r.get("ok"):
                # Refuse the save rather than persist under a matrix we cannot
                # cleanly stop-and-recapture (base law over hot-apply).
                for bb in stopped:
                    _restart_matrix(bb)  # bare matrix_start() was a TypeError landmine (arity)
                return {"ok": False, "error": f"matrix stop refused: {r.get('error')}"}
            stopped.append(b)
        try:
            out = {"ok": True, "path": profile_store.save_profile(doc)}
        except (ValueError, OSError) as exc:
            for b in stopped:  # doc unchanged — re-arm the matrix we paused (old doc still on disk)
                _restart_matrix(b)
            return _profile_err(exc)
        for b in stopped:
            rr = _restart_matrix(b)  # re-validates + re-captures bases from the NEW doc
            if rr.get("ok"):
                out["matrix_restarted"] = True
            else:
                out["matrix_warning"] = f"matrix restart refused: {rr.get('error')}"
        return out
    return await run_in_threadpool(_do)


@router.post("/profile/delete")
async def profile_delete(request: Request):
    body = await _json_body(request)

    def _do():
        _id = str(body.get("id", ""))
        # regression guard bug 1 (agent parity): refuse a doomed delete BEFORE the
        # sweep — bundled/no-file ids used to kill a running matrix on the
        # way to their ok:false answer.
        if not profile_store.has_user_profile(_id):
            return {"ok": False,
                    "error": "no user preset with that id (bundled presets are factory — copy them first, then delete the copy)"}
        warn = _stop_matrix_if_active(_id)  # #10 + B05 audit: only an ACTIVE-profile delete is a matrix transition
        try:
            ok = profile_store.delete_profile(_id)
            out = {"ok": ok}
            if not ok:  # review 2026-11-09: bare ok:false made the pane say "unreachable" for a 200 answer — name the refusal
                out["error"] = "no user preset with that id (bundled presets are factory — copy them first, then delete the copy)"
            if warn and warn.get("error"):
                out["matrix_warning"] = warn["error"]
            return out
        except (ValueError, OSError) as exc:  # deep rinse: file locked by AV -> named error, not 500
            # the transition stopped the engine but
            # the profile SURVIVED the failed delete — re-arm it (save-route
            # rollback parity) instead of leaving a silently dead wah.
            if warn and warn.get("stopped"):
                _restart_matrix(_active_bus())
            return _profile_err(exc)
    return await run_in_threadpool(_do)


@router.post("/profile/copy")
async def profile_copy(request: Request):
    body = await _json_body(request)

    def _do():
        try:
            return {"ok": True, "path": profile_store.copy_profile(
                str(body.get("from", "")), str(body.get("to", "")))}
        except (KeyError, ValueError, OSError) as exc:
            return _profile_err(exc)
    return await run_in_threadpool(_do)


@router.post("/profile/rename")
async def profile_rename(request: Request):
    body = await _json_body(request)

    def _do():
        # regression guard bug 1 (agent parity): refuse a doomed rename BEFORE the
        # sweep — "already exists" / "no such user profile" used to kill a
        # running matrix on the way to the error.
        refusal = profile_store.rename_refusal(str(body.get("from", "")), str(body.get("to", "")))
        if refusal:
            return {"ok": False, "error": refusal}
        warn = _stop_matrix_if_active(str(body.get("from", "")))  # #10 + B05 audit: the id swaps under a running matrix ONLY if it is the active one
        try:
            out = {"ok": True, "id": profile_store.rename_profile(
                str(body.get("from", "")), str(body.get("to", "")))}
            if warn and warn.get("error"):
                out["matrix_warning"] = warn["error"]
            return out
        except (KeyError, ValueError, OSError) as exc:
            # rename failed -> the preset is intact
            # under its OLD id; re-arm the engine we stopped for it.
            if warn and warn.get("stopped"):
                _restart_matrix(_active_bus())
            return _profile_err(exc)
    return await run_in_threadpool(_do)


@router.post("/profile/import")
async def profile_import(request: Request):
    body = await _json_body(request)
    try:
        path = await run_in_threadpool(
            lambda: profile_store.import_profile(str(body.get("json", "")),
                                                 body.get("new_id")))
        return {"ok": True, "path": path}
    except Exception as exc:
        return _profile_err(exc)


@router.get("/profile/export")
def profile_export(id: str = ""):
    try:
        return {"json": profile_store.export_profile(id)}
    except (KeyError, ValueError, OSError) as exc:  # deep rinse: OSError = disk full / AV lock / sharing violation, refuse by name not 500
        return _profile_err(exc)


# ---- chat box (owner feature 2026-11-09): natural-language preset editing. ----
# The proposal engine — system prompt, JSON extraction, op whitelist — lives in
# chat_engine.py, shared with the agent-side RPC handler and the test battery.
#
# What is GONE here (catalog review, PR 153102): the /ask and /llm/models routes
# and the whole transport stack under them (provider resolution, lane guards,
# SSE, cold-catalog probes, the personal fallback ports). A plugin must not hold
# the user's provider credentials, so the pane now calls the plugin RPC
# `plugin.z-midi-console.chat.ask`; that handler runs the proposal through
# ctx.llm on the `midi_chat` auxiliary slot, and Hermes keeps the keys, picks the
# transport and follows the model the user chose under Settings -> Models ->
# Auxiliary. The names below stay as re-exports: the chat battery pins them and
# the pane's op preview shares them.
from chat_engine import (  # noqa: E402
    CURVES as _CURVES_CHAT,
    EDGE_KINDS as _EDGE_KINDS_CHAT,
    HEX6 as _HEX6,
    WAVES as _WAVES_CHAT,
    build_system_prompt as _chat_system,
    extract_json as _extract_json,
    int_in as _int_in,
    label as _label,
    num_in as _num_in,
    profile_lines as _chat_profile_lines,
    sanitize_edge as _sanitize_edge,
    sanitize_one as _sanitize_one,
)
