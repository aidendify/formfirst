# FormFirst — PRD + Builder brief

**Status:** Ready for Builder. Do not invent scope. Do not code extras. Ship this wedge.
**Working name:** FormFirst (no brand decision needed)
**Suggested GitHub repo (Publisher later):** `aidendify/formfirst`
**Price:** Free lead magnet. Self-hosted. No signup, no license server.
**Stack:** Same factory as Nudge / AfterJob — one Flask app, SQLite, Gunicorn, Docker Compose, optional SMTP. ~15 min install on a cheap 1GB VPS.
**Scout score (web-estimated Demand, not X-counted):** D4 F5 E5 Diff3 M4 = **21**. Survived; third free install after Nudge and AfterJob. Kicked to Spec 2026-09-04.

One-liner: When a website contact form fires, FormFirst sends an instant first reply with two qualify questions, then pings the owner a one-screen summary. First-touch only. No multi-day drip.

---

## 1. Problem

A prospect fills the contact form on a small business website. Nobody replies for hours (or days). Median speed-to-lead for SMB is still measured in tens of hours; the lead already booked someone else.

Owners lose form leads not because they lack a CRM, but because the first human reply never leaves in the first minutes. Existing fixes are either:

- expensive inbox SaaS (Podium, Birdeye, LeadSquared-class tools), or
- DIY Zapier/n8n recipes that break when the owner changes the form.

There is no serious 15-minute self-hosted Compose wedge that does **one** thing: instant first reply + two qualify questions + owner summary, then stops.

This is **not** Nudge. Nudge drips quiet sales leads over Day 0 / 3 / 7. FormFirst fires once, on form submit, and never nags again.

## 2. Who it is for (ICP)

Small business owners and local service businesses who get website contact-form leads and lose them because nobody replies in the first minutes. Think HVAC, plumbing, roofing, landscaping, cleaning, contractors, solo consultants, local agencies — anyone whose “Contact us” form is the front door.

They will:

- Install Docker Compose on a cheap Ubuntu/Debian VPS they control.
- Point their form (or Zapier/n8n/Formspree/Make) at a webhook URL.
- Optionally set SMTP so the first reply actually sends. Without SMTP they can still copy the reply and see the owner summary in the UI.

Not for: multi-tenant agency SaaS, Google Ads lead-form sync, SMS-first intake, or anyone whose pain is “lead went quiet after the quote” (that is Nudge).

## 3. MVP scope (build this, nothing else)

One Compose service. One SQLite file. No LLM. No Redis. No Celery. No Twilio. No CRM sync. No paid ads APIs.

### 3.1 Lead in (webhook is the product)

- Generic inbound webhook `POST /hooks/leads` with JSON. Auth: `Authorization: Bearer $WEBHOOK_SECRET` **or** header `X-FormFirst-Secret: $WEBHOOK_SECRET`. If `WEBHOOK_SECRET` is empty, the webhook returns **403** (do not leave it open).
- Accepted fields (case-insensitive keys; extras stored in `raw_json`):
  - `name` or `full_name` (required)
  - `email` (required, must look like an email)
  - `phone` (optional)
  - `message` or `body` (optional — original form message)
  - `source` (optional string, e.g. `homepage`, `contact`)
  - `page_url` (optional)
  - `external_id` (optional — form submission id for idempotency)
- Invalid JSON / missing name or email → **400**.
- Idempotency: same `external_id` when set, or same email + same `message` hash within a 5-minute window → do **not** create a second lead or send a second first-reply. Return 200 with the existing lead id.
- Also support a simple HTML form POST for smoke tests: `POST /hooks/leads` with `application/x-www-form-urlencoded` of the same fields (same secret header). Browser demo form at `/demo` (owner-gated or secret-gated) that posts into the webhook so Verifier does not need an external form tool.

Ship `sample-lead.json` in the repo for curl examples.

### 3.2 Instant first reply (one shot)

On successful ingest, immediately (same request or in-process queue, no second Compose service):

1. Create an unguessable qualify token (32 bytes hex).
2. Build the first-reply email from templates (no LLM):
   - Subject: `Thanks {first_name} — quick question from {BUSINESS_NAME}`
   - Body: short thanks, acknowledge their message if present (truncate to ~280 chars), then **exactly two** qualify questions as numbered text **plus** a single button/link `{PUBLIC_BASE_URL}/q/{token}` that opens the qualify page.
   - Default questions (env-overridable):
     1. `QUALIFY_Q1` default: `What are you looking for help with?`
     2. `QUALIFY_Q2` default: `When do you need this done?`
   - Sign-off uses `FROM_NAME` / `FROM_EMAIL` when set, else `Your name`.
3. If `SMTP_HOST` is set: send the email, mark lead `replied`.
4. If SMTP is unset: mark lead `ready`, show **Copy reply** / **Copy link** in the UI. Send buttons stay hidden (Nudge / AfterJob pattern).
5. Owner can click **Send now** / **Mark sent** on a single lead.

