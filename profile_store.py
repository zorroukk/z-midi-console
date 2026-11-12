"""Profile store: device profiles as JSON documents.

A profile is the whole truth about one device: which ports match it, which controls
it has (cc, widget, display semantics, layout), and its (software) matrix edges.
The pane renders from it; the backend executes from it. Nothing device-specific is
hard-coded — the MicroFreak is just a bundled profile.

Tiers:
- bundled: profiles/*.json next to this file (read-only, ships with the plugin)
- user:    <LOCALAPPDATA|~/.local/share>/z-midi-console/profiles/*.json
           (editable; overrides a bundled profile with the same profile_id)

Laws (inherited from mf-visualizer registry v2):
- Refuse-closed: a profile that fails validation is NEVER loaded or saved.
- Atomic save: write temp file, then os.replace.
- schema_version on every profile; unknown top-level keys are errors (typo trap).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, TypeGuard

SCHEMA_VERSION = 1

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

WIDGETS = {"knob", "slider", "toggle", "momentary", "stepped", "label", "analyzer"}
KINDS = {"continuous", "toggle", "momentary", "stepped", "enum"}
DOMAINS = {"percent", "raw", "enum"}
RISKS = {"low", "medium", "high"}
MODES = {"add", "mul", "set"}
try:  # package context (agent half)
    from .modmath import CURVES as _CURVES, LFO_WAVES as _LFO_WAVES
except ImportError:  # standalone (dashboard half / tests)
    from modmath import CURVES as _CURVES, LFO_WAVES as _LFO_WAVES
CURVES = set(_CURVES)          # single source: modmath (no hand-mirroring)
LFO_WAVES = set(_LFO_WAVES)

_TOP_KEYS = {"schema_version", "profile_id", "device", "firmware_basis", "evidence",
             "scope", "match", "channel", "controls", "sections", "matrix", "lfo",
             "aliases", "notes", "bus", "keyboard"}
# Deck feature (2026-11-09): optional 'bus' binds a profile to one Bus instance
# (one device pair). Additive + optional: pre-deck profiles stay valid and
# default to 'primary' — no SCHEMA_VERSION bump for an optional key.
_BUS_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CTRL_KEYS = {"label", "cc", "widget", "kind", "min", "max", "display", "labels",
              "echo", "input_only", "risk", "x", "y", "w", "h", "section", "note",
              "automation_note", "color", "name_color", "box_color",
              # Wire remap (LMMS controller-mapping idea, rewritten): 'invert'
              # flips the surface direction, out_min/out_max map the surface
              # 0..127 onto a sub-range of the wire (dead zones, bipolar
              # params). Applied at the Bus boundary: shadow state, matrix and
              # pane all speak the SURFACE domain; only the wire carries the
              # remapped value.
              "invert", "out_min", "out_max",
              # Analyzer widget (owner ask 2026-11-09): spectrum boxes live IN
              # the profile like knobs — draggable, category-assignable. 'source'
              # names what it listens to, 'type' the visual style (pane-owned
              # vocabulary; backend only demands non-empty strings).
              "source", "type"}
# Full-spectrum law (owner ruling): colors are #rrggbb hex, not a small token
# set — the pane's hue wheel produces any hue. Hex is theme-safe, diff-readable,
# and needs no shared palette table between halves.
_HEX = re.compile(r"#[0-9a-fA-F]{6}")
_SECTION_KEYS = {"label", "order", "row", "color", "bg", "font_size", "bold"}
_DISPLAY_KEYS = {"domain", "curve"}
_EDGE_KEYS = {"source", "target", "depth", "curve", "mode", "slew",
              "wave", "rate_hz",
              # Edge kinds (LMMS-inspired, rewritten MIT-clean): 'lfo' is the
              # historical default; 'move' = one-shot envelope; 'when'/'watch'
              # = triggers whose `then` list holds move/lfo edges.
              "kind", "from", "to", "duration_ms", "hold", "crosses", "level",
              "once", "then", "center", "amp", "phase"}
_EDGE_KINDS = {"lfo", "move", "when", "watch"}
_CROSSES = {"up", "down"}

# Deep rinse: bool is a subclass of int, and JSON true/false used to slip into
# every numeric slot as 1/0 (channel:true -> 1, cc:true -> CC 1). The row check
# already excluded bools — the law was simply never propagated. One helper now.
def _is_int(v: Any) -> TypeGuard[int]:
    # TypeGuard (2026-11-10): the `if not (_is_int(x) and 0 <= x <= 127)`
    # idiom is everywhere; a plain bool return left every range check
    # un-narrowed for the type checker (14 phantom errors).
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> TypeGuard[float]:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# Windows refuses files named con/nul/aux/com1... — a valid slug the user tier
# cannot store. Refuse at validation (refuse-closed law), not at os.replace.
_WIN_RESERVED = ({"con", "prn", "aux", "nul"}
                 | {f"com{i}" for i in range(1, 10)}
                 | {f"lpt{i}" for i in range(1, 10)})


def hermes_home() -> Path:
    """The ACTIVE PROFILE's Hermes home (catalog review, PR 153102).

    The store used to hang off %LOCALAPPDATA% directly, which put every profile
    of every Hermes install on ONE shared folder. Hermes' own convention is
    ``<hermes home>/plugin-data/<plugin>/``; resolving the home per call keeps
    it following the active profile (``plugin_data_dir`` resolves fresh for the
    same reason). Standalone test runs have no Hermes on sys.path, so the
    fallback mirrors ``hermes_constants.get_hermes_home``: HERMES_HOME env, then
    %LOCALAPPDATA%/hermes on Windows, then ~/.hermes.
    """
    try:
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found]
        return Path(get_hermes_home())
    except Exception:  # noqa: BLE001 — no Hermes on the path (standalone battery)
        pass
    env = str(os.environ.get("HERMES_HOME") or "").strip()
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        local = str(os.environ.get("LOCALAPPDATA") or "").strip()
        if local:
            return Path(local) / "hermes"
    return Path.home() / ".hermes"


def _legacy_data_dir() -> Path:
    """The pre-review home: %LOCALAPPDATA%/z-midi-console (no profile scope)."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "z-midi-console"


