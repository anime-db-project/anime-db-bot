from __future__ import annotations

import re
import unicodedata


def dedupe_company_names(values: list[str]) -> list[str]:
    """Deduplicate company names and common legal-name suffixes."""
    names: list[str] = []
    seen: set[str] = set()
    suffix = re.compile(
        r"(?:[\s,.]+(?:llc|ltd|inc|limited|corp|corporation|co))+[\s,.]*$",
        re.IGNORECASE,
    )
    for value in values:
        label = " ".join((value or "").split()).strip()
        if not label:
            continue
        key = unicodedata.normalize("NFKC", label).casefold()
        key = suffix.sub("", key).strip(" ,.")
        key = re.sub(r"[\s.,]+", " ", key).strip()
        if key and key not in seen:
            seen.add(key)
            names.append(label)
    return names
