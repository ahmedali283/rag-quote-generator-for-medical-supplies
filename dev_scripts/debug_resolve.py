import os, sys
from urllib.parse import urlparse
import psycopg2, psycopg2.extras, voyageai
from dotenv import load_dotenv
load_dotenv()

THRESHOLD = 0.72
GAP = 0.03

vc = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])
p = urlparse(os.environ["DATABASE_URL"])
conn = psycopg2.connect(host=p.hostname, port=p.port or 5432, dbname=p.path.lstrip("/"), user=p.username, password=p.password)

items = [
    "Sterling nitrile exam gloves medium gray chemo-tested",
    "Life/form venipuncture injection training arm LF00698U",
    "replacement skin vein kit for IV arms LF01122U",
    "Clinton 5-leg 2-hook IV pole",
]
result = vc.embed(items, model="voyage-2", input_type="query")
for desc, emb in zip(items, result.embeddings):
    emb_str = "[" + ",".join(str(x) for x in emb) + "]"
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT ni.source_item_id, si.raw_name, si.raw_price, si.source,
                   1 - (ni.embedding <=> %s::vector) AS similarity
            FROM normalized_items ni
            JOIN source_items si ON si.id = ni.source_item_id
            WHERE ni.embedding IS NOT NULL
            ORDER BY ni.embedding <=> %s::vector LIMIT 5
        """, (emb_str, emb_str))
        candidates = cur.fetchall()
    print(f"\nQuery: {desc}")
    for c in candidates:
        print(f"  sim={float(c['similarity']):.4f}  {c['source']}  ${c['raw_price']}  {c['raw_name'][:70]}")
conn.close()
