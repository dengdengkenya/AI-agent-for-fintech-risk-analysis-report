"""Strict JSON parsing and canonical hashing shared by every layer."""

import hashlib
import json


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(_value):
    raise ValueError("Nonfinite JSON number")


def read_json(text):
    """Parse JSON that has unique keys and only finite numbers."""
    return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def canonical(value):
    """Deterministic compact JSON used for hashes and duplicate detection."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_json(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
