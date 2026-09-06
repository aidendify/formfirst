# FormFirst

Free, self-hosted first-reply for website contact forms. Point your form webhook at FormFirst; it thanks the lead, asks two qualify questions, and shows you a one-screen summary. First-touch only — no multi-day drip.

No signup. No license. One Docker Compose service and a SQLite file. About 15 minutes on a 1GB VPS.

## What it does

- Generic inbound webhook `POST /hooks/leads` for Zapier / n8n / Formspree / Make / your form tool
- Instant first-reply email template (no LLM) with exactly two qualify questions and a token link
- Public qualify page at `/q/<token>` — two short answers, thank-you, no owner login
- Owner inbox lists leads newest first; lead detail has Copy reply / Copy link, Send now / Mark sent / Skip
- Optional owner summary email when SMTP + `OWNER_NOTIFY_EMAIL` are set
- Browser demo form at `/demo` (owner-gated) so smoke tests need no Zapier
- `GET /health` returns HTTP 200 JSON `{"status":"ok","smtp_configured":false}` even when SMTP is unset

Without SMTP you can still copy the first reply and see the owner summary in the UI. Send buttons stay hidden until `SMTP_HOST` is set.

## 15-minute Ubuntu VPS install

Documented on **Ubuntu 22.04 / 24.04**. About 15 minutes.

**Debian 13:** do **not** run the Ubuntu `docker-ce` recipe below on Debian. Use the distro packages instead:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose
sudo usermod -aG docker "$USER"
```

Log out and back in (or `newgrp docker`). On Debian, start the stack with `docker-compose` (hyphen) if `docker compose` is not available.

**Amazon Linux:** not documented yet. Use Ubuntu or Debian.

### 1. Install Docker Engine and the Compose plugin (Ubuntu only)

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo ${UBUNTU_CODENAME:-$VERSION_CODENAME}) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Log out and back in (or run `newgrp docker`) so `docker` works without `sudo`.

### 2. Clone, configure, start

```bash
git clone https://github.com/aidendify/formfirst.git
cd formfirst
cp .env.example .env
```

Edit `.env` and set at least `BUSINESS_NAME`, `PUBLIC_BASE_URL`, `SECRET_KEY`, `OWNER_PASSWORD`, and `WEBHOOK_SECRET`. Optionally customize `QUALIFY_Q1` / `QUALIFY_Q2`. Leave `SMTP_*`, `OWNER_NOTIFY_EMAIL`, and `MARKETING_URL` empty unless you have mail. Set `OWNER_PASSWORD` on any VPS reachable from the internet (empty means the admin UI is open).

```bash
docker compose up --build -d
```

(On Debian, `docker-compose up --build -d` if the Compose plugin is not installed.)

The app binds `0.0.0.0:8080` in the container. Compose maps host `8080:8080`. SQLite lives on the `formfirst-data` volume at `/data/formfirst.db`.

### 3. Smoke test

Use this `.env` for a first pass (Verifier values). Production should use a real `SECRET_KEY`, `OWNER_PASSWORD`, and `WEBHOOK_SECRET`. Do not bake these test passwords as production defaults.

```
OWNER_PASSWORD=testpass
WEBHOOK_SECRET=test-secret
PUBLIC_BASE_URL=http://localhost:8080
BUSINESS_NAME=Harbor HVAC
MARKETING_URL=
SECRET_KEY=change-me
OWNER_NOTIFY_EMAIL=
```

Leave all `SMTP_*` unset.

1. Healthcheck:

   ```bash
   curl -sf http://localhost:8080/health
   ```

   Expected: JSON containing `"status":"ok"` and `"smtp_configured":false`, HTTP 200.

2. Open http://localhost:8080, log in with `testpass` if `OWNER_PASSWORD` is set, then use **Demo** to create a lead — or POST the sample webhook (no `-L`):

   ```bash
   curl -sS -X POST http://localhost:8080/hooks/leads \
     -H "Content-Type: application/json" \
     -H "X-FormFirst-Secret: test-secret" \
     -d @sample-lead.json
   ```

3. Open the lead. Status should be `ready` (SMTP unset, so Send now stays hidden). Copy reply / Copy link work. The email body includes `http://localhost:8080/q/{token}` and both qualify questions.

