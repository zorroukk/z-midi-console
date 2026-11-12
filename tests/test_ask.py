"""Chat box battery: pins the /ask proposal gate (test_ask.py, standalone).

The chat box law: the LLM only PROPOSES. Everything real is decided HERE —
JSON extraction must survive fences/prose, the op whitelist must drop anything
invented, ranges must match the profile validator's own bounds, and /ask must
refuse a non-local model lane BEFORE any request leaves the process.
"""
import asyncio
import builtins
import json
import os
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "dashboard"))

_TMP = tempfile.mkdtemp(prefix="mssc-ask-")
os.environ["LOCALAPPDATA"] = _TMP
os.environ["HERMES_HOME"] = _TMP  # data_dir() is profile-scoped now (PR 153102): pin the home too

import chat_engine  # noqa: E402
import plugin_api  # noqa: E402
import profile_store  # noqa: E402

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


def req(body):
    class Req:
        async def json(self):
            return body
    return Req()


SUMMARY = {
    "profile_id": "t", "device": "Test", "channel": 0,
    "sections": [{"sid": "osc", "label": "Oscillator", "row": 0, "order": 0, "controls": 4}],
    "controls": [{"id": "cutoff", "label": "Cutoff", "cc": 23, "widget": "knob", "section": "osc", "invert": False}],
    "matrix": [], "matrix_running": False,
}


def test_extract():
    assert plugin_api._extract_json('{"say":"x","ops":[]}') == {"say": "x", "ops": []}
    assert plugin_api._extract_json('```json\n{"say":"x","ops":[]}\n```') == {"say": "x", "ops": []}
    assert plugin_api._extract_json('Sure! {"say":"done","ops":[{"op":"matrix","action":"start"}]} hope that helps')["ops"][0]["action"] == "start"
    assert plugin_api._extract_json('no json here') is None
    assert plugin_api._extract_json('["array","not","object"]') is None
    assert plugin_api._extract_json('{"broken":') is None
    # prose braces and multiple objects used to be joined by the
    # first-{ / last-} slice, hiding a later valid proposal.
    want = {"say": "done", "ops": []}
    assert plugin_api._extract_json('Use {a label} here, then ' + json.dumps(want)) == want
    assert plugin_api._extract_json('{"note":"first"}\n' + json.dumps(want)) == {"note": "first"}
    assert plugin_api._extract_json('```json\n' + json.dumps(want) + '\n``` trailing {noise}') == want
    assert plugin_api._extract_json('{"say":"a } brace and \\\"quote\\\"","ops":[]}') == {"say": 'a } brace and "quote"', "ops": []}


def test_mirrors():
    # mirror law: the chat whitelist must not drift from the validator's law
    assert plugin_api._EDGE_KINDS_CHAT == profile_store._EDGE_KINDS
    assert plugin_api._CURVES_CHAT == profile_store.CURVES
    assert plugin_api._WAVES_CHAT == profile_store.LFO_WAVES


def test_sanitize_ops():
    ok = plugin_api._sanitize_one({"op": "color", "target": "section:Oscillator", "hex": "#AB12cd", "junk": 1})
    assert ok == {"op": "color", "target": "section:Oscillator", "hex": "#ab12cd"}, ok
    assert plugin_api._sanitize_one({"op": "color", "target": "control:Cutoff", "hex": "purple"}) is None
    assert plugin_api._sanitize_one({"op": "color", "target": "", "hex": "#ffffff"}) is None
    assert plugin_api._sanitize_one({"op": "clear_color", "target": "control:Cutoff"}) == {"op": "clear_color", "target": "control:Cutoff"}
    assert plugin_api._sanitize_one({"op": "rename", "kind": "section", "from": "Osc", "to": "OSC 1"}) is not None
    assert plugin_api._sanitize_one({"op": "rename", "kind": "widget", "from": "a", "to": "b"}) is None
    assert plugin_api._sanitize_one({"op": "move_section", "name": "Osc", "row": 0, "order": True}) is None  # bool rejected
    assert plugin_api._sanitize_one({"op": "move_section", "name": "Osc", "row": 999, "order": 0}) is None     # out of band
    assert plugin_api._sanitize_one({"op": "move_section", "name": "Osc", "row": "1", "order": "2"}) == {"op": "move_section", "name": "Osc", "row": 1, "order": 2}
    assert plugin_api._sanitize_one({"op": "order_sections", "by": "vibes"}) is None
    assert plugin_api._sanitize_one({"op": "style", "name": "Osc"}) is None  # style with no property = nothing to do
    assert plugin_api._sanitize_one({"op": "style", "name": "Osc", "bold": True, "font_size": 60}) is None
    assert plugin_api._sanitize_one({"op": "delete_everything"}) is None
    assert plugin_api._sanitize_one("not a dict") is None
    assert plugin_api._sanitize_one({"op": "invert", "label": "Cutoff", "on": "yes"}) is None  # bool only
    # set op (live CC move): value 0-127, percent 0-100 -> wire, bool -> 127/0, junk refused
    assert plugin_api._sanitize_one({"op": "set", "label": "Cutoff", "value": 64}) == {"op": "set", "label": "Cutoff", "value": 64}
    assert plugin_api._sanitize_one({"op": "set", "label": "Cutoff", "percent": 50}) == {"op": "set", "label": "Cutoff", "value": 64}
    assert plugin_api._sanitize_one({"op": "set", "label": "Keys Hold", "value": True}) == {"op": "set", "label": "Keys Hold", "value": 127}
    assert plugin_api._sanitize_one({"op": "set", "label": "Keys Hold", "value": False}) == {"op": "set", "label": "Keys Hold", "value": 0}
    assert plugin_api._sanitize_one({"op": "set", "label": "Cutoff", "value": 200}) is None
    assert plugin_api._sanitize_one({"op": "set", "label": "Cutoff", "percent": 101}) is None
    assert plugin_api._sanitize_one({"op": "set", "label": "", "value": 5}) is None
    assert plugin_api._sanitize_one({"op": "set", "label": "Cutoff"}) is None


