"""FormFirst: instant first-reply for website contact forms."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

APP_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = APP_ROOT / "formfirst.db"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY", "formfirst-self-hosted-change-me")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

STATUSES = ("new", "ready", "replied", "qualified", "skipped")
OPEN_ENDPOINTS = {"health", "login", "logout", "qualify", "ingest_webhook", "static"}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def database_path() -> str:
    raw = _env("DATABASE_PATH")
    if raw:
        return raw
    return str(DEFAULT_DB)


def smtp_configured() -> bool:
    return bool(_env("SMTP_HOST"))


def owner_password() -> str:
    return os.environ.get("OWNER_PASSWORD", "").strip()


def webhook_secret() -> str:
    return os.environ.get("WEBHOOK_SECRET", "").strip()


def public_base_url() -> str:
    return _env("PUBLIC_BASE_URL").rstrip("/")


def qualify_q1() -> str:
    return _env("QUALIFY_Q1") or "What are you looking for help with?"


def qualify_q2() -> str:
    return _env("QUALIFY_Q2") or "When do you need this done?"


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_now_iso() -> str:
    return to_iso(utc_now())


def valid_email(value: str) -> bool:
    return bool(EMAIL_RE.match((value or "").strip()))


def first_name(name: str) -> str:
    parts = (name or "").strip().split()
    return parts[0] if parts else "there"


def signoff_block() -> str:
    name = _env("FROM_NAME")
    email = _env("FROM_EMAIL")
    if name and email:
        return f"{name}\n{email}"
    if name:
        return name
    if email:
        return email
    return "Your name"


def qualify_link(token: str) -> str:
    base = public_base_url() or "http://localhost:8080"
    return f"{base}/q/{token}"


def message_hash(message: str) -> str:
    return hashlib.sha256((message or "").encode("utf-8")).hexdigest()


def first_reply_subject(lead: sqlite3.Row | dict) -> str:
    name = lead["name"] if not isinstance(lead, dict) else lead["name"]
    business = _env("BUSINESS_NAME") or "us"
    return f"Thanks {first_name(name)} — quick question from {business}"


def first_reply_body(lead: sqlite3.Row | dict) -> str:
    name = lead["name"] if not isinstance(lead, dict) else lead["name"]
    token = lead["token"] if not isinstance(lead, dict) else lead["token"]
    message = lead["message"] if not isinstance(lead, dict) else lead.get("message")
    fn = first_name(name)
    business = _env("BUSINESS_NAME") or "us"
    link = qualify_link(token)
    q1 = qualify_q1()
    q2 = qualify_q2()
    sign = signoff_block()
    parts = [
        f"Hi {fn},",
        "",
        f"Thanks for reaching out to {business}. We got your note and wanted to reply right away.",
    ]
    msg = (message or "").strip()
    if msg:
        truncated = msg if len(msg) <= 280 else msg[:277].rstrip() + "..."
        parts.extend(["", f'You wrote: "{truncated}"'])
    parts.extend(
        [
            "",
            "Two quick questions so we can help faster:",
            "",
            f"1. {q1}",
            f"2. {q2}",
            "",
            f"Answer here: {link}",
            "",
            f"Thank you,",
            sign,
            "",
        ]
    )
    return "\n".join(parts)


def owner_summary_subject(lead: sqlite3.Row | dict) -> str:
    name = lead["name"] if not isinstance(lead, dict) else lead["name"]
    return f"New form lead: {name}"


def owner_summary_body(lead: sqlite3.Row | dict) -> str:
    name = lead["name"] if not isinstance(lead, dict) else lead["name"]
    email = lead["email"] if not isinstance(lead, dict) else lead["email"]
    phone = lead["phone"] if not isinstance(lead, dict) else lead.get("phone")
    message = lead["message"] if not isinstance(lead, dict) else lead.get("message")
    q1a = lead["q1_answer"] if not isinstance(lead, dict) else lead.get("q1_answer")
    q2a = lead["q2_answer"] if not isinstance(lead, dict) else lead.get("q2_answer")
    lead_id = lead["id"] if not isinstance(lead, dict) else lead["id"]
    base = public_base_url() or "http://localhost:8080"
    return (
        f"Name: {name}\n"
        f"Email: {email}\n"
        f"Phone: {phone or '—'}\n"
        f"Message: {message or '—'}\n"
        f"\n"
        f"Q1 ({qualify_q1()}): {q1a or '—'}\n"
        f"Q2 ({qualify_q2()}): {q2a or '—'}\n"
        f"\n"
        f"Open in FormFirst: {base}/leads/{lead_id}\n"
    )


def connect_db() -> sqlite3.Connection:
    path = database_path()
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    db = sqlite3.connect(path, timeout=15, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def get_db() -> sqlite3.Connection:
    db = getattr(g, "_db", None)
    if db is None:
        db = connect_db()
        g._db = db
    return db


@app.teardown_appcontext
def close_db(_exc: BaseException | None) -> None:
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    with app.app_context():
        db = get_db()
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                phone TEXT,
                message TEXT,
                source TEXT,
                page_url TEXT,
                external_id TEXT,
                raw_json TEXT,
                token TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                q1_answer TEXT,
                q2_answer TEXT,
                replied_at TEXT,
                qualified_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_token ON leads(token);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_external_id
                ON leads(external_id) WHERE external_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_leads_created_at ON leads(created_at DESC);
            """
        )
        db.commit()


