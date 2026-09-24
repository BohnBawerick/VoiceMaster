"""Tests for supervisor/hermes_profile_registry.py.

Run from the hermes/ directory:

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests -q
"""

import argparse
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "supervisor"))

import hermes_profile_registry as reg  # noqa: E402


# --- helpers ---------------------------------------------------------------


def make_profile(root, name, config="model: test\n", env=None, incomplete=False):
    """Create a profile directory the way `hermes profile create` leaves one."""
    path = os.path.join(root, name)
    os.makedirs(path, exist_ok=True)
    if config is not None:
        with open(os.path.join(path, "config.yaml"), "w") as fh:
            fh.write(config)
    if env is not None:
        with open(os.path.join(path, ".env"), "w") as fh:
            fh.write(env)
    if incomplete:
        open(os.path.join(path, reg.INCOMPLETE_MARKER), "w").close()
    return path


def by_name(records):
    return dict((r.name, r) for r in records)


RESERVED = dict(reg.NAMESPACE_PORTS)


# --- degrade-to-today behaviour -------------------------------------------


def test_missing_profiles_dir_yields_nothing(tmp_path):
    """No profiles directory is the single-default-gateway container of today."""
    records = reg.discover(str(tmp_path / "nope"), reserved_ports=RESERVED)
    assert records == []


def test_empty_profiles_dir_yields_nothing(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    assert reg.discover(str(root), reserved_ports=RESERVED) == []


# --- discovery is by directory --------------------------------------------


def test_directory_with_config_is_discovered_without_any_env_var(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "newagent")
    monkeypatch.delenv("HERMES_PROFILES", raising=False)

    records = reg.discover(str(root), reserved_ports=RESERVED)

    assert len(records) == 1
    rec = records[0]
    assert rec.name == "newagent"
    assert rec.status == reg.OK
    assert rec.port == reg.PORT_POOL_START
    assert rec.url == "http://127.0.0.1:%d" % reg.PORT_POOL_START


def test_profile_without_env_gets_every_api_var_injected(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "newagent")
    rec = reg.discover(str(root), reserved_ports=RESERVED)[0]
    assert sorted(rec.inject) == ["ENABLED", "HOST", "KEY", "PORT"]


def test_profile_env_port_is_honoured_and_not_reinjected(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "concierge",
                 env="API_SERVER_PORT=18790\nAPI_SERVER_KEY=abc\n")
    rec = reg.discover(str(root), reserved_ports=RESERVED)[0]
    assert rec.status == reg.OK
    assert rec.port == 18790
    assert rec.explicit_port is True
    # .env supplies PORT and KEY, so the supervisor must not override them.
    assert sorted(rec.inject) == ["ENABLED", "HOST"]


def test_config_yaml_api_server_port_is_honoured(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "cfgport",
                 config="api_server:\n  port: 18795\n  host: 0.0.0.0\n")
    rec = reg.discover(str(root), reserved_ports=RESERVED)[0]
    assert rec.port == 18795
    assert rec.url == "http://127.0.0.1:18795"


# --- the default profile deterministically owns the shared port ------------

def test_profile_may_not_claim_the_default_api_port(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "greedy", env="API_SERVER_PORT=18789\n")
    rec = reg.discover(str(root), reserved_ports=RESERVED)[0]
    assert rec.status == reg.CONFLICT
    assert "reserved" in rec.error
    assert rec.port is None
    assert rec.url is None


def test_auto_assignment_never_lands_on_a_reserved_port(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    for i in range(3):
        make_profile(str(root), "auto%d" % i)
    records = reg.discover(str(root), reserved_ports=RESERVED)
    ports = [r.port for r in records]
    assert reg.DEFAULT_API_PORT not in ports
    assert reg.DEFAULT_WEBUI_PORT not in ports
    assert ports == [18790, 18791, 18792]


def test_reserved_ports_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("API_SERVER_PORT", "18999")
    monkeypatch.setenv("HERMES_WEBUI_PORT", "9999")
    monkeypatch.delenv("HERMES_RESERVED_PORTS", raising=False)
    reserved = reg.reserved_ports_from_env()
    assert 18999 in reserved and 9999 in reserved


def test_reserved_ports_fall_back_when_env_is_garbage(monkeypatch):
    monkeypatch.setenv("API_SERVER_PORT", "not-a-port")
    monkeypatch.delenv("HERMES_WEBUI_PORT", raising=False)
    monkeypatch.delenv("HERMES_RESERVED_PORTS", raising=False)
    reserved = reg.reserved_ports_from_env()
    assert reg.DEFAULT_API_PORT in reserved
    assert reg.DEFAULT_WEBUI_PORT in reserved


# --- the OTHER services in the shared network namespace (F3) ---------------
#
# When the VoiceMaster services share one network namespace with the gateways,
# there is one flat port space. A profile that takes the phone bridge's 3336 kills
# the inbound phone number the next time the bridge restarts.


@pytest.mark.parametrize("port,owner", sorted(reg.NAMESPACE_PORTS.items()))
def test_no_profile_may_claim_a_port_another_service_is_listening_on(tmp_path, port, owner):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "thief", env="API_SERVER_PORT=%d\n" % port)
    make_profile(str(root), "honest")

    recs = by_name(reg.discover(str(root), reserved_ports=reg.reserved_ports_from_env()))

    assert recs["thief"].status == reg.CONFLICT, "port %d (%s) was stealable" % (port, owner)
    assert "reserved" in recs["thief"].error
    assert owner in recs["thief"].error   # the log has to name who owns it
    assert recs["thief"].port is None
    assert recs["honest"].status == reg.OK  # and the neighbour is untouched


def test_the_phone_bridges_port_is_reserved_via_config_yaml_too(tmp_path):
    """3336 is the phone bridge. The .env route is not the only way to ask for it."""
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "modecthief", config="api_server:\n  port: 3336\n")
    rec = reg.discover(str(root), reserved_ports=reg.reserved_ports_from_env())[0]
    assert rec.status == reg.CONFLICT
    assert "phone bridge" in rec.error


