"""
Anatomy Warehouse HTML fetcher.

URL filtering
-------------
In addition to robots.txt enforcement (applied by fetch_with_retry), this
source has three URL patterns that must be skipped individually per spec.
These are site-specific exclusions, not robots.txt rules, so they are
enforced here rather than in robots.py.

  simrated-custom-kit   — SimRated custom kit builder pages (not real products)
  landing-page-template — Shopify landing page templates
  ems-bundles           — EMS bundle promotional pages

Note: 'simrated' is intentional (not 'simulated') — it matches the actual
URL pattern on the site. Do not "correct" this spelling.

Volume cap
----------
Scope reduction: target 150-250 products for this source. Two category pages
with LISTING_PAGE_LIMIT pages each gives us enough IV trainers and manikins.
Comment retained per spec.
"""

import logging
import re
from typing import AsyncIterator, Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from crawler.fetchers.base import RateLimiter, fetch_with_retry

log = logging.getLogger(__name__)

BASE_URL = "https://anatomywarehouse.com"
HOST = "anatomywarehouse.com"

LISTING_PATHS = [
    "/simulators/task-trainers/iv-training/",
    "/simulators/manikins/",
]

# Anatomy Warehouse uses ?page=N pagination on category pages.
# Target 150-250 products; each page has ~12-24 products.
# 4 pages × 2 listings × ~20 products = ~160 products — within target range.
LISTING_PAGE_LIMIT = 4

# URL substrings that must never be fetched from this source.
# These are individually blocked per spec, independent of robots.txt.
SKIP_URL_PATTERNS = [
    "simrated-custom-kit",
    "landing-page-template",
    "ems-bundles",
]


def should_skip_url(url: str) -> bool:
    """
    Return True if the URL contains any of the individually blocked patterns.
    Case-insensitive to handle any URL casing variations.
    """
    lower = url.lower()
    for pattern in SKIP_URL_PATTERNS:
        if pattern in lower:
            log.info("Skipped (blocked pattern %r): %s", pattern, url)
            return True
    return False


def listing_url(path: str, page: int = 1) -> str:
    base = urljoin(BASE_URL, path)
    return base if page == 1 else f"{base}?page={page}"


async def fetch_listing_page(
    session: httpx.AsyncClient,
    path: str,
    page: int,
    rate_limiter: RateLimiter,
    no_network: bool = False,
) -> tuple[Optional[bytes], str]:
    url = listing_url(path, page)
    if should_skip_url(url):
        return None, "skipped"
    return await fetch_with_retry(
        session, url, source="anatomywarehouse", rate_limiter=rate_limiter,
        no_network=no_network, ext="html",
    )


def extract_product_urls_from_listing(
    html: bytes, schema: dict
) -> list[str]:
    """
    Extract product page URLs from listing HTML.

    All returned URLs are filtered through should_skip_url before being
    returned, so callers never receive a URL that should be blocked.
    Returns absolute URLs.
    """
    soup = BeautifulSoup(html, "html.parser")
    selector = schema.get("product_link_selector")
    urls: list[str] = []

    if selector:
        try:
            anchors = soup.select(selector)
            urls = [
                urljoin(BASE_URL, a["href"])
                for a in anchors
                if a.get("href")
            ]
        except Exception as exc:
            log.warning("Schema product_link_selector %r failed: %s", selector, exc)

    # Heuristic fallback: Anatomy Warehouse product URLs have slugs like
    # /product-name-a-NNNNNN (the -a-NNNNNN suffix is a product ID).
    if not urls:
        log.debug("Using heuristic product URL extraction for Anatomy Warehouse listing")
        id_pattern = re.compile(r"-a-\d+")
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            full = urljoin(BASE_URL, href)
            parsed = urlparse(full)
            if (
                parsed.netloc in ("anatomywarehouse.com", "www.anatomywarehouse.com")
                and id_pattern.search(parsed.path)
                and full not in seen
            ):
                seen.add(full)
                urls.append(full)

    # Filter individually blocked URL patterns.
    filtered = [u for u in urls if not should_skip_url(u)]
    if len(filtered) < len(urls):
        log.info(
            "Filtered %d blocked URLs from listing (%d remain)",
            len(urls) - len(filtered), len(filtered),
        )

    return filtered


async def fetch_product_page(
    session: httpx.AsyncClient,
    url: str,
    rate_limiter: RateLimiter,
    no_network: bool = False,
) -> tuple[Optional[bytes], str]:
    """Fetch a product page, applying the blocked-pattern filter first."""
    if should_skip_url(url):
        return None, "skipped"
    return await fetch_with_retry(
        session, url, source="anatomywarehouse", rate_limiter=rate_limiter,
        no_network=no_network, ext="html",
    )


async def iter_all_products(
    session: httpx.AsyncClient,
    rate_limiter: RateLimiter,
    schema: dict,
    no_network: bool = False,
) -> AsyncIterator[tuple[str, bytes]]:
    """
    Yield (product_url, html_bytes) for every valid product found across
    all configured listing paths.

    All requests go through the same rate limiter (strictly serial within host).
    """
    seen_product_urls: set[str] = set()

    for path in LISTING_PATHS:
        log.info("Crawling listing: %s", path)
        for page_num in range(1, LISTING_PAGE_LIMIT + 1):
            html, fetch_source = await fetch_listing_page(
                session, path, page_num, rate_limiter, no_network
            )
            if html is None:
                if fetch_source != "skipped":
                    log.warning("Failed to fetch listing %s page %d", path, page_num)
                break

            product_urls = extract_product_urls_from_listing(html, schema)
            new_urls = [u for u in product_urls if u not in seen_product_urls]
            seen_product_urls.update(new_urls)

            if not new_urls:
                log.debug(
                    "No new product URLs on %s page %d — stopping pagination",
                    path, page_num,
                )
                break

            log.info(
                "Listing %s page %d: %d new product URLs",
                path, page_num, len(new_urls),
            )

            for product_url in new_urls:
                product_html, _ = await fetch_product_page(
                    session, product_url, rate_limiter, no_network
                )
                if product_html is not None:
                    yield product_url, product_html
                else:
                    log.warning("Failed to fetch product page: %s", product_url)
