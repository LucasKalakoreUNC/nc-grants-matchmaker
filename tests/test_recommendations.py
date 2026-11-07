import json
import sqlite3
import unittest
import tempfile
from pathlib import Path
from datetime import date
from unittest.mock import patch

import app
import db
import pipeline
import programs
import notices


class RecommendationRegressionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA + programs.SCHEMA)
        self.profiles = pipeline.load_agency_profiles()

    def tearDown(self):
        self.conn.close()

    def test_school_district_picker_is_retired_but_dpi_remains(self):
        self.assertFalse(any(profile.get("category") == "k12" for profile in self.profiles.values()))
        self.assertNotIn("any_school_district", self.profiles)
        self.assertEqual(self.profiles["dpi"]["category"], "state")

    def test_state_applicant_wording(self):
        descriptions = [
            "State governments may apply.",
            "State or local government entity",
            "State and local governmental entities",
            "State/local governments",
            "State agencies may apply.",
            "State, county, city, or other political subdivision of a State",
        ]
        for description in descriptions:
            with self.subTest(description=description):
                self.assertTrue(programs.applicant_type_matches(description, self.profiles["commerce"]))

    def test_united_states_is_not_a_state_applicant_type(self):
        description = ("SBIR grants can be awarded only to domestic small business concerns. "
                       "The research must be performed in the United States or its possessions.")
        self.assertFalse(programs.applicant_type_matches(description, self.profiles["commerce"]))
        self.assertFalse(programs.applicant_type_matches("Federally Recognized Indian Tribal Governments.", self.profiles["commerce"]))

    def test_dpi_is_a_state_education_agency(self):
        description = "State educational agencies are eligible applicants."
        self.assertTrue(programs.applicant_type_matches(description, self.profiles["dpi"]))
        self.assertTrue(programs.applicant_type_matches("A State educational agency may apply.", self.profiles["dpi"]))
        self.assertFalse(programs.applicant_type_matches(description, self.profiles["commerce"]))
        self.assertFalse(programs.applicant_type_matches("Independent school districts", self.profiles["dpi"]))
        self.assertFalse(programs.applicant_type_matches("Institutions of higher education", self.profiles["commerce"]))

    def test_undated_federal_program_is_not_an_open_application(self):
        db.upsert_program(self.conn, "84.371", "Comprehensive Literacy Development", "Education", {
            "who_can_apply": "State educational agencies are eligible applicants.",
            "sam_url": "https://sam.gov/",
        })
        self.conn.execute("INSERT INTO program_matches VALUES (?, ?, ?, ?)", (
            "dpi", "84.371", 88.1, json.dumps({"cfda": "84.371", "title": "Comprehensive Literacy Development", "fit": 88.1})))
        items = app.other_items(self.conn, "dpi", self.profiles["dpi"])
        self.assertEqual(items, [])

    @patch("db.nc_today", return_value=date(2026, 11, 7))
    def test_current_applications_across_single_grouped_best_and_counts(self, clock):
        fixtures = [
            ("closed", "closed", "2026-12-01", None, None),
            ("archived", "archived", "2026-12-01", None, None),
            ("expired", "posted", "2026-11-06", None, None),
            ("stale-posted", "posted", None, None, "2020-02-01"),
            ("due-today", "posted", "2026-11-07", None, None),
            ("open", "posted", "2026-11-20", None, None),
            ("no-deadline", "posted", None, None, None),
            ("expired-forecast", "forecasted", None, "2026-09-01", "2026-10-01"),
            ("upcoming", "forecasted", None, "2026-12-01", "2027-01-01"),
            ("stale-placeholder", "forecasted", None, "2025-01-01", "2025-01-01"),
            ("future-placeholder", "forecasted", None, "2026-12-01", "2026-12-01"),
            ("unannounced", "forecasted", None, None, None),
            ("program-only", "program", "2026-12-01", None, None),
        ]
        for index, (oid, status, close, opening, due) in enumerate(fixtures):
            db.upsert_opportunity(self.conn, {
                "id": oid, "title": oid, "status": status, "close_date": close,
                "expected_open_date": opening, "expected_due_date": due,
                "days_to_close": 999,  # deliberately stale cached value
            })
            db.save_matches(self.conn, "dpi", [{"id": oid, "total_score": 100 - index,
                "status": "posted", "close_date": "2000-01-01", "expected_due_date": "2000-01-01"}])
        expected = {"due-today", "open", "no-deadline", "upcoming", "future-placeholder", "unannounced"}
        single = db.scored_for_office(self.conn, "dpi")
        self.assertEqual({item["id"] for item in single}, expected)
        self.assertEqual({item["id"] for item in db.scored_best(self.conn, ["dpi"])}, expected)
        # Expired, closed and archived high scores must not use up the top slots.
        self.assertEqual([item["id"] for item in db.top_per_office(self.conn, ["dpi"], per=2)], ["due-today", "open"])
        today_item = next(item for item in single if item["id"] == "due-today")
        self.assertEqual(today_item["days_to_close"], 0)
        self.assertEqual(next(item for item in single if item["id"] == "open")["days_to_close"], 13)
        counts = db.stats(self.conn)
        self.assertEqual(counts["available"], 6)
        self.assertEqual(counts["available_open"], 3)
        self.assertEqual(counts["available_forecasted"], 3)
        clock.return_value = date(2026, 11, 8)
        self.assertNotIn("due-today", {item["id"] for item in db.scored_for_office(self.conn, "dpi")})

    def test_new_posting_can_supersede_an_old_forecast(self):
        item = {"status": "posted", "close_date": None, "open_date": "2021-11-02",
                "expected_due_date": "2021-11-30"}
        self.assertFalse(db.application_available(item, date(2026, 11, 7)))
        item["open_date"] = "2026-11-01"
        self.assertTrue(db.application_available(item, date(2026, 11, 7)))

    @patch("db.nc_today", return_value=date(2026, 11, 7))
    def test_state_applications_require_a_current_sourced_deadline(self, clock):
        base = {"name": "State grant", "url": "https://www.nc.gov/", "fit": 80}
        records = [
            {**base, "id": "undated", "deadlines": []},
            {**base, "id": "expired", "deadlines": [{"date": "2026-11-06", "quote": "Due November 6"}]},
            {**base, "id": "invalid", "deadlines": [{"date": "not a date"}]},
            {**base, "id": "today", "deadlines": [{"date": "2026-11-07", "quote": "Due November 7"}]},
            {**base, "id": "next-round", "deadlines": [
                {"date": "2025-01-01", "quote": "Old round"},
                {"date": "2026-12-01", "quote": "Next round due December 1"}]},
        ]
        with patch("app.state_programs.for_office", return_value=records):
            items = app.other_items(self.conn, "schools", {"category": "k12"})
        self.assertEqual([item["id"] for item in items], ["st-today", "st-next-round"])
        self.assertEqual(items[0]["days_to_close"], 0)
        self.assertEqual(items[1]["deadline_quote"], "Next round due December 1")

    def test_catalog_counts_keep_funding_sources_separate(self):
        for source, record_id, kind in [
            ("nc-state-program", "state-a", "North Carolina state program"),
            ("nc-state-program", "state-b", "North Carolina state program"),
            ("nc-state-program", "foundation-a", "Private foundation (NC)"),
            ("sam.gov-listing", "federal-a", "Private foundation (NC)"),
        ]:
            db.upsert_raw(self.conn, source, record_id, {"kind": kind})
        # An updated record must not count as a second program.
        db.upsert_raw(self.conn, "nc-state-program", "state-a", {"kind": "North Carolina state program", "updated": True})
        counts = db.stats(self.conn)
        self.assertEqual(counts["state_programs"], 2)
        self.assertEqual(counts["foundations"], 1)
        self.assertEqual(counts["opportunities"], 0)
        self.assertEqual(counts["programs"], 0)

    @patch("db.nc_today", return_value=date(2026, 11, 7))
    def test_pruning_removes_inventory_and_caches_but_preserves_decisions(self, clock):
        self.conn.executescript(notices.SCHEMA_SQL)
        item = {"id": "expired", "status": "posted", "close_date": "2026-11-30"}
        db.upsert_opportunity(self.conn, item, source="grants.gov")
        db.upsert_opportunity(self.conn, {**item, "id": "current"})
        db.upsert_raw(self.conn, "grants.gov", "expired", {"oppStatus": "posted", "closeDate": "11/30/2026"})
        db.upsert_raw(self.conn, "grants.gov-detail", "expired", {"detail": True})
        db.save_matches(self.conn, "dpi", [{"id": "expired", "total_score": 80}])
        db.save_decision(self.conn, "dpi", {"opportunity_id": "expired", "status": "Watch"})
        db.log_fetch(self.conn, "grants.gov-detail", "expired", 1)
        self.conn.execute("INSERT INTO notice_files (opportunity_id, attachment_id) VALUES ('expired','file')")
        self.conn.execute("INSERT INTO notice_requirements (opportunity_id) VALUES ('expired')")
        # Simulate a legacy closed record already present before this upgrade.
        self.conn.execute("UPDATE opportunities SET status='closed' WHERE id='expired'")
        self.assertEqual(db.prune_unavailable(self.conn), 1)
        self.assertEqual([r[0] for r in self.conn.execute("SELECT id FROM opportunities")], ["current"])
        for table in ["matches", "opportunity_sources", "notice_files", "notice_requirements", "raw_records", "fetch_log"]:
            with self.subTest(table=table):
                self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 1)
        self.assertEqual(db.prune_unavailable(self.conn), 0)

    @patch("db.nc_today", return_value=date(2026, 11, 7))
    def test_ingestion_does_not_store_closed_or_expired_notices(self, clock):
        for status, close in [("closed", "2026-12-01"), ("archived", None), ("posted", "2026-11-06")]:
            with self.subTest(status=status):
                self.assertEqual(db.upsert_opportunity(self.conn, {"id": status, "status": status, "close_date": close}), "excluded")
        self.assertEqual(db.upsert_raw(self.conn, "grants.gov", "raw-expired", {
            "oppStatus": "posted", "closeDate": "11/06/2026"}), "excluded")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0], 0)
        # A legitimate later round under the same ID may be stored again.
        self.assertEqual(db.upsert_opportunity(self.conn, {"id": "posted", "status": "posted", "close_date": "2026-12-01"}), "new")

    @patch("db.nc_today", return_value=date(2026, 11, 7))
    def test_totals_follow_selection_and_count_each_grant_once(self, clock):
        profiles = {
            "state-a": {"category": "state", "eligible_applicant_types": ["State governments"]},
            "state-b": {"category": "state", "eligible_applicant_types": ["State governments"]},
            "nonprofit": {"category": "nonprofit", "eligible_applicant_types": ["Nonprofits"]},
            "county": {"category": "county", "eligible_applicant_types": ["County governments"]},
        }
        for oid, eligibility in [("state", ["State governments"]), ("nonprofit", ["Nonprofits"]),
                                 ("county", ["County governments"]), ("check-notice", [])]:
            db.upsert_opportunity(self.conn, {"id": oid, "status": "posted", "close_date": "2026-12-01", "eligibility": eligibility})
            for profile in profiles:
                db.save_matches(self.conn, profile, [{"id": oid, "total_score": 20}])
        with patch("app.other_items", return_value=[]):
            self.assertEqual(app.selection_counts(self.conn, profiles)["available"], 4)
            self.assertEqual(app.selection_counts(self.conn, profiles, category="state")["available"], 2)
            self.assertEqual(app.selection_counts(self.conn, profiles, office="state-a")["available"], 2)
            self.assertEqual(app.selection_counts(self.conn, profiles, category="nonprofit")["available"], 2)
        self.assertEqual({item["id"] for item in app.scored_for_profile(self.conn, "state-a", profiles["state-a"])}, {"state", "check-notice"})

    @patch("db.nc_today", return_value=date(2026, 11, 7))
    def test_database_connection_prunes_again_on_the_next_day(self, clock):
        db.upsert_opportunity(self.conn, {"id": "due-today", "status": "posted", "close_date": "2026-11-07"})
        db.upsert_opportunity(self.conn, {"id": "later", "status": "posted", "close_date": "2026-12-01"})
        self.conn.commit()
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "grants.db")
            disk = sqlite3.connect(path)
            self.conn.backup(disk)
            disk.close()
            with patch.object(db, "DB_PATH", path), patch.object(db, "_initialized", False), patch.object(db, "_last_pruned", None):
                first = db.connect()
                self.assertEqual(first.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 2)
                first.close()
                clock.return_value = date(2026, 11, 8)
                second = db.connect()
                self.assertEqual([r[0] for r in second.execute("SELECT id FROM opportunities")], ["later"])
                second.close()


if __name__ == "__main__":
    unittest.main()
