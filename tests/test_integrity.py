"""Integrity verification: mismatch quarantined, limited retries, distinct errors."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .conftest import artifact_url


def test_digest_mismatch_rejected_quarantined_and_retried(mirror, upstream, http, tmp_path):
    upstream.add_package("demo", "demo-1.0.tar.gz", b"TAMPERED-BYTES")
    upstream.declare_digest("demo", "demo-1.0.tar.gz", hashlib.sha256(b"GOOD").hexdigest())
    url = artifact_url(mirror.url, upstream, "demo", "demo-1.0.tar.gz")

    resp = http.get(url)
    assert resp.status_code == 502
    assert resp.headers["X-Mirror-Error"] == "integrity-failed"
    assert "sha256 mismatch" in resp.headers["X-Mirror-Reason"]

    # verify_retries=1 -> exactly 2 upstream attempts, then give up
    assert upstream.hits("artifact", "demo/demo-1.0.tar.gz") == 2

    cache_dir = Path(mirror.settings.cache_dir)
    # failed artifact must NOT enter the servable cache
    assert not (cache_dir / "artifacts" / "demo" / "demo-1.0.tar.gz").exists()
    # ...but must be isolated in quarantine with a reason
    quarantined = list((cache_dir / "quarantine").glob("*demo-1.0.tar.gz*"))
    assert quarantined
    assert any(p.name.endswith(".reason") for p in quarantined)


def test_integrity_failure_not_cached_forever(mirror, upstream, http):
    upstream.add_package("demo", "demo-1.0.tar.gz", b"V1-BAD")
    upstream.declare_digest("demo", "demo-1.0.tar.gz", hashlib.sha256(b"GOOD").hexdigest())
    url = artifact_url(mirror.url, upstream, "demo", "demo-1.0.tar.gz")
    assert http.get(url).status_code == 502

    # upstream fixed: content now matches the declared digest
    upstream.add_package("demo", "demo-1.0.tar.gz", b"GOOD")
    resp = http.get(url)
    assert resp.status_code == 200
    assert resp.content == b"GOOD"
    assert resp.headers["X-Mirror-Source"] == "upstream"


def test_integrity_error_distinguishable_from_upstream_down(mirror, upstream, http):
    upstream.add_package("demo", "demo-1.0.tar.gz", b"BYTES")
    upstream.declare_digest("demo", "demo-1.0.tar.gz", hashlib.sha256(b"OTHER").hexdigest())
    url = artifact_url(mirror.url, upstream, "demo", "demo-1.0.tar.gz")
    integrity_resp = http.get(url)
    assert integrity_resp.status_code == 502
    assert integrity_resp.headers["X-Mirror-Error"] == "integrity-failed"

    upstream.online = False
    upstream.add_package("demo", "demo-2.0.tar.gz", b"V2")
    down_resp = http.get(artifact_url(mirror.url, upstream, "demo", "demo-2.0.tar.gz"))
    assert down_resp.status_code == 503
    assert down_resp.headers["X-Mirror-Error"] == "upstream-unavailable"
    assert integrity_resp.headers["X-Mirror-Error"] != down_resp.headers["X-Mirror-Error"]
