"""Browser-level tests for the Calls screens.

Every defect the three review rounds of this ticket found was visible by opening
a page and invisible to the test suite. These tests open the pages.

A real Chromium (Playwright) drives the real built React bundle in
``static/assets/`` against a real uvicorn serving the real Calls API, backed by a
Hindsight mock that returns exactly what the three retainers write
(``tests/hindsight_producer_fixtures``).

Requires ``playwright`` (in requirements-dev.txt) plus its Chromium:

    .venv/bin/pip install -r requirements-dev.txt
    .venv/bin/playwright install chromium

Set ``PLAYWRIGHT_CHROMIUM_EXECUTABLE`` to use a Chromium already on the machine.
"""
import json
import re

import pytest

import calls_page as cp
from browser_harness import (
    body_text as _body_text,
    visible_warn_banners as _visible_warn_banners,
)
from hindsight_producer_fixtures import (
    cascade_outbound,
    hypothetical_call_with_outcome,
    talk_outbound,
    three_real_calls,
    twilio_inbound,
)

pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed (requirements-dev.txt)"
)


# The documents every scenario below is built from: the three retainers' output
# plus one hypothetical document that does carry a retained outcome, so "not
# retained" can be told apart from a screen that says it unconditionally.
def _corpus():
    return three_real_calls() + [hypothetical_call_with_outcome()]


# --------------------------------------------------------------------------
# The Calls screen
#
# Ticket 15 deleted the hand-written screen this file used to test beside the
# React one. Everything the legacy half asserted is asserted here against the
# built bundle: the not-retained wording, the retained-outcome contrast, the
# partial and total store-failure states, the date-only stamp and the pager.
# --------------------------------------------------------------------------


def test_react_list_shows_not_retained_rather_than_an_invented_outcome(stack, page):
    running = stack({"voice": _corpus(), "hermes": []})
    page.goto(running.base, wait_until="networkidle")
    cp.wait_for_rows(page)
    text = _body_text(page)

    assert "Calls" in text
    ids = cp.row_ids(page)
    for doc in three_real_calls():
        assert doc["id"] in ids
    assert "incomplete" not in text.lower()

    assert len(ids) == 4
    for call_id in ids:
        if "with-summary" in call_id:
            assert cp.field(page, call_id, "summary") == "Booked the table for 7pm."
        else:
            assert cp.field(page, call_id, "outcome") == "Not retained"

    # the inbound call's other party is not retained either, and says so
    assert cp.field(page, "voice-twilio-inbound-real", "who") == "Not retained"


def test_react_detail_shows_outcome_and_summary_as_not_retained(stack, page):
    running = stack({"voice": _corpus(), "hermes": []})
    doc = talk_outbound("voice-talk-outbound-real", "2026-08-17T10:15:00Z")
    page.goto(running.base, wait_until="networkidle")
    cp.wait_for_rows(page)
    cp.open_call(page, doc["id"])
    text = _body_text(page)

    # the Call has its own URL, and its id is on the detail view
    assert page.url.endswith(f"/calls/{doc['id']}")
    assert page.inner_text('[data-testid="call-id"]').strip() == doc["id"]
    fields = cp.meta(page)
    assert fields["How the call ended"] == "Not retained"
    assert page.inner_text('[data-testid="summary-detail-absent"]') == "Not retained"
    assert "incomplete" not in text.lower()

    absent = [el.inner_text() for el in page.query_selector_all(".meta-item-absent")]
    assert absent.count("Not retained") >= 2  # outcome and summary
    assert cp.verbatim(page) == doc["original_text"]

    # No banner of any kind: nothing about this call is wrong, it is only
    # unrecorded. (The dead-store test below is the positive control that this
    # selector can fire at all.)
    assert _visible_warn_banners(page) == []
    assert "did not finish cleanly" not in text


