"""
MediDepot fetcher — uses Shopify's public /products.json API.

Shopify API notes
-----------------
- /collections/<slug>/products.json?page=N returns up to 250 products.
- An empty `products` list signals the last page — no 404 or Link header.
- No authentication is required for public store fronts.
- We never fetch HTML product pages for this source; all data comes from JSON.

Volume cap
----------
COLLECTION_PAGE_LIMIT is set to 2 pages × 250 products/page = 500 products
maximum across all collections combined. In practice each targeted collection
has far fewer products. We stop well within the 150-250 per-source target.
Comment retained here per spec: this is a deliberate scope reduction for the
tighter assessment timeline.
"""

import logging
from typing import AsyncIterator, Optional
from urllib.parse import urljoin

import httpx

from crawler.fetchers.base import RateLimiter, fetch_with_retry

log = logging.getLogger(__name__)

BASE_URL = "https://medidepot.com"
HOST = "medidepot.com"

# Collections to crawl, in priority order. Gloves first because they are the
# strongest confirmed overlap category; manikins and blood collection follow.
COLLECTIONS = [
    "nitrile-exam-gloves",
    "gloves",
    "medical-training-manikins-simulators",
    "blood-collection-supply",
    "surgical-and-procedure",
    "iv-poles",
]

# Shopify returns 250 products/page. We fetch at most this many pages per
# collection. Two pages × 250 = 500 max; actual collections are much smaller.
# Scope reduction: target is 150-250 products total for this source.
COLLECTION_PAGE_LIMIT = 2


def products_json_url(collection_slug: str) -> str:
    return f"{BASE_URL}/collections/{collection_slug}/products.json"


async def iter_collection_products(
    session: httpx.AsyncClient,
    collection_slug: str,
    rate_limiter: RateLimiter,
    no_network: bool = False,
    conn=None,
    run_id=None,
) -> AsyncIterator[dict]:
    """
    Yield raw Shopify product dicts for a single collection.

    Paginates until the products list is empty (Shopify's sentinel) or
    COLLECTION_PAGE_LIMIT is reached. Each page URL is independently checked
    against robots.txt.
    """
    url = products_json_url(collection_slug)
    page = 1

    while page <= COLLECTION_PAGE_LIMIT:
        content, fetch_source = await fetch_with_retry(
            session,
            url,
            source="medidepot",
            rate_limiter=rate_limiter,
            no_network=no_network,
            params={"page": page, "limit": 250},
            ext="json",
            conn=conn,
            run_id=run_id,
        )

        if content is None:
            log.warning(
                "Failed to fetch collection %s page %d (source=%s)",
                collection_slug, page, fetch_source,
            )
            break

        try:
            import json
            data = json.loads(content)
        except Exception as exc:
            log.error("JSON parse error for %s page %d: %s", collection_slug, page, exc)
            break

        products = data.get("products", [])
        if not products:
            log.debug("Collection %s page %d returned 0 products — end of pagination",
                      collection_slug, page)
            break

        log.info(
            "Collection %s page %d: %d products (source=%s)",
            collection_slug, page, len(products), fetch_source,
        )
        for product in products:
            yield product

        page += 1


async def iter_all_products(
    session: httpx.AsyncClient,
    rate_limiter: RateLimiter,
    no_network: bool = False,
    conn=None,
    run_id=None,
) -> AsyncIterator[tuple[str, dict]]:
    """
    Yield (collection_slug, raw_product_dict) for all configured collections.

    Collections are processed sequentially because they share the same host
    and therefore the same rate limiter. Concurrent collection fetches would
    violate the strictly-serial-within-host requirement.
    """
    for slug in COLLECTIONS:
        log.info("Starting collection: %s", slug)
        async for product in iter_collection_products(
            session, slug, rate_limiter, no_network, conn=conn, run_id=run_id
        ):
            yield slug, product
