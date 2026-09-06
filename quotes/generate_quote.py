"""
Part 3 RAG quote generation pipeline.

Steps:
  2. Parse customer email → extract customer name + requested items via Claude
  3. Retrieve 3 most similar historical quotes via pgvector + exact customer name match
  4. Resolve each line item against normalized_items via embedding similarity
  5. Generate quote via Claude using retrieved context and template
  6. Flag unresolved items clearly in output

Usage:
  python quotes/generate_quote.py --email path/to/email.txt --out quotes/output/quote_out.txt
"""

import argparse
import json
import logging
import os
import re
import sys
import urllib.parse
from pathlib import Path
from urllib.parse import urlparse

import anthropic
import psycopg2
import psycopg2.extras
import voyageai
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

VOYAGE_MODEL = "voyage-2"
ITEM_RESOLUTION_THRESHOLD = 0.72   # cosine similarity floor for catalog match
SIZE_NARROWED_THRESHOLD = 0.65     # lower floor when size filter reduced candidates to exactly one
AMBIGUITY_GAP = 0.02               # if top-2 matches within this gap, flag as ambiguous
TEMPLATE_PATH = Path(__file__).parent / "quote_template.txt"

# Words that appear in product descriptions but carry no brand signal.
# Used by the brand-token check to distinguish meaningful name tokens from
# generic category vocabulary.
_GENERIC_TOKENS = {
    "gloves", "glove", "nitrile", "latex", "vinyl", "exam", "powder", "free",
    "box", "case", "pack", "trainer", "training", "arm", "arms", "kit", "set",
    "manikin", "mannequin", "simulator", "simulation", "adult", "medium",
    "large", "small", "white", "black", "gray", "grey", "blue", "and",
    "for", "the", "with", "size", "color", "pair", "pairs", "boxes",
    "replacement", "injection", "venipuncture", "supply", "supplies",
    "standard", "cuff", "advanced", "basic", "deluxe",
    "system", "unit", "units", "piece", "pieces", "count", "each",
}


def brand_tokens(text: str) -> set[str]:
    """
    Return the meaningful (non-generic) lowercase tokens from a name string.
    These are the tokens we can use to confirm a catalog result matches what
    the customer actually asked for by brand or model.
    """
    return {
        w.lower().strip("()[].,/") for w in text.split()
        if len(w) >= 3 and w.lower().strip("()[].,/") not in _GENERIC_TOKENS
    }


# ---------------------------------------------------------------------------
# Step 2 — Parse email
# ---------------------------------------------------------------------------

def parse_email(email_text: str, claude: anthropic.Anthropic) -> dict:
    """Extract customer identity and requested items from a raw email."""
    prompt = f"""You are parsing a customer email for a medical/dental supply company.
Extract the following and return ONLY valid JSON, no markdown fences:
{{
  "customer_name": "full name or organization name, or null if not mentioned",
  "customer_segment": "one of: nursing_school, hospital_sim_lab, ems_program, dental_clinic, clinical_skills_lab, unknown",
  "requested_items": [
    {{
      "description": "natural language item description (full phrase from email)",
      "quantity": integer or null,
      "size": "explicit size token if mentioned (e.g. 'small', 'medium', 'large', 'XL', 'adult', 'pediatric', 'infant'), or null if not specified",
      "attributes": ["list of other explicit attributes: material, color, style — only what the email actually states, never inferred"]
    }}
  ]
}}

Rules:
- Only populate 'size' if the email explicitly names a size. Do not infer.
- Only populate 'attributes' with things the email explicitly states (e.g. 'nitrile', 'gray', 'powder-free'). Do not infer.
- If the email is vague about what product is wanted, preserve that vagueness in 'description'.

Email:
{email_text}"""

    msg = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    # Strip markdown fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Step 3 — Retrieve historical quotes
# ---------------------------------------------------------------------------

