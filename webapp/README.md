# Quote Generation Demo — Web Interface

Demo web interface for the Part 3 RAG pipeline. Not part of the graded assessment deliverables — built as an interview-facing demo on top of the existing pipeline.

---

## Prerequisites

- Project `.env` filled in (`ANTHROPIC_API_KEY`, `VOYAGE_API_KEY`, `DATABASE_URL`)
- Project venv created and dependencies installed (`pip install -r requirements.txt` from project root)
- FastAPI and uvicorn installed (`pip install fastapi==0.115.0 "uvicorn[standard]==0.30.6"`)
- Node.js 18+ and npm

---

## Running the backend

From the **project root** (so that `quotes/` is importable):

```
venv\Scripts\activate
uvicorn webapp.backend.main:app --reload --port 8000
```

The API will be available at `http://localhost:8000`. Interactive docs at `http://localhost:8000/docs`.

---

## Running the frontend

```
cd webapp/frontend
npm install
npm run dev
```

Open `http://localhost:3000`.

---

## Using the demo

1. Paste a customer email subject into the Subject field.
2. Paste the email body into the Email Body field.
3. Click **Generate Quote**.
4. The pipeline runs server-side (several seconds — it makes Voyage AI and Anthropic API calls).
5. The returned quote appears in a monospace block, with a summary of how many items resolved vs. need clarification.

---

## API

`POST http://localhost:8000/api/quote`

Request:
```json
{ "subject": "Quote request", "body": "Hi, we need 10 boxes of..." }
```

Response:
```json
{
  "quote_text": "...",
  "resolved_count": 1,
  "unresolved_count": 2,
  "error": null
}
```
