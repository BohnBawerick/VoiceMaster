"""s15d: the REAL CascadeBridge constructor must run.

s14b call 7 (the first ever Talk cascade dial) rang the owner's client and then died in
bridge setup with:

    AttributeError: 'OutboundMission' object has no attribute 'to'

`cascade_bridge.py` built its CallRecorder from `mission.to`. OutboundMission has never
had a `to` field (brief / report_channel / report_address / target_display). So the
cascade lane could not construct — s12b code that had never once run end-to-end.

Why the suite missed it, AGAIN: `test_s12b_dispatch.py` monkeypatches
`session_mod.cascade_bridge.CascadeBridge` with a fake class to test dispatch, and
`test_s12b_cascade_bridge.py` exercises PacatWire. Between them the REAL constructor was
never executed. Same shape as the two bugs before it — the fakes agreed with the code
instead of with reality.

These tests build the real object.
"""
import pytest

import cascade_bridge
import config
from voicecore import profiles
from outbound import OutboundMission

CASCADE_DOC = {
    "id": "supplier-caller",
    "pipeline": "cascade",
    "providers": {"stt": "deepgram", "llm": "gpt-4.1", "tts": "elevenlabs"},
    "knobs": {"vad": {"silence_ms": 250}},
}

ENV = {"DEEPGRAM_API_KEY": "dg-test", "OPENAI_API_KEY": "sk-test",
       "ELEVENLABS_API_KEY": "el-test"}


def _profile():
    registry = profiles.load_registry(profiles.config_dir(ENV))
    return profiles.ActiveProfile(agent_id="supplier-caller", source="s15d-test",
                                  doc=CASCADE_DOC, registry=registry)


def _mission():
    return OutboundMission(brief="confirm stock and lead time",
                           target_display="Sam")


def test_the_real_cascade_bridge_constructs():
    """RED before the fix: AttributeError: 'OutboundMission' object has no attribute 'to'."""
    bridge = cascade_bridge.CascadeBridge(config.load_base(), _profile(), _mission(),
                                          env=ENV, token="room8tok")
    assert bridge is not None


def test_call_id_is_the_room_token_matching_the_realtime_talk_lane():
    """The Talk lane keys eventlog rows by ROOM TOKEN (realtime_bridge does the same, and
    s14b confirmed the token is the only genuine join key on this transport). Cascade must
    not invent a second convention — a lane whose rows cannot be joined to the call is a
    lane we cannot grade."""
    bridge = cascade_bridge.CascadeBridge(config.load_base(), _profile(), _mission(),
                                          env=ENV, token="room8tok")
    assert bridge._recorder.call_id == "room8tok"


def test_recorder_carries_the_cascade_lane_identity():
    """s14b scored a rejoined realtime call as a cascade pass. `pipeline` on the row is
    the field that catches that, so pin it at construction."""
    bridge = cascade_bridge.CascadeBridge(config.load_base(), _profile(), _mission(),
                                          env=ENV, token="room8tok")
    rec = bridge._recorder
    assert rec.mode == "talk"
    assert rec.pipeline == "cascade"
    assert rec.direction == "outbound"
    assert rec.target == "Sam"


def test_mission_has_no_to_attribute():
    """Pin the premise, so a future refactor that adds `to` does not silently re-diverge
    the two lanes' call_id conventions."""
    assert not hasattr(_mission(), "to")
