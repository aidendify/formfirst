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