_MIGRATED = False


def _migrate_legacy(root: Path) -> None:
    """Copy the old shared store into the profile home ONCE, never deleting it.

    Copy-not-move on purpose: the old tree stays so a rollback (or a second
    Hermes profile that has not migrated yet) cannot lose a device profile.
    Only runs when the new home has nothing of its own.
    """
    global _MIGRATED
    if _MIGRATED:
        return
    _MIGRATED = True
    try:
        old = _legacy_data_dir()
        if not old.is_dir() or old == root:
            return
        if any(root.iterdir()):
            return
        import shutil
        for entry in old.iterdir():
            dest = root / entry.name
            if dest.exists():
                continue
            if entry.is_dir():
                shutil.copytree(entry, dest)
            else:
                shutil.copy2(entry, dest)
    except OSError:
        pass  # a failed migration degrades to an empty store, never a crash


def data_dir() -> Path:
    """Durable store: ``<hermes home>/plugin-data/z-midi-console/``.

    Prefers Hermes' sanctioned ``plugins.plugin_storage.plugin_data_dir`` and
    computes the same layout itself when that module is not importable.
    """
    try:
        from plugins.plugin_storage import plugin_data_dir  # type: ignore[import-not-found]
        root = Path(plugin_data_dir("z-midi-console"))
    except Exception:  # noqa: BLE001 — standalone: same layout, computed locally
        root = hermes_home() / "plugin-data" / "z-midi-console"
        root.mkdir(parents=True, exist_ok=True)
    _migrate_legacy(root)
    return root


def user_profiles_dir() -> Path:
    return data_dir() / "profiles"


def bundled_profiles_dir() -> Path:
    return Path(__file__).resolve().parent / "profiles"


