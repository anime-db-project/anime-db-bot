"""Conservative session-local person/character identity matching."""

from __future__ import annotations
from collections import defaultdict
from functools import lru_cache
import unicodedata
from core.canonicalization import person_identity_keys, normalize_key, JAPANESE_RE


def names_for_credit(record):
    return [record.name, *record.aliases]


def credit_name_keys(record):
    return _keys(record.name, tuple(record.aliases))


@lru_cache(maxsize=16384)
def _keys(name, aliases):
    names = [name, *aliases]
    keys = person_identity_keys(names)
    for name in names:
        if JAPANESE_RE.search(name):
            keys.add(
                "jp:" + unicodedata.normalize("NFKC", name).replace(" ", "").casefold()
            )
    # A one-word alternative first name can qualify with the surname from
    # the full name (Lutheran Barnett / Ruslan -> Ruslan Barnett).
    # Do not treat the bare alias as a full-name match in this case.
    full = normalize_key(names[0]).split()
    if len(full) == 2:
        surname = full[0] if "," in names[0] else full[1]
        for alias in aliases:
            words = normalize_key(alias).split()
            if (
                len(words) == 1
                and words[0] not in full
                and not JAPANESE_RE.search(alias)
            ):
                keys.difference_update(person_identity_keys([alias]))
                keys.update(person_identity_keys([f"{words[0]} {surname}"]))
    return frozenset(keys)


def ambiguous_name_keys(groups):
    ambiguous = set()
    for group in groups:
        counts = defaultdict(set)
        for i, record in enumerate(group):
            ident = tuple(sorted(record.external_ids.items())) or ("row", i)
            for key in credit_name_keys(record):
                counts[key].add(ident)
        ambiguous.update(key for key, ids in counts.items() if len(ids) > 1)
    return ambiguous


def shared_credit_id(left, right):
    a, b = left.external_ids, right.external_ids
    if credit_conflict(left, right):
        return False
    if any(v and b.get(k) == v for k, v in a.items()):
        return True
    for x, y in (("mal", "shikimori"), ("shikimori", "mal")):
        if (
            a.get(x)
            and a.get(x) == b.get(y)
            and credit_name_keys(left) & credit_name_keys(right)
        ):
            return True
    return False


def credit_conflict(left, right):
    a, b = left.external_ids, right.external_ids
    return any(v and b.get(k) and b[k] != v for k, v in a.items())


def matching_credit_index(merged, record, ambiguous, portrait_match=None):
    strong = [i for i, p in enumerate(merged) if shared_credit_id(p, record)]
    if strong:
        return strong[0] if len(strong) == 1 else None
    if portrait_match is not None:
        visual = [
            i
            for i, p in enumerate(merged)
            if not credit_conflict(p, record)
            and credit_name_keys(p) & credit_name_keys(record)
            and portrait_match(p, record)
        ]
        if len(visual) == 1:
            return visual[0]
    keys = credit_name_keys(record) - ambiguous
    candidates = [
        i
        for i, p in enumerate(merged)
        if not credit_conflict(p, record) and keys & credit_name_keys(p)
    ]
    return candidates[0] if len(candidates) == 1 else None
