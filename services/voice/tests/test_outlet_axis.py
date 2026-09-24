"""s16 tests, Mode C: the outlet axis in active.yaml.

The shared ``voicecore.profiles`` resolution matrix (the canonical shape, the
refusal of the deleted flat shape, per-outlet loud failure) plus the phone
bridge's own wiring:
media_stream resolves the PHONE outlet, never the talk outlet, even when the file
assigns different agents to the two.

The Talk bridge's copy of the call-path wiring lives in the talk suite
(``talk-voice-bridge/tests/test_outlet_axis.py``); the resolution matrix itself is
NOT duplicated there - both suites import the same module, and one copy of the
matrix is the point of the shared package (VC17).
"""
import pytest
import yaml
from fastapi.testclient import TestClient

from voicecore import profiles
import server
from conftest import FakeOpenAIWS
from profile_helpers import profile_doc, write_config_dir

PHONE_MODEL = "gpt-realtime-probe-phone"
TALK_MODEL = "gpt-realtime-probe-talk"


def _agent(aid, voice, model, **overrides):
    return profile_doc(id=aid, knobs={"voice": voice, "model": model}, **overrides)


def _write_outlet_shape(d, *, phone=None, talk=None, extra=None, raw=None):
    doc = {"outlets": {}}
    if phone is not None:
        doc["outlets"]["phone"] = phone
    if talk is not None:
        doc["outlets"]["talk"] = talk
    if extra is not None:
        doc["outlets"].update(extra)
    (d / "active.yaml").write_text(
        raw if raw is not None else yaml.safe_dump(doc))


def _write_flat_shape(d, inbound=None, outbound=None):
    """The shape the old Agents screen used to write: a direction and no Outlet.
    Deleted with s17; kept here only so the tests can prove it lands nowhere."""
    (d / "active.yaml").write_text(yaml.safe_dump(
        {"inbound": inbound, "outbound": outbound}))


def _point_env(monkeypatch, d, voice_agent=None):
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(d))
    if voice_agent is None:
        monkeypatch.delenv("VOICE_AGENT", raising=False)
    else:
        monkeypatch.setenv("VOICE_AGENT", voice_agent)


# ---------------------------------------------------------------------------
# Resolution matrix: the new canonical shape
# ---------------------------------------------------------------------------

def test_new_shape_resolves_each_outlet_independently(tmp_path, monkeypatch):
    d = write_config_dir(tmp_path, [_agent("phone-agent", "marin", PHONE_MODEL),
                                    _agent("talk-agent", "cedar", TALK_MODEL)])
    _write_outlet_shape(d,
                        phone={"inbound": "phone-agent", "outbound": "phone-agent"},
                        talk={"inbound": "talk-agent", "outbound": "talk-agent"})
    _point_env(monkeypatch, d)
    p = profiles.load_effective_profile("inbound", outlet=profiles.OUTLET_PHONE)
    t = profiles.load_effective_profile("inbound", outlet=profiles.OUTLET_TALK)
    assert p.agent_id == "phone-agent" and p.persona == ""
    assert t.agent_id == "talk-agent"
    assert profiles.load_effective_profile(
        "outbound", outlet=profiles.OUTLET_PHONE).agent_id == "phone-agent"
    assert profiles.load_effective_profile(
        "outbound", outlet=profiles.OUTLET_TALK).agent_id == "talk-agent"


def test_new_shape_partial_file_slots_are_independently_unset(tmp_path, monkeypatch):
    d = write_config_dir(tmp_path, [_agent("phone-agent", "marin", PHONE_MODEL)])
    _write_outlet_shape(d, phone={"inbound": "phone-agent"})  # talk absent entirely
    _point_env(monkeypatch, d)
    assert profiles.load_effective_profile(
        "inbound", outlet=profiles.OUTLET_PHONE).agent_id == "phone-agent"
    assert profiles.load_effective_profile(
        "outbound", outlet=profiles.OUTLET_PHONE) is None
    assert profiles.load_effective_profile(
        "inbound", outlet=profiles.OUTLET_TALK) is None
    assert profiles.load_effective_profile(
        "outbound", outlet=profiles.OUTLET_TALK) is None