def test_sanitize_edges():
    e = plugin_api._sanitize_edge({"kind": "move", "target": "Cutoff", "from": 0, "to": 127, "duration_ms": 5000, "curve": "lin", "bogus": 9})
    assert e == {"kind": "move", "target": "Cutoff", "from": 0, "to": 127, "duration_ms": 5000, "curve": "lin"}, e
    assert plugin_api._sanitize_edge({"kind": "move", "target": "Cutoff", "from": 0, "to": 200, "duration_ms": 5000}) is None      # to>127
    assert plugin_api._sanitize_edge({"kind": "move", "target": "Cutoff", "from": 0, "to": 9, "duration_ms": 5}) is None           # <10ms
    assert plugin_api._sanitize_edge({"kind": "lfo", "target": "Cutoff", "wave": "square"}) is None                                 # not a wave
    assert plugin_api._sanitize_edge({"kind": "lfo", "target": "Cutoff", "rate_hz": 99}) is None
    assert plugin_api._sanitize_edge({"kind": "lfo", "target": "Cutoff", "phase": 0.5}) == {"kind": "lfo", "target": "Cutoff", "phase": 0.5}
    w = plugin_api._sanitize_edge({"kind": "watch", "source": "Timbre", "once": False, "then": [
        {"kind": "lfo", "target": "Resonance", "center": 64, "amp": 25, "rate_hz": 0.5, "wave": "tri"}]})
    assert w["then"][0]["center"] == 64 and w["once"] is False
    assert plugin_api._sanitize_edge({"kind": "when", "source": "Wave", "level": 89, "crosses": "up"}) is None       # then missing
    assert plugin_api._sanitize_edge({"kind": "when", "source": "Wave", "level": 89, "crosses": "sideways", "then": [{"kind": "move", "target": "x", "from": 0, "to": 1, "duration_ms": 100}]}) is None
    # watch edges keep level/crosses (validator accepts them)
    wd = plugin_api._sanitize_edge({"kind": "watch", "source": "Timbre", "level": 40, "crosses": "down",
                                    "then": [{"kind": "move", "target": "x", "from": 0, "to": 1, "duration_ms": 100}]})
    assert wd and wd.get("crosses") == "down" and wd.get("level") == 40, wd
    # then-lfo source is forced to 'lfo' — a stray label used to
    # pass the sanitizer and blow up the SAVE later (whole doc refused)
    tl = plugin_api._sanitize_edge({"kind": "watch", "source": "Timbre",
                                    "then": [{"kind": "lfo", "source": "wah", "target": "Res", "center": 64, "amp": 10}]})
    assert tl and tl["then"][0]["source"] == "lfo", tl
    deep = plugin_api._sanitize_edge({"kind": "when", "source": "Wave", "level": 1, "crosses": "up",
                                      "then": [{"kind": "when", "source": "a", "level": 2, "crosses": "up", "then": [{"kind": "move", "target": "b", "from": 0, "to": 1, "duration_ms": 10}]}]})
    assert deep is None  # then-edges must be move|lfo — no trigger nesting, same law as the validator


def test_system_prompt():
    s = plugin_api._chat_system(SUMMARY)
    assert "Cutoff" in s and "matrix_add" in s and "70% -> 89" in s
    assert "json" in s.lower()
    assert "same language" in s


def _run(coro):
    return asyncio.run(coro)


# The slug path imports hermes_cli.runtime_provider — its auth chain needs the
# hermes-agent repo root on sys.path (the 'pm' package), exactly like the real
# plugin host process has it (it runs as `python -m hermes_cli...` from there).
import importlib.util as _ilu
_spec = _ilu.find_spec("hermes_cli")
if _spec and _spec.origin:
    sys.path.insert(0, str(Path(_spec.origin).resolve().parent.parent))