def validate_profile(p: Any) -> list[str]:
    """Return a list of errors; empty list means the profile is valid."""
    errs: list[str] = []
    if not isinstance(p, dict):
        return ["profile must be an object"]
    for k in sorted(set(p) - _TOP_KEYS):
        errs.append(f"unknown top-level key: {k!r}")
    if p.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"schema_version must be {SCHEMA_VERSION}")
    pid = p.get("profile_id")
    if not (isinstance(pid, str) and _SLUG.match(pid)):
        errs.append("profile_id must be a lowercase slug")
    elif pid in _WIN_RESERVED:
        errs.append("profile_id is a Windows reserved device name")
    if not (isinstance(p.get("device"), str) and p["device"].strip()):
        errs.append("device must be a non-empty string")
    ch = p.get("channel", 0)
    if not (_is_int(ch) and 0 <= ch <= 15):
        errs.append("channel must be int 0-15")
    match = p.get("match")
    if match is not None:
        if not isinstance(match, dict) or set(match) - {"name_contains"} \
                or not isinstance(match.get("name_contains"), str):
            errs.append("match keys must be [name_contains] (string)")
    bus = p.get("bus")
    if bus is not None and not (isinstance(bus, str) and _BUS_SLUG.match(bus)):
        errs.append("bus must be a lowercase slug (or omitted for 'primary')")
    # Piano law (owner ask 2026-11-09): a profile may declare its synth's
    # keyboard so the on-screen piano matches the real device key-for-key.
    kb = p.get("keyboard")
    if kb is not None:
        if (not isinstance(kb, dict) or set(kb) - {"keys", "first"}
                or not (_is_int(kb.get("keys")) and 12 <= kb["keys"] <= 88)
                or not (_is_int(kb.get("first")) and 0 <= kb["first"] <= 108)
                # Wire law (review 2026-11-09): notes ride the 0-127 wire — a
                # span that runs past 127 renders permanently-dead keys.
                or kb["first"] + kb["keys"] > 128):
            errs.append("keyboard must be {keys: int 12-88, first: int 0-108, first+keys <= 128} or omitted")

    sections = p.get("sections", {})
    if not isinstance(sections, dict):
        errs.append("sections must be an object")
        sections = {}
    # B2 (2026-11-10): the renderer keeps a never-hide
    # defence, but collisions must never be SAVED — two categories on one
    # band/slot visually overlay, two control footprints on one cell make
    # the covered control vanish from the surface.
    sec_spots: dict[Any, str] = {}
    for sid, sec in sections.items():
        if not _SLUG.match(sid):
            errs.append(f"bad section id: {sid!r}")
        if not (isinstance(sec, dict) and isinstance(sec.get("label"), str)):
            errs.append(f"section {sid!r} needs a label")
            continue
        for k in sorted(set(sec) - _SECTION_KEYS):
            errs.append(f"section {sid!r}: unknown key {k!r}")
        scol = sec.get("color")
        if scol is not None and (not isinstance(scol, str) or not _HEX.fullmatch(scol)):
            errs.append(f"section {sid!r}: color must be '#rrggbb' hex")
        sbg = sec.get("bg")
        if sbg is not None and (not isinstance(sbg, str) or not _HEX.fullmatch(sbg)):
            errs.append(f"section {sid!r}: bg must be '#rrggbb' hex")
        fs = sec.get("font_size")
        if fs is not None and (not _is_int(fs) or not 8 <= fs <= 48):
            errs.append(f"section {sid!r}: font_size must be int 8-48")
        if sec.get("bold") is not None and not isinstance(sec.get("bold"), bool):
            errs.append(f"section {sid!r}: bold must be a boolean")
        order = sec.get("order")
        if order is not None and (not _is_int(order) or not 0 <= order <= 99):
            errs.append(f"section {sid!r}: order must be int 0-99")
        row = sec.get("row")
        if row is not None and (isinstance(row, bool) or not isinstance(row, int) or not 0 <= row <= 99):
            errs.append(f"section {sid!r}: row must be int 0-99")
        if _is_int(row if row is not None else 0) and _is_int(sec.get("order", 0)):
            rk2 = (row if row is not None else 0, sec.get("order", 0))
            prev_sid = sec_spots.get(rk2)
            if prev_sid is not None:
                errs.append(f"section {sid!r} sits on the same band/slot as {prev_sid!r}")
            else:
                sec_spots[rk2] = sid

    ctrls = p.get("controls")
    if not isinstance(ctrls, dict):
        errs.append("controls must be an object")
        ctrls = {}
    seen_cc: dict[int, str] = {}
    for cid, c in ctrls.items():
        if not _SLUG.match(cid):
            errs.append(f"bad control id: {cid!r}")
            continue
        if not isinstance(c, dict):
            errs.append(f"control {cid!r} must be an object")
            continue
        for k in sorted(set(c) - _CTRL_KEYS):
            errs.append(f"control {cid!r}: unknown key {k!r}")
        if not (isinstance(c.get("label"), str) and c["label"].strip()):
            errs.append(f"control {cid!r}: label required")
        widget = c.get("widget", "knob")
        if not isinstance(widget, str) or widget not in WIDGETS:
            errs.append(f"control {cid!r}: widget must be one of {sorted(WIDGETS)}")
        kind = c.get("kind", "continuous")
        if not isinstance(kind, str) or kind not in KINDS:
            errs.append(f"control {cid!r}: kind must be one of {sorted(KINDS)}")
        col = c.get("color")
        if col is not None and (not isinstance(col, str) or not _HEX.fullmatch(col)):
            errs.append(f"control {cid!r}: color must be '#rrggbb' hex")
        for key in ("name_color", "box_color"):
            v = c.get(key)
            if v is not None and (not isinstance(v, str) or not _HEX.fullmatch(v)):
                errs.append(f"control {cid!r}: {key} must be '#rrggbb' hex")
        cc = c.get("cc")
        if widget not in ("label", "analyzer"):
            if not (_is_int(cc) and 0 <= cc <= 127):
                errs.append(f"control {cid!r}: cc must be int 0-127")
            elif cc in seen_cc:
                errs.append(f"control {cid!r}: cc {cc} already used by {seen_cc[cc]!r}")
            else:
                seen_cc[cc] = cid
        # Analyzer span law: 'w' is the grid-cell width (analyzer boxes are
        # 4 wide); refuse-closed on junk so a typo can't render a monster.
        w = c.get("w", 1)
        if not (_is_int(w) and 1 <= w <= 8):
            errs.append(f"control {cid!r}: w must be int 1-8")
        if widget == "analyzer":
            for key in ("source", "type"):
                v = c.get(key)
                if not isinstance(v, str) or not v.strip():
                    errs.append(f"control {cid!r}: analyzer {key} must be a non-empty string")
        mn, mx = c.get("min", 0), c.get("max", 127)
        # #14: bounded to the wire domain — min:-200/max:999 used to pass and
        # then mean nothing at the wire.
        if not (_is_int(mn) and _is_int(mx)
                and 0 <= mn < mx <= 127):
            errs.append(f"control {cid!r}: need ints 0 <= min < max <= 127")
        if "invert" in c and not isinstance(c["invert"], bool):
            errs.append(f"control {cid!r}: invert must be a bool")
        om, ox = c.get("out_min"), c.get("out_max")
        if om is not None or ox is not None:
            if not (_is_int(om) and _is_int(ox) and 0 <= om < ox <= 127):
                errs.append(f"control {cid!r}: out_min/out_max must be ints 0 <= out_min < out_max <= 127")
        disp = c.get("display")
        if disp is not None:
            if not isinstance(disp, dict) or set(disp) - _DISPLAY_KEYS:
                errs.append(f"control {cid!r}: display keys must be {sorted(_DISPLAY_KEYS)}")
            else:
                # #14 totality: type-check BEFORE membership (an unhashable
                # JSON value like domain:[] used to CRASH the validator with a
                # TypeError instead of returning a named error — and crashed
                # profile listing with it).
                dom = disp.get("domain", "percent")
                if not isinstance(dom, str) or dom not in DOMAINS:
                    errs.append(f"control {cid!r}: display.domain invalid")
                crv = disp.get("curve", "lin")
                if not isinstance(crv, str) or crv not in CURVES:
                    errs.append(f"control {cid!r}: display.curve invalid")
        labels = c.get("labels")
        if labels is not None:
            if (not isinstance(labels, dict)
                    or not all(isinstance(k, str) and isinstance(v, str)
                               for k, v in labels.items())):
                errs.append(f"control {cid!r}: labels must map values to strings")
            else:
                # #14: stepped keys must be real positions inside the control's
                # range — {"oops":"Oops"} used to validate and render nothing.
                for k in labels:
                    try:
                        kv = int(k)
                    except ValueError:
                        errs.append(f"control {cid!r}: label key {k!r} must be a "
                                    "numeric CC value inside min..max")
                        continue
                    if not (mn <= kv <= mx):
                        errs.append(f"control {cid!r}: label key {kv} outside "
                                    f"min..max ({mn}..{mx})")
        risk = c.get("risk", "low")
        if not isinstance(risk, str) or risk not in RISKS:
            errs.append(f"control {cid!r}: risk must be one of {sorted(RISKS)}")
        for pos in ("x", "y"):
            v = c.get(pos, 0)
            if not (_is_int(v) and 0 <= v <= 99):
                errs.append(f"control {cid!r}: {pos} must be int 0-99")
        # w already bounded 1-8 by the analyzer law (line ~226); h was the
        # unbounded one (B1).
        v = c.get("h", 1)
        if not (_is_int(v) and 1 <= v <= 8):
            errs.append("control {cid!r}: h must be int 1-8".format(cid=cid))
        sec = c.get("section")
        if sec is not None and (not isinstance(sec, str) or sec not in sections):
            errs.append(f"control {cid!r}: section {sec!r} not declared")

    # B2 (2026-11-10): overlapping control footprints are
    # refused at the door. Row semantics match the renderer (one visual slot
    # per y; the cover-set swallows x-spans) — a control can never be saved
    # into a position that would make it invisible.
    placed: dict[Any, list] = {}
    for cid, c in ctrls.items():
        if not isinstance(c, dict):
            continue
        x, y, w = c.get("x", 0), c.get("y", 0), c.get("w", 1)
        if not (_is_int(x) and _is_int(y) and _is_int(w)):
            continue
        s_k = c.get("section") if isinstance(c.get("section"), str) else ""
        placed.setdefault((s_k, y), []).append((x, x + max(1, w) - 1, cid))
    for spot, spans in sorted(placed.items(), key=lambda kv: str(kv[0])):
        for i in range(len(spans)):
            for j in range(i + 1, len(spans)):
                if spans[i][0] <= spans[j][1] and spans[j][0] <= spans[i][1]:
                    errs.append(f"control {spans[i][2]!r} overlaps {spans[j][2]!r} in section {spot[0]!r} row {spot[1]}")

    matrix = p.get("matrix", [])
    if not isinstance(matrix, list):
        errs.append("matrix must be a list")
        matrix = []
    def _check_target(tag, tgt):
        if not isinstance(tgt, str) or tgt not in ctrls:
            errs.append(f"{tag}: target {tgt!r} is not a control")
        elif isinstance(ctrls.get(tgt), dict) \
                and ctrls[tgt].get("widget") in ("label", "analyzer"):
            errs.append(f"{tag}: target {tgt!r} is a {ctrls[tgt].get('widget')} (no wire)")
        elif isinstance(ctrls.get(tgt), dict) and ctrls[tgt].get("input_only"):
            # #14: input_only means "the device sends this; we never write it"
            # — a matrix edge WRITES its target, so the combination is a lie.
            errs.append(f"{tag}: target {tgt!r} is input_only (cannot be written)")

    def _check_continuous(tag, t):
        """Shared numeric checks for lfo/move edges (top level or inside
        a trigger's `then`)."""
        crv = t.get("curve", "lin")
        if not isinstance(crv, str) or crv not in CURVES:
            errs.append(f"{tag}: curve invalid")
        md = t.get("mode", "add")
        if not isinstance(md, str) or md not in MODES:
            errs.append(f"{tag}: mode invalid")
        d = t.get("depth", 1.0)
        if not (_is_num(d) and -2.0 <= d <= 2.0):
            errs.append(f"{tag}: depth must be a number in -2..2")
        sl = t.get("slew", 0.0)
        if not (_is_num(sl) and 0.0 <= sl <= 1.0):
            errs.append(f"{tag}: slew must be a number in 0..1")
        # Multi-matrix law: every LFO lane carries its OWN oscillator. wave/
        # rate_hz on the edge override the profile-level lfo default, so N
        # lanes = N independent modulations — no shared-clock limitation.
        wv = t.get("wave")
        if wv is not None and (not isinstance(wv, str) or wv not in LFO_WAVES):
            errs.append(f"{tag}: wave must be one of {sorted(LFO_WAVES)}")
        rt = t.get("rate_hz")
        if rt is not None and not (_is_num(rt) and 0.01 <= float(rt) <= 10.0):
            errs.append(f"{tag}: rate_hz must be a number in 0.01..10")
        ph = t.get("phase")
        if ph is not None and not (_is_num(ph) and 0.0 <= float(ph) <= 1.0):
            errs.append(f"{tag}: phase must be a number in 0..1 (cycle offset)")
        for key in ("center", "amp"):
            v = t.get(key)
            if v is not None and not (_is_int(v) and 0 <= v <= 127):
                errs.append(f"{tag}: {key} must be an int 0..127")

    def _check_then(tag, then):
        if not isinstance(then, list) or not then:
            errs.append(f"{tag}: then must be a non-empty list of move/lfo edges")
            return
        for j, t in enumerate(then):
            if not isinstance(t, dict) or set(t) - _EDGE_KEYS:
                errs.append(f"{tag}[{j}]: keys must be {sorted(_EDGE_KEYS)}")
                continue
            k = t.get("kind", "move")
            if k not in ("move", "lfo"):
                errs.append(f"{tag}[{j}]: kind {k!r} cannot nest (move/lfo only)")
                continue
            _check_target(f"{tag}[{j}]", t.get("target"))
            _check_continuous(f"{tag}[{j}]", t)
            if k == "move":
                for key in ("from", "to"):
                    v = t.get(key)
                    if not (_is_int(v) and 0 <= v <= 127):
                        errs.append(f"{tag}[{j}]: {key} must be an int 0..127")
                dm = t.get("duration_ms")
                if not (_is_int(dm) and 10 <= dm <= 600000):
                    errs.append(f"{tag}[{j}]: duration_ms must be an int 10..600000")
                if "hold" in t and not isinstance(t["hold"], bool):
                    errs.append(f"{tag}[{j}]: hold must be a bool")
            if k == "lfo" and t.get("source") not in (None, "lfo"):
                errs.append(f"{tag}[{j}]: then-lfo source must be 'lfo'")

    for i, e in enumerate(matrix):
        if not isinstance(e, dict) or set(e) - _EDGE_KEYS:
            errs.append(f"matrix[{i}]: keys must be {sorted(_EDGE_KEYS)}")
            continue
        kind = e.get("kind", "lfo")
        if kind not in _EDGE_KINDS:
            errs.append(f"matrix[{i}]: kind {kind!r} must be one of {sorted(_EDGE_KINDS)}")
            continue
        # #14 totality: str-check before dict membership (a list source used
        # to raise TypeError: unhashable — crashing listing, not refusing).
        src = e.get("source")
        if kind in ("lfo", "when", "watch"):
            allow_lfo = kind != "watch"  # watch senses a CONTROL twist; an LFO always "changes"
            if not isinstance(src, str) or (src not in ctrls and not (allow_lfo and src == "lfo")):
                # Message kept compatible with the old battery: lfo-allowed
                # kinds say "control or 'lfo'"; watch names its exception.
                if allow_lfo:
                    errs.append(f"matrix[{i}]: source {src!r} is not a control or 'lfo'")
                else:
                    errs.append(f"matrix[{i}]: source {src!r} is not a control (watch senses physical twists — an lfo source is meaningless)")
            elif src != "lfo" and isinstance(ctrls.get(src), dict) \
                    and ctrls[src].get("widget") in ("label", "analyzer"):
                errs.append(f"matrix[{i}]: source {src!r} is a {ctrls[src].get('widget')} (no wire)")
        elif kind == "move" and src is not None:
            errs.append("matrix[i]: move edges have no source (they are one-shot envelopes)")
        if kind in ("lfo", "move"):
            _check_target(f"matrix[{i}]", e.get("target"))
            _check_continuous(f"matrix[{i}]", e)
            if kind == "move":
                for key in ("from", "to"):
                    v = e.get(key)
                    if not (_is_int(v) and 0 <= v <= 127):
                        errs.append(f"matrix[{i}]: {key} must be an int 0..127")
                dm = e.get("duration_ms")
                if not (_is_int(dm) and 10 <= dm <= 600000):
                    errs.append(f"matrix[{i}]: duration_ms must be an int 10..600000")
                if "hold" in e and not isinstance(e["hold"], bool):
                    errs.append(f"matrix[{i}]: hold must be a bool")
        else:  # when / watch
            lv = e.get("level", 64)
            if not (_is_int(lv) and 0 <= lv <= 127):
                errs.append(f"matrix[{i}]: level must be an int 0..127")
            cx = e.get("crosses", "up")
            if cx not in _CROSSES:
                errs.append(f"matrix[{i}]: crosses must be up or down")
            if "once" in e and not isinstance(e["once"], bool):
                errs.append(f"matrix[{i}]: once must be a bool")
            _check_then(f"matrix[{i}].then", e.get("then"))
    lfo = p.get("lfo", {"wave": "sine", "rate_hz": 0.5})
    if not isinstance(lfo, dict) or set(lfo) - {"wave", "rate_hz"}:
        errs.append("lfo keys must be [rate_hz, wave]")
    else:
        wv = lfo.get("wave", "sine")
        if not isinstance(wv, str) or wv not in LFO_WAVES:
            errs.append(f"lfo.wave must be one of {sorted(LFO_WAVES)}")
        r = lfo.get("rate_hz", 0.5)
        # #16: the evaluator ticks at 30 Hz — a 40 Hz LFO aliases (30 Hz
        # sampling of a 40 Hz sine reads as 10 Hz). Honest ceiling: 10 Hz.
        if not (_is_num(r) and 0.01 <= r <= 10.0):
            errs.append("lfo.rate_hz must be a number in 0.01..10")
    aliases = p.get("aliases", {})
    if not isinstance(aliases, dict):
        errs.append("aliases must be an object")
    else:
        # #14: an alias to a nonexistent control is a dead wire waiting to
        # happen (midi_control would fail at send time with a vaguer error).
        for a, cid in aliases.items():
            if not isinstance(a, str) or not isinstance(cid, str) or cid not in ctrls:
                errs.append(f"alias {a!r} points at unknown control {cid!r}")
    return errs


