"""s16 c1 (Talk-side wiring): the honest debounce knob and the clamped inner timeout.

The budget arithmetic itself is engine-level and tested in `services/voice`
(`test_s16_tool_budget.py`) — cascade_live.py is one md5-pinned twin, so proving it once
proves it for both lanes. What is Talk-SPECIFIC, and what actually shipped broken, is how
this bridge CONSTRUCTS the engine:

* `filler_debounce_s=max(cfg.filler_debounce_ms / 1000.0, 2.0)` floored the knob, so the
  deployed `VOICE_FILLER_DEBOUNCE_MS=1500` silently ran as 2000. The env var could only
  ever RAISE the debounce, never lower it — inert downward, the same class as pfSense's
  `management_ports` listing ports the box wasn't using. A test that reads
  `cfg.filler_debounce_ms` would have seen 1500 and called it fine; only the value the
  CONSTRUCTOR receives tells the truth.

* `timeout=cfg.hermes_timeout` handed httpx 120s inside what was then a 47s outer cap.
  The engine cancelled and apologised while the request was still live, so a tool turn
  with side effects (an email sent, a memory written) could DO them while the caller was
  told it failed.
"""
import cascade_bridge
from voicecore import cascade_live
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
    return profiles.ActiveProfile(agent_id="supplier-caller", source="s16-test",
                                  doc=CASCADE_DOC, registry=registry)


def _bridge(cfg):
    return cascade_bridge.CascadeBridge(
        cfg, _profile(),
        OutboundMission(brief="confirm stock", target_display="Alex"),
        env=ENV, token="tok16")


def _debounce_reaching_the_engine(ms, monkeypatch):
    """The value the ENGINE receives, not the value Config holds."""
    monkeypatch.setenv("VOICE_FILLER_DEBOUNCE_MS", str(ms))
    cfg = config.load_base()
    assert cfg.filler_debounce_ms == ms, "precondition: env reached Config"
    return _bridge(cfg)._session._filler_debounce_s


# -- the knob is honest in BOTH directions ------------------------------------

def test_c1_debounce_below_two_seconds_is_no_longer_floored(monkeypatch):
    """THE DEFECT. 1500ms is the deployed value; it used to arrive as 2.0."""
    assert _debounce_reaching_the_engine(1500, monkeypatch) == 1.5


def test_c1_debounce_above_two_seconds_still_passes_through(monkeypatch):
    """The other pole — a bipolar check, so a fix that hardcodes 1.5 fails here."""
    assert _debounce_reaching_the_engine(4000, monkeypatch) == 4.0


def test_c1_debounce_default_is_honest(monkeypatch):
    monkeypatch.delenv("VOICE_FILLER_DEBOUNCE_MS", raising=False)
    assert _debounce_reaching_the_engine(1500, monkeypatch) == 1.5


# -- the inner HTTP timeout cannot outlive the outer budget -------------------

def test_c1_hermes_http_timeout_is_clamped_to_the_tool_budget(monkeypatch):
    """A 120s client inside a 60s budget means we apologise while the request is live."""
    monkeypatch.setenv("HERMES_TIMEOUT", "120")
    cfg = config.load_base()
    assert cfg.hermes_timeout == 120.0, "precondition: the inversion is reachable"

    bridge = _bridge(cfg)
    inner = bridge._session._hermes_call.keywords["timeout"]
    assert inner <= cascade_live.TOOL_BUDGET_S, (
        f"inner HTTP timeout {inner}s outlives the {cascade_live.TOOL_BUDGET_S}s tool "
        "budget — the caller hears an apology for a request still in flight")


def test_c1_a_shorter_configured_timeout_is_respected(monkeypatch):
    """Clamp, not override: an operator who deliberately sets a TIGHTER timeout keeps it."""
    monkeypatch.setenv("HERMES_TIMEOUT", "5")
    inner = _bridge(config.load_base())._session._hermes_call.keywords["timeout"]
    assert inner == 5.0
