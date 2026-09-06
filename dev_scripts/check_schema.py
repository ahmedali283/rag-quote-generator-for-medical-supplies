import os, sys
from urllib.parse import urlparse
import psycopg2
from dotenv import load_dotenv
load_dotenv()
p = urlparse(os.environ['DATABASE_URL'])
conn = psycopg2.connect(host=p.hostname, port=p.port or 5432, dbname=p.path.lstrip('/'), user=p.username, password=p.password)
cur = conn.cursor()
cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name")
print('Tables:', [r[0] for r in cur.fetchall()])
for tbl in ('historical_quotes', 'historical_quote_line_items', 'price_history'):
    cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name=%s ORDER BY ordinal_position", (tbl,))
    rows = cur.fetchall()
    print(f'{tbl}: {rows if rows else "NOT FOUND"}')
conn.close()
