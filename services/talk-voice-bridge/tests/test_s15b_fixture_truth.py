"""s15b-A: fixture truth + product-backed cascade validation.

THREE defects shipped through one hole: fixtures that agreed with the code instead of
with reality. The root landmine was ``profile_doc``'s shallow ``doc.update(overrides)``
-- ``profile_doc(pipeline="cascade")`` flipped the NAME and left ``providers.realtime``
intact, so 118 green tests never once reached the cascade branch.

The consult (grok+kimi, 2026-07-21) found the deeper half: production never validated
cascade providers AT ALL -- ``activation_problem`` returned before reading them -- so a
test-only guard would have left fixtures stricter than production while poison YAML
still dialled. a2/a3 close that; a1 closes the fixture landmine.
"""
import pytest

from voicecore import profiles
from profile_helpers import profile_doc, write_config_dir

CASCADE_PROVIDERS = {"stt": "deepgram", "llm": "nvidia-nemotron", "tts": "elevenlabs"}


def _errors(tmp_path, doc, name="vcfg"):
    """Validate one doc against the CANONICAL registry, return the error strings."""
    d = write_config_dir(tmp_path, [doc], name=name)
    return profiles.validate_dir(d)


# -- a1: profile_doc is pipeline-aware ----------------------------------------

def test_a1_cascade_doc_carries_no_realtime_key():
    """The landmine itself: cascade in NAME must mean cascade in SHAPE."""
    doc = profile_doc(pipeline="cascade")
    assert doc["pipeline"] == "cascade"
    assert "realtime" not in doc["providers"], (
        "profile_doc(pipeline='cascade') still emits providers.realtime -- this is the "
        "exact shallow-update defect that shipped the s14b crash past 118 green tests")
    assert set(doc["providers"]) == {"stt", "llm", "tts"}


def test_a1_realtime_doc_unchanged():
    """Realtime defaults must not move -- every existing test depends on them."""
    doc = profile_doc()
    assert doc["pipeline"] == "realtime"
    assert doc["providers"] == {"realtime": "openai-gpt-realtime"}


def test_a1_explicit_providers_still_honoured():
    """An explicit, COMPLETE providers block for the pipeline passes through."""
    doc = profile_doc(pipeline="cascade", providers=dict(CASCADE_PROVIDERS))
    assert doc["providers"] == CASCADE_PROVIDERS


@pytest.mark.parametrize("pipeline,providers", [
    ("cascade", {"realtime": "openai-gpt-realtime"}),   # the historical poison
    ("cascade", {"stt": "deepgram"}),                   # incomplete cascade
    ("cascade", {"stt": "deepgram", "llm": "nvidia-nemotron"}),
    ("realtime", dict(CASCADE_PROVIDERS)),              # inverse poison
])
def test_a1_incomplete_providers_raise(pipeline, providers):
    """An explicit providers= that is wrong for the pipeline is a LOUD helper error,
    not a doc that quietly reaches the product."""
    with pytest.raises(ValueError) as exc:
        profile_doc(pipeline=pipeline, providers=providers)
    assert pipeline in str(exc.value)


# -- a2: production rejects poison cascade at LOAD -----------------------------

def test_a2_cascade_with_realtime_provider_rejected(tmp_path):
    """☠️ Before s15b-A this document was fully VALID and ACTIVATABLE."""
    doc = {"id": "t-agent", "pipeline": "cascade",  # s15b: deliberate poison
           "providers": {"realtime": "openai-gpt-realtime"}}
    errs = _errors(tmp_path, doc)
    assert errs, "cascade carrying providers.realtime must not validate"
    joined = " ".join(errs)
    assert "providers.realtime" in joined and "cascade" in joined


@pytest.mark.parametrize("missing", ["stt", "llm", "tts"])
def test_a2_cascade_missing_role_rejected(tmp_path, missing):
    providers = {k: v for k, v in CASCADE_PROVIDERS.items() if k != missing}
    doc = {"id": "t-agent", "pipeline": "cascade", "providers": providers}
    errs = _errors(tmp_path, doc)
    assert errs, f"cascade missing {missing} must not validate"
    assert any(f"providers.{missing}" in e for e in errs)


def test_a2_cascade_with_no_providers_block_rejected(tmp_path):
    """The StubProfile shape -- cascade with no providers at all."""
    # s15b: deliberate poison
    errs = _errors(tmp_path, {"id": "t-agent", "pipeline": "cascade"})
    assert errs, "cascade with no providers block must not validate"


def test_a2_hybrid_cascade_rejected(tmp_path):
    """{stt,llm,tts,realtime} -- complete AND poisoned. Both rules must bind."""
    providers = dict(CASCADE_PROVIDERS, realtime="openai-gpt-realtime")
    errs = _errors(tmp_path, {"id": "t-agent", "pipeline": "cascade",
                              "providers": providers})
    assert errs, "a hybrid cascade/realtime providers block must not validate"
    assert any("providers.realtime" in e for e in errs)


def test_a2_honest_cascade_accepted(tmp_path):
    """The positive control -- without it the rejections prove nothing."""
    doc = {"id": "t-agent", "pipeline": "cascade",
           "providers": dict(CASCADE_PROVIDERS)}
    assert _errors(tmp_path, doc) == []


def test_a2_poison_cascade_fails_activation_too(tmp_path, monkeypatch):
    """Not just the CLI validator: the load path an operator's YAML actually takes."""
    monkeypatch.setattr(profiles, "CASCADE_CAPABILITY", {"talk": frozenset({"outbound", "inbound"})})
    d = write_config_dir(tmp_path, [{"id": "t-agent",  # s15b: deliberate poison
                                     "pipeline": "cascade",
                                     "providers": {"realtime": "openai-gpt-realtime"}}])
    with pytest.raises(profiles.ProfileError) as exc:
        profiles.load_effective_profile(
            "outbound", env={"VOICE_CONFIG_DIR": str(d), "VOICE_AGENT": "t-agent"})
    assert "providers.realtime" in str(exc.value)


# -- a3: inverse poison --------------------------------------------------------

def test_a3_realtime_with_cascade_providers_rejected(tmp_path):
    """pipeline: realtime carrying only {stt,llm,tts}.

    NOTE: this was ALREADY rejected before s15b-A (the pipeline=='realtime' branch
    required providers.realtime). Pinned here so the cascade work cannot regress it --
    it is not new coverage and is not billed as such.
    """
    doc = {"id": "t-agent", "pipeline": "realtime",
           "providers": dict(CASCADE_PROVIDERS)}
    errs = _errors(tmp_path, doc)
    assert errs
    assert any("providers.realtime" in e for e in errs)