def test_chat_profile_lines():
    # the summary used to be a raw JSON dump CHAR-truncated
    # mid-record (Push = 9515 chars -> model got broken JSON hiding 35 of 85
    # controls). Lines are whole records; overflow drops WHOLE lines and says.
    # the compaction point: 300 controls RENDER COMPLETE (the old 6000-char
    # JSON cut died around control 150) — and a monster that still overflows
    # drops WHOLE lines and tells the model so.
    big = {"controls": [{"id": f"c{i}", "label": f"Ctrl {i}", "cc": i % 128,
                          "widget": "knob", "section": "S"} for i in range(300)],
           "matrix": []}
    out = plugin_api._chat_profile_lines(big)
    assert "- Ctrl 84 (" in out, "line rendering lost a control"   # was inside the old mid-'save_btn' cut zone
    assert "- Ctrl 299 (" in out, "300 short lines fit — no silent loss"
    assert all(l.startswith("- ") for l in out.splitlines()), "every line is a whole record"
    assert "further entries exist" not in out, "a fitting profile must not claim overflow"
    monster = {"controls": [{"id": f"c{i}", "label": "L" * 60 + str(i), "cc": i % 128,
                              "widget": "slider", "section": "SomeSection"} for i in range(800)],
               "matrix": []}
    out3 = plugin_api._chat_profile_lines(monster)
    assert "further entries exist" in out3, "overflow must drop WHOLE lines and say so"
    assert all(l.startswith("- ") or l.startswith("- (") for l in out3.splitlines())
    small = {"controls": [{"label": "Wave", "cc": 21}, {"label": "Fader", "cc": 3, "widget": "slider", "invert": True}], "matrix": [{"kind": "lfo", "target": "Wave"}]}
    out2 = plugin_api._chat_profile_lines(small)
    assert "- Wave (cc21)" in out2 and "- Fader (cc3, slider, inverted)" in out2 and "- lane {" in out2, out2
    assert "further entries exist" not in out2, "small profiles must not claim overflow"
    sys_txt = plugin_api._chat_system(small)
    assert "one line per control" in sys_txt and "- Wave (cc21)" in sys_txt  # rendered lines, not a raw dump


def _chain_push_behaviour(pane):
    # BEHAVIOUR harness (owner 2026-11-10: "like the 30th attempt... utterly
    # broken"). Source-string pins proved patch TEXT shipped while the LAW
    # stayed wrong — this harness extracts the REAL shipped chainPushRow and
    # runs it in node against the owner's exact symptoms. Red on the bug,
    # green only when the law itself is right.
    import re
    import subprocess
    import tempfile
    m = re.search(r"function chainPushRow\(mates, x0, w\) \{.*?\n\}", pane, re.S)
    assert m, "chainPushRow law missing from pane"
    harness = m.group(0) + r"""
const key = (a) => a.map(o => o.id + '@' + o.x).join(',')
const eq = (got, want, msg) => { if (key(got) !== key(want)) { console.error('FAIL ' + msg + ': got [' + key(got) + '] want [' + key(want) + ']'); process.exit(1) } }
// Symptom 1 — drop into a VISIBLE EMPTY cell: nothing else may move.
eq(chainPushRow([{id:'a',x:0,w:1},{id:'b',x:3,w:1}], 1, 1), [{id:'a',x:0},{id:'b',x:3}], 'empty-slot drop made neighbours flee')
// Symptom 2 — drop ONTO an occupant: only the touched mate moves; a mate
// behind a gap stays put (the old law fled the whole tail).
eq(chainPushRow([{id:'a',x:5,w:1},{id:'b',x:8,w:1}], 5, 1), [{id:'a',x:6},{id:'b',x:8}], 'untouched tail fled')
// Chain — contiguous occupants slide one by one and STOP at the first gap.
eq(chainPushRow([{id:'a',x:5,w:1},{id:'b',x:6,w:1},{id:'c',x:9,w:1}], 5, 1), [{id:'a',x:6},{id:'b',x:7},{id:'c',x:9}], 'chain broken')
// Wide footprint vs a wide mate starting left of the span.
eq(chainPushRow([{id:'a',x:2,w:3}], 3, 4), [{id:'a',x:7}], 'wide push wrong')
// Categories (w=1): band [0,3] — drop at 1 moves NOTHING; drop at 0 pushes
// only 0->1, the section at 3 never flees.
eq(chainPushRow([{id:'s0',x:0,w:1},{id:'s3',x:3,w:1}], 1, 1), [{id:'s0',x:0},{id:'s3',x:3}], 'category empty-slot fled')
eq(chainPushRow([{id:'s0',x:0,w:1},{id:'s3',x:3,w:1}], 0, 1), [{id:'s0',x:1},{id:'s3',x:3}], 'category chain wrong')
// Group span of 2 landing in a gap: band [0,4], n=2 at 1 touches nothing.
eq(chainPushRow([{id:'s0',x:0,w:1},{id:'s4',x:4,w:1}], 1, 2), [{id:'s0',x:0},{id:'s4',x:4}], 'group span fled')
console.log('CHAIN-PUSH OK')
"""
    fd, path = tempfile.mkstemp(suffix=".mjs")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(harness)
        r = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0 and "CHAIN-PUSH OK" in r.stdout, f"chain law RED: {r.stdout}{r.stderr}"
    finally:
        os.unlink(path)


