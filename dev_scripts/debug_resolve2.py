"""Debug: show resolution outcome for each clean email item."""
import os, sys, json
from urllib.parse import urlparse
import psycopg2, psycopg2.extras, voyageai, anthropic, re
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, '.')
from quotes.generate_quote import parse_email, resolve_line_items, ITEM_RESOLUTION_THRESHOLD, AMBIGUITY_GAP

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
vc = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])
p = urlparse(os.environ["DATABASE_URL"])
conn = psycopg2.connect(host=p.hostname, port=p.port or 5432, dbname=p.path.lstrip("/"), user=p.username, password=p.password)

email = open("quotes/test_email_clean.txt", encoding="utf-8").read()
parsed = parse_email(email, claude)
print("Parsed items:")
for it in parsed["requested_items"]:
    print(f"  desc={it['description']!r}  qty={it.get('quantity')}  size={it.get('size')!r}  attrs={it.get('attributes')}")

print()
resolutions = resolve_line_items(parsed["requested_items"], conn, vc)
print("Resolutions:")
for r in resolutions:
    if r["status"] == "resolved":
        m = r["match"]
        print(f"  RESOLVED: {r['description']!r}")
        print(f"    -> {m['name'][:70]}  sim={m['similarity']:.4f}  price=${m['price']}")
    else:
        print(f"  UNRESOLVED: {r['description']!r}")
        print(f"    reason: {r['reason']}")
conn.close()
