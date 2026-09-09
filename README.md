# Support Ticket Triage

**▶ Live demo: <https://fadhlyemen.github.io/ticket-triage-demo/>**

No signup, no server, no API key. Send a ticket, or use the fault injector to
watch the pipeline handle a malformed model response. The same page runs against
the real FastAPI service when served by it — see *Run it* below.

Webhook-driven support-ticket classification: a FastAPI endpoint receives a
ticket, sends it to the OpenAI API under a strict JSON schema, validates and
repairs the response, routes high-confidence tickets automatically, sends
everything else to a review queue, blocks duplicate deliveries, and logs every
failed call.

Runs with **no API key** out of the box — an offline keyword classifier stands in
for the model so the whole pipeline, including the failure paths, can be
demonstrated and tested without spending a request.

---

## Run it

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

* Dashboard — <http://127.0.0.1:8000/>
* OpenAPI docs — <http://127.0.0.1:8000/docs>
* Tests — `python tests/test_pipeline.py` (14 checks, no network needed)

To use the real model, set `OPENAI_API_KEY` (see `.env.example`). Nothing else
changes: `/health` will report `provider: openai` and the dashboard picks it up.

`static/index.html` also works as a **standalone file**. Opened without a
backend it runs the identical policy logic in the browser — same taxonomy,
thresholds, validation rules and fingerprinting — so it can be hosted as a
static page anywhere. Served by the FastAPI app, it detects `/health` and drives
the real service instead.

---

## Deploy

Any container host works. Cloud Run:

```bash
gcloud run deploy ticket-triage \
  --source . --region us-central1 --allow-unauthenticated \
  --set-env-vars DB_PATH=/tmp/triage.db
# add: --set-env-vars OPENAI_API_KEY=sk-...   to switch on the real model
```

Render / Fly / Railway: point them at the `Dockerfile`, no other configuration.
Static-only (dashboard in simulation mode): drop `static/index.html` on GitHub
Pages, Netlify Drop, or Cloudflare Pages.

---

## The pipeline

```
POST /webhook/ticket
      │
      ├─ 1. Ingest        payload validated against the request schema (422 if not)
      ├─ 2. Dedupe        content fingerprint claimed atomically; a repeat is dropped
      │                   before any API call is made and answered 200
      ├─ 3. Classify      strict json_schema, temperature 0, retry with backoff
      ├─ 4. Validate      every field re-checked; repairable problems fixed and
      │                   flagged, structural ones get one repair call
      ├─ 5. Route         confidence + validation state + hard policy overrides
      └─ 6. Persist       ticket, decision, flags and errors written to SQLite
```

### 1 · Reliable model output

* `response_format: json_schema` with `strict: true`, enums inlined, temperature 0.
* Retries on 408/409/429/5xx, timeouts, transport errors and unparseable bodies,
  with exponential backoff and `Retry-After` honoured. 401/403/404 fail fast —
  they will not fix themselves.
* `finish_reason == "length"` is treated as a failure, not as a result.
* The prompt asks for calibrated confidence explicitly, and tells the model that
  ambiguous, multi-issue and non-English tickets should score low rather than
  guess.

### 2 · Validation and repair

The response is treated as untrusted input even under strict mode — a fallback
model, a proxy, a truncated stream or a silently downgraded provider can all
produce something the schema promised would not happen.

| Problem | Severity | What happens |
|---|---|---|
| Empty / unparseable response | structural | fall back to `general_inquiry` @ 0.0, log, repair call |
| Missing or invented `category` / `urgency` / `team` | structural | default or derive, log, repair call |
| Non-canonical enum spelling | repaired | snapped to the nearest valid value |
| Team that does not own the category | repaired | overridden by the routing table |
| `confidence` non-numeric | structural | set to 0.0 → review |
| `confidence` = 1.7 | repaired | clamped to 1.0 (a botched 0–1 answer, not 1.7 %) |
| `confidence` = 92 | repaired | rescaled from percent **and capped at 0.5** — a score we had to reinterpret must never clear the auto-route bar |
| `summary` empty / over 300 chars | repaired | subject substituted / truncated |