def test_new_shape_reads_null_explicitly(tmp_path, monkeypatch):
    d = write_config_dir(tmp_path, [_agent("phone-agent", "marin", PHONE_MODEL)])
    _write_outlet_shape(d, phone={"inbound": "phone-agent", "outbound": None})
    _point_env(monkeypatch, d)
    assert profiles.load_effective_profile(
        "outbound", outlet=profiles.OUTLET_PHONE) is None
    assert profiles.load_effective_profile(
        "inbound", outlet=profiles.OUTLET_PHONE).agent_id == "phone-agent"


# ---------------------------------------------------------------------------
# s17: the flat shape lands NOWHERE
#
# A top-level direction names no Outlet, so the only thing it could ever mean
# was "every Outlet at once" - which is exactly how one click on the old Agents
# screen silently undid a per-outlet split. The loader now refuses the shape
# instead of reading past it. Refusing beats ignoring: ignoring would turn a
# file that used to route calls into "nothing is assigned" with nobody told.
# ---------------------------------------------------------------------------

def test_a_flat_file_is_refused_on_every_outlet(tmp_path, monkeypatch):
    d = write_config_dir(tmp_path, [_agent("phone-agent", "marin", PHONE_MODEL)])
    _write_flat_shape(d, inbound="phone-agent", outbound="phone-agent")
    _point_env(monkeypatch, d)
    for outlet in profiles.OUTLETS:
        for direction in profiles.ACTIVE_DIRECTIONS:
            with pytest.raises(profiles.ProfileError) as exc:
                profiles.load_effective_profile(direction, outlet=outlet)
            # the refusal must teach the shape, not just say no
            assert "outlets" in str(exc.value)
            assert "phone" in str(exc.value) and "talk" in str(exc.value)


def test_a_flat_key_is_refused_even_beside_a_valid_outlets_map(tmp_path, monkeypatch):
    """The loophole an 'ignore it' rule would leave open: append the flat keys to
    a healthy file and they are quietly dropped, so the writer believes it
    assigned something. Nothing may read past a flat key."""
    d = write_config_dir(tmp_path, [_agent("phone-agent", "marin", PHONE_MODEL),
                                    _agent("talk-agent", "cedar", TALK_MODEL)])
    _write_outlet_shape(d,
                        phone={"inbound": "phone-agent"},
                        talk={"inbound": "talk-agent"})
    (d / "active.yaml").write_text(
        (d / "active.yaml").read_text() + "inbound: talk-agent\noutbound: talk-agent\n")
    _point_env(monkeypatch, d)
    for outlet in profiles.OUTLETS:
        with pytest.raises(profiles.ProfileError):
            profiles.load_effective_profile("inbound", outlet=outlet)


def test_an_unknown_top_level_key_is_still_ignored(tmp_path, monkeypatch):
    """Negative control for the two above: the refusal is aimed at the flat
    ASSIGNMENT keys, not at any stray key. Without this, a rule that rejected
    every unknown key would pass them both and break forward compatibility."""
    d = write_config_dir(tmp_path, [_agent("phone-agent", "marin", PHONE_MODEL)])
    _write_outlet_shape(d, phone={"inbound": "phone-agent"})
    (d / "active.yaml").write_text(
        (d / "active.yaml").read_text() + "mystery: 7\n")
    _point_env(monkeypatch, d)
    assert profiles.load_effective_profile(
        "inbound", outlet=profiles.OUTLET_PHONE).agent_id == "phone-agent"


# ---------------------------------------------------------------------------
# Loud failure is per outlet+direction - never a silent fallback
# ---------------------------------------------------------------------------

def test_bad_slot_fails_loud_only_for_that_outlet(tmp_path, monkeypatch):
    d = write_config_dir(tmp_path, [_agent("phone-agent", "marin", PHONE_MODEL)])
    _write_outlet_shape(d,
                        phone={"inbound": "phone-agent"},
                        talk={"inbound": "[not, a, string]"})
    _point_env(monkeypatch, d)
    with pytest.raises(profiles.ProfileError) as exc:
        profiles.load_effective_profile("inbound", outlet=profiles.OUTLET_TALK)
    assert "outlets.talk.inbound" in str(exc.value)
    assert profiles.load_effective_profile(
        "inbound", outlet=profiles.OUTLET_PHONE).agent_id == "phone-agent"


