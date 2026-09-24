"""s15b-B b3/b4/b5: the PSTN lanes over REAL cascade-shaped profiles.

Recon's finding: **no PSTN-cascade test used a real profile shape at all.** Every cascade
config test in this suite passed a raw dict straight to ``build_cascade_config``, bypassing
``profiles`` entirely -- so the loader→registry→cascade-config chain the live lane walks
was never driven end to end with a document an operator could actually write.

b5 asserts the eventlog OFF DISK rather than on a constructor kwarg: "the writer was
handed pipeline='cascade'" is not the same claim as "the row on disk says cascade", and
only the second one is what calllog and the dashboard read.
"""
import json

import pytest
import yaml

from pathlib import Path

from voicecore import cascade_config
from voicecore import eventlog
from voicecore import profiles
from profile_helpers import write_config_dir

SERVICES = Path(__file__).resolve().parent.parent.parent

CASCADE_PROVIDERS = {"stt": "deepgram", "llm": "nvidia-nemotron", "tts": "elevenlabs"}
REALTIME_PROVIDERS = {"realtime": "openai-gpt-realtime"}


def _cfg_dir(tmp_path, pipeline, providers, aid="pstn-agent"):
    d = write_config_dir(tmp_path, [{"id": aid, "pipeline": pipeline,
                                     "providers": dict(providers)}])
    (d / "active.yaml").write_text(yaml.safe_dump(
        {"outlets": {o: {"inbound": None, "outbound": aid}
                     for o in profiles.OUTLETS}}))
    return d


# -- b3: PSTN-cascade, real profile shape through the real loader --------------

def test_b3_pstn_cascade_resolves_through_the_real_loader(tmp_path, monkeypatch):
    """The chain no test walked before: on-disk YAML → load_effective_profile →
    registry → build_cascade_config, with a document an operator could write."""
    monkeypatch.setattr(profiles, "CASCADE_CAPABILITY", {"phone": frozenset({"outbound", "inbound"})})
    d = _cfg_dir(tmp_path, "cascade", CASCADE_PROVIDERS)

    snapshot = profiles.load_effective_profile(
        "outbound", env={"VOICE_CONFIG_DIR": str(d)})
    assert snapshot is not None and snapshot.pipeline == "cascade"
    # cascade-SHAPED, not merely cascade-named
    assert set(snapshot.doc["providers"]) == set(CASCADE_PROVIDERS)
    assert "realtime" not in snapshot.doc["providers"]

    config = cascade_config.build_cascade_config(
        snapshot.doc, snapshot.registry, {},
        base_prompt="p")
    assert config["stt"]["provider"] == "deepgram"
    assert config["llm"]["provider"] == "nvidia-nemotron"
    assert config["tts"]["provider"] == "elevenlabs"
    # the honesty gates the live lane refuses on
    assert config["stt"]["wired_live"] and config["tts"]["wired_live"]


def test_b3_pstn_cascade_is_outbound_only(tmp_path, monkeypatch):
    """Pinned so the matrix is not 'completed' with a cell the code refuses."""
    monkeypatch.setattr(profiles, "CASCADE_CAPABILITY", {"phone": frozenset({"outbound", "inbound"})})
    d = write_config_dir(tmp_path, [{"id": "pstn-agent", "pipeline": "cascade",
                                     "providers": dict(CASCADE_PROVIDERS)}])
    (d / "active.yaml").write_text(yaml.safe_dump(
        {"outlets": {o: {"inbound": "pstn-agent", "outbound": "pstn-agent"}
                     for o in profiles.OUTLETS}}))
    with pytest.raises(profiles.ProfileError) as exc:
        profiles.load_effective_profile("inbound", env={"VOICE_CONFIG_DIR": str(d)})
    assert "outbound-only" in str(exc.value)


def test_b3_cascade_refused_when_host_flag_is_off(tmp_path, monkeypatch):
    """Negative control for the host gate -- without it, b3's pass could be vacuous."""
    monkeypatch.setattr(profiles, "CASCADE_CAPABILITY", {})
    d = _cfg_dir(tmp_path, "cascade", CASCADE_PROVIDERS)
    with pytest.raises(profiles.ProfileError) as exc:
        profiles.load_effective_profile("outbound", env={"VOICE_CONFIG_DIR": str(d)})
    assert "not implemented in this bridge" in str(exc.value)


# -- b4: PSTN-realtime ---------------------------------------------------------

def test_b4_pstn_realtime_resolves_through_the_real_loader(tmp_path):
    d = _cfg_dir(tmp_path, "realtime", REALTIME_PROVIDERS, aid="rt-agent")
    snapshot = profiles.load_effective_profile(
        "outbound", env={"VOICE_CONFIG_DIR": str(d)})
    assert snapshot is not None and snapshot.pipeline == "realtime"
    assert snapshot.doc["providers"]["realtime"] == "openai-gpt-realtime"
    assert set(snapshot.doc["providers"]) == {"realtime"}