**Hard rule: first-touch only.** No Day-3 / Day-7. No “still interested?” follow-up. No sequence table. One reply per lead. Statuses: `new` | `ready` | `replied` | `qualified` | `skipped`.

### 3.3 Qualify page (public, token only)

`GET /q/<token>` — no owner login.

- Shows business name, the two questions as a short form (text inputs or short textarea each).
- Submit → thank-you page: “Got it — {BUSINESS_NAME} will follow up.”
- Token already used: show the same thank-you, do not overwrite answers.
- Unknown token: 404.

On submit, mark lead `qualified`, store answers, and (if SMTP set) send the **owner summary** email to `OWNER_NOTIFY_EMAIL` (required when SMTP is used for owner ping; if unset, skip the email and rely on the in-app inbox).

### 3.4 Owner summary (in-app + optional email)

- In-app `/` lists leads newest first: name, email, phone, status, time, source.
- Lead detail `/leads/<id>`: original message, first-reply copy, qualify answers, copy buttons, send-now, skip.
- Owner summary email (template, when SMTP + `OWNER_NOTIFY_EMAIL` set) subject: `New form lead: {name}` — body includes name, email, phone, original message, Q1/Q2 answers, and a link to `{PUBLIC_BASE_URL}/leads/{id}`.
- No SMS. No Slack. No push. Email + UI only in MVP.

### 3.5 Owner auth

- If `OWNER_PASSWORD` is set: session login for `/`, lead detail, `/demo`. Cookie via `SECRET_KEY`.
- `GET /q/<token>` and `GET /health` never require login.
- Webhook uses the secret header, not the owner session.
- If `OWNER_PASSWORD` is empty: admin is open (local/dev). README must say: set this on any VPS reachable from the internet.

### 3.6 Health and footer

- `GET /health` → HTTP 200 JSON `{"status":"ok","smtp_configured":false}` even when SMTP is unset. `smtp_configured` is true only when `SMTP_HOST` is set.
- `MARKETING_URL` optional. If set, footer link **Powered by FormFirst** points there. If unset, no footer. Same as Nudge / AfterJob.

### 3.7 Compose shape

Clone Nudge / AfterJob:

- `Dockerfile` + `docker-compose.yml` + `.env.example`
- One service `web`, host `8080:8080`, container always binds `0.0.0.0:8080`
- Volume `formfirst-data` → `/data/formfirst.db`
- Compose healthcheck: `curl -sf http://127.0.0.1:8080/health` (install `curl` in the image)
- `restart: unless-stopped`
- Gunicorn in the image

---

## 4. Out of scope (do not build)

- Multi-day nurture, Day 0/3/7 sequences, “still interested?” drips (that is **Nudge** — kill on sight)
- SMS, telephony, A2P, WhatsApp, missed-call
- CRM sync (HubSpot, Salesforce, Jobber, Housecall Pro native APIs)
- Google Ads / Meta lead-form APIs
- LLM-written replies (templates only)
- Multi-tenant / agency white-label
- Slack / Discord / Telegram owner alerts
- Calendar booking links as a required step (a plain URL in the thank-you template is fine as static text in env later; do not build Cal.com)
- Auto-scoring / lead routing / teams
- License server, payments inside the app
- Redis, Celery, second Compose service

---

## 5. Free vs later paid + Gumroad packaging

| Now (free magnet) | Later Gumroad (do not build in MVP) |
| --- | --- |
| Webhook + instant first-reply template | SMS first-reply (Twilio / BYO) |
| Two qualify questions on a token page | Custom question packs / conditional Qs |
| Owner summary email + in-app inbox | Slack/Teams owner ping, CRM push |
| Copy/export when SMTP unset | Hosted form snippet + branded thank-you |
| Docker Compose on their VPS | Same |

**Gumroad rule (Publisher must follow; Builder does not implement checkout):**

- The **public** Gumroad product page must **not** include the GitHub URL or raw install instructions.
- The install URL / clone command is delivered **only post-purchase** (Gumroad “content after purchase” / download / email).
- Free listing still exists as a lead magnet, but the public page sells the outcome (“instant form reply on your VPS”), not a public repo dump. Contrast with early Nudge/AfterJob free listings that pointed at GitHub on the page — FormFirst flips that for the paid path and any future free→paid packaging CoS chooses.
- `MARKETING_URL` may later point at the Gumroad page; leave empty for Verifier.

Lead path for this free install: GitHub README is still how factory Verifier and early users install. Publisher decides whether the Gumroad free SKU mirrors AfterJob (GitHub on page) or starts the post-purchase-only pattern; PRD preference for FormFirst paid SKU is **post-purchase install URL only**.

---

## 6. Data model (suggested SQLite)