def test_missing_agent_fails_loud_only_for_that_outlet(tmp_path, monkeypatch):
    d = write_config_dir(tmp_path, [_agent("phone-agent", "marin", PHONE_MODEL)])
    _write_outlet_shape(d,
                        phone={"inbound": "phone-agent"},
                        talk={"inbound": "ghost-agent"})
    _point_env(monkeypatch, d)
    with pytest.raises(profiles.ProfileError) as exc:
        profiles.load_effective_profile("inbound", outlet=profiles.OUTLET_TALK)
    assert "ghost-agent" in str(exc.value) and "not found" in str(exc.value)
    assert profiles.load_effective_profile(
        "inbound", outlet=profiles.OUTLET_PHONE).agent_id == "phone-agent"


def test_disabled_agent_fails_loud_only_for_that_outlet(tmp_path, monkeypatch):
    d = write_config_dir(tmp_path, [_agent("phone-agent", "marin", PHONE_MODEL),
                                    _agent("off-agent", "cedar", TALK_MODEL,
                                           enabled=False)])
    _write_outlet_shape(d,
                        phone={"inbound": "phone-agent"},
                        talk={"inbound": "off-agent"})
    _point_env(monkeypatch, d)
    with pytest.raises(profiles.ProfileError) as exc:
        profiles.load_effective_profile("inbound", outlet=profiles.OUTLET_TALK)
    assert "off-agent" in str(exc.value) and "enabled" in str(exc.value)
    assert profiles.load_effective_profile(
        "inbound", outlet=profiles.OUTLET_PHONE).agent_id == "phone-agent"


def test_outlets_not_a_map_fails_loud_on_every_slot(tmp_path, monkeypatch):
    d = write_config_dir(tmp_path, [_agent("phone-agent", "marin", PHONE_MODEL)])
    _write_outlet_shape(d, raw="outlets: 5\n")
    _point_env(monkeypatch, d)
    for outlet in profiles.OUTLETS:
        with pytest.raises(profiles.ProfileError) as exc:
            profiles.load_effective_profile("inbound", outlet=outlet)
        assert "'outlets'" in str(exc.value)


def test_outlet_entry_not_a_map_fails_loud_for_that_outlet_only(tmp_path, monkeypatch):
    d = write_config_dir(tmp_path, [_agent("phone-agent", "marin", PHONE_MODEL)])
    _write_outlet_shape(d, phone={"inbound": "phone-agent"}, talk="nope")
    _point_env(monkeypatch, d)
    with pytest.raises(profiles.ProfileError) as exc:
        profiles.load_effective_profile("inbound", outlet=profiles.OUTLET_TALK)
    assert "outlets.talk" in str(exc.value)
    assert profiles.load_effective_profile(
        "inbound", outlet=profiles.OUTLET_PHONE).agent_id == "phone-agent"


# ---------------------------------------------------------------------------
# Forward compatibility: a third outlet must never rework the model
# ---------------------------------------------------------------------------

def test_unknown_outlet_key_is_ignored(tmp_path, monkeypatch):
    d = write_config_dir(tmp_path, [_agent("phone-agent", "marin", PHONE_MODEL)])
    _write_outlet_shape(d,
                        phone={"inbound": "phone-agent"},
                        extra={"sms": {"inbound": "ghost-agent",
                                       "outbound": None}})
    _point_env(monkeypatch, d)
    # The unknown outlet is ignored wholesale - a bad value inside it must not
    # poison the outlets this build knows.
    assert profiles.load_effective_profile(
        "inbound", outlet=profiles.OUTLET_PHONE).agent_id == "phone-agent"
    assert profiles.load_effective_profile(
        "inbound", outlet=profiles.OUTLET_TALK) is None


def test_unknown_outlet_argument_is_loud(tmp_path, monkeypatch):
    d = write_config_dir(tmp_path, [_agent("phone-agent", "marin", PHONE_MODEL)])
    _write_outlet_shape(d, phone={"inbound": "phone-agent"})
    _point_env(monkeypatch, d)
    with pytest.raises(profiles.ProfileError) as exc:
        profiles.load_effective_profile("inbound", outlet="sms")
    assert "unknown outlet" in str(exc.value)


