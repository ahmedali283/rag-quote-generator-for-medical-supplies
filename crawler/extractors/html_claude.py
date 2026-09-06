"""
Claude API-based structure discovery for HTML product pages.

Why Claude for structure discovery?
------------------------------------
HTML product pages from Sky Dental and Anatomy Warehouse have no public
API. Rather than hard-coding CSS selectors that break when the site
redesigns, we ask Claude to identify extraction patterns from a sample page.
The discovered schema is cached on the filesystem so Claude is only called
once per source (or when the site changes and extraction starts failing).

Schema cache
------------
Stored as cache/<source>/__schema__.json using the same atomic-write
mechanism as all other cache entries. The synthetic URL key "__schema__"
produces a stable filename via sha256. Delete this file to force
re-discovery, or pass --force-rediscover on the CLI.

Failure threshold
-----------------
If more than FAILURE_THRESHOLD of pages in a run fail extraction, we
automatically re-invoke Claude with fresh HTML. This detects site redesigns
without requiring manual intervention. The counter resets after each
re-discovery to avoid infinite rediscovery loops.

Claude API errors
-----------------
If the API is unavailable or returns unparseable JSON, we fall back to
DEFAULT_SCHEMA (all selectors None) and log a CRITICAL error. The crawl
continues — html_parser.py's heuristic fallbacks handle the None selectors.
The ANTHROPIC_API_KEY env var must be set; a missing key raises immediately
at import time if we try to call the API, so it is checked lazily.
"""

import json
import logging
import os
from typing import Optional

from crawler import cache as cache_module

log = logging.getLogger(__name__)

# Schema cache uses a synthetic URL key so cache.py's path logic applies.
_SCHEMA_CACHE_URL = "__schema__"
_SCHEMA_CACHE_EXT = "json"

# Re-discover if more than this fraction of pages fail extraction.
FAILURE_THRESHOLD = 0.30

# Claude model to use. Pinned to current-generation Sonnet for cost/quality
# balance — schema discovery is a one-shot call per site per schema version.
CLAUDE_MODEL = "claude-sonnet-4-6"

# We truncate sample HTML to this many characters before sending to Claude.
# Enough to capture the structural patterns; keeps prompt tokens low.
HTML_SAMPLE_CHARS = 20_000

DEFAULT_SCHEMA: dict = {
    "name_selector": None,
    "price_selector": None,
    "sku_selector": None,
    "description_selector": None,
    "pack_size_selector": None,
    "uom_selector": None,
    "product_link_selector": None,
    "notes": "fallback default schema — selectors not discovered",
}

_DISCOVERY_PROMPT = """\
You are analyzing an e-commerce product page to identify reliable CSS selectors
for data extraction. The page sells medical or anatomical products.

HTML (truncated):
{html_sample}

Identify the most reliable CSS selectors for these fields on a PRODUCT page:
1. Product name / title
2. Price (the numeric price value, e.g. "$24.99")
3. SKU or product code
4. Description (main body text, not navigation)
5. Pack size / quantity (e.g. "100/Box", "Case of 12")
6. Unit of measure

And on a LISTING / CATEGORY page:
7. Anchor tags that link to individual product detail pages

Return ONLY a valid JSON object with exactly these keys:
{{
  "name_selector": "css_selector or null",
  "price_selector": "css_selector or null",
  "sku_selector": "css_selector or null",
  "description_selector": "css_selector or null",
  "pack_size_selector": "css_selector or null",
  "uom_selector": "css_selector or null",
  "product_link_selector": "css_selector or null",
  "notes": "one-sentence explanation of your extraction strategy"
}}

If you cannot determine a reliable selector for a field, use null.
Do not include any text outside the JSON object.
"""

_REPAIR_PROMPT = """\
Your previous response was not valid JSON. Return only the JSON object with
these exact keys and no other text:
name_selector, price_selector, sku_selector, description_selector,
pack_size_selector, uom_selector, product_link_selector, notes.
"""