def test_react_detail_relays_a_retained_outcome(stack, page):
    """The contrast case: when the store holds an outcome, the screen shows it.

    Ticket 15 moved this off the legacy screen. Without it every "not retained"
    assertion above is satisfied by a screen that says "not retained"
    unconditionally.
    """
    running = stack({"voice": _corpus(), "hermes": []})
    page.goto(running.base, wait_until="networkidle")
    cp.wait_for_rows(page)
    cp.open_call(page, "voice-talk-with-summary")

    fields = cp.meta(page)
    assert fields["How the call ended"] == "ok", fields
    assert fields["Other Party"] == "+61491570159", fields
    # the retained values are NOT rendered through the absent style
    absent = [el.inner_text() for el in page.query_selector_all(".meta-item-absent")]
    assert "ok" not in absent and "+61491570159" not in absent

    # ...and nothing about this call is wrong, so no banner claims it is.
    assert _visible_warn_banners(page) == []
    assert "Error loading transcript" not in _body_text(page)


def test_react_list_on_a_dead_store_does_not_claim_there_are_no_calls(stack, page):
    """Both banks down. "No calls recorded yet" is a claim about history that a
    store we could not read is not entitled to make.

    Ticket 15 moved this off the legacy screen, where it was the only test of a
    TOTAL outage as opposed to one bank failing.
    """
    running = stack({"voice": 503, "hermes": 503})
    page.goto(running.base, wait_until="networkidle")
    page.wait_for_selector(".alert-banner.alert-unreachable", timeout=10_000)
    text = _body_text(page)

    assert "No calls recorded yet" not in text
    assert "Store Unreachable" in text
    assert cp.rows(page) == []


# --------------------------------------------------------------------------
# Ticket 07: the recording player, in a real browser, against the real bundle
# --------------------------------------------------------------------------


def _put_recording(root, call_id, payload=b"OggS" + bytes(4000)):
    """Put a recording on the volume exactly where the writer would have."""
    day = root / "2026" / "08"
    day.mkdir(parents=True, exist_ok=True)
    (day / f"{call_id}.opus").write_bytes(payload)
    (day / f"{call_id}.json").write_text(json.dumps({
        "schema": 1, "call_id": call_id, "status": "ok",
        "ref": f"2026/08/{call_id}.opus", "duration_s": 65.0,
        "size_bytes": len(payload), "dropped_frames": 0, "error": None,
        "outlet": "phone", "direction": "inbound", "sample_rate": 8000, "channels": 2,
    }))
    return day / f"{call_id}.opus"


