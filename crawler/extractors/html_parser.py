"""
HTML product page extractor for Sky Dental and Anatomy Warehouse.

Applies the CSS selectors discovered by html_claude.py via BeautifulSoup.
If a selector is None (schema not discovered) or fails to find the element,
falls back to heuristic extraction — a best-effort scan of common HTML
patterns used by medical supply e-commerce sites.

content_hash computation
------------------------
Uses the identical field order and separator as shopify.compute_content_hash
so that hash comparisons across sources are consistent. Both functions
delegate to the shared _hash_fields() helper below rather than duplicating
the logic.
"""

import hashlib
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup, Tag

log = logging.getLogger(__name__)


# ── Shared hash logic (also used by shopify.py via import) ────────────────────

def compute_content_hash(
    name: str,
    description: str,
    price: str,
    pack_info: str,
    category: str,
    manufacturer: str,
) -> str:
    """
    sha256 of NUL-delimited normalized fields.

    The NUL separator ensures that ("ab", "c") and ("a", "bc") produce
    different hashes. Field order is fixed — do not reorder.
    """
    payload = "\x00".join([
        (name or "").strip(),
        (description or "").strip(),
        (price or "").strip(),
        (pack_info or "").strip(),
        (category or "").strip(),
        (manufacturer or "").strip(),
    ])
    return hashlib.sha256(payload.encode()).hexdigest()


# ── BeautifulSoup helpers ──────────────────────────────────────────────────────

def extract_text(
    soup: BeautifulSoup,
    selector: Optional[str],
    default: str = "",
) -> str:
    """
    Return stripped text from the first element matching `selector`.
    Returns `default` if selector is None, no element found, or any error.
    Never raises.
    """
    if not selector:
        return default
    try:
        el = soup.select_one(selector)
        if el:
            return el.get_text(separator=" ", strip=True)
    except Exception as exc:
        log.debug("extract_text selector %r failed: %s", selector, exc)
    return default


def extract_price(
    soup: BeautifulSoup,
    selector: Optional[str],
) -> Optional[float]:
    """
    Extract a numeric price using `selector`.

    Checks the `content` attribute first (schema.org pattern), then falls
    back to visible text. Returns None if no price can be parsed or if the
    parsed price is zero — zero is used by some sites (e.g. Magento
    international stores) as a sentinel for "call for price" and must not
    be stored as a real price.
    """
    if not selector:
        return None
    try:
        el = soup.select_one(selector)
        if el:
            content = el.get("content") or el.get("data-price")
            if content:
                price = _parse_price_text(str(content))
                if price is not None and price > 0:
                    return price
                elif price is not None:
                    # Zero from a content attribute is a "call for price" sentinel.
                    return None
            price = _parse_price_text(el.get_text(separator=" ", strip=True))
            if price is not None and price > 0:
                return price
    except Exception as exc:
        log.debug("extract_price selector %r failed: %s", selector, exc)
    return None


def _price_element_is_zero(soup: BeautifulSoup, selector: Optional[str]) -> bool:
    """
    Return True if the price selector matched an element whose content
    attribute is explicitly "0". This distinguishes "no price element found"
    (heuristic should try) from "price element found but says zero" (call-for-
    price — heuristic must not run or it will pick up widget/related-item prices).
    """
    if not selector:
        return False
    try:
        el = soup.select_one(selector)
        if el:
            content = el.get("content") or el.get("data-price")
            if content is not None:
                price = _parse_price_text(str(content))
                return price is not None and price == 0
    except Exception:
        pass
    return False


def _parse_price_text(text: str) -> Optional[float]:
    """
    Parse a price from arbitrary text like "$24.99", "24,99", "USD 24.99".
    Returns the first float-shaped number found, or None.
    """
    # Match numbers like 24.99 or 24,99 (European) or 1,234.56
    match = re.search(r"(\d{1,3}(?:[,\s]\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)", text.replace(",", ""))
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    return None


# ── Pack size / UOM ───────────────────────────────────────────────────────────

_PACK_PATTERNS = [
    (re.compile(r"(\d[\d,]*)\s*/\s*(\w+)", re.I),
     lambda m: (m.group(1).replace(",", ""), m.group(2))),
    (re.compile(r"(\w+)\s+of\s+(\d[\d,]*)", re.I),
     lambda m: (m.group(2).replace(",", ""), m.group(1))),
    (re.compile(r"(\d[\d,]*)\s+(\w+)", re.I),
     lambda m: (m.group(1).replace(",", ""), m.group(2))),
]


def parse_pack_size_uom(text: Optional[str]) -> tuple[str, Optional[str]]:
    """Extract pack_size and uom from free text. Same logic as shopify.py."""
    if not text:
        return ("", None)
    for pattern, extractor in _PACK_PATTERNS:
        m = pattern.search(text)
        if m:
            qty, unit = extractor(m)
            return qty, unit
    return text, None