4. Open the qualify link, submit both answers. Thank-you shows. Lead becomes `qualified`; detail shows Q1/Q2. Reloading the token does not overwrite answers.

## Configuration

Copy `.env.example` to `.env` before `docker compose up`. Variables:

| Variable | Purpose |
| --- | --- |
| `PORT` | Documented as 8080. The container always binds gunicorn to `0.0.0.0:8080`. |
| `DATABASE_PATH` | SQLite file. Compose overrides this to `/data/formfirst.db`. |
| `SECRET_KEY` | Flask session key. Change it on a public VPS. |
| `OWNER_PASSWORD` | Admin login. Empty = open admin (local/dev). Set this on any internet-reachable VPS. |
| `BUSINESS_NAME` | Used in the first-reply email and qualify page. |
| `PUBLIC_BASE_URL` | No trailing slash. Used in qualify links, e.g. `http://localhost:8080`. |
| `WEBHOOK_SECRET` | Auth for `POST /hooks/leads`. Empty → webhook returns 403. |
| `QUALIFY_Q1` | First qualify question (default: What are you looking for help with?). |
| `QUALIFY_Q2` | Second qualify question (default: When do you need this done?). |
| `FROM_NAME`, `FROM_EMAIL` | Sign-off and SMTP From. |
| `OWNER_NOTIFY_EMAIL` | Owner summary destination when SMTP is set. Empty → in-app only. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_TLS` | Optional send. If `SMTP_HOST` is unset, send buttons are hidden. |
| `MARKETING_URL` | If set, footer link **Powered by FormFirst** points here. If unset, there is no footer. |

Do not commit `.env`. SMTP passwords, `OWNER_PASSWORD`, and `WEBHOOK_SECRET` are never written to application logs.

## Webhook

`POST /hooks/leads` accepts JSON or `application/x-www-form-urlencoded` with the same fields. Auth is `Authorization: Bearer $WEBHOOK_SECRET` **or** `X-FormFirst-Secret: $WEBHOOK_SECRET`. If `WEBHOOK_SECRET` is empty, the endpoint returns 403.

Accepted fields (case-insensitive keys; extras stored in `raw_json`):

- `name` or `full_name` (required)
- `email` (required)
- `phone` (optional)
- `message` or `body` (optional)
- `source` (optional)
- `page_url` (optional)
- `external_id` (optional — form submission id for idempotency)

Same `external_id` when set, or same email + same message hash within a 5-minute window, is idempotent and does not create a second lead or second first-reply.

Success: HTTP 200 or 201 JSON `{"status":"ok","lead_id":N}`. Do not `curl -L` this route.

## Qualify page

`GET /q/<token>` is public (no owner login). Two questions, Submit, thank-you: “Got it — {BUSINESS_NAME} will follow up.”

Already-used tokens show the same thank-you and do not overwrite answers. Unknown tokens return 404.

## Healthcheck

`GET /health` → HTTP 200:

```json
{"status":"ok","smtp_configured":false}
```

`smtp_configured` is `true` only when `SMTP_HOST` is set. Health succeeds even when SMTP is unset. This route never requires login.

## Local development (optional)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_PATH=./formfirst.db
python app.py
```

Then open http://localhost:8080. This path is for hacking on the code; the supported install is Docker Compose.

## What this is not

FormFirst is **not** a Nudge-style multi-day drip. There is no Day 0/3/7 sequence, no “still interested?” follow-up, and no `sequences` table. One first-touch reply per lead, then stop.

It also does not send SMS, sync a CRM, call Google Ads / Meta lead APIs, write replies with an LLM, or run as multi-tenant SaaS. It is a single Compose service you run yourself.