def retrieve_similar_quotes(
    parsed: dict,
    email_text: str,
    conn,
    vc: voyageai.Client,
    k: int = 3,
) -> list[dict]:
    """
    Returns up to k+1 historical quotes:
    - Top-k by cosine similarity to the email embedding
    - Plus any exact customer-name match not already in the top-k
    """
    # Embed the email
    emb_input = f"{parsed['customer_segment'].replace('_', ' ')} customer purchasing: " + \
                ", ".join(it["description"] for it in parsed["requested_items"])
    result = vc.embed([emb_input], model=VOYAGE_MODEL, input_type="query")
    emb = result.embeddings[0]
    emb_str = "[" + ",".join(str(x) for x in emb) + "]"

    retrieved = []
    seen_ids = set()

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Semantic retrieval
        cur.execute(
            """
            SELECT id, source_file, customer_name, customer_segment, quote_date,
                   full_text, subtotal, discount, freight, total, notes,
                   1 - (embedding <=> %s::vector) AS similarity
            FROM historical_quotes
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (emb_str, emb_str, k),
        )
        for row in cur.fetchall():
            seen_ids.add(row["id"])
            retrieved.append({"match_type": "semantic", "similarity": float(row["similarity"]), **dict(row)})
            log.info("  semantic match: %s (sim=%.4f)", row["source_file"], row["similarity"])

        # Exact customer-name match — search on first meaningful word of org name
        # (avoids em-dash / punctuation encoding mismatches between extracted
        # name and stored name, e.g. "Riverside Community College, School of Nursing"
        # vs "Riverside Community College — School of Nursing")
        if parsed.get("customer_name"):
            # Use first 3 words, stripping trailing punctuation, as search fragment
            words = [w.strip(",'\"") for w in parsed["customer_name"].split()[:3]]
            name_fragment = " ".join(w for w in words if w)
            cur.execute(
                """
                SELECT id, source_file, customer_name, customer_segment, quote_date,
                       full_text, subtotal, discount, freight, total, notes,
                       1 - (embedding <=> %s::vector) AS similarity
                FROM historical_quotes
                WHERE customer_name ILIKE %s
                ORDER BY quote_date DESC
                LIMIT 3
                """,
                (emb_str, f"%{name_fragment}%"),
            )
            for row in cur.fetchall():
                if row["id"] not in seen_ids:
                    seen_ids.add(row["id"])
                    retrieved.append({"match_type": "exact_name", "similarity": float(row["similarity"]), **dict(row)})
                    log.info("  exact-name match: %s", row["source_file"])

    return retrieved


# ---------------------------------------------------------------------------
# Step 4 — Resolve line items against catalog
# ---------------------------------------------------------------------------

def _resolve_single_item(
    item: dict,
    emb: list[float],
    cur,
    email_text: str,
) -> dict:
    """
    Resolve one requested item against the catalog using a pre-computed embedding.
    Returns a single resolution dict (status resolved or unresolved).
    """
    emb_str = "[" + ",".join(str(x) for x in emb) + "]"

    cur.execute(
        """
        SELECT
            ni.source_item_id,
            si.raw_name,
            si.raw_manufacturer,
            si.raw_price,
            si.source,
            si.source_url,
            ni.category_bucket,
            1 - (ni.embedding <=> %s::vector) AS similarity
        FROM normalized_items ni
        JOIN source_items si ON si.id = ni.source_item_id
        WHERE ni.embedding IS NOT NULL
        ORDER BY ni.embedding <=> %s::vector
        LIMIT 5
        """,
        (emb_str, emb_str),
    )
    candidates = [dict(r) for r in cur.fetchall()]

    if not candidates:
        return {
            "status": "unresolved",
            "description": item["description"],
            "quantity": item.get("quantity"),
            "reason": "No products in catalog",
        }

    # --- SKU filter ---
    # If the description contains an explicit SKU/model number
    # (alphanumeric token with digits, e.g. LF00698U, IV-35, PP-AM-100M-MS),
    # filter candidates to those whose raw_name contains that token.
    # Search description, attributes list, AND the raw email — the parser
    # sometimes places the SKU in attributes rather than the description text,
    # and the raw email is the ground truth fallback for any case Claude drops.
    attrs_text = " ".join(item.get("attributes") or [])
    search_text = item["description"] + " " + attrs_text + " " + email_text
    sku_match = re.search(r'\b([A-Z]{1,6}[-_]?\d[\w\-]{2,})\b', search_text)
    if sku_match:
        sku_token = sku_match.group(1).upper()
        sku_filtered = [c for c in candidates if sku_token in c["raw_name"].upper()]
        if sku_filtered:
            log.info("  SKU filter '%s' → %d candidates", sku_token, len(sku_filtered))
            candidates = sku_filtered

    # --- Size filter ---
    # If the email explicitly specified a size, filter candidates to those
    # whose raw_name contains that size token (case-insensitive).
    # This prevents silently picking the wrong size when multiple sizes
    # score similarly.
    size = (item.get("size") or "").strip().lower()
    size_aliases = {
        "small": ["small", " s ", "s,", "(s)"],
        "medium": ["medium", " m ", "m,", "(m)"],
        "large": ["large", " l ", "l,", "(l)"],
        "xl": ["x-large", "xl", "x large", "extra large"],
        "adult": ["adult"],
        "pediatric": ["pediatric", "child", "junior"],
        "infant": ["infant", "neonatal", "newborn"],
    }
    size_tokens = size_aliases.get(size, [size]) if size else []

    size_actually_narrowed = False

    if size_tokens:
        size_filtered = [
            c for c in candidates
            if any(tok in c["raw_name"].lower() for tok in size_tokens)
        ]
        if not size_filtered:
            return {
                "status": "unresolved",
                "description": item["description"],
                "quantity": item.get("quantity"),
                "reason": (
                    f"Requested size '{size}' not found among top catalog matches. "
                    f"Closest match was '{candidates[0]['raw_name'][:60]}' (sim={float(candidates[0]['similarity']):.3f}). "
                    "Confirm correct size with customer or check catalog."
                ),
            }
        # The filter discriminates only when it reduces the list to a single
        # candidate. Removing one-of-five still leaves multiple close
        # competitors — the remaining gap must resolve ambiguity on its own.
        size_actually_narrowed = len(size_filtered) == 1
        candidates = size_filtered

    top = candidates[0]
    top_sim = float(top["similarity"])
    second_sim = float(candidates[1]["similarity"]) if len(candidates) > 1 else 0.0

    # --- Threshold check ---
    # When size filter narrowed to exactly one candidate, use a lower floor
    # (SIZE_NARROWED_THRESHOLD) because we already have a size discriminator.
    floor = SIZE_NARROWED_THRESHOLD if size_actually_narrowed else ITEM_RESOLUTION_THRESHOLD
    if top_sim < floor:
        return {
            "status": "unresolved",
            "description": item["description"],
            "quantity": item.get("quantity"),
            "reason": f"Best catalog match '{top['raw_name'][:60]}' has low confidence (similarity={top_sim:.3f}, threshold={floor})",
        }

    # --- Ambiguity check ---
    # top-2 candidates within AMBIGUITY_GAP means the
    # embedding can't distinguish them — ask the customer to clarify.
    # Skipped only when the size filter demonstrably narrowed the candidate
    # list, meaning the size token actually discriminated between options.
    # If size was present but every candidate matched it (e.g. 'adult'
    # appearing in all arm products), the filter changed nothing and
    # ambiguity must still be checked.
    if not size_actually_narrowed and len(candidates) > 1 and (top_sim - second_sim) < AMBIGUITY_GAP:
        return {
            "status": "unresolved",
            "description": item["description"],
            "quantity": item.get("quantity"),
            "reason": (
                f"Ambiguous: '{top['raw_name'][:55]}' (sim={top_sim:.3f}) "
                f"vs '{candidates[1]['raw_name'][:55]}' (sim={second_sim:.3f}) score "
                f"within {AMBIGUITY_GAP} — email does not specify which variant. "
                "Ask customer to clarify."
            ),
        }

    # --- Brand-token check ---
    # If the item description contains meaningful brand/model tokens, at least
    # one must appear in the matched product's name or manufacturer. Prevents
    # resolving "Adenna nitrile gloves" to Sterling (high cosine, same category,
    # wrong brand). Generic descriptions with no brand signal pass through.
    desc_tokens = brand_tokens(item["description"])
    if desc_tokens:
        candidate_text = (
            (top["raw_name"] or "") + " " + (top["raw_manufacturer"] or "")
        ).lower()
        if not any(tok in candidate_text for tok in desc_tokens):
            log.warning(
                "  brand mismatch: '%s' has tokens %s absent from '%s' — unresolved",
                item["description"][:40], desc_tokens, top["raw_name"][:50],
            )
            return {
                "status": "unresolved",
                "description": item["description"],
                "quantity": item.get("quantity"),
                "reason": (
                    f"Description contains brand/model signal {desc_tokens} "
                    f"not found in closest catalog match '{top['raw_name'][:60]}' "
                    f"(sim={top_sim:.3f}). Likely a wrong-brand result. "
                    "Ask customer to confirm brand or provide a model number."
                ),
            }

    log.info("  resolved '%s' → '%s' (sim=%.3f, price=$%s)",
             item["description"][:40], top["raw_name"][:50],
             top_sim, top["raw_price"])
    return {
        "status": "resolved",
        "description": item["description"],
        "quantity": item.get("quantity"),
        "match": {
            "source_item_id": top["source_item_id"],
            "name": top["raw_name"],
            "manufacturer": top["raw_manufacturer"],
            "price": float(top["raw_price"]) if top["raw_price"] else None,
            "source": top["source"],
            "source_url": top["source_url"],
            "similarity": top_sim,
        },
    }


def resolve_line_items(
    requested_items: list[dict],
    conn,
    vc: voyageai.Client,
    email_text: str = "",
) -> list[dict]:
    """
    For each requested item, find the best matching product in normalized_items.
    Returns a list of resolution results, each either:
      {"status": "resolved", "description": ..., "quantity": ..., "match": {...}}
      {"status": "unresolved", "description": ..., "quantity": ..., "reason": ...}
    email_text is passed so SKU filter can scan the raw email when Claude's
    parsed description drops a parenthetical SKU.
    """
    descriptions = [it["description"] for it in requested_items]
    result = vc.embed(descriptions, model=VOYAGE_MODEL, input_type="query")
    embeddings = result.embeddings

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        return [
            _resolve_single_item(item, emb, cur, email_text)
            for item, emb in zip(requested_items, embeddings)
        ]


# ---------------------------------------------------------------------------
# Step 4b — Price anomaly detection
# ---------------------------------------------------------------------------

# If a resolved catalog price is more than this factor below any historical
# per-unit price for the same product, flag the item as unresolved.
PRICE_ANOMALY_RATIO = 10.0   # e.g. $17 vs $1700+ → ratio 100x → flag

# Product name keywords that indicate high-value simulation equipment.
# If a matched product name contains any of these terms AND the catalog
# price is below SIMULATION_PRICE_FLOOR, flag as anomalous even without
# a historical price to compare against.
SIMULATION_PRICE_FLOOR = 100.0
_SIMULATION_KEYWORDS = {
    "simulator", "simulation", "simjunior", "simman", "simbaby",
    "manikin", "mannequin", "phantom", "trainer", "training system",
    "laerdal", "gaumard", "nasco life/form",
}


def _sku_tokens(name: str) -> set[str]:
    """Return all model-number-like tokens from a product name."""
    return set(re.findall(r'[A-Z]{1,6}[-_]?\d[\w\-]{2,}', name.upper()))


def _unit_prices_for_product(full_text: str, name_skus: set[str], name_fragment: str) -> list[float]:
    """
    Extract per-unit prices from lines in a historical quote full_text that
    mention the given product (by SKU token or name fragment).

    Each line-item line looks like:
      "  2. Product Name — qty N @ $UNIT_PRICE = $EXTENDED"
    We extract only the unit price (the value after '@ $'), not the extended total,
    to avoid comparing $296.95/unit against $3,563.40 (12-unit extended).
    """
    prices: list[float] = []
    for line in full_text.splitlines():
        line_upper = line.upper()
        line_lower = line.lower()
        mentions = (
            any(sku in line_upper for sku in name_skus)
            or (name_fragment and name_fragment in line_lower)
        )
        if not mentions:
            continue
        # Try to extract unit price: value after "@ $" and before " ="
        unit_match = re.search(r'@\s*\$([0-9,]+\.[0-9]{2})', line)
        if unit_match:
            prices.append(float(unit_match.group(1).replace(",", "")))
        else:
            # Fallback: any dollar amount on this line
            for m in re.findall(r'\$([0-9,]+\.[0-9]{2})', line):
                prices.append(float(m.replace(",", "")))
    return prices


def flag_price_anomalies(
    resolutions: list[dict],
    retrieved_quotes: list[dict],
) -> list[dict]:
    """
    Post-resolution pass: for each resolved item, check whether the catalog
    price conflicts with per-unit prices found in the retrieved historical quotes.

    A conflict is defined as: the catalog price is more than PRICE_ANOMALY_RATIO
    times smaller than any per-unit price on a historical quote line that
    explicitly mentions the same product (by SKU token or name fragment).

    Items that fail this check are converted to status="unresolved".
    Items with no catalog price (None) are also flagged immediately.
    """
    flagged: list[dict] = []
    for r in resolutions:
        if r["status"] != "resolved":
            flagged.append(r)
            continue

        m = r["match"]
        catalog_price = m.get("price")

        # No price in catalog → cannot quote, flag immediately
        if catalog_price is None:
            log.warning("  price anomaly: '%s' has no catalog price", m["name"][:50])
            flagged.append({
                "status": "unresolved",
                "description": r["description"],
                "quantity": r["quantity"],
                "reason": (
                    f"Catalog match '{m['name'][:60]}' has no price on record "
                    "(call-for-price or data missing). Cannot quote without verified price."
                ),
            })
            continue

        # Absolute floor check: simulation/training equipment below $100 is
        # almost certainly a data entry error regardless of historical context.
        name_lower = m["name"].lower()
        is_sim_product = any(kw in name_lower for kw in _SIMULATION_KEYWORDS)
        if is_sim_product and catalog_price < SIMULATION_PRICE_FLOOR:
            log.warning(
                "  price anomaly: '%s' catalog=$%.2f is implausibly low for simulation equipment",
                m["name"][:50], catalog_price,
            )
            flagged.append({
                "status": "unresolved",
                "description": r["description"],
                "quantity": r["quantity"],
                "reason": (
                    f"Catalog price for '{m['name'][:60]}' is ${catalog_price:,.2f}, "
                    f"which is implausibly low for simulation/training equipment. "
                    "This is almost certainly a data entry error (missing zeros or wrong SKU). "
                    "Do not quote until the price is verified against current supplier pricing."
                ),
                "anomaly_detail": {
                    "catalog_price": catalog_price,
                    "floor": SIMULATION_PRICE_FLOOR,
                    "matched_name": m["name"],
                },
            })
            continue

        # Identify product by SKU tokens and short name fragment
        name_skus = _sku_tokens(m["name"])
        name_words = [w for w in m["name"].split() if len(w) > 2][:4]
        name_fragment = " ".join(name_words).lower()

        conflicting: list[tuple[float, str]] = []
        for q in retrieved_quotes:
            unit_prices = _unit_prices_for_product(
                q.get("full_text", ""), name_skus, name_fragment
            )
            for hp in unit_prices:
                if hp > 0 and catalog_price > 0 and hp / catalog_price >= PRICE_ANOMALY_RATIO:
                    conflicting.append((hp, q["source_file"]))

        if conflicting:
            conflict_desc = "; ".join(
                f"${hp:,.2f} in {src}" for hp, src in conflicting[:3]
            )
            log.warning(
                "  price anomaly: '%s' catalog=$%.2f conflicts with historical unit prices: %s",
                m["name"][:50], catalog_price, conflict_desc,
            )
            flagged.append({
                "status": "unresolved",
                "description": r["description"],
                "quantity": r["quantity"],
                "reason": (
                    f"Catalog price for '{m['name'][:60]}' is ${catalog_price:,.2f}, "
                    f"but historical quote(s) show a much higher per-unit price for the same product "
                    f"({conflict_desc}). Likely a unit-of-measure error or data entry anomaly. "
                    "Do not quote until the price is verified against current supplier pricing."
                ),
                "anomaly_detail": {
                    "catalog_price": catalog_price,
                    "conflicting_historical": conflicting[:3],
                    "matched_name": m["name"],
                },
            })
        else:
            flagged.append(r)

    return flagged


# ---------------------------------------------------------------------------
# Step 5 — Generate quote via Claude
# ---------------------------------------------------------------------------

def generate_quote(
    email_text: str,
    parsed: dict,
    resolutions: list[dict],
    retrieved_quotes: list[dict],
    claude: anthropic.Anthropic,
) -> str:
    """Generate the final quote text using Claude with full RAG context."""

    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    resolved = [r for r in resolutions if r["status"] == "resolved"]
    unresolved = [r for r in resolutions if r["status"] == "unresolved"]

    # Build context block from retrieved quotes
    quote_context = ""
    for i, q in enumerate(retrieved_quotes, 1):
        quote_context += f"\n--- Historical Quote {i} ({q['match_type']}: sim={q['similarity']:.3f}) ---\n"
        quote_context += q["full_text"] + "\n"

    # Build resolved items block
    resolved_block = ""
    for r in resolved:
        m = r["match"]
        price_str = f"${m['price']:.2f}" if m["price"] else "PRICE UNKNOWN"
        resolved_block += (
            f"- {r['description']}\n"
            f"  Matched: {m['name']} (source: {m['source']}, similarity: {m['similarity']:.3f})\n"
            f"  Catalog price: {price_str} | Qty requested: {r['quantity']}\n"
        )

    unresolved_block = ""
    for r in unresolved:
        unresolved_block += f"- {r['description']} (qty: {r['quantity']})\n  Reason: {r['reason']}\n"

    prompt = f"""You are a sales rep at Meridian Medical & Dental Supply Co. generating a formal sales quotation.

## Customer Email
{email_text}

## Parsed Request
Customer: {parsed.get('customer_name', 'Unknown')}
Segment: {parsed.get('customer_segment', 'unknown')}

## Retrieved Historical Quotes (for pricing and discount pattern context)
{quote_context if quote_context else "No similar historical quotes found."}

## Resolved Catalog Items (use these prices — do not invent prices)
{resolved_block if resolved_block else "No items resolved."}

## Unresolved Items (must appear in quote as flagged — do not guess prices)
{unresolved_block if unresolved_block else "None."}

## Quote Template Format
{template}

## Instructions
1. Generate a complete sales quotation following the template format exactly.
2. Use the quote number format QUOTE-NEW-{{}}, today's date, and leave the rep line as "Generated via RAG pipeline".
3. For RESOLVED items: use the catalog price shown above as the list price — never substitute, round, or adjust any price not explicitly documented in the historical quotes.
   - If the same customer appears in historical quotes with preferred pricing on this product type, apply that documented preferred price (note it per line and cite the quote).
   - If the order total qualifies for a volume discount based on the pattern in the historical quotes, apply it and cite the historical quote that established the tier.
   - Do NOT apply preferred pricing unless the historical quotes explicitly show it for this customer or account.
   - If a catalog price looks surprising to you, do NOT adjust it — it has already been validated or flagged by the pipeline before reaching you. Use it exactly as given.
4. For UNRESOLVED items: include them in a clearly marked "Items Requiring Manual Review" section at the bottom of the quote. Do not include them in the subtotal. State the reason each item could not be resolved.
5. Apply freight logic based on the pattern in the historical quotes (free over $500, $18.50 otherwise).
6. Show your reasoning for discounts in the Notes field of the quote — cite the specific historical quote that established the pattern.
7. Do not invent any prices, discounts, or product details not present in the context above.

Generate the complete quote now:"""

    msg = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline(email_path: str, output_path: str) -> None:
    email_text = Path(email_path).read_text(encoding="utf-8")
    log.info("Processing email: %s", email_path)

    p = urlparse(os.environ["DATABASE_URL"])
    conn = psycopg2.connect(
        host=p.hostname, port=p.port or 5432,
        dbname=p.path.lstrip("/"), user=p.username,
        password=urllib.parse.unquote(p.password or ""),
    )

    claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    vc = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])

    log.info("Step 2: parsing email...")
    parsed = parse_email(email_text, claude)
    log.info("  customer: %s | segment: %s | items: %d",
             parsed.get("customer_name"), parsed.get("customer_segment"),
             len(parsed.get("requested_items", [])))

    log.info("Step 3: retrieving historical quotes...")
    retrieved = retrieve_similar_quotes(parsed, email_text, conn, vc)
    log.info("  %d quotes retrieved", len(retrieved))

    log.info("Step 4: resolving line items...")
    resolutions = resolve_line_items(parsed.get("requested_items", []), conn, vc, email_text=email_text)
    resolved_count = sum(1 for r in resolutions if r["status"] == "resolved")
    unresolved_count = sum(1 for r in resolutions if r["status"] == "unresolved")
    log.info("  %d resolved, %d unresolved (before price check)", resolved_count, unresolved_count)

    log.info("Step 4b: checking resolved prices against historical context...")
    resolutions = flag_price_anomalies(resolutions, retrieved)
    resolved_count = sum(1 for r in resolutions if r["status"] == "resolved")
    unresolved_count = sum(1 for r in resolutions if r["status"] == "unresolved")
    log.info("  %d resolved, %d unresolved (after price check)", resolved_count, unresolved_count)

    log.info("Step 5: generating quote via Claude...")
    quote_text = generate_quote(email_text, parsed, resolutions, retrieved, claude)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(quote_text, encoding="utf-8")
    log.info("Quote written to %s", output_path)

    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="RAG quote generation pipeline")
    ap.add_argument("--email", required=True, help="Path to customer email .txt file")
    ap.add_argument("--out", required=True, help="Output path for generated quote")
    args = ap.parse_args()
    run_pipeline(args.email, args.out)