def test_extra_ports_can_be_reserved_without_a_code_change(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_RESERVED_PORTS", "4444, junk ,5555")
    reserved = reg.reserved_ports_from_env()
    assert 4444 in reserved and 5555 in reserved
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "newport", env="API_SERVER_PORT=4444\n")
    rec = reg.discover(str(root), reserved_ports=reserved)[0]
    assert rec.status == reg.CONFLICT


def test_auto_assignment_never_lands_on_any_namespace_port(tmp_path):
    """The pool must not overlap anything already listening in the namespace."""
    overlap = set(reg.NAMESPACE_PORTS) & set(range(reg.PORT_POOL_START,
                                                   reg.PORT_POOL_END + 1))
    assert overlap == set(), "port pool overlaps %s" % sorted(overlap)


# --- a broken profile must not take out its neighbours ---------------------


def test_half_created_directory_is_incomplete_and_isolated(tmp_path):
    """`mkdir profiles/half` with nothing in it. The good profile still runs."""
    root = tmp_path / "profiles"
    root.mkdir()
    os.makedirs(str(root / "half"))
    make_profile(str(root), "good")

    recs = by_name(reg.discover(str(root), reserved_ports=RESERVED))

    assert recs["half"].status == reg.INCOMPLETE
    assert "config.yaml" in recs["half"].error
    assert recs["good"].status == reg.OK
    assert recs["good"].port == reg.PORT_POOL_START


def test_incomplete_marker_holds_a_profile_back(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "wizard", incomplete=True)
    rec = reg.discover(str(root), reserved_ports=RESERVED)[0]
    assert rec.status == reg.INCOMPLETE
    assert reg.INCOMPLETE_MARKER in rec.error


def test_invalid_yaml_is_reported_and_isolated(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "broken", config="model: [unclosed\n  bad: : :\n")
    make_profile(str(root), "good")

    recs = by_name(reg.discover(str(root), reserved_ports=RESERVED))

    assert recs["broken"].status == reg.INVALID
    assert "YAML" in recs["broken"].error
    assert recs["broken"].url is None
    assert recs["good"].status == reg.OK


def test_config_yaml_that_is_not_a_mapping_is_invalid(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "listy", config="- a\n- b\n")
    rec = reg.discover(str(root), reserved_ports=RESERVED)[0]
    assert rec.status == reg.INVALID
    assert "mapping" in rec.error


def test_non_integer_port_is_invalid_not_fatal(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "badport", env="API_SERVER_PORT=eighteen\n")
    make_profile(str(root), "good")

    recs = by_name(reg.discover(str(root), reserved_ports=RESERVED))

    assert recs["badport"].status == reg.INVALID
    assert "not an integer" in recs["badport"].error
    assert recs["good"].status == reg.OK


def test_out_of_range_port_is_invalid(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "huge", env="API_SERVER_PORT=99999\n")
    rec = reg.discover(str(root), reserved_ports=RESERVED)[0]
    assert rec.status == reg.INVALID
    assert "out of range" in rec.error


