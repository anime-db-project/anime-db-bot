from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from localization.catalog import aliases


# Match terminal legal forms only. Studio, Animation, Pictures, Films, etc.
# remain meaningful parts of a name: subsidiaries must not be merged blindly.
_LEGAL_SUFFIX = re.compile(
    r"(?:[ ,]+(?:co(?:mpany)?|ltd|limited|inc(?:orporated)?|corp(?:oration)?|llc|llp|plc|gmbh|ag|sarl|sas|pty|bv|s a|s r l))+$"
)


def _label(value: str) -> str:
    return " ".join((value or "").split()).strip()


def _base_key(value: str) -> str:
    key = unicodedata.normalize("NFKC", _label(value)).casefold()
    # Periods in abbreviations are not word breaks. Preserve all scripts,
    # diacritics and meaningful punctuation rather than dropping non-Latin text.
    key = key.replace(".", "")
    key = re.sub(r"[\s,]+", " ", key).strip()
    shortened = _LEGAL_SUFFIX.sub("", key).strip()
    return shortened or key


@lru_cache(maxsize=1)
def _company_aliases() -> dict[str, str]:
    return {
        _base_key(alias): canonical for alias, canonical in aliases("companies").items()
    }


def company_identity_key(value: str) -> str:
    key = _base_key(value)
    return _base_key(_company_aliases().get(key, key))


def dedupe_company_names(values: list[str]) -> list[str]:
    """Unify catalog aliases and legal forms, without fuzzy company matching."""
    names: dict[str, str] = {}
    for value in values:
        label = _label(value)
        key = company_identity_key(label)
        if not label or not key:
            continue
        canonical = _company_aliases().get(_base_key(label))
        if canonical:
            names[key] = canonical
        elif key not in names:
            names[key] = label
        elif _base_key(names[key]) == _base_key(label):
            # Prefer a supplied short brand name over its registered variant.
            # Never invent a shortened display name for an unknown company.
            folded = unicodedata.normalize("NFKC", label).casefold()
            previous = unicodedata.normalize("NFKC", names[key]).casefold()
            if folded == key and previous != key:
                names[key] = label
    return list(names.values())
