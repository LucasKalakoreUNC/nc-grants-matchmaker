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
import hmac
from html import escape as html_escape

import pipeline
import db
import notices
import ai
import programs
import state_programs
import notifications


# Fields only the grant panel needs; left out of list responses to keep them
# small and sent by /api/opportunity/<id>/sources instead.
DETAIL_FIELDS = ("description", "past_awardees", "past_award_stats", "required_documents", "action_steps",
                 "eligibility_text", "eligibility", "complexity_factors", "positive_drivers")


def slim(items):
    return [{k: v for k, v in it.items() if k not in DETAIL_FIELDS} for it in items]


def other_items(conn, office_id, profile):
    """State/foundation applications with a sourced deadline that has not passed.

    Undated program descriptions do not establish an open or upcoming application.
    """
    cat = profile.get("category", "state")
    today = db.nc_today()
    out = []
    if cat != "state":  # state agencies run state programs rather than apply to them
        for m in state_programs.for_office(conn, office_id):
            foundation = (m.get("kind") or "").startswith("Private")
            if foundation and cat in ("county", "municipal"):
                rec = conn.execute("SELECT payload FROM raw_records WHERE source='nc-state-program' AND source_record_id=?", (m["id"],)).fetchone()
                text = (json.loads(rec["payload"]).get("text") or "").lower() if rec else ""
                if "nonprofit" in text[:400] and "government" not in text[:600]:
                    continue  # funds nonprofits only
            deadlines = [(db.application_deadline({"close_date": d.get("date")}), d)
                         for d in m.get("deadlines", [])]
            deadlines = [(day, d) for day, d in deadlines if day and day >= today]
            if not deadlines:
                continue
            close, d = min(deadlines, key=lambda pair: pair[0])
            out.append({"id": f"st-{m['id']}", "kind": "Private foundation" if foundation else "NC state program",
                        "title": m["name"], "agency": "Private foundation" if foundation else "State of North Carolina",
                        "url": m["url"], "total_score": round(m["fit"] * 0.8, 1), "status": "posted",
                        "close_date": close.isoformat(), "deadline_quote": d.get("quote"),
                        "days_to_close": (close - today).days,
                        "matched_goal_title": m.get("matched_goal_title"), "page_read": m.get("page_read", True),
                        "overview": m.get("overview") or {}, "parent": m.get("parent"), "shared_words": m.get("shared_words"),
                        "best_office_id": office_id})
    return out


def load_scored(agency_id):
    """Scored grants for one office, best first, read from the database."""
    conn = db.connect()
    try:
        profile = pipeline.load_agency_profiles().get(agency_id)
        return scored_for_profile(conn, agency_id, profile) if profile else []
    finally:
        conn.close()


def scored_for_profile(conn, office_id, profile):
    """Keep all scores, excluding notices that rule out this applicant type."""
    return [item for item in db.scored_for_office(conn, office_id)
            if pipeline.eligibility_fit(item, profile)[0] > 10]


def selection_counts(conn, profiles, office="all", category="all"):
    ids = [office] if office != "all" else [oid for oid, profile in profiles.items()
            if category == "all" or profile.get("category", "state") == category]
    selected = [profiles[oid] for oid in ids if oid in profiles]
    today = db.nc_today()
    grants = [item for item in db.opportunities(conn)
              if db.application_available(item, today)
              and any(pipeline.eligibility_fit(item, profile)[0] > 10 for profile in selected)]
    extras = {item["id"]: item for oid in ids if oid in profiles
              for item in other_items(conn, oid, profiles[oid])}
    foundations = sum(item["kind"] == "Private foundation" for item in extras.values())
    return {"available": len(grants),
            "open": sum(item["status"] == "posted" for item in grants),
            "upcoming": sum(item["status"] == "forecasted" for item in grants),
            "state_programs": len(extras) - foundations, "foundations": foundations,
            "total": len(grants) + len(extras)}