@app.context_processor
def inject_globals() -> dict:
    return {
        "marketing_url": _env("MARKETING_URL"),
        "smtp_configured": smtp_configured(),
        "business_name": _env("BUSINESS_NAME") or "FormFirst",
        "owner_locked": bool(owner_password()),
        "logged_in": bool(session.get("owner")) or not owner_password(),
        "qualify_q1": qualify_q1(),
        "qualify_q2": qualify_q2(),
    }


@app.before_request
def protect_owner_routes():
    if request.endpoint in OPEN_ENDPOINTS or request.endpoint is None:
        return None
    if not owner_password():
        return None
    if session.get("owner"):
        return None
    nxt = request.path if request.method == "GET" else "/"
    return redirect(url_for("login", next=nxt))


def secrets_equal(provided: str, expected: str) -> bool:
    a = (provided or "").encode("utf-8")
    b = (expected or "").encode("utf-8")
    if len(a) != len(b):
        return hmac.compare_digest(b, b) and False
    return hmac.compare_digest(a, b)


def get_lead(lead_id: int) -> sqlite3.Row | None:
    return get_db().execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()


def get_lead_by_token(token: str) -> sqlite3.Row | None:
    return get_db().execute("SELECT * FROM leads WHERE token = ?", (token,)).fetchone()


def find_duplicate(
    conn: sqlite3.Connection,
    email: str,
    message: str,
    external_id: str | None,
) -> sqlite3.Row | None:
    if external_id:
        row = conn.execute(
            "SELECT * FROM leads WHERE external_id = ?",
            (external_id,),
        ).fetchone()
        if row is not None:
            return row
    email_l = email.strip().lower()
    msg_h = message_hash(message or "")
    cutoff = to_iso(utc_now() - timedelta(minutes=5))
    rows = conn.execute(
        """
        SELECT * FROM leads
        WHERE lower(email) = ? AND created_at >= ?
        ORDER BY id DESC
        """,
        (email_l, cutoff),
    ).fetchall()
    for row in rows:
        if message_hash(row["message"] or "") == msg_h:
            return row
    return None


def send_smtp(to_email: str, subject: str, body: str) -> None:
    host = _env("SMTP_HOST")
    if not host:
        raise RuntimeError("SMTP is not configured.")
    from_email = _env("FROM_EMAIL")
    if not from_email:
        raise RuntimeError("FROM_EMAIL is required to send mail.")
    port = int(_env("SMTP_PORT") or "587")
    user = _env("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD", "")
    tls_raw = _env("SMTP_TLS") or "true"
    use_tls = tls_raw.lower() in {"1", "true", "yes", "on"}
    from_name = _env("FROM_NAME")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, from_email)) if from_name else from_email
    msg["To"] = to_email
    msg.set_content(body)

    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if use_tls:
            smtp.starttls()
        if user:
            smtp.login(user, password)
        smtp.send_message(msg)


