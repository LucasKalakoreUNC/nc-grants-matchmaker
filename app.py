#!/usr/bin/env python3
"""
NC Grants Matchmaker - web server.

Serves public/index.html and a small JSON API backed by the local database
(see db.py). MODE=job runs the pipeline once instead of serving.
"""

import os
import sys
import json
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from datetime import datetime
import csv
import io
import re

import pipeline
import db
import notices
import ai
import programs


# Fields only the grant panel needs; left out of list responses to keep them
# small and sent by /api/opportunity/<id>/sources instead.
DETAIL_FIELDS = ("description", "past_awardees", "past_award_stats", "required_documents", "action_steps",
                 "eligibility_text", "eligibility", "complexity_factors", "positive_drivers")


def slim(items):
    return [{k: v for k, v in it.items() if k not in DETAIL_FIELDS} for it in items]


def load_scored(agency_id):
    """Scored grants for one office, best first, read from the database."""
    conn = db.connect()
    try:
        return db.scored_for_office(conn, agency_id)
    finally:
        conn.close()

PORT = int(os.environ.get("PORT", 8000))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

class MatchmakerHandler(BaseHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def _send_json(self, data, status_code=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if len(body) > 2048 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            import gzip
            body = gzip.compress(body, compresslevel=5)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_csv(self, filename, csv_content):
        body = csv_content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, file_path, content_type="text/html; charset=utf-8"):
        if not os.path.exists(file_path):
            self.send_error(404, "File not found")
            return
        with open(file_path, "rb") as f:
            content = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # 1. API: List all profiles with categories (State, County, Education, Business, Municipal)
        if path == "/api/profiles":
            profiles = pipeline.load_agency_profiles()
            res = [
                {
                    "agency_id": k,
                    "name": v.get("name"),
                    "short_name": v.get("short_name"),
                    "category": v.get("category", "state"),
                    "citation": v.get("source_citation"),
                    "draft": v.get("profile_status") == "draft",
                    "source_url": v.get("source_url")
                }
                for k, v in profiles.items()
            ]
            self._send_json(res)
            return

        # 3. API: Get specific agency profile
        if path.startswith("/api/profiles/"):
            agency_id = path.replace("/api/profiles/", "").strip()
            profile_file = os.path.join(DATA_DIR, "profiles", f"{agency_id}.json")
            if os.path.exists(profile_file):
                with open(profile_file, "r", encoding="utf-8") as f:
                    self._send_json(json.load(f))
            else:
                self._send_json({"error": "Profile not found"}, 404)
            return

        # 4. API: Get scored opportunities for agency
        if path.startswith("/api/opportunity/") and path.endswith("/sources"):
            opp_id = path.split("/")[3]
            conn = notices.connect()
            try:
                row = conn.execute("SELECT cfda, url, data FROM opportunities WHERE id=?", (opp_id,)).fetchone()
                program = programs.program_record(conn, row["cfda"]) if row else None
                if not row:
                    self._send_json({"error": "Not found"}, 404)
                    return
                links = [{"name": "Official listing on Grants.gov", "url": row["url"]}]
                if row["cfda"]:
                    links.append({"name": f"Program details on SAM.gov (Assistance Listing {row['cfda']})",
                                  "url": f"https://sam.gov/search/?index=cfda&keywords={urllib.parse.quote(row['cfda'])}"})
                full = json.loads(row["data"])
                self._send_json({
                    "detail": {k: full.get(k) for k in DETAIL_FIELDS},
                    "files": notices.files_for(conn, opp_id),
                    "program": program,
                    "requirements": notices.requirements_for(conn, opp_id),
                    "review": ai.review_for(urllib.parse.parse_qs(parsed.query).get("office", [""])[0], opp_id),
                    "links": links,
                })
            finally:
                conn.close()
            return

        if path.startswith("/api/programs/"):
            conn = db.connect()
            try:
                self._send_json(programs.programs_for_office(conn, path.rsplit("/", 1)[-1]))
            finally:
                conn.close()
            return

        if path in ("/api/scored/all", "/api/decisions/all"):
            category = urllib.parse.parse_qs(parsed.query).get("category", ["all"])[0]
            profiles = pipeline.load_agency_profiles()
            ids = [k for k, v in profiles.items() if category in ("all", "") or v.get("category", "state") == category]
            conn = db.connect()
            try:
                if path == "/api/scored/all":
                    # Each organization's own top matches, grouped by organization in the page
                    per = int(urllib.parse.parse_qs(parsed.query).get("per", ["25"])[0])
                    items = db.top_per_office(conn, ids, per=per)
                    for it in items:
                        it["best_office_name"] = profiles.get(it["best_office_id"], {}).get("name", "")
                    self._send_json(slim(items))
                else:
                    self._send_json(db.get_decisions_for(conn, ids))
            finally:
                conn.close()
            return

        if path.startswith("/api/scored/"):
            agency_id = path.replace("/api/scored/", "").strip()
            self._send_json(slim(load_scored(agency_id)))
            return

        # 5. API: Get decisions for agency
        if path.startswith("/api/decisions/"):
            agency_id = path.replace("/api/decisions/", "").strip()
            conn = db.connect()
            try:
                self._send_json(db.get_decisions(conn, agency_id))
            finally:
                conn.close()
            return

        if path == "/api/db/stats":
            conn = db.connect()
            try:
                self._send_json(db.stats(conn))
            finally:
                conn.close()
            return

        if path == "/api/runs/latest":
            latest_run_file = os.path.join(DATA_DIR, "runs", "latest.json")
            if os.path.exists(latest_run_file):
                with open(latest_run_file, "r", encoding="utf-8") as f:
                    self._send_json(json.load(f))
            else:
                self._send_json({"status": "No runs recorded yet"}, 200)
            return

        # 8. API: Export shortlist to CSV (§5.6)
        if path.startswith("/api/export/"):
            parts = path.replace("/api/export/", "").split("/")
            agency_id = parts[0]
            items = load_scored(agency_id)
            if not items:
                self._send_json({"error": "No scored data found for agency"}, 404)
                return

            conn = db.connect()
            try:
                decisions = {d["opportunity_id"]: d for d in db.get_decisions(conn, agency_id)["decisions"]}
            finally:
                conn.close()
            dollars = lambda v: f"${int(v):,}" if v else ""
            label = lambda sc: "Strong match" if sc >= 70 else "Good match" if sc >= 55 else "Possible match"
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow([
                "Rank", "Score", "Match", "Fit with strategic plan", "Grant", "Opportunity number", "Funding agency",
                "Status", "Opens (expected, if forecasted)", "Deadline (expected, if forecasted)", "Days left", "Award up to", "Typical past award", "Estimated staff hours",
                "Matching funds", "Matched goal", "Goal citation", "Why it fits", "Your decision", "Decision notes",
                "Official listing",
            ])
            for i, it in enumerate(items[:100]):
                typical = (it.get("typical_award") or {}).get("median")
                dec = decisions.get(str(it.get("id")), {})
                cs = it.get("cost_share_required")
                writer.writerow([
                    i + 1, it.get("total_score", 0), label(it.get("total_score", 0)),
                    round((it.get("score_breakdown") or {}).get("relevancy", 0)),
                    it.get("title", ""), it.get("number", ""), it.get("agency", ""), it.get("status", ""),
                    "Open now" if it.get("status") != "forecasted" else (it.get("expected_open_date") or "Not announced"),
                    it.get("close_date") or (it.get("expected_due_date")
                                             if (it.get("expected_due_date") or "") > (it.get("expected_open_date") or "") else ""),
                    "" if it.get("days_to_close") is None else it.get("days_to_close"),
                    dollars(it.get("award_ceiling") or it.get("award_estimate")), dollars(typical),
                    it.get("est_completion_hours", ""),
                    "Required" if cs is True else "Not required" if cs is False else "See notice",
                    it.get("matched_goal_title", ""), it.get("matched_goal_citation", ""), it.get("reason", ""),
                    dec.get("status", ""), dec.get("notes", ""), it.get("url", ""),
                ])
            filename = f"NC_{agency_id.upper()}_Federal_Grants_Shortlist_{datetime.now().strftime('%Y%m%d')}.csv"
            self._send_csv(filename, output.getvalue())
            return

        # 9. Web App Main HTML page
        if path == "/" or path == "/index.html":
            index_path = os.path.join(BASE_DIR, "public", "index.html")
            self._send_file(index_path, "text/html; charset=utf-8")
            return

        # Static assets
        safe_rel_path = path.lstrip("/")
        candidate = os.path.join(BASE_DIR, "public", safe_rel_path)
        if os.path.exists(candidate) and os.path.isfile(candidate):
            content_type = "text/plain"
            if candidate.endswith(".js"): content_type = "application/javascript"
            elif candidate.endswith(".css"): content_type = "text/css"
            elif candidate.endswith(".svg"): content_type = "image/svg+xml"
            self._send_file(candidate, content_type)
            return

        self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {}

        # 2. Save Staff Decision (§5.6 & §5.0)
        if path.startswith("/api/decisions/"):
            agency_id = path.replace("/api/decisions/", "").strip()
            opp_id = str(payload.get("opportunity_id", ""))
            if not opp_id:
                self._send_json({"error": "opportunity_id is required"}, 400)
                return
            conn = db.connect()
            try:
                db.save_decision(conn, agency_id, {
                    "opportunity_id": opp_id,
                    "number": payload.get("number", ""),
                    "title": payload.get("title", ""),
                    "status": payload.get("status", "Pursue"),
                    "notes": payload.get("notes", ""),
                    "decided_by": payload.get("decided_by", "Grants staff"),
                    "decided_at": datetime.now().isoformat(timespec="seconds"),
                })
                conn.commit()
            finally:
                conn.close()

            self._send_json({"success": True, "agency_id": agency_id, "saved_decision": payload})
            return

        # 3. Trigger Ingestion / Refresh Pipeline on Demand (§5.7)
        if path == "/api/pipeline/run":
            print(f"[API] Manual trigger of daily refresh pipeline initiated by user...")
            try:
                run_log = pipeline.run_pipeline()
                self._send_json({"success": True, "run_log": run_log})
            except Exception as e:
                self._send_json({"success": False, "error": str(e)}, 500)
            return

        # 4. Multi-Equation Scoring & Dynamic Sliders (§8.1 & Ranking Complexity)
        if path.startswith("/api/rescore/"):
            agency_id = path.replace("/api/rescore/", "").strip()
            scoring_model = payload.get("scoring_model", "balanced")
            weights = payload.get("weights", {
                "semantic": 0.45,
                "keyword": 0.25,
                "eligibility": 0.15,
                "timing": 0.10,
                "affinity": 0.05
            })
            profiles = pipeline.load_agency_profiles()
            profile = profiles.get(agency_id)
            if not profile:
                self._send_json({"error": "Profile not found"}, 404)
                return

            conn = db.connect()
            try:
                normalized = db.opportunities(conn)
            finally:
                conn.close()
            if not normalized:
                self._send_json({"error": "No opportunities found"}, 404)
                return

            rescored = []
            for opp in normalized:
                score_meta = pipeline.calculate_score(opp, profile, weights, scoring_model=scoring_model)
                item = dict(opp)
                item.update(score_meta)
                rescored.append(item)

            rescored.sort(key=lambda x: x["total_score"], reverse=True)
            self._send_json({
                "agency_id": agency_id,
                "scoring_model_used": scoring_model,
                "weights_used": weights,
                "count": len(rescored),
                "items": rescored
            })
            return

        self.send_error(404, "Not Found")

def start_refresh_timer():
    """If REFRESH_HOURS is set, run the pipeline in the background on that interval.

    Lets one container both serve the site and keep the database fresh, so the
    SQLite file never has to be shared between containers."""
    hours = float(os.environ.get("REFRESH_HOURS", "0") or 0)
    if hours <= 0:
        return
    import threading, time

    def loop():
        while True:
            time.sleep(hours * 3600)
            try:
                pipeline.run_pipeline()
            except Exception as e:
                print(f"[WARN] Scheduled refresh failed: {e}")

    threading.Thread(target=loop, daemon=True).start()
    print(f"Automatic refresh every {hours:g} hours")


def run_server():
    start_refresh_timer()
    try:
        print(f"Requirement files loaded: {notices.load_requirements_dir(notices.connect())}")
    except Exception as e:
        print(f"[WARN] Could not load requirement files: {e}")
    os.makedirs(os.path.join(BASE_DIR, "public"), exist_ok=True)
    server_address = ("", PORT)
    httpd = ThreadingHTTPServer(server_address, MatchmakerHandler)
    print(f"NC Grants Matchmaker running at http://localhost:{PORT} "
          f"({len(pipeline.load_agency_profiles())} organization profiles)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        httpd.server_close()

if __name__ == "__main__":
    mode = os.environ.get("MODE", "app")
    if mode == "job":
        pipeline.run_pipeline()
    else:
        run_server()
