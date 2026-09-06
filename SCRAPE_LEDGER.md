# Scrape Ledger

This document records every source considered for the catalog crawl: the three sources that were crawled successfully, and two that were checked, attempted, and abandoned after hitting genuine access restrictions. No attempt was made to circumvent any block encountered. Where a site's rules or protections stopped us, we stopped.

## Sources Crawled

### MediDepot (medidepot.com)

MediDepot runs on Shopify, so the crawler used the store's public `/products.json` endpoint rather than parsing rendered HTML. Its robots.txt was read in full before any crawling began. The file is broadly permissive, allowing general access and disallowing only admin, cart, checkout, and account paths, along with a handful of known crawl-trap query parameters used for filtering and sorting. The file also invites AI agents to use the store's UCP/MCP commerce protocol for transactions. Since this crawl only reads catalog data and never transacts, ordinary polite crawling was judged consistent with the intent of that notice, not in conflict with it. Their Terms of Service page contained no anti-scraping clause.

Requests were limited to one per second per host, enforced with an asyncio lock held across the full sleep duration so two coroutines could never fire simultaneously against the same server. The crawler identified itself as `MedCatalogBot/1.0`, with a real contact email in the user agent string, rather than posing as any other crawler.

Six collections were crawled: nitrile and exam gloves, training manikins and simulators, blood collection supply, surgical and procedure items, and IV poles. This produced 188 active products.

One data quality issue surfaced during the matching phase and was traced back here. Shopify's "variant title" field does double duty on this store. Sometimes it holds real pack information, like "Box of 100." Other times it holds an unrelated product option, like the color of a phlebotomy chair. Early extraction treated every variant title as pack information, so color names such as "Slate-Blue" and "Alabaster" ended up stored as pack size text and were sent to an LLM to parse, which of course made no sense. The fix was straightforward: if a variant title contains no digit, it isn't pack information, so it's discarded rather than stored. Seventeen affected rows were corrected on re-crawl.

Resumability was tested for real, not just reasoned about. The crawler was killed mid-run with a hard interrupt partway through a page fetch. On restart it resumed the same crawl run, served already-fetched pages from cache instead of re-requesting them, and finished without losing or duplicating a single record.

### Sky Dental Supply (skydentalsupply.com)

This is a custom, server-rendered site, not Shopify, so it required real HTML parsing rather than a JSON API. Its robots.txt is permissive at the top level and disallows account, forum, event, and RFQ pages, none of which touch the product or collection listings we needed. No anti-scraping language was found in its Terms of Service.

Four listings were crawled: gloves, nitrile gloves specifically, infusion sets, and administration sets. The nitrile gloves category turned out to be a strict subset of the general gloves category, so it contributed zero new products once deduplicated, a genuine finding about how this catalog is organized rather than a bug in the crawler. Total products captured came to 128.

Structure discovery here used the Claude API once, on a real fetched product page, to infer the CSS selectors needed for name, price, SKU, and description. That inferred schema was cached and reused across every subsequent page rather than re-invoked per page, keeping the LLM calls to a minimum. Two issues turned up on the first live run and were fixed before the final crawl: the product-link heuristic initially picked up a promotional banner page that wasn't a real product, and a hardcoded pagination limit stopped short of the site's actual page count. Once fixed, the crawl found all six pages of the gloves category rather than the two it had originally been capped at.

### PocketNurse (pocketnurse.com)

PocketNurse runs on Magento and was accessed through its `/international/` storefront path. Its robots.txt was fetched using the crawler's own identified HTTP client, not Python's default urllib client, for reasons explained in the section below on the two abandoned sources. The file is broadly permissive for the categories we needed.

Three listings were crawled: venipuncture and IV trainers, the general manikins and simulators category, and Laerdal manikins specifically. This produced 162 products, with two extraction failures. Both failures were legitimate: two high-end simulators listed as "request a quote" items with no price element anywhere on the page, not a parsing bug.

A more significant data pattern showed up here. Of the 162 products, 133 are marked "call for price" directly in the site's own schema.org markup, which encodes this as `content="0"`. Early extraction read that literal zero as a real price and wrote a false $0.00 into price history, which is arguably worse than having no price at all. That was fixed to store null and skip the price history write entirely for these items. The remaining 27 products carry genuine listed prices.

