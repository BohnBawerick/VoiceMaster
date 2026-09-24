"""Tests for the structured voice event log (Idea 2).

Covers the best-effort append (never raises, tolerates a missing dir) and CallRecorder's per-turn
/ per-call accounting: TTFB from speech_stopped→first-audio, tool-call durations, token totals.
"""
import json

import pytest

from voicecore import eventlog


def _read(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_append_event_writes_one_json_line_and_makes_dir(tmp_path):
    target = tmp_path / "nested" / "voice_events.jsonl"   # parent does not exist yet
    eventlog.append_event({"type": "call", "call_id": "c1"}, str(target))
    eventlog.append_event({"type": "turn", "call_id": "c1"}, str(target))
    rows = _read(target)
    assert [r["type"] for r in rows] == ["call", "turn"]


def test_append_event_never_raises_on_bad_path():
    # A path whose parent can't be created (a file component in the middle) must be swallowed.
    eventlog.append_event({"x": 1}, "/dev/null/cannot/mkdir/here.jsonl")   # no exception


def test_recorder_emits_turn_with_ttfb_and_tokens(tmp_path):
    target = str(tmp_path / "ev.jsonl")
    clk = iter([100.0, 200.0])   # start_ts, then end_ts on finish
    rec = eventlog.CallRecorder(call_id="c1", mode="twilio", direction="inbound",
                                caller="+615550000", path=target, clock=lambda: next(clk))
    rec.on_speech_stopped(10.0)              # monotonic
    rec.on_audio_delta(10.25)                # +250 ms → TTFB
    rec.on_audio_delta(10.50)                # ignored — only the first counts
    rec.on_tool_call("hermes_agent", 29000.0, ok=True)
    rec.on_response_done(usage={
        "input_tokens": 100, "output_tokens": 40,
        "input_token_details": {"audio_tokens": 80},
        "output_token_details": {"audio_tokens": 30},
    }, ts=150.0)
    rec.finish(outcome="ok", retain_status="ok")

    rows = _read(tmp_path / "ev.jsonl")
    turn = next(r for r in rows if r["type"] == "turn")
    call = next(r for r in rows if r["type"] == "call")

    assert turn["ttfb_ms"] == 250.0
    assert turn["tool_calls"] == [{"name": "hermes_agent", "duration_ms": 29000.0, "ok": True}]
    assert turn["usage"] == {"input_tokens": 100, "output_tokens": 40,
                             "input_audio_tokens": 80, "output_audio_tokens": 30}
    assert call["num_turns"] == 1 and call["num_tool_calls"] == 1
    assert call["tokens_total"] == {"input_tokens": 100, "output_tokens": 40,
                                    "input_audio_tokens": 80, "output_audio_tokens": 30}
    assert call["direction"] == "inbound" and call["mode"] == "twilio"
    assert call["duration_s"] == 100.0 and call["retain_status"] == "ok"


def test_recorder_ttfb_none_without_speech_marker(tmp_path):
    target = str(tmp_path / "ev.jsonl")
    rec = eventlog.CallRecorder(call_id="c2", mode="talk", direction="outbound", path=target)
    rec.on_audio_delta(5.0)          # no speech_stopped first → no TTFB
    rec.on_response_done(usage={}, ts=1.0)
    turn = next(r for r in _read(tmp_path / "ev.jsonl") if r["type"] == "turn")
    assert turn["ttfb_ms"] is None
    assert turn["usage"]["input_tokens"] is None
