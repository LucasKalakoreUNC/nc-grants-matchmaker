#!/usr/bin/env python3
"""
Federal Funds Matchmaker - Core Pipeline
Ingestion -> Normalization & Multi-Source Enrichment -> Scoring -> Summarization -> Run Log
Supports MODE=job (batch run and exit) and CLI invocations.
"""

import os
import sys
import json
import time
import math
import re
import urllib.request
import urllib.error
from datetime import datetime

import db

# "Today" for deadline math. Defaults to the real date; set PIPELINE_DATE=YYYY-MM-DD to replay a past run.
CURRENT_DATE_STR = os.environ.get("PIPELINE_DATE") or datetime.now().strftime("%Y-%m-%d")
CURRENT_DATE = datetime.strptime(CURRENT_DATE_STR, "%Y-%m-%d")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
PROFILES_DIR = os.path.join(DATA_DIR, "profiles")
RAW_DIR = os.path.join(DATA_DIR, "raw")
RUNS_DIR = os.path.join(DATA_DIR, "runs")

SEARCH_API_URL = "https://api.grants.gov/v1/api/search2"
FETCH_API_URL = "https://api.grants.gov/v1/api/fetchOpportunity"

KEYWORDS_PULL = [
    "water quality", "drinking water", "environmental", "climate resilience",
    "transportation", "highway safety", "electric vehicle", "transit",
    "behavioral health", "opioid", "maternal health", "public health",
    "broadband", "workforce", "economic development", "clean energy",
    "disaster mitigation", "flood", "cybersecurity", "emergency",
    "affordable housing", "community development block grant", "FEMA BRIC",
    "SBIR", "STTR", "small business", "commercialization", "cleantech",
    "STEM", "higher education", "apprenticeship", "safe streets", "urban forestry"
]