def test_unreadable_profile_directory_is_isolated(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    root = tmp_path / "profiles"
    root.mkdir()
    locked = make_profile(str(root), "locked")
    make_profile(str(root), "good")
    os.chmod(locked, 0o000)
    try:
        recs = by_name(reg.discover(str(root), reserved_ports=RESERVED))
        assert recs["locked"].status == reg.INVALID
        assert recs["good"].status == reg.OK
    finally:
        os.chmod(locked, 0o755)


def test_non_directory_and_bad_names_are_ignored(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    (root / "README.md").write_text("not a profile\n")
    (root / "bad name").mkdir()
    (root / ".hidden").mkdir()
    make_profile(str(root), "good")

    recs = by_name(reg.discover(str(root), reserved_ports=RESERVED))

    assert recs["README.md"].status == reg.IGNORED
    assert recs["bad name"].status == reg.IGNORED
    assert recs[".hidden"].status == reg.IGNORED
    assert recs["good"].status == reg.OK


def test_missing_pyyaml_does_not_condemn_every_profile(tmp_path, monkeypatch):
    """No parser means "cannot validate", not "everything is broken"."""
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "good", config="model: [unclosed\n")
    monkeypatch.setitem(sys.modules, "yaml", None)
    rec = reg.discover(str(root), reserved_ports=RESERVED)[0]
    assert rec.status == reg.OK


# --- the guarantee holds without PyYAML too (F6) ---------------------------
#
# The file's docstring states the reserved-port rule unconditionally ("no discovered
# profile may EVER claim it"). Before this, an ImportError silently switched the
# config.yaml half of that check off.


def test_config_yaml_port_is_still_seen_without_pyyaml(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "yamlthief", config="api_server:\n  port: 18789\n")
    monkeypatch.setitem(sys.modules, "yaml", None)
    rec = reg.discover(str(root), reserved_ports=reg.reserved_ports_from_env())[0]
    assert rec.status == reg.CONFLICT
    assert "reserved" in rec.error


def test_port_scan_without_pyyaml_reads_only_the_api_server_block(tmp_path):
    """Narrow on purpose: it must not invent a port that is not really declared."""
    p = tmp_path / "config.yaml"
    p.write_text("# api_server:\n#   port: 1111\n"
                 "webui:\n  port: 2222\n"
                 "model: test\n")
    assert reg._scan_api_server_port(str(p)) is None

    p.write_text("model: test\napi_server:\n  host: 0.0.0.0\n  port: 18789   # mine\n")
    assert reg._scan_api_server_port(str(p)) == "18789"

    p.write_text("api_server:\n  port: '18790'\n")
    assert reg._scan_api_server_port(str(p)) == "18790"

    # A `port:` that has left the api_server block behind is not ours.
    p.write_text("api_server:\n  host: 0.0.0.0\nwebui:\n  port: 8787\n")
    assert reg._scan_api_server_port(str(p)) is None

    # Nor is one nested deeper inside it. `api_server.tls.port` is not the port
    # the gateway binds, and guessing it would invent a conflict that is not real.
    p.write_text("api_server:\n  host: 0.0.0.0\n  tls:\n    port: 443\n")
    assert reg._scan_api_server_port(str(p)) is None

    p.write_text("api_server:\n  tls:\n    port: 443\n  port: 18789\n")
    assert reg._scan_api_server_port(str(p)) == "18789"


def test_missing_pyyaml_is_announced_not_silent(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "good")
    monkeypatch.setitem(sys.modules, "yaml", None)
    rc, out, err, _ = _run_cli(tmp_path, root)
    assert rc == 0
    assert "PyYAML is not importable" in err


# --- 'default' is the container's own profile (F8) -------------------------


def test_a_profile_directory_named_default_is_refused(tmp_path):
    """Two gateways over one profile home would share its sessions and state db."""
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "default")
    make_profile(str(root), "good")

    recs = by_name(reg.discover(str(root), reserved_ports=RESERVED))

    assert recs["default"].status == reg.INVALID
    assert "container's own profile" in recs["default"].error
    assert recs["default"].port is None
    assert recs["good"].status == reg.OK
    assert recs["good"].port == reg.PORT_POOL_START   # not pushed along by it


def test_a_refused_default_directory_is_in_the_registry_and_the_log(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "default")
    rc, out, err, registry_path = _run_cli(tmp_path, root)
    assert rc == 0
    assert out.strip() == ""                       # never started
    assert "default invalid" in err
    assert reg.read_registry(registry_path)["profiles"]["default"]["status"] == reg.INVALID


