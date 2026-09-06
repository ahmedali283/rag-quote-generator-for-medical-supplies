"""
FastAPI backend for the quote generation demo.

Wraps the existing Part 3 pipeline (quotes/generate_quote.py) behind a single
POST endpoint. All pipeline logic lives in generate_quote.py — this file only
wires it to HTTP.

Run from the project root (so that quotes/ is importable):
    uvicorn webapp.backend.main:app --reload --port 8000
Or from this directory with the project root on PYTHONPATH:
    cd webapp/backend
    PYTHONPATH=../.. uvicorn main:app --reload --port 8000
"""

import os
import sys
import urllib.parse
from pathlib import Path
from urllib.parse import urlparse

# Ensure the project root is on sys.path so `quotes.generate_quote` is importable
# regardless of where uvicorn is launched from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import anthropic
import psycopg2
import psycopg2.extras
import voyageai
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from quotes.generate_quote import (
    flag_price_anomalies,
    generate_quote,
    parse_email,
    resolve_line_items,
    retrieve_similar_quotes,
)
from quotes.generate_customer_email import generate_customer_email

load_dotenv(_PROJECT_ROOT / ".env")

app = FastAPI(title="Quote Generation API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001"],
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


class QuoteRequest(BaseModel):
    subject: str
    body: str


class QuoteResponse(BaseModel):
    quote_text: str
    reply_email: str
    resolved_count: int
    unresolved_count: int
    error: str | None = None


def _make_email_text(subject: str, body: str) -> str:
    """
    Reconstruct a plain-text email string from subject + body.
    parse_email() receives the full email as a single string; the subject line
    gives the parser context about intent, the body contains the item list.
    """
    return f"Subject: {subject}\n\n{body}"


def _db_conn():
    raw = os.environ.get("DATABASE_URL", "")
    if not raw:
        raise RuntimeError("DATABASE_URL is not set — copy .env.example to .env and fill it in.")
    p = urlparse(raw)
    return psycopg2.connect(
        host=p.hostname,
        port=p.port or 5432,
        dbname=p.path.lstrip("/"),
        user=p.username,
        password=urllib.parse.unquote(p.password or ""),
    )


@app.post("/api/quote", response_model=QuoteResponse)
def create_quote(req: QuoteRequest) -> QuoteResponse:
    try:
        email_text = _make_email_text(req.subject, req.body)

        claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        vc = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])
        conn = _db_conn()

        try:
            parsed = parse_email(email_text, claude)
            retrieved = retrieve_similar_quotes(parsed, email_text, conn, vc)
            resolutions = resolve_line_items(
                parsed.get("requested_items", []), conn, vc, email_text=email_text
            )
            resolutions = flag_price_anomalies(resolutions, retrieved)
            quote_text = generate_quote(email_text, parsed, resolutions, retrieved, claude)
            reply_email = generate_customer_email(email_text, parsed, resolutions, retrieved, claude)
        finally:
            conn.close()

        resolved_count = sum(1 for r in resolutions if r["status"] == "resolved")
        unresolved_count = sum(1 for r in resolutions if r["status"] == "unresolved")

        return QuoteResponse(
            quote_text=quote_text,
            reply_email=reply_email,
            resolved_count=resolved_count,
            unresolved_count=unresolved_count,
        )

    except Exception as exc:  # noqa: BLE001
        return QuoteResponse(
            quote_text="",
            reply_email="",
            resolved_count=0,
            unresolved_count=0,
            error=str(exc),
        )