def apply_first_reply(conn: sqlite3.Connection, lead_id: int) -> None:
    """Send first-reply if SMTP is set; otherwise mark ready. Same-request path."""
    lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if lead is None or lead["status"] not in ("new",):
        return
    now = utc_now_iso()
    if smtp_configured():
        try:
            send_smtp(lead["email"], first_reply_subject(lead), first_reply_body(lead))
            conn.execute(
                "UPDATE leads SET status = 'replied', replied_at = ? WHERE id = ? AND status = 'new'",
                (now, lead_id),
            )
            conn.commit()
        except Exception as exc:  # noqa: BLE001 — never log SMTP credentials
            app.logger.warning("SMTP send failed for lead_id=%s: %s", lead_id, type(exc).__name__)
            conn.execute(
                "UPDATE leads SET status = 'ready' WHERE id = ? AND status = 'new'",
                (lead_id,),
            )
            conn.commit()
    else:
        conn.execute(
            "UPDATE leads SET status = 'ready' WHERE id = ? AND status = 'new'",
            (lead_id,),
        )
        conn.commit()


def normalize_key(key: str) -> str:
    return re.sub(r"[\s_\-]+", "", (key or "").strip().lower())


FIELD_MAP = {
    "name": "name",
    "fullname": "name",
    "email": "email",
    "phone": "phone",
    "message": "message",
    "body": "message",
    "source": "source",
    "pageurl": "page_url",
    "externalid": "external_id",
}


def parse_lead_fields(source: dict) -> tuple[dict | None, dict]:
    """Return (parsed, extras). parsed is None when name/email invalid."""
    mapped: dict[str, str] = {}
    extras: dict = {}
    for key, value in source.items():
        if key is None:
            continue
        nk = normalize_key(str(key))
        field = FIELD_MAP.get(nk)
        if field and field not in mapped:
            mapped[field] = ("" if value is None else str(value)).strip()
        else:
            extras[str(key)] = value
    name = mapped.get("name", "")
    email = mapped.get("email", "")
    if not name or not valid_email(email):
        return None, extras
    return {
        "name": name,
        "email": email.strip(),
        "phone": mapped.get("phone", "") or None,
        "message": mapped.get("message", "") or None,
        "source": mapped.get("source", "") or None,
        "page_url": mapped.get("page_url", "") or None,
        "external_id": mapped.get("external_id", "") or None,
    }, extras


def extract_webhook_secret() -> str:
    header = (request.headers.get("X-FormFirst-Secret") or "").strip()
    if header:
        return header
    auth = request.headers.get("Authorization") or ""
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


def _safe_next(val: str | None) -> str:
    raw = (val or "").strip()
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    return url_for("index")


def insert_lead(data: dict, raw_payload: dict | None = None) -> tuple[sqlite3.Row, bool]:
    """Insert a lead or return existing duplicate. (row, created)."""
    conn = get_db()
    existing = find_duplicate(
        conn,
        data["email"],
        data.get("message") or "",
        data.get("external_id"),
    )
    if existing is not None:
        return existing, False
    token = secrets.token_hex(32)
    raw_json = json.dumps(raw_payload if raw_payload is not None else data, default=str)
    cur = conn.execute(
        """
        INSERT INTO leads (
            name, email, phone, message, source, page_url, external_id,
            raw_json, token, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
        """,
        (
            data["name"],
            data["email"],
            data.get("phone"),
            data.get("message"),
            data.get("source"),
            data.get("page_url"),
            data.get("external_id"),
            raw_json,
            token,
            utc_now_iso(),
        ),
    )
    conn.commit()
    lead_id = int(cur.lastrowid)
    apply_first_reply(conn, lead_id)
    lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    return lead, True


@app.get("/health")
def health() -> Response:
    return jsonify({"status": "ok", "smtp_configured": smtp_configured()})


