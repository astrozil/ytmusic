# Agent Handoff Context (YTMusic API)

This file captures the major context and decisions from the recent multi-step refactor so future agents can continue work without re-discovery.

## Current State Summary

- App is a Flask + Waitress API (`app.py`) with modular services:
  - `settings.py` (env parsing/defaults)
  - `cache_layer.py` (cache envelope + safe cache access)
  - `clients.py` (YTMusic + HTTP wrappers with retry/timeout)
  - `services/hot_endpoints.py` (hot endpoint orchestration)
  - `services/prewarm.py` (background prewarm worker)
  - `errors.py` (centralized request/error hooks)
- Test suite currently passes:
  - `30 passed` via `.\.venv\Scripts\python -m pytest -q`

## What Was Implemented

### 1) Phase 1 reliability/performance hardening
- Refactored monolith logic into modules listed above.
- Kept API response compatibility as priority.
- Added deterministic cache keys for hot endpoints.
- Added cache envelope model (`payload`, `fetched_at`, `fresh_until`, `stale_until`).
- Added stale fallback behavior (serve stale on upstream failure when available).
- Added `X-Cache` (`hit|miss|stale`) and `X-Data-Stale: 1` headers on hot endpoints.
- Added targeted rate limits for hot endpoints.
- Added request ID + latency logging hooks and centralized error handling.

### 2) P0/P1 performance work (done after phase 1)
- Added in-process single-flight per cache key in `HotEndpointsService` to reduce thundering herd.
- Added sub-caches for expensive fanout calls:
  - watch playlist by seed song
  - artist fetches
  - playlist fetches
  - artist albums fetches
  - album fetches
- Updated logic to avoid mutating cached upstream objects in recommendation fanout.

### 3) Phase 2 prewarm/refresh
- Added `PrewarmManager` in `services/prewarm.py`.
- In-process daemon worker (non-blocking startup), disabled by default.
- Prewarms:
  - `/trending` for configured country/limit variants (default `US,50`)
  - `/billboard`
- Refresh cadence is TTL-derived:
  - trending interval: `clamp(ttl*0.5, 300, 3600)`
  - billboard interval: `clamp(ttl*0.5, 3600, 86400)`
- Failure policy: log and continue; no crash/retry storm.
- Added prewarm state metrics into `/health` under `prewarm`.
- Worker starts in `create_app()` and stops via `atexit`.

### 4) Phase 3 endpoint performance upgrades
- Implemented cached + parallelized `GET /artist/<artist_id>/songs` in `HotEndpointsService`.
  - Uses endpoint cache with stale-fallback envelope semantics.
  - Reuses artist/album subcaches and fetches album fanout concurrently.
- Implemented cached + parallelized `POST /songs` in `HotEndpointsService`.
  - Uses endpoint cache keyed by ordered `song_ids`.
  - Uses per-song subcache to avoid repeated `get_song` upstream calls.
  - Preserves request order and response compatibility.
- Added artist-search subcache for billboard artist enrichment.
  - `_resolve_artist` now resolves via subcache keyed by normalized artist name.
  - Reduces repeated `search(filter="artists")` calls during billboard fanout.
- Added Redis-based distributed single-flight for multi-instance deployments.
  - Uses Redis locks per cache key with token-safe unlock.
  - Follower instances wait for leader refresh before falling back.
  - Works in both sync and async cache wrappers.
  - Fail-open behavior: if lock ops fail, request proceeds with local single-flight.
- Added lyrics caching + negative backoff cache on `GET /lyrics`.
  - Successful lyric payloads are cached with short TTL + stale window.
  - Missing lyrics responses use negative cache with exponential backoff to avoid repeated upstream calls.
  - Backoff applies across Genius scrape + lyrics.ovh fallback path.

## Important Environment Variables

