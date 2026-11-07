#!/usr/bin/env python3
"""
Federal Funds Matchmaker - Web Application Server
Serves the Staff-Facing UI and REST API endpoints on port 8000.
Compliant with Saturday Starter Kit §4 Step 7 & §5.6.
"""

import os
import sys
import json
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
import csv
import io

import pipeline

PORT = int(os.environ.get("PORT", 8000))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

class MatchmakerHandler(BaseHTTPRequestHandler):
    def end_headers(self):
        # Allow cross-origin and disable caching for live demo
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
        query = urllib.parse.parse_qs(parsed.query)

        # 1. API: List all agency profiles
        if path == "/api/profiles":
            profiles = pipeline.load_agency_profiles()
            res = [{"agency_id": k, "name": v.get("name"), "short_name": v.get("short_name"), "citation": v.get("source_citation")} for k, v in profiles.items()]
            self._send_json(res)
            return

        # 2. API: Get specific agency profile
        if path.startswith("/api/profiles/"):
            agency_id = path.replace("/api/profiles/", "").strip()
            profile_file = os.path.join(DATA_DIR, "profiles", f"{agency_id}.json")
            if os.path.exists(profile_file):
                with open(profile_file, "r", encoding="utf-8") as f:
                    self._send_json(json.load(f))
            else:
                self._send_json({"error": "Profile not found"}, 404)
            return

        # 3. API: Get scored opportunities for agency
        if path.startswith("/api/scored/"):
            agency_id = path.replace("/api/scored/", "").strip()
            scored_file = os.path.join(DATA_DIR, "scored", agency_id, "latest.json")
            if os.path.exists(scored_file):
                with open(scored_file, "r", encoding="utf-8") as f:
                    self._send_json(json.load(f))
            else:
                self._send_json([], 200)
            return

        # 4. API: Get decisions for agency
        if path.startswith("/api/decisions/"):
            agency_id = path.replace("/api/decisions/", "").strip()
            dec_file = os.path.join(DATA_DIR, "decisions", f"{agency_id}.json")
            if os.path.exists(dec_file):
                with open(dec_file, "r", encoding="utf-8") as f:
                    self._send_json(json.load(f))
            else:
                self._send_json({"agency_id": agency_id, "decisions": []}, 200)
            return

        # 5. API: Get evaluation metrics (§6)
        if path == "/api/evaluation":
            eval_file = os.path.join(DATA_DIR, "eval", "labels.json")
            if os.path.exists(eval_file):
                with open(eval_file, "r", encoding="utf-8") as f:
                    self._send_json(json.load(f))
            else:
                self._send_json({"error": "Evaluation labels not found"}, 404)
            return

        # 6. API: Get run status / latest pipeline log
        if path == "/api/runs/latest":
            latest_run_file = os.path.join(DATA_DIR, "runs", "latest.json")
            if os.path.exists(latest_run_file):
                with open(latest_run_file, "r", encoding="utf-8") as f:
                    self._send_json(json.load(f))
            else:
                self._send_json({"status": "No runs recorded yet"}, 200)
            return

        # 7. API: Export shortlist to CSV (§5.6)
        if path.startswith("/api/export/"):
            parts = path.replace("/api/export/", "").split("/")
            agency_id = parts[0]
            scored_file = os.path.join(DATA_DIR, "scored", agency_id, "latest.json")
            if not os.path.exists(scored_file):
                self._send_json({"error": "No scored data found for agency"}, 404)
                return

            with open(scored_file, "r", encoding="utf-8") as f:
                items = json.load(f)

            # Build CSV
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow([
                "Rank", "Total Score", "Confidence", "Opportunity Title",
                "Opportunity Number", "Funding Agency", "Status",
                "Days to Close", "Close Date", "Award Ceiling ($)",
                "Matched Strategic Goal", "Match Reason", "Official URL"
            ])
            for i, it in enumerate(items[:50]):
                writer.writerow([
                    i + 1,
                    it.get("total_score", 0),
                    it.get("confidence", "Medium"),
                    it.get("title", ""),
                    it.get("number", ""),
                    it.get("agency", ""),
                    it.get("status", ""),
                    it.get("days_to_close", ""),
                    it.get("close_date", ""),
                    f"${it.get('award_ceiling', 0):,}",
                    it.get("matched_goal_title", ""),
                    it.get("reason", ""),
                    it.get("url", "")
                ])
            filename = f"NC_{agency_id.upper()}_Federal_Grants_Shortlist_{datetime.now().strftime('%Y%m%d')}.csv"
            self._send_csv(filename, output.getvalue())
            return

        # 8. Web App Main HTML page
        if path == "/" or path == "/index.html":
            index_path = os.path.join(BASE_DIR, "public", "index.html")
            self._send_file(index_path, "text/html; charset=utf-8")
            return

        # Serve static assets from public/ if requested
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

        # 1. Save Staff Decision (§5.6 & §5.0)
        if path.startswith("/api/decisions/"):
            agency_id = path.replace("/api/decisions/", "").strip()
            dec_file = os.path.join(DATA_DIR, "decisions", f"{agency_id}.json")
            existing = {"agency_id": agency_id, "decisions": []}
            if os.path.exists(dec_file):
                try:
                    with open(dec_file, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                except Exception:
                    pass

            opp_id = str(payload.get("opportunity_id", ""))
            status = payload.get("status", "Pursue")
            notes = payload.get("notes", "")
            decided_by = payload.get("decided_by", "State Grants Coordinator")
            decided_at = datetime.now().isoformat()

            # Update or append
            found = False
            for d in existing.get("decisions", []):
                if str(d.get("opportunity_id")) == opp_id:
                    d["status"] = status
                    d["notes"] = notes
                    d["decided_by"] = decided_by
                    d["decided_at"] = decided_at
                    found = True
                    break
            if not found:
                existing.setdefault("decisions", []).append({
                    "opportunity_id": opp_id,
                    "number": payload.get("number", ""),
                    "title": payload.get("title", ""),
                    "status": status,
                    "notes": notes,
                    "decided_by": decided_by,
                    "decided_at": decided_at
                })

            os.makedirs(os.path.join(DATA_DIR, "decisions"), exist_ok=True)
            with open(dec_file, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2)

            self._send_json({"success": True, "agency_id": agency_id, "saved_decision": payload})
            return

        # 2. Trigger Daily Ingestion / Refresh Pipeline on Demand (§5.7)
        if path == "/api/pipeline/run":
            print(f"[API] Manual trigger of daily refresh pipeline initiated by user...")
            try:
                run_log = pipeline.run_pipeline()
                self._send_json({"success": True, "run_log": run_log})
            except Exception as e:
                self._send_json({"success": False, "error": str(e)}, 500)
            return

        # 3. Dynamic Weight Re-scoring (§8.1 Weight Sliders)
        if path.startswith("/api/rescore/"):
            agency_id = path.replace("/api/rescore/", "").strip()
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

            opps_file = os.path.join(DATA_DIR, "opportunities", "latest.json")
            if not os.path.exists(opps_file):
                self._send_json({"error": "No opportunities found"}, 404)
                return

            with open(opps_file, "r", encoding="utf-8") as f:
                normalized = json.load(f)

            rescored = []
            for opp in normalized:
                score_meta = pipeline.calculate_score(opp, profile, weights)
                item = dict(opp)
                item.update(score_meta)
                rescored.append(item)

            rescored.sort(key=lambda x: x["total_score"], reverse=True)
            self._send_json({
                "agency_id": agency_id,
                "weights_used": weights,
                "count": len(rescored),
                "items": rescored
            })
            return

        self.send_error(404, "Not Found")

def run_server():
    os.makedirs(os.path.join(BASE_DIR, "public"), exist_ok=True)
    server_address = ("", PORT)
    httpd = HTTPServer(server_address, MatchmakerHandler)
    print(f"\n=======================================================")
    print(f" NC Federal Funds Matchmaker Web UI running!")
    print(f" Local URL: http://localhost:{PORT}")
    print(f" Mode: {os.environ.get('MODE', 'app')}")
    print(f"=======================================================\n")
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
