"""Loopback battery: winmm send->echo through a loopMIDI port.

Standalone runner: python tests/test_loopback.py
Skips (exit 0) when no loopMIDI port is present; fails loudly when one is
present but the round trip breaks. Requires loopMIDI.exe running.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import midi_core  # noqa: E402


def main() -> int:
    devs = midi_core.list_devices()
    names = [d["name"] for d in devs if d["dir"] == "in"]
    if not any(n.lower().startswith("loopmidi") for n in names):
        print("SKIP — no loopMIDI port on this machine")
        return 0
    r = midi_core.BUS.connect("loopMIDI Port")
    assert r.get("ok"), f"connect failed: {r}"
    assert r.get("mirror"), "loopMIDI in not opened — mirror path untested"
    try:
        # One resend is allowed: MIDI routers intermittently drop the very first
        # message after a port (re)open; the battery tests OUR path, not the
        # router's first-message luck.
        sent = 0
        cc7 = []
        st = {}
        for _attempt in range(2):
            sent += 1
            midi_core.BUS.send(kind="cc", cc=7, value=99, source="probe")
            time.sleep(0.5)
            st = midi_core.BUS.status()
            cc7 = [c for c in st["cc"] if c["cc"] == 7]
            if cc7 and cc7[0]["value"] == 99 and cc7[0]["source"] == "echo":
                break
        assert cc7 and cc7[0]["value"] == 99, f"CC7 echo missing/wrong: {cc7}"
        # #11 (): our OWN send's loopback echo is correlated and
        # tagged "echo" — never "panel". Only traffic we did not send is panel.
        assert cc7[0]["source"] == "echo", f"expected correlated echo, got {cc7[0]['source']}"
        # #2: every event carries a monotonic seq cursor for arm-at-cursor flows
        assert isinstance(st.get("event_seq"), int) and st["event_seq"] > 0, st.get("event_seq")
        evs = [e for e in st["events"] if e.get("kind") == "cc" and e.get("cc") == 7]
        assert evs and all(isinstance(e.get("seq"), int) for e in evs), f"events lack seq: {evs}"
        assert evs[-1]["source"] == "echo", evs[-1]
        # own echoes must not sit in panel state (the REST /state learn feed
        # filters on source == "panel" — this pins the provenance underneath)
        cc7b = [c for c in midi_core.BUS.status()["cc"] if c["cc"] == 7]
        assert cc7b and cc7b[0]["source"] == "echo", f"own echo in panel state: {cc7b}"
        midi_core.BUS.send(kind="note_on", note=60, velocity=100, source="probe")
        time.sleep(0.5)
        st = midi_core.BUS.status()
        # Hardened (regression review): the note must be proven by a RECEIVED event,
        # not by shadow state our own send writes anyway. #11: that received
        # echo of OUR note is tagged "echo".
        recv = [e for e in st["events"]
                if e.get("kind") == "note" and e.get("note") == 60
                and e.get("source") == "echo" and e.get("velocity")]
        assert recv, f"note_on echo event missing: {st['events'][-6:]}"
        assert any(n["note"] == 60 and n["channel"] == 0 for n in st["notes"]), f"note state missing: {st['notes']}"
        midi_core.BUS.send(kind="note_off", note=60, source="probe")
        time.sleep(0.4)
        st = midi_core.BUS.status()
        recv = [e for e in st["events"]
                if e.get("kind") == "note" and e.get("note") == 60
                and e.get("source") == "echo" and not e.get("velocity")]
        assert recv, "note_off echo event missing"
        assert not any(n["note"] == 60 for n in st["notes"]), "note_off echo did not clear"
        # #11 second half: traffic we did NOT send stays "panel" (the real
        # hardware-knob path). Open a second out client on the same port —
        # winmm allows many out clients; only in-clients are exclusive.
        out_idx = next(d["index"] for d in midi_core.list_devices()
                       if d["dir"] == "out" and d["name"] == r["device"]["out"])
        ext = midi_core._MidiOut(out_idx)
        ext.open()
        try:
            ext.send_short(0xB0 | (42 << 8) | (77 << 16))  # CC 42 ch0 = 77, external
            time.sleep(0.5)
        finally:
            ext.close()
        st = midi_core.BUS.status()
        panel42 = [e for e in st["events"]
                   if e.get("kind") == "cc" and e.get("cc") == 42 and e.get("source") == "panel"]
        assert panel42, f"external CC42 not tagged panel: {st['events'][-6:]}"
        ent42 = [c for c in st["cc"] if c["cc"] == 42]
        assert ent42 and ent42[0]["source"] == "panel", \
            f"external panel CC42 state provenance wrong: {ent42}"
        # #5: panic = all notes off on every channel + release what we held
        midi_core.BUS.send(kind="note_on", note=72, velocity=90, source="probe")
        time.sleep(0.3)
        pr = midi_core.BUS.panic()
        assert pr.get("ok"), pr
        assert pr.get("all_notes_off_channels") == 16, pr
        time.sleep(0.4)
        assert not any(n["note"] == 72 for n in midi_core.BUS.status()["notes"]), \
            "panic did not release our held note"
        # audit trail law: every send is journaled in the plugin data dir
        # (single-source with profiles/exports — not a stale dir name).
        p = midi_core.BUS.sendlog_path()
        assert p.parent.name == "z-midi-console", f"sendlog dir: {p}"
        tail = p.read_text(encoding="utf-8", errors="replace")[-16000:]
        rows = [x for x in tail.splitlines() if '"probe"' in x and '"cc": 7' in x]
        assert rows, f"probe CC7 not journaled in {p}"
        # Wire-remap pin (LMMS-borrow batch, 2026-11-10): the shadow and
        # everything above it speak SURFACE; the physical wire carries the
        # invert/out-range transform both ways.
        import profile_store
        profile_store._WIRE_CACHE["m"] = {"primary": {7: (True, 0, 127),
                                                      45: (False, 20, 100)}}
        try:
            midi_core.BUS.send(kind="cc", cc=7, value=99, source="probe")
            time.sleep(0.4)
            rec = [s for s in list(midi_core.BUS._recent_sends)
                   if s[0] == "cc" and s[2] == 7][-1]
            assert rec[3] == 28, f"wire must carry inverted 28, correlator says {rec}"
            ent = [c for c in midi_core.BUS.status()["cc"] if c["cc"] == 7][0]
            assert ent["value"] == 99, f"shadow must stay SURFACE (99), got {ent}"
            # physical inbound on a transformed cc lands UNWIREd (panel path)
            ext2 = midi_core._MidiOut(out_idx)
            ext2.open()
            try:
                ext2.send_short(0xB0 | (45 << 8) | (60 << 16))  # wire 60 -> surface ~64
                time.sleep(0.4)
            finally:
                ext2.close()
            ent45 = [c for c in midi_core.BUS.status()["cc"] if c["cc"] == 45]
            assert ent45 and abs(ent45[0]["value"] - 64) <= 1, \
                f"panel wire 60 on out_min20/out_max100 must surface ~64, got {ent45}"
        finally:
            profile_store.invalidate_wire_maps()
        # #17: rotation — past 8 MB the journal keeps the tail, never grows forever
        big = ('{"k": "rotate-probe", "pad": "' + "x" * 60 + '"}\n') * 130000
        p.write_text(big, encoding="utf-8")
        last = big.splitlines()[-1]
        midi_core.BUS._rotate_sendlog()
        sz = p.stat().st_size
        assert sz < 1_500_000, f"sendlog not rotated: {sz} B"
        tail2 = p.read_text(encoding="utf-8").splitlines()
        assert tail2[-1] == last, "rotation lost the newest line"
        assert all(x.startswith('{"k"') for x in tail2[:3]), "rotation left a partial line"
    finally:
        midi_core.BUS.disconnect()
    print("ALL OK — winmm loopback battery passed (cc round trip, echo correlation, panel provenance, seq cursor, panic, note on/off)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
