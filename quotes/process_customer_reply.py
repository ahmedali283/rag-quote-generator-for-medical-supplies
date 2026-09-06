"""
Customer reply processor — Part B of the customer communication layer.

Takes a customer's clarifying reply to an earlier quote email and produces an
updated resolutions list + final quote document. Three outcomes per previously-
unresolved item:

  1. Customer confirms a specific candidate → re-resolve directly to that
     catalog item (no similarity search; customer told us which one).
  2. Customer says to remove/skip the item → drop it entirely.
  3. Customer provides partial info that still doesn't fully resolve it →
     keep unresolved with an updated, more specific reason.

Re-uses generate_quote.py's generation step. Does not duplicate any logic from
the core pipeline.

Usage:
  python quotes/process_customer_reply.py \\
      --original-email   quotes/tests/test_email_live.txt \\
      --reply-email      quotes/tests/test_email_live_reply.txt \\
      --out              quotes/output/quote_live_updated.txt

Optional: also write the updated customer-facing email if --email-out is given.
"""

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import anthropic
import psycopg2
import psycopg2.extras
import voyageai
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quotes.generate_quote import (
    parse_email,
    retrieve_similar_quotes,
    resolve_line_items,
    flag_price_anomalies,
    generate_quote,
    brand_tokens,
)
from quotes.generate_customer_email import generate_customer_email

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parse the customer's reply against the prior unresolved items
# ---------------------------------------------------------------------------

def parse_customer_reply(
    original_email: str,
    reply_email: str,
    prior_resolutions: list[dict],
    claude: anthropic.Anthropic,
) -> list[dict]:
    """
    Use Claude to interpret the customer's reply against ALL prior items —
    both resolved and unresolved. A customer can correct a resolved item
    (e.g. "those gloves are wrong, I meant Adenna") as well as answer
    clarifying questions about unresolved ones.

    Returns one update instruction per item that the reply actually addresses:
      {"description": "...",
       "action": "confirm" | "remove" | "still_unclear" | "no_change",
       "confirmed_name": "product name customer specified, or null",
       "updated_reason": "if still_unclear, more specific reason; else null"}

    Items the reply doesn't mention get action="no_change".
    """
    if not prior_resolutions:
        return []

    all_items_summary = json.dumps(
        [
            {
                "description": r["description"],
                "status": r["status"],
                "currently_resolved_to": (
                    r["match"]["name"] if r["status"] == "resolved" else None
                ),
                "unresolved_reason": (
                    r.get("reason") if r["status"] == "unresolved" else None
                ),
            }
            for r in prior_resolutions
        ],
        indent=2,
    )

    prompt = f"""You are interpreting a customer's reply to a sales quote email.
The customer may be answering clarifying questions about items that couldn't be
priced, OR correcting items that were already resolved to the wrong product.

ORIGINAL CUSTOMER REQUEST:
{original_email}

ALL ITEMS IN THE PRIOR QUOTE (resolved and unresolved):
{all_items_summary}

CUSTOMER'S REPLY:
{reply_email}

For EACH item above that the customer's reply addresses, output a JSON object:
  "description": the item description string (copy exactly from above)
  "action": one of:
    "confirm"       — customer accepted, selected, or named a specific product
    "remove"        — customer said to drop this item entirely
    "still_unclear" — customer gave partial info but item still can't be resolved
    "no_change"     — the reply doesn't address this item at all
  "confirmed_name": if action is "confirm", the product name or brand the customer
    specified. For a correction to a resolved item, this is what they said instead.
    Null for all other actions.
  "updated_reason": if action is "still_unclear", a more specific reason
    incorporating the new information. Null otherwise.

IMPORTANT: If the customer names a specific brand or product for something that
was already resolved to a DIFFERENT product, treat that as a correction —
action="confirm" with the customer's stated name as confirmed_name.

Return ONLY a JSON array, one object per item. Items not addressed by the reply
must still appear with action="no_change". No prose, no markdown fences.
Preserve exact description strings.

Example:
[
  {{"description": "nitrile exam gloves", "action": "confirm",
    "confirmed_name": "Adenna Nitrile Powder Free Exam Gloves", "updated_reason": null}},
  {{"description": "SuperFlex Deluxe Manikin", "action": "remove",
    "confirmed_name": null, "updated_reason": null}},
  {{"description": "adult venipuncture training arms", "action": "no_change",
    "confirmed_name": null, "updated_reason": null}}
]"""

    msg = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=768,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Look up a confirmed catalog item by name