def test_env_voice_agent_still_wins_over_every_outlet(tmp_path, monkeypatch):
    d = write_config_dir(tmp_path, [_agent("phone-agent", "marin", PHONE_MODEL),
                                    _agent("talk-agent", "cedar", TALK_MODEL)])
    _write_outlet_shape(d,
                        phone={"inbound": "phone-agent"},
                        talk={"inbound": "talk-agent"})
    _point_env(monkeypatch, d, voice_agent="talk-agent")
    for outlet in profiles.OUTLETS:
        p = profiles.load_effective_profile("inbound", outlet=outlet)
        assert p.agent_id == "talk-agent"


# ---------------------------------------------------------------------------
# The phone bridge reads the PHONE outlet
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    return TestClient(server.app)


def _drive(client, monkeypatch, fake):
    urls = []

    def connect(url, *a, **kw):
        urls.append(url)
        return fake

    monkeypatch.setattr(server.websockets, "connect", connect)
    monkeypatch.setattr(server, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(server.eventlog, "append_event", lambda *a, **k: None)
    with client.websocket_connect("/voice/stream") as ws:
        ws.send_json({"event": "start", "sequenceNumber": "1", "streamSid": "MZs3",
                      "start": {"streamSid": "MZs3", "callSid": "CAx",
                                "customParameters": {"inbound_token":
                                                     server._mint_inbound_token()}}})
        ws.send_json({"event": "stop", "sequenceNumber": "2", "streamSid": "MZs3"})
    return urls


def test_media_stream_answers_with_the_phone_outlet_agent(client, monkeypatch,
                                                          tmp_path):
    """The wire-level pin: with DIFFERENT agents on the two outlets, an inbound call
    to the phone number must run the PHONE outlet's agent - the talk assignment must
    not leak into the DID's answer."""
    d = write_config_dir(tmp_path, [_agent("phone-agent", "marin", PHONE_MODEL),
                                    _agent("talk-agent", "cedar", TALK_MODEL)])
    _write_outlet_shape(d,
                        phone={"inbound": "phone-agent", "outbound": "phone-agent"},
                        talk={"inbound": "talk-agent", "outbound": "talk-agent"})
    _point_env(monkeypatch, d)
    fake = FakeOpenAIWS()
    urls = _drive(client, monkeypatch, fake)
    assert urls == [f"wss://api.openai.com/v1/realtime?model={PHONE_MODEL}"]
    ups = [m for m in fake.sent if m.get("type") == "session.update"]
    assert len(ups) == 1 and ups[0]["session"]["audio"]["output"]["voice"] == "marin"


def test_media_stream_refuses_when_the_phone_outlet_slot_is_broken(client,
                                                                   monkeypatch,
                                                                   tmp_path):
    """The loud bar on the live path: a broken PHONE slot refuses the call; a broken
    TALK slot leaves the phone line alone."""
    d = write_config_dir(tmp_path, [_agent("phone-agent", "marin", PHONE_MODEL)])
    _write_outlet_shape(d, phone={"inbound": "ghost-agent"},
                        talk={"inbound": "phone-agent"})
    _point_env(monkeypatch, d)
    fake = FakeOpenAIWS()
    urls = _drive(client, monkeypatch, fake)
    assert urls == []          # no dial at all - the refusal is loud, not silent


def test_a_flat_file_does_not_answer_the_did(client, monkeypatch, tmp_path):
    """The refusal on the REAL call path: the file the old screen used to write
    no longer answers the phone number. It is a broken pointer like any other -
    loud, and (ticket 08) eligible for the last-known-good snapshot, which does
    not exist on a config dir that has never completed a call."""
    d = write_config_dir(tmp_path, [_agent("phone-agent", "marin", PHONE_MODEL)])
    _write_flat_shape(d, inbound="phone-agent", outbound="phone-agent")
    _point_env(monkeypatch, d)
    fake = FakeOpenAIWS()
    urls = _drive(client, monkeypatch, fake)
    assert urls == []