@app.route("/login", methods=["GET", "POST"])
def login():
    nxt = _safe_next(request.values.get("next"))
    if not owner_password():
        return redirect(nxt)
    if session.get("owner"):
        return redirect(nxt)
    if request.method == "POST":
        provided = request.form.get("password") or ""
        if secrets_equal(provided, owner_password()):
            session["owner"] = True
            return redirect(nxt)
        flash("Wrong password.", "error")
    return render_template("login.html", next=nxt, public=True)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login") if owner_password() else url_for("index"))


@app.get("/")
def index() -> str:
    status = (request.args.get("status") or "").strip().lower()
    db = get_db()
    if status in STATUSES:
        leads = db.execute(
            "SELECT * FROM leads WHERE status = ? ORDER BY created_at DESC, id DESC",
            (status,),
        ).fetchall()
    else:
        status = ""
        leads = db.execute(
            "SELECT * FROM leads ORDER BY created_at DESC, id DESC"
        ).fetchall()
    counts_rows = db.execute(
        "SELECT status, COUNT(*) AS n FROM leads GROUP BY status"
    ).fetchall()
    counts = {row["status"]: row["n"] for row in counts_rows}
    counts["all"] = sum(counts.values())
    return render_template("index.html", leads=leads, status=status, counts=counts)


@app.get("/leads/<int:lead_id>")
def lead_detail(lead_id: int):
    lead = get_lead(lead_id)
    if lead is None:
        abort(404)
    return render_template(
        "lead.html",
        lead=lead,
        email_subject=first_reply_subject(lead),
        email_body=first_reply_body(lead),
        qualify_url=qualify_link(lead["token"]),
    )


@app.post("/leads/<int:lead_id>/send")
def send_now(lead_id: int):
    lead = get_lead(lead_id)
    if lead is None:
        abort(404)
    if lead["status"] not in ("new", "ready"):
        flash("Lead is not ready to send.", "error")
        return redirect(url_for("lead_detail", lead_id=lead_id))
    if not smtp_configured():
        flash("SMTP is not configured.", "error")
        return redirect(url_for("lead_detail", lead_id=lead_id))
    conn = get_db()
    try:
        send_smtp(lead["email"], first_reply_subject(lead), first_reply_body(lead))
        conn.execute(
            "UPDATE leads SET status = 'replied', replied_at = ? WHERE id = ?",
            (utc_now_iso(), lead_id),
        )
        conn.commit()
        flash("First reply sent.", "ok")
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("SMTP send failed for lead_id=%s: %s", lead_id, type(exc).__name__)
        flash("Send failed. Check SMTP settings.", "error")
    return redirect(url_for("lead_detail", lead_id=lead_id))


@app.post("/leads/<int:lead_id>/mark-sent")
def mark_sent(lead_id: int):
    lead = get_lead(lead_id)
    if lead is None:
        abort(404)
    if lead["status"] not in ("new", "ready"):
        flash("Lead cannot be marked sent.", "error")
        return redirect(url_for("lead_detail", lead_id=lead_id))
    conn = get_db()
    conn.execute(
        "UPDATE leads SET status = 'replied', replied_at = ? WHERE id = ?",
        (utc_now_iso(), lead_id),
    )
    conn.commit()
    flash("Marked as replied.", "ok")
    return redirect(url_for("lead_detail", lead_id=lead_id))


@app.post("/leads/<int:lead_id>/skip")
def skip_lead(lead_id: int):
    lead = get_lead(lead_id)
    if lead is None:
        abort(404)
    if lead["status"] in ("qualified", "skipped"):
        flash("Lead already closed.", "error")
        return redirect(url_for("lead_detail", lead_id=lead_id))
    conn = get_db()
    conn.execute(
        "UPDATE leads SET status = 'skipped' WHERE id = ?",
        (lead_id,),
    )
    conn.commit()
    flash("Lead skipped.", "ok")
    return redirect(url_for("lead_detail", lead_id=lead_id))


