"""
Sky Dental Supply HTML fetcher.

Crawl strategy
--------------
Fetch each configured listing page, extract product links using the
Claude-discovered schema, then fetch each product page individually.
Pagination: Sky Dental category pages use ?page=N — we follow until no
new product links appear or the page limit is reached.

Volume cap
----------
Scope reduction: target 150-250 products for this source. We cap at
LISTING_PAGE_LIMIT pages per category. Comment retained per spec.
"""

import logging
import re
from typing import AsyncIterator, Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from crawler.fetchers.base import RateLimiter, fetch_with_retry

log = logging.getLogger(__name__)

BASE_URL = "https://www.skydentalsupply.com"
HOST = "www.skydentalsupply.com"

# Listing pages to seed the crawl. Confirmed to contain nitrile exam gloves
# and IV/infusion supply categories based on pre-crawl category research.
LISTING_PATHS = [
    "/gloves/",
    "/gloves-nitrile/",
    "/infusion-set/",
    "/administration-sets/",
]

# Pages per listing; each page holds ~20 products.
# /gloves/ has 6 pages; /gloves-nitrile/ has 3 but fully overlaps /gloves/.
# Limit 6 covers all gloves pages; infusion/admin-sets have only 1 page each.
# Expected yield: ~22*6 gloves + 8 infusion + 11 admin ≈ 151 products.
LISTING_PAGE_LIMIT = 6


def listing_url(path: str, page: int = 1) -> str:
    # Sky Dental pagination uses /category/2/ not ?page=2
    base = urljoin(BASE_URL, path)
    return base if page == 1 else f"{BASE_URL}{path}{page}/"


async def fetch_listing_page(
    session: httpx.AsyncClient,
    path: str,
    page: int,
    rate_limiter: RateLimiter,
    no_network: bool = False,
    conn=None,
    run_id=None,
) -> tuple[Optional[bytes], str]:
    url = listing_url(path, page)
    return await fetch_with_retry(
        session, url, source="skydental", rate_limiter=rate_limiter,
        no_network=no_network, ext="html", conn=conn, run_id=run_id,
    )


def extract_product_urls_from_listing(
    html: bytes, schema: dict
) -> list[str]:
    """
    Pull product page URLs from a listing page HTML.

    Tries the Claude-discovered CSS selector first, then falls back to a
    heuristic scan of <a> tags whose href looks like a product detail path.
    Returns absolute URLs.
    """
    soup = BeautifulSoup(html, "html.parser")
    selector = schema.get("product_link_selector")
    urls: list[str] = []

    if selector:
        try:
            anchors = soup.select(selector)
            seen: set[str] = set()
            for a in anchors:
                if a.get("href"):
                    full = urljoin(BASE_URL, a["href"])
                    if full not in seen:
                        seen.add(full)
                        urls.append(full)
        except Exception as exc:
            log.warning("Schema product_link_selector %r failed: %s", selector, exc)

    # Heuristic fallback: look for <a> hrefs that look like product pages.
    # Sky Dental product detail pages end in .htm (e.g. /product-name.htm).
    # Category/nav pages have no file extension — this is the key discriminator.
    if not urls:
        log.debug("Using heuristic product URL extraction for Sky Dental listing")
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            full = urljoin(BASE_URL, href)
            parsed = urlparse(full)
            if (
                parsed.netloc in ("www.skydentalsupply.com", "skydentalsupply.com")
                and parsed.path.endswith(".htm")
                and full not in seen
            ):
                seen.add(full)
                urls.append(full)

    log.debug("Extracted %d product URLs from listing", len(urls))
    return urls


async def fetch_product_page(
    session: httpx.AsyncClient,
    url: str,
    rate_limiter: RateLimiter,
    no_network: bool = False,
    conn=None,
    run_id=None,
) -> tuple[Optional[bytes], str]:
    return await fetch_with_retry(
        session, url, source="skydental", rate_limiter=rate_limiter,
        no_network=no_network, ext="html", conn=conn, run_id=run_id,
    )


async def iter_all_products(
    session: httpx.AsyncClient,
    rate_limiter: RateLimiter,
    schema: dict,
    no_network: bool = False,
    conn=None,
    run_id=None,
) -> AsyncIterator[tuple[str, bytes]]:
    """
    Yield (product_url, html_bytes) for every product found across all
    configured listing paths.

    Listing pages and product pages are fetched sequentially because all
    share the same host and rate limiter.
    """
    seen_product_urls: set[str] = set()

    for path in LISTING_PATHS:
        log.info("Crawling listing: %s", path)
        for page_num in range(1, LISTING_PAGE_LIMIT + 1):
            html, fetch_source = await fetch_listing_page(
                session, path, page_num, rate_limiter, no_network,
                conn=conn, run_id=run_id,
            )
            if html is None:
                log.warning("Failed to fetch listing %s page %d", path, page_num)
                break

            product_urls = extract_product_urls_from_listing(html, schema)
            new_urls = [u for u in product_urls if u not in seen_product_urls]
            seen_product_urls.update(new_urls)

            if not new_urls:
                log.info("No new product URLs on %s page %d — stopping pagination", path, page_num)
                break

            log.info("Listing %s page %d: %d new product URLs", path, page_num, len(new_urls))

            for product_url in new_urls:
                product_html, _ = await fetch_product_page(
                    session, product_url, rate_limiter, no_network,
                    conn=conn, run_id=run_id,
                )
                if product_html is not None:
                    yield product_url, product_html
                else:
                    log.warning("Failed to fetch product page: %s", product_url)