def test_b4_the_two_pstn_lanes_are_distinguishable(tmp_path, monkeypatch):
    """One loader, one config dir shape, two pipelines -- the lane follows the doc."""
    monkeypatch.setattr(profiles, "CASCADE_CAPABILITY", {"phone": frozenset({"outbound", "inbound"})})
    cas = profiles.load_effective_profile("outbound", env={
        "VOICE_CONFIG_DIR": str(_cfg_dir(tmp_path / "a", "cascade",
                                         CASCADE_PROVIDERS))})
    rt = profiles.load_effective_profile("outbound", env={
        "VOICE_CONFIG_DIR": str(_cfg_dir(tmp_path / "b", "realtime",
                                         REALTIME_PROVIDERS))})
    assert (cas.pipeline, rt.pipeline) == ("cascade", "realtime")
    assert cas.doc["providers"].keys() != rt.doc["providers"].keys()


# -- b5: the eventlog row, READ BACK OFF DISK ----------------------------------

LANES = [
    ("twilio", "realtime"),
    ("twilio", "cascade"),
    ("talk", "realtime"),
    ("talk", "cascade"),
]


@pytest.mark.parametrize("mode,pipeline", LANES)
def test_b5_each_lane_writes_its_own_mode_pipeline_pair(tmp_path, mode, pipeline):
    """All four lanes, distinguishable on disk.

    Asserted from the JSONL the dashboard actually reads -- not from the kwarg the
    recorder was handed, which would prove only that the test passed what it passed.
    """
    path = tmp_path / "events.jsonl"
    rec = eventlog.CallRecorder(call_id=f"{mode}-{pipeline}", mode=mode,
                                pipeline=pipeline, direction="outbound",
                                target="Alex", path=str(path))
    rec.finish(outcome="ok")

    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    terminal = [r for r in rows if r.get("call_id") == f"{mode}-{pipeline}"]
    assert terminal, f"no row on disk for {mode}/{pipeline}"
    row = terminal[-1]
    assert row["mode"] == mode
    assert row["pipeline"] == pipeline


# The FOUR real production construction sites. b5's first cut read back the pair the
# TEST passed in -- tautological, and the evaluator proved it blind by mutating
# server.py's writer to mode="cascade" with the suite still green. Lane IDENTITY must
# therefore be derived from the PRODUCTION source, not from a parametrize list.
PRODUCTION_WRITERS = {
    "voice/server.py": {("twilio", "realtime"), ("twilio", "cascade")},
    "talk-voice-bridge/cascade_bridge.py": {("talk", "cascade")},
    "talk-voice-bridge/realtime_bridge.py": {("talk", "realtime")},
}


def _writer_pairs(rel_path):
    """(mode, pipeline) literal pairs at every eventlog.CallRecorder(...) in a file."""
    import ast
    src = (SERVICES / rel_path).read_text()
    pairs = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name != "CallRecorder":
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        mode, pipeline = kw.get("mode"), kw.get("pipeline")
        if isinstance(mode, ast.Constant) and isinstance(pipeline, ast.Constant):
            pairs.add((mode.value, pipeline.value))
    return pairs


@pytest.mark.parametrize("rel_path,expected", sorted(PRODUCTION_WRITERS.items()))
def test_b5_production_writers_declare_their_own_lane(rel_path, expected):
    """Each PRODUCTION writer is pinned to its lane at its real construction site.

    This is what catches the s12c lane-identity collapse (mode='cascade' folding
    transport and pipeline into one field): mutate any writer's mode/pipeline and this
    goes red, which the read-back test alone did not.
    """
    assert _writer_pairs(rel_path) == expected, (
        f"{rel_path} declares {_writer_pairs(rel_path)}, expected {expected} — a "
        f"production writer changed lane identity")


def test_b5_the_four_production_writers_cover_all_four_lanes():
    """Set-level: the union of the real writers is exactly the four lanes, each once."""
    union = set()
    for rel in PRODUCTION_WRITERS:
        union |= _writer_pairs(rel)
    assert union == set(LANES)
    assert len(union) == 4


def test_b5_writer_scan_is_not_vacuous():
    """Anti-vacuity: the AST scan must actually find CallRecorder calls in each file."""
    for rel in PRODUCTION_WRITERS:
        assert _writer_pairs(rel), f"scan found no CallRecorder in {rel}"


def test_b5_the_four_lanes_are_mutually_distinguishable_on_disk(tmp_path):
    """The point of the (mode, pipeline) split: four rows, four distinct identities.

    Before s12c the live rows carried mode='cascade', collapsing transport and pipeline
    into one field -- talk-cascade and pstn-cascade were indistinguishable.
    """
    path = tmp_path / "events.jsonl"
    for mode, pipeline in LANES:
        eventlog.CallRecorder(call_id=f"{mode}-{pipeline}", mode=mode, pipeline=pipeline,
                              direction="outbound", path=str(path)).finish(outcome="ok")

    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    seen = {(r["mode"], r["pipeline"]) for r in rows if r.get("pipeline")}
    assert seen == set(LANES), f"lanes not distinguishable on disk: {seen}"
    assert len(seen) == 4