def test_react_detail_plays_a_recording_that_exists(stack, page, tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_RECORDINGS_DIR", str(tmp_path))
    doc = twilio_inbound("voice-twilio-inbound-real", "2026-08-17T09:05:00Z")
    _put_recording(tmp_path, doc["id"])

    running = stack({"voice": _corpus(), "hermes": []})
    page.goto(running.base, wait_until="networkidle")
    cp.wait_for_rows(page)
    cp.open_call(page, doc["id"])
    # This file is not decodable audio, so the waveform cannot be drawn and the
    # plain player with its own controls is what the owner gets.
    page.wait_for_selector('[data-testid="waveform-fallback"] audio.recording-player')

    player = page.query_selector("audio.recording-player")
    assert player.get_attribute("src") == f"/api/calls/{doc['id']}/recording"
    assert player.get_attribute("controls") is not None
    assert "1:05" in _body_text(page)

    # and the endpoint the player points at really answers a range request, from a
    # real browser fetch rather than a test client
    probe = page.evaluate(
        """async (url) => {
            const res = await fetch(url, {headers: {Range: 'bytes=10-19'}});
            return {status: res.status, range: res.headers.get('content-range'),
                    len: (await res.arrayBuffer()).byteLength};
        }""",
        f"/api/calls/{doc['id']}/recording")
    assert probe["status"] == 206
    assert probe["len"] == 10
    assert probe["range"].startswith("bytes 10-19/")


def test_react_detail_of_a_call_with_no_recording_shows_no_player(stack, page, tmp_path,
                                                                  monkeypatch):
    """The acceptance item: no player, not a broken one."""
    monkeypatch.setenv("VOICE_RECORDINGS_DIR", str(tmp_path))
    doc = talk_outbound("voice-talk-outbound-real", "2026-08-17T10:15:00Z")

    running = stack({"voice": _corpus(), "hermes": []})
    page.goto(running.base, wait_until="networkidle")
    cp.wait_for_rows(page)
    cp.open_call(page, doc["id"])

    assert page.query_selector("audio") is None
    assert "Recording" not in _body_text(page)


# --------------------------------------------------------------------------
# Bank failure is partial, never total
# --------------------------------------------------------------------------


def test_react_shows_the_healthy_banks_calls_when_the_other_bank_fails(stack, page):
    running = stack({"voice": _corpus(), "hermes": 503})
    page.goto(running.base, wait_until="networkidle")
    cp.wait_for_rows(page)
    text = _body_text(page)

    # the calls that were fetched are on the screen...
    assert len(cp.rows(page)) == 4
    # ...with a banner naming the bank that was not read
    assert "Partial call history" in text
    assert "hermes" in text
    assert "503" in text
    assert "Call Archive Unreachable" not in text


def test_absent_bank_under_the_shipping_configuration_is_not_an_outage(stack, page):
    """The compose sets HINDSIGHT_BANK=hermes for voice-control.

    Nothing has ever written to the `voice` bank, so the store may 404 it. Four
    real calls in a healthy store must not black out the screen.
    """
    running = stack({"hermes": _corpus()}, configured_bank="hermes")
    page.goto(running.base, wait_until="networkidle")
    cp.wait_for_rows(page)
    text = _body_text(page)

    assert len(cp.rows(page)) == 4
    assert "Call Archive Unreachable" not in text
    assert "Partial call history" not in text
    assert "Store Unreachable" not in text


def test_react_detail_of_a_call_in_a_failed_bank_is_not_reported_as_missing(stack, page):
    """The call exists. The bank holding it is down. Do not say it never existed."""
    running = stack({"voice": [], "hermes": 503})
    page.goto(f"{running.base}", wait_until="networkidle")
    page.wait_for_selector(f".empty-state, {cp.ROWS}")
    # ask for a call that lives in the bank that is down
    page.goto(f"{running.base}/api/calls/voice-cascade-hermes-004")
    payload = json.loads(page.inner_text("body"))
    assert payload["call"] is None
    assert payload["partial"] is True
    assert "hermes" in payload["error"]
    assert "may exist" in payload["error"]


def test_non_call_documents_are_not_shown_as_calls(stack, page):
    """The fallback bank holds Hermes's general memories, not only calls."""
    from hindsight_producer_fixtures import non_call_memory

    running = stack(
        {
            "voice": [talk_outbound("voice-talk-voice-000", "2026-08-17T13:00:00Z")],
            "hermes": [non_call_memory("hermes-pref-001", "2026-08-17T13:05:00Z")],
        }
    )
    page.goto(running.base, wait_until="networkidle")
    cp.wait_for_rows(page)
    text = _body_text(page)
    assert cp.row_ids(page) == ["voice-talk-voice-000"]
    assert "hermes-pref-001" not in page.content()
    assert "espresso" not in text


def _date_only_corpus():
    """What both screens see if Hindsight's listing does not stamp `created_at`.

    The only timestamp left is the retainers' `%Y-%m-%d`. Nothing retained a
    time of day, so neither screen may print one.
    """
    from hindsight_producer_fixtures import drop_created_at

    return [
        drop_created_at(talk_outbound("voice-talk-voice-000", "2026-08-17T10:15:00Z")),
        drop_created_at(cascade_outbound("voice-cascade-voice-001", "2026-08-17T11:25:00Z")),
    ]


def test_react_renders_a_date_only_stamp_without_a_clock(stack, page):
    running = stack({"voice": _date_only_corpus(), "hermes": []})
    page.goto(running.base, wait_until="networkidle")
    cp.wait_for_rows(page)

    when = cp.field(page, "voice-talk-voice-000", "when")
    assert "2026" in when and "17" in when
    assert ":" not in when, f"a time of day nothing retained: {when!r}"
    headings = [h.inner_text() for h in page.query_selector_all(".day-heading")]
    assert not any(re.search(r"\d:\d\d", h) for h in headings), headings

    assert "Unknown date" in cp.field(page, "voice-cascade-voice-001", "when")

    cp.open_call(page, "voice-talk-voice-000")
    when_detail = cp.meta(page)["When"]
    assert "2026" in when_detail
    assert not re.search(r"\d:\d\d", when_detail), when_detail


def test_paging_controls_walk_the_whole_history(stack, page):
    """125 documents in each bank: the browser must reach page 13, not page 10."""
    voice = [
        talk_outbound(f"voice-talk-voice-{i:03d}", f"2026-08-17T14:{i % 60:02d}:00Z")
        for i in range(125)
    ]
    hermes = [
        cascade_outbound(f"voice-cascade-hermes-{i:03d}", f"2026-08-17T10:{i % 60:02d}:00Z")
        for i in range(125)
    ]
    running = stack({"voice": voice, "hermes": hermes})
    page.goto(running.base, wait_until="networkidle")
    cp.wait_for_rows(page)
    assert "250 calls" in _body_text(page)

    seen = set()
    for expected_page in range(1, 14):
        cp.wait_for_rows(page)
        assert f"Page {expected_page} " in _body_text(page)
        seen.update(cp.row_ids(page))
        if expected_page < 13:
            # The page number updates on click, the rows only when the fetch
            # returns, so waiting on the number samples a frame still showing
            # the previous page's rows and re-counts it. Wait for the rows.
            cp.next_page(page)

    assert len(seen) == 250


# --------------------------------------------------------------------------
# C4: the stereo waveform. Caller on the left channel drawn in the top lane,
# Agent on the right channel in the bottom lane. Asserted on the rendered
# pixels, not on a class name: a swapped channel or a lane painted in the wrong
# colour would pass any test that only read the DOM.
# --------------------------------------------------------------------------


def _stereo_opus(path):
    """8 s of real stereo Opus: the left channel speaks in the first half only,
    the right channel in the second half only."""
    import shutil
    import subprocess

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is not installed, so no real Opus file can be made")
    subprocess.run(
        [ffmpeg, "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "aevalsrc='0.6*sin(2*PI*440*t)*lt(t,4)|0.6*sin(2*PI*660*t)*gte(t,4)':s=48000:d=8",
         "-c:a", "libopus", "-b:a", "32k", str(path)],
        check=True,
    )
    return path.read_bytes()


def test_react_detail_draws_caller_and_agent_as_two_lanes(stack, page, tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_RECORDINGS_DIR", str(tmp_path))
    doc = twilio_inbound("voice-twilio-inbound-real", "2026-08-17T09:05:00Z")
    _put_recording(tmp_path, doc["id"], payload=_stereo_opus(tmp_path / "src.opus"))

    running = stack({"voice": _corpus(), "hermes": []})
    page.goto(running.base, wait_until="networkidle")
    cp.wait_for_rows(page)
    cp.open_call(page, doc["id"])
    page.wait_for_selector('[data-testid="waveform"] canvas.waveform-canvas', timeout=10_000)
    assert page.query_selector('[data-testid="waveform-fallback"]') is None

    # Average colour of the drawn bars in each lane and each half of the call.
    # Bars not yet played are drawn at 42% alpha, hence the low alpha floor.
    lanes = page.eval_on_selector(
        "canvas.waveform-canvas",
        """c => {
            const ctx = c.getContext('2d');
            const w = c.width, h = c.height;
            const sample = (x0, x1, y0, y1) => {
              const d = ctx.getImageData(x0, y0, x1 - x0, y1 - y0).data;
              let n = 0, r = 0, g = 0, b = 0;
              for (let i = 0; i < d.length; i += 4) {
                if (d[i + 3] > 60) { n++; r += d[i]; g += d[i + 1]; b += d[i + 2]; }
              }
              return {n, r: n ? r / n : 0, g: n ? g / n : 0, b: n ? b / n : 0};
            };
            const top = [Math.round(h * 0.08), Math.round(h * 0.42)];
            const bottom = [Math.round(h * 0.58), Math.round(h * 0.92)];
            return {
              topFirst: sample(0, w / 2 - 4, ...top), topSecond: sample(w / 2 + 4, w, ...top),
              bottomFirst: sample(0, w / 2 - 4, ...bottom), bottomSecond: sample(w / 2 + 4, w, ...bottom),
            };
        }""",
    )
    # the caller (left channel, top lane) speaks first; the Agent (right, bottom) second
    assert lanes["topFirst"]["n"] > 10 * max(1, lanes["topSecond"]["n"]), lanes
    assert lanes["bottomSecond"]["n"] > 10 * max(1, lanes["bottomFirst"]["n"]), lanes
    # the caller lane is orange, the Agent lane teal
    top, bottom = lanes["topFirst"], lanes["bottomSecond"]
    assert top["r"] > top["g"] > top["b"], top
    assert bottom["g"] > bottom["r"] and bottom["b"] > bottom["r"], bottom
