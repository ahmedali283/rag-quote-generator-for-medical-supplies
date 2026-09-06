"""
Shopify products.json extractor for MediDepot.

All data arrives as structured JSON — no HTML parsing, no Claude discovery.
This extractor maps Shopify's product schema to the normalized source_items
schema.

Price handling
--------------
Shopify returns prices as strings (e.g., "285.58"). We keep them as strings
in the content_hash input to avoid floating-point representation drift, but
convert to float for the price column. For bulk medical supplies the
$0.001 precision of NUMERIC(12,4) is sufficient.

Pack size / UOM
---------------
Shopify encodes pack info in the variant title (e.g., "Gray / Medium",
"100/Box"). We apply a cascade of regex patterns to extract structured
pack_size and uom values. If no pattern matches, the full variant title
is stored as pack_size with uom=None.
"""

import hashlib
import html
import logging
import re
from html.parser import HTMLParser
from typing import Optional

log = logging.getLogger(__name__)

# ── Content hash ──────────────────────────────────────────────────────────────

def compute_content_hash(
    name: str,
    description: str,
    price: str,
    pack_info: str,
    category: str,
    manufacturer: str,
) -> str:
    """
    sha256 of concatenated normalized text fields.

    Field order is fixed and must be identical in html_parser.py.
    Using text fields (not raw HTML) means markup-only changes do not
    register as content changes and do not trigger spurious price_history rows.
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


# ── HTML stripping ────────────────────────────────────────────────────────────

class _TextExtractor(HTMLParser):
    """Minimal HTMLParser subclass that collects visible text."""

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip_tags = {"script", "style"}
        self._current_skip = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self._skip_tags:
            self._current_skip += 1

    def handle_endtag(self, tag):
        if tag.lower() in self._skip_tags and self._current_skip > 0:
            self._current_skip -= 1

    def handle_data(self, data):
        if self._current_skip == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(self._parts).split()  # type: ignore[return-value]


def strip_html(html_str: Optional[str]) -> str:
    """
    Strip HTML tags using stdlib html.parser.

    We use stdlib rather than BeautifulSoup here because this function is
    also called during content_hash computation where importing BS4 would
    be a heavier dependency than needed. BS4 is reserved for full-page
    structural parsing in html_parser.py.
    """
    if not html_str:
        return ""
    extractor = _TextExtractor()
    try:
        extractor.feed(html_str)
        return " ".join(extractor._parts).strip()
    except Exception:
        # If parsing fails, do a naive regex strip as last resort.
        return re.sub(r"<[^>]+>", " ", html_str).strip()


# ── Pack size / UOM parsing ───────────────────────────────────────────────────

_PACK_PATTERNS = [
    # "100/Box", "500/Case"
    (re.compile(r"(\d[\d,]*)\s*/\s*(\w+)", re.I), lambda m: (m.group(1).replace(",", ""), m.group(2))),
    # "Box of 100", "Case of 50"
    (re.compile(r"(\w+)\s+of\s+(\d[\d,]*)", re.I), lambda m: (m.group(2).replace(",", ""), m.group(1))),
    # "100 Pieces", "50 Gloves"
    (re.compile(r"(\d[\d,]*)\s+(\w+)", re.I), lambda m: (m.group(1).replace(",", ""), m.group(2))),
]


def parse_pack_size_uom(text: Optional[str]) -> tuple[str, Optional[str]]:
    """
    Extract structured pack_size and uom from a free-text variant title.
    Returns (pack_size_string, uom_string_or_None).
    """
    if not text:
        return ("", None)
    for pattern, extractor in _PACK_PATTERNS:
        m = pattern.search(text)
        if m:
            qty, unit = extractor(m)
            return qty, unit
    return text, None


# ── Main extractor ────────────────────────────────────────────────────────────

def extract_product(
    raw: dict,
    collection_slug: str,
    source_base_url: str = "https://medidepot.com",
) -> Optional[dict]:
    """
    Map a Shopify products.json product dict to a normalized item dict.

    Returns None if name or price is missing — these are the minimum fields
    needed for a useful source_items row. Missing SKU/description are
    acceptable (stored as None).

    Multi-variant products: we take the first variant's price and SKU.
    If a product has meaningful variant-level data (multiple sizes at
    different prices), full multi-variant support is a Part 2 concern.
    """
    name = (raw.get("title") or "").strip()
    if not name:
        log.warning("Skipping product with no title (id=%s)", raw.get("id"))
        return None

    variants = raw.get("variants") or []
    first_variant = variants[0] if variants else {}

    price_str = (first_variant.get("price") or "").strip()
    if not price_str:
        log.warning("Skipping product with no price: %s", name)
        return None

    try:
        price = float(price_str)
    except ValueError:
        log.warning("Unparseable price %r for product: %s", price_str, name)
        return None

    sku = (first_variant.get("sku") or "").strip() or None
    body_html = raw.get("body_html") or ""
    description = strip_html(body_html)
    manufacturer = (raw.get("vendor") or "").strip() or None
    category = (raw.get("product_type") or "").strip() or collection_slug
    handle = (raw.get("handle") or "").strip()
    source_url = f"{source_base_url}/products/{handle}" if handle else None
    if not source_url:
        log.warning("Skipping product with no handle: %s", name)
        return None

    variant_title = (first_variant.get("title") or "").strip()
    # Shopify uses "Default Title" when there is only one variant with no options.
    if variant_title.lower() == "default title":
        variant_title = ""
    pack_size, uom = parse_pack_size_uom(variant_title)

    # If pack_size contains no digit, the variant title is a color/style label
    # (e.g. "Slate-Blue", "Alabaster"), not a quantity. Discard it entirely so
    # we don't store color names in raw_pack_info.
    if pack_size and not re.search(r"\d", pack_size):
        pack_size, uom = "", None

    content_hash = compute_content_hash(
        name, description, price_str, pack_size or "", category, manufacturer or ""
    )

    # Pack info string: combine pack_size and uom for the raw_pack_info field.
    pack_info = " ".join(filter(None, [pack_size, uom])) or None

    return {
        # Columns match the real source_items schema.
        "source": "medidepot",
        "source_url": source_url,
        "source_sku": sku,
        "raw_name": name,
        "raw_description": description or None,
        "raw_price": price_str,
        "raw_pack_info": pack_info,
        "raw_category": category or None,
        "raw_manufacturer": manufacturer,
        "content_hash": content_hash,
    }
