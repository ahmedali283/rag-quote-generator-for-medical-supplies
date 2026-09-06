'use client'

import { useState, FormEvent, useRef } from 'react'

interface QuoteResponse {
  quote_text: string
  reply_email: string
  resolved_count: number
  unresolved_count: number
  error?: string | null
}

export default function Home() {
  const [subject, setSubject] = useState('')
  const [body, setBody] = useState('')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<QuoteResponse | null>(null)
  const printRef = useRef<HTMLDivElement>(null)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setLoading(true)
    setResult(null)

    try {
      const res = await fetch('http://localhost:8001/api/quote', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subject, body }),
      })
      const data: QuoteResponse = await res.json()
      setResult(data)
    } catch (err) {
      setResult({
        quote_text: '',
        reply_email: '',
        resolved_count: 0,
        unresolved_count: 0,
        error: err instanceof Error
          ? err.message
          : 'Network error — is the backend running on port 8001?',
      })
    } finally {
      setLoading(false)
    }
  }

  function handlePrint() {
    window.print()
  }

  // Strip leading/trailing ``` fences the LLM sometimes wraps the output in
  function clean(text: string): string {
    return text.replace(/^```[^\n]*\n?/, '').replace(/\n?```$/, '').trim()
  }

  const cleanedQuote = result?.quote_text ? clean(result.quote_text) : ''
  const cleanedEmail = result?.reply_email ? clean(result.reply_email) : ''

  return (
    <>
      {/* ── Print stylesheet: only the quotation prints ────────────────── */}
      <style>{`
        @media print {
          body * { visibility: hidden; }
          #print-area, #print-area * { visibility: visible; }
          #print-area {
            position: fixed;
            inset: 0;
            padding: 32px 40px;
            font-family: 'Courier New', Courier, monospace;
            font-size: 10pt;
            line-height: 1.5;
            color: #000;
            background: #fff;
          }
          .no-print { display: none !important; }
        }
      `}</style>

      <main className="min-h-screen bg-slate-100">

        {/* ── Top bar ───────────────────────────────────────────────────── */}
        <header className="bg-white border-b border-slate-200 px-6 py-4 no-print">
          <div className="max-w-7xl mx-auto flex items-center justify-between">
            <div>
              <h1 className="text-lg font-semibold text-slate-900">Meridian Medical &amp; Dental Supply</h1>
              <p className="text-xs text-slate-500 mt-0.5">AI Quote Generation — demo interface</p>
            </div>
            {result && !result.error && (
              <div className="flex items-center gap-3">
                <span className="inline-flex items-center gap-1.5 rounded-full bg-green-100 px-3 py-1 text-xs font-medium text-green-800">
                  <span className="w-1.5 h-1.5 rounded-full bg-green-500 inline-block" />
                  {result.resolved_count} resolved
                </span>
                {result.unresolved_count > 0 && (
                  <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-100 px-3 py-1 text-xs font-medium text-amber-800">
                    <span className="w-1.5 h-1.5 rounded-full bg-amber-500 inline-block" />
                    {result.unresolved_count} need clarification
                  </span>
                )}
                <button
                  onClick={handlePrint}
                  className="inline-flex items-center gap-2 rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 transition-colors"
                >
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                      d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                  </svg>
                  Download Quotation PDF
                </button>
              </div>
            )}
          </div>
        </header>

        <div className="max-w-7xl mx-auto px-6 py-8">

          {/* ── Input form ────────────────────────────────────────────── */}
          <div className="no-print mb-6">
            <form
              onSubmit={handleSubmit}
              className="bg-white rounded-xl border border-slate-200 shadow-sm p-6"
            >
              <h2 className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-4">
                Inbound Customer Email
              </h2>
              <div className="grid grid-cols-1 gap-4">
                <div>
                  <label htmlFor="subject" className="block text-xs font-medium text-slate-600 mb-1">
                    Subject
                  </label>
                  <input
                    id="subject"
                    type="text"
                    value={subject}
                    onChange={e => setSubject(e.target.value)}
                    required
                    placeholder="Quote request"
                    className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                  />
                </div>
                <div>
                  <label htmlFor="body" className="block text-xs font-medium text-slate-600 mb-1">
                    Email Body
                  </label>
                  <textarea
                    id="body"
                    value={body}
                    onChange={e => setBody(e.target.value)}
                    required
                    rows={result ? 3 : 7}
                    placeholder="Hi, we need 10 boxes of medium nitrile exam gloves..."
                    className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent resize-y"
                  />
                </div>
              </div>
              <div className="mt-4 flex items-center gap-3">
                <button
                  type="submit"
                  disabled={loading}
                  className="rounded-md bg-blue-600 px-5 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                >
                  {loading ? 'Generating…' : result ? 'Regenerate' : 'Generate Quote'}
                </button>
                {loading && (
                  <span className="flex items-center gap-2 text-sm text-slate-500">
                    <span className="inline-block w-4 h-4 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
                    Parsing · retrieving quotes · resolving catalog · drafting reply…
                  </span>
                )}
              </div>
            </form>
          </div>

          {/* ── Error ─────────────────────────────────────────────────── */}
          {result?.error && (
            <div className="no-print bg-red-50 border border-red-200 rounded-xl p-5 text-sm text-red-700">
              <p className="font-semibold mb-1">Pipeline error</p>
              <p>{result.error}</p>
            </div>
          )}

          {/* ── Two-panel result ──────────────────────────────────────── */}
          {result && !result.error && (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">

              {/* Left — outbound reply email to customer */}
              <div className="no-print bg-white rounded-xl border border-slate-200 shadow-sm flex flex-col">
                <div className="px-5 py-3 border-b border-slate-100 flex items-center gap-2">
                  <svg className="w-4 h-4 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                      d="M3 10h10a8 8 0 018 8v2M3 10l6 6m-6-6l6-6" />
                  </svg>
                  <span className="text-xs font-semibold text-slate-600 uppercase tracking-wide">
                    Reply Email to Customer
                  </span>
                  <span className="ml-auto text-xs text-slate-400">draft — ready to send</span>
                </div>
                <div className="p-5 flex-1 overflow-auto">
                  <pre className="text-sm text-slate-800 whitespace-pre-wrap leading-relaxed font-sans">
                    {cleanedEmail}
                  </pre>
                </div>
              </div>

              {/* Right — formal quotation document */}
              <div className="bg-white rounded-xl border border-slate-200 shadow-sm flex flex-col">
                <div className="no-print px-5 py-3 border-b border-slate-100 flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <svg className="w-4 h-4 text-slate-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                        d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                    </svg>
                    <span className="text-xs font-semibold text-slate-600 uppercase tracking-wide">
                      Formal Quotation Document
                    </span>
                  </div>
                  <button
                    onClick={handlePrint}
                    className="text-xs text-blue-600 hover:text-blue-800 font-medium transition-colors"
                  >
                    ↓ Download PDF
                  </button>
                </div>
                <div className="p-5 flex-1 overflow-auto">
                  <pre className="text-xs text-slate-800 font-mono whitespace-pre-wrap leading-relaxed">
                    {cleanedQuote}
                  </pre>
                </div>
              </div>

            </div>
          )}

        </div>
      </main>

      {/* ── Hidden print-only area: only the formal quotation prints ──── */}
      {result && !result.error && (
        <div id="print-area" ref={printRef}>
          <pre style={{
            fontFamily: "'Courier New', Courier, monospace",
            fontSize: '10pt',
            lineHeight: 1.5,
            whiteSpace: 'pre-wrap',
            color: '#000',
            margin: 0,
          }}>
            {cleanedQuote}
          </pre>
        </div>
      )}
    </>
  )
}
