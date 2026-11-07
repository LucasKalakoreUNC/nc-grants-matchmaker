import io
import json
import os
import sqlite3
import unittest
import tempfile
from pathlib import Path
from datetime import date
from unittest.mock import Mock, patch

import app
import db
import notices


class BriefingTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA + notices.SCHEMA_SQL)

    def tearDown(self):
        self.conn.close()

    @patch("db.nc_today", return_value=date(2026, 11, 7))
    def test_briefing_uses_current_scoped_grants_and_keeps_notes_as_text(self, clock):
        profiles = {
            "state": {"name": "State agency", "category": "state", "eligible_applicant_types": ["State governments"]},
            "county": {"name": "County", "category": "county", "eligible_applicant_types": ["County governments"]},
        }
        for oid, eligibility in [("pursue", ["State governments"]), ("passed", ["State governments"]),
                                 ("possible", ["State governments"]), ("county-only", ["County governments"]),
                                 ("shared", ["Unrestricted"])]:
            db.upsert_opportunity(self.conn, {"id": oid, "title": oid, "status": "posted", "close_date": "2026-11-07",
                "eligibility": eligibility, "url": "https://www.grants.gov/", "agency": "Agency"})
            for office in profiles:
                db.save_matches(self.conn, office, [{"id": oid, "total_score": 20}])
        db.save_decision(self.conn, "state", {"opportunity_id": "pursue", "status": "Pursue", "notes": '<img src=x onerror="alert(1)">', "decided_by": "<Lead>"})
        db.save_decision(self.conn, "state", {"opportunity_id": "passed", "status": "Pass"})
        db.save_decision(self.conn, "state", {"opportunity_id": "removed", "status": "Pursue", "title": "Past grant"})
        with patch("pipeline.load_agency_profiles", return_value=profiles):
            digest = app.build_weekly_digest(self.conn, "all", category="state")
            all_digest = app.build_weekly_digest(self.conn, "all")
        self.assertEqual(digest["summary"]["pursuing_count"], 1)
        self.assertEqual(digest["pursuing_grants"][0]["days_to_close"], 0)
        self.assertEqual(digest["summary"]["closing_soon_count"], 1)
        self.assertEqual({item["id"] for item in digest["new_opportunities"]}, {"possible", "shared"})
        self.assertEqual(sum(item["id"] == "shared" for item in all_digest["new_opportunities"]), 1)
        self.assertIn("Possible match (20/100)", digest["text"])
        self.assertIn("&lt;img", digest["html"])
        self.assertNotIn('<img src=x', digest["html"])
        self.assertNotIn("<Lead>", digest["html"])

    def dispatch(self, host):
        handler = object.__new__(app.MatchmakerHandler)
        body = json.dumps({"email": "nobody@example.invalid", "office_id": "deq"}).encode()
        handler.path = "/api/digest/send"
        handler.headers = {"Content-Length": str(len(body)), "X-Admin-Token": "test-only-token"}
        handler.rfile = io.BytesIO(body)
        handler._send_json = Mock()
        digest = {"subject": "Test", "text": "Test", "html": "<p>Test</p>", "summary": {}}
        with patch("db.connect", return_value=self.conn), patch("app.build_weekly_digest", return_value=digest), patch.dict(os.environ, {"ADMIN_TOKEN": "test-only-token", "SMTP_HOST": host, "SMTP_USER": "", "SMTP_PASS": ""}):
            handler.do_POST()
        return handler._send_json.call_args.args[0]

    def test_unconfigured_email_does_not_claim_delivery(self):
        with patch("smtplib.SMTP") as smtp:
            result = self.dispatch("")
        smtp.assert_not_called()
        self.assertFalse(result["success"])
        self.assertFalse(result["sent"])
        self.assertIn("unavailable", result["message"])

    def test_configured_email_reports_mocked_delivery(self):
        with patch("smtplib.SMTP") as smtp:
            result = self.dispatch("mail.example.invalid")
        smtp.return_value.__enter__.return_value.send_message.assert_called_once()
        self.assertTrue(result["sent"])
        self.assertTrue(result["smtp_delivered"])

    def test_failed_email_does_not_claim_delivery(self):
        with patch("smtplib.SMTP", side_effect=OSError("mail unavailable")):
            result = self.dispatch("mail.example.invalid")
        self.assertFalse(result["sent"])
        self.assertFalse(result["success"])

    def test_static_files_stay_inside_public_directory(self):
        handler = object.__new__(app.MatchmakerHandler)
        handler._send_file = Mock()
        handler.send_error = Mock()
        with tempfile.TemporaryDirectory() as folder, patch.object(app, "BASE_DIR", folder):
            public = Path(folder) / "public"
            public.mkdir()
            (public / "asset.txt").write_text("Public")
            (Path(folder) / "private.txt").write_text("Private fixture")
            handler.path = "/asset.txt"
            handler.do_GET()
            handler._send_file.assert_called_once()
            handler._send_file.reset_mock()
            handler.path = "/../private.txt"
            handler.do_GET()
            handler._send_file.assert_not_called()
            handler.send_error.assert_called_once_with(404, "Not Found")

    def test_subscriber_addresses_are_not_public(self):
        handler = object.__new__(app.MatchmakerHandler)
        handler.path = "/api/subscribers/deq"
        handler.headers = {}
        handler._send_json = Mock()
        with patch.dict(os.environ, {"ADMIN_TOKEN": "test-only-token"}), patch("db.connect") as connect:
            handler.do_GET()
        connect.assert_not_called()
        handler._send_json.assert_called_once_with({"error": "Not allowed"}, 403)


if __name__ == "__main__":
    unittest.main()
