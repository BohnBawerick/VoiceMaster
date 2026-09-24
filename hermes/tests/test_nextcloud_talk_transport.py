"""
Unit tests for the pure OCS message helpers in transport.py.

These cover the loop guard, mention detection, trigger gating, mention/rich-object
rendering, owner/guest tagging, and reply chunking - i.e. the moderation- and
correctness-critical logic that must survive the sidecar -> native-plugin port.
No gateway, no network: importing transport.py pulls in no Hermes deps (httpx is
imported lazily, only inside the client's I/O methods).

Run (from hermes/): PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests -q
"""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins", "nextcloud_talk"))

import transport as t  # noqa: E402


US = "ai-agent"


def _comment(actor="alice", text="hello", **kw):
    m = {
        "id": kw.pop("id", 100),
        "messageType": "comment",
        "systemMessage": "",
        "actorType": "users",
        "actorId": actor,
        "message": text,
        "actorDisplayName": kw.pop("display", actor.title()),
    }
    m.update(kw)
    return m


# --- render_message --------------------------------------------------------

def test_render_plain():
    assert t.render_message(_comment(text="  hi there  ")) == "hi there"


def test_render_resolves_user_mention_param():
    m = _comment(
        text="hey {mention-user1} ping",
        messageParameters={"mention-user1": {"type": "user", "id": "ai-agent", "name": "AI"}},
    )
    assert t.render_message(m) == "hey @ai-agent ping"


def test_render_resolves_non_user_object():
    m = _comment(
        text="see {file1}",
        messageParameters={"file1": {"type": "file", "name": "report.pdf"}},
    )
    assert t.render_message(m) == "see report.pdf"


# --- is_human_message (loop guard) -----------------------------------------

def test_human_message_accepts_other_user():
    assert t.is_human_message(_comment(actor="alice"), US, set()) is True


def test_human_message_rejects_our_own():
    assert t.is_human_message(_comment(actor=US), US, set()) is False


def test_human_message_rejects_system():
    assert t.is_human_message(_comment(systemMessage="call_started"), US, set()) is False


def test_human_message_rejects_non_comment():
    assert t.is_human_message(_comment(messageType="system"), US, set()) is False


def test_human_message_rejects_bot_actor():
    assert t.is_human_message(_comment(actorType="bots"), US, set()) is False


def test_human_message_rejects_echoed_reference_id():
    m = _comment(referenceId="abc123")
    assert t.is_human_message(m, US, {"abc123"}) is False


def test_human_message_rejects_empty_text():
    assert t.is_human_message(_comment(text="   "), US, set()) is False


# --- is_mentioned ----------------------------------------------------------

def test_mentioned_via_rich_param():
    m = _comment(
        text="{mention-user1} yo",
        messageParameters={"mention-user1": {"type": "user", "id": US}},
    )
    assert t.is_mentioned(m, US) is True


def test_mentioned_via_plain_text():
    assert t.is_mentioned(_comment(text="hey @ai-agent"), US) is True


def test_not_mentioned():
    assert t.is_mentioned(_comment(text="hey @someone-else"), US) is False


# --- should_respond (trigger gate) -----------------------------------------

def test_smart_one_to_one_answers_all():
    assert t.should_respond(t.ROOM_TYPE_ONE_TO_ONE, _comment(text="hi"), US, "smart") is True


def test_smart_group_requires_mention():
    assert t.should_respond(2, _comment(text="hi"), US, "smart") is False
    assert t.should_respond(2, _comment(text="hi @ai-agent"), US, "smart") is True


def test_mode_all_always_true():
    assert t.should_respond(2, _comment(text="hi"), US, "all") is True


def test_mode_mention_only():
    assert t.should_respond(t.ROOM_TYPE_ONE_TO_ONE, _comment(text="hi"), US, "mention") is False
    assert t.should_respond(t.ROOM_TYPE_ONE_TO_ONE, _comment(text="@ai-agent hi"), US, "mention") is True


def test_mode_one_to_one_only():
    assert t.should_respond(t.ROOM_TYPE_ONE_TO_ONE, _comment(), US, "oneToOneOnly") is True
    assert t.should_respond(2, _comment(text="@ai-agent hi"), US, "oneToOneOnly") is False


# --- speaker_tag (moderation identity) -------------------------------------

def test_speaker_tag_owner():
    tag, is_owner = t.speaker_tag("olivia", "Olivia", {"olivia"})
    assert is_owner is True
    assert "OWNER" in tag and "olivia" in tag


def test_speaker_tag_guest():
    tag, is_owner = t.speaker_tag("alice", "Alice", {"olivia"})
    assert is_owner is False
    assert "GUEST" in tag


def test_speaker_tag_owner_case_insensitive():
    _, is_owner = t.speaker_tag("Olivia", "Olivia", {"olivia"})
    assert is_owner is True


def test_speaker_tag_spoof_resistant():
    # A guest whose display name claims to be the owner is still a GUEST:
    # identity comes from actorId, not the display name.
    tag, is_owner = t.speaker_tag("mallory", "olivia", {"olivia"})
    assert is_owner is False
    assert "GUEST" in tag


# --- chunk_text ------------------------------------------------------------

def test_chunk_short_single():
    assert t.chunk_text("hello") == ["hello"]


def test_chunk_empty():
    assert t.chunk_text("   ") == []


def test_chunk_splits_long_on_boundary():
    body = ("A" * 20000) + "\n\n" + ("B" * 20000)
    chunks = t.chunk_text(body, chunk_limit=25000)
    assert len(chunks) == 2
    assert all(len(c) <= 25000 for c in chunks)
    assert chunks[0].startswith("A") and chunks[1].startswith("B")


def test_chunk_hard_split_when_no_boundary():
    body = "C" * 60000
    chunks = t.chunk_text(body, chunk_limit=25000)
    assert len(chunks) == 3
    assert sum(len(c) for c in chunks) == 60000
