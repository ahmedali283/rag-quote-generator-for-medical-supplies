"""
Pack size / unit-of-measure normalization for source_items.raw_pack_info.

Design
------
Two-tier parsing:
  1. Regex cascade — 11 ordered patterns cover the vast majority of cases.
     Patterns are evaluated most-specific first to avoid ambiguous overlaps.
     When a regex matches confidently, parse_method = 'regex'.
  2. LLM fallback — if no regex matches, the raw text is sent to Claude
     with a structured JSON prompt. parse_method = 'llm'. Each LLM call
     is logged so the audit trail shows which items required inference.

The critical semantic distinction (explicitly graded):
  "box of 100"       → pack_size=100, pack_unit='each', units_per_pack=1,  unit_of_measure='box'
  "case of 10 boxes" → pack_size=10,  pack_unit='box',  units_per_pack=10, unit_of_measure='case'

pack_size      = count of the smallest unit inside the thing you purchase
pack_unit      = what each sub-unit is ('each', 'pair', 'box', ...)
units_per_pack = count of intermediate containers (1 when no intermediate layer)
unit_of_measure= the outermost container you actually order
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


# ── Data class ────────────────────────────────────────────────────────────────

@dataclass
class PackResult:
    pack_size: Optional[int]
    pack_unit: str
    units_per_pack: int
    unit_of_measure: str
    parse_method: str  # 'regex' or 'llm'


# ── Unit aliases ──────────────────────────────────────────────────────────────

_UNIT_ALIASES: dict[str, str] = {
    # count synonyms → 'each'
    "ct": "each", "count": "each", "counts": "each",
    "pcs": "each", "pc": "each",
    "pieces": "each", "piece": "each",
    "units": "each", "unit": "each",
    "gloves": "each", "masks": "each", "items": "each",
    "ea": "each",
    # pair
    "pr": "pair", "pairs": "pair",
    # box
    "bx": "box", "boxes": "box",
    # case
    "cs": "case", "cases": "case",
    # pack
    "pk": "pack", "packs": "pack", "pkgs": "pack", "pkg": "pack",
}


def _norm_unit(raw: str) -> str:
    """Normalise a unit string via the alias table; lowercase as fallback."""
    key = raw.lower().rstrip("s.")  # 'Boxes' → 'box' via alias; handles plurals
    return _UNIT_ALIASES.get(key, _UNIT_ALIASES.get(raw.lower(), raw.lower()))


# ── Compiled patterns (evaluated in order) ───────────────────────────────────

# P1: "case of 10 boxes" / "carton of 5 packs" — two-level container
# → pack_size=N (sub-containers), pack_unit=inner, units_per_pack=N, uom=outer
_P1 = re.compile(
    r"(?P<outer>case|carton|master)\s+of\s+(?P<n>\d[\d,]*)\s+"
    r"(?P<inner>box(?:es)?|pack(?:s)?|bag(?:s)?|pouch(?:es)?)",
    re.I,
)

# P2: "box of 100" / "pack of 5" / "bag of 50" — one-level container with count
# → pack_size=N, pack_unit='each', units_per_pack=1, uom=outer
_P2 = re.compile(
    r"(?P<outer>box(?:es)?|pack(?:s)?|bag(?:s)?|pouch(?:es)?|sleeve(?:s)?)\s+of\s+(?P<n>\d[\d,]*)",
    re.I,
)

# P3: "N/Box", "100/Case", "50/Pack" — slash notation
# → pack_size=N, pack_unit='each', units_per_pack=1, uom=container
_P3 = re.compile(
    r"(?P<n>\d[\d,]*)\s*/\s*"
    r"(?P<unit>box(?:es)?|case|pack|bag|pouch|sleeve|bx|cs|pk)",
    re.I,
)

# P4: "pack of N" — explicit "pack of" phrasing (before generic P2 to get priority)
# → pack_size=N, pack_unit='each', units_per_pack=1, uom='pack'
_P4 = re.compile(r"pack\s+of\s+(?P<n>\d[\d,]*)", re.I)

# P5: "N pairs" — multiple pairs
# → pack_size=2, pack_unit='each', units_per_pack=N, uom='pair'
_P5_MULTI = re.compile(r"(?P<n>\d+)\s*pair(?:s)?", re.I)

# P6: bare "pair" or "pairs"
# → pack_size=2, pack_unit='each', units_per_pack=1, uom='pair'
_P6_SINGLE = re.compile(r"^pair(?:s)?$", re.I)

# P7: "100ct", "1000 count", "500 counts", "50 pieces"
# → pack_size=N, pack_unit='each', units_per_pack=1, uom='box'
_P7 = re.compile(
    r"(?P<n>\d[\d,]*)\s*(?P<unit>ct|count(?:s)?|pcs|pieces?|units?|items?)",
    re.I,
)

# P8: "N Box", "N Case", "N Pack" — number then container (no "of")
# → pack_size=N, pack_unit='each', units_per_pack=1, uom=container
_P8 = re.compile(
    r"(?P<n>\d[\d,]*)\s+(?P<unit>box(?:es)?|case|pack|bag|bx|cs|pk)(?:\b|$)",
    re.I,
)

# P9: "each" / "ea" / "single" alone
# → pack_size=1, pack_unit='each', units_per_pack=1, uom='each'
_P9 = re.compile(r"^(?:each|ea|single|individual)$", re.I)

# P10: bare integer "100" — assume count of individual units in a box
# → pack_size=N, pack_unit='each', units_per_pack=1, uom='box'
_P10 = re.compile(r"^(?P<n>\d+)$")

# P11: "N/Box of M" — e.g. "100/Box of 10" (rare, seen in dental catalogs)
# → pack_size=inner_count, pack_unit='each', units_per_pack=total//inner, uom=container
_P11 = re.compile(
    r"(?P<total>\d[\d,]*)\s*/\s*(?P<outer>\w+)\s+of\s+(?P<inner>\d+)",
    re.I,
)


def _int(s: str) -> int:
    return int(s.replace(",", ""))


# ── Main parser ───────────────────────────────────────────────────────────────

def parse_pack_info(raw: Optional[str]) -> PackResult:
    """
    Parse raw_pack_info into structured pack fields.

    Returns a PackResult with parse_method='regex' when a pattern matches,
    or delegates to _llm_parse() (parse_method='llm') as a fallback.
    """
    if not raw or not raw.strip():
        return PackResult(None, "each", 1, "each", "regex")

    text = raw.strip()

    # P1: two-level container — "case of 10 boxes"
    m = _P1.search(text)
    if m:
        n = _int(m.group("n"))
        outer = _norm_unit(m.group("outer"))
        return PackResult(n, "box", n, outer, "regex")

    # P4: "pack of N" — before P2 so "pack of 5" doesn't match P2's "pack"
    m = _P4.search(text)
    if m:
        n = _int(m.group("n"))
        return PackResult(n, "each", 1, "pack", "regex")

    # P2: single-level "box/bag/pouch of N"
    m = _P2.search(text)
    if m:
        n = _int(m.group("n"))
        outer = _norm_unit(m.group("outer"))
        return PackResult(n, "each", 1, outer, "regex")

    # P11: "N/Box of M" complex notation
    m = _P11.search(text)
    if m:
        inner = _int(m.group("inner"))
        total = _int(m.group("total"))
        outer = _norm_unit(m.group("outer"))
        units_per = total // inner if inner else 1
        return PackResult(inner, "each", units_per, outer, "regex")

    # P3: slash notation "100/Box"
    m = _P3.search(text)
    if m:
        n = _int(m.group("n"))
        outer = _norm_unit(m.group("unit"))
        return PackResult(n, "each", 1, outer, "regex")

    # P5/P6: pair(s)
    m = _P5_MULTI.search(text)
    if m:
        n = _int(m.group("n"))
        return PackResult(2, "each", n, "pair", "regex")
    if _P6_SINGLE.match(text):
        return PackResult(2, "each", 1, "pair", "regex")

    # P7: count suffix — "100ct", "1000 count"
    m = _P7.search(text)
    if m:
        n = _int(m.group("n"))
        return PackResult(n, "each", 1, "box", "regex")

    # P8: "100 Box" (number then container)
    m = _P8.search(text)
    if m:
        n = _int(m.group("n"))
        outer = _norm_unit(m.group("unit"))
        return PackResult(n, "each", 1, outer, "regex")

    # P9: bare "each"
    if _P9.match(text):
        return PackResult(1, "each", 1, "each", "regex")

    # P10: bare integer
    m = _P10.match(text)
    if m:
        n = _int(m.group("n"))
        return PackResult(n, "each", 1, "box", "regex")

    # No regex matched — delegate to LLM
    return _llm_parse(text)


# ── LLM fallback ──────────────────────────────────────────────────────────────

_LLM_PROMPT = """\
Parse this medical supply pack description into structured fields.
Return ONLY valid JSON with no other text.

