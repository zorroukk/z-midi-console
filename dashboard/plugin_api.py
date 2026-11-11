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
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".local" / "share"
    p = root / "hermes" / "plugins" / "audio-strip" / "state.json"
    try:
        return _json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"error": f"audio-strip unavailable: {exc}"}


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
# Laws: stdlib only (urllib); NO secrets and NO exfil — /ask will only ever talk
# to a local http://127.0.0.1 model lane (the pane passes {base, id}; anything
# non-local is refused before the request). The LLM ONLY PROPOSES: every op is
# whitelisted here, and the pane re-resolves every label against the live doc —
# a hallucinated label can never write anything.
import json as _chatjson          # noqa: E402  (chat-local alias: the route half is an add-on, not core)
import re as _chatre              # noqa: E402
import urllib.request as _urlreq  # noqa: E402
import urllib.parse as _urlsplit  # noqa: E402  (exfil guard parses the lane URL)
import ipaddress as _chatip  # noqa: E402  (cloud-lane guard: which http hosts are 'trusted net')
import threading as _chatthr      # noqa: E402
import concurrent.futures as _chatfut  # noqa: E402  (cold catalogs probe together under one 1.5 s wall)
_CHAT_SEM = _chatthr.Semaphore(2)  # /ask concurrency cap (threadpool starvation)

_CHAT_LANES = (
    ("lm-studio funnel", "http://127.0.0.1:1249/v1"),
    ("ollama", "http://127.0.0.1:11434/v1"),
    ("gateway turbofit", "http://127.0.0.1:8091/v1"),
)
_HEX6 = _chatre.compile(r"^#[0-9a-f]{6}$")
_EDGE_KINDS_CHAT = {"lfo", "move", "when", "watch"}
_CURVES_CHAT = {"lin", "exp", "log", "s", "step4", "snap12"}
_WAVES_CHAT = {"sine", "tri", "saw", "sh"}


def _http_json(url: str, payload=None, timeout: float = 8.0, headers=None):
    data = None if payload is None else _chatjson.dumps(payload).encode("utf-8")
    hdrs = {"content-type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = _urlreq.Request(url, data=data, headers=hdrs,
                          method="POST" if data is not None else "GET")
    try:
        with _lane_opener().open(req, timeout=timeout) as r:
            return _chatjson.loads(r.read().decode("utf-8"))
    except _urlreq.HTTPError as e:  # urllib.request re-exports HTTPError
        # the status and body ARE the diagnosis — a 401 (auth),
        # a 429 (quota), a 400 (unsupported param) used to all collapse into
        # one 'unreachable (HTTPError)'. Keep the class name (TimeoutError
        # detection upstream relies on it) but say what the lane actually said.
        try:
            detail = e.read().decode("utf-8", "replace")[:240].replace("\n", " ").strip()
        except Exception:  # noqa: BLE001
            detail = ""
        raise _urlreq.HTTPError(e.url, e.code, f"{e.reason} — {detail or 'no body'}", e.headers, None) from None


def _http_sse_json(url: str, payload, timeout: float = 25.0, headers=None):
    """POST a Responses request and collapse its SSE text deltas to JSON shape."""
    hdrs = {"content-type": "application/json", "accept": "text/event-stream"}
    if headers:
        hdrs.update(headers)
    req = _urlreq.Request(url, data=_chatjson.dumps(payload).encode("utf-8"), headers=hdrs, method="POST")
    parts = []
    completed = None
    try:
        with _lane_opener().open(req, timeout=timeout) as response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    event = _chatjson.loads(data)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind = str(event.get("type") or "")
                if kind in ("response.output_text.delta", "response.text.delta") and isinstance(event.get("delta"), str):
                    parts.append(event["delta"])
                elif kind == "response.completed" and isinstance(event.get("response"), dict):
                    completed = event["response"]
                elif kind in ("error", "response.failed", "response.incomplete"):
                    detail = event.get("error") or event.get("response") or event
                    raise RuntimeError("Responses stream failed: " + str(detail)[:220])
    except _urlreq.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:240].replace("\n", " ").strip()
        except Exception:  # noqa: BLE001
            detail = ""
        raise _urlreq.HTTPError(e.url, e.code, f"{e.reason} — {detail or 'no body'}", e.headers, None) from None
    if parts:
        return {"output_text": "".join(parts)}
    return completed if isinstance(completed, dict) else {"output_text": ""}