# --- nothing under profiles/ disappears silently (F2) ----------------------
#
# docs/HERMES_ARCHITECTURE.md § 4.5 promises "broken is never invisible". A
# directory that fails NAME_RE, or a dangling symlink, used to produce no registry
# entry and no log line at all - the exact failure this ticket exists to remove.


def test_a_real_profile_in_a_badly_named_directory_is_reported(tmp_path):
    """`My Agent/` has a valid config.yaml and will never start. Say so."""
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "My Agent")
    records = reg.discover(str(root), reserved_ports=RESERVED)
    rec = by_name(records)["My Agent"]

    assert rec.status == reg.IGNORED
    assert rec.reportable is True
    registry = reg.build_registry(records, "http://localhost:18789")
    assert registry["profiles"]["My Agent"]["status"] == reg.IGNORED
    assert "rename" in registry["profiles"]["My Agent"]["error"]


def test_a_dangling_symlink_under_profiles_is_reported(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    os.symlink(str(tmp_path / "gone"), str(root / "brokenlink"))
    records = reg.discover(str(root), reserved_ports=RESERVED)
    rec = by_name(records)["brokenlink"]

    assert rec.status == reg.IGNORED
    assert rec.reportable is True
    assert "unreadable" in rec.error
    assert "brokenlink" in reg.build_registry(records, "http://x")["profiles"]


def test_a_profile_shaped_name_that_is_a_file_is_reported(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    (root / "notadir").write_text("oops\n")
    records = reg.discover(str(root), reserved_ports=RESERVED)
    rec = by_name(records)["notadir"]
    assert rec.status == reg.IGNORED
    assert rec.reportable is True
    assert "notadir" in reg.build_registry(records, "http://x")["profiles"]


def test_scratch_files_stay_quiet(tmp_path):
    """The other half of the rule: noise must not drown the news."""
    root = tmp_path / "profiles"
    root.mkdir()
    (root / "README.md").write_text("not a profile\n")
    (root / ".hidden").mkdir()
    (root / "notes").mkdir()          # a bare directory, no config.yaml
    records = reg.discover(str(root), reserved_ports=RESERVED)
    recs = by_name(records)

    assert recs["README.md"].reportable is False
    assert recs[".hidden"].reportable is False
    registry = reg.build_registry(records, "http://x")
    assert "README.md" not in registry["profiles"]
    assert ".hidden" not in registry["profiles"]
    # `notes/` is INCOMPLETE, not IGNORED - it is a directory and may yet grow a
    # config.yaml, so it is reported and retried.
    assert registry["profiles"]["notes"]["status"] == reg.INCOMPLETE


def test_a_hidden_directory_holding_a_config_is_still_reported(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), ".halfmoved")
    records = reg.discover(str(root), reserved_ports=RESERVED)
    assert by_name(records)[".halfmoved"].reportable is True
    assert ".halfmoved" in reg.build_registry(records, "http://x")["profiles"]


def test_cli_reports_ignored_entries_that_hold_a_profile(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "My Agent")
    (root / "README.md").write_text("x\n")
    make_profile(str(root), "good")

    rc, out, err, _ = _run_cli(tmp_path, root)

    assert rc == 0
    assert "My Agent ignored" in err
    assert "README.md" not in err        # noise stays quiet
    assert "START\tgood" in out


def test_an_ignored_entry_is_never_routable(tmp_path):
    """Reported, but still not somewhere a call may be sent."""
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "My Agent")
    records = reg.discover(str(root), reserved_ports=RESERVED)
    path = str(tmp_path / "gateways.json")
    reg.write_registry(path, reg.build_registry(records, "http://localhost:18789"))
    assert reg.gateway_url_for_profile("My Agent", registry_path=path, env={}) is None


# --- a single bad entry never ends the scan (F7) ---------------------------


def test_one_entry_blowing_up_does_not_end_the_scan(tmp_path, monkeypatch):
    """The per-entry guard in discover(). Every error path _inspect knows about is
    handled inside _inspect, so only an injected fault reaches this one."""
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "aaa")
    make_profile(str(root), "boom")
    make_profile(str(root), "zzz")

    real_inspect = reg._inspect

    def exploding(name, profiles_dir):
        if name == "boom":
            raise RuntimeError("something nobody predicted")
        return real_inspect(name, profiles_dir)

    monkeypatch.setattr(reg, "_inspect", exploding)
    recs = by_name(reg.discover(str(root), reserved_ports=RESERVED))

    assert recs["boom"].status == reg.INVALID
    assert "discovery failed: RuntimeError: something nobody predicted" in recs["boom"].error
    assert recs["boom"].port is None
    # Both neighbours - the one before it and the one after it - still start.
    assert recs["aaa"].status == reg.OK
    assert recs["zzz"].status == reg.OK
    assert [recs["aaa"].port, recs["zzz"].port] == [18790, 18791]


