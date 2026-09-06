"""Local Flask test client covering PRD §12 as much as possible without Compose."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

# Ensure env before importing app
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["OWNER_PASSWORD"] = "testpass"
os.environ["WEBHOOK_SECRET"] = "test-secret"
os.environ["PUBLIC_BASE_URL"] = "http://localhost:8080"
os.environ["BUSINESS_NAME"] = "Harbor HVAC"
os.environ["MARKETING_URL"] = ""
os.environ.pop("SMTP_HOST", None)
os.environ.pop("OWNER_NOTIFY_EMAIL", None)
os.environ.pop("FROM_NAME", None)
os.environ.pop("FROM_EMAIL", None)

SAMPLE = {
    "name": "Priya Sharma",
    "email": "priya.sharma@example.com",
    "phone": "+1-555-0100",
    "message": "Need a quote for a 3-ton AC install next week.",
    "source": "homepage",
    "page_url": "https://example.com/contact",
    "external_id": "fs-abc123",
}


class FormFirstTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = str(Path(self._tmpdir.name) / "test.db")
        os.environ["DATABASE_PATH"] = db_path
        os.environ["OWNER_PASSWORD"] = "testpass"
        os.environ["WEBHOOK_SECRET"] = "test-secret"
        os.environ["PUBLIC_BASE_URL"] = "http://localhost:8080"
        os.environ["BUSINESS_NAME"] = "Harbor HVAC"
os.environ["MARKETING_URL"] = ""
        os.environ.pop("SMTP_HOST", None)
        os.environ.pop("OWNER_NOTIFY_EMAIL", None)

        import importlib
        import app as app_module

        importlib.reload(app_module)
        self.app_module = app_module
        self.app = app_module.app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        with self.app.app_context():
            app_module.init_db()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _login(self) -> None:
        r = self.client.post(
            "/login",
            data={"password": "testpass", "next": "/"},
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303))

    def _post_lead(self, payload: dict | None = None, secret: str | None = "test-secret") -> object:
        headers = {"Content-Type": "application/json"}
        if secret is not None:
            headers["X-FormFirst-Secret"] = secret
        body = json.dumps(payload if payload is not None else SAMPLE)
        return self.client.post("/hooks/leads", data=body, headers=headers)

    # 1. Health
    def test_health(self) -> None:
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertIs(data["smtp_configured"], False)

    # 2. Auth: / redirects to login; /health and /q/token public
    def test_auth_gate(self) -> None:
        r = self.client.get("/", follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        self.assertIn("/login", r.headers.get("Location", ""))

        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)

        # create a lead via webhook (no owner session)
        resp = self._post_lead()
        self.assertIn(resp.status_code, (200, 201))
        lead_id = resp.get_json()["lead_id"]
        with self.app.app_context():
            lead = self.app_module.get_lead(lead_id)
            token = lead["token"]
        r = self.client.get(f"/q/{token}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"What are you looking for help with?", r.data)

    # 3. Webhook auth + success
    def test_webhook_auth_and_ingest(self) -> None:
        r = self._post_lead()
        self.assertIn(r.status_code, (200, 201))
        data = r.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("lead_id", data)

        r = self._post_lead(secret="wrong")
        self.assertIn(r.status_code, (401, 403))

        r = self._post_lead(secret=None)
        self.assertIn(r.status_code, (401, 403))

        # empty WEBHOOK_SECRET → 403
        os.environ["WEBHOOK_SECRET"] = ""
        r = self._post_lead(
            payload={**SAMPLE, "external_id": "empty-secret-case"},
            secret="anything",
        )
        self.assertEqual(r.status_code, 403)
        os.environ["WEBHOOK_SECRET"] = "test-secret"

        r = self.client.post(
            "/hooks/leads",
            data="not-json",
            headers={
                "Content-Type": "application/json",
                "X-FormFirst-Secret": "test-secret",
            },
        )
        self.assertEqual(r.status_code, 400)

        r = self._post_lead(payload={"name": "No Email"})
        self.assertEqual(r.status_code, 400)

    # 4. Idempotency by external_id
    def test_idempotency_external_id(self) -> None:
        r1 = self._post_lead()
        r2 = self._post_lead()
        self.assertIn(r1.status_code, (200, 201))
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.get_json()["lead_id"], r2.get_json()["lead_id"])
        with self.app.app_context():
            db = self.app_module.get_db()
            n = db.execute(
                "SELECT COUNT(*) AS c FROM leads WHERE external_id = ?",
                ("fs-abc123",),
            ).fetchone()["c"]
            self.assertEqual(n, 1)

    # 5. First-touch copy includes qualify URL + both Qs; send buttons hidden
    def test_first_reply_copy(self) -> None:
        r = self._post_lead()
        lead_id = r.get_json()["lead_id"]
        self._login()
        page = self.client.get(f"/leads/{lead_id}")
        self.assertEqual(page.status_code, 200)
        html = page.data.decode("utf-8")
        with self.app.app_context():
            lead = self.app_module.get_lead(lead_id)
            token = lead["token"]
            self.assertEqual(lead["status"], "ready")
        self.assertIn(f"http://localhost:8080/q/{token}", html)
        self.assertIn("What are you looking for help with?", html)
        self.assertIn("When do you need this done?", html)
        self.assertNotIn(">Send now<", html)
        self.assertIn("SMTP is not configured", html)
        self.assertIn("Copy reply", html)
        self.assertIn("Copy link", html)

    # 6. Qualify happy path + no overwrite
    def test_qualify_path(self) -> None:
        r = self._post_lead(payload={**SAMPLE, "external_id": "qual-1"})
        lead_id = r.get_json()["lead_id"]
        with self.app.app_context():
            token = self.app_module.get_lead(lead_id)["token"]

        page = self.client.get(f"/q/{token}")
        self.assertEqual(page.status_code, 200)

        post = self.client.post(
            f"/q/{token}",
            data={"q1": "AC install", "q2": "Next week"},
            follow_redirects=False,
        )
        self.assertEqual(post.status_code, 200)
        self.assertIn(b"Got it", post.data)
        self.assertIn(b"Harbor HVAC", post.data)

        with self.app.app_context():
            lead = self.app_module.get_lead(lead_id)
            self.assertEqual(lead["status"], "qualified")
            self.assertEqual(lead["q1_answer"], "AC install")
            self.assertEqual(lead["q2_answer"], "Next week")

        # reload / resubmit does not overwrite
        again = self.client.post(
            f"/q/{token}",
            data={"q1": "CHANGED", "q2": "CHANGED"},
        )
        self.assertEqual(again.status_code, 200)
        self.assertIn(b"Got it", again.data)
        with self.app.app_context():
            lead = self.app_module.get_lead(lead_id)
            self.assertEqual(lead["q1_answer"], "AC install")
            self.assertEqual(lead["q2_answer"], "Next week")

        self._login()
        detail = self.client.get(f"/leads/{lead_id}")
        self.assertIn(b"AC install", detail.data)
        self.assertIn(b"Next week", detail.data)

        bad = self.client.get("/q/not-a-real-token")
        self.assertEqual(bad.status_code, 404)

    # 7. Skip
    def test_skip(self) -> None:
        r = self._post_lead(payload={**SAMPLE, "external_id": "skip-1"})
        lead_id = r.get_json()["lead_id"]
        self._login()
        post = self.client.post(f"/leads/{lead_id}/skip", follow_redirects=False)
        self.assertIn(post.status_code, (302, 303))
        with self.app.app_context():
            self.assertEqual(self.app_module.get_lead(lead_id)["status"], "skipped")

    # 8. No sequences table / no Day-3 drip
    def test_no_drip_schema_or_code(self) -> None:
        with self.app.app_context():
            db = self.app_module.get_db()
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertIn("leads", tables)
            self.assertNotIn("sequences", tables)

        src = Path(self.app_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("sequences", src.lower())
        self.assertNotIn("day-3", src.lower())
        self.assertNotIn("day_3", src.lower())
        self.assertNotIn("Day 3", src)
        self.assertNotIn("Day 7", src)
        self.assertNotIn("still interested", src.lower())

    # 9. Empty MARKETING_URL → no Powered by FormFirst
    def test_no_footer_without_marketing_url(self) -> None:
        self._login()
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"Powered by FormFirst", r.data)

    # 10. Demo form when logged in
    def test_demo(self) -> None:
        self._login()
        get = self.client.get("/demo")
        self.assertEqual(get.status_code, 200)
        post = self.client.post(
            "/demo",
            data={
                "name": "Demo User",
                "email": "demo@example.com",
                "phone": "555",
                "message": "Demo message",
                "source": "demo",
            },
            follow_redirects=False,
        )
        self.assertIn(post.status_code, (302, 303))
        self.assertIn("/leads/", post.headers.get("Location", ""))
        with self.app.app_context():
            db = self.app_module.get_db()
            row = db.execute(
                "SELECT * FROM leads WHERE email = ?",
                ("demo@example.com",),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "ready")

    def test_form_urlencoded_webhook(self) -> None:
        r = self.client.post(
            "/hooks/leads",
            data={
                "full_name": "Form User",
                "email": "form@example.com",
                "body": "urlencoded body",
                "external_id": "form-1",
            },
            headers={"X-FormFirst-Secret": "test-secret"},
        )
        self.assertIn(r.status_code, (200, 201))
        self.assertEqual(r.get_json()["status"], "ok")

    def test_bearer_auth(self) -> None:
        r = self.client.post(
            "/hooks/leads",
            data=json.dumps({**SAMPLE, "external_id": "bearer-1"}),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer test-secret",
            },
        )
        self.assertIn(r.status_code, (200, 201))

    def test_mark_sent(self) -> None:
        r = self._post_lead(payload={**SAMPLE, "external_id": "mark-1"})
        lead_id = r.get_json()["lead_id"]
        self._login()
        post = self.client.post(f"/leads/{lead_id}/mark-sent", follow_redirects=False)
        self.assertIn(post.status_code, (302, 303))
        with self.app.app_context():
            self.assertEqual(self.app_module.get_lead(lead_id)["status"], "replied")


if __name__ == "__main__":
    unittest.main()