def _probe_models(base: str) -> list[str]:
    """Read OpenAI-style or Ollama-native model catalogs from one cold lane."""
    data = _http_json(base.rstrip("/") + "/models", timeout=1.5)
    rows = data.get("data") or data.get("models") or []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = row.get("id") or row.get("model") or row.get("name")
        if mid:
            out.append(str(mid))
    return out[:40]


def _lane_ok(u) -> bool:
    """Owner override 2026-11-10: cloud lanes are allowed — the guard is no
    longer 'never leaves the machine' but 'never crosses the open internet in
    plaintext': https anywhere, http only on loopback / private LAN / Tailscale
    CGNAT (100.64.0.0/10 — common tailnet CGNAT range). Credentials in the URL
    userinfo stay refused (the regression review userinfo-bypass class)."""
    if u.scheme == "https":
        return not (u.username or u.password)
    if u.scheme != "http":
        return False
    if u.username or u.password:
        return False
    host = (u.hostname or "").lower()
    if host == "localhost":
        return True
    try:
        ip = _chatip.ip_address(host)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_private:  # is_private covers 10/8, 172.16/12, 192.168/16, ::1, fc00::/7
        return True
    return ip.version == 4 and ip in _chatip.ip_network("100.64.0.0/10")


class _LaneGuardRedirect(_urlreq.HTTPRedirectHandler):
    """A2 (): urllib follows 301/302 blindly and
    re-sends the ORIGINAL headers — including Authorization — to the new
    host. A lane answering with a 302 used to hand our Bearer to any origin,
    and the redirect target was never re-checked against the guard law.
    Re-check the guard at every hop; strip auth when the origin changed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        r = super().redirect_request(req, fp, code, msg, headers, newurl)
        if r is None:
            return None
        if not _lane_ok(_urlsplit.urlsplit(newurl)):
            raise _urlreq.HTTPError(
                newurl, code,
                "lane redirected outside the guard law (https, or http on "
                "localhost/LAN/Tailscale) — refused, credentials did not follow",
                headers, fp)
        if _urlsplit.urlsplit(req.full_url).netloc != _urlsplit.urlsplit(newurl).netloc:
            for h in ("Authorization", "Proxy-Authorization"):
                if r.has_header(h):
                    r.remove_header(h)
        return r


def _lane_opener():
    return _urlreq.build_opener(_LaneGuardRedirect())


def _int_in(v, lo: int, hi: int):
    if isinstance(v, bool):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _num_in(v, lo: float, hi: float):
    if isinstance(v, bool):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _label(v):
    s = str(v or "").strip()
    return s[:80] or None


def _sanitize_edge(e):
    if not isinstance(e, dict):
        return None
    kind = e.get("kind", "lfo")
    if kind not in _EDGE_KINDS_CHAT:
        return None
    out = {"kind": kind}
    for key in ("source", "target"):
        lab = _label(e.get(key))
        if lab:
            out[key] = lab
    if "depth" in e:
        d = _num_in(e.get("depth"), -2.0, 2.0)
        if d is None:
            return None
        out["depth"] = d
    if "slew" in e:
        s = _num_in(e.get("slew"), 0.0, 1.0)
        if s is None:
            return None
        out["slew"] = s
    if "mode" in e:
        if e.get("mode") not in ("add", "mul", "set"):
            return None
        out["mode"] = e["mode"]
    if "curve" in e:
        if e.get("curve") not in _CURVES_CHAT:
            return None
        out["curve"] = e["curve"]
    if "wave" in e:
        if e.get("wave") not in _WAVES_CHAT:
            return None
        out["wave"] = e["wave"]
    if "rate_hz" in e:
        r = _num_in(e.get("rate_hz"), 0.01, 10.0)
        if r is None:
            return None
        out["rate_hz"] = r
    if "phase" in e:
        p = _num_in(e.get("phase"), 0.0, 1.0)
        if p is None:
            return None
        out["phase"] = p
    if "center" in e:
        c = _int_in(e.get("center"), 0, 127)
        if c is None:
            return None
        out["center"] = c
    if "amp" in e:
        a = _int_in(e.get("amp"), 0, 127)
        if a is None:
            return None
        out["amp"] = a
    if kind == "move":
        for key in ("from", "to"):
            n = _int_in(e.get(key), 0, 127)
            if n is None:
                return None
            out[key] = n
        d = _int_in(e.get("duration_ms"), 10, 600000)
        if d is None:
            return None
        out["duration_ms"] = d
        if "hold" in e:
            if not isinstance(e.get("hold"), bool):
                return None
            out["hold"] = e["hold"]
    if kind in ("when", "watch"):
        # the validator accepts level/crosses on
        # watch edges too — dropping them made "wah fires on down" via chat
        # silently become up.
        if kind == "when" or "level" in e:
            lv = _int_in(e.get("level"), 0, 127)
            if lv is None:
                return None
            out["level"] = lv
        if kind == "when" or "crosses" in e:
            if e.get("crosses") not in ("up", "down"):
                return None
            out["crosses"] = e["crosses"]
        if "once" in e:
            if not isinstance(e.get("once"), bool):
                return None
            out["once"] = e["once"]
        # then-lfo source is law-bound to 'lfo' —
        # carry any label through and the SAVE refuses the whole doc later.
        then = [_sanitize_edge(t) for t in (e.get("then") or [])
                if isinstance(t, dict) and t.get("kind") in ("move", "lfo")]
        then = [dict(t, source="lfo") if t and t.get("kind") == "lfo" else t for t in then]
        then = [t for t in then if t]
        if not then:
            return None
        out["then"] = then
    return out


def _sanitize_one(o):
    if not isinstance(o, dict):
        return None
    op = o.get("op")
    if op in ("color", "clear_color"):
        t = _label(o.get("target"))
        if not t:
            return None
        if op == "clear_color":
            return {"op": "clear_color", "target": t}
        h = str(o.get("hex") or "").lower()
        if not _HEX6.match(h):
            return None
        return {"op": "color", "target": t, "hex": h}
    if op == "rename":
        kind, frm, to = o.get("kind"), _label(o.get("from")), _label(o.get("to"))
        if kind not in ("section", "control") or not frm or not to:
            return None
        return {"op": "rename", "kind": kind, "from": frm, "to": to}
    if op == "move_section":
        nm = _label(o.get("name"))
        row, order = _int_in(o.get("row"), 0, 99), _int_in(o.get("order"), 0, 99)
        if not nm or row is None or order is None:
            return None
        return {"op": "move_section", "name": nm, "row": row, "order": order}
    if op == "order_sections":
        by = o.get("by")
        if by not in ("size", "name"):
            return None
        return {"op": "order_sections", "by": by}
    if op == "style":
        nm = _label(o.get("name"))
        if not nm:
            return None
        out: dict[str, object] = {"op": "style", "name": nm}
        if "bold" in o:
            if not isinstance(o.get("bold"), bool):
                return None
            out["bold"] = o["bold"]
        if "font_size" in o:
            fs = _int_in(o.get("font_size"), 8, 48)
            if fs is None:
                return None
            out["font_size"] = fs
        if len(out) == 2:
            return None
        return out
    if op == "invert":
        lab, on = _label(o.get("label")), o.get("on")
        if not lab or not isinstance(on, bool):
            return None
        return {"op": "invert", "label": lab, "on": on}
    if op == "set":
        # Live move of one control (a CC send through the Bus — wire remap and
        # shadow apply like any panel move). NOT a preset edit.
        lab = _label(o.get("label"))
        if not lab:
            return None
        if o.get("percent") is not None:
            p = _num_in(o.get("percent"), 0.0, 100.0)
            if p is None:
                return None
            return {"op": "set", "label": lab, "value": int(round(p * 127.0 / 100.0))}
        v = o.get("value")
        if isinstance(v, bool):  # bool BEFORE int (bool is an int subclass)
            return {"op": "set", "label": lab, "value": 127 if v else 0}
        vi = _int_in(v, 0, 127)
        if vi is None:
            return None
        return {"op": "set", "label": lab, "value": vi}
    if op == "matrix_add":
        e = _sanitize_edge(o.get("edge"))
        return {"op": "matrix_add", "edge": e} if e else None
    if op == "matrix":
        act = o.get("action")
        if act not in ("start", "stop"):
            return None
        return {"op": "matrix", "action": act}
    if op == "save_as":
        nm = _label(o.get("name"))
        if not nm:
            return None
        return {"op": "save_as", "name": nm}
    return None


def _extract_json(text: str):
    """First complete JSON object in a reply, preferring fenced payloads.

    A first-``{``/last-``}`` slice joined unrelated prose braces and multiple
    objects into invalid JSON. Scan balanced candidates instead, respecting
    quoted braces and escapes; a malformed candidate cannot hide a later good
    proposal.
    """
    if not isinstance(text, str):
        return None
    t = text.strip()
    fenced = _chatre.findall(r"```(?:json)?\s*(.*?)```", t, flags=_chatre.IGNORECASE | _chatre.DOTALL)

    def _candidates(src: str):
        start = None
        depth = 0
        quoted = False
        escaped = False
        for i, ch in enumerate(src):
            if start is None:
                if ch == "{":
                    start, depth = i, 1
                continue
            if quoted:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    quoted = False
                continue
            if ch == '"':
                quoted = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield src[start:i + 1]
                    start = None

    for src in [*fenced, t]:
        for candidate in _candidates(src):
            try:
                obj = _chatjson.loads(candidate)
            except (ValueError, TypeError):
                continue
            if isinstance(obj, dict):
                return obj
    return None


def _reply_text(resp, responses_mode: bool) -> str:
    """Normalize the text-bearing reply shapes used by supported transports."""
    if not isinstance(resp, dict):
        return ""
    if responses_mode:
        direct = resp.get("output_text")
        if isinstance(direct, str) and direct:
            return direct
        parts = []
        for item in (resp.get("output") or []):
            if not isinstance(item, dict):
                continue
            for content in (item.get("content") or []):
                if not isinstance(content, dict) or content.get("type") not in ("output_text", "text"):
                    continue
                value = content.get("text")
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(parts)
    direct_message = resp.get("message")
    if isinstance(direct_message, dict) and isinstance(direct_message.get("content"), str):
        return direct_message["content"]  # Ollama native /api/chat
    choices = resp.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message") or {}
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") in ("text", "output_text"):
                value = part.get("text")
                if isinstance(value, str):
                    parts.append(value)
                elif isinstance(value, dict) and isinstance(value.get("value"), str):
                    parts.append(value["value"])
        return "\n".join(parts)
    return ""


def _chat_system(summary) -> str:
    return (
        "You are the chat command engine of Z's MIDI Console, a MIDI control-surface plugin. "
        "The user speaks a change to their device preset; you answer with ONE JSON object and "
        "nothing else — no fences, no prose outside the JSON:\n"
        '{"say": "<one or two short sentences: what you did or what you need clarified>", "ops": [ ... ]}\n'
        "Allowed ops (exact shapes; anything else is dropped):\n"
        '{"op":"color","target":"section:<name>"|"control:<label>","hex":"#rrggbb"} — paint a whole category (label+box+contents) or one control (value+name+card)\n'
        '{"op":"clear_color","target":"..."} — remove colors from that target\n'
        '{"op":"rename","kind":"section"|"control","from":"<current label>","to":"<new label>"}\n'
        '{"op":"move_section","name":"<section label>","row":0-99,"order":0-99} — grid: row=band (0 top), order=column (0 left)\n'
        '{"op":"order_sections","by":"size"|"name"} — the pane re-lays every category: biggest (or A-Z) first, two per band, in order\n'
        '{"op":"style","name":"<section label>","bold":bool,"font_size":8-48} — category label styling\n'
        '{"op":"invert","label":"<control label>","on":bool} — flip the control on the wire (device gets 127-value)\n'
        '{"op":"set","label":"<control label>","value":0-127} — move the REAL control right now (live send; the device and panel follow). "percent":0-100 also accepted. Toggles: 127=on, 0=off. For a gradual sweep over time use a move lane instead.\n'
        '{"op":"matrix_add","edge":{...}} — add a modulation lane. Edge kinds:\n'
        '  lfo: {"kind":"lfo","target":"<label>","wave":"sine|tri|saw|sh","rate_hz":0.01-10,"depth":-2..2,"phase":0-1}\n'
        '  move: {"kind":"move","target":"<label>","from":0-127,"to":0-127,"duration_ms":10-600000,"curve":"lin|exp|log|s|step4|snap12","hold":bool} — one-shot ramp\n'
        '  when: {"kind":"when","source":"<label>","level":0-127,"crosses":"up|down","once":bool,"then":[move|lfo edges]}\n'
        '  watch: {"kind":"watch","source":"<label>","once":bool,"then":[move|lfo edges]} — fires when the user twists that control; then-lfos may carry "center":0-127 and "amp":0-127 to orbit a named range\n'
        '{"op":"matrix","action":"start"|"stop"}\n'
        '{"op":"save_as","name":"<new preset name>"} — duplicate the current preset under a new name and switch to it\n'
        "Rules: surface values are 0-127 — convert percents (70% -> 89, 100% -> 127). "
        "Name targets with EXACT labels from the profile summary below; never invent one. "
        "When the user points loosely ('wave knob', 'the cutoff', 'main filter'), pick the control "
        "whose label best matches and use THAT exact label — 'Wave knob' means control \"Wave\". "
        "Only ask when two controls match equally well. "
        "Write `say` in the same language the user used; op names and fields stay exactly as documented. "
        "If a sentence is ambiguous, return ops: [] and ask in say. Order ops as the user wants them applied. "
        "The user cannot see this summary.\n"
        "PROFILE SUMMARY (one line per control: LABEL (cc, widget, section, inverted)):\n"
        + _chat_profile_lines(summary)
    )


def _chat_profile_lines(summary: dict) -> str:
    """the raw JSON dump used to be CHAR-truncated at 6000 —
    the bundled Push profile (9515 chars) reached the model as broken JSON
    cut mid-"save_btn", hiding 35 of 85 controls. One short line per control
    fits whole profiles; if a monster still overflows, WHOLE lines drop and
    the model is told — never half a record."""
    out = []
    for c in (summary.get("controls") or []):
        if not isinstance(c, dict):
            continue
        lab = str(c.get("label") or c.get("id") or "?")
        det = []
        if c.get("cc") is not None:
            det.append(f"cc{c['cc']}")
        w = str(c.get("widget") or "")
        if w and w != "knob":
            det.append(w)
        if c.get("section"):
            det.append(str(c["section"]))
        if c.get("invert"):
            det.append("inverted")
        out.append("- " + lab + ((" (" + ", ".join(det) + ")") if det else ""))
    for e in (summary.get("matrix") or []):
        if isinstance(e, dict):
            out.append("- lane " + _chatjson.dumps(e, separators=(",", ":")))
    budget, kept, used = 9000, [], 0
    for line in out:
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    if len(kept) < len(out):
        kept.append(f"- ({len(out) - len(kept)} further entries exist but were omitted for space — "
                    "if your target may be among them, say so in `say` instead of guessing a label)")
    return "\n".join(kept)


@router.get("/llm/models")
def get_llm_models():
    """Model lanes for the chat box — the SAME list the app's bottom-right model
    selector shows (owner law 2026-11-10: 'same as a standard user finds in the
    model selector, individual to each user's hermes environment'). We call the
    shared inventory substrate behind /api/model/options, not our own probes.
    Lanes whose catalog came back empty get one cheap live probe (cold hosts);
    still-empty lanes are returned marked dead so the pane can say why."""
    payload = None
    inv_err = ""
    ctx = None
    try:
        from hermes_cli import inventory  # type: ignore[import-not-found]  # same-host import: the host IS a hermes_cli process
        ctx = inventory.load_picker_context()
        payload = inventory.build_model_options_payload(ctx, explicit_only=True, refresh=False)
    except Exception as exc:  # noqa: BLE001 — no hermes_cli (tests): fall back to local probes
        payload = None
        inv_err = exc.__class__.__name__
    if payload is None:
        out = []
        for name, base in _CHAT_LANES:
            try:
                data = _http_json(base + "/models", timeout=1.5).get("data") or []
                ms = [m.get("id") for m in data if isinstance(m, dict) and m.get("id")][:40]
                if ms:
                    out.append({"lane": name, "slug": name, "models": ms})
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "lanes": out, "inventory": False, "inventory_error": inv_err}
    # base urls for live re-probes of cold lanes (named providers only — cloud
    # catalogs come from Hermes' own cached catalogs and need no probe)
    bases: dict[str, str] = {}
    for c in (getattr(ctx, "custom_providers", None) or []):
        if isinstance(c, dict) and c.get("provider_key"):
            bases[str(c["provider_key"])] = str(c.get("base_url") or "")
    for k, v in (getattr(ctx, "user_providers", None) or {}).items():
        if isinstance(v, dict) and v.get("base_url"):
            bases.setdefault(str(k), str(v["base_url"]))
    lanes = []
    # lanes the chat CANNOT drive (anthropic messages, bedrock,
    # app-server, plugin transports) must not sit selectable-by-default in the
    # picker. Mark them from the resolver's own api_mode (row or provider
    # config); unknown api_mode stays drivable — most lanes are OpenAI-style
    # and a real mismatch still refuses by name at /ask.
    _drive = ("chat_completions", "openai_chat", "codex_responses", "responses", "")
    # A9 (): built-in rows carry no api_mode, so
    # anthropic/bedrock/azure sat selectable-by-default and only refused by
    # name AFTER Send. These transports are never drivable from this box.
    _nonchat = {"anthropic", "bedrock", "vertex", "azure-foundry", "azure"}
    prov_mode: dict[str, str] = {}
    for c in (getattr(ctx, "custom_providers", None) or []):
        if isinstance(c, dict) and c.get("provider_key"):
            prov_mode[str(c["provider_key"])] = str(c.get("api_mode") or "")
    for k, v in (getattr(ctx, "user_providers", None) or {}).items():
        if isinstance(v, dict) and v.get("api_mode"):
            prov_mode.setdefault(str(k), str(v["api_mode"]))
    rows = [row for row in payload.get("providers", []) if isinstance(row, dict)]
    # A 1.5 s timeout PER empty provider used to add linearly; enough stopped
    # local/custom lanes could push model-list open toward the desktop IPC wall.
    # Probe all named cold catalogs together so the wall stays about 1.5 s.
    cold = {str(row.get("slug") or ""): bases[str(row.get("slug") or "")]
            for row in rows
            if row.get("slug") and not (row.get("models") or []) and str(row.get("slug")) in bases}
    probed: dict[str, list[str]] = {}
    if cold:
        with _chatfut.ThreadPoolExecutor(max_workers=min(8, len(cold)), thread_name_prefix="chat-models") as pool:
            futures = {slug: pool.submit(_probe_models, base) for slug, base in cold.items()}
            for slug, future in futures.items():
                try:
                    probed[slug] = future.result()
                except Exception:  # noqa: BLE001 — an offline lane is data, not a route failure
                    probed[slug] = []
    for row in rows:
        slug = str(row.get("slug") or "")
        if not slug or slug == "moa":
            continue  # MoA is a composite of other lanes — not a chat endpoint
        ms = [str(m) for m in (row.get("models") or []) if m][:40]
        dead = False
        if not ms and slug in probed:
            ms = probed[slug]
            dead = not ms
        amode = str(row.get("api_mode") or prov_mode.get(slug) or "")
        lanes.append({"lane": str(row.get("name") or slug), "slug": slug, "models": ms,
                      "current": bool(row.get("is_current")), "dead": dead and not ms,
                      "drivable": (amode in _drive) if amode else slug not in _nonchat})
    return {"ok": True, "lanes": lanes, "inventory": True,
            # A9 (): the payload's top-level model IS the model the
            # app itself is using — without it the pane could only default to
            # models[0], not the current model.
            "current_model": str(payload.get("model") or "")}


@router.post("/ask")
async def post_ask(request: Request):
    body = await _json_body(request)
    text = str(body.get("text") or "").strip()[:4000]
    if not text:
        return {"ok": False, "error": "nothing to ask"}
    model = body.get("model") or {}
    mid = str(model.get("id") or "").strip()
    slug = str(model.get("slug") or "")
    api_mode = "chat_completions"
    responses_stream = False
    lane_hdrs = None
    _ro = None  # resolver request_overrides ()
    if slug:
        # Owner law 2026-11-10: the chat box list mirrors the app's model
        # selector — so the ask resolves the picked slug through Hermes' OWN
        # resolver, the same ladder the agent uses when you switch models in
        # the picker (OAuth logins, subscriptions, named custom lanes).
        try:
            from hermes_cli.runtime_provider import resolve_runtime_provider  # type: ignore[import-not-found]
            rt = resolve_runtime_provider(requested=slug, target_model=mid)
        except Exception as exc:  # noqa: BLE001 — AuthError etc: name it, never 500
            return {"ok": False, "error": f"model lane {slug!r} could not be resolved: {str(exc)[:160]}"}
        base = str(rt.get("base_url") or "").rstrip("/")
        api_mode = str(rt.get("api_mode") or "chat_completions")
        responses_stream = str(rt.get("provider") or slug).lower() == "openai-codex"
        _ak = rt.get("api_key")
        lane_hdrs = None
        if callable(_ak):
            # Azure-Entra-class providers hand back a CALLABLE
            # token provider, not a key. str() of it produced "Bearer <function
            # …>" — a guaranteed 401 that Hermes' own source warns about.
            # Refuse by name; the agent runtime can mint it, this box cannot.
            return {"ok": False, "error": f"model lane {slug!r} authenticates through a Hermes token provider (Azure/vertex class) the console chat cannot hold — pick a static-key or subscription lane"}
        if isinstance(_ak, str) and _ak:
            lane_hdrs = {"Authorization": "Bearer " + _ak}
        # the resolver's EXTRA HEADERS are the whole auth story
        # for subscription lanes (Codex identity/account/residency) and
        # proxy-authenticated custom endpoints. Dropping them made every
        # non-Bearer provider a 401. Forward them (config order wins over our
        # Bearer only if the provider itself defines Authorization).
        _eh = rt.get("extra_headers")
        if isinstance(_eh, dict):
            for hk, hv in _eh.items():
                if hv is None:
                    continue
                lane_hdrs = lane_hdrs or {}
                lane_hdrs[str(hk)] = str(hv)
        _ro = rt.get("request_overrides") if isinstance(rt.get("request_overrides"), dict) else None
    else:
        base = str(model.get("base") or "")
    # Guard law (owner override 2026-11-10 supersedes local-only): cloud lanes
    # are in. The rule is now 'no plaintext over the open internet' — https
    # anywhere, http only loopback/LAN/Tailscale. URL userinfo stays refused
    # (the regression review userinfo-bypass class: http://127.0.0.1:***@evil.example).
    _u = _urlsplit.urlsplit(base)
    if not _lane_ok(_u):
        return {"ok": False, "error": "model lane must be https (cloud) or http on localhost/LAN/Tailscale — your presets never cross the open internet unencrypted"}
    if not mid:
        return {"ok": False, "error": "pick a model in the chat box first"}
    summary = body.get("summary")
    if not isinstance(summary, dict) or not isinstance(summary.get("controls"), list):
        return {"ok": False, "error": "pane must send the profile summary (controls)"}
    msgs = [{"role": "system", "content": _chat_system(summary)}]
    log = body.get("log")
    if isinstance(log, list):
        for m in log[-6:]:
            if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("text"):
                msgs.append({"role": m["role"], "content": str(m["text"])[:1200]})
    msgs.append({"role": "user", "content": text})
    # Effort knob (owner ask 2026-11-10): thinking models spend max_tokens on
    # hidden reasoning BEFORE the answer — small budgets can yield empty
    # content, big ones stall the ask. Translated per lane: Ollama speaks
    # think/think_budget, LM Studio & compatible servers reasoning_effort.
    effort = str(body.get("effort") or "auto")
    if effort not in ("auto", "fast", "balanced", "thoughtful"):
        effort = "auto"
    req_body = {"model": mid, "messages": msgs, "temperature": 0.1,
                "max_tokens": 1400 if effort == "thoughtful" else 900}
    if _ro:
        # A7 (): Hermes hands request_overrides in the
        # SDK's shape, where extra_body is MERGED into the JSON body — passing
        # it through as a literal "extra_body" property made lanes silently
        # ignore the params (or 400 on the unknown field). Spread it like the SDK.
        _ro = dict(_ro)
        _eb = _ro.pop("extra_body", None)
        if isinstance(_eb, dict):
            req_body.update(_eb)
        req_body.update(_ro)  # resolver body shaping (custom params some lanes demand)
    # Hermes normally exposes Ollama through its OpenAI-compatible /v1 base,
    # not /api. Key off the resolved lane name too or "effort: fast" leaves
    # Qwen thinking until the 25 s IPC-safe timeout despite the knob saying off.
    ollama_lane = base.endswith("/api") or slug.lower().startswith("ollama")
    if ollama_lane:
        if effort == "fast":
            req_body["think"] = False
        elif effort in ("balanced", "thoughtful"):
            req_body["think_budget"] = 2048 if effort == "balanced" else 8192
            req_body.setdefault("think", True)
    elif effort != "auto":
        req_body["reasoning_effort"] = {"fast": "low", "balanced": "medium",
                                        "thoughtful": "high"}[effort]
    # Transports the console chat can drive: plain OpenAI chat_completions
    # (every local lane + most clouds) and the OpenAI Responses API (codex
    # subscription lanes). Anything else (app-server, anthropic, bedrock) is
    # refused by name instead of sending bytes it won't understand.
    if api_mode not in ("chat_completions", "openai_chat", "codex_responses", "responses", ""):
        return {"ok": False, "error": f"model lane {slug or base!r} speaks {api_mode!r}, which the console chat cannot drive — pick a chat model from the list"}
    if api_mode in ("codex_responses", "responses"):
        # NO temperature on the Responses lane — the gpt-5
        # family 400s on anything but its default, and Hermes does
        # endpoint-specific construction for exactly this reason.
        req_body = {"model": mid, "instructions": msgs[0]["content"],
                    "input": [{"role": m["role"], "content": m["content"]} for m in msgs[1:]],
                    "store": False,
                    "max_output_tokens": 1400 if effort == "thoughtful" else 900}
        if effort != "auto":
            req_body["reasoning"] = {"effort": {"fast": "low", "balanced": "medium",
                                                "thoughtful": "high"}[effort]}
        if _ro:
            req_body.update(_ro)
        if responses_stream:
            # ChatGPT/Codex's subscription endpoint is SSE-only even for this
            # short proposal: it returns HTTP 400 unless stream is true.
            req_body["stream"] = True
        call_url, call_body = base + "/responses", req_body
    elif ollama_lane:
        # Use Ollama's native endpoint so its `think` control is actually
        # honored. The OpenAI-compatible /v1 path accepts the request but can
        # ignore `think:false`, leaving small local models reasoning past the
        # desktop's 25 s safety wall.
        root = base[:-3] if base.endswith("/v1") else (base[:-4] if base.endswith("/api") else base)
        call_url = root.rstrip("/") + "/api/chat"
        proposal_schema = {"type": "object", "properties": {
            "say": {"type": "string"},
            "ops": {"type": "array", "items": {"type": "object"}},
        }, "required": ["say", "ops"]}
        call_body = {"model": mid, "messages": msgs, "stream": False, "format": proposal_schema,
                     "options": {"temperature": 0.1, "num_predict": 1400 if effort == "thoughtful" else (500 if effort == "fast" else 900)}}
        if effort == "fast":
            call_body["think"] = False
        elif effort in ("balanced", "thoughtful"):
            call_body["think"] = True
        if _ro:
            call_body.update(_ro)
    else:
        call_url, call_body = base + "/chat/completions", req_body
    # a stalled cold model holds a threadpool worker
    # for 90 s; a handful of stacked asks would starve the pool that /state,
    # /matrix/stop and every other route shares. Cap concurrency, refuse by name.
    if not _CHAT_SEM.acquire(blocking=False):
        return {"ok": False, "error": "chat lane busy (2 asks already running) — retry in a moment"}
    try:
        # 25 s, not 90: the desktop IPC gives up at 30 s — a named slow answer
        # beats the app's raw 'Timed out connecting to Hermes backend'.
        transport = _http_sse_json if responses_stream else _http_json
        resp = await run_in_threadpool(transport, call_url, call_body, 25.0, lane_hdrs)
    except Exception as exc:  # noqa: BLE001
        slow = exc.__class__.__name__ in ("TimeoutError", "socket.timeout") or "timed out" in str(exc).lower()
        if isinstance(exc, _urlreq.HTTPError):
            # a 401/429/400 is NOT "unreachable" — show what
            # the lane answered (auth problems then name themselves).
            return {"ok": False, "error": f"model lane answered {exc.code}: {str(exc.reason)[:220]}"}
        return {"ok": False, "error": ("model lane took over 25 s — it may be cold-loading or queued behind another job; press Send again once it is up"
                                       if slow else "model lane unreachable ({0})".format(exc.__class__.__name__))}
    finally:
        _CHAT_SEM.release()
    content = _reply_text(resp, api_mode in ("codex_responses", "responses"))
    if not content.strip():
        return {"ok": False, "error": "the model returned no answer text — it may have spent its reply budget thinking; try effort: fast or a larger model lane"}
    parsed = _extract_json(content)
    if parsed is None:
        return {"ok": False, "error": "the model answered with something unparseable — try a plainer sentence", "raw": str(content)[:300]}
    raw_ops = parsed.get("ops")
    if not isinstance(raw_ops, list):
        raw_ops = []
    ops, dropped = [], []
    for o in raw_ops:
        keep = _sanitize_one(o)
        if keep:
            ops.append(keep)
        else:
            dropped.append(_chatjson.dumps(o, default=str)[:120])
    say = str(parsed.get("say") or "")[:600]
    return {"ok": True, "say": say, "ops": ops, "dropped": dropped}
