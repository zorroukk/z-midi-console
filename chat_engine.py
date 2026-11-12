"""Chat-box proposal engine (Z's MIDI Console).

The chat box law is unchanged: the model only PROPOSES. Everything real is
decided here and in the pane — JSON extraction must survive fences/prose, the
op whitelist must drop anything invented, and ranges must match the profile
validator's own bounds.

What changed (catalog review, PR 153102): the model CALL is no longer made
here. The plugin used to resolve the user's provider credentials itself and
drive the transport (chat_completions / Responses / Ollama native, lane
guards, SSE). Hermes forbids a plugin holding those credentials, so the call
now goes through ``ctx.llm.complete_structured(..., task="midi_chat")`` — the
host keeps the keys, picks the transport, and the user chooses the model under
Settings -> Models -> Auxiliary. That retired the whole transport stack and
both private ``hermes_cli`` imports.

Stdlib only, and free of any Hermes import: this module is what the RPC
handler (``__init__.py``) and the dashboard half share, and what the test
battery drives directly.
"""

import json
import re

HEX6 = re.compile(r"^#[0-9a-f]{6}$")
EDGE_KINDS = {"lfo", "move", "when", "watch"}
CURVES = {"lin", "exp", "log", "s", "step4", "snap12"}
WAVES = {"sine", "tri", "saw", "sh"}

#: The proposal shape. The host requests JSON in this shape and validates it
#: when jsonschema is installed; ``extract_json`` still re-parses defensively,
#: because the whitelist below — not the schema — is what protects the wire.
PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "say": {"type": "string"},
        "ops": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["say", "ops"],
}

#: effort -> reply budget. Thinking models spend budget on hidden reasoning
#: before the answer; the old per-lane think/reasoning translation is gone
#: because the aux slot (auxiliary.midi_chat) now owns reasoning effort.
EFFORT_TOKENS = {"fast": 500, "balanced": 900, "thoughtful": 1400}


HEX6 = re.compile(r"^#[0-9a-f]{6}$")
EDGE_KINDS = {"lfo", "move", "when", "watch"}
CURVES = {"lin", "exp", "log", "s", "step4", "snap12"}
WAVES = {"sine", "tri", "saw", "sh"}

def int_in(v, lo: int, hi: int):
    if isinstance(v, bool):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def num_in(v, lo: float, hi: float):
    if isinstance(v, bool):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def label(v):
    s = str(v or "").strip()
    return s[:80] or None


def sanitize_edge(e):
    if not isinstance(e, dict):
        return None
    kind = e.get("kind", "lfo")
    if kind not in EDGE_KINDS:
        return None
    out = {"kind": kind}
    for key in ("source", "target"):
        lab = label(e.get(key))
        if lab:
            out[key] = lab
    if "depth" in e:
        d = num_in(e.get("depth"), -2.0, 2.0)
        if d is None:
            return None
        out["depth"] = d
    if "slew" in e:
        s = num_in(e.get("slew"), 0.0, 1.0)
        if s is None:
            return None
        out["slew"] = s
    if "mode" in e:
        if e.get("mode") not in ("add", "mul", "set"):
            return None
        out["mode"] = e["mode"]
    if "curve" in e:
        if e.get("curve") not in CURVES:
            return None
        out["curve"] = e["curve"]
    if "wave" in e:
        if e.get("wave") not in WAVES:
            return None
        out["wave"] = e["wave"]
    if "rate_hz" in e:
        r = num_in(e.get("rate_hz"), 0.01, 10.0)
        if r is None:
            return None
        out["rate_hz"] = r
    if "phase" in e:
        p = num_in(e.get("phase"), 0.0, 1.0)
        if p is None:
            return None
        out["phase"] = p
    if "center" in e:
        c = int_in(e.get("center"), 0, 127)
        if c is None:
            return None
        out["center"] = c
    if "amp" in e:
        a = int_in(e.get("amp"), 0, 127)
        if a is None:
            return None
        out["amp"] = a
    if kind == "move":
        for key in ("from", "to"):
            n = int_in(e.get(key), 0, 127)
            if n is None:
                return None
            out[key] = n
        d = int_in(e.get("duration_ms"), 10, 600000)
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
            lv = int_in(e.get("level"), 0, 127)
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
        then = [sanitize_edge(t) for t in (e.get("then") or [])
                if isinstance(t, dict) and t.get("kind") in ("move", "lfo")]
        then = [dict(t, source="lfo") if t and t.get("kind") == "lfo" else t for t in then]
        then = [t for t in then if t]
        if not then:
            return None
        out["then"] = then
    return out