@app.route("/demo", methods=["GET", "POST"])
def demo():
    if request.method == "POST":
        source = {k: v for k, v in request.form.items()}
        parsed, extras = parse_lead_fields(source)
        if parsed is None:
            flash("Name and a valid email are required.", "error")
            return render_template("demo.html"), 400
        raw = dict(source)
        raw.update(extras)
        lead, created = insert_lead(parsed, raw_payload=raw)
        if created:
            flash(f"Lead created (id {lead['id']}).", "ok")
        else:
            flash(f"Idempotent: existing lead id {lead['id']}.", "ok")
        return redirect(url_for("lead_detail", lead_id=lead["id"]))
    return render_template("demo.html")


@app.post("/hooks/leads")
def ingest_webhook():
    expected = webhook_secret()
    if not expected:
        return jsonify({"status": "error", "error": "webhook disabled"}), 403
    provided = extract_webhook_secret()
    if not provided or not secrets_equal(provided, expected):
        return jsonify({"status": "error", "error": "unauthorized"}), 401

    raw_payload: dict
    if request.is_json:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"status": "error", "error": "invalid json"}), 400
        raw_payload = payload
    elif request.form:
        raw_payload = {k: v for k, v in request.form.items()}
    else:
        # Try JSON anyway (some clients omit content-type)
        payload = request.get_json(silent=True, force=True)
        if isinstance(payload, dict):
            raw_payload = payload
        else:
            return jsonify({"status": "error", "error": "invalid json"}), 400

    parsed, extras = parse_lead_fields(raw_payload)
    if parsed is None:
        return jsonify({"status": "error", "error": "missing name or email"}), 400

    lead, created = insert_lead(parsed, raw_payload=raw_payload)
    code = 201 if created else 200
    return jsonify({"status": "ok", "lead_id": lead["id"]}), code


@app.route("/q/<token>", methods=["GET", "POST"])
def qualify(token: str):
    lead = get_lead_by_token(token)
    if lead is None:
        abort(404)
    business = _env("BUSINESS_NAME") or "us"
    thank_you = f"Got it — {business} will follow up."

    already = lead["status"] == "qualified" or bool(lead["qualified_at"])
    if already:
        return render_template(
            "qualify.html",
            lead=lead,
            done=True,
            thank_you=thank_you,
            public=True,
        )

    if request.method == "POST":
        # Re-check to avoid overwrite races
        lead = get_lead_by_token(token)
        if lead is None:
            abort(404)
        if lead["status"] == "qualified" or lead["qualified_at"]:
            return render_template(
                "qualify.html",
                lead=lead,
                done=True,
                thank_you=thank_you,
                public=True,
            )
        q1 = (request.form.get("q1") or "").strip()
        q2 = (request.form.get("q2") or "").strip()
        now = utc_now_iso()
        conn = get_db()
        conn.execute(
            """
            UPDATE leads
            SET status = 'qualified', q1_answer = ?, q2_answer = ?, qualified_at = ?
            WHERE id = ? AND (qualified_at IS NULL)
            """,
            (q1, q2, now, lead["id"]),
        )
        conn.commit()
        lead = get_lead_by_token(token)

        notify = _env("OWNER_NOTIFY_EMAIL")
        if smtp_configured() and notify and lead is not None:
            try:
                send_smtp(notify, owner_summary_subject(lead), owner_summary_body(lead))
            except Exception as exc:  # noqa: BLE001
                app.logger.warning(
                    "Owner notify failed for lead_id=%s: %s",
                    lead["id"],
                    type(exc).__name__,
                )

        return render_template(
            "qualify.html",
            lead=lead,
            done=True,
            thank_you=thank_you,
            public=True,
        )

    return render_template(
        "qualify.html",
        lead=lead,
        done=False,
        thank_you=thank_you,
        public=True,
    )


@app.errorhandler(404)
def not_found(_e):
    return render_template("404.html", public=True), 404


init_db()


if __name__ == "__main__":
    port = int(_env("PORT") or "8080")
    app.run(host="0.0.0.0", port=port, debug=False)
