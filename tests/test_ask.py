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


def test_reply_text():
    proposal = '{"say":"ok","ops":[]}'
    assert plugin_api._reply_text({"output_text": proposal}, True) == proposal
    assert plugin_api._reply_text({"output": [{"content": [{"type": "output_text", "text": proposal}]}]}, True) == proposal
    assert plugin_api._reply_text({"choices": [{"message": {"content": [{"type": "text", "text": proposal}]}}]}, False) == proposal
    assert plugin_api._reply_text({"message": {"content": proposal}}, False) == proposal
    assert plugin_api._reply_text({"choices": [{"message": {"content": []}}]}, False) == ""


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

def test_ask_guards():
    r = _run(plugin_api.post_ask(req({"text": "", "model": {"base": "http://127.0.0.1:1/v1", "id": "m"}, "summary": SUMMARY})))
    assert r == {"ok": False, "error": "nothing to ask"}, r
    # Guard law (owner override 2026-11-10): cloud lanes are IN — the rule is
    # 'no PLAINTEXT over the open internet'. https anywhere passes; http only
    # loopback/LAN/Tailscale.
    r = _run(plugin_api.post_ask(req({"text": "paint it", "model": {"base": "http://evil.example/v1", "id": "m"}, "summary": SUMMARY})))
    assert not r["ok"] and "unencrypted" in r["error"], r  # plaintext remote refused before any request
    # userinfo-URL bypass — "http://127.0.0.1:***@host"
    # passed the prefix check but urlsplit/urllib sent the profile to HOST.
    for sneaky in ("http://127.0.0.1:***@evil.example/v1",
                   "http://evil.example:8080/v1"):
        r = _run(plugin_api.post_ask(req({"text": "paint it", "model": {"base": sneaky, "id": "m"}, "summary": SUMMARY})))
        assert not r["ok"] and "unencrypted" in r["error"], (sneaky, r)
    r = _run(plugin_api.post_ask(req({"text": "paint it", "model": {"base": "http://127.0.0.1:1/v1", "id": ""}, "summary": SUMMARY})))
    assert not r["ok"] and "model" in r["error"]  # empty model id refused
    r = _run(plugin_api.post_ask(req({"text": "paint it", "model": {"base": "http://127.0.0.1:1/v1", "id": "m"}, "summary": {"no_controls": 1}})))
    assert not r["ok"] and "summary" in r["error"]
    # dead local lane -> named unreachable, never a crash
    r = _run(plugin_api.post_ask(req({"text": "paint it", "model": {"base": "http://127.0.0.1:1/v1", "id": "m"}, "summary": SUMMARY})))
    assert not r["ok"] and "unreachable" in r["error"], r
    # slug path (owner law 2026-11-10: chat box mirrors the app's model
    # selector): an unknown slug must name the resolution failure, never 500.
    r = _run(plugin_api.post_ask(req({"text": "paint it", "model": {"slug": "no-such-lane-xyz", "id": "m"}, "summary": SUMMARY})))
    assert not r["ok"] and "could not be resolved" in r["error"], r
    lanes = plugin_api.get_llm_models()
    assert lanes["ok"] and isinstance(lanes["lanes"], list)  # sandbox: offline lanes simply absent