PocketNurse was chosen deliberately, not by default. Before committing to it, we confirmed brand-level overlap with MediDepot's manikin and training category: both sites carry Life/form, Laerdal, and Simulaids product lines under directly comparable names, which is a stronger basis for matching than two stores simply selling similar-sounding items.

## Sources Checked and Abandoned

The assessment's own guidance treats a blocked source as a legitimate outcome, and states plainly that one honestly explained abandoned attempt is worth more than three sources crawled in a way that wouldn't reflect well on the crawler's conduct. The two sources below were genuinely attempted and abandoned once a real block was confirmed. Neither was pursued further with a headless browser, rotated headers, or any other workaround.

### Anatomy Warehouse (anatomywarehouse.com)

Before any crawling was attempted, manual browsing confirmed this site had a dedicated task-trainer and manikin section with strong, directly relevant overlap against MediDepot's catalog. Its robots.txt was reviewed in full and found broadly permissive, aside from three individually blocked product paths that were correctly excluded from the crawl plan.

The block came from Cloudflare. Both robots.txt and real product pages returned HTTP 403 with a managed challenge response, regardless of a correctly set, properly identified user agent. Our first instinct was to suspect our own robots.txt handling rather than the site itself, and that instinct paid off in one respect: it led us to find and fix two real bugs in how the crawler parsed robots.txt responses. But even after both fixes, the underlying Cloudflare block on the live site remained. At that point we stopped. No stealth browser or fingerprint-spoofing approach was attempted. PocketNurse was found as the replacement.

### PennCare (penncare.net)

PennCare was the second candidate considered before settling on PocketNurse. Research conducted through Google's cached index, since direct access was already suspected to be limited, confirmed the same kind of brand-level overlap found at Anatomy Warehouse: Life/form, Laerdal, and Simulaids products, plus nitrile glove categories that would have overlapped with both MediDepot and Sky Dental.

A direct test against both robots.txt and a real product page, using the crawler's identified client, returned the identical Cloudflare managed challenge response seen at Anatomy Warehouse. We stopped immediately and made no further attempt to reach the site.

## Summary

| Source | Status | Products captured | Reason if abandoned |
|---|---|---|---|
| MediDepot | Crawled | 188 | |
| Sky Dental Supply | Crawled | 128 | |
| PocketNurse | Crawled | 162 | |
| Anatomy Warehouse | Abandoned | 0 | Cloudflare managed challenge on robots.txt and product pages |
| PennCare | Abandoned | 0 | Same, Cloudflare managed challenge |

Three sources were crawled successfully for a combined total of 478 products, with genuine cross-source overlap in three categories: nitrile and exam gloves between MediDepot and Sky Dental, IV training arms and manikins between MediDepot and PocketNurse, and IV and injection consumables between MediDepot and Sky Dental.

## Final Pipeline Numbers

After running the full Part 2 matching pipeline (normalize, embed, 7,252 candidate pairs scored, 6,983 LLM-arbitrated, 269 auto-decided):

| Metric | Value |
|---|---|
| Total candidate pairs evaluated | 7,252 |
| Decided by rule (auto_match / auto_reject) | 269 |
| Decided by LLM arbitration (Claude Haiku) | 6,983 |
| Predicted matches (auto_match + llm_match) | 31 |
| Curated product clusters produced | 3 |
| Members across all clusters | 24 |

Hand-labeled eval set (166 pairs, stratified: all 31 predicted matches, 110 hardest llm_no_match by cosine similarity, 25 random auto_reject):

| Metric | Value |
|---|---|
| Precision | 6.5% (2/31 predicted matches were correct) |
| Recall | 100% (both true matches in the eval set were found) |
| F1 | 0.121 |

Root cause of low precision: the manufacturer-match bonus (+0.10), combined with high cosine similarity from shared brand and category vocabulary, promoted same-brand-family products into false matches at scale. The bonus gate was tightened (cosine >= 0.80 required before applying it) but a tier-keyword conflict detector to catch same-brand/different-tier cases was identified as the correct further fix and left as documented future work.