# ---------------------------------------------------------------------------

def resolve_by_name(
    confirmed_name: str,
    description: str,
    quantity,
    conn,
    vc: voyageai.Client,
) -> dict:
    """
    When the customer has named a specific catalog item, find it by embedding
    the confirmed name (not the original vague description). If we get a
    high-confidence single match, return it as resolved. If not, keep unresolved
    with a more specific reason.
    """
    result = vc.embed([confirmed_name], model="voyage-2", input_type="query")
    emb = result.embeddings[0]
    emb_str = "[" + ",".join(str(x) for x in emb) + "]"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
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
            LIMIT 3
            """,
            (emb_str, emb_str),
        )
        candidates = [dict(r) for r in cur.fetchall()]

    if not candidates:
        return {
            "status": "unresolved",
            "description": description,
            "quantity": quantity,
            "reason": f"Customer confirmed '{confirmed_name}' but no matching catalog entry found.",
        }

    top = candidates[0]
    top_sim = float(top["similarity"])

    # For a customer-named item, use a slightly lower bar: 0.65 rather than 0.72.
    # The customer explicitly told us what they want; we're just looking it up.
    if top_sim < 0.65:
        return {
            "status": "unresolved",
            "description": description,
            "quantity": quantity,
            "reason": (
                f"Customer confirmed '{confirmed_name}' but best catalog match "
                f"'{top['raw_name'][:60]}' has low confidence (sim={top_sim:.3f}). "
                "Rep should verify manually."
            ),
        }

    # Sanity check: at least one meaningful (non-generic) token from confirmed_name
    # must appear in the matched product's raw_name or raw_manufacturer. Uses the
    # shared brand_tokens() from generate_quote so the generic-word list stays in
    # one place.
    confirmed_tokens = brand_tokens(confirmed_name)
    candidate_text = (
        (top["raw_name"] or "") + " " + (top["raw_manufacturer"] or "")
    ).lower()
    token_match = not confirmed_tokens or any(tok in candidate_text for tok in confirmed_tokens)

    if not token_match:
        log.warning(
            "  brand mismatch: customer confirmed '%s' but top hit is '%s' (sim=%.3f) — flagging unresolved",
            confirmed_name[:40], top["raw_name"][:50], top_sim,
        )
        return {
            "status": "unresolved",
            "description": description,
            "quantity": quantity,
            "reason": (
                f"Customer confirmed '{confirmed_name}' but the closest catalog match "
                f"is a different product ('{top['raw_name'][:60]}', sim={top_sim:.3f}). "
                "No token from the confirmed name appears in the matched result. "
                "Needs manual verification — rep should locate the correct catalog entry."
            ),
        }

    log.info("  confirmed name '%s' → '%s' (sim=%.3f, $%s)",
             confirmed_name[:40], top["raw_name"][:50], top_sim, top["raw_price"])

    return {
        "status": "resolved",
        "description": description,
        "quantity": quantity,
        "match": {
            "source_item_id": top["source_item_id"],
            "name": top["raw_name"],
            "manufacturer": top["raw_manufacturer"],
            "price": float(top["raw_price"]) if top["raw_price"] else None,
            "source": top["source"],
            "source_url": top["source_url"],
            "similarity": top_sim,
        },
        "confirmed_by_customer": True,
    }


# ---------------------------------------------------------------------------
# Apply reply updates to the prior resolution list
# ---------------------------------------------------------------------------

def apply_reply_updates(
    prior_resolutions: list[dict],
    reply_updates: list[dict],
    conn,
    vc: voyageai.Client,
    retrieved_quotes: list[dict],
) -> list[dict]:
    """
    Merge reply instructions into the prior resolutions list. Handles both
    previously-resolved and previously-unresolved items.

    For resolved items:
      - no_change → pass through unchanged
      - remove    → drop entirely
      - confirm   → check whether the customer's named product matches what
                    we resolved to; if not, re-resolve by the customer's name
                    (override); if yes, pass through unchanged

    For unresolved items:
      - confirm      → re-resolve by the customer's confirmed name
      - remove       → drop entirely
      - still_unclear → keep with updated reason
      - no_change    → keep as-is
    """
    update_map = {u["description"].strip(): u for u in reply_updates}

    updated: list[dict] = []
    for r in prior_resolutions:
        desc = r["description"].strip()
        update = update_map.get(desc)
        action = (update or {}).get("action", "no_change")

        # --- Already-resolved items ---
        if r["status"] == "resolved":
            if action == "remove":
                log.info("  dropping resolved '%s' — customer removed it", desc[:50])
                continue

            if action == "confirm":
                confirmed_name = (update or {}).get("confirmed_name") or ""
                if confirmed_name:
                    # Check whether the confirmed name matches what we already
                    # resolved to. If the customer named something whose brand
                    # tokens are absent from the current resolution, it's a
                    # correction — override it.
                    current_match_text = (
                        (r["match"].get("name") or "") + " "
                        + (r["match"].get("manufacturer") or "")
                    ).lower()
                    confirmed_tkns = brand_tokens(confirmed_name)
                    already_correct = (
                        not confirmed_tkns  # generic name, no brand signal to check
                        or any(tok in current_match_text for tok in confirmed_tkns)
                    )
                    if already_correct:
                        log.info(
                            "  resolved '%s' already matches customer's '%s' — no change",
                            desc[:40], confirmed_name[:40],
                        )
                        updated.append(r)
                    else:
                        log.info(
                            "  customer correction: '%s' resolved to '%s' but customer said '%s' — overriding",
                            desc[:40], r["match"]["name"][:40], confirmed_name[:40],
                        )
                        resolution = resolve_by_name(
                            confirmed_name, r["description"], r.get("quantity"), conn, vc
                        )
                        checked = flag_price_anomalies([resolution], retrieved_quotes)
                        updated.extend(checked)
                else:
                    # confirm with no name → treat as no_change
                    updated.append(r)
            else:
                # no_change or still_unclear on a resolved item → pass through
                updated.append(r)
            continue

        # --- Previously-unresolved items ---
        if action == "remove":
            log.info("  dropping unresolved '%s' — customer removed it", desc[:50])
            continue

        if action == "confirm":
            confirmed_name = (update or {}).get("confirmed_name") or desc
            resolution = resolve_by_name(
                confirmed_name, r["description"], r.get("quantity"), conn, vc
            )
            checked = flag_price_anomalies([resolution], retrieved_quotes)
            updated.extend(checked)
            continue

        if action == "still_unclear":
            new_reason = (update or {}).get("updated_reason") or r.get("reason", "")
            updated.append({
                "status": "unresolved",
                "description": r["description"],
                "quantity": r.get("quantity"),
                "reason": new_reason,
            })
            continue

        # no_change — keep as-is
        updated.append(r)

    return updated


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline(
    original_email_path: str,
    reply_email_path: str,
    output_path: str,
    email_output_path: str | None = None,
) -> None:
    original_email = Path(original_email_path).read_text(encoding="utf-8")
    reply_email = Path(reply_email_path).read_text(encoding="utf-8")
    log.info("Original email: %s", original_email_path)
    log.info("Customer reply: %s", reply_email_path)

    p = urlparse(os.environ["DATABASE_URL"])
    conn = psycopg2.connect(
        host=p.hostname, port=p.port or 5432,
        dbname=p.path.lstrip("/"), user=p.username, password=p.password,
    )
    claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    vc = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])

    # Re-run pipeline steps 2-4b on the ORIGINAL email to get prior resolutions
    log.info("Step 2: parsing original email...")
    parsed = parse_email(original_email, claude)

    log.info("Step 3: retrieving historical quotes...")
    retrieved = retrieve_similar_quotes(parsed, original_email, conn, vc)

    log.info("Step 4: resolving line items from original email...")
    prior_resolutions = resolve_line_items(
        parsed.get("requested_items", []), conn, vc, email_text=original_email
    )
    prior_resolutions = flag_price_anomalies(prior_resolutions, retrieved)

    prior_resolved = sum(1 for r in prior_resolutions if r["status"] == "resolved")
    prior_unresolved = sum(1 for r in prior_resolutions if r["status"] == "unresolved")
    log.info("  prior state: %d resolved, %d unresolved", prior_resolved, prior_unresolved)

    # Parse customer reply
    log.info("Parsing customer reply...")
    reply_updates = parse_customer_reply(original_email, reply_email, prior_resolutions, claude)
    log.info("  %d update instructions parsed from reply", len(reply_updates))
    for u in reply_updates:
        log.info("    [%s] %s → %s", u["action"], u["description"][:40],
                 u.get("confirmed_name") or u.get("updated_reason") or "(dropped)")

    # Apply updates — track which items were explicitly removed
    log.info("Applying reply updates...")
    removed_items = [
        u["description"] for u in reply_updates if u.get("action") == "remove"
    ]
    updated_resolutions = apply_reply_updates(
        prior_resolutions, reply_updates, conn, vc, retrieved
    )

    resolved_count = sum(1 for r in updated_resolutions if r["status"] == "resolved")
    unresolved_count = sum(1 for r in updated_resolutions if r["status"] == "unresolved")
    log.info("  updated state: %d resolved, %d unresolved", resolved_count, unresolved_count)

    # Annotate the email context so the generation prompt knows which items the
    # customer explicitly removed. Without this, Claude reads the original email,
    # sees those items mentioned, and regenerates them as unresolved sections even
    # though they were dropped from the resolutions list.
    annotated_email = original_email
    if removed_items:
        removal_note = (
            "\n\n[PIPELINE NOTE — DO NOT INCLUDE IN QUOTE: The customer's reply explicitly "
            "removed the following item(s) from this order. Do not include them anywhere in "
            "the quote, not even as unresolved items: "
            + "; ".join(f'"{d}"' for d in removed_items)
            + "]"
        )
        annotated_email = original_email + removal_note

    # Regenerate quote with updated resolutions
    log.info("Regenerating quote with updated resolutions...")
    quote_text = generate_quote(annotated_email, parsed, updated_resolutions, retrieved, claude)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(quote_text, encoding="utf-8")
    log.info("Updated quote written to %s", output_path)

    # Optionally regenerate customer-facing email
    if email_output_path:
        log.info("Generating updated customer reply email...")
        email_out = generate_customer_email(
            annotated_email, parsed, updated_resolutions, retrieved, claude
        )
        Path(email_output_path).write_text(email_out, encoding="utf-8")
        log.info("Updated customer email written to %s", email_output_path)

    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Process customer reply and update quote")
    ap.add_argument("--original-email", required=True, help="Path to the original customer email")
    ap.add_argument("--reply-email", required=True, help="Path to the customer's clarifying reply")
    ap.add_argument("--out", required=True, help="Output path for updated quote document")
    ap.add_argument("--email-out", default=None, help="Optional: output path for updated customer reply email")
    args = ap.parse_args()
    run_pipeline(args.original_email, args.reply_email, args.out, args.email_out)
