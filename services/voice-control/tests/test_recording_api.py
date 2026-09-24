"""Ticket 07 — serving a Call's recording so the page can play and SCRUB it.

Scrubbing an ``<audio>`` element is a ``Range: bytes=…`` request. A server that
ignores Range answers the whole body and the scrub bar goes dead, which is why the
range behaviour is pinned here rather than assumed.

The other half is the honest-absence rule: a Call with no recording gets a 404 and a
detail payload that tells the screen to render no player, never a broken one.
"""
import json

import pytest
from fastapi.testclient import TestClient

import app as voice_app
from voicecore import recording_store


AUDIO = bytes(range(256)) * 40      # 10240 bytes of stand-in Ogg/Opus


@pytest.fixture
def volume(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_RECORDINGS_DIR", str(tmp_path))
    day = tmp_path / "2026" / "08"
    day.mkdir(parents=True)
    (day / "MZplayable.opus").write_bytes(AUDIO)
    (day / "MZplayable.json").write_text(json.dumps({
        "schema": 1, "call_id": "MZplayable", "status": "ok",
        "ref": "2026/08/MZplayable.opus", "duration_s": 61.25,
        "size_bytes": len(AUDIO), "dropped_frames": 0, "error": None,
        "outlet": "phone", "direction": "inbound", "sample_rate": 8000,
        "channels": 2,
    }))
    (day / "MZbroken.json").write_text(json.dumps({
        "schema": 1, "call_id": "MZbroken", "status": "failed", "ref": None,
        "error": "OSError: No space left on device", "dropped_frames": 0,
    }))
    return tmp_path


@pytest.fixture
def client(volume):
    return TestClient(voice_app.create_app())


# ------------------------------------------------------------- serving the audio --

def test_the_whole_file_is_served_with_range_support_advertised(client):
    res = client.get("/api/calls/MZplayable/recording")
    assert res.status_code == 200
    assert res.content == AUDIO
    assert res.headers["accept-ranges"] == "bytes"
    assert res.headers["content-type"].startswith("audio/ogg")


def test_a_range_request_gets_exactly_that_range(client):
    """This is the request a browser makes when the listener drags the scrub bar."""
    res = client.get("/api/calls/MZplayable/recording",
                     headers={"Range": "bytes=1000-1099"})
    assert res.status_code == 206
    assert res.content == AUDIO[1000:1100]
    assert res.headers["content-range"] == f"bytes 1000-1099/{len(AUDIO)}"
    assert res.headers["accept-ranges"] == "bytes"


def test_an_open_ended_range_runs_to_the_end_of_the_file(client):
    res = client.get("/api/calls/MZplayable/recording",
                     headers={"Range": "bytes=10000-"})
    assert res.status_code == 206
    assert res.content == AUDIO[10000:]
    assert res.headers["content-range"] == f"bytes 10000-{len(AUDIO) - 1}/{len(AUDIO)}"


def test_a_suffix_range_serves_the_tail(client):
    """Chrome asks for the tail to find the Ogg stream length before it will seek."""
    res = client.get("/api/calls/MZplayable/recording", headers={"Range": "bytes=-256"})
    assert res.status_code == 206
    assert res.content == AUDIO[-256:]


def test_a_range_past_the_end_is_refused_rather_than_answered_with_junk(client):
    res = client.get("/api/calls/MZplayable/recording",
                     headers={"Range": "bytes=99999-"})
    assert res.status_code == 416
    assert res.headers["content-range"] == f"bytes */{len(AUDIO)}"


def test_a_call_with_no_recording_is_a_404_not_an_empty_body(client):
    res = client.get("/api/calls/MZnothing/recording")
    assert res.status_code == 404
    assert res.content != AUDIO


def test_a_call_id_cannot_reach_outside_the_recordings_volume(client, volume):
    secret = volume.parent / "secret.opus"
    secret.write_bytes(b"not yours")
    for hostile in ("..%2F..%2Fsecret", "....//secret", "%2Fetc%2Fpasswd"):
        res = client.get(f"/api/calls/{hostile}/recording")
        assert res.status_code == 404, hostile
        assert b"not yours" not in res.content


# ---------------------------------------------------- telling the screen what to do --

def test_the_detail_payload_points_at_a_playable_recording(client, monkeypatch):
    async def fake_get_call(call_id):
        return {"call": {"call_id": call_id, "transcript": "x"}, "unreachable": False,
                "partial": False, "error": None}

    monkeypatch.setattr(voice_app.hindsight_calls, "get_call", fake_get_call)
    body = client.get("/api/calls/MZplayable").json()
    assert body["recording"]["available"] is True
    assert body["recording"]["url"] == "/api/calls/MZplayable/recording"
    assert body["recording"]["duration_s"] == 61.25
    assert body["recording"]["size_bytes"] == len(AUDIO)


def test_the_detail_payload_says_nothing_for_a_call_that_was_never_recorded(
        client, monkeypatch):
    async def fake_get_call(call_id):
        return {"call": {"call_id": call_id, "transcript": "x"}, "unreachable": False,
                "partial": False, "error": None}

    monkeypatch.setattr(voice_app.hindsight_calls, "get_call", fake_get_call)
    body = client.get("/api/calls/MZancient").json()
    assert body["recording"]["available"] is False
    assert body["recording"]["status"] is None
    assert body["recording"]["url"] is None


def test_the_detail_payload_reports_a_capture_that_failed(client, monkeypatch):
    """A lost recording must not look like a call that predates recording."""
    async def fake_get_call(call_id):
        return {"call": {"call_id": call_id, "transcript": "x"}, "unreachable": False,
                "partial": False, "error": None}

    monkeypatch.setattr(voice_app.hindsight_calls, "get_call", fake_get_call)
    body = client.get("/api/calls/MZbroken").json()
    assert body["recording"]["available"] is False
    assert body["recording"]["status"] == "failed"
    assert "No space left" in body["recording"]["error"]


def test_availability_is_resolved_against_the_disk_not_against_the_metadata(
        client, monkeypatch, volume):
    """A Call whose metadata still names a recording that has been deleted must show
    no player. The reference is a pointer; the volume is the truth."""
    async def fake_get_call(call_id):
        return {"call": {"call_id": call_id, "transcript": "x",
                         "recording_ref": "2026/08/MZplayable.opus"},
                "unreachable": False, "partial": False, "error": None}

    monkeypatch.setattr(voice_app.hindsight_calls, "get_call", fake_get_call)
    (volume / "2026" / "08" / "MZplayable.opus").unlink()
    body = client.get("/api/calls/MZplayable").json()
    assert body["call"]["recording_ref"] == "2026/08/MZplayable.opus"
    assert body["recording"]["available"] is False


def test_the_reference_a_producer_wrote_reaches_the_screen(monkeypatch):
    """One additive field on the Call metadata — the shape ticket 05 owns is untouched."""
    import hindsight_calls
    formatted = hindsight_calls.format_call_doc({
        "id": "voice-twilio-MZ1",
        "content": "Them: hi",
        "metadata": {"platform": "voice_twilio", "direction": "inbound",
                     "recording": "2026/08/MZ1.opus"},
    })
    assert formatted["recording_ref"] == "2026/08/MZ1.opus"
    plain = hindsight_calls.format_call_doc({
        "id": "voice-twilio-MZ2", "content": "x",
        "metadata": {"platform": "voice_twilio", "direction": "inbound"},
    })
    assert plain["recording_ref"] is None
