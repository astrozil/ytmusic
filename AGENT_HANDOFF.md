# Agent Handoff Context (YTMusic API)

This file captures the major context and decisions from the recent multi-step refactor so future agents can continue work without re-discovery.

## 2026-10-02 Billboard Match Cache Optimization

- Successful Billboard song searches now share a per-song cache keyed by normalized
  title and artist, independently of chart week, rank, or statistics. Duplicate
  concurrent lookups use the existing single-flight protection.
- `CACHE_TTL_BILLBOARD_MATCH_SEC` defaults to 2592000 (30 days), and
  `CACHE_STALE_BILLBOARD_MATCH_SEC` defaults to 5184000 (60 days). The latter
  is an overall cache lifetime, including the fresh period. Memory entries remain
  subject to eviction and reset on process restart.
- Every chart rebuild assembles rank, lastPos, peakPos, weeks, and chart date from
  the newly fetched chart; cached matches contain only the YouTube Music result.
- Empty, invalid, or failed searches are not stored as successful song matches.
  An incomplete chart is cached fresh for at most 60 seconds (plus configured
  jitter), with at most 120 seconds of overall retention, instead of a full week.
- Stale song matches may supplement current chart rows during upstream failure;
  `/billboard` reports `X-Cache: stale` and `X-Data-Stale: 1`. These results are
  not stored as a fresh chart, so a later request can retry the match refresh.
- Validation: 126 tests pass. Anonymous live Hot 100 check matched all 100 songs;
  cold build took ~11.0s (100 song and 89 artist searches), forced chart rebuild
  took ~1.1s with zero additional YouTube Music calls, and endpoint hit took ~1ms.
  Rank, lastPos, peakPos, and weeks were identical across the two live chart fetches.

## 2026-10-02 Trending Repair

- Reproduced deployed `get_charts` failure locally with ytmusicapi 1.11.5:
  `KeyError: 'musicTwoRowItemRenderer'`.
- Pinned `ytmusicapi==1.12.3` in requirements.txt and updated the local virtual environment.
- Current chart responses contain playlist cards rather than individual songs.
  Trending now resolves their tracks through the existing playlist subcache, prefers
  the playlist with "trending" in its title, and falls back to daily/weekly/other
  video charts. Legacy direct-track responses remain supported.
- Failed or empty playlist fetches do not cache playlist cards as songs; upstream
  failures retain endpoint stale fallback behavior.
- Validation: 41 tests pass. Live Flask route checks returned HTTP 200 for
  US limit=50 (19 playable tracks, followed by a cache hit) and SG limit=10
  (10 playable tracks). The limit is a maximum; chart availability may be smaller.
- Deployment must rebuild dependencies and restart with the changed source.

## 2026-10-02 Additional Deployment Repairs

- Added `services/billboard_chart.py`, a Billboard compatibility parser that reads
  labelled LW/PEAK/WEEKS ON CHART statistics. Current desktop/mobile nested cells
  broke billboard.py's positional parser. Older positional markup remains supported;
  malformed rows raise so the existing stale fallback can serve cached data.
- Pinned `billboard.py==7.1.0`, whose fetch and ChartEntry interfaces the adapter uses.
  Chart fetching now uses configured upstream timeouts and retry count.
- Mix album/single lookups now use each artist section's browseId (e.g. MPAD...),
  falling back to the artist ID for older responses. Artist ID plus modern params
  returned the artist's Top songs shelf instead of releases and caused huge parser errors.
- The ytmusicapi upgrade also fixes the logged watch-playlist endpoint failure.
  Live recommendations for the two logged seeds returned HTTP 200 with 50 items.
- Redis hostname from the deployment logs does not resolve locally either. No
  credentials or deployed environment were changed. The current Redis Cloud public
  endpoint is required to repair the deployment's REDIS_URL; it cannot be inferred
  safely from the invalid hostname. Asked the user for hostname:port only.
- Cache `/health` now includes configured_backend, degraded and startup_error.
  SimpleCache remains usable during Redis startup failure, and successful cache
  operations no longer erase the persistent indication that Redis is unavailable.