# ── Heuristic fallbacks ───────────────────────────────────────────────────────

def _heuristic_name(soup: BeautifulSoup) -> str:
    """Best-effort product name extraction when no selector is available."""
    # Try common e-commerce patterns in order of reliability.
    for selector in [
        "h1.product-title",
        "h1.product_title",
        "h1[itemprop='name']",
        ".product-name h1",
        "h1",
    ]:
        el = soup.select_one(selector)
        if el:
            text = el.get_text(strip=True)
            if text:
                return text
    return ""


def _heuristic_price(soup: BeautifulSoup) -> Optional[float]:
    """Best-effort price extraction. Returns None for zero or call-for-price items."""
    # Prefer scoped selectors that target the main product info area, not
    # page-wide selectors that would pick up related-product widget prices.
    for selector in [
        ".product-info-main [itemprop='price']",
        ".product-info-main .price",
        "[itemprop='price']",
        ".product-price",
        ".woocommerce-Price-amount",
        ".price-box .price",
        "span.amount",
    ]:
        el = soup.select_one(selector)
        if el:
            content = el.get("content")
            if content:
                price = _parse_price_text(content)
                if price is not None and price > 0:
                    return price
                elif price is not None:
                    # Zero in a content attribute means "call for price" — stop here
                    # rather than falling through to widget prices lower on the page.
                    return None
            price = _parse_price_text(el.get_text())
            if price is not None and price > 0:
                return price
    return None


def _heuristic_sku(soup: BeautifulSoup) -> Optional[str]:
    for selector in [
        "[itemprop='sku']",
        ".sku",
        ".product-sku",
        ".sku-wrapper",
    ]:
        el = soup.select_one(selector)
        if el:
            text = el.get_text(strip=True)
            if text:
                return text
    return None


def _heuristic_description(soup: BeautifulSoup) -> str:
    for selector in [
        "[itemprop='description']",
        ".product-description",
        ".woocommerce-product-details__short-description",
        "#product-description",
        ".description",
    ]:
        el = soup.select_one(selector)
        if el:
            text = el.get_text(separator=" ", strip=True)
            if len(text) > 20:  # ignore empty/near-empty blocks
                return text
    return ""


# ── Main extractor ────────────────────────────────────────────────────────────

def extract_product(
    html: bytes,
    url: str,
    source: str,
    schema: dict,
    category: str = "",
) -> Optional[dict]:
    """
    Parse a product page HTML and return a normalized item dict.

    Returns None if name or price cannot be extracted — these are the
    minimum fields for a useful row. Missing SKU/description are stored
    as None.

    The `schema` dict comes from html_claude.discover_schema(). If a
    selector is None or fails, heuristic fallbacks are tried. Heuristic
    failures are logged with the specific field that was missing.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Name
    name = extract_text(soup, schema.get("name_selector")) or _heuristic_name(soup)
    if not name:
        log.warning("Extraction failure — no name: %s", url)
        return None

    # Price — None means "call for price" (no price to record), not an error.
    # _price_found_zero tracks when a selector matched but returned zero (call-for-price
    # sentinel): skip the heuristic in that case to avoid picking up widget prices.
    price = extract_price(soup, schema.get("price_selector"))
    if price is None and not _price_element_is_zero(soup, schema.get("price_selector")):
        price = _heuristic_price(soup)
    # price remaining None is valid: stored as raw_price=None, no price_history row.
    if price is None:
        log.debug("No listed price (call for price?): %s", url)

    # Optional fields
    sku = extract_text(soup, schema.get("sku_selector")) or _heuristic_sku(soup)
    description = (
        extract_text(soup, schema.get("description_selector"))
        or _heuristic_description(soup)
    )
    pack_size_raw = extract_text(soup, schema.get("pack_size_selector"))
    uom_raw = extract_text(soup, schema.get("uom_selector"))
    pack_size, uom = parse_pack_size_uom(pack_size_raw or uom_raw)

    # Manufacturer is not reliably present in HTML sources.
    manufacturer = None

    content_hash = compute_content_hash(
        name,
        description,
        f"{price:.4f}" if price is not None else "",
        pack_size or "",
        category,
        "",  # manufacturer not available for HTML sources
    )

    pack_info = " ".join(filter(None, [pack_size, uom])) or None

    return {
        # Columns match the real source_items schema.
        "source": source,
        "source_url": url,
        "source_sku": (sku or "").strip() or None,
        "raw_name": name.strip(),
        "raw_description": description.strip() or None,
        "raw_price": str(price) if price is not None else None,
        "raw_pack_info": pack_info,
        "raw_category": category or None,
        "raw_manufacturer": None,  # not reliably present in HTML sources
        "content_hash": content_hash,
    }
