"""s15b-A a4: the fixture-fidelity guard.

Fails the PYTEST RUN (not a separate lint step anyone can forget) if any test in this
suite builds a cascade-NAMED, realtime-SHAPED profile -- the fixture defect that let
three separate bugs ship past a green suite.

Deliberately scans SOURCE, not ``profile_doc`` output: the consult's named lazy pass was
a guard that only inspects the helper's return value while inline dict literals -- which
is how most cascade fixtures in this repo are actually written -- slip straight past it.

Deliberate poison (negative controls that must build a malformed doc to prove the
product rejects it) opts out with an explicit marker comment on, or immediately above,
the offending line. Accidental poison is impossible; deliberate poison is greppable.

This file is byte-identical across the voice / talk-voice-bridge / voice-control suites;
test_s15b_parity pins that.
"""
import ast
import io
import tokenize
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
MARKER = "s15b: deliberate poison"
CASCADE_ROLES = ("stt", "llm", "tts")


def _marked_lines(source: str) -> set:
    """Line numbers carrying the opt-out marker, plus the line below each."""
    out = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT and MARKER in tok.string:
                out.add(tok.start[0])
                out.add(tok.start[0] + 1)
    except (tokenize.TokenError, IndentationError):
        pass
    return out


def _const_str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _dict_keys(node) -> dict:
    """{literal-str-key: value-node} for a dict literal."""
    if not isinstance(node, ast.Dict):
        return {}
    out = {}
    for k, v in zip(node.keys, node.values):
        ks = _const_str(k) if k is not None else None
        if ks is not None:
            out[ks] = v
    return out


