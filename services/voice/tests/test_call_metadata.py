"""s5 (ticket 05): what a retained Call document is allowed to say about itself.

Two claims, and only two, but they are the ones the ticket stands on:

1. **The Outlet is recorded, not inferred.** Ticket 16 made the Outlet a real dimension
   and each bridge resolves its own. The archive must carry THAT value. A row that says
   "phone" because the transport happened to be Twilio, or because the call was inbound,
   is a guess wearing a fact's clothes -- and it would be wrong the moment a third Outlet
   exists, or the moment Talk places an outbound call.

2. **A field nobody supplied is left out.** Not "", not "unknown", not 0. The reader and
   the screens have exactly one way to say "not recorded", and it is the absence of the
   key. This project has twice shipped a surface stating something untrue; both times the
   value came from a default that looked like data.

The Outlet claim is asserted against the PRODUCTION construction sites by AST, the same
way `test_s15b_lane_traversal` pins (mode, pipeline) -- reading back the value a test
passed in would prove only that the test passed it.
"""
import ast
from pathlib import Path

import pytest

from voicecore import call_record
from voicecore import eventlog
from voicecore import profiles

SERVICES = Path(__file__).resolve().parent.parent.parent

# Every production CallRecorder site, and the Outlet each one is entitled to declare.
# voice/server.py builds two (realtime + cascade), both on the phone Outlet.
PRODUCTION_OUTLETS = {
    "voice/server.py": {"OUTLET_PHONE"},
    "talk-voice-bridge/cascade_bridge.py": {"OUTLET_TALK"},
    "talk-voice-bridge/realtime_bridge.py": {"OUTLET_TALK"},
}


def _declared_outlets(rel_path):
    """The `outlet=` argument at every eventlog.CallRecorder(...) in a file.

    Returns the ATTRIBUTE NAME (``OUTLET_PHONE``), so a site that hard-codes the string
    "phone" instead of resolving `profiles.OUTLET_PHONE` shows up as something else and
    fails. The constants are the model ticket 16 landed; a literal would drift from it.
    """
    src = (SERVICES / rel_path).read_text()
    found = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name != "CallRecorder":
            continue
        outlet = {k.arg: k.value for k in node.keywords if k.arg}.get("outlet")
        if isinstance(outlet, ast.Attribute):
            found.add(outlet.attr)
        elif outlet is None:
            found.add("<missing>")
        else:
            found.add("<not a profiles constant>")
    return found


@pytest.mark.parametrize("rel_path,expected", sorted(PRODUCTION_OUTLETS.items()))
def test_every_production_writer_declares_its_own_outlet(rel_path, expected):
    assert _declared_outlets(rel_path) == expected, (
        f"{rel_path} declares outlet {_declared_outlets(rel_path)}, expected {expected}")


def test_the_outlet_scan_is_not_vacuous():
    for rel in PRODUCTION_OUTLETS:
        assert _declared_outlets(rel), f"scan found no CallRecorder in {rel}"


def test_the_declared_outlets_are_the_ones_the_model_has():
    """The scan's constants are real names on `profiles`, and cover every Outlet."""
    resolved = {getattr(profiles, name)
                for names in PRODUCTION_OUTLETS.values() for name in names}
    assert resolved == set(profiles.OUTLETS)


def test_the_outlet_reaches_the_record_on_disk(tmp_path):
    """The kwarg is not enough: the value has to appear in the row the dashboard reads."""
    path = tmp_path / "events.jsonl"
    eventlog.CallRecorder(call_id="c1", mode="talk", pipeline="realtime",
                          direction="inbound", outlet=profiles.OUTLET_TALK,
                          path=str(path)).finish(outcome="ok")
    import json
    row = json.loads(path.read_text().splitlines()[-1])
    assert row["outlet"] == "talk"


# -- rule 2: never guess a field ------------------------------------------------------


def test_unknown_fields_are_absent_not_defaulted():
    meta = call_record.build_metadata(platform="voice_twilio")
    assert meta == {"platform": "voice_twilio"}
    for field in ("outlet", "direction", "agent", "mission", "outcome", "duration_s",
                  "target", "caller", "timestamp"):
        assert field not in meta, f"{field} was invented"


def test_blank_and_whitespace_values_count_as_unknown():
    """An empty target is what an inbound call has. It must not become a key."""
    meta = call_record.build_metadata(platform="voice_talk", target="", caller="   ",
                                      agent=None, mission="")
    assert set(meta) == {"platform"}


def test_known_fields_are_all_recorded():
    meta = call_record.build_metadata(
        platform="voice_twilio", outlet="phone", direction="outbound",
        target="+61400000000", agent="hermes-main", mission="Book a table for 7pm.",
        outcome="ok", duration_s=63.25, started_at=1_787_000_000.0)
    assert meta["outlet"] == "phone"
    assert meta["direction"] == "outbound"
    assert meta["agent"] == "hermes-main"
    assert meta["mission"] == "Book a table for 7pm."
    assert meta["outcome"] == "ok"
    assert meta["duration_s"] == "63.2"          # one decimal, as a string; Hindsight
    assert meta["timestamp"].startswith("2026-")  # stringifies every value anyway
    assert meta["timestamp"].endswith("+00:00")   # UTC, stated, never a naive stamp


def test_a_long_mission_is_cut_and_the_cut_is_visible():
    """A silently shortened brief would read as the whole brief."""
    brief = "x" * (call_record.MISSION_MAX + 400)
    meta = call_record.build_metadata(platform="voice_twilio", mission=brief)
    assert len(meta["mission"]) <= call_record.MISSION_MAX + len(call_record.TRUNCATION_MARK)
    assert meta["mission"].endswith(call_record.TRUNCATION_MARK)


def test_a_mission_that_fits_is_not_marked():
    meta = call_record.build_metadata(platform="voice_twilio", mission="Call the vet.")
    assert meta["mission"] == "Call the vet."


def test_tags_carry_the_axes_and_never_an_empty_one():
    assert call_record.build_tags(lane="twilio", direction="inbound", outlet="phone",
                                  agent="hermes-main") == [
        "voice", "twilio", "inbound", "phone", "hermes-main"]
    # No agent, no outlet: no placeholder tags.
    assert call_record.build_tags(lane="talk", direction="inbound") == [
        "voice", "talk", "inbound"]
    # The Talk lane's lane word and its Outlet are the same word: tagged once.
    assert call_record.build_tags(lane="talk", direction="inbound", outlet="talk") == [
        "voice", "talk", "inbound"]
