"""Shared data types."""

from __future__ import annotations

from enum import Enum


class IndexFreshness(str, Enum):
    """Freshness annotation for served index pages."""

    FRESH = "fresh"
    STALE = "stale"
    NEGATIVE = "negative"


class MirrorErrorKind(str, Enum):
    """Machine-readable failure categories."""

    UPSTREAM_UNREACHABLE = "upstream_unreachable"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    RETRIES_EXHAUSTED = "retries_exhausted"
