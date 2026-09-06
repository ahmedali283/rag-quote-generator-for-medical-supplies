"""
Pocket Nurse HTML fetcher.

Crawl strategy
--------------
Magento store at pocketnurse.com/international/. Fetch each configured
listing page, extract product links using the Claude-discovered schema (with
heuristic fallback), then fetch each product page individually.
Pagination: Magento uses ?p=N query parameter — we follow until no new
product links appear or the page limit is reached.

Volume cap
----------
Target 150-250 products. Three seed categories with 5 pages each (13
products/page) gives up to 195 products before deduplication. LISTING_PAGE_LIMIT
caps at 5 pages per category.
"""

import logging
from typing import AsyncIterator, Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from crawler.fetchers.base import RateLimiter, fetch_with_retry

log = logging.getLogger(__name__)

BASE_URL = "https://www.pocketnurse.com"
HOST = "www.pocketnurse.com"

LISTING_PATHS = [
    "/international/products/manikins-simulators/inj-venipuncture-trainers",
    "/international/products/manikins-simulators",
    "/international/products/laerdal-manikins",
]

# 5 pages confirmed via pagination links on all three seed categories.
LISTING_PAGE_LIMIT = 5


def listing_url(path: str, page: int = 1) -> str:
    base = urljoin(BASE_URL, path)
    return base if page == 1 else f"{base}?p={page}"


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
        session, url, source="pocketnurse", rate_limiter=rate_limiter,
        no_network=no_network, ext="html", conn=conn, run_id=run_id,
    )


def extract_product_urls_from_listing(
    html: bytes, schema: dict
) -> list[str]:
    """
    Pull product page URLs from a listing page HTML.

    Tries the Claude-discovered CSS selector first, then falls back to
    scanning .product-item containers for absolute /international/<sku-slug>
    anchors (depth-2 paths: ['international', '<slug>']).
    Returns absolute URLs.
    """
    soup = BeautifulSoup(html, "html.parser")
    selector = schema.get("product_link_selector")
    candidate_anchors: list = []

    if selector:
        try:
            candidate_anchors = soup.select(selector)
        except Exception as exc:
            log.warning("Schema product_link_selector %r failed: %s", selector, exc)

    # Always filter via depth-2 path check regardless of source.
    # Claude's selector can match nav/image anchors that don't point to products.
    # Product pages live at /international/<sku-slug> (exactly 2 path segments).
    seen: set[str] = set()
    urls: list[str] = []

    anchors_to_scan = candidate_anchors or soup.select(".product-item a[href]")
    if not candidate_anchors:
        log.debug("Using heuristic product URL extraction for Pocket Nurse listing")

    for a in anchors_to_scan:
        href = a.get("href")
        if not href:
            continue
        full = urljoin(BASE_URL, href)
        parsed = urlparse(full)
        if "pocketnurse.com" not in parsed.netloc:
            continue
        segs = [s for s in parsed.path.split("/") if s]
        if len(segs) == 2 and segs[0] == "international" and full not in seen:
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
        session, url, source="pocketnurse", rate_limiter=rate_limiter,
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