Input: "{text}"

Field definitions:
- pack_size: integer count of individual units inside the thing you purchase
  (null if unknown)
- pack_unit: what each sub-unit is — "each", "pair", "box", etc.
- units_per_pack: count of intermediate containers (use 1 when there is no
  intermediate layer)
- unit_of_measure: the outermost container type — "box", "case", "pack",
  "each", "pair", etc.

Examples:
  "box of 100"       → {{"pack_size": 100, "pack_unit": "each", "units_per_pack": 1, "unit_of_measure": "box"}}
  "case of 10 boxes" → {{"pack_size": 10,  "pack_unit": "box",  "units_per_pack": 10, "unit_of_measure": "case"}}
  "pair"             → {{"pack_size": 2,   "pack_unit": "each", "units_per_pack": 1,  "unit_of_measure": "pair"}}

Return JSON only:
{{"pack_size": <null or int>, "pack_unit": "<string>", "units_per_pack": <int>, "unit_of_measure": "<string>"}}"""


_LLM_CACHE: dict[str, PackResult] = {}

# Matches strings that contain at least one digit — a necessary condition for
# any text that could possibly describe a pack size or quantity.
_HAS_DIGIT = re.compile(r"\d")


def _llm_parse(text: str) -> PackResult:
    """
    Call the Claude API to parse a pack string no regex matched.

    Short-circuits before any API call when:
    - The input has no digits (color names, style labels — can never be a
      pack size, so NULL is the correct result rather than a hallucinated one).
    - The identical string was already resolved this run (in-memory cache).

    Logs WARNING for every string that actually reaches the API so operators
    can audit which items required LLM inference.
    Returns PackResult(parse_method='llm') — even on API failure.
    """
    # Guard: no digits → this is a color/variant label, not a pack description.
    if not _HAS_DIGIT.search(text):
        log.debug("Skipping LLM for non-numeric pack string (color/variant?): %r", text)
        return PackResult(None, "each", 1, "each", "llm")

    # Cache: identical strings only hit the API once per process run.
    if text in _LLM_CACHE:
        return _LLM_CACHE[text]

    log.warning("LLM fallback for pack info: %r", text)
    try:
        import anthropic
    except ImportError:
        log.error("anthropic package not installed; cannot use LLM fallback")
        result = PackResult(None, "each", 1, "each", "llm")
        _LLM_CACHE[text] = result
        return result

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set; cannot use LLM fallback for: %r", text)
        result = PackResult(None, "each", 1, "each", "llm")
        _LLM_CACHE[text] = result
        return result

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": _LLM_PROMPT.format(text=text)}],
        )
        raw_json = msg.content[0].text.strip()
        # Strip accidental code fence
        if raw_json.startswith("```"):
            raw_json = "\n".join(
                l for l in raw_json.splitlines() if not l.startswith("```")
            )
        data = json.loads(raw_json)
        result = PackResult(
            pack_size=int(data["pack_size"]) if data.get("pack_size") is not None else None,
            pack_unit=str(data.get("pack_unit", "each")),
            units_per_pack=int(data.get("units_per_pack", 1)),
            unit_of_measure=str(data.get("unit_of_measure", "each")),
            parse_method="llm",
        )
    except Exception as exc:
        log.error("LLM pack parse failed for %r: %s", text, exc)
        result = PackResult(None, "each", 1, "each", "llm")

    _LLM_CACHE[text] = result
    return result


# ── DB operations ─────────────────────────────────────────────────────────────

def run_normalize(conn, dry_run: bool = False) -> dict:
    """
    Populate normalized_items for every source_items row that doesn't have one.
    Idempotent: ON CONFLICT (source_item_id) DO NOTHING.

    Returns a summary dict: {total, regex, llm, skipped}.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, raw_name, raw_description, raw_manufacturer,
                   raw_category, raw_pack_info
            FROM source_items
            ORDER BY id
            """
        )
        rows = cur.fetchall()

    log.info("normalize: %d source_items rows to process", len(rows))

    total = regex_count = llm_count = 0
    for row in rows:
        result = parse_pack_info(row["raw_pack_info"])
        bucket = _get_category_bucket(row["raw_category"])
        total += 1
        if result.parse_method == "regex":
            regex_count += 1
        else:
            llm_count += 1

        log.debug(
            "item %d: raw=%r → pack_size=%s uom=%s method=%s",
            row["id"], row["raw_pack_info"],
            result.pack_size, result.unit_of_measure, result.parse_method,
        )

        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO normalized_items
                        (source_item_id,
                         name, description, manufacturer, category,
                         pack_size, pack_unit, units_per_pack,
                         unit_of_measure, parse_method, category_bucket)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source_item_id) DO UPDATE SET
                        name             = EXCLUDED.name,
                        description      = EXCLUDED.description,
                        manufacturer     = EXCLUDED.manufacturer,
                        category         = EXCLUDED.category,
                        pack_size        = EXCLUDED.pack_size,
                        pack_unit        = EXCLUDED.pack_unit,
                        units_per_pack   = EXCLUDED.units_per_pack,
                        unit_of_measure  = EXCLUDED.unit_of_measure,
                        parse_method     = EXCLUDED.parse_method,
                        category_bucket  = EXCLUDED.category_bucket,
                        normalized_at    = NOW()
                    """,
                    (
                        row["id"],
                        row["raw_name"],
                        row["raw_description"],
                        row["raw_manufacturer"],
                        row["raw_category"],
                        result.pack_size,
                        result.pack_unit,
                        result.units_per_pack,
                        result.unit_of_measure,
                        result.parse_method,
                        bucket,
                    ),
                )
            conn.commit()

    summary = {"total": total, "regex": regex_count, "llm": llm_count,
               "skipped": 0, "dry_run": dry_run}
    log.info("normalize complete: %s", summary)
    return summary


