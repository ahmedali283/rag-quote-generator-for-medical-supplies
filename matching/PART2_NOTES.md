# Part 2 — Matching Pipeline: Design Notes

## What Was Built

A three-source product deduplication pipeline that takes the `source_items`
table populated by the Part 1 crawler and produces a `curated_products` table
containing de-duplicated product clusters, with full audit logs of every match
decision.

### File layout

```
matching/
├── migration.sql       DDL for 4 new tables + pgvector indexes
├── normalize.py        Pack size / UOM parsing (regex-first, LLM fallback)
├── embed.py            Voyage AI embedding generation
├── matcher.py          Blocking, scoring, LLM arbitration, cluster building
├── build_eval_set.py   Candidate pair CSV export for hand labeling
├── score_eval.py       Precision/recall metrics from labeled CSV
└── main.py             CLI tying all steps together
```

---

## Step 1 — Pack Size / UOM Normalization

### The key semantic distinction

Two raw strings that look similar must produce clearly different normalized
values:

| Input             | pack_size | pack_unit | units_per_pack | unit_of_measure |
|-------------------|-----------|-----------|----------------|-----------------|
| "box of 100"      | 100       | each      | 1              | box             |
| "case of 10 boxes"| 10        | box       | 10             | case            |

`pack_size` = individual unit count inside the thing you actually purchase.
For "case of 10 boxes", the thing you purchase *is* a case; the sub-units
are boxes; each box contains an unknown number of individual items. We store
the sub-unit count (10 boxes) as pack_size and record the sub-unit type
(box) as pack_unit, with units_per_pack=10 signaling the intermediate layer.

This distinction matters for the UOM conflict penalty in Step 4: if item A
is "100/Box" (pack_size=100, uom=box) and item B is "pair" (pack_size=2,
uom=pair), they are not the same purchasable line item.

### Regex-first ordering

11 patterns are evaluated in specificity order — two-level container patterns
before single-level, explicit "pack of N" before the generic N-then-container
pattern — to avoid ambiguous overlaps.

### LLM fallback auditability

Every item that required the Claude API is logged at WARNING level with its
raw_pack_info string. The `parse_method` column on `normalized_items`
('regex' or 'llm') makes the audit trail queryable:

```sql
SELECT parse_method, COUNT(*) FROM normalized_items GROUP BY parse_method;
```

---

## Step 2 — Embeddings

**Model:** voyage-2 (1024 dims). Matches the `historical_quotes.embedding`
column type for schema consistency.

**Input string:** `"{raw_name} | {raw_category} | {raw_manufacturer}"`

Empty fields are omitted entirely (not padded with empty strings), so the
embedding captures only the signal that actually exists for each item. The
" | " separator preserves field boundaries without making them appear as a
single run-on phrase.

**Batch size:** 128 (Voyage AI's per-call limit). Processing is batched and
committed after each batch, so a crash mid-run can be resumed without
re-embedding already-stored rows (the query filters WHERE `embedding IS NULL`).

---

## Step 3 — Candidate Pair Generation (Blocking)

### Why blocking matters

With ~500 items across three sources, a full cross-source comparison would
produce ~83,000 pairs (500 × 500 / 3 sources). Most pairs are obviously
unrelated. Blocking reduces this to a tractable set by only comparing items
in the same category bucket.

### Category buckets

Raw `raw_category` values (Shopify collection slugs, HTML path segments) are
mapped to four coarse buckets:

| Bucket      | Sources                                                          |
|-------------|------------------------------------------------------------------|
| gloves      | medidepot: nitrile-exam-gloves, gloves; skydental: gloves, gloves-nitrile |
| simulators  | medidepot: medical-training-manikins-simulators; pocketnurse: manikins-simulators, laerdal-manikins, inj-venipuncture-trainers |
| iv_supplies | medidepot: iv-poles; skydental: infusion-set, administration-sets |
| other       | Everything else (excluded from blocking)                         |

Items in the `other` bucket are not compared at all — they have no
counterparts in the other sources. This keeps the blocking conservative and
avoids noise.

### Cosine similarity floor

The default blocking floor is **0.50**. At this threshold:
- Pairs below 0.50 are almost certainly unrelated (blocked entirely)
- Pairs above 0.50 but below 0.85 are sent to scoring and possibly Claude
- The `build_eval_set` step automatically lowers to **0.35** if the 0.50
  floor produces fewer than 150 pairs, to ensure enough rows for labeling

---

## Step 4 — Scoring Formula

### Formula

```
final_score = clamp(cosine_sim + Σ adjustments, 0.0, 1.0)
```

### Adjustments

**+0.10 manufacturer/brand bonus**
Fires when a known brand token (e.g., "Laerdal", "Life/form", "3B Scientific")
appears in both items' raw_name (or raw_manufacturer for MediDepot, which has
structured Shopify vendor data). Rewards explicit brand alignment: two items
that both say "Laerdal" are more likely to be the same product than two
generics.