def build_weekly_digest(conn, office_id, category="all"):
    """Generate weekly email briefing covering pursuing grants, upcoming due dates, and new matches."""
    profiles = pipeline.load_agency_profiles()
    profile = profiles.get(office_id, {})
    org_name = profile.get("name", office_id.replace("_", " ").title() if office_id != "all" else "All North Carolina Organizations")
    today = db.nc_today()
    today_str = today.strftime("%B %d, %Y")
    selected_ids = [office_id] if office_id != "all" else [oid for oid, p in profiles.items()
                    if category == "all" or p.get("category", "state") == category]
    if office_id == "all" and category != "all":
        org_name = {"state": "State agencies", "county": "Counties", "municipal": "Cities and towns",
                    "education": "Universities and colleges", "nonprofit": "Nonprofits",
                    "tribal": "Tribes", "business": "Businesses"}.get(category, category)

    # 1. Grants the department is pursuing
    if selected_ids:
        marks = ",".join("?" for _ in selected_ids)
        dec_rows = conn.execute(f"""
            SELECT office_id, opportunity_id, number, title, notes, decided_by, decided_at
            FROM decisions WHERE office_id IN ({marks}) AND status='Pursue' ORDER BY decided_at DESC
        """, selected_ids).fetchall()
    else:
        dec_rows = []

    pursuing = []
    total_funding = 0
    closing_soon_count = 0

    for d in dec_rows:
        oid = d["opportunity_id"]
        opp = conn.execute("SELECT * FROM opportunities WHERE id=?", (oid,)).fetchone()
        req = notices.requirements_for(conn, oid)

        close_date = None
        days_to_close = None
        award_ceiling = None
        agency = "Federal / State Funder"
        url = f"https://www.grants.gov/search-results-detail/{oid}"
        title = d["title"] or "Grant Opportunity"
        number = d["number"] or ""

        if opp:
            record = json.loads(opp["data"])
            record.update(status=opp["status"], close_date=opp["close_date"])
            if not db.application_available(record, today):
                continue
            title = opp["title"]
            number = opp["number"] or number
            deadline = db.application_deadline(record)
            close_date = deadline.isoformat() if deadline else None
            award_ceiling = opp["award_ceiling"]
            agency = opp["agency"]
            url = opp["url"]
            if close_date:
                try:
                    days_to_close = (datetime.fromisoformat(close_date[:10]).date() - today).days
                except Exception:
                    days_to_close = None
        elif str(oid).startswith(("st-", "fp-")):
            others = other_items(conn, d["office_id"], profiles.get(d["office_id"], {}))
            it = next((x for x in others if x["id"] == oid), None)
            if it:
                agency = it.get("agency")
                url = it.get("url")
                close_date = it.get("close_date")
                days_to_close = it.get("days_to_close")
            else:
                continue
        else:
            continue  # retained historical decisions are not active applications

        if award_ceiling:
            total_funding += award_ceiling

        is_soon = (days_to_close is not None and 0 <= days_to_close <= 30)
        if is_soon:
            closing_soon_count += 1

        docs_list = []
        verified_docs = [doc for doc in (req or {}).get("required_documents", []) if doc.get("verified") is not False]
        if verified_docs:
            for doc in verified_docs[:8]:
                docs_list.append({
                    "name": doc.get("name"),
                    "category": doc.get("category", "Standard"),
                    "details": doc.get("details", ""),
                    "source": doc.get("source", "")
                })

        pursuing.append({
            "opportunity_id": oid,
            "title": title,
            "number": number,
            "agency": agency,
            "close_date": close_date,
            "days_to_close": days_to_close,
            "closing_soon": is_soon,
            "award_ceiling": award_ceiling,
            "notes": d["notes"],
            "decided_by": d["decided_by"],
            "decided_at": d["decided_at"],
            "required_documents": docs_list,
            "required_doc_count": len(verified_docs),
            "url": url
        })

    pursuing.sort(key=lambda x: (x["days_to_close"] is None or x["days_to_close"] < 0, x["days_to_close"] if x["days_to_close"] is not None else 9999))

    # 2. New & Top Strategic Opportunities for this department
    new_candidates = {}
    for oid in selected_ids:
        decided = {str(d[0]) for d in conn.execute("SELECT opportunity_id FROM decisions WHERE office_id=?", (oid,))}
        for it in scored_for_profile(conn, oid, profiles.get(oid, {})):
            if str(it["id"]) in decided:
                continue
            previous = new_candidates.get(it["id"])
            if previous is None or it["total_score"] > previous["total_score"]:
                new_candidates[it["id"]] = {**it, "best_office_id": oid}
    top_new = sorted(new_candidates.values(), key=lambda it: -it["total_score"])[:6]

    money_str = lambda n: f"${int(n/1e6 * 10)/10:.1f}M" if n and n >= 1e6 else (f"${int(n):,}" if n else "See notice")

    # 3. Plain text format
    lines = [
        "=" * 70,
        f" NORTH CAROLINA FEDERAL & STATE GRANTS WEEKLY BRIEFING",
        f" Department: {org_name}",
        f" Date: {today_str}",
        "=" * 70,
        "",
        f"SUMMARY:",
        f" * Grants You Are Pursuing: {len(pursuing)} active ({closing_soon_count} closing within 30 days)",
        f" * Total Tracked Potential Funding: ${total_funding:,.0f}" if total_funding else " * Active Pursuits Tracked",
        f" * New Department Strategic Matches: {len(top_new)} opportunities",
        "",
        "-" * 70,
        f"1. GRANTS YOUR DEPARTMENT WANTS TO PURSUE & DUE DATES ({len(pursuing)})",
        "-" * 70,
    ]

    if not pursuing:
        lines.append("No grants currently marked 'Pursue' for this department.")
        lines.append("Select opportunities in the Matchmaker and mark them 'Pursue' to track their due dates and document checklists here.")
    else:
        for idx, p in enumerate(pursuing, 1):
            due_badge = f"*** CLOSING IN {p['days_to_close']} DAYS! (Deadline: {p['close_date']}) ***" if p['closing_soon'] else (f"Deadline: {p['close_date']} ({p['days_to_close']} days left)" if p['days_to_close'] is not None else "Deadline: Open / See Notice")
            lines.append(f"\n[{idx}] {p['title']}")
            lines.append(f"    Funding Agency: {p['agency']} | ID: {p['number']}")
            lines.append(f"    Award Amount: Up to {money_str(p['award_ceiling'])}")
            lines.append(f"    STATUS & DUE DATE: {due_badge}")
            if p['decided_by']:
                lines.append(f"    Team Lead: {p['decided_by']}")
            if p['notes']:
                lines.append(f"    Team Note: \"{p['notes']}\"")
            if p['required_documents']:
                lines.append(f"    Application Checklist ({p['required_doc_count']} required documents):")
                for doc in p['required_documents'][:5]:
                    lines.append(f"      [ ] {doc['category']}: {doc['name']}")
                if p['required_doc_count'] > 5:
                    lines.append(f"      ... plus {p['required_doc_count'] - 5} more documents in full checklist")
            lines.append(f"    Notice URL: {p['url']}")

    lines.extend([
        "",
        "-" * 70,
        f"2. NEW HIGH-SCORING OPPORTUNITIES FOR YOUR DEPARTMENT ({len(top_new)})",
        "-" * 70,
    ])

    for idx, it in enumerate(top_new, 1):
        fit_lbl = "Strong match" if it.get("total_score", 0) >= 70 else "Good match" if it.get("total_score", 0) >= 55 else "Possible match"
        due = "Upcoming (forecasted)" if it.get("status") == "forecasted" else f"{it.get('days_to_close')} days left (Due {it.get('close_date')})" if it.get('days_to_close') is not None else "Open; no deadline listed"
        lines.append(f"\n* {it.get('title')}")
        lines.append(f"  Match: {fit_lbl} ({it.get('total_score')}/100) | Funder: {it.get('agency')}")
        if it.get("matched_goal_title"):
            lines.append(f"  Supports Strategic Goal: \"{it.get('matched_goal_title')}\"")
        lines.append(f"  Award: Up to {money_str(it.get('award_ceiling'))} | Timeframe: {due}")
        lines.append(f"  Est. Staff Effort: ~{it.get('est_completion_hours', '?')} hours")
        lines.append(f"  View Details: {it.get('url')}")

    lines.extend([
        "",
        "=" * 70,
        "Generated automatically by NC Grants Matchmaker for state and local staff.",
        "Confirm official deadlines on Grants.gov or agency notice before submission.",
        "=" * 70
    ])

    text_content = "\n".join(lines)

    # 4. HTML format
    html_pursuing = ""
    if not pursuing:
        html_pursuing = "<p style='color:#565c65;font-style:italic;'>No grants currently marked 'Pursue'. Click 'Pursue' on any opportunity in the Matchmaker to track its due dates and checklist here.</p>"
    else:
        for idx, p in enumerate(pursuing, 1):
            p = {k: html_escape(v, quote=True) if isinstance(v, str) else v for k, v in p.items()}
            urgency_bg = "#fef0f0" if p['closing_soon'] else "#f0f4f8"
            urgency_border = "#d9381e" if p['closing_soon'] else "#005ea2"
            urgency_text = f"<strong style='color:#d9381e'>[URGENT] {p['days_to_close']} days left (Due {p['close_date']})</strong>" if p['closing_soon'] else f"<strong>Due: {p['close_date']} ({p['days_to_close']} days left)</strong>" if p['days_to_close'] is not None else "Deadline: Open / See Notice"

            docs_html = "".join([f"<li style='margin-bottom:3px;'><strong>{html_escape(d['category'])}:</strong> {html_escape(d['name'])}</li>" for d in p['required_documents'][:5]])
            if p['required_doc_count'] > 5:
                docs_html += f"<li style='color:#565c65;'><em>+ {p['required_doc_count'] - 5} more documents in checklist</em></li>"
            team_html = ("<div style='font-size:0.9em;margin-bottom:6px;'><strong>Team Lead:</strong> "
                         + p['decided_by'] + ' &middot; <em>"' + p['notes'] + '"</em></div>') if p['notes'] or p['decided_by'] else ""

            html_pursuing += f"""
            <div style="background:{urgency_bg};border-left:5px solid {urgency_border};padding:14px 18px;margin-bottom:16px;border-radius:3px;">
              <div style="font-size:1.1em;font-weight:bold;margin-bottom:4px;">{idx}. <a href="{p['url']}" style="color:#005ea2;text-decoration:underline;">{p['title']}</a></div>
              <div style="font-size:0.9em;color:#565c65;margin-bottom:8px;">{p['agency']} &middot; Opp #{p['number']} &middot; Award: <strong>{money_str(p['award_ceiling'])}</strong></div>
              <div style="margin-bottom:8px;font-size:0.95em;">{urgency_text}</div>
              {team_html}
              {f"<div style='margin-top:8px;'><div style='font-size:0.85em;font-weight:bold;text-transform:uppercase;color:#565c65;'>Required Documents Checklist ({p['required_doc_count']}):</div><ul style='margin:4px 0 0;padding-left:20px;font-size:0.9em;'>{docs_html}</ul></div>" if docs_html else ""}
            </div>
            """

    html_new = ""
    for idx, it in enumerate(top_new, 1):
        it = {k: html_escape(v, quote=True) if isinstance(v, str) else v for k, v in it.items()}
        fit_color = "#00a91c" if it.get("total_score", 0) >= 70 else "#005ea2"
        due = "Upcoming" if it.get("status") == "forecasted" else f"{it.get('days_to_close')} days left" if it.get('days_to_close') is not None else "No deadline listed"
        html_new += f"""
        <tr style="border-bottom:1px solid #dfe1e2;">
          <td style="padding:10px 8px;"><a href="{it.get('url')}" style="color:#005ea2;font-weight:bold;text-decoration:none;">{it.get('title')}</a><br><span style="font-size:0.85em;color:#565c65;">{it.get('agency')} &middot; Goal: {it.get('matched_goal_title', 'Strategic priority')}</span></td>
          <td style="padding:10px 8px;white-space:nowrap;"><span style="background:{fit_color};color:#fff;padding:2px 8px;border-radius:3px;font-size:0.85em;font-weight:bold;">{it.get('total_score')}/100</span></td>
          <td style="padding:10px 8px;white-space:nowrap;font-weight:bold;">{money_str(it.get('award_ceiling'))}</td>
          <td style="padding:10px 8px;white-space:nowrap;font-size:0.9em;">{due}</td>
        </tr>
        """

    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:'Public Sans',Arial,sans-serif;line-height:1.5;color:#1b1b1b;background:#f9f9f9;margin:0;padding:20px;">
  <div style="max-width:680px;margin:0 auto;background:#ffffff;border:1px solid #dfe1e2;border-radius:4px;overflow:hidden;">
    <div style="background:#162e51;color:#ffffff;padding:20px 24px;">
      <div style="font-size:0.85em;letter-spacing:1px;text-transform:uppercase;color:#c9d4e3;">North Carolina Federal & State Grants Briefing</div>
      <h1 style="font-family:Merriweather,Georgia,serif;font-size:1.4em;margin:6px 0 2px;color:#ffffff;">{html_escape(org_name)}</h1>
      <div style="font-size:0.9em;color:#e7f2f8;">Briefing for week of {today_str}</div>
    </div>
    <div style="padding:20px 24px;">
      <div style="background:#f0f4f8;padding:12px 16px;border-radius:4px;margin-bottom:20px;">
        <strong>{len(pursuing)}</strong> Grants Pursuing &middot; <strong style="color:{'#d9381e' if closing_soon_count else 'inherit'};">{closing_soon_count}</strong> Closing Within 30 Days &middot; <strong>{len(top_new)}</strong> New Matches
      </div>
      <h2 style="font-size:1.2em;border-bottom:2px solid #162e51;padding-bottom:6px;margin:24px 0 12px;color:#162e51;">
        1. Grants Your Department Wants to Pursue & Due Dates
      </h2>
      {html_pursuing}
      <h2 style="font-size:1.2em;border-bottom:2px solid #162e51;padding-bottom:6px;margin:28px 0 12px;color:#162e51;">
        2. New High-Scoring Matches for Your Strategic Goals
      </h2>
      <table style="width:100%;border-collapse:collapse;font-size:0.95em;">
        <thead>
          <tr style="background:#f0f4f8;text-align:left;font-size:0.85em;color:#565c65;">
            <th style="padding:8px;">Grant Opportunity</th>
            <th style="padding:8px;">Match</th>
            <th style="padding:8px;">Award Up To</th>
            <th style="padding:8px;">Deadline</th>
          </tr>
        </thead>
        <tbody>
          {html_new}
        </tbody>
      </table>
      <div style="margin-top:30px;padding:14px;background:#f0f0f0;border-radius:4px;font-size:0.85em;color:#565c65;">
        Sent automatically by the <strong>NC Grants Matchmaker</strong>. Confirm official deadlines on Grants.gov or agency notice before submission.
      </div>
    </div>
  </div>