def _sec_over_behaviour(pane):
    # BEHAVIOUR harness #2 (owner 22:39: "moving a box clearly not on the
    # same storey still moves the boxes above it like its hallucinating
    # being in the way"). Extracts the REAL shipped computeSecOver and runs
    # it in node against a synthetic two-band snapshot whose columns DIFFER
    # per band — the exact shape that broke when columns were shared across
    # bands. A lower-band pointer must resolve in ITS OWN band's columns.
    import re
    import subprocess
    import tempfile
    m = re.search(r"const computeSecOver = \(px, py, sids\) => \{.*?\n  \}", pane, re.S)
    assert m, "computeSecOver law missing from pane"
    harness = (
        "const rootRef = { current: { querySelector: () => null } };\n"
        "const document = { querySelector: () => null };\n"
        "const secSnapRef = { current: null };\n"
        + m.group(0) + r"""
secSnapRef.current = { bands: [
  { row: 0, top: 100, bottom: 300, cols: [{ col: 0, left: 0, right: 400 }, { col: 1, left: 414, right: 600 }] },
  { row: 1, top: 314, bottom: 514, cols: [{ col: 0, left: 0, right: 260 }, { col: 1, left: 274, right: 434 }] }
], left: 0, right: 800, top: 100, bottom: 514 }
const eq = (got, want, msg) => { const bad = want === null ? got !== null : (!got || got.row !== want.row || got.col !== want.col); if (bad) { console.error('FAIL ' + msg + ': got ' + JSON.stringify(got) + ' want ' + JSON.stringify(want)); process.exit(1) } }
// The owner's exact frame: pointer in the LOWER band at x=350. Shared-track
// era resolved columns globally (x=350 sat inside the upper band's wide col
// 0) and dragged boxes above it. Per-band law: lower band's own col 1.
eq(computeSecOver(350, 400), { row: 1, col: 1 }, 'lower-band pointer resolved in the WRONG band columns')
eq(computeSecOver(350, 200), { row: 0, col: 0 }, 'upper-band containment')
eq(computeSecOver(500, 200), { row: 0, col: 1 }, 'upper-band second column')
eq(computeSecOver(500, 400), { row: 1, col: 2 }, 'right of lower band last cell = new column IN THAT band')
eq(computeSecOver(350, 700), { row: 2, col: 1 }, 'below all bands = new band')
eq(computeSecOver(350, 60), { row: -1, col: 0 }, 'above all bands = new band above')
eq(computeSecOver(1500, 400), null, 'far outside = cancel (snap back)')
console.log('SEC-OVER OK')
"""
    )
    fd, path = tempfile.mkstemp(suffix=".mjs")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(harness)
        r = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0 and "SEC-OVER OK" in r.stdout, f"band-targeting law RED: {r.stdout}{r.stderr}"
    finally:
        os.unlink(path)


