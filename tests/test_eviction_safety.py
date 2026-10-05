from __future__ import annotations


def test_eviction_skips_entry_being_read(tmp_path):
    import asyncio

    from pkgmirror.artifact_cache import ArtifactCache

    async def scenario():
        cache = ArtifactCache(tmp_path)
        key = "f" * 64
        entry = cache.entry_path(key)
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_bytes(b"in-use")

        async with cache.reading(key):
            removed = await cache.evict_lru(1)
            assert removed == []
            assert entry.exists()

        removed = await cache.evict_lru(1)
        assert removed == [str(entry)]
        assert not entry.exists()

    asyncio.run(scenario())


def test_eviction_skips_entry_being_downloaded(tmp_path):
    import asyncio

    from pkgmirror.artifact_cache import _locked

    async def scenario():
        from pkgmirror.artifact_cache import ArtifactCache

        cache = ArtifactCache(tmp_path)
        key = "e" * 64
        entry = cache.entry_path(key)
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_bytes(b"downloading")

        async with _locked(str(cache.lock_path(key)), exclusive=True):
            removed = await cache.evict_lru(1)
            assert removed == []
            assert entry.exists()

    asyncio.run(scenario())