def sanitize_one(o):
    if not isinstance(o, dict):
        return None
    op = o.get("op")
    if op in ("color", "clear_color"):
        t = label(o.get("target"))
        if not t:
            return None
        if op == "clear_color":
            return {"op": "clear_color", "target": t}
        h = str(o.get("hex") or "").lower()
        if not HEX6.match(h):
            return None
        return {"op": "color", "target": t, "hex": h}
    if op == "rename":
        kind, frm, to = o.get("kind"), label(o.get("from")), label(o.get("to"))
        if kind not in ("section", "control") or not frm or not to:
            return None
        return {"op": "rename", "kind": kind, "from": frm, "to": to}
    if op == "move_section":
        nm = label(o.get("name"))
        row, order = int_in(o.get("row"), 0, 99), int_in(o.get("order"), 0, 99)
        if not nm or row is None or order is None:
            return None
        return {"op": "move_section", "name": nm, "row": row, "order": order}
    if op == "order_sections":
        by = o.get("by")
        if by not in ("size", "name"):
            return None
        return {"op": "order_sections", "by": by}
    if op == "style":
        nm = label(o.get("name"))
        if not nm:
            return None
        out: dict[str, object] = {"op": "style", "name": nm}
        if "bold" in o:
            if not isinstance(o.get("bold"), bool):
                return None
            out["bold"] = o["bold"]
        if "font_size" in o:
            fs = int_in(o.get("font_size"), 8, 48)
            if fs is None:
                return None
            out["font_size"] = fs
        if len(out) == 2:
            return None
        return out
    if op == "invert":
        lab, on = label(o.get("label")), o.get("on")
        if not lab or not isinstance(on, bool):
            return None
        return {"op": "invert", "label": lab, "on": on}
    if op == "set":
        # Live move of one control (a CC send through the Bus — wire remap and
        # shadow apply like any panel move). NOT a preset edit.
        lab = label(o.get("label"))
        if not lab:
            return None
        if o.get("percent") is not None:
            p = num_in(o.get("percent"), 0.0, 100.0)
            if p is None:
                return None
            return {"op": "set", "label": lab, "value": int(round(p * 127.0 / 100.0))}
        v = o.get("value")
        if isinstance(v, bool):  # bool BEFORE int (bool is an int subclass)
            return {"op": "set", "label": lab, "value": 127 if v else 0}
        vi = int_in(v, 0, 127)
        if vi is None:
            return None
        return {"op": "set", "label": lab, "value": vi}
    if op == "matrix_add":
        e = sanitize_edge(o.get("edge"))
        return {"op": "matrix_add", "edge": e} if e else None
    if op == "matrix":
        act = o.get("action")
        if act not in ("start", "stop"):
            return None
        return {"op": "matrix", "action": act}
    if op == "save_as":
        nm = label(o.get("name"))
        if not nm:
            return None
        return {"op": "save_as", "name": nm}
    return None


def extract_json(text: str):
    """First complete JSON object in a reply, preferring fenced payloads.

    A first-``{``/last-``}`` slice joined unrelated prose braces and multiple
    objects into invalid JSON. Scan balanced candidates instead, respecting
    quoted braces and escapes; a malformed candidate cannot hide a later good
    proposal.
    """
    if not isinstance(text, str):
        return None
    t = text.strip()
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", t, flags=re.IGNORECASE | re.DOTALL)

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
                obj = json.loads(candidate)
            except (ValueError, TypeError):
                continue
            if isinstance(obj, dict):
                return obj
    return None


def build_system_prompt(summary) -> str:
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
        + profile_lines(summary)
    )


def profile_lines(summary: dict) -> str:
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
            out.append("- lane " + json.dumps(e, separators=(",", ":")))
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


def run_proposal(llm, *, text, summary, log=None, effort="auto"):
    """Ask the host-owned model for a proposal and gate it through the whitelist.

    ``llm`` is ``ctx.llm`` (anything with ``complete_structured`` — the battery
    hands in a fake). Returns the pane-facing dict: {ok, say, ops, dropped}.
    Raises on a host-side failure; the RPC handler names it to the pane.
    """
    if not isinstance(summary, dict) or not isinstance(summary.get("controls"), list):
        return {"ok": False, "error": "pane must send the profile summary (controls)"}
    if effort not in EFFORT_TOKENS:
        effort = "balanced" if effort == "auto" else effort
        effort = effort if effort in EFFORT_TOKENS else "balanced"
    turns = []
    for m in (log or [])[-6:]:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("text"):
            turns.append(f"{m['role']}: {str(m['text'])[:1200]}")
    ask = text if not turns else (
        "CONVERSATION SO FAR (oldest first):\n" + "\n".join(turns) + "\n\nNEW REQUEST: " + text)
    result = llm.complete_structured(
        instructions=ask,
        input=[{"type": "text", "text": ask}],
        json_schema=PROPOSAL_SCHEMA,
        schema_name="z-midi-console.proposal",
        system_prompt=build_system_prompt(summary),
        task="midi_chat",
        temperature=0.1,
        max_tokens=EFFORT_TOKENS[effort],
        purpose="z-midi-console.chat",
    )
    parsed = result.parsed
    if parsed is None:
        parsed = extract_json(result.text or "")
    if not isinstance(parsed, dict):
        return {"ok": False,
                "error": "the model answered with something unparseable — try a plainer sentence",
                "raw": str(result.text or "")[:300]}
    raw_ops = parsed.get("ops")
    if not isinstance(raw_ops, list):
        raw_ops = []
    ops, dropped = [], []
    for o in raw_ops:
        keep = sanitize_one(o)
        if keep:
            ops.append(keep)
        else:
            dropped.append(json.dumps(o, default=str)[:120])
    return {"ok": True, "say": str(parsed.get("say") or "")[:600],
            "ops": ops, "dropped": dropped,
            # Attribution the pane can show honestly: which lane actually answered.
            "provider": getattr(result, "provider", None),
            "model": getattr(result, "model", None)}

