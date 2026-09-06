import json, os, sys, traceback
from urllib.parse import urlparse
import psycopg2
from dotenv import load_dotenv

load_dotenv()
print("loaded .env", flush=True)
print(f"DATABASE_URL present: {'DATABASE_URL' in os.environ}", flush=True)
p = urlparse(os.environ["DATABASE_URL"])
print(f"host={p.hostname} port={p.port} db={p.path.lstrip('/')}", flush=True)
conn = psycopg2.connect(
    host=p.hostname, port=p.port or 5432,
    dbname=p.path.lstrip("/"), user=p.username, password=p.password
)
cur = conn.cursor()

with open("logs/match_decisions_20260904_132411.jsonl") as f:
    records = [json.loads(l) for l in f]

updated = 0
for r in records:
    cur.execute("""
        UPDATE candidate_pairs SET
            decision           = %s,
            computed_score     = %s,
            manufacturer_bonus = %s,
            uom_conflict       = %s,
            attribute_conflict = %s,
            llm_reason         = %s,
            decided_at         = %s
        WHERE id = %s
    """, (
        r["decision"],
        r["computed_score"],
        r["manufacturer_bonus"],
        r["uom_conflict"],
        r["attribute_conflict"],
        r["reason"] if r["reason"] else None,
        r["ts"],
        r["pair_id"],
    ))
    updated += cur.rowcount

conn.commit()
conn.close()
print(f"Restored {updated} / {len(records)} rows")
