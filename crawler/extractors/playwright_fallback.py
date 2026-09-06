"""
Playwright-based fallback fetcher for JavaScript-rendered pages.

This module is invoked only when plain-HTTP HTML is missing expected product
data — either because the price/name patterns are absent or because the
response contains bot-detection markers (Cloudflare "Just a moment...", etc.).

Every invocation is logged at WARNING level because frequent triggers are a
meaningful signal: they indicate the site relies on JavaScript rendering and
the plain-HTTP strategy is the wrong approach for that source. This should
be reported in the assessment write-up.

Playwright is imported lazily so the module can be imported without the
package installed. Install via:
    pip install playwright
    playwright install chromium

The fallback does NOT write to the cache — the caller (main.py orchestrator)
receives the HTML and decides whether to cache it. This keeps the fallback
function pure (no side effects) and makes testing easier.
"""

import asyncio
import logging
from typing import Optional

log = logging.getLogger(__name__)

PLAYWRIGHT_TIMEOUT = 30_000  # milliseconds

# Strings that indicate a bot-detection or lazy-loaded page when found in
# plain-HTTP HTML. The first two are Cloudflare challenge markers;
# the third catches generic empty-shell SPAs.
_BOT_DETECTION_MARKERS = [
    "Just a moment...",
    "cf-browser-verification",
    "Enable JavaScript and cookies to continue",
]

# Minimum expected response length for a real product page.
_MIN_CONTENT_LENGTH = 1_000  # bytes


async def fetch_with_playwright(url: str, source: str) -> Optional[bytes]:
    """
    Launch a headless Chromium instance, navigate to `url`, wait for the
    network to settle, and return the rendered page HTML as bytes.

    Logs a WARNING on every call — frequent triggers indicate the source
    needs a JS-first fetch strategy rather than plain HTTP.

    Returns None on timeout or navigation error.
    """
    log.warning("PLAYWRIGHT FALLBACK triggered for %s (source=%s)", url, source)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.error(
            "playwright package not installed — cannot use fallback. "
            "Run: pip install playwright && playwright install chromium"
        )
        return None

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()

            # Set a realistic User-Agent so the page renders properly.
            # This is distinct from our bot User-Agent — Playwright is used
            # only when the site refuses plain-HTTP access, so we use a
            # browser UA to get renderable HTML. We are transparent about
            # our identity in robots.txt (MedCatalogBot) but Playwright is
            # the tool of last resort for JS-gated content.
            await page.set_extra_http_headers({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            })

            try:
                await page.goto(url, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT)
            except Exception as exc:
                log.error("Playwright navigation failed for %s: %s", url, exc)
                await browser.close()
                return None

            html = await page.content()
            await browser.close()
            return html.encode()

    except Exception as exc:
        log.error("Playwright session failed for %s: %s", url, exc)
        return None


def needs_playwright_fallback(html: bytes, schema: dict) -> bool:
    """
    Return True if the plain-HTTP response looks like it needs JS rendering.

    Checks:
    1. Response is suspiciously short (likely an empty shell or redirect page).
    2. Contains known bot-detection/lazy-load markers.
    3. Extraction would yield no name AND no price (tried inline to avoid
       importing html_parser here — use the schema selectors directly).

    Keeps this function import-free of html_parser to avoid circular imports.
    """
    if len(html) < _MIN_CONTENT_LENGTH:
        log.debug("Response too short (%d bytes) — flagging for Playwright", len(html))
        return True

    text = html.decode(errors="replace")

    for marker in _BOT_DETECTION_MARKERS:
        if marker in text:
            log.debug("Bot detection marker %r found — flagging for Playwright", marker)
            return True

    # Quick schema-based check: if we have selectors and neither matches,
    # it is likely a JS-rendered page.
    name_sel = schema.get("name_selector")
    price_sel = schema.get("price_selector")

    if name_sel or price_sel:
        try:
            from bs4 import BeautifulSoup  # noqa: PLC0415
            soup = BeautifulSoup(html, "html.parser")
            has_name = bool(name_sel and soup.select_one(name_sel))
            has_price = bool(price_sel and soup.select_one(price_sel))
            if not has_name and not has_price:
                log.debug("Neither name nor price selector matched — flagging for Playwright")
                return True
        except Exception:
            pass

    return False