def fetch_grants_from_api(keyword=None, rows=25, opp_statuses="forecasted|posted"):
    """Query Grants.gov search2 endpoint."""
    payload = {
        "oppStatuses": opp_statuses,
        "rows": rows
    }
    if keyword:
        payload["keyword"] = keyword

    req = urllib.request.Request(
        SEARCH_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "NC-Grants-Matchmaker/2.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            if res_data.get("errorcode") == 0:
                return res_data.get("data", {}).get("oppHits", [])
    except Exception as e:
        print(f"[WARN] Error fetching keyword '{keyword}': {e}", file=sys.stderr)
    return []

def parse_date(date_str):
    """Parse dates in MM/DD/YYYY, YYYY-MM-DD or return None."""
    if not date_str:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%b %d, %Y", "%m/%d/%y"):
        try:
            return datetime.strptime(date_str.strip().split()[0], fmt)
        except (ValueError, IndexError):
            continue
    return None

def compute_days_to_close(close_date_dt):
    if not close_date_dt:
        return None
    delta = (close_date_dt - CURRENT_DATE).days
    return delta

def load_agency_profiles():
    profiles = {}
    if not os.path.exists(PROFILES_DIR):
        return profiles
    for fname in sorted(os.listdir(PROFILES_DIR)):
        if fname.endswith(".json"):
            with open(os.path.join(PROFILES_DIR, fname), "r", encoding="utf-8") as f:
                data = json.load(f)
                profiles[data.get("agency_id", fname.replace(".json", ""))] = data
    return profiles

def estimate_completion_effort(opp):
    """Rough staff-time estimate from facts in the funding notice.

    Starts at 25 hours for a basic federal application, then adds time for
    things that are known to make applications harder. Shown to staff as an
    estimate, with the reasons listed.
    """
    hours = 25
    factors = ["Basic federal application package (SF-424 forms, narrative, budget): 25 hours"]
    ceiling = opp.get("award_ceiling") or 0
    if ceiling >= 5_000_000:
        hours += 35
        factors.append("Large award (over $5 million) needs a detailed budget and work plan: +35 hours")
    elif ceiling >= 1_000_000:
        hours += 20
        factors.append("Award over $1 million needs a fuller budget justification: +20 hours")
    if opp.get("cost_share_required"):
        hours += 20
        factors.append("Matching funds required; budget office must commit the match: +20 hours")
    cats = " ".join(opp.get("funding_categories", [])).lower() + " " + opp.get("title", "").lower()
    if any(w in cats for w in ["infrastructure", "construction", "transportation", "highway", "water", "housing"]):
        hours += 15
        factors.append("Construction or infrastructure work may need environmental review: +15 hours")
    if any(w in (opp.get("eligibility_text") or "").lower() for w in ["partnership", "consortium", "letters of support", "collaborat"]):
        hours += 10
        factors.append("Partners or letters of support likely needed: +10 hours")
    weeks = max(1, -(-hours // 15))  # assumes about 15 staff hours per week
    tier = "Light" if hours <= 35 else "Moderate" if hours <= 70 else "Heavy"
    return hours, weeks, tier, factors

def generate_required_documents(opp):
    """Typical federal application documents, adjusted to what the notice says.
    This is a starting checklist; the official notice lists the exact requirements."""
    docs = {
        "standard_forms": [
            {"code": "SF-424", "name": "Application for Federal Assistance", "required": True, "notes": "Signed by your authorized representative in Grants.gov Workspace"},
            {"code": "SF-424A", "name": "Budget Information", "required": True, "notes": "Federal share and any matching funds by budget category"},
            {"code": "SF-424B", "name": "Assurances", "required": True, "notes": "Standard federal certifications"},
        ],
        "project_narrative": [
            {"name": "Project abstract", "required": True, "notes": "Usually 1 to 2 pages"},
            {"name": "Project narrative", "required": True, "notes": "Must address each review criterion in the notice"},
            {"name": "Budget justification", "required": True, "notes": "Explains each line of the SF-424A"},
        ],
        "administrative_legal": [
            {"name": "Active SAM.gov registration and UEI", "required": True, "notes": "Registration must not expire before the award date"},
            {"name": "Resumes of key staff", "required": False, "notes": "Often requested for project director and key personnel"},
            {"name": "NC State Clearinghouse review (Executive Order 12372)", "required": False, "notes": "Needed only if the federal program is covered by intergovernmental review"},
        ],
    }
    if (opp.get("award_ceiling") or 0) > 100_000:
        docs["standard_forms"].append({"code": "SF-LLL", "name": "Disclosure of Lobbying Activities", "required": True, "notes": "Required for awards over $100,000"})
    if opp.get("cost_share_required"):
        docs["financial_budget"] = [{"name": "Matching funds commitment letter", "required": True, "notes": "Signed by your budget or finance director"}]
    cats = " ".join(opp.get("funding_categories", [])).lower() + " " + opp.get("title", "").lower()
    if any(w in cats for w in ["infrastructure", "construction", "transportation", "highway", "water", "housing"]):
        docs["compliance_environmental"] = [{"name": "Environmental review (NEPA) information", "required": False, "notes": "Needed if the project involves construction or ground disturbance"}]
    return docs

def generate_action_steps(opp, profile=None):
    """Plain next steps built from facts about this grant, with real dates
    counted back from the deadline. Details come from the notice itself."""
    from datetime import timedelta
    days = opp.get("days_to_close")
    close = None
    if opp.get("close_date") and days is not None:
        try:
            close = datetime.strptime(opp["close_date"], "%Y-%m-%d")
        except ValueError:
            close = None
    when = lambda before: (close - timedelta(days=before)).strftime("%b %d, %Y") if close and days and days > before else "As soon as possible"

    steps = [{"title": "Decide whether to apply and name a lead",
              "deadline_day": when(int(days * 0.8)) if days and days > 10 else "As soon as possible",
              "action": "Read the requirements below and the official notice, confirm your organization is eligible, and assign one person to lead the application."},
             {"title": "Check registrations",
              "deadline_day": when(30) if days and days > 30 else "As soon as possible",
              "action": "Make sure your SAM.gov registration is active (renewal can take 10 business days or more) and that someone has the Authorized Organization Representative role on Grants.gov."}]
    if opp.get("cost_share_required"):
        steps.append({"title": "Confirm matching funds",
                      "deadline_day": when(21) if days and days > 21 else "As soon as possible",
                      "action": "This grant requires matching funds. Ask your budget office to confirm the source and amount; the notice gives the required percentage."})
    steps.append({"title": "Write the application",
                  "deadline_day": when(10) if days and days > 10 else "As soon as possible",
                  "action": "Prepare each document on the requirements list, following the notice's page limits and section headings."})
    steps.append({"title": "Submit early",
                  "deadline_day": when(3) if days and days > 3 else "Before the deadline",
                  "action": "Submit on Grants.gov a few days before the deadline. Validation can take up to two business days, and late applications are usually rejected."})
    if opp.get("status") == "forecasted":
        steps = [{"title": "Watch for the official notice",
                  "deadline_day": "When posted",
                  "action": "This grant is forecasted. Mark it Watch, check registrations now, and start planning; requirements and dates are final only when the notice is posted."}] + steps[1:2]
    for n, st in enumerate(steps, 1):
        st["step"] = n
    return steps

def _strip_html(text):
    import html as _html
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", _html.unescape(text)).strip()


def _money(v):
    try:
        v = int(float(str(v).replace(",", "").replace("$", "")))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def fetch_opportunity_detail(opp_id):
    """Full Grants.gov record for one opportunity (description, award sizes,
    eligibility, cost sharing, contact)."""
    req = urllib.request.Request(
        FETCH_API_URL,
        data=json.dumps({"opportunityId": int(opp_id)}).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "NC-Grants-Matchmaker/2.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            res = json.loads(r.read().decode("utf-8"))
            if res.get("errorcode") == 0:
                return res.get("data")
    except Exception as e:
        print(f"[WARN] Detail fetch failed for {opp_id}: {e}", file=sys.stderr)
    return None


def sync_opportunity_details(conn, raw_hits, max_workers=6):
    """Fetch details only for grants we have no details for, or whose search
    record changed since the details were stored."""
    from concurrent.futures import ThreadPoolExecutor
    have = {r["source_record_id"]: r["last_changed"] for r in conn.execute(
        "SELECT source_record_id, last_changed FROM raw_records WHERE source='grants.gov-detail'")}
    changed = {r["source_record_id"]: r["last_changed"] for r in conn.execute(
        "SELECT source_record_id, last_changed FROM raw_records WHERE source='grants.gov'")}
    todo = [str(h["id"]) for h in raw_hits if str(h["id"]) not in have or changed.get(str(h["id"]), "") > have[str(h["id"])]]
    if not todo:
        return 0, len(raw_hits)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(ex.map(fetch_opportunity_detail, todo))
    got = 0
    for oid, detail in zip(todo, results):
        if detail:
            db.upsert_raw(conn, "grants.gov-detail", oid, detail)
            got += 1
    conn.commit()
    return got, len(raw_hits) - len(todo)


USASPENDING_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"


def usaspending_search(payload, label=""):
    """POST a spending_by_award search. Returns the results list, or None on failure."""
    req = urllib.request.Request(USASPENDING_URL, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "User-Agent": "NC-Grants-Matchmaker/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8")).get("results", [])
    except Exception as e:
        if "CERTIFICATE_VERIFY_FAILED" not in str(e):
            print(f"[WARN] USAspending fetch failed for {label}: {e}", file=sys.stderr)
            return None
    # Some Python certificate bundles no longer trust USAspending's Entrust
    # certificate; the operating system's store (used by curl) still does.
    import subprocess
    try:
        out = subprocess.run(["curl", "-sS", "--max-time", "30", "-X", "POST", USASPENDING_URL,
                              "-H", "Content-Type: application/json", "-d", json.dumps(payload)],
                             capture_output=True, text=True, check=True).stdout
        return json.loads(out).get("results", [])
    except Exception as e:
        print(f"[WARN] USAspending fetch failed for {label}: {e}", file=sys.stderr)
        return None


def fetch_nc_awards(cfda):
    """Largest recent awards to North Carolina recipients under one Assistance Listing."""
    return usaspending_search({
        "filters": {
            "award_type_codes": ["02", "03", "04", "05"],
            "program_numbers": [cfda],
            "recipient_locations": [{"country": "USA", "state": "NC"}],
            "time_period": [{"start_date": "2019-10-01", "end_date": CURRENT_DATE.strftime("%Y-%m-%d")}],
        },
        "fields": ["Award ID", "Recipient Name", "Award Amount", "Start Date", "Description"],
        "limit": 8, "sort": "Award Amount", "order": "desc",
    }, cfda)


def fetch_recent_awards(cfda):
    """Up to 100 of the most recent awards nationwide under one Assistance Listing (last 3 years)."""
    start = CURRENT_DATE.replace(year=CURRENT_DATE.year - 3).strftime("%Y-%m-%d")
    return usaspending_search({
        "filters": {
            "award_type_codes": ["02", "03", "04", "05"],
            "program_numbers": [cfda],
            "time_period": [{"start_date": start, "end_date": CURRENT_DATE.strftime("%Y-%m-%d")}],
        },
        "fields": ["Award ID", "Award Amount", "Start Date"],
        "limit": 100, "sort": "Start Date", "order": "desc",
    }, cfda)


def sync_program_award_stats(conn, cfdas, max_age_hours=24 * 7, max_workers=4):
    """Typical recent award size per program (median, average, count), refreshed weekly."""
    from concurrent.futures import ThreadPoolExecutor
    import statistics
    conn.execute("""CREATE TABLE IF NOT EXISTS program_award_stats (
        cfda TEXT PRIMARY KEY, sample_size INTEGER, median REAL, average REAL,
        low REAL, high REAL, since TEXT, fetched_at TEXT)""")
    todo = [c for c in sorted(set(cfdas)) if c and not db.is_fresh(conn, "usaspending-stats", c, max_age_hours)]
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(ex.map(fetch_recent_awards, todo))
    ok = 0
    since = CURRENT_DATE.replace(year=CURRENT_DATE.year - 3).strftime("%Y-%m-%d")
    for cfda, rows in zip(todo, results):
        if rows is None:
            db.log_fetch(conn, "usaspending-stats", cfda, 0, ok=False, error="request failed")
            continue
        amounts = sorted(a for a in (r.get("Award Amount") for r in rows) if a and a > 0)
        if amounts:
            conn.execute("INSERT OR REPLACE INTO program_award_stats VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (cfda, len(amounts), statistics.median(amounts), sum(amounts) / len(amounts),
                          amounts[0], amounts[-1], since, db.now()))
        else:
            conn.execute("DELETE FROM program_award_stats WHERE cfda=?", (cfda,))
        db.log_fetch(conn, "usaspending-stats", cfda, len(amounts), ok=True)
        ok += 1
    conn.commit()
    return ok


def program_stats(conn, cfda):
    try:
        r = conn.execute("SELECT * FROM program_award_stats WHERE cfda=?", (cfda,)).fetchone()
        return dict(r) if r else None
    except Exception:
        return None


def sync_nc_awards(conn, cfdas, max_age_hours=24 * 7, max_workers=4):
    """Refresh NC award history for each program at most once a week."""
    from concurrent.futures import ThreadPoolExecutor
    todo = [c for c in sorted(set(cfdas)) if c and not db.is_fresh(conn, "usaspending", c, max_age_hours)]
    if not todo:
        return 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(ex.map(fetch_nc_awards, todo))
    ok = 0
    for cfda, rows in zip(todo, results):
        if rows is None:
            db.log_fetch(conn, "usaspending", cfda, 0, ok=False, error="request failed")
            continue
        for r in rows:
            start = r.get("Start Date") or ""
            db.upsert_award(conn, "usaspending", r.get("Award ID") or r.get("generated_internal_id"), cfda,
                            r.get("Recipient Name"), r.get("Award Amount"),
                            int(start[:4]) if start[:4].isdigit() else None, "NC", r)
        db.log_fetch(conn, "usaspending", cfda, len(rows), ok=True)
        ok += 1
    conn.commit()
    return ok


def enrich_opportunity(item, conn=None):
    """Adds staff-time estimate, document checklist, NC award history and next steps."""
    source = item.get("source", "grants.gov")
    item["source_display"] = {"grants.gov": "Grants.gov", "sam.gov": "SAM.gov Assistance Listings",
                              "federalregister": "Federal Register"}.get(source, source)

    hours, weeks, tier, factors = estimate_completion_effort(item)
    days = item.get("days_to_close")
    item["est_completion_hours"] = hours
    item["est_completion_weeks"] = weeks
    item["complexity_tier"] = tier
    item["complexity_factors"] = factors
    item["feasibility_warning"] = (
        f"This application likely needs about {weeks} weeks of work, but only {days} days remain."
        if days is not None and 0 <= days < weeks * 7 else None)

    awards = db.awards_for_cfda(conn, item.get("cfda")) if conn is not None and item.get("cfda") else []
    item["past_awardees"] = [{
        "recipient": (a["recipient"] or "").title(),
        "amount": a["amount"],
        "fiscal_year": a["fiscal_year"],
        "location": "North Carolina",
        "project_title": (json.loads(a["data"]).get("Description") or "")[:160].capitalize(),
        "source": "USAspending.gov",
    } for a in awards]
    item["past_award_stats"] = {"count": len(awards), "total": sum(a["amount"] or 0 for a in awards)}

    stats = program_stats(conn, item.get("cfda")) if conn is not None and item.get("cfda") else None
    if stats:
        item["typical_award"] = {k: stats[k] for k in ("sample_size", "median", "average", "low", "high", "since")}
        ceiling = item.get("award_ceiling")
        item["typical_award"]["well_below_ceiling"] = bool(ceiling and stats["median"] < ceiling * 0.25)

    req_docs = generate_required_documents(item)
    item["required_documents"] = req_docs
    item["required_documents_count"] = sum(len(v) for v in req_docs.values())
    item["action_steps"] = generate_action_steps(item)
    return item


AMOUNT_RE = re.compile(
    r"(award ceiling|ceiling|maximum (?:award|amount|request|federal)[a-z ]*|max(?:imum)?\.? award|"
    r"up to|not (?:to )?exceed|no more than|may request)[^$\n]{0,80}?\$\s?([\d,]+(?:\.\d+)?)\s*(million|billion|m\b|k\b)?",
    re.I)


def find_amount(text):
    """First stated per-award maximum in a block of text. Returns (amount, page or None)."""
    if not text:
        return None, None
    for m in AMOUNT_RE.finditer(text):
        try:
            v = float(m.group(2).replace(",", ""))
        except ValueError:
            continue
        around = (text[max(0, m.start() - 60): m.start()] + text[m.end(): m.end() + 80]).lower()
        if any(w in around for w in ("total", "available", "program funding", "anticipated funding", "aggregate")):
            continue
        unit = (m.group(3) or "").lower()
        v *= 1e6 if unit in ("million", "m") else 1e9 if unit == "billion" else 1e3 if unit == "k" else 1
        if v >= 1000:
            page = None
            marks = re.findall(r"\[page (\d+)\]", text[: m.start()])
            if marks:
                page = int(marks[-1])
            return int(v), page
    return None, None


def fill_award_amount(opp, conn=None):
    """Best available per-award amount, with a plain-language note on where it came from."""
    ceiling, total, n = opp.get("award_ceiling"), opp.get("estimated_total_funding"), opp.get("number_of_awards")
    if ceiling:
        opp["award_amount_source"] = "Award ceiling listed on Grants.gov"
        return
    amount, page = find_amount(opp.get("description"))
    where = "Grant description on Grants.gov"
    if not amount and conn is not None:
        try:
            rows = conn.execute("SELECT file_name, text FROM notice_files WHERE opportunity_id=? AND text IS NOT NULL "
                                "ORDER BY attachment_id DESC", (opp["id"],)).fetchall()
        except Exception:
            rows = []
        for r in rows:
            amount, page = find_amount(r["text"])
            if amount:
                where = f"Funding notice ({r['file_name']}{f', p. {page}' if page else ''})"
                break
    if amount:
        opp["award_ceiling"] = amount
        opp["award_amount_source"] = f"Stated in the {where[0].lower() + where[1:]}"
        return
    if total and n and n > 0:
        opp["award_estimate"] = int(total / n)
        opp["award_amount_source"] = f"Estimated: ${total:,} total funding divided by {n} expected awards (Grants.gov)"
        return
    if total:
        opp["award_amount_source"] = "Only total program funding is listed on Grants.gov"
        return
    opp["award_amount_source"] = "Not listed on Grants.gov or in the notice"


def normalize_opportunities(raw_hits, details=None, conn=None):
    """Turn Grants.gov search hits (plus stored detail records) into one clean record per grant."""
    details = details or {}
    seen_ids = set()
    normalized = []

    for item in raw_hits:
        opp_id = str(item.get("id", "")).strip()
        if not opp_id or opp_id in seen_ids:
            continue
        seen_ids.add(opp_id)
        d = details.get(opp_id) or {}
        syn = d.get("synopsis") or d.get("forecast") or {}

        status = item.get("oppStatus", "posted").lower()
        open_dt = parse_date(item.get("openDate"))
        close_dt = parse_date(item.get("closeDate"))
        days_to_close = compute_days_to_close(close_dt)
        if days_to_close is not None and days_to_close < 0 and status == "posted":
            continue

        cfda_list = item.get("cfdaList", item.get("alnList", []))
        cfda = cfda_list[0] if cfda_list else ""
        program_title = next((c.get("programTitle") for c in d.get("cfdas", []) if c.get("cfdaNumber") == cfda), "")

        # syn["agencyName"] is often the contact person; use the agency records instead.
        agency = ((d.get("agencyDetails") or {}).get("agencyName") or item.get("agency")
                  or (d.get("topAgencyDetails") or {}).get("agencyName") or "Federal agency").strip()
        fc = d.get("forecast") or {}
        pick = lambda key: _money(syn.get(key)) or _money(fc.get(key))
        description = _strip_html(syn.get("synopsisDesc") or syn.get("forecastDesc"))
        cost_sharing = syn.get("costSharing")

        norm = {
            "id": opp_id,
            "source": "grants.gov",
            "number": item.get("number", "").strip(),
            "title": _strip_html(item.get("title", "Untitled opportunity")),
            "agency": agency,
            "agency_code": item.get("agencyCode", ""),
            "cfda": cfda,
            "program_title": program_title,
            "status": status,
            "open_date": open_dt.strftime("%Y-%m-%d") if open_dt else None,
            "close_date": close_dt.strftime("%Y-%m-%d") if close_dt else None,
            "days_to_close": days_to_close,
            "eligibility": [t.get("description", "") for t in syn.get("applicantTypes", [])],
            "eligibility_text": _strip_html(syn.get("applicantEligibilityDesc"))[:1500],
            "funding_categories": [c.get("description", "") for c in syn.get("fundingActivityCategories", [])],
            "award_ceiling": pick("awardCeiling"),
            "award_floor": pick("awardFloor"),
            "estimated_total_funding": pick("estimatedFunding"),
            "number_of_awards": pick("numberOfAwards"),
            "cost_share_required": bool(cost_sharing) if cost_sharing is not None else None,
            "description": description[:2500] if description else "The full description is on the official notice.",
            "contact_email": syn.get("agencyContactEmail"),
            "has_details": bool(syn),
            "url": f"https://www.grants.gov/search-results-detail/{opp_id}",
            "ingested_at": datetime.now().isoformat(),
        }
        fill_award_amount(norm, conn)
        normalized.append(enrich_opportunity(norm, conn))

    return normalized

# ---------------------------------------------------------------- word similarity
# Compares all the words in a goal (title, description, objectives, phrases)
# with all the words in a grant. Rare, specific words count much more than
# common ones (TF-IDF), and simple word endings are ignored (fund/funding).
SIM_STOP = set("""a an and are as at be by for from has have in is it its of on or that the this to was were will
with within their they which who may can not all any also been but into more other such than these those through
under use used using via program programs project projects funding fund funds grant grants award awards applicant
applicants application applications federal state states support north carolina carolinians nc provide including
include new opportunity notice eligible organization organizations activities activity year years one two three""".split())
_SIM = {"idf": None, "n": 0}


def _stem(w):
    for suf in ("ations", "ation", "ments", "ment", "ings", "ing", "ies", "es", "s", "ed", "al"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def _tokens(text):
    return [_stem(w) for w in re.findall(r"[a-z][a-z0-9]+", (text or "").lower()) if w not in SIM_STOP and len(w) > 2]


def build_similarity_index(opps):
    """Word rarity across all current grants; call once per run before scoring."""
    df = {}
    for o in opps:
        for w in set(_tokens(o.get("title", "") + " " + o.get("description", ""))):
            df[w] = df.get(w, 0) + 1
    n = max(1, len(opps))
    _SIM["n"] = n
    _SIM["idf"] = {w: math.log((n + 1) / (c + 0.5)) for w, c in df.items()}
    _SIM["goal_cache"] = {}


def _vector(tokens):
    idf, n = _SIM["idf"] or {}, _SIM["n"] or 1
    v = {}
    for w in tokens:
        v[w] = v.get(w, 0) + 1
    v = {w: (1 + math.log(c)) * idf.get(w, math.log(n + 1)) for w, c in v.items()}
    norm = math.sqrt(sum(x * x for x in v.values())) or 1
    return {w: x / norm for w, x in v.items()}


def goal_similarity(opp, goals):
    """Best (score 0-100, goal, shared words) across goals, or (0, None, [])."""
    if not _SIM["idf"] or not goals:
        return 0.0, None, []
    text = opp.get("title", "") + " " + opp.get("title", "") + " " + opp.get("description", "")
    gv = _vector(_tokens(text))
    shown = {}
    for w in re.findall(r"[a-z][a-z0-9]+", text.lower()):
        shown.setdefault(_stem(w), w)
    best = (0.0, None, [])
    for g in goals:
        key = id(g)
        q = _SIM["goal_cache"].get(key)
        if q is None:
            q = _vector(_tokens(" ".join([g.get("title", ""), g.get("description", ""), " ".join(g.get("objectives", [])),
                                          " ".join(g.get("keywords", [])) * 2])))
            _SIM["goal_cache"][key] = q
        cos = sum(x * gv.get(w, 0) for w, x in q.items())
        if cos > best[0]:
            shared = sorted((w for w in q if w in gv), key=lambda w: -q[w] * gv[w])[:5]
            best = (cos, g, shared)
    # A cosine of about 0.25 is a very close match in practice; scale to 0-100.
    return round(min(100.0, best[0] / 0.25 * 100), 1), best[1], [shown.get(w, w) for w in best[2]]


GENERIC_WORDS = {"and", "with", "from", "state", "federal", "program", "programs", "public", "water", "health",
                 "community", "services", "development", "research", "data", "safety", "local", "support",
                 "access", "system", "systems", "education", "energy", "city", "county", "rural", "business"}

# How the overall score is built. Fit with the strategic plan is the gate:
#   Score = Fit x (0.6 + 0.4 x Practical / 100)
# where Practical is a weighted average of the parts below (weights add to 1).
# A grant with no real connection to the organization's goals cannot score
# well no matter how large or convenient it is.
SCORE_WEIGHTS = {
    "eligibility": 0.30,   # can this type of office apply
    "money_amount": 0.25,  # size of the award
    "timing": 0.25,        # enough time before the deadline for the work needed
    "effort": 0.15,        # staff hours to apply (less is better)
    "affinity": 0.05,      # funder this office usually works with
}
SCORE_LABELS = {
    "relevancy": "Fit with your strategic plan", "eligibility": "Your office can apply",
    "money_amount": "Award size", "timing": "Time before deadline",
    "effort": "Effort to apply", "affinity": "Familiar funder",
}

CATEGORY_ELIGIBILITY_WORDS = {
    "state": ["state government", "states", "state agenc"],
    "county": ["county", "counties", "local government", "units of local government"],
    "municipal": ["city", "cities", "municipal", "township", "local government", "units of local government"],
    "education": ["institutions of higher education", "universit", "college"],
    "business": ["small business", "for-profit", "for profit", "commercial"],
    "nonprofit": ["nonprofit", "non-profit", "501(c)"],
}


def eligibility_fit(opp, profile):
    """Score 0-100 for whether this office type may apply, plus a plain-language note."""
    types = [t.lower() for t in opp.get("eligibility", [])]
    title = (opp.get("title") or "").lower()
    is_tribal_org = any("tribal" in t.lower() for t in profile.get("eligible_applicant_types", []))
    if not is_tribal_org and re.search(r"\b(tribal|indian housing|urban indian|native american|native hawaiian)\b", title):
        return 10.0, "This program appears to be limited to tribes or Native organizations; check the notice."
    if not types:
        return 60.0, "Eligibility was not listed in the search data; check the official notice."
    joined = " | ".join(types)
    if "unrestricted" in joined:
        return 100.0, "Open to all applicant types."
    wanted = [t.lower() for t in profile.get("eligible_applicant_types", [])]
    if any(w in joined or any(w in t or t in w for t in types) for w in wanted):
        return 100.0, "Your type of organization is listed as eligible."
    words = CATEGORY_ELIGIBILITY_WORDS.get(profile.get("category", "state"), [])
    text = (opp.get("eligibility_text") or "").lower()
    if "others" in joined:
        if any(w in text for w in words):
            return 85.0, "The eligibility description appears to include your type of organization; confirm in the notice."
        return 50.0, "Eligibility is described in the notice text; check whether your office qualifies."
    return 10.0, "Your type of organization is not listed as eligible."


def calculate_score(opp, profile, weights=None, scoring_model="balanced"):
    """
    Multi-Model Scoring Engine supporting 5 distinct ranking equations:
    1. 'balanced': Standard weighted sum
    2. 'urgent_high_impact': Velocity & high-ceiling multiplier
    3. 'equity_need': Underserved communities & 0% cost-share preference
    4. 'compliance_strict': Strict applicant gating & feasibility penalty
    5. 'custom': Dynamic user sliders
    """
    if weights is None:
        weights = {
            "semantic": 0.45,
            "keyword": 0.25,
            "eligibility": 0.15,
            "timing": 0.10,
            "affinity": 0.05
        }

    title_text = opp.get("title", "").lower()
    desc_text = (opp.get("description", "") + " " + title_text).lower()
    opp_agency = opp.get("agency", "").lower()
    opp_agency_code = opp.get("agency_code", "").lower()

    # Agency acronym lookup map across State, County, Education, Business, Municipal
    agency_acronyms = {
        "deq": ["epa", "environmental protection", "noaa", "marine fisheries", "geological survey", "usgs", "energy", "fish and wildlife", "interior", "corps of engineers"],
        "dot": ["dot", "fhwa", "fta", "fra", "highway", "transit", "transportation", "marad", "aviation", "traffic"],
        "dhhs": ["hhs", "cdc", "nih", "samhsa", "hrsa", "health", "children and families", "substance abuse", "disease control"],
        "commerce": ["eda", "economic development", "ntia", "telecommunications", "commerce", "sba", "small business", "labor", "appalachian regional"],
        "dps": ["fema", "homeland security", "dhs", "cisa", "justice", "ojp", "bja", "emergency management", "traffic safety"],
        "wake_county": ["hud", "dot", "hhs", "justice", "epa", "fema", "county", "local government", "housing"],
        "buncombe_county": ["fema", "usda", "eda", "dot", "epa", "appalachian regional", "arc", "rural development", "mountain"],
        "unc_system": ["nsf", "nih", "energy", "education", "nasa", "defense", "science", "higher education", "research"],
        "nc_smallbiz": ["sba", "small business", "nsf", "energy", "nist", "eda", "defense", "sbir", "sttr"],
        "city_charlotte": ["dot", "fta", "hud", "epa", "energy", "justice", "transit", "municipal", "city"]
    }
    agency_id = profile.get("agency_id", "").lower()
    known_funder_terms = agency_acronyms.get(agency_id, []) + [f.lower() for f in profile.get("funder_affinities", [])]

    # 1. Goal / Semantic alignment
    best_goal = None
    best_goal_score = 0.0
    best_objective = ""
    matched_goal_kws = []

    goals = profile.get("goals", [])
    for goal in goals:
        g_keywords = [k.lower() for k in goal.get("keywords", [])]
        # Full keyword phrases count most; single words split from phrases count
        # a little, and very common words barely at all.
        phrases = set(g_keywords)
        words = set()
        for kw in g_keywords:
            for word in kw.split():
                if len(word) > 3 and word not in GENERIC_WORDS and word not in phrases:
                    words.add(word)

        matches = []
        score_val = 0.0
        for term in phrases:
            pat = r'\b' + re.escape(term) + r'\b'
            if re.search(pat, title_text):
                score_val += 0.35; matches.append(term)
            elif re.search(pat, desc_text):
                score_val += 0.12; matches.append(term)
        for term in words:
            pat = r'\b' + re.escape(term) + r'\b'
            if re.search(pat, title_text):
                score_val += 0.10; matches.append(term)
            elif re.search(pat, desc_text):
                score_val += 0.02
        raw_goal_score = min(1.0, score_val)

        if raw_goal_score > best_goal_score:
            best_goal_score = raw_goal_score
            best_goal = goal
            matched_goal_kws = matches
            objectives = goal.get("objectives", [])
            best_objective = objectives[0] if objectives else goal.get("title", "")

    if not best_goal and goals:
        best_goal = goals[0]
        best_objective = goals[0].get("objectives", [""])[0]
        best_goal_score = 0.20

    if best_goal_score > 0.4:
        semantic_component = round(min(100.0, 50.0 + (best_goal_score * 50.0)), 1)
    else:
        semantic_component = round(best_goal_score * 80.0, 1)

    # 2. Overall Agency Keyword Fit
    profile_keywords = [k.lower() for k in profile.get("keywords", [])]
    kw_hits = []
    for k in profile_keywords:
        if k in GENERIC_WORDS:
            continue
        if re.search(r'\b' + re.escape(k) + r'\b', desc_text) and k not in kw_hits:
            kw_hits.append(k)

    if kw_hits:
        keyword_component = round(min(100.0, 40.0 + (len(kw_hits) * 20.0)), 1)
    else:
        keyword_component = 15.0

    # 3. Eligibility: compare the notice's applicant types with who this office is
    eligibility_component, eligibility_note = eligibility_fit(opp, profile)

    # 4. Time available: days left compared with the estimated weeks of work
    days = opp.get("days_to_close")
    weeks_needed = opp.get("est_completion_weeks", 3)
    if opp.get("status") == "forecasted":
        timing_component = 90.0
    elif days is None:
        timing_component = 75.0
    else:
        ratio = days / max(1, weeks_needed * 7)
        timing_component = 100.0 if ratio >= 2 else 85.0 if ratio >= 1.25 else 65.0 if ratio >= 1 else 35.0 if ratio >= 0.5 else 10.0

    # 5. Agency Affinity
    affinity_component = 20.0
    for term in known_funder_terms:
        if term in opp_agency or term in opp_agency_code:
            affinity_component = 100.0
            break

    # Relevancy component (combined strategic semantic + keyword fit)
    phrase_fit = min(100.0, (semantic_component * 0.65) + (keyword_component * 0.35))
    similarity_component, sim_goal, shared_words = goal_similarity(opp, goals)
    relevancy_component = round(0.65 * phrase_fit + 0.35 * similarity_component, 1)
    if best_goal_score < 0.2 and sim_goal is not None and similarity_component >= 60:
        # No phrase hit, but the grant's wording closely matches one goal: name that goal.
        best_goal = sim_goal
        objectives = sim_goal.get("objectives", [])
        best_objective = objectives[0] if objectives else sim_goal.get("title", "")

    # Award size (largest single award). Unknown amounts get a neutral 50.
    ceiling_val = opp.get("award_ceiling") or opp.get("award_estimate")
    if not ceiling_val:
        money_component = 50.0
    elif ceiling_val >= 10_000_000:
        money_component = 100.0
    elif ceiling_val >= 5_000_000:
        money_component = 85.0
    elif ceiling_val >= 2_000_000:
        money_component = 70.0
    elif ceiling_val >= 1_000_000:
        money_component = 55.0
    elif ceiling_val >= 500_000:
        money_component = 40.0
    else:
        money_component = 25.0

    # Effort to apply (less staff time scores higher)
    hours = opp.get("est_completion_hours", 40)
    effort_component = 100.0 if hours <= 30 else 80.0 if hours <= 50 else 55.0 if hours <= 80 else 30.0

    parts = {
        "eligibility": eligibility_component,
        "money_amount": money_component,
        "timing": timing_component,
        "effort": effort_component,
        "affinity": affinity_component,
    }
    w = dict(SCORE_WEIGHTS)
    if scoring_model == "custom" and weights:
        w.update({k: v for k, v in weights.items() if k in w})
    w_sum = sum(w.values()) or 1
    practical = sum(parts[k] * w[k] for k in w) / w_sum
    total_score = relevancy_component * (0.6 + 0.4 * practical / 100)
    if eligibility_component <= 10:
        # Listed applicant types exclude this organization: keep it visible, but not as a good match.
        total_score *= 0.6
    equation_name = "Fit x practical factors"
    equation_latex = "Score = Fit x (0.6 + 0.4 x Practical / 100)"

    total_score = round(min(100.0, max(0.0, total_score)), 1)
    confidence = "High" if total_score >= 70 else "Medium" if total_score >= 55 else "Low"

    goal_title = best_goal.get("title", "") if best_goal else "Strategic priorities"
    goal_cite = best_goal.get("citation", "") if best_goal else ""
    matched_kws = ", ".join(kw_hits[:4]) if kw_hits else (", ".join(matched_goal_kws[:3]) if matched_goal_kws else "")
    positive_drivers = []

    # Evidence: the sentences in the grant that contain the matched phrases
    raw_text = (opp.get("title", "") + ". " + opp.get("description", ""))
    sentences = re.split(r"(?<=[.!?])\s+", raw_text)
    fit_evidence = []
    for term in sorted(set(matched_goal_kws) | set(kw_hits), key=len, reverse=True):
        for sent in sentences:
            if re.search(r"\b" + re.escape(term) + r"\b", sent, re.I) and len(fit_evidence) < 3 \
                    and sent not in [e["text"] for e in fit_evidence]:
                fit_evidence.append({"term": term, "text": sent.strip()[:300]})
                break

    risk_factors = []
    if opp.get("feasibility_warning"):
        risk_factors.append(opp["feasibility_warning"])
    elif days is not None and 0 <= days <= 14:
        risk_factors.append(f"Short deadline: {days} days left")
    if opp.get("cost_share_required"):
        risk_factors.append("Your office must provide matching funds (the notice gives the amount)")
    if eligibility_component < 60:
        risk_factors.append(eligibility_note)
    if hours >= 80:
        risk_factors.append(f"Large application: about {hours} staff hours")

    if relevancy_component >= 40 and fit_evidence:
        reason = (f"The grant's own wording matches the {profile.get('short_name')} goal \"{goal_title}\" "
                  f"(matching phrases: {matched_kws}). {eligibility_note}")
    elif fit_evidence:
        reason = (f"Only a weak link to the goal \"{goal_title}\": the grant mentions {matched_kws}, "
                  f"but not as its main purpose. {eligibility_note}")
    elif similarity_component >= 60 and shared_words:
        reason = (f"The grant uses much of the same language as the {profile.get('short_name')} goal \"{goal_title}\" "
                  f"(shared terms: {', '.join(shared_words[:4])}), though none of the goal's key phrases appear exactly. "
                  f"{eligibility_note}")
    else:
        reason = (f"No direct connection found between this grant and {profile.get('short_name')}'s goals. "
                  f"It appears only because of general factors such as eligibility or award size.")

    award_fmt = f"${opp.get('award_ceiling', 0):,}" if opp.get('award_ceiling') else "Not stated"
    deadline_text = f"closes in {days} days ({opp.get('close_date')})" if days is not None else f"status is {opp.get('status')}"

    ai_summary = (
        f"The {opp.get('agency')} is accepting proposals under '{opp.get('title')}'. "
        f"This opportunity provides up to {award_fmt} in financial assistance and {deadline_text}. "
        f"Eligible applicants include {', '.join(profile.get('eligible_applicant_types', ['state and local entities'])[:2])}. "
        f"Estimated application effort is ~{opp.get('est_completion_hours', 45)} hours ({opp.get('complexity_tier', 'Standard')})."
    )

    # Generate tailored sample drafts and action steps for this specific profile

    return {
        "total_score": total_score,
        "confidence": confidence,
        "scoring_model_used": scoring_model,
        "equation_name": equation_name,
        "equation_latex": equation_latex,
        "eligibility_note": eligibility_note,
        "fit_evidence": fit_evidence,
        "shared_words": shared_words,
        "practical_score": round(practical, 1),
        "score_weights": {k: round(w[k] / w_sum, 4) for k in w},
        "matched_goal_id": best_goal.get("id") if best_goal else "G1",
        "matched_goal_title": goal_title,
        "matched_goal_citation": goal_cite,
        "matched_objective": best_objective,
        "matched_keywords": kw_hits or matched_goal_kws,
        "reason": reason,
        "positive_drivers": positive_drivers,
        "risk_factors": risk_factors,
        "score_breakdown": {
            "relevancy": relevancy_component,
            "money_amount": money_component,
            "timing": timing_component,
            "eligibility": eligibility_component,
            "effort": effort_component,
            "phrase_fit": round(phrase_fit, 1),
            "similarity": similarity_component,
            "semantic": semantic_component,
            "keyword": keyword_component,
            "affinity": affinity_component
        }
    }

def run_pipeline():
    """Main pipeline execution for MODE=job or interactive sync."""
    start_time = time.time()
    today_str = CURRENT_DATE_STR
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Starting North Carolina Federal Funds Matchmaker Pipeline (v2.0)...")

    # 1. Ingestion (cached). Each Grants.gov query is only re-pulled once its
    # last successful pull is older than FETCH_MAX_AGE_HOURS; everything we
    # already have stays in the database and is reused.
    conn = db.connect()
    max_age = float(os.environ.get("FETCH_MAX_AGE_HOURS", "20"))
    force = os.environ.get("FORCE_REFRESH") == "1"
    counts = {"new": 0, "changed": 0, "unchanged": 0}
    pulled = skipped = 0
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking Grants.gov (cache window {max_age:g}h)...")
    for q in KEYWORDS_PULL + [None]:
        qkey = q or "(all)"
        if not force and db.is_fresh(conn, "grants.gov", qkey, max_age):
            skipped += 1
            continue
        hits = fetch_grants_from_api(keyword=q, rows=25 if q else 50)
        pulled += 1
        for hit in hits:
            if hit.get("id"):
                counts[db.upsert_raw(conn, "grants.gov", str(hit["id"]), hit)] += 1
        # An empty result usually means the network is down; don't mark it fresh.
        db.log_fetch(conn, "grants.gov", qkey, len(hits), ok=bool(hits), error=None if hits else "no results")
        conn.commit()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Queries pulled: {pulled}, skipped (still fresh): {skipped}. "
          f"Records new: {counts['new']}, changed: {counts['changed']}, unchanged: {counts['unchanged']}")

    raw_pulls = db.raw_records(conn, "grants.gov")
    open_hits = [h for h in raw_pulls if (compute_days_to_close(parse_date(h.get("closeDate"))) or 0) >= 0]

    # 1b. Full details from Grants.gov, fetched once per grant (again only if it changed)
    got, reused = sync_opportunity_details(conn, open_hits)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Grant details: {got} fetched, {reused} reused from database")
    details = {r["source_record_id"]: json.loads(r["payload"]) for r in conn.execute(
        "SELECT source_record_id, payload FROM raw_records WHERE source='grants.gov-detail'")}

    # 1c. North Carolina award history from USAspending, refreshed weekly per program
    cfdas = [c for h in open_hits for c in (h.get("cfdaList") or [])[:1]]
    m = sync_program_award_stats(conn, cfdas)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] USAspending typical award sizes: {m} programs refreshed")
    n = sync_nc_awards(conn, cfdas)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] USAspending: {n} programs refreshed; others reused from database or unavailable")

    # 2. Normalize and de-duplicate everything stored, then save to the database
    normalized = normalize_opportunities(open_hits, details, conn)
    opp_changes = {"new": 0, "changed": 0, "unchanged": 0}
    for opp in normalized:
        opp_changes[db.upsert_opportunity(conn, opp, source="grants.gov", source_record_id=opp["id"])] += 1
    closed = db.mark_closed(conn, today_str)
    conn.commit()
    normalized = db.opportunities(conn)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {len(normalized)} open/forecasted grants in database "
          f"({opp_changes['new']} new, {opp_changes['changed']} updated, {closed} newly closed)")

    # 3. Score & Rank across all NC organization profiles
    build_similarity_index(normalized)
    profiles = load_agency_profiles()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Scoring opportunities against {len(profiles)} NC Profiles (State, County, Education, Business, Municipal)...")

    scored_counts = {}
    for agency_id, profile in profiles.items():
        scored_list = []
        for opp in normalized:
            score_meta = calculate_score(opp, profile)
            # Store only the score; grant details live once in the opportunities table.
            scored_item = {"id": opp["id"], "title": opp.get("title", "")}
            scored_item.update(score_meta)
            scored_list.append(scored_item)

        scored_list.sort(key=lambda x: x["total_score"], reverse=True)
        db.save_matches(conn, agency_id, scored_list)
        conn.commit()

        high_count = sum(1 for x in scored_list if x["confidence"] == "High")
        med_count = sum(1 for x in scored_list if x["confidence"] == "Medium")
        scored_counts[agency_id] = {
            "name": profile.get("short_name"),
            "category": profile.get("category", "state"),
            "total": len(scored_list),
            "high": high_count,
            "medium": med_count,
            "top_match": scored_list[0]["title"] if scored_list else None,
            "top_score": scored_list[0]["total_score"] if scored_list else 0
        }
        print(f"  -> [{profile.get('category', 'state').upper()}] {profile['short_name']}: {len(scored_list)} scored ({high_count} High, {med_count} Medium). Top: {scored_list[0]['title'][:40]}... ({scored_list[0]['total_score']} pts)")

    # 3b. Download notice files for new grants (needs pypdf, installed in .venv), then
    # load notice requirements recorded in data/requirements/ (shared through git)
    try:
        import notices
        nconn = notices.connect()
        try:
            import pypdf  # noqa: F401
            got, had, failed = notices.sync_notice_files(nconn)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Notice files: {got} new, {had} already stored, {failed} failed")
            lg, lb = notices.sync_linked_notices(nconn)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Agency-website announcements: {lg} fetched, {lb} unavailable")
            waiting = len(notices.priority_ids(nconn))
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Grants with notices not yet read: {waiting} (see NOTICE_READING_GUIDE.md)")
        except ImportError:
            print("[INFO] Skipping notice downloads: run with .venv/bin/python to include them")
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Requirement files loaded: {notices.load_requirements_dir(nconn)}")
    except Exception as e:
        print(f"[WARN] Could not load requirement files: {e}", file=sys.stderr)


    # 5. Run Log
    duration = round(time.time() - start_time, 2)
    run_log = {
        "timestamp": timestamp,
        "run_date": today_str,
        "status": "Succeeded",
        "counts": {
            "raw_pulled": len(raw_pulls),
            "normalized_unique": len(normalized),
            "queries_pulled": pulled,
            "queries_skipped_cached": skipped,
            "records_new": counts["new"],
            "records_changed": counts["changed"],
            "agencies_scored": len(profiles),
            "breakdown": scored_counts
        },
        "duration_seconds": duration,
        "errors": []
    }
    os.makedirs(RUNS_DIR, exist_ok=True)
    run_log_file = os.path.join(RUNS_DIR, f"{timestamp}.json")
    with open(run_log_file, "w", encoding="utf-8") as f:
        json.dump(run_log, f, indent=2)

    with open(os.path.join(RUNS_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(run_log, f, indent=2)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Pipeline completed successfully in {duration}s. Run log: {run_log_file}")
    return run_log

if __name__ == "__main__":
    mode = os.environ.get("MODE", "job")
    if mode == "job" or len(sys.argv) > 1 and sys.argv[1] == "--job":
        try:
            run_pipeline()
            sys.exit(0)
        except Exception as e:
            print(f"[FATAL] Pipeline failed: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        run_pipeline()