def matrix_inputs(p: dict) -> tuple:
    """(edges, lfo, cc_of_cid, channel) — the WIRE view of a profile's matrix.
    Law: labels have no wire; every edge target/source maps to a real CC."""
    ctrls = p.get("controls", {})
    cc_of = {cid: int(c["cc"]) for cid, c in ctrls.items()
             if c.get("cc") is not None and c.get("widget") not in ("label", "analyzer")}  # analyzers have no wire either — a stray cc must not make one (review 2026-11-09)
    return p.get("matrix", []), p.get("lfo"), cc_of, int(p.get("channel", 0))


def _scan(dir_path: Path) -> dict[str, tuple[Path, dict, list[str]]]:
    out: dict[str, tuple[Path, dict, list[str]]] = {}
    if not dir_path.is_dir():
        return out
    for f in sorted(dir_path.glob("*.json")):
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            out[f.stem] = (f, {}, [f"unreadable: {exc}"])
            continue
        errs = validate_profile(p)
        # Deep rinse: a JSON file holding a list/number/string parses fine and
        # validate_profile refuses it — but p.get() used to CRASH the whole
        # scan (list_profiles / /profile/list -> 500) before the refusal could
        # be surfaced. Refuse-closed means SHOW the error row, never crash.
        if not isinstance(p, dict):
            p = {}
        pid = p.get("profile_id")
        if not isinstance(pid, str) or not pid:
            pid = f.stem
        out[pid] = (f, p, errs)
    return out