def test_an_entry_that_blows_up_is_reported_not_dropped(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "boom")
    monkeypatch.setattr(reg, "_inspect", lambda *a: (_ for _ in ()).throw(OSError("io")))
    rc, out, err, registry_path = _run_cli(tmp_path, root)
    assert rc == 0
    assert "boom invalid" in err
    assert reg.read_registry(registry_path)["profiles"]["boom"]["status"] == reg.INVALID


# --- two profiles claiming the same port -----------------------------------


def test_two_profiles_claiming_one_port_resolve_deterministically(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "zulu", env="API_SERVER_PORT=18790\n")
    make_profile(str(root), "alpha", env="API_SERVER_PORT=18790\n")

    recs = by_name(reg.discover(str(root), reserved_ports=RESERVED))

    # Lexicographically first wins, on every boot, in either listdir order.
    assert recs["alpha"].status == reg.OK
    assert recs["alpha"].port == 18790
    assert recs["zulu"].status == reg.CONFLICT
    assert "already claimed by alpha" in recs["zulu"].error
    assert recs["zulu"].url is None


def test_auto_assignment_does_not_steal_an_explicit_port(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "aaa")                                   # auto
    make_profile(str(root), "bbb", env="API_SERVER_PORT=18790\n")    # explicit
    recs = by_name(reg.discover(str(root), reserved_ports=RESERVED))
    assert recs["bbb"].port == 18790
    assert recs["aaa"].port == 18791


def test_exhausted_port_pool_is_a_conflict_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(reg, "PORT_POOL_START", 18790)
    monkeypatch.setattr(reg, "PORT_POOL_END", 18791)
    root = tmp_path / "profiles"
    root.mkdir()
    for name in ("a", "b", "c"):
        make_profile(str(root), name)
    recs = by_name(reg.discover(str(root), reserved_ports=RESERVED))
    assert recs["a"].status == reg.OK
    assert recs["b"].status == reg.OK
    assert recs["c"].status == reg.CONFLICT
    assert "no free port" in recs["c"].error


# --- sticky ports ----------------------------------------------------------


def test_previous_assignment_is_sticky(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "aaa")
    make_profile(str(root), "zzz")
    # zzz already held 18790 before aaa existed; adding aaa must not renumber it.
    recs = by_name(reg.discover(str(root), reserved_ports=RESERVED,
                                previous={"zzz": 18790}))
    assert recs["zzz"].port == 18790
    assert recs["aaa"].port == 18791


# --- .env parsing ----------------------------------------------------------


def test_env_parsing_handles_quotes_exports_comments_and_junk(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "# a comment\n"
        "\n"
        "export API_SERVER_PORT=18790\n"
        'API_SERVER_KEY="quoted+key="\n'
        "API_SERVER_HOST='127.0.0.1'\n"
        "this line has no equals sign\n"
        "=novalue\n"
    )
    env = reg.parse_env_file(str(p))
    assert env["API_SERVER_PORT"] == "18790"
    assert env["API_SERVER_KEY"] == "quoted+key="
    assert env["API_SERVER_HOST"] == "127.0.0.1"
    assert "this line has no equals sign" not in env


def test_env_parsing_of_a_missing_file_is_empty(tmp_path):
    assert reg.parse_env_file(str(tmp_path / "nothing")) == {}


# --- registry file ---------------------------------------------------------


def test_registry_round_trip(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "good")
    records = reg.discover(str(root), reserved_ports=RESERVED)
    registry = reg.build_registry(records, "http://localhost:18789",
                                  running={"good": 4242}, now=111)
    path = str(tmp_path / "gw" / "gateways.json")
    reg.write_registry(path, registry)

    loaded = reg.read_registry(path)
    assert loaded["version"] == reg.REGISTRY_VERSION
    assert loaded["default"]["gateway_url"] == "http://localhost:18789"
    assert loaded["profiles"]["good"]["status"] == reg.OK
    assert loaded["profiles"]["good"]["running"] is True
    assert loaded["profiles"]["good"]["pid"] == 4242
    assert loaded["profiles"]["good"]["gateway_url"] == "http://127.0.0.1:18790"


