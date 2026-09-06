import json, os, sys
from urllib.parse import urlparse
import psycopg2
from dotenv import load_dotenv
from matching.matcher import (
    BONUS_MANUFACTURER, PENALTY_UOM_CONFLICT, PENALTY_ATTRIBUTE_CONFLICT,
    THRESHOLD_AUTO_MATCH, THRESHOLD_AUTO_REJECT,
)

load_dotenv()
p = urlparse(os.environ["DATABASE_URL"])
conn = psycopg2.connect(
    host=p.hostname, port=p.port or 5432,
    dbname=p.path.lstrip("/"), user=p.username, password=p.password
)
cur = conn.cursor()

with open("logs/match_decisions_20260904_132411.jsonl") as f:
    records = [json.loads(l) for l in f]

# Identify the 63 auto pairs where Fix 1 changes the decision
changed = []
for r in records:
    if r["tier"] != "auto":
        continue
    cosine_sim = float(r["cosine_sim"])
    score = cosine_sim
    new_bonus = False
    if r["manufacturer_bonus"] and cosine_sim >= 0.80:
        score += BONUS_MANUFACTURER
        new_bonus = True
    if r["uom_conflict"]:
        score += PENALTY_UOM_CONFLICT
    if r["attribute_conflict"]:
        score += PENALTY_ATTRIBUTE_CONFLICT
    score = max(0.0, min(1.0, score))

    if score >= THRESHOLD_AUTO_MATCH:
        new_decision = "auto_match"
    elif score <= THRESHOLD_AUTO_REJECT:
        new_decision = "auto_reject"
    else:
        new_decision = "pending_llm"

    if new_decision != r["decision"]:
        changed.append({
            "pair_id": r["pair_id"],
            "new_score": round(score, 4),
            "new_bonus": new_bonus,
            "new_decision": new_decision,
            "old_decision": r["decision"],
            "cosine_sim": cosine_sim,
        })

print(f"Pairs to recalculate: {len(changed)}")
for c in changed:
    print(f"  pair_id={c['pair_id']}  {c['old_decision']} -> {c['new_decision']}  "
          f"cosine_sim={c['cosine_sim']}  new_score={c['new_score']}  bonus={c['new_bonus']}")

print()
print("Writing to DB...", flush=True)
updated = 0
for c in changed:
    cur.execute("""
        UPDATE candidate_pairs SET
            computed_score     = %s,
            manufacturer_bonus = %s,
            decision           = %s,
            decided_at         = NULL,
            llm_reason         = NULL
        WHERE id = %s
    """, (c["new_score"], c["new_bonus"], c["new_decision"], c["pair_id"]))
    updated += cur.rowcount

conn.commit()
conn.close()
print(f"Updated {updated} / {len(changed)} rows")