def test_pane_chat_contracts_and_save_collision():
    pane = (_ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
    # The model sees actual apply/refusal results on the next turn.
    assert "['user', 'say', 'done', 'err'].includes(m.role)" in pane
    assert "console result: " in pane
    # Category fuzzy matches now obey the same unique-only safety law as controls.
    assert "const chatSecTier" in pane and "chatSecHint(d, op.name)" in pane
    # A11 (): the ambiguity law has NO exact-tier
    # exemption — two "Attack" controls or two "Filter" categories refuse and
    # name candidates instead of silently picking by JSON insertion order.
    assert "if (t.hits.length > 1) return null" in pane
    assert "!t.hits.length || t.hits.length > 1" in pane
    assert "t.tier !== 'exact' && t.hits.length > 1" not in pane
    # A3/A5/A6/A10: pane race + binding contracts ().
    assert "if (aid === lastActiveIdRef.current) {" in pane           # out-of-order profile commit refused
    assert "offFnsRef.current[n] = onNoteRef.current" in pane         # note-off returns on the note-on wire
    assert "window.addEventListener('blur', blurRelease)" in pane     # focus steal releases held keys
    # 'everything' merges the mic lane (owner video 2026-11-10): it used to ride
    # aB, a channel only 'led' paints, so bars/spectro/curve ignored a live mic.
    assert "const aA = src === 'mic' ? _m : src === 'all' ? _mg() : _o" in pane
    assert "const aA = src === 'mic' ? bands('mic') : bands('out')" not in pane
    assert "pidAtSend && d0 && d0.profile_id !== pidAtSend" in pane   # ops bound to the summarized profile
    assert "dgen === deckGenRef.current" in pane                      # deck docs die on saves, not on every poll
    # Chat transport (PR 153102): the pane calls its own Python half over a plugin
    # RPC and awaits the answer as a session event — it never sees a credential,
    # and the lane picker is gone (the aux slot in Settings is the picker now).
    assert "host.request('plugin.z-midi-console.chat.ask'" in pane
    assert "host.onEvent('plugin.z-midi-console.chat.answered'" in pane
    assert "host.state.focusedSessionId.get()" in pane
    assert "api.rest('/ask'" not in pane            # the credential-holding route is DEAD
    assert "/llm/models" not in pane                # and so is the in-box lane mirror
    # (owner rulings 2026-11-10): workspace laws.
    assert "grabRef.current = { dx:" in pane                    # B5/B6: grab offset captured at pointerdown
    assert "rowsT[t.y] = rowsT[t.y] || []" in pane              # B5: commitMove lands the group as a SHAPE
    assert "- offX" in pane                                      # B6: pointer offset wins over centre-the-span
    assert "category snap-back" in pane                          # B7: cancel region outside the workspace
    assert "box('push', over.row, m.x, colW(over.row, m.x))" in pane   # B8 law lives on: pushed neighbours previewed — now the CHAIN set only (empty-slot drop previews zero push)
    assert "for (const m of chainPushRow(inBand.map(" in pane        # category commit runs the SAME chain law as the preview
    assert "for (const m of chainPushRow(matesAt(ys).map(" in pane   # control commit too — one law, both grids
    assert "if ((sec.order || 0) >= col) sec.order" not in pane      # old shift-everything-at-or-after splice is DEAD
    assert ">= xmin)) all[cid2].x" not in pane                       # old knob-row tail-flee is DEAD
    assert "function chainPushRow(mates, x0, w) {" in pane           # module-scope pure law (harness runs the REAL code)
    assert "for (let y = 0; y <= maxY; y++) if (!byRow.has(y))" in pane  # DENSE ROWS: every y renders, gaps are real + hittable
    assert "minHeight: 88 }, 'data-empty'" in pane                   # empty cells carry slot height (no row collapse)
    assert "minHeight: 88 }, 'data-vacated'" in pane                 # lifted cards keep their row's height mid-drag
    _chain_push_behaviour(pane)                                      # BEHAVIOUR harness — source pins alone lied for ~30 attempts
    _sec_over_behaviour(pane)                                        # harness #2: band targeting law runs in node vs the owner's 22:39 frame
    assert "c.x == null && c.y == null" in pane                  # B4: unplaced analyzer = toolbar; placed = surface
    assert "while (byRow.get(y).has(x)) x += 1" in pane          # B2 defence: collisions never vanish from the Map
    assert "if (!owned.has(ck)) cover.add(ck)" in pane           # B2 defence: owned cells are never span-swallowed
    assert "if (!edit) return" in pane                           # B12: category measurement only exists in edit mode
    # Owner NIGHT rebuild (2026-11-10, beyond 0395ffb): the real flip-flop was
    # LAYOUT FEEDBACK — the preview was a grid child and columns are
    # max-content, so every target change resized the grid under the pointer.
    # Geometry now FROZEN at pointerdown; the pointer resolves ARITHMETICALLY
    # against the snapshot; the preview is an inert fixed overlay OUTSIDE the
    # grid flow (grid shape during a drag derives from sections ONLY).
    assert "secCellsCacheRef" not in pane
    assert "secSnapRef.current = snapSecGrid()" in pane
    assert "if (px >= c.left && px < c.right) return c.col" in pane          # column containment, arithmetic
    assert "for (const b of snap.bands) { if (py >= b.top && py < b.bottom) return b }" in pane  # band containment, arithmetic
    assert "return { row: b.row, col: colAt(b) }" in pane
    assert "'secph'" not in pane and "secpush-" not in pane                  # no preview as grid child
    assert "position: 'fixed', left: 0, top: 0, width: '100vw', height: '100vh', pointerEvents: 'none', zIndex: 30" in pane
    assert "secSel[sid] ? [sid," in pane                         # B9: an unselected click drags ONLY its category
    assert "setSel({}); setSecSel({})" in pane                   # B9: Deselect clears control AND category selection
    # Owner 22:39 rebuild: the LAST cross-band coupling was the category grid
    # itself — ONE CSS grid with max-content columns SHARED across bands, so
    # widening a section in the bottom band slid every box above it in that
    # column ("hallucinating being in the way" — no data moved, CSS moved
    # them). Bands are now INDEPENDENT stacked flex rows with per-band
    # columns; shared tracks are structurally GONE.
    assert "gridTemplateColumns" not in pane                                  # no shared CSS grid tracks ANYWHERE
    assert "'data-secband'" in pane and "const bandDivs = allRows.map(r => {" in pane  # per-band independent rows
    assert "root.querySelectorAll('[data-secband]')" in pane                  # snapshot captures PER-BAND column extents
    assert "const colAt = (b) => {" in pane                                   # columns resolve inside the resolved band
    assert "if (px > b.cols[b.cols.length - 1].right + 8) return b.cols.length" in pane  # new column belongs to THAT band
    assert "width: 'fit-content', boxSizing: 'border-box', minWidth: 160" in pane  # box IS its cell (no shared track to dead-edge against)
    assert "width: '100%', boxSizing: 'border-box', minWidth: 160" not in pane     # old fill-the-shared-cell law is DEAD
    # A live set under active modulation says why the value moves again.
    assert "chatMatrixTargets(d, cid)" in pane and "the next matrix tick may move it again" in pane
    # Busy UX is disabled in the pane, while the backend's own concurrency cap
    # refuses a third ask by name — test_rpc_handler_busy_cap_refuses_the_third_ask
    # exercises that cap end to end now that it lives with the RPC handler.
    assert "disabled: chatBusy || !chatIn.trim()" in pane
    assert "if (!text || chatBusy) return" in pane

    # save_as delegates to copy_profile, whose collision law is refusal rather
    # than overwrite. Exercise the actual store twice.
    dst = "chatbox-collision-pin"
    profile_store.copy_profile("microfreak", dst)
    try:
        profile_store.copy_profile("microfreak", dst)
        raise AssertionError("second copy overwrote an existing preset")
    except ValueError as exc:
        assert "already exists" in str(exc), exc


print("chat box battery")

# ---- the host-owned chat lane (catalog review, PR 153102) -------------------
# The transport law changed: the plugin no longer resolves the user's provider
# credentials or speaks to a model lane itself. These batteries pin what is left
# to us — the proposal gate — plus the two seams that replaced the transport:
# ctx.llm on the `midi_chat` aux task, and the plugin RPC job/event handshake.

import importlib as _il  # noqa: E402

sys.path.insert(0, str(_ROOT.parent))
PKG = _il.import_module(_ROOT.name)  # the agent half, for _make_chat_ask


class FakeLlm:
    """Stands in for ctx.llm: records the call, answers with a canned proposal."""

    def __init__(self, parsed=None, text=None, boom=None):
        self.parsed, self.text, self.boom, self.kwargs = parsed, text, boom, None

    def complete_structured(self, **kw):
        self.kwargs = kw
        if self.boom:
            raise self.boom
        return SimpleNamespace(parsed=self.parsed, text=self.text,
                               provider="fake-lane", model="fake/model-1")


def test_run_proposal_routes_through_the_aux_task():
    llm = FakeLlm(parsed={"say": "done", "ops": [{"op": "matrix", "action": "start"},
                                                 {"op": "nonsense"}]})
    out = chat_engine.run_proposal(llm, text="start the matrix", summary=SUMMARY, effort="fast")
    kw = llm.kwargs
    assert kw is not None, "run_proposal never reached the lane"
    assert kw["task"] == "midi_chat", f"task={kw.get('task')!r} — the route must be OUR aux slot"
    assert kw["json_schema"] == chat_engine.PROPOSAL_SCHEMA
    assert kw["max_tokens"] == chat_engine.EFFORT_TOKENS["fast"]
    assert kw["temperature"] == 0.1
    assert "Cutoff" in kw["system_prompt"], "the profile summary must ride in the system prompt"
    assert out["ok"] and out["ops"] == [{"op": "matrix", "action": "start"}]
    assert out["dropped"] and "nonsense" in out["dropped"][0]
    assert out["model"] == "fake/model-1", "the pane shows which lane actually answered"


def test_run_proposal_effort_maps_and_clamps():
    for want, effort in (("balanced", "auto"), ("balanced", "bogus"), ("thoughtful", "thoughtful")):
        llm = FakeLlm(parsed={"say": "", "ops": []})
        chat_engine.run_proposal(llm, text="x", summary=SUMMARY, effort=effort)
        assert llm.kwargs is not None and llm.kwargs["max_tokens"] == chat_engine.EFFORT_TOKENS[want], f"{effort} -> {want}"


def test_run_proposal_carries_the_conversation():
    llm = FakeLlm(parsed={"say": "", "ops": []})
    chat_engine.run_proposal(llm, text="no, the other one", summary=SUMMARY,
                             log=[{"role": "user", "text": "paint cutoff purple"}] * 20)
    kw = llm.kwargs
    assert kw is not None, "run_proposal never reached the lane"
    assert "CONVERSATION SO FAR" in kw["instructions"]
    assert kw["instructions"].count("paint cutoff purple") <= 6, "log must stay bounded"


def test_run_proposal_falls_back_to_extract_json():
    llm = FakeLlm(parsed=None, text='prose first ```json\n{"say":"hi","ops":[{"op":"matrix","action":"stop"}]}\n``` tail')
    out = chat_engine.run_proposal(llm, text="stop it", summary=SUMMARY)
    assert out["ok"] and out["ops"] == [{"op": "matrix", "action": "stop"}]


def test_run_proposal_unparseable_names_it():
    llm = FakeLlm(parsed=None, text="I shall consider your request.")
    out = chat_engine.run_proposal(llm, text="x", summary=SUMMARY)
    assert not out["ok"] and "unparseable" in out["error"] and out["raw"]


def test_run_proposal_refuses_a_bad_summary():
    llm = FakeLlm(parsed={"say": "", "ops": []})
    out = chat_engine.run_proposal(llm, text="x", summary={"controls": "not a list"})
    assert not out["ok"] and llm.kwargs is None, "refuse BEFORE spending a model call"


def test_run_proposal_lets_a_lane_failure_raise():
    llm = FakeLlm(boom=RuntimeError("lane is down"))
    try:
        chat_engine.run_proposal(llm, text="x", summary=SUMMARY)
        raise AssertionError("the lane failure was swallowed")
    except RuntimeError as exc:
        assert "lane is down" in str(exc)


class FakeCtx:
    def __init__(self, llm):
        self.llm = llm
        self.emitted = []

    def emit_to_session(self, session_id, event, payload):
        self.emitted.append((session_id, event, payload))


def _call(ask_fn, params, session_id):
    """The RPC contract answers with a JSON object; pyright cannot see that."""
    out = ask_fn(params, session_id=session_id)
    assert isinstance(out, dict)
    return out

def _await_event(ctx, job, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for sid, ev, payload in ctx.emitted:
            if payload.get("job") == job:
                return sid, ev, payload
        time.sleep(0.02)
    raise AssertionError(f"no chat.answered event for job {job}")


def test_rpc_handler_answers_with_a_job_then_emits():
    ctx = FakeCtx(FakeLlm(parsed={"say": "painted", "ops": [{"op": "matrix", "action": "start"}]}))
    ask = PKG._make_chat_ask(ctx)
    out = _call(ask, {"text": "start the matrix", "summary": SUMMARY}, session_id="sess-1")
    assert isinstance(out, dict) and out.get("job"), f"handler must return a job id at once, got {out!r}"
    sid, ev, payload = _await_event(ctx, out["job"])
    assert sid == "sess-1" and ev == "chat.answered"
    assert payload["ok"] and payload["ops"] == [{"op": "matrix", "action": "start"}]


def test_rpc_handler_never_holds_the_lane():
    # A slow model must not slow the RPC answer: the call returns while the
    # model is still thinking (an RPC call holds the client's request lane).
    class SlowLlm(FakeLlm):
        def complete_structured(self, **kw):
            time.sleep(0.6)
            return super().complete_structured(**kw)
    ctx = FakeCtx(SlowLlm(parsed={"say": "late", "ops": []}))
    ask = PKG._make_chat_ask(ctx)
    t0 = time.time()
    out = _call(ask, {"text": "x", "summary": SUMMARY}, session_id="s")
    assert time.time() - t0 < 0.3, "the handler blocked on the model"
    _await_event(ctx, out["job"], timeout=5.0)


def test_rpc_handler_lane_failure_is_data():
    ctx = FakeCtx(FakeLlm(boom=RuntimeError("no credentials configured")))
    ask = PKG._make_chat_ask(ctx)
    out = _call(ask, {"text": "x", "summary": SUMMARY}, session_id="s")
    _, _, payload = _await_event(ctx, out["job"])
    assert not payload["ok"] and "no credentials configured" in payload["error"]


def test_rpc_handler_refuses_without_a_session():
    ctx = FakeCtx(FakeLlm(parsed={"say": "", "ops": []}))
    ask = PKG._make_chat_ask(ctx)
    for bad in (None, ""):
        try:
            _call(ask, {"text": "x", "summary": SUMMARY}, bad)
            raise AssertionError("a sessionless ask was accepted")
        except ValueError as exc:
            assert "session_id" in str(exc)


def test_rpc_handler_refuses_a_bad_summary_and_empty_text():
    ctx = FakeCtx(FakeLlm(parsed={"say": "", "ops": []}))
    ask = PKG._make_chat_ask(ctx)
    for params in ({"text": "  ", "summary": SUMMARY}, {"text": "x", "summary": {}}):
        try:
            _call(ask, params, "s")
            raise AssertionError(f"accepted {params}")
        except ValueError:
            pass


def test_rpc_handler_busy_cap_refuses_the_third_ask():
    # Two in flight (the old /ask cap) — the third refuses by name instead of
    # queueing a worker nobody can cancel.
    class BlockingLlm(FakeLlm):
        def __init__(self):
            super().__init__(parsed={"say": "", "ops": []})
            self.entered, self.release = threading.Event(), threading.Event()

        def complete_structured(self, **kw):
            self.entered.set()
            self.release.wait(timeout=5)
            return super().complete_structured(**kw)
    llm = BlockingLlm()
    ctx = FakeCtx(llm)
    ask = PKG._make_chat_ask(ctx)
    j1 = _call(ask, {"text": "a", "summary": SUMMARY}, session_id="s")["job"]
    assert llm.entered.wait(2), "first ask never reached the lane"
    j2 = _call(ask, {"text": "b", "summary": SUMMARY}, session_id="s")["job"]
    try:
        ask({"text": "c", "summary": SUMMARY}, session_id="s")
        raise AssertionError("a third ask was accepted while two ran")
    except RuntimeError as exc:
        assert "busy" in str(exc)
    llm.release.set()
    for job in (j1, j2):
        _await_event(ctx, job)
    _call(ask, {"text": "d", "summary": SUMMARY}, "s")  # slots recovered


def test_dashboard_half_holds_no_credentials():
    """The reviewer's rule, pinned as a regression guard.

    A plugin must never hold the user's provider credentials. The dashboard half
    used to import hermes_cli, take api_key/extra_headers and drive the
    transport itself — including this machine's own model ports.
    """
    src = (_ROOT / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")
    for gone in ("resolve_runtime_provider", "hermes_cli", "extra_headers",
                 "_CHAT_LANES", "127.0.0.1:1249", "127.0.0.1:11434", "127.0.0.1:8091",
                 "api_key"):
        assert gone not in src, f"{gone!r} is back in the dashboard half"
    assert "from chat_engine import" in src, "the proposal gate stays shared with the RPC handler"


def test_store_lives_in_the_profile_home():
    """PR 153102: profiles must not share one machine-wide store."""
    got = str(profile_store.data_dir()).replace("\\", "/").lower()
    want = str(Path(_TMP) / "plugin-data" / "z-midi-console").replace("\\", "/").lower()
    assert got == want, f"data_dir() escaped the pinned HERMES_HOME: {got!r} != {want!r}"


def test_legacy_store_migrates_once_and_is_never_deleted():
    old = Path(_TMP) / "z-midi-console"
    (old / "profiles").mkdir(parents=True, exist_ok=True)
    (old / "profiles" / "legacy.json").write_text('{"profile_id":"legacy"}', encoding="utf-8")
    fresh = Path(_TMP) / "fresh-home"
    fresh.mkdir(exist_ok=True)
    os.environ["HERMES_HOME"] = str(fresh)
    try:
        profile_store._MIGRATED = False
        new = profile_store.data_dir()
        assert (new / "profiles" / "legacy.json").is_file(), "the old store did not follow the profile"
        assert (old / "profiles" / "legacy.json").is_file(), "migration must COPY, never move"
        profile_store._MIGRATED = False
        (new / "marker").write_text("x", encoding="utf-8")
        profile_store.data_dir()
        assert (new / "marker").is_file(), "a populated new home must not be re-overwritten"
    finally:
        os.environ["HERMES_HOME"] = _TMP
        profile_store._MIGRATED = False


check("extract_json survives fences + prose", test_extract)
check("whitelists mirror the validator (kinds/curves/waves)", test_mirrors)
check("op sanitizer: shapes, ranges, bool-rejects, unknown dropped", test_sanitize_ops)
check("edge sanitizer: move/when/watch/then, no nesting, ranges", test_sanitize_edges)
check("system prompt carries labels + rules", test_system_prompt)
check("chat profile lines: whole-record rendering, no mid-line truncation", test_chat_profile_lines)
check("run_proposal routes through the midi_chat aux task", test_run_proposal_routes_through_the_aux_task)
check("run_proposal effort maps to a reply budget and clamps junk", test_run_proposal_effort_maps_and_clamps)
check("run_proposal carries a bounded conversation", test_run_proposal_carries_the_conversation)
check("run_proposal falls back to extract_json", test_run_proposal_falls_back_to_extract_json)
check("run_proposal names an unparseable reply", test_run_proposal_unparseable_names_it)
check("run_proposal refuses a bad summary BEFORE spending a call", test_run_proposal_refuses_a_bad_summary)
check("run_proposal lets a lane failure raise to the handler", test_run_proposal_lets_a_lane_failure_raise)
check("chat.ask RPC: job id at once, proposal on the session stream", test_rpc_handler_answers_with_a_job_then_emits)
check("chat.ask RPC: a slow model never holds the request lane", test_rpc_handler_never_holds_the_lane)
check("chat.ask RPC: a lane failure arrives as data, not a crash", test_rpc_handler_lane_failure_is_data)
check("chat.ask RPC: sessionless asks refuse by name", test_rpc_handler_refuses_without_a_session)
check("chat.ask RPC: empty text / bad summary refuse", test_rpc_handler_refuses_a_bad_summary_and_empty_text)
check("chat.ask RPC: third concurrent ask refuses, slots recover", test_rpc_handler_busy_cap_refuses_the_third_ask)
check("dashboard half holds no provider credentials, no personal ports", test_dashboard_half_holds_no_credentials)
check("store lives in the profile home", test_store_lives_in_the_profile_home)
check("legacy machine-wide store migrates once, copy-not-move", test_legacy_store_migrates_once_and_is_never_deleted)
check("pane context/ambiguity/matrix/busy contracts; save_as collision refuses", test_pane_chat_contracts_and_save_collision)

if __name__ == "__main__":
    print("FAILURES:", FAIL if FAIL else "none")
    sys.exit(1 if FAIL else 0)