def list_profiles() -> list[dict]:
    """Meta rows for every profile; user tier overrides bundled by profile_id.
    Invalid files are listed with their errors (visible, never half-loaded)."""
    rows: dict[str, dict] = {}
    # list_profiles used to CRASH (TypeError from len(5)) on a
    # hand-edited profile whose "controls" is not a dict — one malformed file
    # 500'd the whole list instead of showing as an invalid row. Count only
    # real dicts; junk files already surface via valid:false + errors.
    def _ccount(p: dict) -> int:
        c = p.get("controls")
        return len(c) if isinstance(c, dict) else 0
    for pid, (f, p, errs) in _scan(bundled_profiles_dir()).items():
        rows[pid] = {"profile_id": pid, "device": p.get("device", "?"),
                     "tier": "bundled", "valid": not errs, "errors": errs,
                     "controls": _ccount(p)}
    for pid, (f, p, errs) in _scan(user_profiles_dir()).items():
        rows[pid] = {"profile_id": pid, "device": p.get("device", "?"),
                     "tier": "user", "valid": not errs, "errors": errs,
                     "controls": _ccount(p)}
    return sorted(rows.values(), key=lambda r: r["profile_id"])


def load_profile(pid: str) -> dict:
    """Load one profile by id (user overrides bundled). Raises on invalid (refuse-closed)."""
    if not _SLUG.match(str(pid or "")):
        raise ValueError("bad profile_id (slug required)")
    for d in (user_profiles_dir(), bundled_profiles_dir()):
        f = d / f"{pid}.json"
        if f.is_file():
            p = json.loads(f.read_text(encoding="utf-8"))
            errs = validate_profile(p)
            if errs:
                raise ValueError(f"profile {pid!r} invalid: {'; '.join(errs)}")
            # Piano law (2026-11-09): 'keyboard' is a fact about the DEVICE,
            # not the user's layout — a user-tier override that omits it (e.g.
            # one saved before the key existed) inherits it from the bundled
            # profile of the same id, so the on-screen piano still matches
            # the real synth key-for-key.
            if "keyboard" not in p and d == user_profiles_dir():
                bf = bundled_profiles_dir() / f"{pid}.json"
                if bf.is_file():
                    try:
                        # Review 2026-11-09: bundled JSON that parses to a LIST (valid JSON,
                        # not an object) used to raise AttributeError outside the except
                        # net — every later load 500'd. And the inherited kb is now
                        # re-validated: a corrupt bundled keyboard is dropped, not trusted.
                        obj = json.loads(bf.read_text(encoding="utf-8"))
                        kb = obj.get("keyboard") if isinstance(obj, dict) else None
                        if (isinstance(kb, dict) and set(kb) <= {"keys", "first"}
                                and _is_int(kb.get("keys")) and 12 <= kb["keys"] <= 88
                                and _is_int(kb.get("first")) and 0 <= kb["first"] <= 108
                                and kb["first"] + kb["keys"] <= 128):
                            p["keyboard"] = kb
                    except (OSError, ValueError):
                        pass
            return p
    raise KeyError(f"no profile {pid!r}")


