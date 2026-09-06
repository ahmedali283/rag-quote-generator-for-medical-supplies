"""
Core HTTP fetching primitives shared by all source fetchers.

Rate limiting design
--------------------
Each host gets exactly one RateLimiter instance (module-level registry).
The asyncio.Lock inside RateLimiter is held for the entire duration of the
inter-request sleep, ensuring that two coroutines targeting the same host
queue behind the lock rather than both deciding "no wait needed" at the
same instant. This gives strictly serial access within a host while allowing
different hosts to proceed concurrently.

Retry design
------------
- 429: honour Retry-After header (capped at MAX_BACKOFF). Count against retries.
- 5xx: exponential backoff starting at BASE_BACKOFF, doubling each attempt.
- After MAX_RETRIES failures: return (None, 'failed') — never crash the run.
- Every retry is logged with URL, attempt number, and status code so operators
  can identify persistently flaky endpoints.

Cache integration
-----------------
fetch_with_retry checks the filesystem cache before touching the network.
Successful responses are written to cache before being returned so that a
crash immediately after a download still has the data on disk.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
import psycopg2.extensions

from crawler import cache as cache_module
from crawler import db as db_module
from crawler import robots

log = logging.getLogger(__name__)

USER_AGENT = "MedCatalogBot/1.0 (+mailto:crawler@example.com)"
MAX_RETRIES = 3
BASE_BACKOFF = 2.0    # seconds; doubles each retry attempt
MAX_BACKOFF = 60.0    # cap so a hostile Retry-After cannot stall us for hours


class NetworkDisabledError(Exception):
    """Raised when --no-network is set and the requested URL is not cached."""


# ── Rate limiter ──────────────────────────────────────────────────────────────

@dataclass
class RateLimiter:
    """
    Enforces a minimum inter-request interval for a single host.

    The lock must cover the sleep (not just the timestamp check) so that
    two coroutines cannot both observe "no wait needed" simultaneously.

    The lock is created lazily on first acquire() call rather than at
    __init__ time. asyncio.Lock() must be created inside a running event
    loop; constructing it in a dataclass default_factory (which runs at
    instantiation, potentially outside the loop) raises DeprecationWarning
    in Python <=3.9 and can bind to the wrong loop.
    """

    host: str
    min_interval: float = 1.0
    _last_request_time: float = field(default=0.0, init=False, repr=False)
    _lock: Optional[asyncio.Lock] = field(default=None, init=False, repr=False)

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def acquire(self) -> None:
        async with self._get_lock():
            now = time.monotonic()
            wait = self.min_interval - (now - self._last_request_time)
            if wait > 0:
                log.debug("Rate limiting %s: sleeping %.2fs", self.host, wait)
                await asyncio.sleep(wait)
            self._last_request_time = time.monotonic()


# Module-level registry so each host has exactly one RateLimiter.
_rate_limiters: dict[str, RateLimiter] = {}


def get_rate_limiter(host: str, crawl_delay_override: Optional[float] = None) -> RateLimiter:
    """
    Return (creating if necessary) the singleton RateLimiter for `host`.

    If the site's robots.txt specifies a Crawl-delay greater than our
    default 1 req/sec, we respect it.
    """
    if host not in _rate_limiters:
        robots_delay = robots.get_crawl_delay(host)
        interval = max(
            1.0,
            crawl_delay_override or 0.0,
            robots_delay or 0.0,
        )
        _rate_limiters[host] = RateLimiter(host=host, min_interval=interval)
        log.debug("Created RateLimiter for %s (interval=%.1fs)", host, interval)
    return _rate_limiters[host]


# ── Session builder ───────────────────────────────────────────────────────────

def build_session() -> httpx.AsyncClient:
    """
    Return a configured httpx.AsyncClient. Use as an async context manager.

    Timeouts: 10s connect, 30s read — long enough for slow medical supply
    sites, short enough to surface hangs quickly.
    """
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0),
        follow_redirects=True,
        http2=False,  # many older hosting stacks don't support HTTP/2
    )


# ── Core fetch ───────────────────────────────────────────────────────────────

async def fetch_with_retry(
    session: httpx.AsyncClient,
    url: str,
    source: str,
    rate_limiter: RateLimiter,
    no_network: bool = False,
    params: Optional[dict] = None,
    ext: str = "html",
    conn: Optional[psycopg2.extensions.connection] = None,
    run_id: Optional[int] = None,
) -> tuple[Optional[bytes], str]:
    """
    Fetch `url` with caching, robots.txt enforcement, and retry logic.

    Returns (content_bytes, source_label) where source_label is one of:
      'cache'      — served from local filesystem cache
      'network'    — freshly fetched and written to cache
      'robots'     — disallowed by robots.txt; caller should log and skip
      'failed'     — exhausted all retries; caller should mark queue item failed
      'no_network' — --no-network mode and URL not in cache (raises instead)
      'skipped'    — URL was filtered by source-specific rules before fetch

    The caller is responsible for committing DB state after this returns.
    """
    # Build the canonical URL including any query params (for cache keying).
    if params:
        encoded = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        cache_url = f"{url}?{encoded}"
    else:
        cache_url = url

    # 1. Cache hit — skip everything.
    cached = cache_module.get(source, cache_url, ext)
    if cached is not None:
        log.debug("Cache hit: %s", cache_url)
        return cached, "cache"

    # 2. --no-network: error loudly rather than silently returning None.
    if no_network:
        raise NetworkDisabledError(
            f"--no-network is set but {cache_url!r} is not in the cache. "
            f"Run without --no-network first to populate the cache."
        )

    # 3. robots.txt check — run for every URL, including paginated ones.
    robots.ensure_fetched(url)
    if not robots.is_allowed(url):
        log.info("Skipped (robots.txt): %s", url)
        return None, "robots"

    # 4. Fetch with retry.
    attempt = 0
    last_error: Optional[str] = None
    full_url = url  # requests use the real URL; cache uses cache_url

    while attempt < MAX_RETRIES:
        await rate_limiter.acquire()
        try:
            response = await session.get(full_url, params=params)
        except httpx.RequestError as exc:
            last_error = f"RequestError: {exc}"
            log.warning("Attempt %d/%d failed for %s: %s", attempt + 1, MAX_RETRIES, url, exc)
            if conn is not None and run_id is not None:
                db_module.increment_crawl_run_counter(conn, run_id, "retries")
            backoff = min(BASE_BACKOFF * (2 ** attempt), MAX_BACKOFF)
            await asyncio.sleep(backoff)
            attempt += 1
            continue

        if response.status_code == 200:
            content = response.content
            cache_module.put(source, cache_url, content, ext)
            log.debug("Fetched %s (%d bytes)", url, len(content))
            return content, "network"

        if response.status_code == 429:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            sleep_for = min(retry_after or BASE_BACKOFF * (2 ** attempt), MAX_BACKOFF)
            log.warning(
                "429 from %s on attempt %d/%d — sleeping %.0fs",
                url, attempt + 1, MAX_RETRIES, sleep_for,
            )
            if conn is not None and run_id is not None:
                db_module.increment_crawl_run_counter(conn, run_id, "retries")
            await asyncio.sleep(sleep_for)
            attempt += 1
            last_error = f"HTTP 429 after {sleep_for:.0f}s backoff"
            continue

        if response.status_code >= 500:
            backoff = min(BASE_BACKOFF * (2 ** attempt), MAX_BACKOFF)
            log.warning(
                "HTTP %d from %s on attempt %d/%d — backing off %.0fs",
                response.status_code, url, attempt + 1, MAX_RETRIES, backoff,
            )
            if conn is not None and run_id is not None:
                db_module.increment_crawl_run_counter(conn, run_id, "retries")
            await asyncio.sleep(backoff)
            attempt += 1
            last_error = f"HTTP {response.status_code}"
            continue

        # 4xx (except 429) are not retryable.
        log.warning("HTTP %d for %s — not retrying", response.status_code, url)
        return None, f"failed:http{response.status_code}"

    log.error("Exhausted %d retries for %s (last error: %s)", MAX_RETRIES, url, last_error)
    return None, "failed"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_retry_after(header_value: Optional[str]) -> Optional[float]:
    """
    Parse Retry-After as an integer (delta-seconds) or HTTP-date.
    Returns seconds to sleep, or None if unparseable.
    """
    if not header_value:
        return None
    try:
        return float(header_value)
    except ValueError:
        pass
    import email.utils
    try:
        dt = email.utils.parsedate_to_datetime(header_value)
        import datetime
        delta = (dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        return max(0.0, delta)
    except Exception:
        return None