- Redeploy with rebuilt requirements. Set REDIS_URL using the current public
  endpoint from Redis Cloud, preserve credentials privately, URL-encode the password,
  and use the provider's required redis:// or rediss:// scheme. Restart and verify
  `/health` reports backend=redis and degraded=false. If intentionally using only
  memory caching, set CACHE_BACKEND=simple explicitly.
- Live mix: HTTP 200 with 5 requested songs, ~14.8s; no release parser failures.
- Live Billboard: HTTP 200, 100 entries, chart date 2026-10-03; miss ~9.1s and
  subsequent hit ~14ms. Stats preserved, including rank 1 weeks=49 (not weeks at #1).
- Lyrics request in the log used artist `Song`. With artist `LANY`, the same title
  returned HTTP 200 through the existing LRCLIB source. Genius 403 is an upstream
  access restriction; do not bypass it or invent lyrics when all providers miss.
- Validation: 53 tests pass, including current/legacy Billboard parsing, malformed
  data, Redis DNS fallback health, and modern/legacy artist release IDs.

## Current State Summary

### 2026-10-02 Startup and Connection Pool Follow-up

- User explicitly chose memory caching. CACHE_BACKEND now defaults to simple,
  including invalid values, and render.yaml sets CACHE_BACKEND=simple. Explicit
  CACHE_BACKEND=redis remains supported; remove/change any such existing Render
  environment override to use the user's selected memory backend.
- Added GET / with service status and a /health link; Flask also serves HEAD /
  with HTTP 200 for platform probes. /health continues providing detailed status.
- Added an optional Render Blueprint with the existing build/start commands and
  healthCheckPath=/health. It is not automatically applied to a manually created
  service. Browser control failed at Windows sandbox initialization, so existing
  Render dashboard settings could not be edited in this session.
- Both Requests sessions now use blocking connection pools sized to the YTMusic
  executor. YTMusic receives its own configured session through requests_session;
  its original 30-second socket timeout is preserved. This removes the default
  pool size 10 bottleneck during concurrent Billboard enrichment.
- Validation: 57 tests pass. A local HTTP server received two bursts of 12
  concurrent calls over the same 12 connections with no pool-full warnings.
  GET/HEAD / return 200; default startup with an obsolete REDIS_URL reports
  backend=simple, configured_backend=simple, degraded=false, startup_error=null.

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

## 5) Artwork Quality Upgrade (544 Profile, Schema-Compatible)

### Goal
Improve artwork quality returned by API responses (songs, artists, albums, related, mix, trending, recommendations, etc.) without breaking existing response contracts.

### What Was Added
- New centralized thumbnail utility module:
  - `services/thumbnail_quality.py`
  - Capabilities:
    - rewrite Google image size segments (`wX-hY`) to higher resolution targets,
    - normalize/dedupe thumbnail arrays,
    - append higher-quality variant (target max dimension 544) when source is small,
    - generate fallback thumbnails from `videoId` when no thumbnails are provided,
    - recursively enhance nested payloads while preserving schema.

### Integration Points
- `app.py`:
  - `transform_song_data(...)` now normalizes thumbnails through shared helper.
  - Route payload enhancement applied before `jsonify(...)` on:
    - `/search`
    - `/song/<song_id>`
    - `/related/<song_id>`
    - `/artist/<artist_id>`
    - `/artist/<artist_id>/songs`
    - `/album/<album_id>`
    - `/mix`
    - `/trending`
    - `/billboard`
    - `/songs`
    - `/recommendations`
    - `/artists` (including `thumbnail` object enhancement path)
- `services/hot_endpoints.py`:
  - `format_thumbnails(...)` now routes through shared normalization helper.

### Compatibility Notes
- Endpoint schemas and field names were preserved.
- No cache-key changes were introduced.
- Behavioral-only improvements:
  - better URLs inside existing thumbnail fields,
  - optional additional high-quality thumbnail entry in existing `thumbnails` arrays,
  - fallback thumbnail population when upstream thumbnails are empty and `videoId` is known.

### Tests Added
- New test module:
  - `tests/test_thumbnail_quality.py`
- Coverage includes:
  - URL rewrite/upscale behavior,
  - aspect-ratio-preserving upscale behavior,
  - empty-thumbnail fallback generation from `videoId`,
  - recursive payload enhancement behavior.

### Validation
- Command:
  - `.\.venv\Scripts\python -m pytest -q`
- Result after this workstream:
  - `34 passed`

### App Repo Counterpart
- Companion Flutter-side artwork quality updates are documented in:
  - `D:\flutter projects\jazz\AGENT_HANDOFF.md`

## 6) Lyrics Provider Chain Upgrade + `/lyrics` Contract Extension

### Goal
Improve `/lyrics` reliability and payload quality while preserving backward compatibility for existing clients.

### What Was Added
- Expanded `/lyrics` resolver to use a provider chain:
  1. `lrclib` (`/api/get`) for synced + plain payloads
  2. existing Genius scrape fallback
  3. existing `lyrics.ovh` fallback
- Added artist normalization candidate flow:
  - exact artist input first
  - primary artist fallback (split by comma/feat/ft/featuring separators)
- Added plain-lyrics extraction from synced LRC when plain text is missing.

### `/lyrics` Response Compatibility
- Existing successful fields are preserved:
  - `song_title`
  - `artist`
  - `lyrics`
- Added optional extended fields:
  - `syncedLyrics`
  - `isSynced`
  - `source` (`lrclib` | `genius` | `lyrics_ovh`)
  - `normalizedArtist`

### Cache Behavior
- Positive lyrics caching remains envelope-based (`payload`, TTL + stale window).
- Negative backoff cache semantics remain unchanged:
  - exponential backoff retry windows
  - repeated misses are short-circuited until retry threshold.
- Extended lyrics payload is now what gets cached on successful responses.

### Files Modified in This Workstream (API Repo)
- `app.py`
- `tests/test_lyrics_cache.py`
- `AGENT_HANDOFF.md`

### Tests Added/Updated
- `tests/test_lyrics_cache.py` now covers:
  1. successful cached response with extended fields (`syncedLyrics`, `isSynced`, etc.)
  2. plain-only lyrics payload handling
  3. negative-cache short-circuit behavior
  4. negative-cache backoff TTL growth behavior
  5. primary-artist normalization fallback behavior for multi-artist input

### Validation
- Command:
  - `.\.venv\Scripts\python -m pytest -q`
- Result after this workstream:
  - `36 passed`

### App Repo Counterpart
- Companion Flutter-side lyrics state/parser/backend-integration updates are documented in:
  - `D:\flutter projects\jazz\AGENT_HANDOFF.md`

## 7) Lyrics Follow-up: Encoded `lyrics.ovh` Fallback Paths

### Goal
Fix fallback reliability for `/lyrics` when artist/title values include URL-sensitive characters, while preserving existing `/lyrics` response contract and cache behavior.

### Problem
`fetch_lyrics_ovh(...)` built fallback URLs with raw path interpolation:
- `https://api.lyrics.ovh/v1/{artist}/{title}`

For values like `AC/DC`, unencoded path separators could alter endpoint path semantics and cause fallback misses.

### What Was Added
- Updated fallback URL construction in `app.py`:
  - encode `artist_name` and `song_title` with `quote(..., safe="")`
  - use encoded values for path segment assembly
- Added regression test coverage in `tests/test_lyrics_cache.py`:
  - new `lyrics_ovh_success` mode pass-through in fake client chain
  - new test validates:
    - `/lyrics` success response with `source == "lyrics_ovh"` for encoded-input scenario
    - outbound fallback URL contains encoded segments (`AC%2FDC`, `Back%20In%20Black`)

### Compatibility Notes
- `/lyrics` response schema is unchanged.
- Existing cache semantics (positive cache and negative backoff cache) are unchanged.
- This is a behavior fix only for fallback path construction.

### Files Modified in This Workstream (API Repo)
- `app.py`
- `tests/test_lyrics_cache.py`
- `AGENT_HANDOFF.md`

### Validation
- Command:
  - `.\.venv\Scripts\python -m pytest -q tests/test_lyrics_cache.py`
- Result after this follow-up:
  - `6 passed`

### App Repo Counterpart
- Companion Flutter-side synced-lyrics index guard fix is documented in:
  - `D:\flutter projects\jazz\AGENT_HANDOFF.md`
