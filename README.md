# Package Index Mirror

A local caching proxy that speaks the [PEP 503](https://peps.python.org/pep-0503/)
"simple repository" HTTP API. It proxies package indexes and artifact downloads
from an upstream repository, caches them on local disk, and keeps installs
reproducible and verifiable in offline / restricted-network environments.

The acceptance tests use a loopback mock upstream (a `http.server` bound to
`127.0.0.1`); no external network is required.

## Running

```bash
uv sync
PIP_INDEX_URL=http://127.0.0.1:8000/simple \
MIRROR_CACHE_DIR=./.mirror-cache \
uv run uvicorn main:app --host 127.0.0.1 --port 9000
```

Point pip at the mirror:

```bash
pip install --index-url http://127.0.0.1:9000/simple/ demo
```

Endpoints:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/simple/` | root package index |
| GET | `/simple/<package>/` | per-package artifact index (links rewritten to the proxy) |
| GET | `/artifacts/?url=<encoded-upstream-url>` | cached/proxied artifact download |
| GET | `/admin/config` | inspect current configuration snapshot |
| PUT | `/admin/config` | update configuration fields |
| POST | `/admin/evict?max_bytes=N` | force LRU eviction down to `N` bytes |
| GET | `/healthz` | liveness |

Index pages returned by the mirror rewrite every anchor `href` so package
links resolve against the mirror. Artifact links keep the upstream
`#sha256=...` (or `#sha512=...`) fragment inside the encoded `url=` query
parameter; this is used for integrity verification and never sent upstream.

## Configuration

Configuration is read from environment variables at startup and can be
changed at runtime through `PUT /admin/config`.

| Env variable | Config field | Default | Meaning |
| --- | --- | --- | --- |
| `PIP_INDEX_URL` | `upstream_url` | `https://pypi.org/simple` | upstream index root URL |
| `MIRROR_TIMEOUT` | `request_timeout` | `10` | per-request timeout (seconds) |
| `MIRROR_RETRIES` | `retry_attempts` | `2` | transport/5xx retries for index fetches |
| `MIRROR_INDEX_TTL` | `index_ttl_seconds` | `300` | positive index cache lifetime |
| `MIRROR_NEGATIVE_TTL` | `negative_ttl_seconds` | `60` | negative (404) cache lifetime |
| `MIRROR_MAX_CACHE_BYTES` | `max_cache_bytes` | `1073741824` | artifact cache ceiling; `0` = unlimited |
| `MIRROR_VERIFY_ATTEMPTS` | `artifact_verify_max_attempts` | `3` | bounded download/verify retries per artifact |
| `MIRROR_SERVE_STALE` | `serve_stale_on_failure` | `1` | serve stale cache when upstream is unreachable |
| `MIRROR_CACHE_DIR` | — | `.mirror-cache` | on-disk cache directory |

### In-flight request semantics

Configuration is stored as an immutable snapshot. A request captures the
snapshot when it starts and keeps using it (timeout, retries, TTL, upstream
URL) until it finishes. Only requests started after a `PUT /admin/config`
observe the new values. An in-flight download is therefore never interrupted
or re-parameterised by a configuration change.

## Cache-consistency semantics

### Concurrent request de-duplication

* In one process, a `SingleFlight` map coalesces concurrent requests for the
  same artifact (and same index page): exactly one coroutine performs the
  upstream fetch; waiters share its result/exception.
* Across processes sharing a cache directory, a per-key `flock`
  (`locks/<key>.lock`) serialises downloads. The second process blocks, then
  re-checks and serves the completed entry. Either way, an artifact is
  downloaded at most once, and cache-hit vs. direct-fetch bytes are identical
  (the same verified file is served).

### Atomic writes and crash recovery

* Downloads stream to `tmp/.<key>.part-<pid>-*`, are `fsync`-ed, verified,
  then published with a single atomic `os.replace` (plus a directory fsync).
  Readers can therefore never observe a partially written artifact.
* On startup the cache sweeps `tmp/`, deleting only part files whose owning
  PID is no longer alive. A crashed download never leaves an entry that could
  be served as valid; verified entries under `artifacts/` survive restarts.

### Integrity verification and quarantine

* Artifacts are verified against the upstream-declared hash
  (`#sha512=` / `#sha256=` / `#md5=`) while streaming, before publication.
* A checksum mismatch never enters the servable cache. The offending bytes are
  moved to `quarantine/<key>-<timestamp>` and the download is retried, bounded
  by `artifact_verify_max_attempts`; after that the request fails with
  `502` and header `X-Mirror-Error: checksum_mismatch` (body includes expected
  and actual digests and the attempt count).

### Distinguishable failure modes

| Situation | Status | `X-Mirror-Error` |
| --- | --- | --- |
| upstream unreachable / 5xx exhausted | `502` | `upstream_unreachable` |
| artifact checksum mismatch | `502` | `checksum_mismatch` |
| artifact 404 upstream | `404` | `artifact_not_found` |

Artifact responses carry `X-Mirror-Source: upstream|cache`.

### Index freshness and negative caching

* Positive index pages are cached for `index_ttl_seconds`, 404 pages as
  negative entries for `negative_ttl_seconds`.
* Responses are annotated with `X-Mirror-Freshness: fresh|stale|negative`,
  `X-Mirror-Age: <seconds>`, and `X-Mirror-Negative: 1` for negative entries.
* When an entry is stale and the upstream is unreachable, the mirror falls
  back to the last cached copy (including stale negative entries) when
  `serve_stale_on_failure` is enabled, explicitly marked `stale`. With no
  cached copy (or stale fallback disabled), it returns
  `502 / upstream_unreachable`, which is distinct from checksum failure.

### Capacity limit and safe eviction

* The artifact cache enforces `max_cache_bytes` using LRU by access time.
* Eviction acquires each candidate's per-key lock exclusively and
  non-blocking. An entry currently being downloaded (exclusive lock) or read
  (shared lock held for the whole response stream) is skipped, so eviction
  never deletes an in-use artifact or breaks an in-progress install. On POSIX
  an already-open file also remains readable even if unlinked.

## Project layout

```
pkgmirror/
  config.py          immutable Config snapshot + ConfigStore
  models.py          freshness/error enums
  singleflight.py    in-process coroutine de-duplication
  locks.py           flock helpers
  artifact_cache.py  atomic download, verify, quarantine, eviction
  index_cache.py     TTL positive/negative index cache
  upstream.py        httpx client with retries and typed errors
  pep503.py          fragment-hash parsing and index link rewriting
  service.py         orchestration
  app.py             FastAPI routes / response annotations
tests/               loopback mock upstream + scenario tests
```

## Tests

```bash
uv run pytest
```

The suite spins up a `127.0.0.1` mock upstream and covers:

* concurrent de-duplication (20 concurrent requests → 1 upstream fetch);
* checksum failure isolation, bounded retries and distinct error header;
* mid-download crash leftovers swept on restart; verified entry reuse after a
  restart with the upstream down; live-PID part files preserved;
* upstream unreachable, stale fallback with freshness headers, and the
  `upstream_unreachable` vs. `checksum_mismatch` distinction;
* negative caching and expiry/revalidation;
* cache ceiling / LRU eviction and read/download lock protection;
* cross-process single download via `flock`;
* configuration updates and in-flight snapshot semantics.
