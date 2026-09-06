"""
Customer-facing reply email generator.

Takes the resolutions list already produced by generate_quote.py's pipeline
steps (Steps 2-4b) and drafts a plain-text email the sales rep can send
directly to the customer.

Resolved items → stated price and quantity in a "here's what we can confirm"
section.

Unresolved items split into two categories:
  - Genuinely ambiguous (multiple plausible candidates, or a size not in catalog
    names): asks a specific, answerable clarifying question naming the real options.
  - No plausible catalog match: states plainly this item isn't carried, offers
    nearby alternatives only if genuinely relevant.

Never states a price for anything not in the resolved list.

Usage:
  python quotes/generate_customer_email.py \
      --email quotes/tests/test_email_live.txt \
      --out   quotes/output/email_live.txt

Or import and call generate_customer_email() directly with the pipeline's
resolutions/parsed/retrieved dicts (skips re-running the pipeline).
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

# Import the full pipeline from generate_quote so we reuse all logic
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quotes.generate_quote import (
    parse_email,
    retrieve_similar_quotes,
    resolve_line_items,
    flag_price_anomalies,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


def _classify_unresolved(reason: str) -> str:
    """
    Map an internal pipeline reason string to a category for the email prompt.
    Returns one of: 'ambiguous', 'size_not_found', 'no_match', 'price_anomaly'.
    """
    r = reason.lower()
    if "ambiguous" in r or "ask customer to clarify" in r or "within" in r:
        return "ambiguous"
    if "size" in r and ("not found" in r or "not confirmed" in r):
        return "size_not_found"
    if "implausibly low" in r or "price" in r and "anomaly" in r or "data entry error" in r:
        return "price_anomaly"
    if "low confidence" in r or "no products" in r or "no catalog" in r:
        return "no_match"
    return "ambiguous"


def _extract_candidate_names(reason: str) -> list[str]:
    """
    Pull out candidate product names mentioned in the reason string.
    Used to pass real names to the email prompt rather than internal notation.
    """
    # Pattern: 'Name Here' (sim=0.XXX) or "Name Here" (sim=0.XXX)
    candidates = re.findall(r"['\"]([^'\"]+)['\"].*?sim=[\d.]+", reason)
    if candidates:
        return candidates
    # Fallback: grab anything in single quotes
    return re.findall(r"'([^']{10,})'", reason)


def generate_customer_email(
    email_text: str,
    parsed: dict,
    resolutions: list[dict],
    retrieved_quotes: list[dict],
    claude: anthropic.Anthropic,
) -> str:
    """
    Draft a customer-facing reply email from the pipeline's resolved/unresolved
    resolutions. Does not re-call the pipeline.
    """
    resolved = [r for r in resolutions if r["status"] == "resolved"]
    unresolved = [r for r in resolutions if r["status"] == "unresolved"]

    # Build a plain-English summary of resolved items for the prompt
    resolved_lines = []
    for r in resolved:
        m = r["match"]
        qty = r.get("quantity") or 1
        price = m.get("price")
        price_str = f"${price:,.2f}" if price is not None else "price TBD"
        resolved_lines.append(
            f"- {m['name']} | qty {qty} | {price_str}/unit | extended ${qty * price:,.2f}"
            if price is not None
            else f"- {m['name']} | qty {qty} | price not on record"
        )

    # Build an annotated summary of unresolved items
    unresolved_lines = []
    for r in unresolved:
        category = _classify_unresolved(r.get("reason", ""))
        candidates = _extract_candidate_names(r.get("reason", ""))
        unresolved_lines.append(
            f"- DESCRIPTION: {r['description']}\n"
            f"  QTY: {r.get('quantity')}\n"
            f"  CATEGORY: {category}\n"
            f"  INTERNAL_REASON: {r['reason']}\n"
            f"  CANDIDATE_NAMES: {candidates}"
        )

    resolved_block = "\n".join(resolved_lines) if resolved_lines else "(none)"
    unresolved_block = "\n".join(unresolved_lines) if unresolved_lines else "(none)"

    customer_name = parsed.get("customer_name") or "there"
    # Use first name or org name shortened for salutation
    salutation_name = customer_name.split(",")[0].strip()

    prompt = f"""You are a sales representative at Meridian Medical & Dental Supply Co.
Draft a friendly, professional reply email to a customer inquiry. The tone should be
direct and helpful — like a knowledgeable rep who has actually looked up the order,
not a form letter. No corporate filler, no excessive hedging.

ORIGINAL CUSTOMER EMAIL:
{email_text}

ITEMS WE CAN CONFIRM (resolved with real prices from our catalog):
{resolved_block}

ITEMS THAT NEED FOLLOW-UP (unresolved — do NOT quote a price for any of these):
{unresolved_block}

INSTRUCTIONS:

1. Open with a brief, direct acknowledgment — name the customer or org if known.

2. If there are RESOLVED items: present them clearly. State product name, quantity,
   and unit price. A brief running subtotal at the end of this section is fine.
   Do not show internal similarity scores or pipeline details.

3. For each UNRESOLVED item, handle it based on its CATEGORY:

   - "ambiguous": List the real candidate options by name (from CANDIDATE_NAMES
     above — clean up any truncation or notation artifacts). Ask which one the
     customer wants. Be specific about what differs between them.

   - "size_not_found": Explain that this particular size isn't confirmed in the
     top catalog results for this product. Ask the customer to confirm: is the
     closest match acceptable in the size they need, or should we check for a
     different SKU? Name the closest match from CANDIDATE_NAMES if available.

   - "no_match": Say plainly that we don't appear to carry this item. If
     CANDIDATE_NAMES lists anything genuinely nearby (same general category),
     offer it as an alternative — but only if it could plausibly serve the
     same purpose. If nothing reasonable exists, just say we don't carry it
     and they should reach out if they find a specific brand/model we can source.

   - "price_anomaly": Do NOT mention catalog prices or internal data errors.
     Simply say we're confirming current pricing with our supplier for this item
     and will have a figure shortly. No explanation of the anomaly.

4. Close by inviting the customer to reply with their selections/confirmations
   so you can finalize the quote. Give them a clear next step.

5. Sign off as James Whitfield, Senior Account Manager, Meridian Medical & Dental.
   Include the company phone (614) 555-0190.

6. Format as plain text. Subject line at the top. No markdown.
   Do not mention "cosine similarity", "sim=", "pipeline", "catalog resolution",
   or any other internal system language. The customer should not know how the
   back-end works.
   Do not invent prices, product availability, or delivery timelines.

Draft the email now:"""

    msg = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


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

    log.info("Step 4b: checking resolved prices against historical context...")
    resolutions = flag_price_anomalies(resolutions, retrieved)

    resolved_count = sum(1 for r in resolutions if r["status"] == "resolved")
    unresolved_count = sum(1 for r in resolutions if r["status"] == "unresolved")
    log.info("  %d resolved, %d unresolved", resolved_count, unresolved_count)

    log.info("Generating customer reply email...")
    email_out = generate_customer_email(email_text, parsed, resolutions, retrieved, claude)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(email_out, encoding="utf-8")
    log.info("Customer email written to %s", output_path)

    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Generate customer-facing reply email from quote pipeline")
    ap.add_argument("--email", required=True, help="Path to customer email .txt file")
    ap.add_argument("--out", required=True, help="Output path for generated reply email")
    args = ap.parse_args()
    run_pipeline(args.email, args.out)