def test_ask_slug_transport():
    # Owner law 2026-11-10: a picked slug resolves through Hermes' own runtime
    # resolver; the ask must honour the resolved transport (chat_completions
    # vs Responses API), carry the resolved key, and keep the lane guard on
    # the RESOLVED base (defence in depth).
    import hermes_cli.runtime_provider as rp
    orig_resolver, orig_http, orig_sse = rp.resolve_runtime_provider, plugin_api._http_json, plugin_api._http_sse_json
    cap = {}
    def fake_rt(*, requested=None, target_model=None, **kw):
        assert requested == "fakelane"
        return {"base_url": "http://127.0.0.1:65001/v1", "api_mode": "codex_responses", "api_key": "sekret"}
    def fake_http(url, payload=None, timeout=8.0, headers=None):
        cap["url"], cap["body"], cap["headers"] = url, payload, headers
        content = '{"ops": [], "say": "done via responses"}'
        if url.endswith("/responses"):
            return {"output_text": content}
        return {"choices": [{"message": {"content": content}}]}
    rp.resolve_runtime_provider = fake_rt
    plugin_api._http_json = fake_http
    try:
        r = _run(plugin_api.post_ask(req({"text": "paint it", "model": {"slug": "fakelane", "id": "gpt-x"},
                                          "effort": "thoughtful", "summary": SUMMARY})))
        assert r["ok"] and r.get("say") == "done via responses", r
        assert cap["url"].endswith("/responses"), cap["url"]
        assert cap["headers"] == {"Authorization": "Bearer sekret"}, cap["headers"]
        assert cap["body"]["instructions"] and cap["body"]["input"][-1]["role"] == "user", cap["body"]
        # ChatGPT/Codex subscription endpoint contract: requests are ephemeral.
        # The live lane returns HTTP 400 "Store must be set to false" without it.
        assert cap["body"]["store"] is False, cap["body"]
        assert cap["body"]["reasoning"] == {"effort": "high"}, cap["body"]
        assert cap["body"]["max_output_tokens"] == 1400, cap["body"]
        # the Responses API gets NO temperature (gpt-5 family
        # 400s on anything but its default)
        assert "temperature" not in cap["body"], cap["body"]
        # extra_headers + request_overrides FROM THE RESOLVER
        # must ride (Codex identity headers, proxy auth, custom body params)
        rp.resolve_runtime_provider = lambda **kw: {
            "base_url": "http://127.0.0.1:65001/v1", "api_mode": "chat_completions",
            "api_key": "k", "extra_headers": {"x-scx-identity": "1", "x-account": "a1"},
            "request_overrides": {"service_tier": "fast"}}
        cap.clear()
        r = _run(plugin_api.post_ask(req({"text": "paint it", "model": {"slug": "fakelane", "id": "gpt-x"},
                                          "summary": SUMMARY})))
        assert r["ok"], r
        assert cap["headers"].get("x-scx-identity") == "1" and cap["headers"].get("Authorization") == "Bearer k", cap["headers"]
        assert cap["body"].get("service_tier") == "fast", cap["body"]
        # A7 (): the SDK's extra_body MERGES into the
        # JSON body — the ask must spread it, never send a literal
        # "extra_body" property that lanes ignore (or 400 on).
        rp.resolve_runtime_provider = lambda **kw: {
            "base_url": "http://127.0.0.1:65001/v1", "api_mode": "chat_completions",
            "api_key": "k", "request_overrides": {"extra_body": {"prediction": {"content": "x"}}}}
        cap.clear()
        r = _run(plugin_api.post_ask(req({"text": "paint it", "model": {"slug": "fakelane", "id": "gpt-x"},
                                          "summary": SUMMARY})))
        assert r["ok"], r
        assert cap["body"].get("prediction") == {"content": "x"}, cap["body"]
        assert "extra_body" not in cap["body"], cap["body"]
        # The ChatGPT/Codex subscription endpoint is Responses SSE-only.
        rp.resolve_runtime_provider = lambda **kw: {
            "provider": "openai-codex", "base_url": "https://chatgpt.com/backend-api/codex",
            "api_mode": "codex_responses", "api_key": "oauth"}
        def fake_sse(url, payload=None, timeout=8.0, headers=None):
            cap["url"], cap["body"], cap["headers"] = url, payload, headers
            return {"output_text": '{"ops": [], "say": "subscription works"}'}
        plugin_api._http_sse_json = fake_sse
        cap.clear()
        r = _run(plugin_api.post_ask(req({"text": "paint it", "model": {"slug": "openai-codex", "id": "gpt-x"},
                                          "summary": SUMMARY})))
        assert r["ok"] and r["say"] == "subscription works", r
        assert cap["body"]["store"] is False and cap["body"]["stream"] is True, cap["body"]
        # a CALLABLE token provider (Azure Entra class) must
        # refuse by name — str()-ing it used to send "Bearer <function …>"
        rp.resolve_runtime_provider = lambda **kw: {
            "base_url": "https://example.azure.com/v1", "api_mode": "chat_completions",
            "api_key": (lambda: "tok")}
        r = _run(plugin_api.post_ask(req({"text": "paint it", "model": {"slug": "fakelane", "id": "gpt-x"},
                                          "summary": SUMMARY})))
        assert not r["ok"] and "token provider" in r["error"], r
        # un-driveable transport -> named refusal, no bytes sent
        rp.resolve_runtime_provider = lambda **kw: {"base_url": "http://127.0.0.1:65001/v1",
                                                    "api_mode": "codex_app_server", "api_key": ""}
        r = _run(plugin_api.post_ask(req({"text": "paint it", "model": {"slug": "fakelane", "id": "gpt-x"},
                                          "summary": SUMMARY})))
        assert not r["ok"] and "codex_app_server" in r["error"], r
        # resolved base still passes the exfil guard (plaintext remote refused)
        rp.resolve_runtime_provider = lambda **kw: {"base_url": "http://evil.example/v1",
                                                    "api_mode": "chat_completions", "api_key": "k"}
        r = _run(plugin_api.post_ask(req({"text": "paint it", "model": {"slug": "fakelane", "id": "gpt-x"},
                                          "summary": SUMMARY})))
        assert not r["ok"] and "unencrypted" in r["error"], r
    finally:
        rp.resolve_runtime_provider = orig_resolver
        plugin_api._http_json = orig_http
        plugin_api._http_sse_json = orig_sse