</body>
</html>"""

    subject = f"[NC Grants Briefing] {org_name}: {len(pursuing)} In Progress ({closing_soon_count} Closing Soon) | {len(top_new)} New Matches"

    return {
        "office_id": office_id,
        "office_name": org_name,
        "date": today_str,
        "subject": subject,
        "pursuing_grants": pursuing,
        "new_opportunities": top_new,
        "summary": {
            "pursuing_count": len(pursuing),
            "closing_soon_count": closing_soon_count,
            "new_count": len(top_new),
            "total_funding": total_funding
        },
        "text": text_content,
        "html": html_body,
        "email_delivery_available": bool(os.environ.get("SMTP_HOST")),
    }

PORT = int(os.environ.get("PORT", 8000))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

class MatchmakerHandler(BaseHTTPRequestHandler):
    def log_request(self, code="-", size="-"):
        path = urllib.parse.urlparse(self.path).path
        self.log_message('%s %s %s', self.command, path, str(code))

    def _send_html(self, content, status=200):
        body = content.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        self.wfile.write(body)

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

        if path == '/api/notifications/status':
            conn = db.connect()
            try:
                counts = {r['state']: r['n'] for r in conn.execute("SELECT state, COUNT(*) AS n FROM notification_outbox GROUP BY state")}
            finally:
                conn.close()
            self._send_json({'configured': notifications.mail_ready(), 'reminder_days': list(notifications.REMINDER_DAYS),
                             'weekly_schedule': 'Monday mornings, America/New_York', 'minimum_field_fit': notifications.MIN_FIELD_FIT,
                             'outbox': {'pending': counts.get('pending', 0), 'sent': counts.get('sent', 0), 'failed': counts.get('failed', 0)}})
            return

        if path == '/notifications/confirm':
            token = urllib.parse.parse_qs(parsed.query).get('token', [''])[0]
            conn = db.connect()
            try:
                account = notifications.confirm(conn, token)
                conn.commit()
            finally:
                conn.close()
            if not account:
                self._send_html('<h1>Confirmation link expired</h1><p>Request a new confirmation from the grant finder.</p>', 400)
                return
            self._send_html('<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Email confirmed</title>'
                '<h1>Your grant emails are confirmed</h1><p>Deadline reminders and weekly updates you requested are now active.</p>'
                '<p><a href="/">Return to NC Grants Matchmaker</a></p><script>'
                'localStorage.setItem("user_email",' + json.dumps(account['email']) + ');'
                'localStorage.setItem("notification_token",' + json.dumps(account['token']) + ');</script></html>')
            return

        if path == '/notifications/unsubscribe':
            token = urllib.parse.parse_qs(parsed.query).get('token', [''])[0]
            self._send_html('<!doctype html><html lang="en"><title>Stop grant emails</title><h1>Stop grant emails</h1>'
                '<p>This stops your deadline reminders and weekly grant updates. Staff decisions stay saved.</p>'
                '<form method="post" action="/notifications/unsubscribe"><input type="hidden" name="token" value="' + html_escape(token, quote=True) + '">'
                '<button type="submit">Stop my grant emails</button></form></html>')
            return

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

        if path == "/api/ai/status":
            info = {"drafting": bool(ai.provider()), "provider": ai.provider()}
            if ai.provider() == "azure-openai":
                from urllib.parse import urlparse
                base, deployment, version = ai.azure_settings()
                info.update(endpoint_host=urlparse(base).netloc, deployment=deployment, api_version=version)
            self._send_json(info)
            return

        if path.startswith("/api/state-programs/"):
            office = path.rsplit("/", 1)[-1]
            profile = pipeline.load_agency_profiles().get(office, {})
            conn = db.connect()
            try:
                # State agencies run these programs rather than apply to them
                is_state = profile.get("category", "state") == "state"
                self._send_json({
                    "programs": [] if is_state else state_programs.for_office(conn, office),
                    "legislative": [] if is_state else state_programs.legislative_for(conn, profile),
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
                    items = []
                    for oid in ids:
                        for item in scored_for_profile(conn, oid, profiles[oid])[:per]:
                            item["best_office_id"] = oid
                            items.append(item)
                        items += other_items(conn, oid, profiles[oid])
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
            conn = db.connect()
            try:
                profile = pipeline.load_agency_profiles().get(agency_id, {})
                extra = other_items(conn, agency_id, profile) if profile else []
                items = scored_for_profile(conn, agency_id, profile) if profile else []
            finally:
                conn.close()
            self._send_json(slim(items) + extra)
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

        # 6. API: Weekly Email Digest for Department
        if path.startswith("/api/digest/"):
            agency_id = path.replace("/api/digest/", "").strip()
            conn = db.connect()
            try:
                if agency_id != "all" and agency_id not in pipeline.load_agency_profiles():
                    self._send_json({"error": "Profile not found"}, 404)
                    return
                category = urllib.parse.parse_qs(parsed.query).get("category", ["all"])[0]
                digest = build_weekly_digest(conn, agency_id, category)
                digest["email_delivery_available"] = bool(
                    digest["email_delivery_available"] and os.environ.get("ADMIN_TOKEN")
                    and self.headers.get("X-Admin-Token") == os.environ["ADMIN_TOKEN"])
                self._send_json(digest)
            finally:
                conn.close()
            return

        if path.startswith("/api/subscribers/"):
            token = os.environ.get("ADMIN_TOKEN")
            if not token or self.headers.get("X-Admin-Token") != token:
                self._send_json({"error": "Not allowed"}, 403)
                return
            agency_id = path.replace("/api/subscribers/", "").strip()
            conn = db.connect()
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS subscribers (email TEXT, office_id TEXT, role TEXT, subscribed_at TEXT, PRIMARY KEY(email, office_id))")
                subs = [dict(r) for r in conn.execute("SELECT email, role, subscribed_at FROM subscribers WHERE office_id=? ORDER BY subscribed_at DESC", (agency_id,)).fetchall()]
                self._send_json({"subscribers": subs})
            finally:
                conn.close()
            return

        if path == "/api/db/stats":
            conn = db.connect()
            try:
                query = urllib.parse.parse_qs(parsed.query)
                office = query.get("office", ["all"])[0]
                category = query.get("category", ["all"])[0]
                profiles = pipeline.load_agency_profiles()
                if office != "all" and office not in profiles:
                    self._send_json({"error": "Profile not found"}, 404)
                    return
                result = db.stats(conn)
                result["selection"] = selection_counts(conn, profiles, office, category)
                self._send_json(result)
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
                "Rank", "Score", "Match", "Fit with strategic plan", "Type", "Grant", "Opportunity number", "Funding agency",
                "Status", "Opens (expected, if forecasted)", "Deadline (expected, if forecasted)", "Days left", "Award up to", "Typical past award", "Estimated staff hours",
                "Matching funds", "Matched goal", "Goal citation", "Why it fits", "Your decision", "Decision notes",
                "Official listing",
            ])
            for i, it in enumerate(items[:100]):
                typical = (it.get("typical_award") or {}).get("median")
                dec = decisions.get(str(it.get("id")), {})
                cs = it.get("cost_share_required")
                due = db.application_deadline(it)
                writer.writerow([
                    i + 1, it.get("total_score", 0), label(it.get("total_score", 0)),
                    round((it.get("score_breakdown") or {}).get("relevancy", 0)), "Federal grant",
                    it.get("title", ""), it.get("number", ""), it.get("agency", ""), it.get("status", ""),
                    "Open now" if it.get("status") != "forecasted" else (it.get("expected_open_date") or "Not announced"),
                    (due.isoformat() if due else "") if it.get("status") == "forecasted" else (it.get("close_date") or ""),
                    "" if it.get("days_to_close") is None else it.get("days_to_close"),
                    dollars(it.get("award_ceiling") or it.get("award_estimate")), dollars(typical),
                    it.get("est_completion_hours", ""),
                    "Required" if cs is True else "Not required" if cs is False else "See notice",
                    it.get("matched_goal_title", ""), it.get("matched_goal_citation", ""), it.get("reason", ""),
                    dec.get("status", ""), dec.get("notes", ""), it.get("url", ""),
                ])
            # State programs, foundations and standing federal programs this organization can apply for
            profile = pipeline.load_agency_profiles().get(agency_id, {})
            if profile:
                conn = db.connect()
                try:
                    others = sorted(other_items(conn, agency_id, profile), key=lambda x: -x["total_score"])
                finally:
                    conn.close()
                for j, it in enumerate(others, len(items[:100]) + 1):
                    r = it.get("award_range") or {}
                    writer.writerow([
                        j, it["total_score"], label(it["total_score"]), "", it["kind"], it["title"], "", it.get("parent") or it.get("agency", ""),
                        "", "", it.get("close_date") or "", "",
                        f"{dollars(r.get('low'))} to {dollars(r.get('high'))}" if r.get("low") else "", "", "", "",
                        it.get("matched_goal_title", ""), "", (it.get("overview") or {}).get("description", "")[:500], "", "", it.get("url", ""),
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
        public_dir = os.path.realpath(os.path.join(BASE_DIR, "public"))
        candidate = os.path.realpath(os.path.join(public_dir, safe_rel_path))
        if os.path.commonpath([public_dir, candidate]) == public_dir and os.path.isfile(candidate):
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
            payload = dict(urllib.parse.parse_qsl(body)) if path == '/notifications/unsubscribe' else {}

        if path in ('/notifications/unsubscribe', '/api/notifications/unsubscribe'):
            token = payload.get('token') or urllib.parse.parse_qs(parsed.query).get('token', [''])[0]
            conn = db.connect()
            try:
                stopped = notifications.unsubscribe(conn, token)
                conn.commit()
            finally:
                conn.close()
            if path == '/notifications/unsubscribe':
                self._send_html('<h1>Grant emails stopped</h1><p>You will no longer receive reminders or weekly updates for this address.</p>' if stopped else '<h1>Invalid unsubscribe link</h1>', 200 if stopped else 400)
            else:
                self._send_json({'success': stopped}, 200 if stopped else 400)
            return

        if path == '/api/notifications/run':
            token = os.environ.get('ADMIN_TOKEN')
            if not token or self.headers.get('X-Admin-Token') != token:
                self._send_json({'error':'Not allowed'}, 403)
                return
            conn = db.connect()
            try:
                result = notifications.run_once(conn)
            finally:
                conn.close()
            self._send_json(result)
            return

        # 2. Save Staff Decision (§5.6 & §5.0)
        if path.startswith("/api/decisions/"):
            agency_id = path.replace("/api/decisions/", "").strip()
            opp_id = str(payload.get("opportunity_id", ""))[:40]
            status = payload.get("status")
            if agency_id not in pipeline.load_agency_profiles() or status not in ("Pursue", "Watch", "Pass"):
                self._send_json({"error": "Unknown organization or status"}, 400)
                return
            try:
                alert_email = notifications.valid_email(payload['alert_email']) if payload.get('alert_email') else None
            except ValueError as error:
                self._send_json({'error': str(error)}, 400)
                return
            conn = db.connect()
            notification_result = None
            try:
                row = conn.execute("SELECT number, title FROM opportunities WHERE id=?", (opp_id,)).fetchone()
                if not row and opp_id.startswith(("st-", "fp-")):
                    # State program, foundation or federal program shown to this organization
                    other = next((x for x in other_items(conn, agency_id, pipeline.load_agency_profiles()[agency_id]) if x["id"] == opp_id), None)
                    row = {"number": "", "title": other["title"]} if other else None
                if not row:
                    self._send_json({"error": "Unknown grant"}, 400)
                    return
                db.save_decision(conn, agency_id, {
                    "opportunity_id": opp_id,
                    "number": row["number"],
                    "title": row["title"],
                    "status": status,
                    "notes": str(payload.get("notes", ""))[:1000],
                    "decided_by": str(payload.get("decided_by", "") or "Grants staff")[:100],
                    "decided_at": datetime.now().isoformat(timespec="seconds"),
                })
                if status != 'Pursue':
                    conn.execute('UPDATE notification_pursuits SET active=0 WHERE office_id=? AND opportunity_id=?', (agency_id, opp_id))
                elif alert_email and payload.get('alerts_enabled'):
                    notification_result = notifications.request_subscription(conn, alert_email, agency_id,
                        pipeline.load_agency_profiles()[agency_id].get('category', 'state'), grant=opp_id,
                        token=str(payload.get('notification_token') or ''))
                elif alert_email and payload.get('notification_token'):
                    account = conn.execute('SELECT manage_token FROM notification_accounts WHERE email=?', (alert_email,)).fetchone()
                    if account and hmac.compare_digest(account['manage_token'], str(payload['notification_token'])):
                        conn.execute('UPDATE notification_pursuits SET active=0 WHERE email=? AND office_id=? AND opportunity_id=?', (alert_email, agency_id, opp_id))
                conn.commit()
            finally:
                conn.close()
            if notification_result and notification_result.get("delivery_ready"):
                flush_notifications_async()

            self._send_json({"success": True, "agency_id": agency_id, 'notification': notification_result})
            return

        # 2b. Weekly Digest Subscription
        if path == "/api/subscribe":
            conn = db.connect()
            try:
                result = notifications.request_subscription(conn, payload.get('email'), str(payload.get('office_id') or 'all'),
                    str(payload.get('category') or 'all'))
                conn.commit()
                if result.get("delivery_ready"):
                    # Send this person's confirmation now (capped so a backlog can't stall the request);
                    # anything else waits for the 60-second background loop.
                    delivery = notifications.run_once(conn, max_items=3)
                    result["sent"] = delivery.get("sent", 0)
                    result["failed"] = delivery.get("failed", 0)
            except ValueError as error:
                conn.rollback()
                self._send_json({'error':str(error)}, 400)
                return
            finally:
                conn.close()
            self._send_json({'success':True, **result})
            return

        # 2c. Send / Dispatch Weekly Digest
        if path == "/api/digest/send":
            token = os.environ.get("ADMIN_TOKEN")
            if not token or self.headers.get("X-Admin-Token") != token:
                self._send_json({"error": "Not allowed"}, 403)
                return
            email = str(payload.get("email", "")).strip()
            office_id = str(payload.get("office_id", "deq")).strip()
            if not email or "@" not in email:
                self._send_json({"error": "Valid email address required"}, 400)
                return
            conn = db.connect()
            try:
                digest = build_weekly_digest(conn, office_id)
            finally:
                conn.close()

            smtp_host = os.environ.get("SMTP_HOST")
            sent_real = False
            if smtp_host:
                try:
                    import smtplib
                    from email.mime.multipart import MIMEMultipart
                    from email.mime.text import MIMEText
                    msg = MIMEMultipart("alternative")
                    msg["Subject"] = digest["subject"]
                    msg["From"] = os.environ.get("SMTP_FROM", "grants-matchmaker@nc.gov")
                    msg["To"] = email
                    msg.attach(MIMEText(digest["text"], "plain"))
                    msg.attach(MIMEText(digest["html"], "html"))
                    port = int(os.environ.get("SMTP_PORT", 587))
                    with smtplib.SMTP(smtp_host, port) as server:
                        if os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASS"):
                            server.starttls()
                            server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
                        server.send_message(msg)
                    sent_real = True
                except Exception as e:
                    print(f"[WARN] SMTP delivery failed: {e}")

            self._send_json({
                "success": sent_real,
                "sent": sent_real,
                "smtp_delivered": sent_real,
                "message": f"Weekly briefing dispatched to {email}" if sent_real else "Email delivery is unavailable. Preview or copy the briefing instead.",
                "subject": digest["subject"],
                "summary": digest["summary"]
            })
            return

        # 3. Trigger Ingestion / Refresh Pipeline on Demand (§5.7)
        if path.startswith("/api/draft-doc/"):
            parts = path.split("/")
            if len(parts) != 5 or not payload.get("document"):
                self._send_json({"error": "Use /api/draft-doc/<organization>/<grant> with a document name"}, 400)
                return
            conn = db.connect()
            try:
                self._send_json(ai.get_or_create_doc_draft(conn, parts[3], parts[4][:40], str(payload["document"])[:300]))
            except ValueError as e:
                self._send_json({"error": str(e)}, 400)
            except Exception as e:
                detail = re.sub(r"[A-Za-z0-9]{32,}", "[hidden]", f"{type(e).__name__}: {e}")[:300]
                print(f"[WARN] Document draft failed: {detail}")
                self._send_json({"error": "The draft could not be written right now. Try again in a minute.", "detail": detail}, 503)
            finally:
                conn.close()
            return

        if path.startswith("/api/draft/"):
            parts = path.split("/")
            if len(parts) != 5:
                self._send_json({"error": "Use /api/draft/<organization>/<grant>"}, 400)
                return
            conn = db.connect()
            try:
                self._send_json(ai.get_or_create_draft(conn, parts[3], parts[4][:40]))
            except ValueError as e:
                self._send_json({"error": str(e)}, 400)
            except Exception as e:
                # The provider's error code (e.g. DeploymentNotFound) helps fix setup; it never contains the key.
                detail = re.sub(r"[A-Za-z0-9]{32,}", "[hidden]", f"{type(e).__name__}: {e}")[:300]
                print(f"[WARN] Draft failed: {detail}")
                self._send_json({"error": "The draft could not be written right now. Try again in a minute.", "detail": detail}, 503)
            finally:
                conn.close()
            return

        # Heavy maintenance endpoints are not used by the page; on a public site they need ADMIN_TOKEN
        if path in ("/api/pipeline/run",) or path.startswith("/api/rescore/"):
            token = os.environ.get("ADMIN_TOKEN")
            if not token or self.headers.get("X-Admin-Token") != token:
                self._send_json({"error": "Not allowed"}, 403)
                return

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
                if pipeline.outside_nc(opp):
                    continue
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

def flush_notifications_async():
    """Send newly queued confirmation emails immediately without waiting for the 60s timer."""
    if not notifications.mail_ready():
        return
    import threading

    def send_now():
        conn = None
        try:
            conn = db.connect()
            notifications.run_once(conn)
        except Exception:
            print('[WARN] Immediate grant email check failed; it will be retried.')
        finally:
            if conn is not None:
                conn.close()

    threading.Thread(target=send_now, daemon=True).start()


def start_notification_timer():
    """Hourly external wake-ups provide a fallback when App Service is idle."""
    import threading, time

    def loop():
        while True:
            conn = None
            try:
                conn = db.connect()
                notifications.run_once(conn)
            except Exception:
                print('[WARN] Grant email check failed; it will be retried.')
            finally:
                if conn is not None:
                    conn.close()
            time.sleep(60)

    threading.Thread(target=loop, daemon=True).start()


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
    start_notification_timer()
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
