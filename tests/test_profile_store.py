"""Profile store battery: refuse-closed validation, tiers, atomicity, import/export.

Standalone runner: python tests/test_profile_store.py
Runs against a throwaway LOCALAPPDATA so the real user tier is never touched.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "dashboard"))

# Redirect the user tier BEFORE importing the store (data_dir reads env per call).
_TMP = tempfile.mkdtemp(prefix="mssc-test-")
os.environ["LOCALAPPDATA"] = _TMP

import profile_store  # noqa: E402


def good_profile(pid="unit-test"):
    return {
        "schema_version": 1,
        "profile_id": pid,
        "device": "Test Device",
        "controls": {
            "a": {"label": "A", "cc": 1, "widget": "knob", "x": 0, "y": 0},
            "b": {"label": "B", "cc": 2, "widget": "toggle", "kind": "toggle", "x": 1, "y": 0},
        },
    }


def expect_err(p, needle):
    errs = profile_store.validate_profile(p)
    assert errs and any(needle in e for e in errs), \
        f"expected error containing {needle!r}, got {errs}"


def main() -> int:
    # 1. CURVES mirror law: profile_store.CURVES == modmath.CURVES keys
    import modmath
    assert profile_store.CURVES == set(modmath.CURVES), \
        f"CURVES must all derive from modmath (single source): {profile_store.CURVES} vs {set(modmath.CURVES)}"

    # 2. every bundled profile validates (CI-style law)
    for row in profile_store.list_profiles():
        assert row["tier"] == "bundled" and row["valid"], f"bundled {row} invalid"

    # 3. flagship profile: 21 controls, unique CCs, sections referenced
    mf = profile_store.load_profile("microfreak")
    assert len(mf["controls"]) == 21, f"expected 21 controls, got {len(mf['controls'])}"
    ccs = [c["cc"] for c in mf["controls"].values()]
    assert len(set(ccs)) == len(ccs), "duplicate CC in microfreak profile"
    assert mf["match"]["name_contains"] == "MicroFreak"
    assert profile_store.control_cc_map("microfreak")[23] == "cutoff"

    # 4. refuse-closed validation matrix
    p = good_profile(); p["controls"]["a"]["cc"] = 128
    expect_err(p, "cc must be int 0-127")
    p = good_profile(); p["controls"]["b"]["cc"] = 1
    expect_err(p, "already used by")
    p = good_profile(); p["controls"]["a"]["widget"] = "dial"
    expect_err(p, "widget must be")
    p = good_profile(); p["typo_field"] = 1
    expect_err(p, "unknown top-level key")
    p = good_profile(); p["controls"]["a"]["display"] = {"curve": "bogus"}
    expect_err(p, "display.curve invalid")
    p = good_profile(); p["matrix"] = [{"source": "a", "target": "ghost"}]
    expect_err(p, "is not a control")
    p = good_profile(); p["controls"]["a"]["section"] = "nosection"
    expect_err(p, "section")
    p = good_profile(); p["schema_version"] = 99
    expect_err(p, "schema_version")
    # Multi-matrix law: lanes carry their own oscillator; junk refused.
    p = good_profile()
    p["matrix"] = [{"source": "lfo", "target": "a", "depth": 0.5,
                    "wave": "tri", "rate_hz": 4.0}]
    assert profile_store.validate_profile(p) == [], \
        f"per-lane wave/rate must validate, got {profile_store.validate_profile(p)}"
    p = good_profile(); p["matrix"] = [{"source": "lfo", "target": "a", "wave": "square"}]
    expect_err(p, "wave must be one of")
    p = good_profile(); p["matrix"] = [{"source": "lfo", "target": "a", "rate_hz": 50}]
    expect_err(p, "rate_hz must be a number in 0.01..10")
    # Full-spectrum law (owner ruling): colors are #rrggbb hex (hue wheel in
    # the pane), never a token set; junk refused.
    p = good_profile(); p["controls"]["a"]["color"] = "#12ab34"
    assert profile_store.validate_profile(p) == [], \
        f"hex color must validate, got {profile_store.validate_profile(p)}"
    p = good_profile(); p["controls"]["a"]["color"] = "teal"
    expect_err(p, "color must be")
    p = good_profile(); p["controls"]["a"]["color"] = "#ff00"
    expect_err(p, "color must be")
    # Category styling (owner ask): label size/bold/color/order on sections.
    p = good_profile()
    styled = {"label": "S1", "color": "#ff8800", "bg": "#102030", "font_size": 16, "bold": True, "order": 2, "row": 2}
    p["sections"] = {"s1": styled}
    assert profile_store.validate_profile(p) == [], \
        f"section styling must validate, got {profile_store.validate_profile(p)}"
    p = good_profile(); p["sections"] = {"s1": {"label": "S1", "row": "top"}}
    assert any("row must be int 0-99" in e for e in profile_store.validate_profile(p)), "bad row refused"
    p = good_profile(); p["controls"]["a"]["name_color"] = "#12ab34"
    p["controls"]["a"]["box_color"] = "#ff0000"
    assert profile_store.validate_profile(p) == [], \
        f"part colors must validate, got {profile_store.validate_profile(p)}"
    p = good_profile(); p["controls"]["a"]["name_color"] = "red"
    expect_err(p, "name_color must be")
    p = good_profile(); p["controls"]["a"]["box_color"] = "#ff00"
    expect_err(p, "box_color must be")
    p = good_profile(); p["sections"] = {"s1": {"label": "S1", "font_size": 200}}
    expect_err(p, "font_size must be int 8-48")
    p = good_profile(); p["sections"] = {"s1": {"label": "S1", "bold": "yes"}}
    expect_err(p, "bold must be a boolean")
    p = good_profile(); p["sections"] = {"s1": {"label": "S1", "color": "neon"}}
    expect_err(p, "color must be")
    p = good_profile(); p["sections"] = {"s1": {"label": "S1", "bg": "neon"}}
    expect_err(p, "bg must be")
    p = good_profile(); p["sections"] = {"s1": {"label": "S1", "typo": 1}}
    expect_err(p, "unknown key")
    # regression hardening (2026-11-07): empty controls legal (delete-last works),
    # wrong-typed fields give named errors not TypeError, ids are path-contained.
    p = good_profile(); p["controls"] = {}
    assert profile_store.validate_profile(p) == [], \
        f"empty controls must validate, got {profile_store.validate_profile(p)}"
    p = good_profile(); p["controls"]["a"]["widget"] = ["knob"]
    expect_err(p, "widget must be")
    p = good_profile(); p["controls"]["a"]["section"] = ["s1"]
    expect_err(p, "section")
    for evil in ("../evil", "..", "a/b", ""):
        for fn in (profile_store.load_profile, profile_store.export_profile):
            try:
                fn(evil)
                raise AssertionError(f"path escape not refused: {fn.__name__}({evil!r})")
            except ValueError:
                pass
    # #14 (): validator TOTALITY — unhashable JSON returns named
    # errors, never a TypeError crash (a crash used to take profile listing down).
    p = good_profile(); p["controls"]["a"]["display"] = {"domain": []}
    expect_err(p, "display.domain invalid")
    p = good_profile(); p["controls"]["a"]["display"] = {"curve": ["lin"]}
    expect_err(p, "display.curve invalid")
    p = good_profile(); p["matrix"] = [{"source": ["a"], "target": "b"}]
    expect_err(p, "is not a control or 'lfo'")
    # ranges are enforced against the wire domain, min:-200/max:999 refused
    p = good_profile(); p["controls"]["a"]["min"] = -200; p["controls"]["a"]["max"] = 999
    expect_err(p, "need ints 0 <= min < max <= 127")
    # stepped label keys must be numeric CC positions inside min..max
    p = good_profile(); p["controls"]["a"]["labels"] = {"oops": "Oops"}
    expect_err(p, "label key")
    p = good_profile(); p["controls"]["a"]["labels"] = {"300": "far"}
    expect_err(p, "outside min..max")
    # aliases must resolve to real controls
    p = good_profile(); p["aliases"] = ["osc"]
    expect_err(p, "aliases must be an object")
    p = good_profile(); p["aliases"] = {"ghost": "nope"}
    expect_err(p, "points at unknown control")
    assert profile_store.validate_profile(good_profile()) == []

    # 5. save/load round trip + atomicity (no .tmp litter)
    path = profile_store.save_profile(good_profile())
    assert Path(path).is_file() and Path(path).parent == profile_store.user_profiles_dir()
    assert not list(Path(path).parent.glob("*.tmp")), "temp litter left behind"
    loaded = profile_store.load_profile("unit-test")
    assert loaded["controls"]["a"]["cc"] == 1

    # 6. user tier overrides bundled by id
    mf2 = json.loads(json.dumps(mf))
    mf2["controls"]["cutoff"]["label"] = "CUTOFF-OVERRIDE"
    profile_store.save_profile(mf2)
    assert profile_store.load_profile("microfreak")["controls"]["cutoff"]["label"] == "CUTOFF-OVERRIDE"
    assert profile_store.delete_profile("microfreak") is True
    assert profile_store.load_profile("microfreak")["controls"]["cutoff"]["label"] == "Cutoff"

    # 7. import/export round trip
    text = profile_store.export_profile("microfreak")
    profile_store.import_profile(text, new_id="mf-copy")
    assert profile_store.load_profile("mf-copy")["device"] == "Arturia MicroFreak"
    bad = json.loads(json.dumps(mf2)); bad["controls"]["x"] = {"label": ""}
    try:
        profile_store.import_profile(json.dumps(bad))
        raise AssertionError("import accepted invalid profile")
    except ValueError:
        pass

    # 8. active profile: set/get, refuse-closed on activate
    assert profile_store.get_active_profile_id() is None
    profile_store.set_active_profile_id("microfreak")
    assert profile_store.get_active_profile_id() == "microfreak"
    try:
        profile_store.set_active_profile_id("nope")
        raise AssertionError("activated nonexistent profile")
    except KeyError:
        pass
    profile_store.set_active_profile_id(None)
    assert profile_store.get_active_profile_id() is None
    # deleting the ACTIVE profile must clear the marker (no dangling dead id)
    dp = json.loads(json.dumps(mf2)); dp["profile_id"] = "doomed"
    profile_store.save_profile(dp)
    profile_store.set_active_profile_id("doomed")
    assert profile_store.delete_profile("doomed")
    assert profile_store.get_active_profile_id() is None, \
        "delete of active profile must not leave a dangling marker"

    # 9. invalid user file is listed with errors, never half-loaded
    d = profile_store.user_profiles_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "broken.json").write_text("{ this is not json", encoding="utf-8")
    rows = {r["profile_id"]: r for r in profile_store.list_profiles()}
    assert rows["broken"]["valid"] is False and rows["broken"]["errors"]

    # 10. wire-remap law (the card-row UI writes exactly these keys):
    # invert bool; out_min/out_max ints 0<=min<max<=127; one-sided is junk.
    p = good_profile("wire-law")
    p["controls"]["a"]["invert"] = True
    assert profile_store.validate_profile(p) == []
    p["controls"]["a"]["invert"] = "yes"
    expect_err(p, "invert must be a bool")
    p["controls"]["a"]["invert"] = True
    p["controls"]["a"]["out_min"] = 20
    p["controls"]["a"]["out_max"] = 100
    assert profile_store.validate_profile(p) == []
    p["controls"]["a"]["out_max"] = 20
    expect_err(p, "out_min/out_max")
    p["controls"]["a"]["out_max"] = "100"
    expect_err(p, "out_min/out_max")
    del p["controls"]["a"]["out_max"]
    expect_err(p, "out_min/out_max")  # one-sided range is junk, refused

    # 10b. a hand-malformed controls value must show up as an
    # invalid ROW, never crash list_profiles (one bad file used to 500 the
    # whole store view — the exact bug the validator was supposed to prevent).
    (d := profile_store.user_profiles_dir()).mkdir(parents=True, exist_ok=True)
    (d / "junkctrl.json").write_text('{"schema_version": 1, "profile_id": "junkctrl", "device": "x", "controls": 5}', encoding="utf-8")
    rows = {rr["profile_id"]: rr for rr in profile_store.list_profiles()}
    assert rows["junkctrl"]["valid"] is False and rows["junkctrl"]["controls"] == 0, rows["junkctrl"]
    (d / "junkctrl.json").unlink()

    # 10b'. B1 (2026-11-10): grid numbers were unbounded —
    # x:1000000 / order:±1000000 validated and the renderer then tried to
    # create a million grid cells synchronously (workspace freeze). Every
    # grid coordinate is now bounded: order/row/x/y 0-99, w 1-8, h 1-8.
    p = good_profile("bounds-ok")
    p["sections"] = {"s1": {"label": "S", "row": 0, "order": 99}}
    p["controls"]["a"].update({"x": 99, "y": 0, "w": 8, "h": 8})
    assert profile_store.validate_profile(p) == [], profile_store.validate_profile(p)
    p = good_profile("bounds-order")
    p["sections"] = {"s1": {"label": "S", "order": 1000000}}
    expect_err(p, "order must be int 0-99")
    p = good_profile("bounds-order-neg")
    p["sections"] = {"s1": {"label": "S", "order": -5}}
    expect_err(p, "order must be int 0-99")
    p = good_profile("bounds-x")
    p["controls"]["a"]["x"] = 1000000
    expect_err(p, "x must be int 0-99")
    p = good_profile("bounds-w")
    p["controls"]["a"]["w"] = 9
    expect_err(p, "w must be int 1-8")
    p = good_profile("bounds-h")
    p["controls"]["a"]["h"] = 9
    expect_err(p, "h must be int 1-8")

    # 10c. B2 (2026-11-10): collisions used to be VALID —
    # the renderer then silently ate the covered control. Footprint overlap
    # AND two categories on one band/slot are refused at the door now.
    p = good_profile("collide-cell")
    p["controls"]["b"]["x"] = 0
    expect_err(p, "overlaps")
    p = good_profile("collide-span")
    p["controls"]["a"]["w"] = 4
    p["controls"]["b"]["x"] = 2
    expect_err(p, "overlaps")
    p = good_profile("collide-touch")
    p["controls"]["a"]["w"] = 4
    p["controls"]["b"]["x"] = 4   # ends right where 'b' starts — legal, gaps stay real
    assert profile_store.validate_profile(p) == [], profile_store.validate_profile(p)
    p = good_profile("collide-sec")
    p["sections"] = {"s1": {"label": "One"}, "s2": {"label": "Two"}}
    expect_err(p, "same band/slot")

    # 11. regression review bug 1 (2026-11-10): wire transforms belong to the DECK,
    # active first, first-wins. An inactive bystander must never overwrite
    # the active profile's transform for a shared CC; a profile outside the
    # deck drives nothing and never touches the wire.
    pa = good_profile("wire-a")
    pb = good_profile("wire-b")
    pa["controls"]["a"]["invert"] = True
    pb["controls"]["a"]["out_min"] = 20
    pb["controls"]["a"]["out_max"] = 100
    profile_store.save_profile(pa)
    profile_store.save_profile(pb)
    profile_store.set_deck(["wire-b", "wire-a"])   # b active: its 20..100 remap wins
    assert profile_store.wire_xform("primary", 1, 127) == 100, profile_store.wire_maps()
    profile_store.set_deck(["wire-a", "wire-b"])   # a active: its inversion wins
    assert profile_store.wire_xform("primary", 1, 20) == 107, profile_store.wire_maps()
    pc = good_profile("wire-c")                    # NOT in the deck
    pc["controls"]["b"]["invert"] = True
    profile_store.save_profile(pc)
    assert profile_store.wire_xform("primary", 2, 10) == 10, "non-deck profile touched the wire"
    profile_store.delete_profile("wire-a")
    profile_store.delete_profile("wire-b")
    profile_store.delete_profile("wire-c")

    print("ALL OK — profile store battery passed (curves mirror, bundled CI, 21-ctrl flagship, "
          "8 refuse-closed cases, tiers+override, atomic save, import/export, active law, wire-remap law, deck-priority wire)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