*Why +0.10:* It's a confirming signal, not sufficient on its own. Even with
this bonus, a pair at cosine_sim=0.50 would only reach 0.60 — still below
auto_match, so it goes to Claude for a final check.

**-0.30 UOM conflict penalty**
Fires when one item is a pair/single-unit and the other is a bulk container
(box, case, pack) with pack_size > 1. A "pair of gloves" and a "box of 100
gloves" are never the same purchasable line item, regardless of how similar
their names are. This penalty is strong enough to push a borderline pair
(cosine_sim=0.75) below the auto_reject floor.

*Why -0.30:* Must be large enough to decisively reject these pairs without
requiring a -1.0 that would mask the underlying similarity signal. Pairs
knocked to 0.45 still appear in the eval set (useful negative examples).

**-0.40 explicit attribute conflict penalty**
Fires when BOTH items mention an attribute but incompatibly:
- Different sizes (S vs L, M vs XL): detects via size token regex
- Different materials (latex vs nitrile, nitrile vs vinyl)
- Sterile vs non-sterile

The rule: conflict only fires if BOTH items name the attribute. If only one
item mentions "nitrile" and the other doesn't, we can't conclude conflict —
the other item might also be nitrile but not labeled. If both say a material
and they differ, that is definitive.

*Why -0.40:* These are the most dangerous false merge candidates for medical
supplies (sterile vs non-sterile gloves are different products by regulation;
size-specific items should never be merged). The stronger penalty hard-blocks
these cases even for high cosine similarity.

### Three-way thresholds

| Threshold        | Decision    | Rationale                                               |
|-----------------|-------------|----------------------------------------------------------|
| score ≥ 0.85     | auto_match  | At this point, name + category + brand alignment is conclusive |
| score ≤ 0.50     | auto_reject | Below the blocking floor — if it cleared blocking, it's borderline, not obvious |
| 0.50 < score < 0.85 | pending_llm | Genuinely ambiguous — semantic similarity is suggestive but not conclusive |

*Why 0.85 for auto-match:* A cosine similarity of 0.85+ between Voyage-2
embeddings of product names is very high. When combined with potential brand
bonus (+0.10), the base cosine_sim needs to only be 0.75, which is still a
strong signal. Setting the threshold lower (e.g. 0.75) would auto-match many
true positives but also create false merges for similar-but-distinct products
(e.g. different sizes of the same product line).

*Why 0.50 for auto-reject:* This matches the blocking floor, so pairs at
exactly 0.50 are right at the boundary of "worth examining at all." After
penalties, a pair that drops to ≤0.50 has demonstrated a specific
incompatibility, not just low similarity.

### LLM arbitration (the agentic piece)

For the ambiguous tier (0.50 < score < 0.85), Claude is given both items'
full raw data:
- raw_name, raw_category, raw_manufacturer, raw_pack_info, raw_description

Claude is asked to return:
```json
{"decision": "match" or "no_match", "reason": "one concise sentence"}
```

The `reason` field is stored in `candidate_pairs.llm_reason` and written to
the decision log. This makes every LLM decision reviewable: an operator can
open `logs/match_decisions_*.jsonl` and read exactly why Claude decided each
ambiguous pair.

---

## Step 5 — Curated Clusters

### Union-find for transitivity

If A matches B and B matches C, then A, B, C should be one cluster even if
A and C were never directly compared. Union-find (with path compression) is
used to compute transitive closures in O(n·α(n)) time.

### Canonical item selection

Within each cluster, the canonical item is chosen by:
1. Source priority: medidepot > skydental > pocketnurse
   (medidepot has structured Shopify data including manufacturer; higher quality)
2. Within the same source: longer raw_name wins (more descriptive)

### Confidence storage

| Pair type        | Stored confidence            |
|-----------------|------------------------------|
| Canonical member | 1.000                        |
| auto_match       | computed_score from scoring  |
| llm_match        | 0.800 (fixed: human-level judgment applied) |
| Transitively connected | 0.750 (default)        |

---

## Step 6 — Eval Set

The CSV exported by `build_eval_set.py` contains one row per candidate pair
(all pairs above the similarity floor), including computed scores and a blank
`human_label` column. Fill in "match" or "no_match" for each row.

Column `predicted_label` is the pipeline's prediction:
- `match` = auto_match or llm_match
- `no_match` = auto_reject or llm_no_match
- `ambiguous` = pending_llm (not yet arbitrated when export ran)

---

## Step 7 — Scoring Script

After labeling:
```
python -m matching.score_eval --csv eval/candidate_pairs.csv
```

Outputs: precision, recall, F1, confusion matrix, per-tier breakdown (how
the pipeline performs within each decision tier separately), and score
distribution statistics for true matches vs true non-matches.

---

## Known Limitations

### True duplicates this approach would likely miss

1. **Different product names for the same item.** If MediDepot sells a
   "Nitrile Exam Glove" and Sky Dental sells a "Latex-Free Examination Glove",
   cosine similarity may be moderate (0.55-0.65) because the names use different
   words for the same concept. The approach depends on overlapping vocabulary in
   the embedding space — it is not a semantic equivalence oracle.