def save_profile(p: dict) -> str:
    """Validate (refuse-closed) then atomically write to the user tier. Returns path."""
    errs = validate_profile(p)
    if errs:
        raise ValueError("profile invalid: " + "; ".join(errs))
    d = user_profiles_dir()
    d.mkdir(parents=True, exist_ok=True)
    target = d / f"{p['profile_id']}.json"
    fd, tmp = tempfile.mkstemp(dir=str(d), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(p, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    invalidate_wire_maps()  # wire remap law: a saved profile's transforms take effect now
    return str(target)


def has_user_profile(pid: str) -> bool:
    """regression guard review (2026-11-10): callers that must sweep the matrix
    BEFORE mutating need a refusal that costs nothing and disturbs nothing —
    valid slug + user-tier file on disk. (False for bundled ids by design.)"""
    return bool(_SLUG.match(str(pid or ""))) and (user_profiles_dir() / f"{pid}.json").is_file()


def rename_refusal(src: str, dst: str) -> str | None:
    """Cheap pre-check mirroring rename_profile's refusals — a doomed rename
    must not sweep a running matrix on the way to its error (validate-before-
    transition law, regression guard bug 1 parity). None = would proceed."""
    if not (_SLUG.match(str(src or "")) and _SLUG.match(str(dst or ""))):
        return "bad profile_id (slug required)"
    if src == dst:
        return "rename needs a new id"
    if not (user_profiles_dir() / f"{src}.json").is_file():
        return f"no user profile {src!r} to rename (bundled profiles are read-only — copy instead)"
    if (user_profiles_dir() / f"{dst}.json").is_file() or (bundled_profiles_dir() / f"{dst}.json").is_file():
        return f"profile {dst!r} already exists"
    return None


def delete_profile(pid: str) -> bool:
    """Delete a USER-tier profile. Bundled profiles are read-only (override instead)."""
    if not _SLUG.match(pid):
        raise ValueError("bad profile_id")
    f = user_profiles_dir() / f"{pid}.json"
    if f.is_file():
        # regression review debt sweep: this used to be THREE separate settings.json RMWs
        # (an interleaved /deck or activation was silently lost). ONE
        # transaction now — and it must not re-enter _SETTINGS_LOCK, so the
        # active-law / deck-law / preset-law updates happen in one mutate.
        def _sweep(active, deck, presets):
            act = None if active == pid else active
            dk = [d for d in deck if d != pid]
            if not act and dk:
                act = dk[0]  # deck[0]==active law (deactivate promotes survivor)
            pr = [lay for lay in ([[x for x in lay if x != pid] for lay in presets]) if lay]
            return act, dk, pr
        # regression guard bug 2 (2026-11-10): the settings commit goes FIRST now.
        # The old order (unlink -> commit) meant a locked/failed settings write
        # deleted the file while the deck still pointed at the dead id — every
        # later load KeyError'd. Reversed, the worst failure is a harmless
        # orphan file nothing references (it stays listed until a retry).
        _transact_settings(_sweep)
        invalidate_wire_maps()  # a deleted profile's wire transforms die with it
        try:
            os.unlink(f)
        except OSError:
            pass  # AV lock: the deck is already clean — the orphan is the safe half
        return True
    return False


def copy_profile(src: str, dst: str) -> str:
    """Duplicate a profile (either tier) into the user tier under a new id.
    Refuses if dst exists — a copy never silently overwrites someone's work.
    Copying a bundled profile is the sanctioned way to 'override the factory'."""
    if not (_SLUG.match(str(src or "")) and _SLUG.match(str(dst or ""))):
        raise ValueError("bad profile_id (slug required)")
    if src == dst:
        raise ValueError("copy needs a different id")
    if (user_profiles_dir() / f"{dst}.json").is_file() or (bundled_profiles_dir() / f"{dst}.json").is_file():
        raise ValueError(f"profile {dst!r} already exists")
    p = load_profile(src)
    p["profile_id"] = dst
    return save_profile(p)


def rename_profile(src: str, dst: str) -> str:
    """Rename a USER-tier profile: new file, old removed, deck/active updated
    in place (order preserved). Bundled profiles are factory — copy instead."""
    if not (_SLUG.match(str(src or "")) and _SLUG.match(str(dst or ""))):
        raise ValueError("bad profile_id (slug required)")
    if src == dst:
        raise ValueError("rename needs a new id")
    if not (user_profiles_dir() / f"{src}.json").is_file():
        raise KeyError(f"no user profile {src!r} to rename (bundled profiles are read-only — copy instead)")
    if (user_profiles_dir() / f"{dst}.json").is_file() or (bundled_profiles_dir() / f"{dst}.json").is_file():
        raise ValueError(f"profile {dst!r} already exists")
    p = load_profile(src)
    p["profile_id"] = dst
    save_profile(p)
    # regression guard bugs 2+6 (2026-11-10): ONE settings transaction that maps
    # src->dst through deck AND active AND the historical deck_presets (the
    # old code rewrote only the live deck, leaving saved presets pointing at
    # the dead id — every later 'select preset' refused). The commit happens
    # BEFORE the old file is removed: if the commit fails we roll the new
    # file back and the old id survives intact (no dangling deck, no lost
    # profile). If the unlink fails afterwards the old id is a harmless
    # orphan — nothing references it and a retry renames over it cleanly.
    def _rn(active, deck, presets):
        dk: list[str] = []
        for d in deck:
            x = dst if d == src else d
            if x not in dk:
                dk.append(x)
        act = dst if active == src else active
        pr: list[list[str]] = []
        for lay in presets:
            m: list[str] = []
            for x in lay:
                y = dst if x == src else x
                if y not in m:
                    m.append(y)
            if m and m not in pr:
                pr.append(m)
        return act, dk, pr
    try:
        _transact_settings(_rn)
    except BaseException:
        try:
            os.unlink(str(user_profiles_dir() / f"{dst}.json"))
        except OSError:
            pass
        raise
    invalidate_wire_maps()  # the OLD id's transforms must die with its file
    try:
        os.unlink(str(user_profiles_dir() / f"{src}.json"))
    except OSError:
        pass  # AV lock: references already moved to dst — the ghost is the safe half
    return dst


def import_profile(text: str, new_id: str | None = None) -> str:
    """Parse + validate + save JSON text into the user tier. Returns saved path."""
    p = json.loads(text)
    if new_id:
        p["profile_id"] = new_id
    return save_profile(p)


def export_profile(pid: str) -> str:
    """Raw JSON text of a profile (either tier)."""
    if not _SLUG.match(str(pid or "")):
        raise ValueError("bad profile_id (slug required)")
    for d in (user_profiles_dir(), bundled_profiles_dir()):
        f = d / f"{pid}.json"
        if f.is_file():
            return f.read_text(encoding="utf-8")
    raise KeyError(f"no profile {pid!r}")


def _settings_path() -> Path:
    return data_dir() / "settings.json"


def get_active_profile_id() -> str | None:
    try:
        obj = json.loads(_settings_path().read_text(encoding="utf-8"))
        # Deep rinse: settings.json of the wrong shape (a hand-edited list or
        # string) used to raise AttributeError out of the MOST-called function
        # in the store — /state crash-looped until the file was deleted.
        pid = obj.get("active_profile") if isinstance(obj, dict) else None
        return pid if isinstance(pid, str) else None
    except (OSError, json.JSONDecodeError):
        return None


def set_active_profile_id(pid: str | None) -> None:
    if pid is not None:
        load_profile(pid)  # refuse-closed: can't activate what won't validate
        # Deck law: the deck is the on-screen profile list; the ACTIVE profile is
        # always deck[0] (the one being edited / driving matrix + learn).
        # Activating moves the pid to the front and keeps the rest on screen.
        def _act(active, deck, presets):
            d = [x for x in deck if x != pid]
            return pid, ([pid] + d), _upsert_presets(presets, [pid] + d)
        _transact_settings(_act)
        invalidate_wire_maps()  # deck order IS wire order (regression review bug 1)
        return
    # Deactivate (review 2026-11-09): dropping the marker while deck[0] stayed
    # live broke the deck[0]==active law. Deactivating now drops the front and
    # promotes the next survivor — the law never breaks, the rest stay on screen.
    def _deact(active, deck, presets):
        d = deck[1:]
        return (d[0] if d else None), d, _upsert_presets(presets, d)
    _transact_settings(_deact)
    invalidate_wire_maps()  # deck order IS wire order (regression review bug 1)


def get_deck() -> list[str]:
    """On-screen profile ids, active first. Tolerant of junk entries."""
    try:
        obj = json.loads(_settings_path().read_text(encoding="utf-8"))
        raw = obj.get("deck") if isinstance(obj, dict) else None
        if not isinstance(raw, list):
            ap = get_active_profile_id()
            return [ap] if ap else []
        return [d for d in raw if isinstance(d, str) and d]
    except (OSError, json.JSONDecodeError):
        return []


def set_deck(ids: list) -> list[str]:
    """Replace the deck (refuse-closed: every id must load). Returns the deck."""
    out: list[str] = []
    for d in ids:
        # refuse-don't-mask — type junk used to be
        # silently SKIPPED while unknown strings refused: ok:true with a
        # shorter deck than asked. Refuse it by name instead.
        if not (isinstance(d, str) and d):
            raise ValueError(f"deck id must be a non-empty string, got {type(d).__name__}")
        if d in out:
            continue  # duplicate ids collapse (idempotent, not junk)
        load_profile(d)  # raises KeyError/ValueError for junk — caller refuses
        out.append(d)
    _write_settings(out[0] if out else None, out)
    invalidate_wire_maps()  # deck order IS wire order (regression review bug 1)
    return out


def _read_presets() -> list[list[str]]:
    """Saved deck layouts from settings.json; tolerant of junk."""
    try:
        obj = json.loads(_settings_path().read_text(encoding="utf-8"))
        raw = obj.get("deck_presets") if isinstance(obj, dict) else None
        if not isinstance(raw, list):
            return []
        out: list[list[str]] = []
        for p in raw:
            if isinstance(p, list) and p and all(isinstance(d, str) and d for d in p) and p not in out:
                out.append(p)
        return out
    except (OSError, json.JSONDecodeError):
        return []


def get_deck_presets() -> list[list[str]]:
    """Saved deck layouts, oldest first. The live deck is always among them
    (upserted on every deck write) so the pane's 'select preset' menu can
    offer whole-screen layouts, not just single devices."""
    presets = _read_presets()
    deck = get_deck()
    if deck and deck not in presets:
        presets = (presets + [deck])[-12:]
    return presets


_SETTINGS_LOCK = threading.Lock()  # settings.json read-modify-write guard (review 2026-11-09): concurrent /deck + /profile/activate used to last-writer-wins over stale reads


def _dump_settings(active: str | None, deck: list[str], presets: list[list[str]]) -> None:
    """Atomic tempfile+os.replace write (the only writer of settings.json)."""
    _settings_path().parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(_settings_path().parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"active_profile": active, "deck": deck, "deck_presets": presets}, f)
        os.replace(tmp, _settings_path())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_settings_obj() -> dict:
    """Raw settings.json as a (active, deck, presets) dict; tolerant of junk."""
    try:
        obj = json.loads(_settings_path().read_text(encoding="utf-8"))
        if not isinstance(obj, dict):
            obj = {}
    except (OSError, json.JSONDecodeError):
        obj = {}
    raw_presets = obj.get("deck_presets")
    return {"active_profile": obj.get("active_profile"),
            "deck": obj.get("deck") if isinstance(obj.get("deck"), list) else [],
            # same junk-tolerance as _read_presets: a hand-edited settings.json
            # must never make a sweep iterate an int (regression review debt sweep)
            "deck_presets": [p for p in raw_presets
                             if isinstance(p, list) and p and all(isinstance(x, str) and x for x in p)]
                            if isinstance(raw_presets, list) else []}


def _transact_settings(mutate) -> None:
    """ONE settings.json read-modify-write transaction under _SETTINGS_LOCK
    (regression review debt sweep: delete_profile used to run 3 separate RMWs — an
    interleaved /deck or activation was silently lost). `mutate(active, deck,
    presets) -> (active, deck, presets)`. Callers must NOT hold the lock."""
    with _SETTINGS_LOCK:
        obj = _read_settings_obj()
        active, deck, presets = mutate(obj["active_profile"], obj["deck"], obj["deck_presets"])
        _dump_settings(active, deck, presets)


def _write_settings(active: str | None, deck: list[str]) -> None:
    # Deck presets (owner ask 2026-11-09): every deck layout is remembered so
    # 'select preset' can restore a whole screen. Upsert the current deck
    # (dedupe by exact id order), keep the 12 newest.
    with _SETTINGS_LOCK:  # the presets read + write is ONE transaction
        presets = _upsert_presets(_read_presets(), deck)
        _dump_settings(active, deck, presets)


def _upsert_presets(presets: list, deck: list[str]) -> list:
    """Deck-presets law (owner ask 2026-11-09): remember every deck layout,
    dedupe by exact id order, keep the 12 newest."""
    out = [p for p in presets if p != deck]
    if deck:
        out = (out + [deck])[-12:]
    return out


def control_cc_map(pid: str | None = None) -> dict[int, str]:
    """cc -> control id for the active (or named) profile; {} if none/invalid.
    Labels have no wire (matrix_inputs law) — deep rinse: this second wire
    view used to disagree, so a label's phantom cc made real incoming CCs
    un-bindable (passive-learn saw them as already mapped)."""
    pid = pid or get_active_profile_id()
    if not pid:
        return {}
    try:
        p = load_profile(pid)
    except (KeyError, ValueError):
        return {}
    return {c["cc"]: cid for cid, c in p.get("controls", {}).items()
            if _is_int(c.get("cc")) and c.get("widget") not in ("label", "analyzer")}  # same wire law as matrix_inputs — analyzers excluded too


# ------------------------------------------------------- wire remap maps ----
# LMMS controller-mapping idea, rewritten: a control may declare invert and
# out_min/out_max. The transform lives at the WIRE BOUNDARY (Bus.send /
# Bus._on_data): shadow state, the matrix engine and the pane all speak the
# SURFACE domain; only the physical wire carries the remapped value. Keyed by
# bus id (deck rig: each device bus gets its own map); cc collisions across
# profiles on one bus resolve first-wins with a named refusal at validation
# only if they contradict — rare enough that first-wins + stable sort is fine.
_WIRE_CACHE: dict = {}
_WIRE_GEN = [0]  # bumped on every invalidate; a stale sweep must not re-stale (regression guard)


def invalidate_wire_maps() -> None:
    _WIRE_GEN[0] += 1  # a mutation racing a slow sweep must stick
    _WIRE_CACHE.clear()


def wire_maps() -> dict[str, dict[int, tuple]]:
    """bus id -> {cc: (invert, out_min, out_max)} across the DECK — the
    profiles actually driving buses — ACTIVE FIRST, FIRST-WINS.
    (regression review bug 1, 2026-11-10: the old merge walked list_profiles() in
    alphabetical order and let a LATER profile overwrite the ACTIVE one's
    transform for a shared CC — an inactive bystander corrupted the live
    wire. The wire belongs to the deck; deck[0] is the profile being
    edited/driven, so its transforms win. Profiles outside the deck drive
    nothing and never touch the wire.)
    Cached until any profile mutation calls invalidate_wire_maps()."""
    if _WIRE_CACHE:
        return _WIRE_CACHE["m"]
    gen = _WIRE_GEN[0]
    m: dict[str, dict[int, tuple]] = {}
    rows = {r["profile_id"]: r for r in list_profiles() if r["valid"]}
    deck = get_deck()  # active first (deck law)
    seq = [rows[d] for d in deck if d in rows]
    for row in seq:
        try:
            p = load_profile(row["profile_id"])
        except (KeyError, ValueError):
            continue
        bus = p.get("bus") or "primary"
        for c in p.get("controls", {}).values():
            cc = c.get("cc")
            if not _is_int(cc) or c.get("widget") in ("label", "analyzer"):
                continue
            inv = bool(c.get("invert"))
            om, ox = c.get("out_min"), c.get("out_max")
            if not inv and om is None and ox is None:
                continue  # no transform — the common case stays a dict miss
            m.setdefault(bus, {}).setdefault(cc, (inv, om or 0, ox or 127))  # first-wins: active profile beats deck bystanders
    # if a save/delete/rename invalidated the cache
    # DURING this disk sweep, our snapshot is stale — don't re-stale it.
    if _WIRE_GEN[0] == gen:
        _WIRE_CACHE["m"] = m
    return m


def wire_xform(bus: str, cc: int, v: int) -> int:
    """surface 0..127 -> wire 0..127 (identity when the cc has no transform)."""
    t = wire_maps().get(bus, {}).get(cc)
    if t is None:
        return v
    inv, lo, hi = t
    s = v / 127.0
    if inv:
        s = 1.0 - s
    return max(0, min(127, int(round(lo + s * (hi - lo)))))


def unwire_xform(bus: str, cc: int, v: int) -> int:
    """wire 0..127 -> surface 0..127 (inverse of wire_xform)."""
    t = wire_maps().get(bus, {}).get(cc)
    if t is None:
        return v
    inv, lo, hi = t
    s = (v - lo) / float(hi - lo) if hi > lo else 0.0
    s = max(0.0, min(1.0, s))
    if inv:
        s = 1.0 - s
    return max(0, min(127, int(round(s * 127))))
