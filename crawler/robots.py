"""
Programmatic robots.txt enforcement.

Fetches robots.txt using httpx.get() (the synchronous httpx API) with the
same User-Agent and headers sent by all other crawler requests. This avoids
the failure mode of urllib.robotparser.read(), which uses urllib.request
internally with a generic Python User-Agent that Cloudflare and similar WAFs
block with a 403. When RobotFileParser.read() receives a 403, it silently
sets disallow_all=True without raising, so the exception-based fallback in
the old code never fired.

We feed the fetched text into RobotFileParser.parse(lines) rather than
.read(), keeping all the standard parsing logic while controlling the fetch.

Fallback behaviour on fetch failure
------------------------------------
If the httpx fetch fails (network error, non-200 status, or disallow_all
still set after parsing — e.g. a genuine empty-body 403 even with correct
headers), we log a WARNING and store an allow-all parser. The operator sees
the warning; the crawl continues. We do not silently block the entire source.
"""

import logging
import urllib.parse
import urllib.robotparser
from typing import Optional

import httpx

# robots.py imports httpx directly for the synchronous robots.txt fetch.
# It does not import from crawler.fetchers to avoid a circular dependency
# (fetchers.base imports robots). USER_AGENT is duplicated here intentionally
# so robots.py remains a near-leaf module.
_USER_AGENT = "MedCatalogBot/1.0 (+mailto:crawler@example.com)"

log = logging.getLogger(__name__)

# Module-level cache: hostname -> parsed RobotFileParser
_robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}

# The token that robots.txt rules are checked against. Must be a prefix match
# of _USER_AGENT so that rules like "User-agent: MedCatalogBot" apply.
ROBOTS_AGENT = "MedCatalogBot"


def _allow_all_parser() -> urllib.robotparser.RobotFileParser:
    """
    Return a parser that allows all URLs.

    A freshly constructed RobotFileParser with no parse() call returns False
    from can_fetch() because mtime()==0 signals "never loaded". Calling
    parse([]) sets the mtime and leaves entries empty, which the parser
    correctly interprets as "no rules — allow everything".
    """
    rp = urllib.robotparser.RobotFileParser()
    rp.parse([])
    return rp


def fetch_robots(host: str, scheme: str = "https") -> None:
    """
    Fetch and parse robots.txt for `host`. Idempotent — a second call for
    the same host is a no-op (already cached).

    Uses httpx.get() (synchronous) with the crawler's real User-Agent so that
    WAF/CDN bot-detection rules that block urllib's default User-Agent don't
    prevent us from reading the actual rules.

    On any fetch failure or unparseable response, logs a WARNING and stores
    an allow-all parser so the crawl continues.
    """
    if host in _robots_cache:
        return

    url = f"{scheme}://{host}/robots.txt"
    rp = _allow_all_parser()
    rp.set_url(url)

    try:
        response = httpx.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
            timeout=10.0,
        )
    except Exception as exc:
        log.warning(
            "Could not fetch robots.txt for %s (%s) — treating as allow-all",
            host, exc,
        )
        _robots_cache[host] = _allow_all_parser()
        return

    if response.status_code == 200:
        lines = response.text.splitlines()
        rp.parse(lines)
        # Safety net: if parsing a 200 response somehow still sets disallow_all
        # (e.g. empty body with a quirky server), fall back to allow-all.
        if rp.disallow_all:
            log.warning(
                "robots.txt for %s parsed as disallow-all despite 200 response "
                "(empty or malformed file?) — treating as allow-all",
                host,
            )
            _robots_cache[host] = _allow_all_parser()
        else:
            log.info("Loaded robots.txt for %s", host)
            _robots_cache[host] = rp
    else:
        # Non-200: site is up but actively declining to serve robots.txt.
        # Per RFC 9309 s2.3.1: 4xx (other than 401/403) → allow all.
        # 401/403 → disallow all per spec, but we treat unreachable as
        # allow-all to avoid blocking the entire source on a CDN challenge page.
        log.warning(
            "robots.txt for %s returned HTTP %d — treating as allow-all",
            host, response.status_code,
        )
        _robots_cache[host] = _allow_all_parser()


def ensure_fetched(url: str) -> None:
    """
    Idempotently fetch robots.txt for the host of `url`.
    Useful when product URLs are discovered mid-crawl and their host
    might differ from the seed host.
    """
    parsed = urllib.parse.urlparse(url)
    fetch_robots(parsed.netloc, scheme=parsed.scheme or "https")


def is_allowed(url: str) -> bool:
    """
    Return True if ROBOTS_AGENT may fetch `url` according to the cached
    robots.txt for that host.

    If the host is not yet in the cache (should not happen in normal flow),
    we fetch it now and log a warning.
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc
    if host not in _robots_cache:
        log.warning(
            "robots.txt for %s was not pre-fetched; fetching now (URL: %s)",
            host,
            url,
        )
        fetch_robots(host, scheme=parsed.scheme or "https")

    allowed = _robots_cache[host].can_fetch(ROBOTS_AGENT, url)
    if not allowed:
        log.info("robots.txt disallows %s for agent %s", url, ROBOTS_AGENT)
    return allowed


def get_crawl_delay(host: str) -> Optional[float]:
    """
    Return the Crawl-delay directive for ROBOTS_AGENT on `host`, or None.
    Callers use this to floor their per-host rate limit — our minimum is
    already 1 req/sec, so this only matters if the site asks for more.
    """
    if host not in _robots_cache:
        return None
    delay = _robots_cache[host].crawl_delay(ROBOTS_AGENT)
    return float(delay) if delay is not None else None