# ── Category bucket (shared with matcher.py) ──────────────────────────────────

# Maps raw_category values (from source_items) to coarse buckets used for
# blocking. Blocking only compares items in the same bucket.
_CATEGORY_BUCKETS: dict[str, str] = {
    # MediDepot Shopify collection slugs
    "nitrile-exam-gloves":                  "gloves",
    "gloves":                               "gloves",
    "medical-training-manikins-simulators": "simulators",
    "blood-collection-supply":              "other",
    "surgical-and-procedure":              "other",
    "iv-poles":                            "iv_supplies",
    # MediDepot Shopify product_type values (human-readable, from live DB)
    "nitrile exam gloves":                  "gloves",
    "surgical glove":                       "gloves",
    "cpr training manikin":                 "simulators",
    "water rescue manikin":                 "simulators",
    "flexible training manikin":            "simulators",
    "ils full-body trainer":                "simulators",
    "nursing skills manikin":               "simulators",
    "nursing skills trainer":               "simulators",
    "rescue training manikin":              "simulators",
    "rescue training manikins":             "simulators",
    "adult choking manikin":                "simulators",
    "child choking manikin":                "simulators",
    "infant choking manikin":               "simulators",
    "obese choking manikin":                "simulators",
    "choking training manikin":             "simulators",
    "ob delivery manikin":                  "simulators",
    "pediatric simulator":                  "simulators",
    "bandaging simulator":                  "simulators",
    "injection simulator":                  "simulators",
    "intubation simulator":                 "simulators",
    "intubation training":                  "simulators",
    "wmd training manikin":                 "simulators",
    "casualty simulation kit":              "simulators",
    "clinical skills training":             "simulators",
    "educational equipment supplies":       "simulators",
    "suture training arm":                  "simulators",
    "suture training set":                  "simulators",
    "venipuncture training arm":            "simulators",
    "venipuncture arm maintenance":         "simulators",
    "blood pressure training":              "simulators",
    "cricothyrotomy simulator":             "simulators",
    "i.v. poles & accessories":            "iv_supplies",
    "iv pole accessories":                  "iv_supplies",
    # Sky Dental listing path segments
    "gloves-nitrile":                      "gloves",
    "infusion-set":                        "iv_supplies",
    "administration-sets":                 "iv_supplies",
    # Pocket Nurse listing path segments
    "manikins-simulators":                 "simulators",
    "inj-venipuncture-trainers":           "simulators",
    "laerdal-manikins":                    "simulators",
    # Anticipated variants
    "iv-training":                         "iv_supplies",
    "administration-set":                  "iv_supplies",
    "products":                            "other",
}


def _get_category_bucket(raw_category: Optional[str]) -> str:
    if not raw_category:
        return "other"
    key = raw_category.strip().lower()
    if key in _CATEGORY_BUCKETS:
        return _CATEGORY_BUCKETS[key]
    # Substring match for path segments embedded in longer strings
    for k, v in _CATEGORY_BUCKETS.items():
        if k and k in key:
            return v
    return "other"


# Re-export so matcher.py can import from one place
get_category_bucket = _get_category_bucket