Only **structural** problems justify a second API call. Everything else is fixed
deterministically for free.

### 3 · Routing

```
auto-route  ⇔  confidence ≥ 0.85
               AND no decision-bearing field was corrected
               AND no policy override applies
```

Everything else goes to the review queue **with a reason attached**. Three rules
sit above confidence:

* A response the validator had to correct on a decision-bearing field
  (`category`, `team`, `urgency`, `confidence`) loses automation even if the
  correction was trivial — a model that got the field wrong is a model whose
  confidence on that ticket is not evidence. A truncated summary is cosmetic and
  does not block.
* `abuse_security` always gets human eyes.
* `critical` urgency always needs an on-call acknowledgement.

### 4 · Duplicate protection

`external_id` is the primary key when the helpdesk supplies one. Otherwise the
fingerprint is a hash of the normalised subject + body + requester, with quoted
reply chains stripped, whitespace collapsed, case folded and punctuation
removed — so a retried webhook, a reflowed resend, or the same complaint pasted
twice all collapse to one fingerprint.

The claim is a single `INSERT OR IGNORE` on a primary key, so two concurrent
deliveries race in the database and exactly one wins. This is the part that is
correct rather than merely likely under concurrency.

A failed classification **releases** the fingerprint, so the helpdesk can safely
redeliver that exact ticket.

### 5 · Error logging

Every failure is written to the `errors` table with stage, kind, attempt count
and a payload excerpt, and emitted as one structured JSON line to stdout for
whatever log sink is in front of the service. Stages: `ingest`, `classify`,
`parse`, `validate`, `repair`.

---

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/webhook/ticket` | ingest a ticket — 200 processed/duplicate, 422 bad payload, 502 upstream failed |
| GET | `/api/tickets?action=` | ticket list, optionally filtered by routing action |
| GET | `/api/tickets/{id}` | one ticket with flags and decision |
| GET | `/api/review/queue` | everything awaiting a human |
| POST | `/api/review/{id}` | resolve — `{"decision":"approved\|reassigned\|rejected","team":"..."}` |
| GET | `/api/stats` | counts, automation rate, distributions |
| GET | `/api/errors` | the error log |
| GET | `/health` | provider, model, live thresholds |

Fault injection for demos: send `X-Demo-Fault: unknown_enum` (or
`missing_field`, `bad_confidence`, `wrong_team`, `empty_summary`, `not_json`,
`api_error`) with any request.

---

## Layout

```
app/models.py       taxonomy, request/response contracts, the strict JSON schema
app/classifier.py   OpenAI client with retries + the offline mock and its faults
app/validation.py   field validation, repair, and the routing policy
app/store.py        SQLite: tickets, dedupe ledger, error log, stats
app/main.py         FastAPI routes and the pipeline itself
app/samples.py      12-ticket demo corpus, one per branch
static/index.html   dashboard; also runs standalone with the logic mirrored in JS
tests/              14 end-to-end checks, no network
```

## Notes on the demo corpus

The 12 samples are deliberately stacked with edge cases — a vague one-liner, a
multi-issue ticket, an Arabic ticket, an angry email about $3.20, a duplicate —
so the automation rate shown (3 of 11) is far below what a real inbox produces.
The point is not the rate; it is that every ticket the model was unsure about,
or answered badly, lands in the review queue with a reason instead of being
silently misrouted.

---

## Repository notes

`docs/index.html` is the copy GitHub Pages serves; `static/index.html` is the
canonical file the FastAPI app serves. After editing the canonical one:

```bash
cp static/index.html docs/index.html
```

`publish_github.py` pushes this project and enables Pages via the GitHub API
(`GITHUB_TOKEN` needs Contents, Administration and Pages write).
