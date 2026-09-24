"""Speed dial: saved numbers a Call can be aimed at (VC19).

The owner's own number is in the list by default. It comes from
``VOICE_OWNER_NUMBER``, or the first entry of ``VOICE_INBOUND_ALLOWED_CALLERS``
if that is unset - both already live in the service environment; neither is
read from a secrets file. If neither is set the list can still hold numbers the owner
saved, it just has no default row.

The file is ``$VOICE_CONFIG_DIR/speed-dial.yaml``. An absent file is an empty
saved list plus the owner row, not an error.
"""
import os
from pathlib import Path

import yaml

from voicecore import profiles
from voicecore.e164 import normalize_e164

FILENAME = "speed-dial.yaml"
OWNER_LABEL = "Me"


def owner_number(env=None) -> "str | None":
    env = os.environ if env is None else env
    raw = (env.get("VOICE_OWNER_NUMBER") or "").strip()
    if not raw:
        inbound = env.get("VOICE_INBOUND_ALLOWED_CALLERS") or ""
        raw = inbound.split(",")[0].strip() if inbound else ""
    if not raw:
        return None
    return normalize_e164(raw) or raw


def _path(env=None) -> Path:
    return profiles.config_dir(env) / FILENAME


def _atomic_write(path: Path, doc: dict) -> None:
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _read_saved(env=None) -> list:
    path = _path(env)
    try:
        if not path.is_file():
            return []
        doc = yaml.safe_load(path.read_text()) or {}
    except Exception:  # noqa: BLE001 — a corrupt file is an empty saved list
        return []
    if not isinstance(doc, dict):
        return []
    raw = doc.get("numbers")
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        number = item.get("number")
        if not isinstance(number, str) or not number.strip():
            continue
        canonical = normalize_e164(number) or number.strip()
        label = item.get("label")
        label = label.strip() if isinstance(label, str) and label.strip() else canonical
        if item.get("owner") is True:
            continue
        out.append({"label": label, "number": canonical, "owner": False})
    return out


def load(env=None) -> dict:
    """The list the screen shows: owner first (when known), then saved numbers."""
    owner = owner_number(env)
    saved = _read_saved(env)
    numbers = []
    if owner:
        numbers.append({"label": OWNER_LABEL, "number": owner, "owner": True})
    seen = {owner} if owner else set()
    for entry in saved:
        if entry["number"] in seen:
            continue
        seen.add(entry["number"])
        numbers.append(entry)
    return {"owner": owner, "numbers": numbers}


def save(entries, env=None) -> dict:
    """Replace the saved (non-owner) list. The owner row is never stored;
    it is always derived from the environment so rotating the number does
    not leave a stale 'Me' in the file."""
    cleaned = []
    seen = set()
    owner = owner_number(env)
    if owner:
        seen.add(owner)
    if not isinstance(entries, list):
        raise ValueError("numbers: must be a list")
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("each speed-dial entry must be an object")
        number = item.get("number")
        if not isinstance(number, str) or not number.strip():
            raise ValueError("number: required")
        canonical = normalize_e164(number)
        if canonical is None:
            raise ValueError(f"number: {number!r} is not a valid E.164 number")
        if canonical in seen:
            continue
        if item.get("owner") is True:
            continue
        label = item.get("label")
        label = label.strip() if isinstance(label, str) and label.strip() else canonical
        seen.add(canonical)
        cleaned.append({"label": label, "number": canonical})
    _atomic_write(_path(env), {"numbers": cleaned})
    return load(env)