### Core
- `CACHE_BACKEND` (`redis` or `simple`)
- `REDIS_URL`
- `CACHE_FAIL_OPEN`
- `UPSTREAM_TIMEOUT_SEC`
- `UPSTREAM_RETRY_ATTEMPTS`
- `UPSTREAM_RETRY_BACKOFF_MS`
- `MAX_WORKERS_TRENDING`
- `MAX_WORKERS_RECOMMENDATIONS`
- `MAX_WORKERS_MIX`
- `MAX_WORKERS_ARTIST_SONGS`
- `MAX_WORKERS_SONGS`
- `MAX_CONCURRENCY_BILLBOARD`
- `MAX_CONCURRENCY_ARTIST_LOOKUP`
- `ENABLE_DISTRIBUTED_SINGLEFLIGHT`
- `DISTRIBUTED_SINGLEFLIGHT_LOCK_TTL_SEC`
- `DISTRIBUTED_SINGLEFLIGHT_WAIT_TIMEOUT_SEC`
- `DISTRIBUTED_SINGLEFLIGHT_POLL_MS`

### Cache TTL/stale
- `CACHE_TTL_TRENDING_SEC`
- `CACHE_TTL_RECOMMENDATIONS_SEC`
- `CACHE_TTL_MIX_SEC`
- `CACHE_TTL_ARTIST_SONGS_SEC`
- `CACHE_TTL_SONGS_SEC`
- `CACHE_TTL_BILLBOARD_SEC`
- `CACHE_TTL_LYRICS_SEC`
- `CACHE_STALE_TRENDING_SEC`
- `CACHE_STALE_RECOMMENDATIONS_SEC`
- `CACHE_STALE_MIX_SEC`
- `CACHE_STALE_ARTIST_SONGS_SEC`
- `CACHE_STALE_SONGS_SEC`
- `CACHE_STALE_BILLBOARD_SEC`
- `CACHE_STALE_LYRICS_SEC`

### Lyrics negative-cache/backoff
- `CACHE_TTL_LYRICS_NEGATIVE_BASE_SEC`
- `CACHE_TTL_LYRICS_NEGATIVE_MAX_SEC`
- `CACHE_LYRICS_NEGATIVE_BACKOFF_FACTOR`

### Sub-cache TTL/stale
- `CACHE_TTL_SUBCACHE_SEED_SEC`
- `CACHE_TTL_SUBCACHE_ARTIST_SEC`
- `CACHE_TTL_SUBCACHE_SONG_SEC`
- `CACHE_STALE_SUBCACHE_SEED_SEC`
- `CACHE_STALE_SUBCACHE_ARTIST_SEC`
- `CACHE_STALE_SUBCACHE_SONG_SEC`

### Rate limiting
- `ENABLE_RATE_LIMITS`
- `RATELIMIT_STORAGE_URI`

### Prewarm
- `ENABLE_PREWARM` (default `false`)
- `PREWARM_LOOP_TICK_SEC` (default `30`)
- `PREWARM_TRENDING_COUNTRIES` (default `US`)
- `PREWARM_TRENDING_LIMITS` (default `50`)

### Auth
- `YTMUSIC_AUTH_FILE` (default `browser.json`)

## Render Deployment Notes

- Build: `pip install -r requirements.txt`
- Start: `python app.py`
- Health path: `/health`
- Must provide Redis URL if using Redis cache/rate-limit consistency.
- `app.py` binds to `PORT` env automatically.
- Prewarm runs in-process; no Render cron required.

## Known Design Choices

- Strict API compatibility was prioritized (non-breaking behavior).
- Prewarm allows duplicate workers across multiple instances (known tradeoff).
- Single-flight is process-local by default, with Redis-distributed coordination when Redis backend is active and distributed single-flight is enabled.
- Prewarm default is disabled for safer rollout.

## Recommended Next Performance Work

1. Run load smoke against `/lyrics` and tune lyrics TTL/backoff envs for your traffic profile.

## Quick Local Commands

```powershell
# Run API
python app.py

# Run tests
.\.venv\Scripts\python -m pytest -q

# Load smoke
python scripts/load_smoke.py --base-url http://localhost:5000 --artist-ids <ids> --song-ids <ids>
```
