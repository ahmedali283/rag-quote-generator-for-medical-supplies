# Historical Quotes — Generation Notes

## What Was Generated

18 historical sales quotes (`historical_quotes.json`) from Meridian Medical & Dental Supply Co.,
covering the period September 2025 – August 2026. One reusable quote template (`quote_template.txt`)
showing the standard layout these quotes follow.

All product names are drawn from real entries in the `source_items` table (MediDepot, Sky Dental,
PocketNurse). No fictional products were invented. Prices are taken from or derived from
`raw_price` values in the live database.

### Customer Segments Covered

| Segment             | Accounts | Quotes |
|---------------------|----------|--------|
| nursing_school      | 2        | 4      |
| hospital_sim_lab    | 4        | 6      |
| ems_program         | 2        | 4      |
| dental_clinic       | 3        | 4      |
| clinical_skills_lab | 2        | 4      |

---

## Pricing Patterns Embedded

These patterns are intentionally present for Part 3's RAG retrieval to surface:

### 1. Repeat Customer Preferred Pricing

Accounts with **3 or more prior orders in the last 12 months** receive preferred unit pricing,
typically **3–5% below list price** on their established product lines. This is NOT a blanket
discount — it applies per product category based on ordering history.

**Examples in the data:**

- `ACCT-004421` (Riverside Community College): 3% below list on STERLING® nitrile gloves,
  applied from QUOTE-100142 onward. New product categories (CPR manikins in QUOTE-100327)
  still receive list pricing until a purchase history is established for that line.

- `ACCT-003156` (Metro EMS Training Academy): 4% below list on Prestan CPR manikins
  (QUOTE-100201, QUOTE-100421). Airway trainers were added later at list price —
  no preferred pricing for categories without purchase history.

- `ACCT-009012` (Westview Dental Group): 5% below list on all glove lines (highest
  tier — 7+ orders). Visible across QUOTE-100229 and QUOTE-100371.

- `ACCT-007291` (Northgate Community Hospital): 3% below list on venipuncture training
  arms (QUOTE-100280, QUOTE-100518).

- `ACCT-003489` (Gulf Coast EMS): List pricing in QUOTE-100349 (new account),
  preferred pricing (4%) kicks in at QUOTE-100589 after reaching the 3-order threshold.

- `ACCT-005540` (Lakewood University): QUOTE-100255 (new, list price), QUOTE-100567
  (2 orders, still below threshold — list price still applies). This is a deliberate
  near-miss to test whether the retrieval system can distinguish "approaching threshold"
  from "has qualified."

**Key retrieval signal:** When a customer asks for a quote on a product, retrieve prior quotes
for the same account to determine (a) whether they qualify for preferred pricing and
(b) what per-unit price they previously received on that exact product line.

### 2. Volume Discount Schedule

Applied automatically based on order subtotal (before preferred-pricing adjustments):

| Subtotal Range       | Discount |
|----------------------|----------|
| Under $1,000         | 0%       |
| $1,000 – $2,499      | 3%       |
| $2,500 – $4,999      | 5%       |
| $5,000+              | 8%       |

**Examples in the data:**

- QUOTE-100301 ($211.87 subtotal): 0% discount, freight applies ($18.50)
- QUOTE-100541 ($215.88 subtotal): 0% discount, freight applies ($18.50)
- QUOTE-100447 ($1,411.85 subtotal): 3% discount
- QUOTE-100327 ($2,607.48 subtotal): 5% discount
- QUOTE-100178 ($6,623.20 subtotal): 8% discount

**Key retrieval signal:** When generating a new quote, retrieve the discount schedule
and apply the correct tier based on the projected subtotal.

### 3. Freight Policy

- Orders over $500: free freight
- Orders under $500: flat $18.50 freight charge
- Visible on QUOTE-100301 and QUOTE-100541 (both under $500, both charged $18.50)

### 4. Interactions Between Discounts

Preferred pricing (per-unit price reduction) and volume discounts (applied to subtotal)
stack. A repeat customer placing a large order gets both:
- Lower per-unit price from preferred pricing
- Then the volume discount applied to the already-reduced subtotal

See QUOTE-100518 and QUOTE-100589 for examples of both discounts active simultaneously.

---

## How the RAG System Should Use These

A Part 3 quoting assistant should:

1. **Retrieve prior quotes for the same `account_id`** to determine preferred pricing eligibility
   and the per-unit price previously negotiated on specific SKUs.

2. **Count `prior_orders_12mo`** (or compute from retrieved quotes) to confirm the account
   meets the 3-order threshold before applying preferred pricing.

3. **Retrieve the volume discount schedule** (available in every quote's terms section and
   in `quote_template.txt`) and apply the correct tier to the projected subtotal.

4. **Note product-category specificity**: preferred pricing applies per product line,
   not account-wide. A customer's history with gloves does not automatically qualify
   their first manikin purchase for preferred pricing.

5. **Surface the near-miss case** (ACCT-005540, Lakewood University): 2 prior orders means
   they are one order away from preferred pricing — a sales rep could note this proactively.

---

## File Layout

```
quotes/
├── historical_quotes.json   — 18 structured quotes (machine-readable)
├── quote_template.txt       — Standard quote format/layout reference
└── QUOTES_NOTES.md          — This file
```