```
leads(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT NOT NULL,
  phone TEXT,
  message TEXT,
  source TEXT,
  page_url TEXT,
  external_id TEXT,
  raw_json TEXT,
  token TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,          -- new|ready|replied|qualified|skipped
  q1_answer TEXT,
  q2_answer TEXT,
  replied_at TEXT,
  qualified_at TEXT,
  created_at TEXT NOT NULL
)
```

Indexes: unique on `token`; unique on `external_id` where not null; index on `created_at DESC`.

WAL + foreign keys on, same as Nudge / AfterJob. No separate `sequences` table. No drip jobs.

---

## 7. Webhook contract

**Endpoint:** `POST /hooks/leads`

**Auth (required):**

```
Authorization: Bearer <WEBHOOK_SECRET>
# or
X-FormFirst-Secret: <WEBHOOK_SECRET>
```

Empty `WEBHOOK_SECRET` → always **403**.

**JSON example:**

```json
{
  "name": "Priya Sharma",
  "email": "priya.sharma@example.com",
  "phone": "+1-555-0100",
  "message": "Need a quote for a 3-ton AC install next week.",
  "source": "homepage",
  "page_url": "https://example.com/contact",
  "external_id": "fs-abc123"
}
```

**Success:** HTTP 200 or 201 JSON `{"status":"ok","lead_id":1,"token":"..."}` (token may be omitted from response if Builder prefers not to leak it; either is fine as long as the qualify link is only in the email/UI). Prefer including `lead_id` always.

**Errors:**

| Case | Code |
| --- | --- |
| Missing/wrong secret | 401 or 403 |
| Empty secret configured | 403 |
| Bad JSON / missing name or email | 400 |
| Idempotent replay | 200 with existing `lead_id` |

**Curl (Verifier / README):**

```bash
curl -sS -X POST http://localhost:8080/hooks/leads \
  -H "Content-Type: application/json" \
  -H "X-FormFirst-Secret: test-secret" \
  -d @sample-lead.json
```

Do not `curl -L` this route.

---

## 8. Happy-path screens (UI)

1. **Login** (when `OWNER_PASSWORD` set) — simple password form.
2. **Lead list `/`** — table: time, name, email, status badge (`new`/`ready`/`replied`/`qualified`/`skipped`), source. Newest first.
3. **Lead detail `/leads/<id>`** — original message; first-reply subject+body with Copy; qualify link Copy; Send now / Mark sent / Skip; after qualify, show Q1/Q2 answers.
4. **Qualify `/q/<token>`** (public) — two questions, Submit, thank-you.
5. **Demo `/demo`** (owner) — tiny form that POSTs into the webhook so smoke tests need no Zapier.
6. **404** for bad tokens.

Keep CSS minimal (reuse Nudge/AfterJob static style if practical). Mobile-usable qualify page.

---

## 9. Install path (README must match)

Documented install: **Ubuntu 22.04 / 24.04**, ~15 minutes. Debian 13 note with `docker.io` + `docker-compose` (do not run the Ubuntu `docker-ce` recipe on Debian). Amazon Linux: “not documented yet.”

Steps:

1. Install Docker Engine + Compose plugin (copy Nudge’s Ubuntu block).
2. `git clone` the repo, `cp .env.example .env`, set at least `BUSINESS_NAME`, `PUBLIC_BASE_URL`, `SECRET_KEY`, `OWNER_PASSWORD`, `WEBHOOK_SECRET`, and the two qualify question strings if customizing. Leave `SMTP_*`, `OWNER_NOTIFY_EMAIL`, and `MARKETING_URL` empty unless they have mail.
3. `docker compose up --build -d`
4. Smoke: `curl -sf http://localhost:8080/health`, then either browser `/demo` or the curl webhook example with `sample-lead.json`.

README sections: What it does, 15-min Ubuntu install, Debian 13, Configuration table, Webhook contract, CSV/manual not required (webhook + demo), What this is not (especially: not a Nudge-style drip).

---

## 10. Env vars (`.env.example`)