def test_eligible_but_not_yet_running_is_not_routable(tmp_path):
    """A gateway that has not started yet must not be advertised as reachable."""
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "good")
    records = reg.discover(str(root), reserved_ports=RESERVED)
    registry = reg.build_registry(records, "http://localhost:18789", running={})
    entry = registry["profiles"]["good"]
    assert entry["status"] == "starting"
    assert entry["gateway_url"] is None
    assert entry["running"] is False


def test_registry_records_broken_profiles_so_the_ui_can_see_them(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "broken", config="a: [\n")
    os.makedirs(str(root / "half"))
    (root / "README.md").write_text("x")

    records = reg.discover(str(root), reserved_ports=RESERVED)
    registry = reg.build_registry(records, "http://localhost:18789")

    assert registry["profiles"]["broken"]["status"] == reg.INVALID
    assert registry["profiles"]["broken"]["error"]
    assert registry["profiles"]["half"]["status"] == reg.INCOMPLETE
    # IGNORED entries are somebody's scratch directory, not news.
    assert "README.md" not in registry["profiles"]


def test_write_is_atomic_no_partial_file_visible(tmp_path, monkeypatch):
    path = str(tmp_path / "gateways.json")
    reg.write_registry(path, {"version": 1, "profiles": {}})
    real_replace = os.replace
    seen = {}

    def spy(src, dst):
        seen["existing"] = open(dst).read()
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    reg.write_registry(path, {"version": 1, "profiles": {"x": {}}})
    # The old complete file was still fully intact right up to the rename.
    assert json.loads(seen["existing"]) == {"version": 1, "profiles": {}}
    assert reg.read_registry(path)["profiles"] == {"x": {}}


def test_corrupt_registry_reads_as_none(tmp_path):
    path = tmp_path / "gateways.json"
    path.write_text('{"version": 1, "profi')
    assert reg.read_registry(str(path)) is None


def test_missing_registry_reads_as_none(tmp_path):
    assert reg.read_registry(str(tmp_path / "nope.json")) is None


def test_previous_ports_from_registry(tmp_path):
    registry = {"profiles": {"a": {"port": 18790}, "b": {"port": None}, "c": "junk"}}
    assert reg.previous_ports(registry) == {"a": 18790}
    assert reg.previous_ports(None) == {}


# --- reader side (the voice bridges) ---------------------------------------


def test_bridge_resolves_a_running_profile_from_the_file(tmp_path):
    path = str(tmp_path / "gateways.json")
    reg.write_registry(path, {
        "version": 1,
        "default": {"gateway_url": "http://localhost:18789"},
        "profiles": {"concierge": {"status": "ok",
                                      "gateway_url": "http://127.0.0.1:18790"}},
    })
    assert reg.gateway_url_for_profile("concierge", registry_path=path, env={}) \
        == "http://127.0.0.1:18790"
    assert reg.gateway_url_for_profile("default", registry_path=path, env={}) \
        == "http://localhost:18789"


@pytest.mark.parametrize("status", ["starting", "invalid", "conflict", "incomplete"])
def test_bridge_refuses_a_profile_that_is_not_ok(tmp_path, status):
    """Routing a call into the wrong Agent is worse than refusing it."""
    path = str(tmp_path / "gateways.json")
    reg.write_registry(path, {
        "version": 1,
        "default": {"gateway_url": "http://localhost:18789"},
        "profiles": {"concierge": {"status": status,
                                      "gateway_url": "http://127.0.0.1:18790"}},
    })
    assert reg.gateway_url_for_profile("concierge", registry_path=path, env={}) is None


def test_bridge_falls_back_to_the_legacy_env_var_when_no_registry(tmp_path):
    env = {"HERMES_PROFILE_GATEWAY_URLS": "concierge=http://localhost:18790/",
           "HERMES_GATEWAY_URL": "http://localhost:18789"}
    missing = str(tmp_path / "nope.json")
    assert reg.gateway_url_for_profile("concierge", registry_path=missing, env=env) \
        == "http://localhost:18790"
    assert reg.gateway_url_for_profile("default", registry_path=missing, env=env) \
        == "http://localhost:18789"
    assert reg.gateway_url_for_profile("ghost", registry_path=missing, env=env) is None


def test_bridge_default_url_survives_an_empty_environment(tmp_path):
    assert reg.gateway_url_for_profile("default", registry_path=str(tmp_path / "x"),
                                       env={}) == "http://localhost:18789"


# --- CLI (what hermes-supervisor.sh actually parses) -----------------------