def test_redirect_guard_strips_auth():
    # A2 (): urllib followed 301/302 blindly and
    # re-sent Authorization to the new host; the redirect target was never
    # re-checked against the guard law. Every hop must re-validate, and
    # credentials must NOT cross origins.
    import http.server
    seen = {}
    cap_srv = None
    cap_port_holder = {}

    class Cap(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["auth"] = self.headers.get("Authorization")
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": 1}')

        def log_message(self, *args, **kwargs):
            pass

    class Redir(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            target = (f"http://127.0.0.1:{cap_port_holder['p']}/x" if self.path == "/hop"
                      else "http://example.invalid/x")
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()

        def log_message(self, *args, **kwargs):
            pass

    cap_srv = http.server.HTTPServer(("127.0.0.1", 0), Cap)
    red_srv = http.server.HTTPServer(("127.0.0.1", 0), Redir)
    cap_port_holder["p"] = cap_srv.server_address[1]
    red_port = red_srv.server_address[1]
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (cap_srv, red_srv)]
    for t in threads:
        t.start()
    try:
        # cross-origin hop (different port = different origin): auth stripped
        out = plugin_api._http_json(f"http://127.0.0.1:{red_port}/hop",
                                    headers={"Authorization": "Bearer SECRET-A2"})
        assert out.get("ok") == 1, out
        assert seen.get("auth") is None, f"credential followed a cross-origin redirect: {seen}"
        # guard-violating target (plaintext public host): refuse, don't follow
        try:
            plugin_api._http_json(f"http://127.0.0.1:{red_port}/evil")
            raise AssertionError("guard-violating redirect must refuse")
        except plugin_api._urlreq.HTTPError as e:
            assert "guard law" in str(e), e
    finally:
        cap_srv.shutdown()
        red_srv.shutdown()
        cap_srv.server_close()
        red_srv.server_close()


def test_picker_mirror_drivable_and_current_model():
    # A9 (): built-in rows carry no api_mode —
    # anthropic/bedrock sat selectable-by-default and only refused by name
    # AFTER Send; and the pane could not see WHICH model the app is on
    # (it defaulted to lane models[0]).
    import hermes_cli.inventory as inv
    orig_ctx, orig_build = inv.load_picker_context, inv.build_model_options_payload
    ctx = SimpleNamespace(custom_providers=[{"provider_key": "jeff", "base_url": "http://127.0.0.1:65099/v1", "api_mode": "chat_completions"}], user_providers={})
    inv.load_picker_context = lambda: ctx
    inv.build_model_options_payload = lambda *a, **kw: {
        "providers": [
            {"slug": "anthropic", "name": "Anthropic", "is_current": False, "models": ["claude-x"]},
            {"slug": "bedrock", "name": "Bedrock", "is_current": False, "models": ["b-x"]},
            {"slug": "nous", "name": "Nous Portal", "is_current": False, "models": ["n-x"]},
            {"slug": "jeff", "name": "Jeff", "is_current": True, "models": ["qwen-x"]},
        ], "model": "qwen-x", "provider": "jeff"}
    try:
        r = plugin_api.get_llm_models()
    finally:
        inv.load_picker_context, inv.build_model_options_payload = orig_ctx, orig_build
    assert r["ok"] and r["current_model"] == "qwen-x", r
    by = {l["slug"]: l for l in r["lanes"]}
    assert by["anthropic"]["drivable"] is False and by["bedrock"]["drivable"] is False, by
    # unknown built-in stays drivable (most are OpenAI-style; /ask still
    # refuses any real mismatch by name) — and config-known api_mode wins
    assert by["nous"]["drivable"] is True and by["jeff"]["drivable"] is True, by
    assert by["jeff"]["current"] is True, by["jeff"]


def test_responses_sse_parser():
    class FakeResponse:
        def __enter__(self): return iter([
            b'event: response.output_text.delta\n',
            b'data: {"type":"response.output_text.delta","delta":"{\\"say\\":\\"ok\\","}\n',
            b'data: {"type":"response.output_text.delta","delta":"\\"ops\\":[]}"}\n',
            b'data: {"type":"response.completed","response":{"status":"completed"}}\n',
        ])
        def __exit__(self, *args): return False
    orig = plugin_api._lane_opener
    class FakeOpener:
        def open(self, req, timeout=None):
            return FakeResponse()
    plugin_api._lane_opener = lambda: FakeOpener()  # A2 law: both transports go through the lane-guard opener now
    try:
        r = plugin_api._http_sse_json("https://example.test/responses", {"stream": True})
        assert r == {"output_text": '{"say":"ok","ops":[]}'}, r
    finally:
        plugin_api._lane_opener = orig


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


def test_ask_effort():
    # Effort knob (owner ask 2026-11-10): thinking models spend max_tokens on
    # hidden reasoning BEFORE the answer. /ask must clamp effort to the known
    # set and translate it per lane (reasoning_effort / ollama think).
    cap = {}
    orig = plugin_api._http_json
    def fake(url, payload=None, timeout=8.0, headers=None):
        cap["url"] = url
        cap["body"] = payload
        return {"choices": [{"message": {"content": '{"ops": [], "say": "ok"}'}}]}
    plugin_api._http_json = fake
    try:
        base = "http://127.0.0.1:65001/v1"
        for eff, want in (("fast", "low"), ("balanced", "medium"), ("thoughtful", "high")):
            r = _run(plugin_api.post_ask(req({"text": "paint it", "model": {"base": base, "id": "m"},
                                              "effort": eff, "summary": SUMMARY})))
            assert r["ok"], r
            assert cap["body"]["reasoning_effort"] == want, (eff, cap["body"])
            assert cap["body"]["max_tokens"] == (1400 if eff == "thoughtful" else 900), cap["body"]
        _run(plugin_api.post_ask(req({"text": "paint it", "model": {"base": base, "id": "m"},
                                      "effort": "turbo", "summary": SUMMARY})))
        assert "reasoning_effort" not in cap["body"], cap["body"]  # junk effort -> auto
        _run(plugin_api.post_ask(req({"text": "paint it", "model": {"base": "http://127.0.0.1:65001/api",
                                      "id": "m"}, "effort": "fast", "summary": SUMMARY})))
        assert cap["body"].get("think") is False, cap["body"]  # ollama-native path
        # The actual Hermes inventory/resolver shape is slug=ollama-local with
        # an OpenAI-compatible /v1 base; it must still disable thinking.
        import hermes_cli.runtime_provider as rp
        orig_resolver = rp.resolve_runtime_provider
        rp.resolve_runtime_provider = lambda **kw: {
            "provider": "custom", "base_url": "http://127.0.0.1:11434/v1",
            "api_mode": "chat_completions", "api_key": ""}
        try:
            r = _run(plugin_api.post_ask(req({"text": "paint it", "model": {"slug": "ollama-local", "id": "m"},
                                              "effort": "fast", "summary": SUMMARY})))
            assert r["ok"] and cap["body"].get("think") is False, (r, cap["body"])
            assert cap["url"] == "http://127.0.0.1:11434/api/chat", cap["url"]
            assert cap["body"]["stream"] is False and cap["body"]["format"]["required"] == ["say", "ops"], cap["body"]
            assert cap["body"]["options"]["num_predict"] == 500, cap["body"]
        finally:
            rp.resolve_runtime_provider = orig_resolver
    finally:
        plugin_api._http_json = orig


def test_empty_reply_names_thinking_budget():
    orig = plugin_api._http_json
    plugin_api._http_json = lambda *a, **kw: {"choices": [{"message": {"content": ""}}]}
    try:
        r = _run(plugin_api.post_ask(req({"text": "paint it", "model": {"base": "http://127.0.0.1:65001/v1", "id": "m"},
                                          "effort": "balanced", "summary": SUMMARY})))
        assert not r["ok"] and "effort: fast" in r["error"] and "thinking" in r["error"], r
        assert "raw" not in r, r
    finally:
        plugin_api._http_json = orig


def test_model_inventory_parallel_and_fallback():
    import hermes_cli.inventory as inv
    orig_ctx, orig_build, orig_http = inv.load_picker_context, inv.build_model_options_payload, plugin_api._http_json
    ctx = SimpleNamespace(
        custom_providers=[{"provider_key": f"cold-{i}", "base_url": f"http://127.0.0.1:{65010 + i}/v1"} for i in range(3)],
        user_providers={},
    )
    lock = threading.Lock()
    active = 0
    max_active = 0

    def fake_http(url, payload=None, timeout=8.0, headers=None):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        # Also pin Ollama's native {models:[{name:...}]} catalog shape.
        return {"models": [] if "65012" in url else [{"name": "local-model"}]}

    inv.load_picker_context = lambda: ctx
    inv.build_model_options_payload = lambda *a, **kw: {
        "providers": [{"slug": f"cold-{i}", "name": f"Cold {i}", "models": []} for i in range(3)]}
    plugin_api._http_json = fake_http
    try:
        r = plugin_api.get_llm_models()
        assert max_active >= 2, f"cold probes ran serially (max_active={max_active})"
        assert r["inventory"] is True and r["lanes"][0]["models"] == ["local-model"], r
        assert r["lanes"][2]["dead"] is True, r
    finally:
        inv.load_picker_context, inv.build_model_options_payload, plugin_api._http_json = orig_ctx, orig_build, orig_http

    # Legacy ports are reached only when importing/using Hermes inventory
    # fails, and the cause must be surfaced for diagnosis.
    orig_import, orig_lanes, orig_http = builtins.__import__, plugin_api._CHAT_LANES, plugin_api._http_json
    def fail_inventory(name, *args, **kwargs):
        if name == "hermes_cli":
            raise ImportError("inventory unavailable")
        return orig_import(name, *args, **kwargs)
    builtins.__import__ = fail_inventory
    plugin_api._CHAT_LANES = (("fallback", "http://127.0.0.1:65500/v1"),)
    plugin_api._http_json = lambda *a, **kw: {"data": [{"id": "fallback-model"}]}
    try:
        r = plugin_api.get_llm_models()
        assert r["inventory"] is False and r["inventory_error"] == "ImportError", r
        assert r["lanes"][0]["models"] == ["fallback-model"], r
    finally:
        builtins.__import__, plugin_api._CHAT_LANES, plugin_api._http_json = orig_import, orig_lanes, orig_http


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
    # A9: the picker defaults to the model the app itself is using.
    assert "keep.models.includes(currentModel)" in pane
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
    # Busy UX is disabled in the pane, while the backend's third concurrent ask
    # refuses by name without sending bytes.
    assert "disabled: chatBusy || !chatIn.trim()" in pane
    assert plugin_api._CHAT_SEM.acquire(blocking=False)
    assert plugin_api._CHAT_SEM.acquire(blocking=False)
    try:
        r = _run(plugin_api.post_ask(req({"text": "paint it", "model": {"base": "http://127.0.0.1:65001/v1", "id": "m"},
                                          "summary": SUMMARY})))
        assert not r["ok"] and "2 asks already running" in r["error"], r
    finally:
        plugin_api._CHAT_SEM.release()
        plugin_api._CHAT_SEM.release()

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
check("extract_json survives fences + prose", test_extract)
check("reply text normalizes Responses + chat content blocks", test_reply_text)
check("whitelists mirror the validator (kinds/curves/waves)", test_mirrors)
check("op sanitizer: shapes, ranges, bool-rejects, unknown dropped", test_sanitize_ops)
check("edge sanitizer: move/when/watch/then, no nesting, ranges", test_sanitize_edges)
check("system prompt carries labels + rules", test_system_prompt)
check("/ask guards: empty, plaintext-remote, dead lane, bad summary, unknown slug", test_ask_guards)
check("/ask slug transport: resolver, responses API, key, headers/overrides, callable-token, guard", test_ask_slug_transport)
check("Responses SSE transport assembles subscription text deltas", test_responses_sse_parser)
check("chat profile lines: whole-record rendering, no mid-line truncation", test_chat_profile_lines)
check("/ask effort knob: clamped, per-lane translation, max_tokens", test_ask_effort)
check("empty model reply names the thinking-budget remedy", test_empty_reply_names_thinking_budget)
check("model inventory probes cold lanes in parallel; fallback is named", test_model_inventory_parallel_and_fallback)
check("redirects stay in the guard lane; credentials never cross origins", test_redirect_guard_strips_auth)
check("picker mirror: non-chat built-ins unselectable; current model forwarded", test_picker_mirror_drivable_and_current_model)
check("pane context/ambiguity/matrix/busy contracts; save_as collision refuses", test_pane_chat_contracts_and_save_collision)

print("FAILURES:", FAIL or "none")
sys.exit(1 if FAIL else 0)
