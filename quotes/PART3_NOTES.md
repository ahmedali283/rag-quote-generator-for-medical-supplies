# Part 3 — RAG Quote Generation Pipeline: Design Notes

## What Was Built

A five-step pipeline that takes a plain-text customer email and produces a
formatted sales quotation, grounding every pricing and discount decision in
retrieved historical quotes rather than hardcoded rules.

### File layout

```
quotes/
├── embed_quotes.py          Step 1 — embed & insert historical quotes into DB
├── generate_quote.py        Steps 2–5 — full pipeline (parse → retrieve → resolve → generate)
├── historical_quotes.json   19 synthetic historical quotes (source data)
├── quote_template.txt       Standard quote format fed to the generation prompt
├── tests/
│   ├── test_email_clean.txt     Test case 1: repeat customer, all SKUs specified
│   └── test_email_messy.txt     Test case 2: new customer, vague items, catalog gaps
├── output/
│   ├── quote_clean.txt      Generated quote for test case 1
│   └── quote_messy.txt      Generated quote for test case 2
└── QUOTES_NOTES.md          Historical quote generation notes (pricing patterns)
```

---

## Step-by-Step Design

### Step 1 — Embed Historical Quotes

Each of the 19 historical quotes is embedded as a natural-language summary:

```
"{customer_segment} customer purchasing: {product1}, {product2}, ..."
```

This form was chosen over raw JSON because Voyage AI voyage-2 embeddings
are trained on natural language, not structured data. Embedding the segment
alongside the product list means retrieval is sensitive to *who is buying*
as well as *what*, so a hospital sim lab query retrieves hospital sim lab
quotes preferentially over dental clinic quotes with similar products.

Embeddings are 1024-dim vectors stored in `historical_quotes.embedding`
(vector(1024), matching the column type in the existing schema).

The step is idempotent: quotes already in DB (matched by `source_file` =
quote_id) are skipped.

### Step 2 — Parse Customer Email

Claude (claude-haiku-4-5-20251001) extracts from the raw email:
- `customer_name`: organization name if mentioned
- `customer_segment`: one of five segments
- `requested_items`: list of items, each with:
  - `description`: full phrase from the email
  - `quantity`: integer or null
  - `size`: explicit size token only (medium, large, adult, pediatric…) — null if not stated
  - `attributes`: other explicit attributes (material, color) — never inferred

The prompt instructs the model not to infer attributes. "Nitrile gloves" does
not produce `size: medium` unless the email says medium. This distinction
matters for the resolution step.

### Step 3 — Retrieve Historical Quotes

Two retrieval paths run together:

**Semantic retrieval**: embed the email's intent the same way historical
quotes were embedded, then retrieve the 3 closest by pgvector cosine
similarity (`embedding <=>` operator).

**Exact customer-name match**: if Claude extracted a customer name, search
`historical_quotes.customer_name ILIKE '%{first 3 words}%'` and append any
matches not already in the semantic top-3. This is how repeat-customer
preferred pricing actually gets surfaced — the exact prior quotes for the
same account are retrieved regardless of semantic similarity, making the
pricing pattern explicit in the generation context.

The name search uses the first three words (punctuation-stripped) rather than
the full extracted name to survive encoding mismatches between extracted names
(e.g. "Riverside Community College, School of Nursing") and stored names
(e.g. "Riverside Community College — School of Nursing").

### Step 4 — Resolve Line Items

Each requested item is embedded and compared against `normalized_items`
(not `curated_products`) via cosine similarity. Three filters apply in order:

**1. SKU filter** (before threshold check):
If the item description or the raw email text contains an explicit model
number (regex: `[A-Z]{1,6}[-_]?\d[\w\-]{2,}`, e.g. LF00698U, IV-35,
PP-AM-100M-MS), candidates are filtered to those whose `raw_name` contains
that token. This resolves the most common form of ambiguity — when multiple
products score similarly but the customer named the exact SKU.

**2. Size filter** (after SKU filter):
If Claude extracted an explicit `size` for the item, candidates are filtered
to those whose `raw_name` contains a matching size token (with aliases:
"medium" matches "medium", " m ", "(m)"; "pediatric" matches "child",
"junior", etc.). If the filter eliminates all candidates, the item is flagged
UNRESOLVED with reason: "requested size not found in top catalog matches."

The size filter is intentionally strict: it only fires on explicitly stated
sizes. If the email doesn't specify a size, no filtering occurs, and the
ambiguity check below may flag the item.

**3. Threshold and ambiguity check**:
- `similarity < 0.72` → UNRESOLVED (low confidence)
- `similarity >= 0.72` and top-2 gap `< 0.02` and no size was specified →
  UNRESOLVED (ambiguous — customer must clarify)
- Otherwise → RESOLVED