def scan_source(source: str, label: str) -> list:
    """Violations in one python source string: cascade-named, realtime-shaped docs."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    marked = _marked_lines(source)
    violations = []

    for node in ast.walk(tree):
        keys = _dict_keys(node)
        if _const_str(keys.get("pipeline")) != "cascade":
            continue
        if node.lineno in marked:
            continue

        providers = keys.get("providers")
        # A cascade doc with no providers key at all is the StubProfile shape -- it is
        # only a fixture defect when this dict is meant to BE a profile doc, which we
        # infer from it carrying an id/pipeline pair.
        if providers is None:
            if "id" in keys:
                violations.append(
                    f"{label}:{node.lineno}: cascade profile with no providers block")
            continue
        if not isinstance(providers, ast.Dict):
            continue
        pkeys = _dict_keys(providers)
        if providers.lineno in marked:
            continue
        if "realtime" in pkeys:
            violations.append(
                f"{label}:{node.lineno}: cascade profile carries providers.realtime "
                f"— cascade in NAME, realtime in SHAPE (the s14b defect class)")
        missing = [r for r in CASCADE_ROLES if r not in pkeys]
        if missing and not any(isinstance(k, ast.Starred) for k in providers.values):
            violations.append(
                f"{label}:{node.lineno}: cascade profile missing providers "
                f"{missing} — the cascade lane resolves all of {list(CASCADE_ROLES)}")
    return violations


def scan_tests_dir(directory: Path) -> list:
    out = []
    for path in sorted(Path(directory).rglob("*.py")):
        out += scan_source(path.read_text(), path.name)
    return out


def count_cascade_docs(directory: Path) -> int:
    """How many cascade-pipeline dict literals the scanner actually EXAMINED.

    Anti-vacuity: a guard that is green because it found nothing to inspect proves
    nothing at all. Every suite must have real cascade fixtures under the lens.
    """
    total = 0
    for path in sorted(Path(directory).rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if _const_str(_dict_keys(node).get("pipeline")) == "cascade":
                total += 1
    return total


# -- the guard itself ---------------------------------------------------------

def test_a4_no_cascade_named_realtime_shaped_fixtures():
    violations = scan_tests_dir(TESTS_DIR)
    assert violations == [], (
        "cascade-named, realtime-shaped test fixtures found — this is the exact defect "
        "that shipped three bugs past a green suite:\n  " + "\n  ".join(violations))


# -- negative controls: the guard must be able to FIRE ------------------------

POISON = '''
doc = {"id": "x", "pipeline": "cascade",
       "providers": {"realtime": "openai-gpt-realtime"}}
'''
HYBRID = '''
doc = {"id": "x", "pipeline": "cascade",
       "providers": {"stt": "deepgram", "llm": "nvidia-nemotron",
                     "tts": "elevenlabs", "realtime": "openai-gpt-realtime"}}
'''
INCOMPLETE = '''
doc = {"id": "x", "pipeline": "cascade", "providers": {"stt": "deepgram"}}
'''
NO_PROVIDERS = '''
doc = {"id": "x", "pipeline": "cascade"}
'''
HONEST = '''
doc = {"id": "x", "pipeline": "cascade",
       "providers": {"stt": "deepgram", "llm": "nvidia-nemotron",
                     "tts": "elevenlabs"}}
'''
MARKED = '''
doc = {"id": "x", "pipeline": "cascade",  # s15b: deliberate poison
       "providers": {"realtime": "openai-gpt-realtime"}}
'''


def test_a4_guard_catches_the_historical_poison():
    assert scan_source(POISON, "f.py"), "guard blind to the exact s14b shape"
    assert "providers.realtime" in scan_source(POISON, "f.py")[0]


def test_a4_guard_catches_hybrid():
    """{stt,llm,tts,realtime}: complete AND poisoned. Both rules must bind."""
    assert any("realtime" in v for v in scan_source(HYBRID, "f.py"))


def test_a4_guard_catches_incomplete_and_absent_providers():
    assert scan_source(INCOMPLETE, "f.py")
    assert scan_source(NO_PROVIDERS, "f.py"), "the StubProfile shape must be caught"


def test_a4_guard_passes_honest_cascade():
    """Positive control -- without it the guard could be a bare `return [violation]`."""
    assert scan_source(HONEST, "f.py") == []


def test_a4_marker_opts_out():
    assert scan_source(MARKED, "f.py") == []


def test_a4_guard_actually_reads_this_suite():
    """Anti-vacuity: prove the scan really walks this directory and sees real files,
    so a green guard cannot mean 'scanned nothing'."""
    files = list(TESTS_DIR.rglob("*.py"))
    assert len(files) > 5
    assert any(f.name == "test_s15b_fixture_guard.py" for f in files)


def test_a4_guard_examined_real_cascade_fixtures():
    """The stronger anti-vacuity control: this suite really does contain cascade
    profile fixtures, so a green a4 means 'inspected and clean', not 'found none'."""
    found = count_cascade_docs(TESTS_DIR)
    assert found >= 2, (
        f"only {found} cascade fixtures under the guard in {TESTS_DIR.parent.name} — "
        "a green guard here would be vacuous")


# -- a6: twin discipline ------------------------------------------------------
# The product modules are no longer duplicated — every service imports voicecore (VC17).
# What is still hand-copied is TEST scaffolding: this guard and the s15b-A block of
# profile_helpers.py, which each suite keeps its own copy of. These two tests are the
# teeth for that remaining duplication, and nothing else.

SERVICES = TESTS_DIR.parent.parent
SUITES = ("voice", "talk-voice-bridge", "voice-control")
S15B_BLOCK_START = "# s15b-A: the provider roles"
S15B_BLOCK_END = "def write_config_dir"


def _sha(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a6_this_guard_is_identical_across_suites():
    paths = [SERVICES / s / "tests" / "test_s15b_fixture_guard.py" for s in SUITES]
    present = [p for p in paths if p.exists()]
    assert len(present) >= 2, "the guard must be installed in more than one suite"
    shas = {p: _sha(p) for p in present}
    assert len(set(shas.values())) == 1, (
        "the fixture guard has DRIFTED between suites:\n  " +
        "\n  ".join(f"{p}: {s[:12]}" for p, s in shas.items()))


def test_a6_profile_doc_block_is_identical_across_twins():
    """profile_helpers.py as a WHOLE has legitimately diverged (voice uses
    parity_env.env_sandbox, talk uses asyncio realtime_bridge) -- so pin only the
    s15b-A block, which must not."""
    import hashlib
    blocks = {}
    for s in SUITES:
        path = SERVICES / s / "tests" / "profile_helpers.py"
        if not path.exists():
            continue
        src = path.read_text()
        if S15B_BLOCK_START not in src:
            continue
        block = src[src.index(S15B_BLOCK_START):src.index(S15B_BLOCK_END)]
        blocks[str(path)] = hashlib.sha256(block.encode()).hexdigest()
    assert len(blocks) >= 2, "expected the s15b-A block in at least two twins"
    assert len(set(blocks.values())) == 1, (
        "the s15b-A profile_doc block has DRIFTED:\n  " +
        "\n  ".join(f"{p}: {s[:12]}" for p, s in blocks.items()))
