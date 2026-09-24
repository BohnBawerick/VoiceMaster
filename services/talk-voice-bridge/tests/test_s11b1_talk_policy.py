"""s11b-1: talk_policy.allow schema + the shared kind-scoped, pre-resolve membership
decision (talk_allow_decision) + the ActiveProfile.talk_allow_list() accessor.

talk_policy is the Talk (Mode V) allow shape — usernames and/or room tokens matched by
their DIALED FORM before any OCS resolve. It is SEPARATE from number_policy.allow (which
stays E.164/Twilio-only): a Talk dial never consults number_policy, and vice versa.
"""

import pytest
import yaml

from voicecore import profiles
from profile_helpers import CANONICAL, profile_doc


@pytest.fixture
def registry():
    return profiles.load_registry(CANONICAL)


# -- c1: talk_policy is an accepted top-level key that round-trips --------------

def test_talk_policy_is_a_known_top_level_key():
    assert "talk_policy" in profiles._TOP_LEVEL_KEYS


def test_valid_talk_policy_accepted(tmp_path, registry):
    doc = profile_doc(talk_policy={"allow": ["sam", "a1b2c3d4"]})
    assert profiles.validate_profile(doc, registry, tmp_path / "a.yaml") == []


# -- c1: talk_policy.allow schema (list of non-empty Talk-identity strings) -----

TALK_POLICY_REJECTIONS = [
    ("allow-not-a-list", {"allow": "sam"}, "talk_policy.allow"),
    ("talk-policy-not-a-map", "sam", "talk_policy"),
    ("empty-string-entry", {"allow": ["sam", ""]}, "talk_policy.allow[1]"),
    ("whitespace-entry", {"allow": ["  "]}, "talk_policy.allow[0]"),
    ("non-string-entry", {"allow": ["ok", 5]}, "talk_policy.allow[1]"),
]


@pytest.mark.parametrize("name,talk_policy,expected_path", TALK_POLICY_REJECTIONS,
                         ids=[c[0] for c in TALK_POLICY_REJECTIONS])
def test_talk_policy_rejections_are_actionable(tmp_path, registry, name, talk_policy,
                                               expected_path):
    f = tmp_path / f"{name}.yaml"
    doc = profile_doc(talk_policy=talk_policy)
    f.write_text(yaml.safe_dump(doc))
    errors = profiles.validate_profile(yaml.safe_load(f.read_text()), registry, f)
    assert errors, f"{name} should have been rejected"
    joined = "\n".join(errors)
    assert expected_path in joined, f"error must name {expected_path!r}: {joined}"
    assert str(f) in joined, f"error must name the file: {joined}"


def test_talk_policy_allow_is_not_e164_validated(tmp_path, registry):
    """A bare Talk username (not E.164) is a VALID talk_policy entry — number_policy's
    E.164 rule must not leak onto talk_policy."""
    doc = profile_doc(talk_policy={"allow": ["sam", "some.user"]})
    assert profiles.validate_profile(doc, registry, tmp_path / "a.yaml") == []
    # ...while the SAME string in number_policy.allow is still rejected as non-E.164.
    doc2 = profile_doc(number_policy={"allow": ["sam"]})
    assert any("number_policy.allow" in e
               for e in profiles.validate_profile(doc2, registry, tmp_path / "b.yaml"))


# -- c1: the accessor mirrors outbound_allow_list() ----------------------------

def _active(doc):
    return profiles.ActiveProfile(agent_id=doc.get("id") or "t-agent", source="test",
                                  doc=doc, registry={})


def test_talk_allow_list_accessor():
    assert _active(profile_doc(talk_policy={"allow": ["sam"]})).talk_allow_list() == ["sam"]
    assert _active(profile_doc(talk_policy={"allow": []})).talk_allow_list() == []
    assert _active(profile_doc()).talk_allow_list() is None            # field absent
    assert _active(profile_doc(talk_policy={})).talk_allow_list() is None  # no allow key


def test_number_policy_only_leaves_talk_allow_any():
    """A profile carrying ONLY number_policy.allow does NOT gate Talk: talk_allow_list()
    is None (allow-any). number_policy plays no part in the Talk decision."""
    ap = _active(profile_doc(number_policy={"allow": ["+15550001111"]}))
    assert ap.talk_allow_list() is None


# -- c2: the shared kind-scoped, pre-resolve membership decision ----------------

def test_talk_allow_decision_username_and_token_listed():
    assert profiles.talk_allow_decision("username", "sam", ["sam", "a1b2c3d4"]) is True
    assert profiles.talk_allow_decision("token", "a1b2c3d4", ["sam", "a1b2c3d4"]) is True


def test_talk_allow_decision_not_listed_denies():
    assert profiles.talk_allow_decision("username", "eve", ["sam"]) is False
    assert profiles.talk_allow_decision("token", "otherroom", ["sam"]) is False


def test_talk_allow_decision_deny_all_and_allow_any():
    assert profiles.talk_allow_decision("username", "sam", []) is False      # deny-all
    assert profiles.talk_allow_decision("token", "anytoken", []) is False      # deny-all
    assert profiles.talk_allow_decision("username", "anyone", None) is True    # allow-any
    assert profiles.talk_allow_decision("token", "anytoken", None) is True     # allow-any


def test_talk_allow_decision_trims_target():
    assert profiles.talk_allow_decision("username", "  sam  ", ["sam"]) is True


def test_talk_allow_decision_unknown_kind_fails_closed():
    assert profiles.talk_allow_decision("room", "sam", ["sam"]) is False
    assert profiles.talk_allow_decision("", "sam", None) is False

