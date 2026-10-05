"""Artifact integrity verification against upstream-declared hashes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Digest:
    """A declared digest: hash algorithm name and expected hex value."""

    algorithm: str
    hexdigest: str


def parse_fragment_digest(fragment: str) -> Digest | None:
    """Parse a PEP 503 URL fragment like ``sha256=abc...`` into a Digest."""
    fragment = fragment.lstrip("#")
    if "=" not in fragment:
        return None
    algorithm, _, hexdigest = fragment.partition("=")
    algorithm = algorithm.strip().lower()
    hexdigest = hexdigest.strip().lower()
    if not algorithm or not hexdigest or algorithm not in hashlib.algorithms_available:
        return None
    return Digest(algorithm=algorithm, hexdigest=hexdigest)


def verify_bytes(data: bytes, digest: Digest) -> bool:
    """Return True iff ``data`` matches ``digest``."""
    try:
        actual = hashlib.new(digest.algorithm, data).hexdigest()
    except ValueError:
        return False
    return actual.lower() == digest.hexdigest.lower()