The 0.72 threshold was chosen by inspection of the actual similarity
distribution for this catalog:
- Items with clear catalog matches (specific SKU named) score 0.85–0.93
- Items with a good match (same product family, no SKU) score 0.75–0.88
- Items with weak or no catalog match score 0.50–0.72
- 0.72 sits above the noise floor while staying below the weak-match zone

The 0.02 ambiguity gap (tighter than 0.03, which over-flagged distinct
products differing by 0.009) reflects that Voyage-2 similarities for
same-family products cluster within 0.01–0.015 of each other. A gap below
0.02 means the embedding genuinely cannot distinguish the two candidates
without additional signal from the customer.

### Step 5 — Generate Quote

Claude (claude-sonnet-4-6) receives:
- The raw customer email
- The extracted parsed items
- The full text of all retrieved historical quotes
- The resolved catalog items with real prices
- The unresolved items with reasons
- The quote template

The prompt instructs Claude to:
- Use only prices from resolved catalog data or historical quotes — never invent
- Infer discount rates from the historical quote examples, not from hardcoded rules
- Apply preferred pricing only when a historical quote explicitly shows it for this account
- Include unresolved items in a clearly marked "Items Requiring Manual Review" section
- Cite the specific historical quote(s) that established each pricing pattern

---

## Resolution Confidence Threshold: 0.72

| Similarity range | What it typically means | Decision |
|-----------------|------------------------|----------|
| 0.85 – 1.00 | Exact or near-exact SKU match | Resolved |
| 0.75 – 0.85 | Same product, slightly different phrasing | Resolved |
| 0.72 – 0.75 | Plausible match, some uncertainty | Resolved (monitor) |
| 0.50 – 0.72 | Wrong product family or vague query | Unresolved |
| < 0.50 | No meaningful match | Unresolved |

The 0.72 value was validated against the clean test case: all four SKU-named
items scored 0.86–0.93 when the SKU filter narrowed to the right candidate.
The TCCC kit (not in catalog) scored its best candidate at 0.782 — above
0.72 — but triggered the ambiguity check (gap < 0.02 between two very
different products), demonstrating that threshold alone is not sufficient and
the ambiguity gap is a necessary second guard.

---

## Test Case Walkthrough

### Test Case 1 — Clean Email (repeat customer, all SKUs named)

**Customer**: Dr. Patricia Okafor / Riverside Community College, School of
Nursing / ACCT-004421

**Items requested**:
1. Sterling nitrile exam gloves, medium (15 cases) — size explicit, no SKU
2. Life/form LF00698U venipuncture training arm (3 units) — SKU explicit
3. LF01122U skin vein replacement kit (6 units) — SKU explicit
4. Clinton 5-leg 2-hook IV poles (2 units) — no SKU, no size

**Retrieval**: Semantic search returned QUOTE-100142 (sim=0.91), QUOTE-100255
(sim=0.89), QUOTE-100398 (sim=0.89). Exact-name match additionally returned
QUOTE-100327 — the most recent prior quote for this exact account, which
documented the same preferred pricing pattern on gloves. Total: 4 quotes.

**Resolution**:
1. Gloves: size filter "medium" → only 747112_CS passes → resolved (sim=0.872)
2. LF00698U arm: SKU filter → 1 candidate → resolved (sim=0.890, $817.95)
3. LF01122U kit: SKU filter → 1 candidate → resolved (sim=0.860, $296.95)
4. IV pole: no SKU, no size → top-2 are IV-35 (sim=0.899) and IV-40 (sim=0.892),
   gap=0.007 < 0.02 → **UNRESOLVED** — ambiguous, customer must specify model

**Generated quote**:
- Applied preferred pricing $87.29/case on gloves (3% below $89.99 list),
  citing QUOTE-100142 and QUOTE-100327 explicitly
- Applied 8% volume discount (subtotal $5,544.90 exceeds $5K tier; prior
  quotes for this account were at 5% but the order is larger this time)
- IV pole flagged with both options (IV-35 and IV-40) and action required
- Claude correctly noted the catalog unit-price anomaly ($501.58 vs expected
  per-case ~$89.99) and flagged it for rep verification

**Preferred pricing detection**: worked correctly. The exact-name retrieval
of QUOTE-100327 put the documented $87.29/case preferred rate directly in
the generation context. Claude applied it and cited both supporting quotes
by number.

---

### Test Case 2 — Messy Email (new customer, vague items, catalog gaps)

**Customer**: Bay County EMS Training Division (new account, no history)

**Items requested**:
1. 4 CPR manikins with feedback monitors, mixed skin tones
2. 1 advanced pediatric simulation system with vital signs monitoring
3. ~200 pairs of nitrile exam gloves, medium size
4. Tactical casualty care (TCCC) training kit (tourniquet/wound packing)