def _run_cli(tmp_path, root, running="", extra=None):
    import io
    import contextlib
    registry_path = str(tmp_path / "gw" / "gateways.json")
    argv = ["--profiles-dir", str(root), "--registry", registry_path,
            "--default-url", "http://localhost:18789",
            "reconcile", "--running", running]
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = reg.main(argv + (extra or []))
    return rc, out.getvalue(), err.getvalue(), registry_path


def test_cli_emits_start_lines_the_supervisor_can_parse(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "newagent")
    rc, out, err, registry_path = _run_cli(tmp_path, root)
    assert rc == 0
    assert out.strip().split("\t") == ["START", "newagent", "18790", "127.0.0.1",
                                       "PORT+HOST+ENABLED+KEY"]
    assert reg.read_registry(registry_path) is not None


def test_cli_does_not_restart_something_already_running(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "newagent")
    rc, out, err, _ = _run_cli(tmp_path, root, running="newagent:4242")
    assert rc == 0
    assert out.strip() == ""


def test_cli_emits_stop_for_a_deleted_profile(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    rc, out, err, _ = _run_cli(tmp_path, root, running="ghost:99")
    assert rc == 0
    assert out.strip().split("\t")[:2] == ["STOP", "ghost"]


def test_cli_emits_stop_when_a_running_profile_goes_bad(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "rotten", config="a: [\n")
    rc, out, err, _ = _run_cli(tmp_path, root, running="rotten:99")
    assert rc == 0
    assert out.strip().startswith("STOP\trotten\tinvalid")


def test_cli_reports_broken_profiles_on_stderr(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "broken", config="a: [\n")
    os.makedirs(str(root / "half"))
    make_profile(str(root), "good")
    rc, out, err, _ = _run_cli(tmp_path, root)
    assert rc == 0
    assert "broken invalid" in err
    assert "half incomplete" in err
    # The good one still gets a START line.
    assert "START\tgood" in out


def test_cli_survives_a_missing_profiles_dir(tmp_path):
    rc, out, err, registry_path = _run_cli(tmp_path, tmp_path / "nope")
    assert rc == 0
    assert out.strip() == ""
    assert reg.read_registry(registry_path)["profiles"] == {}


def test_cli_still_starts_gateways_when_the_registry_cannot_be_written(tmp_path,
                                                                      monkeypatch):
    """A registry we cannot write is a visibility failure, not a stop-everything."""
    root = tmp_path / "profiles"
    root.mkdir()
    make_profile(str(root), "good")
    monkeypatch.setattr(reg, "write_registry",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only fs")))
    rc, out, err, _ = _run_cli(tmp_path, root)
    assert rc == 0
    assert "START\tgood" in out
    assert "cannot write" in err


def test_parse_running_tolerates_junk():
    assert reg._parse_running("a:1,b:2") == {"a": 1, "b": 2}
    assert reg._parse_running("") == {}
    assert reg._parse_running("a:notapid") == {"a": None}
    assert reg._parse_running(",,:5,") == {}


# --- a bridge can find the registry with no extra configuration (F4) -------
#
# The writer sees the directory at ${HERMES_HOME}/voice-config; a containerised
# bridge mounts the SAME directory at /app/voice-config and is given VOICE_CONFIG_DIR. A
# single hardcoded path is therefore wrong for one of them, and getting it wrong on
# the reader side means falling back to HERMES_PROFILE_GATEWAY_URLS - the env var
# this whole change exists to remove.


def test_a_bridge_finds_the_registry_from_voice_config_dir():
    env = {"VOICE_CONFIG_DIR": "/app/voice-config"}
    assert reg.default_registry_path(env) == "/app/voice-config/gateways/gateways.json"


def test_the_writer_falls_back_to_its_own_path():
    """hermes-agent sets neither variable; it must keep using its own default path."""
    assert reg.default_registry_path({}) == reg.DEFAULT_REGISTRY_PATH


def test_an_explicit_registry_path_always_wins():
    env = {"HERMES_GATEWAY_REGISTRY": "/somewhere/else.json",
           "VOICE_CONFIG_DIR": "/app/voice-config"}
    assert reg.default_registry_path(env) == "/somewhere/else.json"


def test_a_bridge_with_only_voice_config_dir_resolves_a_profile(tmp_path):
    """End to end: the environment the phone bridge actually has, and no other."""
    voice_config = tmp_path / "voice-config"
    registry_path = voice_config / "gateways" / "gateways.json"
    reg.write_registry(str(registry_path), {
        "version": 1,
        "default": {"gateway_url": "http://localhost:18789"},
        "profiles": {"concierge": {"status": "ok",
                                      "gateway_url": "http://127.0.0.1:18790"}},
    })
    # Exactly what the phone bridge is given: VOICE_CONFIG_DIR, and no
    # HERMES_GATEWAY_REGISTRY. The legacy variable is present and must NOT be used.
    env = {"VOICE_CONFIG_DIR": str(voice_config),
           "HERMES_PROFILE_GATEWAY_URLS": "concierge=http://localhost:19999"}
    assert reg.gateway_url_for_profile("concierge", env=env) == "http://127.0.0.1:18790"


# --- doctor: did discovery actually come up after the deploy? --------------


def _doctor(tmp_path, registry, expect=(), now=1000):
    import io
    import contextlib
    path = str(tmp_path / "gateways.json")
    if registry is not None:
        reg.write_registry(path, registry)
    args = argparse.Namespace(registry=path, expect=list(expect))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = reg.cmd_doctor(args, now=now)
    return rc, out.getvalue()


def _healthy_registry(now=1000, status="ok", running=True):
    return {
        "version": reg.REGISTRY_VERSION,
        "generated_at_unix": now,
        "default": {"profile": "default", "status": "ok",
                    "gateway_url": "http://localhost:18789"},
        "profiles": {"concierge": {"status": status, "running": running,
                                      "gateway_url": "http://127.0.0.1:18790",
                                      "port": 18790, "error": None}},
    }


def test_doctor_passes_on_a_healthy_registry(tmp_path):
    rc, out = _doctor(tmp_path, _healthy_registry(), expect=["concierge"])
    assert rc == 0
    assert "FAIL" not in out


def test_doctor_fails_loudly_when_there_is_no_registry_at_all(tmp_path):
    """The old-supervisor symptom: nothing ever writes this file."""
    rc, out = _doctor(tmp_path, None, expect=["concierge"])
    assert rc == 1
    assert "FAIL" in out
    assert "not hermes-supervisor.sh" in out
    assert "Install hermes/supervisor/" in out


def test_doctor_fails_when_discovery_has_stopped_refreshing(tmp_path):
    stale = _healthy_registry(now=1000)
    rc, out = _doctor(tmp_path, stale, now=1000 + reg.STALE_AFTER_SECONDS + 1)
    assert rc == 1
    assert "FAIL  discovery is running" in out


def test_doctor_fails_when_an_expected_profile_never_started(tmp_path):
    rc, out = _doctor(tmp_path, _healthy_registry(status="starting", running=False),
                      expect=["concierge"])
    assert rc == 1
    assert "FAIL  profile concierge" in out


def test_doctor_fails_when_an_expected_profile_is_absent(tmp_path):
    registry = _healthy_registry()
    registry["profiles"] = {}
    rc, out = _doctor(tmp_path, registry, expect=["concierge"])
    assert rc == 1
    assert "not in the registry at all" in out


def test_doctor_surfaces_broken_profiles_even_when_it_passes(tmp_path):
    registry = _healthy_registry()
    registry["profiles"]["My Agent"] = {"status": "ignored", "running": False,
                                        "error": "name does not match"}
    rc, out = _doctor(tmp_path, registry, expect=["concierge"])
    assert rc == 0
    assert "note  profile My Agent is ignored" in out


def test_oversized_config_yaml_is_refused(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    prof = root / "too_big"
    prof.mkdir()
    big_yaml = prof / "config.yaml"
    big_yaml.write_bytes(b"a: 1\n" * 250000)  # ~1.5 MB > MAX_CONFIG_SIZE
    rec = reg.discover(str(root))[0]
    assert rec.status == reg.INVALID
    assert "exceeds maximum size limit" in rec.error
    assert "max 1048576 bytes" in rec.error


def test_unreadable_config_yaml_reports_unreadable_not_invalid_yaml(tmp_path):
    prof = tmp_path / "unreadable"
    prof.mkdir()
    cfg = prof / "config.yaml"
    cfg.write_text("model: test\n")
    cfg.chmod(0)
    data, err = reg.load_config_yaml(str(cfg))
    if err is None or "Permission denied" not in err:
        def mock_open(*args, **kwargs):
            raise PermissionError("[Errno 13] Permission denied: '%s'" % str(cfg))
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("builtins.open", mock_open)
            data, err = reg.load_config_yaml(str(cfg))

    assert data is None
    assert err is not None
    assert err.startswith("config.yaml unreadable:")
    assert "not valid YAML" not in err


