"""E.164 canonicalization for a typed or saved number.

Shared by the Place-a-call API and speed dial so a number the owner types,
picks from the list, or stores is the same number the bridge is asked to
dial. Formatting (spaces, dashes, a leading 00) cannot invent a different
destination.
"""
import re

from . import profiles

_FORMATTING = re.compile(r"[\s\-\.\(\)]")


def normalize_e164(raw: str):
    """Canonical E.164, or None when the input is not a number.

    Strips spaces/dashes/dots/parens and folds a leading international ``00``
    to ``+``. Returns None when the result is not ``profiles._E164_RE`` — the
    same rule the old fire path used, so a number that used to dial still
    does and a non-number still does not.
    """
    if not isinstance(raw, str):
        return None
    text = _FORMATTING.sub("", raw.strip())
    if text.startswith("00"):
        text = "+" + text[2:]
    return text if profiles._E164_RE.match(text) else None
