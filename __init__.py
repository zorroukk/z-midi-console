"""Agent half: MIDI surface tools.

State lives in midi_core.BUS — the dashboard half (dashboard/plugin_api.py) imports the
SAME module instance (same process) so exactly one port owner exists.
"""
from __future__ import annotations

import base64
import json

from . import automation, midi_core, profile_store

TOOLSET = "midi"


def _js(payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _err(message: str) -> str:
    return _js({"error": message})


def _active_bus():
    """Deck parity with the REST half: the Bus of the ACTIVE profile
    (profile 'bus' key, omitted == 'primary' == the historical singleton)."""
    pid = profile_store.get_active_profile_id()
    bus = "primary"
    if pid:
        try:
            bus = str(profile_store.load_profile(pid).get("bus") or "primary")
        except Exception:
            pass
    return midi_core.get_bus(bus)


def _devices_handler(action="list", name=None, bus=None, **_kwargs) -> str:
    """#15: the chat path gets the session actions too — the README claims
    Hermes can connect, so a tool must actually do it. Deck: 'bus' picks the
    device slot (each bus owns one exclusive port pair)."""
    try:
        if action == "connect":
            if not name:
                return _err("name required (device name substring)")
            return _js(midi_core.get_bus(str(bus or "primary")).connect(str(name)))
        if action == "disconnect":
            midi_core.get_bus(str(bus or "primary")).disconnect()
            return _js({"ok": True})
        if action == "panic":
            results = [b.panic() for b in list(midi_core.BUSES.values()) if b.device]
            bad = next((r for r in results if not r.get("ok")), None)
            return _js({"ok": bad is None if results else True, "buses": results,
                        **({"error": bad.get("error") or "bus panic failed"} if bad else {})})
        if action == "list":
            return _js({"devices": midi_core.list_devices()})
        return _err(f"unknown action {action!r}")
    except Exception as exc:
        return _err(str(exc))


def _status_handler(sendlog=False, limit=50, **_kwargs) -> str:
    # Deep rinse: the only tool handler that used to raise straight into the
    # agent loop — every sibling promises "never raise", this one now keeps it.
    try:
        st = _active_bus().status()  # flat keys follow the ACTIVE bus (REST /state parity — agent used to answer 'not connected' about a device the pane showed connected, review 2026-11-09)
        st["active_profile"] = profile_store.get_active_profile_id()
        st["buses"] = [b.status() for b in list(midi_core.BUSES.values())]
        st["deck"] = profile_store.get_deck()
        st["deck_presets"] = profile_store.get_deck_presets()
        if sendlog:
            # REST /sendlog parity: tail-read (last 64 KB), never the whole file.
            import json as _json
            rows = []
            try:
                with open(midi_core.BUS.sendlog_path(), "rb") as f:
                    f.seek(0, 2)
                    f.seek(max(0, f.tell() - 65536))
                    for line in f:
                        try:
                            rows.append(_json.loads(line))
                        except ValueError:
                            pass
            except OSError:
                rows = []
            st["sendlog"] = rows[-max(1, int(limit)):]
        return _js(st)
    except Exception as exc:
        return _err(str(exc))


def _send_handler(kind="cc", cc=None, value=None, note=None, velocity=None,
                  channel=0, bus=None, **_kwargs) -> str:
    try:
        r = midi_core.get_bus(str(bus or "primary")).send(
            kind=kind, cc=cc, value=value, note=note,
            velocity=velocity, channel=channel, source="agent")
    except Exception as exc:  # never raise into the agent loop
        return _err(str(exc))
    return _js(r)


def _stop_matrix_for_transition() -> str | None:
    """#10 parity with the REST half: profile transitions restore matrix
    bases before changing what is active/bound. Deck law: sweep every bus."""
    err = None
    for b in list(midi_core.BUSES.values()):
        if b.matrix_status().get("running"):
            r = b.matrix_stop()
            if not r.get("ok"):
                err = r.get("error")
    return err


def _stop_matrix_if_active(pid) -> str | None:
    """B05 audit (2026-11-10, REST parity): a matrix compiles ONLY from the
    active profile — a delete/rename of a bystander preset must not stop a
    running wah."""
    active = profile_store.get_active_profile_id()
    if pid and str(pid) == active:
        return _stop_matrix_on_active_bus()
    return None


def _active_bus_id() -> str:
    """Bus id the ACTIVE profile binds (deck law: omitted 'bus' == 'primary')."""
    pid = profile_store.get_active_profile_id()
    if pid:
        try:
            return str(profile_store.load_profile(pid).get("bus") or "primary")
        except Exception:
            pass
    return "primary"


def _stop_matrix_on_active_bus() -> str | None:
    """B05 transaction sweep half (REST parity): stop the engine on the bus
    the ACTIVE profile binds — a secondary bus's engine is not this preset's
    matrix and must not be silently killed-and-not-revived."""
    err = None
    bid = _active_bus_id()
    for b in list(midi_core.BUSES.values()):
        if b.bus_id == bid and b.matrix_status().get("running"):
            r = b.matrix_stop()
            if not r.get("ok"):
                err = r.get("error")
    return err


def _restart_matrix(bus) -> dict:
    """B05 restart half (REST parity): re-start one bus's engine from the
    ACTIVE profile doc on disk — matrix_start needs the wire view; a bare
    call is a TypeError."""
    pid = profile_store.get_active_profile_id()
    if not pid:
        return {"ok": False, "error": "no active profile"}
    edges, lfo, cc_of, ch = profile_store.matrix_inputs(
        profile_store.load_profile(pid))
    return bus.matrix_start(edges, lfo, cc_of, ch)


def _profile_handler(action="list", id=None, profile=None, json_text=None,
                     new_id=None, ids=None, **_kwargs) -> str:
    try:
        warn = None
        if action == "list":
            return _js({"profiles": profile_store.list_profiles()})
        if action == "deck":
            return _js({"deck": profile_store.get_deck(),
                        "deck_presets": profile_store.get_deck_presets()})
        if action == "deck_set":
            # Deck law: active profile = deck[0]; every id must load
            # (refuse-closed). Matrix transitions sweep every bus (#10).
            if not isinstance(ids, list):
                return _err("ids must be a list of profile ids")
            # validate BEFORE the
            # destructive sweep; an unchanged deck skips it entirely.
            _seen: list[str] = []
            for d in ids:
                if not (isinstance(d, str) and d):
                    return _err(f"deck refused: ids must be non-empty strings, got {type(d).__name__}")
                if d in _seen:
                    continue  # duplicates collapse (set_deck law) — compare the EFFECTIVE deck
                _seen.append(d)
                try:
                    profile_store.load_profile(d)
                except (KeyError, ValueError) as exc:
                    return _err(f"deck refused: {exc}")
            if _seen == profile_store.get_deck():
                return _js({"ok": True, "deck": profile_store.get_deck()})
            warn = _stop_matrix_for_transition()
            try:
                deck = profile_store.set_deck(ids)
            except (KeyError, ValueError) as exc:
                return _err(f"deck refused: {exc}")
            return _js({"ok": True, "deck": deck,
                        **({"matrix_warning": warn} if warn else {})})
        if action == "get":
            if id is None:
                return _err("id required")  # deep rinse: str(None) made a phantom 'None' profile_id
            return _js(profile_store.load_profile(str(id)))
        if action == "activate":
            # Review 2026-11-09: a missing id silently DEACTIVATED the surface
            # and reported ok:true — same guard family as 'get' from the rinse.
            if id is None:
                return _err("id required (pass empty string to deactivate)")
            target = str(id) or None
            if target:
                # resolve before the
                # sweep — a dead id must not kill a running wah en route.
                # regression guard bug 1: the sweep moved INSIDE this branch, after
                # every refusal — the shared block used to stop a running
                # matrix before even the "id required" check ran.
                try:
                    profile_store.load_profile(target)
                except (KeyError, ValueError) as exc:
                    return _err(str(exc))
            # Re-activating the already-active surface changes nothing —
            # skip the sweep entirely (unchanged-transition law, /deck parity).
            running = [] if target == profile_store.get_active_profile_id() \
                else [b for b in list(midi_core.BUSES.values()) if b.matrix_status().get("running")]
            warn = None if not running else _stop_matrix_for_transition()
            try:
                profile_store.set_active_profile_id(target)
            except (KeyError, ValueError, OSError) as exc:
                # A12 (REST parity): persist failed -> re-arm the
                # engines the transition stopped (restore-on-failure law).
                for b in running:
                    _restart_matrix(b)
                return _err(str(exc))
            return _js({"ok": True, "active": target,
                        **({"matrix_warning": warn} if warn else {})})
        if action == "save":
            if not isinstance(profile, dict):
                return _err("profile must be an object")
            # regression review B05 (REST /profile/save parity, 2026-11-10): saving the
            # ACTIVE profile used to stop the matrix and leave it stopped.
            # Now one transaction: stop (restore bases) -> persist ->
            # restart iff it was running. save_profile validates before
            # writing, so an invalid doc never reaches the stop. A save of a
            # NON-active preset cannot disturb the matrix at all.
            pid = profile.get("profile_id")
            active = profile_store.get_active_profile_id()
            if not pid or pid != active:
                try:
                    return _js({"ok": True, "path": profile_store.save_profile(profile)})
                except (ValueError, OSError) as exc:
                    return _err(str(exc))
            was_running = [b for b in list(midi_core.BUSES.values())
                           if b.bus_id == _active_bus_id()
                           and b.matrix_status().get("running")]
            stopped = []
            for b in was_running:
                r = b.matrix_stop()
                if not r.get("ok"):
                    for bb in stopped:  # refuse the save rather than persist under an unrestorable matrix
                        _restart_matrix(bb)
                    return _err(f"matrix stop refused: {r.get('error')}")
                stopped.append(b)
            try:
                out = {"ok": True, "path": profile_store.save_profile(profile)}
            except (ValueError, OSError) as exc:
                for b in stopped:  # doc unchanged — re-arm the matrix we paused
                    _restart_matrix(b)
                return _err(str(exc))
            for b in stopped:
                rr = _restart_matrix(b)
                if rr.get("ok"):
                    out["matrix_restarted"] = True
                else:
                    out["matrix_warning"] = f"matrix restart refused: {rr.get('error')}"
            return _js(out)
        if action == "delete":
            if id is None:
                return _err("id required")  # str(None) phantom id
            # regression guard bug 1 (validate-before-transition law): a delete that
            # will be refused (bad slug, bundled factory preset, nothing on
            # disk) must not sweep a running matrix on the way to its answer.
            id_s = str(id)
            if not profile_store.has_user_profile(id_s):
                return _js({"ok": False,
                            "error": "no user preset with that id (bundled presets are factory — copy them first, then delete the copy)"})
            # A12: the rollback used to gate on `warn`, which is
            # None after a SUCCESSFUL stop — so it never ran. Track the buses
            # actually stopped (save-branch pattern) and re-arm those.
            running = [b for b in list(midi_core.BUSES.values())
                       if b.bus_id == _active_bus_id() and b.matrix_status().get("running")] \
                if id_s == profile_store.get_active_profile_id() else []
            warn = _stop_matrix_if_active(id_s)  # B05: only an ACTIVE-profile delete is a transition
            try:
                ok = profile_store.delete_profile(id_s)
            except (ValueError, OSError) as exc:
                # delete failed but the preset
                # survived — re-arm the engine the transition stopped.
                for b in running:
                    _restart_matrix(b)
                return _err(str(exc))
            return _js({"ok": ok,
                        **({"matrix_warning": warn} if warn else {})})
        if action in ("copy", "rename") and (id is None or new_id is None):
            return _err(f"{action} needs id (source) and new_id (target)")
        if action == "copy":
            # B05 audit: copy NEVER sweeps — it never changes the active doc
            # (regression guard bug 1: the shared block swept a copy of the ACTIVE
            # profile and killed the wah over a pure bystander edit).
            try:
                return _js({"ok": True, "path": profile_store.copy_profile(str(id), str(new_id))})
            except (KeyError, ValueError, OSError) as exc:
                return _err(str(exc))
        if action == "rename":
            refusal = profile_store.rename_refusal(str(id), str(new_id))
            if refusal:
                return _err(refusal)  # regression guard bug 1: refuse BEFORE the sweep
            running = [b for b in list(midi_core.BUSES.values())
                       if b.bus_id == _active_bus_id() and b.matrix_status().get("running")] \
                if str(id) == profile_store.get_active_profile_id() else []
            warn = _stop_matrix_if_active(str(id))
            try:
                nid = profile_store.rename_profile(str(id), str(new_id))
            except (KeyError, ValueError, OSError) as exc:
                # A12: the warn-gated rollback never ran after a
                # successful stop — re-arm the buses actually stopped (rename
                # failed -> old id intact, the matrix must come back).
                for b in running:
                    _restart_matrix(b)
                return _err(str(exc))
            return _js({"ok": True, "id": nid,
                        **({"matrix_warning": warn} if warn else {})})
        if action == "import":
            path = profile_store.import_profile(str(json_text or ""), new_id)
            return _js({"ok": True, "path": path})
        if action == "export":
            return _js({"json": profile_store.export_profile(str(id))})
        return _err(f"unknown action {action!r}")
    except Exception as exc:
        return _err(str(exc))


def _matrix_handler(action="status", **_kwargs) -> str:
    """Software matrix control for chat/automation: start/stop/status.
    start reads the ACTIVE profile's edges — same engine the pane drives."""
    try:
        if action == "start":
            pid = profile_store.get_active_profile_id()
            if not pid:
                return _err("no active profile; activate one via midi_profile")
            edges, lfo, cc_of, ch = profile_store.matrix_inputs(
                profile_store.load_profile(pid))
            return _js(_active_bus().matrix_start(edges, lfo, cc_of, ch))
        if action == "stop":
            return _js(_active_bus().matrix_stop())
        if action == "reload":
            # REST /matrix/reload parity (review 2026-11-09): hot-apply edited
            # edges to a RUNNING matrix — stop restores bases, start re-captures.
            bus = _active_bus()
            if not bus.matrix_status().get("running"):
                return _js({"ok": True, "running": False})
            pid = profile_store.get_active_profile_id()
            if not pid:
                return _err("no active profile")
            edges, lfo, cc_of, ch = profile_store.matrix_inputs(
                profile_store.load_profile(pid))
            stop_r = bus.matrix_stop()
            if not stop_r.get("ok") and stop_r.get("error") != "matrix not running":
                return _js({"error": f"reload: stop failed: {stop_r['error']}",
                            "failed": stop_r.get("failed", []), "reloaded": False})
            return _js(bus.matrix_start(edges, lfo, cc_of, ch))
        if action == "status":
            return _js(_active_bus().matrix_status())
        return _err(f"unknown action {action!r}")
    except Exception as exc:
        return _err(str(exc))


def _recorder_handler(action="status", bpm=None, name=None, **_kwargs) -> str:
    """Record-a-ride: start, then stop -> .mid file (path returned). Plus the
    take library: takes / take_get (base64) / take_delete."""
    try:
        rec = _active_bus().recorder
        if action == "start":
            return _js(rec.start())
        if action == "status":
            return _js(rec.status())
        if action == "stop":
            # Deep rinse: pass bpm RAW — Recorder.stop validates first with a
            # named error (the documented contract); float() here pre-empted
            # it with a generic "could not convert" and no named refusal.
            # Review 2026-11-09: default bpm=None (was 120) — the old default
            # made the export-retry path ALWAYS re-tempo the pending take.
            r = rec.stop(120.0 if bpm is None else bpm)
            if not r.get("ok"):
                return _js(r)
            pattern = r.pop("pattern", None)
            try:
                r["path"] = automation.export_mid(pattern, name=str(name or "take"))
            except Exception as exc:
                # #7: a failed export does NOT eat the take — it stays
                # pending; retry with action=export.
                return _js({"error": f"export failed: {exc}", "pending": True})
            rec.clear_pending()
            return _js(r)
        if action == "export":
            # #7: retry export of the pending (stopped, unexported) take.
            pattern = rec.pending()
            if pattern is None:
                return _err("no pending take")
            if bpm is not None:
                try:
                    nb = float(bpm)
                except (TypeError, ValueError):
                    return _err("bpm must be a number")
                # Review 2026-11-09: REST parity — refuse-don't-clamp (the
                # /rec/export twin was fixed in the rinse; this twin still
                # silently folded bpm=5000 into 999).
                if not 10.0 <= nb <= 999.0:
                    return _err("bpm must be 10-999 (refused, not clamped)")
                pattern["bpm"] = nb
            try:
                path = automation.export_mid(pattern, name=str(name or "take"))
            except Exception as exc:
                return _js({"error": f"export failed: {exc}", "pending": True})
            rec.clear_pending()
            return _js({"ok": True, "path": path})
        if action == "discard":
            return _js(rec.discard())
        if action == "takes":
            return _js({"takes": automation.list_takes()})
        if action == "take_get":
            # Review 2026-11-09: the description advertises base64 — return it
            # (REST /rec/take/get parity); a path alone made agents guess.
            p = automation.take_path(str(name or ""))
            return _js({"name": p.name, "size": p.stat().st_size, "path": str(p),
                        "mid_b64": base64.b64encode(
                            automation.read_take(str(name or ""))).decode()})
        if action == "take_delete":
            return _js({"ok": automation.delete_take(str(name or ""))})
        return _err(f"unknown action {action!r}")
    except Exception as exc:
        return _err(str(exc))


def _control_handler(control=None, value=None, **_kwargs) -> str:
    """Named control set: resolve a control id/alias against the ACTIVE profile
    and send on its cc + profile channel. The 'set Cutoff to 40' tool."""
    try:
        name = str(control or "").strip()
        if not name:
            return _err("control required (id or alias)")
        pid = profile_store.get_active_profile_id()
        if not pid:
            return _err("no active profile; activate one via midi_profile")
        prof = profile_store.load_profile(pid)
        ctrls = prof.get("controls", {})
        cid = name if name in ctrls else (prof.get("aliases") or {}).get(name)
        if cid not in ctrls:
            return _err(f"no control {name!r} in active profile "
                        f"{prof.get('profile_id')}; see midi_profile get")
        c = ctrls[cid]
        if c.get("widget") in ("label", "analyzer"):
            return _err(f"control {cid} is a {c.get('widget')} (no wire)")  # analyzer gate (review 2026-11-09): used to fall through to c['cc'] KeyError -> bare "'cc'"
        if c.get("input_only"):
            # #14: input_only is a declared wire direction — writing it from
            # chat would contradict the profile's own claim.
            return _err(f"control {cid} is input_only (device sends it; we do not write it)")
        v = 127 if value is None else value
        if isinstance(v, str) and v.strip().endswith("%"):
            pct = max(0.0, min(100.0, float(v.strip()[:-1])))
            v = round(pct * 127 / 100)
        else:
            v = int(v)
        # #14: the control's declared range is enforced centrally, not just
        # decorative — clamp (and say so) instead of silently sending outside.
        lo, hi = int(c.get("min", 0)), int(c.get("max", 127))
        clamped = max(lo, min(hi, v))
        if clamped != v:
            r_extra = {"clamped_from": v}
            v = clamped
        else:
            r_extra = {}
        r = _active_bus().send("cc", channel=int(prof.get("channel", 0)),
                               cc=c["cc"], value=v, source="agent")
        r.update({"control": cid, "cc": c["cc"],
                  "channel": int(prof.get("channel", 0)), "value": v, **r_extra})
        return _js(r)
    except Exception as exc:  # never raise into the agent loop
        return _err(str(exc))


def register(ctx) -> None:
    try:
        # sendlog is declared in plugin.yaml config_schema — wire it (regression review).
        midi_core.BUS.sendlog_on = bool(ctx.get_config("sendlog", True))
    except Exception:
        pass
    ctx.register_tool(
        name="midi_devices", toolset=TOOLSET, emoji="🎛️",
        description="MIDI session: list visible devices; connect (name substring) / disconnect / panic (all notes off).",
        schema={
            "name": "midi_devices",
            "description": "List, connect, disconnect or panic the MIDI session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "connect", "disconnect", "panic"],
                               "description": "Session action (default list)."},
                    "name": {"type": "string", "description": "Device name substring (action=connect)."},
                    "bus": {"type": "string", "description": "Device slot / bus id (default 'primary'; one exclusive port pair per bus — use a distinct bus per physical device)."},
                },
            },
        },
        handler=_devices_handler,
    )
    ctx.register_tool(
        name="midi_status", toolset=TOOLSET, emoji="🎛️",
        description="MIDI surface state: connected device, live CC values, active notes, recent events.",
        schema={
            "name": "midi_status",
            "description": "Show the MIDI surface backend state (connection, CC state, notes).",
            "parameters": {"type": "object", "properties": {
                "sendlog": {"type": "boolean", "description": "include the tail of the wire audit log (REST /sendlog parity, default false)"},
                "limit": {"type": "integer", "description": "sendlog rows to return (default 50)"},
            }},
        },
        handler=_status_handler,
    )
    ctx.register_tool(
        name="midi_send", toolset=TOOLSET, emoji="🎛️",
        description="Send a MIDI message to the connected device: cc (cc+value), note_on/note_off (note, velocity), or pc (value).",
        schema={
            "name": "midi_send",
            "description": "Send MIDI to the connected device.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["cc", "note_on", "note_off", "pc"],
                             "description": "Message kind (default cc)."},
                    "cc": {"type": "integer", "description": "CC number 0-127 (kind=cc)."},
                    "value": {"type": "integer", "description": "CC value 0-127, or program number (kind=pc)."},
                    "note": {"type": "integer", "description": "Note number 0-127 (note_on/note_off)."},
                    "velocity": {"type": "integer", "description": "Velocity 1-127 (note_on)."},
                    "channel": {"type": "integer", "description": "MIDI channel 0-15 (default 0)."},
                    "bus": {"type": "string", "description": "Device slot / bus id (default 'primary')."},
                },
            },
        },
        handler=_send_handler,
    )
    ctx.register_tool(
        name="midi_control", toolset=TOOLSET, emoji="🎛️",
        description="Set a NAMED control on the active device profile (control='cutoff', value=64 or '50%'); resolved against the active profile's cc and channel.",
        schema={
            "name": "midi_control",
            "description": "Drive a named synth parameter from the active profile.",
            "parameters": {
                "type": "object",
                "properties": {
                    "control": {"type": "string", "description": "control id or alias (e.g. cutoff, filter_cutoff)"},
                    "value": {"description": "0-127 integer, or percent string like '40%'"},
                },
                "required": ["control"],
            },
        },
        handler=_control_handler,
    )
    ctx.register_tool(
        name="midi_matrix", toolset=TOOLSET, emoji="🎛️",
        description="Software modulation matrix (LFO/control -> target CC): start/stop/status. start runs the ACTIVE profile's matrix edges.",
        schema={
            "name": "midi_matrix",
            "description": "Run the active profile's software matrix.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["start", "stop", "status", "reload"],
                               "description": "Matrix action (default status)."},
                },
            },
        },
        handler=_matrix_handler,
    )
    ctx.register_tool(
        name="midi_recorder", toolset=TOOLSET, emoji="🎛️",
        description="Record-a-ride: start captures CC rides + notes, stop exports a .mid file (optional name; path returned; import into any DAW). takes/take_get/take_delete manage the saved take library.",
        schema={
            "name": "midi_recorder",
            "description": "Record MIDI, export .mid, manage saved takes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["start", "stop", "status", "export", "discard", "takes", "take_get", "take_delete"],
                               "description": "Recorder action (default status). export retries the pending unexported take."},
                    "bpm": {"type": "number", "description": "Tempo for the exported file (default 120)."},
                    "name": {"type": "string", "description": "Take name for stop (filename tag) or target take for take_get/take_delete."},
                },
            },
        },
        handler=_recorder_handler,
    )
    ctx.register_tool(
        name="midi_profile", toolset=TOOLSET, emoji="🎛️",
        description="Device profile store: list/get/activate/save/delete/import/export JSON device profiles (controls, cc bindings, layout).",
        schema={
            "name": "midi_profile",
            "description": "Manage device profiles (the source of truth for what the surface shows and controls).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["list", "get", "activate", "save", "delete", "copy", "rename", "import", "export",
                                        "deck", "deck_set"],
                               "description": "Store action (default list). copy duplicates a profile (either tier source -> user tier), rename moves a user profile to a new id (deck/active follow). deck = on-screen profile list (active first); deck_set replaces it (ids)."},
                    "id": {"type": "string", "description": "profile_id for get/activate/delete/copy (source)/rename (current)/export."},
                    "profile": {"type": "object", "description": "Full profile object for save."},
                    "json_text": {"type": "string", "description": "Profile JSON text for import."},
                    "new_id": {"type": "string", "description": "Target profile_id for copy/rename; optional new id on import."},
                    "ids": {"type": "array", "items": {"type": "string"},
                            "description": "Profile ids for deck_set (first becomes active)."},
                },
                "required": ["action"],
            },
        },
        handler=_profile_handler,
    )
