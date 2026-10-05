from __future__ import annotations

import os
import subprocess
import sys
import textwrap


def test_stale_partfile_from_dead_pid_swept_and_not_served(tmp_path):
    cache = tmp_path / "cache"
    (cache / "tmp").mkdir(parents=True)
    (cache / "artifacts" / "ab").mkdir(parents=True)
    key = "a" * 64
    leftover = cache / "tmp" / f".{key}.part-2147483000-xyz"
    leftover.write_bytes(b"partial-garbage")

    script = textwrap.dedent(
        f"""
        from pathlib import Path
        from pkgmirror.artifact_cache import ArtifactCache
        cache = ArtifactCache({str(cache)!r})
        assert not cache.has_entry({key!r})
        tmp_files = list(Path({str(cache / 'tmp')!r}).iterdir())
        assert tmp_files == [], tmp_files
        print("swept-ok")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "swept-ok" in result.stdout


def test_verified_entry_survives_restart(client_factory, upstream, artifact_content):
    client, service, _app = client_factory()
    page = client.get("/simple/demo/")
    href = _artifact_href(page.text)
    first = client.get(href)
    assert first.status_code == 200
    assert first.headers["x-mirror-source"] == "upstream"

    cache_dir = str(service.artifact_cache._dir.parent)

    # "restart": brand new service instance over the same cache directory,
    # upstream turned unreachable — cached artifact must still serve.
    upstream.state["reachable"] = False
    client2, service2, _app2 = client_factory()
    assert str(service2.artifact_cache._dir.parent) == cache_dir
    second = client2.get(href)
    assert second.status_code == 200
    assert second.headers["x-mirror-source"] == "cache"
    assert second.content == artifact_content


def test_live_pid_partfile_is_not_swept(tmp_path):
    cache = tmp_path / "cache"
    from pkgmirror.artifact_cache import ArtifactCache

    first = ArtifactCache(cache)
    live = cache / "tmp" / f".x.part-{os.getpid()}-keep"
    live.write_bytes(b"x")
    second = ArtifactCache(cache)
    assert second.sweep_tmp() == 0
    assert live.exists()


def _artifact_href(page_html: str) -> str:
    start = page_html.index('href="') + len('href="')
    end = page_html.index('"', start)
    return page_html[start:end]