| Variable | Required? | Purpose |
| --- | --- | --- |
| `PORT` | documented 8080 | Container always binds 8080 |
| `DATABASE_PATH` | Compose sets `/data/formfirst.db` | SQLite |
| `SECRET_KEY` | yes on a public VPS | Flask sessions |
| `OWNER_PASSWORD` | recommended on a public VPS | Admin login; empty = open admin |
| `BUSINESS_NAME` | yes for copy | Used in first-reply and qualify page |
| `PUBLIC_BASE_URL` | yes for links | No trailing slash. e.g. `http://localhost:8080` |
| `WEBHOOK_SECRET` | yes to use webhook | Empty → webhook 403 |
| `QUALIFY_Q1` | default provided | First qualify question |
| `QUALIFY_Q2` | default provided | Second qualify question |
| `FROM_NAME`, `FROM_EMAIL` | optional | Sign-off and SMTP From |
| `OWNER_NOTIFY_EMAIL` | optional | Owner summary destination when SMTP set |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_TLS` | optional | If `SMTP_HOST` unset, hide send; copy still works |
| `MARKETING_URL` | optional | Footer **Powered by FormFirst**; empty = no footer |

Do not commit `.env`. Never log SMTP passwords, `OWNER_PASSWORD`, or `WEBHOOK_SECRET`.

---

## 11. Routes (Builder)

| Method | Path | Auth | What |
| --- | --- | --- | --- |
| GET | `/health` | none | JSON status |
| GET/POST | `/login` `/logout` | — | Owner session when password set |
| GET | `/` | owner | Lead list |
| GET | `/leads/<id>` | owner | Detail, copy, send now, skip |
| GET/POST | `/demo` | owner | Smoke-test form → webhook |
| POST | `/hooks/leads` | webhook secret | Ingest + first-touch |
| GET | `/q/<token>` | none | Qualify form |
| POST | `/q/<token>` | none | Save answers + owner summary |

---

## 12. Verifier success tests (pass/fail — README is the script)

Verifier follows the README on a clean VPS-like host. Use `.env` with `OWNER_PASSWORD=testpass`, `WEBHOOK_SECRET=test-secret`, `PUBLIC_BASE_URL=http://localhost:8080`, `BUSINESS_NAME=Harbor HVAC`, `QUALIFY_Q1`/`QUALIFY_Q2` defaults, `MARKETING_URL` empty, SMTP unset, `OWNER_NOTIFY_EMAIL` empty.

**Pass only if all hold. Veto on any miss.**

1. **Compose:** `docker compose up --build -d` (or `docker-compose` on Debian) from the README reaches healthy without extra undocumented steps.
2. **Health:** `curl -sf http://localhost:8080/health` → HTTP 200 and JSON containing `"status":"ok"` and `"smtp_configured":false`.
3. **Auth:** `/` requires login when `OWNER_PASSWORD` is set. `/q/<token>` and `/health` do **not**.
4. **Webhook:** POST `sample-lead.json` with `X-FormFirst-Secret: test-secret` → 2xx and a new lead on `/`. Wrong/missing secret → 401/403. Empty secret in env → webhook 403.
5. **Idempotency:** Same `external_id` POST twice → one lead only, no second first-reply body generated as a second row.
6. **First-touch copy:** Lead detail shows a copyable first-reply whose body includes `{PUBLIC_BASE_URL}/q/{token}` and the two qualify questions. SMTP send buttons are **hidden** when SMTP unset.
7. **Qualify happy path:** Open `/q/<token>`, submit answers to both questions. Thank-you shows. Lead status becomes `qualified`. Lead detail shows both answers. Reloading the token does not accept new answers.
8. **Demo form:** Logged-in `/demo` can create a lead without curl.
9. **Skip:** Owner can mark a lead `skipped`.
10. **No drip:** Grep/code or schema: no Day-3/Day-7 sequence, no second scheduled send, no `sequences` table. One reply per lead.
11. **Footer:** Empty `MARKETING_URL` → no Powered-by footer (spot check).
12. **Debian note:** README documents Debian 13 separately from Ubuntu (Nudge lesson).

Fail examples: multi-step nurture emails; webhook open with empty secret; health requires SMTP; second Compose service; CRM/SMS SDKs; qualify page requires owner login; first-reply missing the qualify link.

---

## 13. Builder checklist

- [ ] Flask + SQLite + Gunicorn, one `web` service, port 8080, volume for the db
- [ ] `.env.example`, `Dockerfile`, `docker-compose.yml`, `sample-lead.json`, README with 15-min Ubuntu install + Debian 13 note + smoke tests above
- [ ] Authenticated idempotent webhook + owner demo form
- [ ] Instant first-reply template (copy when SMTP unset; send when set)
- [ ] Exactly two qualify questions on `/q/<token>`; answers on lead detail
- [ ] Owner summary email only when SMTP + `OWNER_NOTIFY_EMAIL` set
- [ ] Optional `OWNER_PASSWORD` and optional `MARKETING_URL`
- [ ] `/health` 200 without SMTP
- [ ] **No** multi-day drip, SMS, CRM, ads APIs, LLM, Redis, second service

Stop at a working repo. Do not publish. Do not invent pricing. Do not hand off to Verifier yourself — Chief of Staff routes Builder → Verifier.

## 14. Copy Builder can use in the README header

Free, self-hosted first-reply for website contact forms. Point your form webhook at FormFirst; it thanks the lead, asks two qualify questions, and shows you a one-screen summary. First-touch only — no multi-day drip.

No signup. No license. One Docker Compose service and a SQLite file. About 15 minutes on a 1GB VPS.
