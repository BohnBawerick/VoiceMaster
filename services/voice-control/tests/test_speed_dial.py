"""Ticket 09: speed dial. Owner number by default; saved numbers persist to a
real file. Tests read the file back — a fixture that agrees with the writer
is not enough.
"""
import yaml
from fastapi.testclient import TestClient

from conftest import SentinelTransport
import speed_dial

OWNER = "+61491570156"
DENTIST = "+61390000000"


def _wire(monkeypatch, tmp_path, owner=OWNER):
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "agents").mkdir(exist_ok=True)
    if owner:
        monkeypatch.setenv("VOICE_OWNER_NUMBER", owner)
    else:
        monkeypatch.delenv("VOICE_OWNER_NUMBER", raising=False)
        monkeypatch.delenv("VOICE_INBOUND_ALLOWED_CALLERS", raising=False)


def test_speed_dial_includes_the_owner_when_the_file_is_absent(
        make_app, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    client = TestClient(make_app(SentinelTransport()))
    r = client.get("/api/speed-dial")
    assert r.status_code == 200
    body = r.json()
    assert body["owner"] == OWNER
    assert body["numbers"][0] == {"label": "Me", "number": OWNER, "owner": True}
    # An absent file is not created by a read.
    assert not (tmp_path / "speed-dial.yaml").exists()


def test_speed_dial_falls_back_to_the_inbound_allowed_caller(
        make_app, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, owner=None)
    monkeypatch.setenv("VOICE_INBOUND_ALLOWED_CALLERS", f"{OWNER},+61000000000")
    client = TestClient(make_app(SentinelTransport()))
    assert client.get("/api/speed-dial").json()["owner"] == OWNER


def test_speed_dial_save_round_trips_through_the_real_file(
        make_app, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    client = TestClient(make_app(SentinelTransport()))
    r = client.put("/api/speed-dial", json={
        "numbers": [{"label": "Dentist", "number": "+61 3900 000 00"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [n["number"] for n in body["numbers"]] == [OWNER, DENTIST]
    assert body["numbers"][1]["label"] == "Dentist"
    assert body["numbers"][1]["owner"] is False

    on_disk = yaml.safe_load((tmp_path / "speed-dial.yaml").read_text())
    assert on_disk["numbers"] == [{"label": "Dentist", "number": DENTIST}]
    # The owner is not stored — it is derived from the env on every read.
    assert all(e.get("owner") is not True for e in on_disk["numbers"])

    again = client.get("/api/speed-dial").json()
    assert again == body


def test_speed_dial_cannot_drop_the_owner(make_app, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    client = TestClient(make_app(SentinelTransport()))
    r = client.put("/api/speed-dial", json={"numbers": []})
    assert r.status_code == 200
    assert r.json()["numbers"] == [{"label": "Me", "number": OWNER, "owner": True}]


def test_speed_dial_rejects_a_non_number(make_app, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    client = TestClient(make_app(SentinelTransport()))
    r = client.put("/api/speed-dial", json={"numbers": [{"number": "not-a-number"}]})
    assert r.status_code == 422
    assert not (tmp_path / "speed-dial.yaml").exists()


def test_owner_number_prefers_voice_owner_number(monkeypatch):
    monkeypatch.setenv("VOICE_OWNER_NUMBER", "+61400000001")
    monkeypatch.setenv("VOICE_INBOUND_ALLOWED_CALLERS", "+61400000002")
    assert speed_dial.owner_number() == "+61400000001"


def test_owner_flags_cannot_relabel_someone_elses_number(
        make_app, monkeypatch, tmp_path):
    """Both owner guards, independently. Removing either leaves the suite
    green on the other tests: the write guard stops a client persisting a
    'Me' row aimed at someone else, and the read guard stops a poisoned
    file from showing that row. Number-dedup only covers the owner's own
    number, so a foreign number labelled owner would otherwise land."""
    _wire(monkeypatch, tmp_path)
    client = TestClient(make_app(SentinelTransport()))

    r = client.put("/api/speed-dial", json={
        "numbers": [{"label": "Me", "number": DENTIST, "owner": True}]})
    assert r.status_code == 200, r.text
    assert r.json()["numbers"] == [{"label": "Me", "number": OWNER, "owner": True}]
    on_disk = yaml.safe_load((tmp_path / "speed-dial.yaml").read_text())
    assert on_disk["numbers"] == []

    (tmp_path / "speed-dial.yaml").write_text(yaml.safe_dump({
        "numbers": [{"label": "Me", "number": DENTIST, "owner": True}]}))
    again = client.get("/api/speed-dial").json()
    assert again["numbers"] == [{"label": "Me", "number": OWNER, "owner": True}]
    assert DENTIST not in yaml.safe_dump(again)
