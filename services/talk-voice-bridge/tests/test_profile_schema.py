"""s1 agent-profile schema tests (c05–c07): full §2.3 sample accepted; rejections
carry file + field path; inline credentials rejected at any depth."""
import pytest
import yaml

from voicecore import profiles
from profile_helpers import CANONICAL, REJECTION_CASES, SAMPLE, profile_doc


@pytest.fixture
def registry():
    return profiles.load_registry(CANONICAL)


def test_valid_sample_accepted(registry):
    """c05: the full §2.3 sample profile validates cleanly."""
    doc = yaml.safe_load(SAMPLE.read_text())
    for key in ("id", "description", "enabled", "hermes_profile", "direction",
                "pipeline", "providers", "knobs", "persona", "number_policy",
                "guardrails", "memory"):
        assert key in doc, f"sample fixture is missing §2.3 field '{key}'"
    assert doc["pipeline"] == "realtime"
    assert "vad" in doc["knobs"]
    assert profiles.validate_profile(doc, registry, SAMPLE) == []


@pytest.mark.parametrize("name,overrides,expected_path",
                         REJECTION_CASES, ids=[c[0] for c in REJECTION_CASES])
def test_rejections_are_actionable(tmp_path, registry, name, overrides, expected_path):
    """c06: every rejection names the offending field path AND the file."""
    doc = profile_doc(**overrides)
    f = tmp_path / f"{name}.yaml"
    f.write_text(yaml.safe_dump(doc))
    errors = profiles.validate_profile(yaml.safe_load(f.read_text()), registry, f)
    assert errors, f"{name} should have been rejected"
    joined = "\n".join(errors)
    assert expected_path in joined, f"error must name field path {expected_path!r}: {joined}"
    assert str(f) in joined, f"error must name the file: {joined}"


INLINE_SECRET_CASES = [
    ("top-level-api-key", {"api_key": "not-a-real-value"}, "api_key"),
    ("nested-knob-api-key", {"knobs": {"foo": {"api_key": "not-a-real-value"}}},
     "knobs.foo.api_key"),
    ("provider-token", {"providers": {"realtime": "openai-gpt-realtime",
                                      "extra": {"token": "not-a-real-value"}},
                        "allow_invalid_providers": True},
     "providers.extra.token"),
    ("top-level-password", {"password": "not-a-real-value"}, "password"),
]


@pytest.mark.parametrize("name,overrides,expected_path",
                         INLINE_SECRET_CASES, ids=[c[0] for c in INLINE_SECRET_CASES])
def test_inline_secret_rejected(tmp_path, registry, name, overrides, expected_path):
    """c07: inline credentials rejected at ANY depth, pointing at secret_env."""
    doc = profile_doc(**overrides)
    f = tmp_path / f"{name}.yaml"
    f.write_text(yaml.safe_dump(doc))
    errors = profiles.validate_profile(yaml.safe_load(f.read_text()), registry, f)
    assert errors
    joined = "\n".join(errors)
    assert expected_path in joined
    assert "secret_env" in joined, f"error must point at secret_env indirection: {joined}"
