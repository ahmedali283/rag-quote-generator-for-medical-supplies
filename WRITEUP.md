# Write-Up

## How you chose your sources and decided what was in bounds to crawl

MediDepot became the hub source. It runs on Shopify, so it was the easiest to crawl cleanly through the store's public `/products.json` endpoint, and it had the broadest range of anything I looked at. Sky Dental Supply and PocketNurse were picked for a specific reason, not just because they sell similar things. Life/form, Laerdal, and Simulaids products show up across MediDepot and PocketNurse in manikins and IV trainers, and gloves plus IV consumables overlap between MediDepot and Sky Dental. That's brand-level overlap, not just category overlap, which matters a lot more for Part 2.

Two other candidates, Anatomy Warehouse and PennCare, got tried and dropped. Both returned Cloudflare 403s on robots.txt and on real product pages, even with a correctly identified custom user agent. I didn't try to get around either block. Full detail is in SCRAPE_LEDGER.md.

## Where you used an agent versus hand-written code, and what you would change

Claude did the parts that don't reduce to a clean rule: pack-size and UOM parsing when regex couldn't confidently read the text (26 of 478 items), matching arbitration on genuinely ambiguous product pairs in Part 2 (6,983 pairs, run on Haiku for cost), and email parsing plus quote generation in Part 3. Hand-written code did rate limiting, caching, database work, the Shopify extraction, and the scoring formula.

Pagination and telling a listing page apart from a product page were also hand-coded, not agent-discovered. The patterns were simple and stable enough that a fixed rule felt more reliable than an LLM call on every page. What I'd change: the arbitration prompt originally didn't include the same conflict flags the rule-based scorer used, so Claude could override a signal it never even saw. Fixed now, but I'd build that consistency in from the start next time.

## Matching strategy, precision and recall, and what changes at 750,000 items

The pipeline scores pairs by cosine similarity on Voyage embeddings, blocked by category, then adjusted with bonuses and penalties (+0.10 for a manufacturer match, -0.30 for a UOM conflict, -0.40 for an attribute conflict), with Claude Haiku arbitrating anything between 0.50 and 0.85. I hand-labeled 166 pairs. Precision came out to 6.5 percent, 2 correct out of 31 predicted matches. Recall was 100 percent.

The cause is clear. The manufacturer bonus, combined with high similarity from shared brand and category words, was pulling same-brand-family products together that aren't actually the same product, a training arm and its own replacement kit, for instance. A tier-keyword conflict detector would likely fix most of this. I scoped it and wrote it down but didn't build it, since re-running the full arbitration pass again under deadline wasn't worth the time. At 750,000 items, blocking stops being optional, and getting the category buckets right matters far more, since the same bucketing mistake turns into an unmanageable number of LLM calls at that scale.

## How you prevent the quote generator from hallucinating products or prices

Unresolved items are never quoted, no exceptions. A price-anomaly check runs in plain code before Claude ever sees the item, comparing the resolved catalog price against real historical prices for the same product. This exists because of a bug I found during testing: an earlier version let Claude invent a "corrected" price when it judged the catalog price suspicious, so the check now happens upstream instead. Every discount has to cite a specific historical quote by number. Preferred pricing only applies when a retrieved quote explicitly documents it for that account, never guessed from customer type alone. One gap remains: a bad price with no historical quote to compare against still slips through.

## How you would monitor this crawl in production

The most likely first break is the cached CSS-selector schema for the two HTML sources, once a site redesigns its markup. Failures would climb gradually rather than crash the run outright. There's already a self-healing step, automatic schema re-discovery past a 30% failure rate, but in production I'd want an alert before that fires, since a schema learned from a half-broken page could get cached and quietly stay wrong. I'd also watch the cache-hit ratio on incremental runs. A site that suddenly stops changing at its normal pace is worth a look on its own, before any customer or sales rep would notice something's off.

## What you deliberately cut, and what you would build next

The tier-keyword conflict detector and a dedicated Part 2 review UI were both scoped but never built. Instead of a from-scratch interface, the hand-labeled CSV plus scoring script served as the actual review mechanism, and it's what produced the real numbers above. Part 3 resolves against `normalized_items` rather than the lower-precision `curated_products`, on purpose, to avoid carrying Part 2's matching errors into the quote pipeline. Next: build the tier-keyword detector, add category-aware blocking to Part 3's resolution step, and add a proper review UI.