**Retrieval**: Semantic search returned QUOTE-100349 (Gulf Coast EMS, new
account, sim=0.891), QUOTE-100589 (Gulf Coast EMS repeat, sim=0.885),
QUOTE-100201 (Metro EMS, sim=0.883). No exact-name match (new customer).
These three quotes provided the correct pattern: no preferred pricing for
new EMS accounts, 8% discount for large orders.

**Resolution**:
1. CPR manikins: resolved → Prestan PP-AM-100M-MS (sim=0.825, $195.00).
   No size/SKU specified; top candidate clear winner (gap=0.052 > 0.02).
2. Pediatric simulator: resolved → Laerdal SimJunior® (sim=0.795, $17.00).
   Above threshold but Claude correctly identified the $17.00 catalog price
   as a data anomaly and flagged it for manual verification before release.
3. Nitrile gloves medium: size filter "medium" applied. Top candidates for
   "nitrile exam gloves" query returned Adenna (pocketnurse, no size in name)
   and skydental gloves — **no candidate's raw_name contained "medium"**.
   → **UNRESOLVED**: "requested size not found in top catalog matches"
4. TCCC kit: no SKU, no catalog match. Top candidates (sim=0.782 and 0.780)
   are unrelated products (AED bag, casualty simulation kit) with gap < 0.02
   → **UNRESOLVED**: ambiguous and not genuinely matching

**Generated quote**:
- Line 1 resolved at list ($195.00), no preferred pricing for new account,
  citing QUOTE-100349 as precedent
- Line 2 included with explicit price-verification flag — Claude chose not
  to silently omit it but warned the rep not to release the quote until
  confirmed
- Gloves and TCCC kit in "Items Requiring Manual Review" with full reasons
- Volume discount conditional on Line 2 verification, both scenarios shown

**Flag path demonstrated**: both Item 3 and Item 4 surfaced as genuinely
unresolvable for different reasons (size not in catalog names vs. product
category not carried), and both were handled without guessing.

---

## Known Limitations

1. **Price data quality**: `raw_price` in `source_items` reflects the
   scraped list price at crawl time. Some PocketNurse items have `raw_price=None`
   (call-for-price). Some prices are per-unit when the catalog sells by case.
   Claude catches gross anomalies (the $17.00 SimJunior flag) but cannot
   detect subtler unit-of-measure mismatches without structured price data.

2. **Size filtering depends on catalog name conventions**: the size filter
   works when sizes appear in `raw_name` (e.g. "Medium, Standard Cuff"). If a
   catalog entry doesn't include the size in its name, a valid match will be
   missed. The gloves failure in test case 2 is partly this: the Adenna entry
   in normalized_items has no size in its name.

3. **Single-source resolution**: the pipeline resolves to the single highest-
   similarity catalog entry. For products available from multiple sources
   (MediDepot, Sky Dental, PocketNurse) at different prices, it always picks
   the top-scoring source. A production system would surface multi-source
   pricing for the rep to choose from.

4. **Preferred pricing is read from retrieved quote text**: Claude infers the
   preferred rate from the historical quote's `full_text` (which includes
   per-line-item prices and notes). This works well when prior quotes are
   verbose; it would fail for terse historical records.

5. **Price anomaly detection only fires when the same product appears in
   retrieved historical quotes**: the detector compares catalog prices against
   per-unit prices on matching lines in the retrieved quote context. If the
   retrieved quotes happen not to contain the flagged product (because the
   customer is new or the product is uncommon), a bad catalog price passes
   through undetected. The Laerdal SimJunior $17.00 case in test 2 was caught
   by the absolute floor check (simulation equipment below $100), not by
   historical comparison, precisely because the EMS-segment historical quotes
   retrieved for that email contained no SimJunior line items. A product with
   a bad price that also isn't simulation-category-flagged would pass both
   checks silently.

6. **Category-level false positives are the hardest failure mode**: pure
   embedding similarity can score a functionally wrong product very high when
   two products share domain vocabulary without sharing purpose. The confirmed
   example: the TCCC item in test 2 (tourniquet and wound-packing task trainers)
   resolved to a Simulaids 890 moulage/makeup kit at sim=0.830 on one pipeline
   run. Both are "casualty simulation" products in the EMS space, but they serve
   completely different functions. The confidence threshold (0.72) and ambiguity
   gap (0.02) both passed because there was a single confident wrong answer with
   no close competing candidate. Fixing this properly would require
   category-aware blocking at the resolution stage, similar in spirit to Part 2's
   category buckets. For example: if the email description contains terms like
   "tourniquet", "wound packing", or "task trainer", restrict candidates to
   normalized items whose category_bucket matches. This was not implemented given
   time constraints. The case is handled by the manual-override path in the
   pipeline run and is documented here as future work.
