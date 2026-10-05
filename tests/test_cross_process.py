from __future__ import annotations

import asyncio
import contextlib
import hashlib
import subprocess
import sys
import textwrap


def test_two_processes_share_one_download(tmp_path, upstream, artifact_content):
    """Two processes requesting the same artifact produce one upstream fetch.

    The mock upstream serializes writes with a delay; the second process
    blocks on the per-key flock and finds the entry completed.
    """
    upstream.state["delay_seconds"] = 1.0
    digest = hashlib.sha256(artifact_content).hexdigest()
    url = (
        f"{upstream.url}/files/demo-1.0-py3-none-any.whl#sha256={digest}"
    )
    script_template = textwrap.dedent(
        """
        import asyncio
        from pkgmirror.config import Config
        from pkgmirror.service import MirrorService

        async def main():
            cfg = Config(
                upstream_url=%(base)r,
                request_timeout=10.0,
                retry_attempts=0,
                index_ttl_seconds=300.0,
                negative_ttl_seconds=60.0,
                max_cache_bytes=0,
                artifact_verify_max_attempts=3,
                serve_stale_on_failure=True,
            )
            svc = MirrorService.build(cfg, %(cache)r)
            result = await svc.get_artifact(%(url)r)
            print(result.source)
            await svc.aclose()

        asyncio.run(main())
        """
    )
    values = {"base": upstream.url, "cache": str(tmp_path / "cache"), "url": url}

    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script_template % values],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    outputs = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=30)
        assert proc.returncode == 0, stderr
        outputs.append(stdout.strip())

    assert sorted(outputs) == ["cache", "upstream"]
    assert upstream.hits("/files/demo-1.0-py3-none-any.whl") == 1