2. **OEM / private-label products.** A MediDepot house-brand glove and the
   equivalent Medline SKU may be identical products sold under different names
   with different catalog numbers. Without a shared brand token or SKU prefix,
   nothing signals they are the same.

3. **Multi-variant Shopify products.** Part 1 captures only the first variant.
   If MediDepot's "Nitrile Glove" captures size=M and Sky Dental's listing is
   size=L, the attribute conflict penalty (-0.40) correctly rejects them even
   though they are the same product family. Full variant support would require
   variant-level matching, which is out of scope.

4. **Call-for-price simulator products.** PocketNurse's 135 call-for-price
   items have `raw_price=None`. They still receive embeddings (price is not
   in the embedding input) and can match. However, since most high-end
   simulators are pocketnurse-only (no equivalent on medidepot/skydental at
   this price tier), they form singleton clusters rather than cross-source
   matches.

5. **Short product names.** Items with very short raw_name strings (e.g.,
   "IV Pole", "Gloves") produce lower-quality embeddings. Two different "IV
   Pole" products from different sources will have very high cosine similarity
   but might not be the same specific pole model.

### What would produce false merges

1. **Same product family, different specifications.** "Nitrile Exam Gloves -
   Medium" and "Nitrile Exam Gloves - Large" have almost identical names.
   The size conflict penalty (-0.40) protects against auto-match, but if
   neither item's name mentions the size, the penalty doesn't fire and Claude
   may decide "match" (since they are the same product family, just different
   sizes).

2. **High cosine similarity without the brand bonus.** Two generic "IV
   Administration Set" products from different manufacturers could hit 0.85+
   cosine similarity if the names are nearly identical, triggering auto_match.
   Since neither has a manufacturer field (HTML sources) and no brand token
   appears in the name, there's no penalty or bonus to modulate the score.
   The auto-match threshold (0.85) is set conservatively to reduce this, but
   it can't be eliminated without additional structured data.

3. **Transitive over-linking.** If A→B and B→C are both low-confidence
   (0.51) LLM matches, the union-find clusters A, B, C together. But the
   A→C similarity may be very low — they were never a direct candidate pair.
   This is the known "chaining" failure mode of single-linkage clustering.
   Mitigation: the 0.85 auto-match threshold and LLM arbitration reduce the
   probability of low-confidence matches entering the cluster set.

4. **Category bucket contamination.** If a product is miscategorized
   (e.g., a glove-related simulator ends up in the `gloves` bucket), it will
   be compared against all glove products. The embedding similarity will be
   low, but it creates noise in the candidate set.

5. **Same-brand cross-product false merge (observed in practice).** The
   manufacturer bonus (+0.10) fires whenever a brand token appears in both
   items, regardless of whether the products are functionally related. This
   was the root cause of the largest false-positive cluster found during
   evaluation.

   **Exact case:** `candidate_pairs.id = 6985`
   - Item A (source_item_id=59): *Life/form LF03602U Adult Airway Management
     Trainer Manikin Torso* — an intubation/CPR torso trainer
   - Item B (source_item_id=1624): *Nasco Life/form® Intraosseous Infusion
     Simulator, Adult or Infant* — a bone-injection simulator
   - `cosine_sim = 0.8123`, `manufacturer_bonus = +0.10`,
     `computed_score = 0.9123` → **auto_match**

   Both items share the "Life/form" brand token, and their embeddings are
   genuinely similar (both are medical training simulators from the same
   manufacturer family), but they are completely different clinical trainers.
   The brand bonus pushed a borderline-strong cosine similarity over the
   auto-match threshold without LLM review.

   **Fix attempted:** A cosine_sim ≥ 0.80 guard was added to the bonus
   (previously ≥ 0.75 was sufficient to trigger it). This eliminated 63
   clear false merges where cosine_sim was 0.75–0.79. Pair 6985 survived
   because its cosine_sim (0.81) exceeds the new guard — it is a harder
   case that would require a higher threshold (≥ 0.85, making the bonus
   redundant) or a sub-category signal (airway vs. IV/injection) to catch.

   **Why it was left as-is:** Manual correction of a single pair would
   improve the cluster output but obscure a real limitation of the scoring
   formula. The pair remains `auto_match` in the database; it surfaces as a
   false positive in the eval set and lowers precision by approximately one
   case, which is an honest reflection of the pipeline's current capability.

---

## Decisions That Are Auditable

Every match decision is recorded in `logs/match_decisions_*.jsonl`:
- Auto decisions: pair_id, tier='auto', computed_score, flags (manufacturer_bonus, uom_conflict, attribute_conflict), both item names
- LLM decisions: same fields plus `reason` (Claude's one-sentence explanation)

To review all auto-matches:
```bash
grep '"decision": "auto_match"' logs/match_decisions_*.jsonl | jq .
```

To review all LLM decisions that resulted in a match:
```bash
grep '"decision": "llm_match"' logs/match_decisions_*.jsonl | jq '{name_a, name_b, reason}'
```