def load_schema_from_cache(source: str) -> Optional[dict]:
    """Return the cached schema dict for `source`, or None if not cached."""
    raw = cache_module.get(source, _SCHEMA_CACHE_URL, _SCHEMA_CACHE_EXT)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("Corrupt schema cache for %s (%s) — treating as miss", source, exc)
        return None


def save_schema_to_cache(source: str, schema: dict) -> None:
    """Atomically write the schema dict to the filesystem cache."""
    cache_module.put(
        source,
        _SCHEMA_CACHE_URL,
        json.dumps(schema, indent=2).encode(),
        _SCHEMA_CACHE_EXT,
    )
    log.info("Schema for %s saved to cache", source)


def _call_claude(prompt: str) -> Optional[str]:
    """
    Call the Claude API and return the raw text response, or None on error.
    Imports anthropic lazily so the module loads without the package installed
    when Claude discovery is not used (e.g., MediDepot-only runs).
    """
    try:
        import anthropic
    except ImportError:
        log.critical(
            "anthropic package not installed. Run: pip install anthropic"
        )
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.critical(
            "ANTHROPIC_API_KEY env var not set — cannot discover schema"
        )
        return None

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text
    except Exception as exc:
        log.error("Claude API call failed: %s", exc)
        return None


def _parse_schema_response(text: str) -> Optional[dict]:
    """
    Parse and validate a JSON response from Claude.
    Returns the dict if all required keys are present, else None.
    """
    try:
        # Claude sometimes wraps JSON in a code fence — strip it.
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.split("\n")
            stripped = "\n".join(
                line for line in lines
                if not line.startswith("```")
            )
        schema = json.loads(stripped)
    except json.JSONDecodeError:
        return None

    required_keys = {
        "name_selector", "price_selector", "sku_selector",
        "description_selector", "pack_size_selector", "uom_selector",
        "product_link_selector", "notes",
    }
    missing = required_keys - set(schema.keys())
    if missing:
        log.warning("Claude schema missing keys: %s", missing)
        # Fill missing keys with None rather than rejecting the whole schema.
        for key in missing:
            schema[key] = None

    return schema


def discover_schema(source: str, sample_html: bytes, force: bool = False) -> dict:
    """
    Return the extraction schema for `source`.

    If a cached schema exists and `force` is False, return the cached version
    without calling Claude. Otherwise, call Claude with a truncated sample of
    the HTML, cache the result, and return it.

    On any error, returns DEFAULT_SCHEMA so the crawl can continue.
    """
    if not force:
        cached = load_schema_from_cache(source)
        if cached is not None:
            log.info("Using cached schema for %s: %s", source, cached.get("notes", ""))
            return cached

    log.info("Invoking Claude for schema discovery: source=%s", source)

    # Truncate HTML to reduce token usage.
    html_sample = sample_html.decode(errors="replace")[:HTML_SAMPLE_CHARS]
    prompt = _DISCOVERY_PROMPT.format(html_sample=html_sample)

    raw_response = _call_claude(prompt)
    if raw_response is None:
        log.critical("Claude discovery failed for %s — using DEFAULT_SCHEMA", source)
        return DEFAULT_SCHEMA

    schema = _parse_schema_response(raw_response)
    if schema is None:
        log.warning("Claude returned invalid JSON for %s — attempting repair", source)
        repair_response = _call_claude(_REPAIR_PROMPT)
        if repair_response:
            schema = _parse_schema_response(repair_response)

    if schema is None:
        log.critical(
            "Claude schema repair also failed for %s — using DEFAULT_SCHEMA", source
        )
        return DEFAULT_SCHEMA

    log.info("Claude discovered schema for %s: %s", source, schema.get("notes", ""))
    save_schema_to_cache(source, schema)
    return schema


def should_rediscover(failures: int, total: int) -> bool:
    """
    Return True if the extraction failure rate exceeds FAILURE_THRESHOLD
    and we have seen enough pages to make the rate meaningful.
    We require at least 10 attempts before triggering rediscovery to avoid
    false positives on the first few pages of a run.
    """
    if total < 10:
        return False
    return (failures / total) > FAILURE_THRESHOLD
