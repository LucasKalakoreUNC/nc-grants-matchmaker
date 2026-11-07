#!/usr/bin/env python3
"""
North Carolina state grant programs, from the official NC.gov directory.

Many federal dollars reach counties, cities and others through grant programs
run by NC state agencies (Commerce, DEQ, Parks, Emergency Management...). The
state lists them at https://www.nc.gov/your-government/all-nc-state-services/grant-opportunities.

This module:
  1. reads that directory (the list itself is the source of truth: only
     programs the state links to are included),
  2. downloads each program's page and keeps its text,
  3. records a deadline only when the page states one next to deadline
     wording, together with the sentence it came from; otherwise nothing
     is filled in,
  4. scores each program against every organization's goals.

Run:  python3 state_programs.py      (also called from pipeline.py)
"""

import re
import html
import json
import hashlib
import urllib.request
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import db

DIRECTORY_URL = "https://www.nc.gov/your-government/all-nc-state-services/grant-opportunities"
SCHEMA = """
CREATE TABLE IF NOT EXISTS state_program_matches (
    office_id   TEXT NOT NULL,
    program_id  TEXT NOT NULL,
    fit         REAL NOT NULL,
    data        TEXT NOT NULL,
    PRIMARY KEY (office_id, program_id)
);
"""
LEGISLATIVE_URL = "https://www.osbm.nc.gov/legislative-grants-database"

# Major North Carolina foundations. Descriptions are taken from each funder's
# own published description; most of these sites block automated reading, so
# no deadlines or amounts are filled in. Pages that can be read are read.
FOUNDATIONS = [
    ("Golden LEAF Foundation: Open Grants Program", "https://www.goldenleaf.org/",
     "Funding for projects that strengthen North Carolina's economy, especially in tobacco-dependent, economically distressed, and/or rural communities; eligible applicants may apply throughout the year. Focus areas include economic development, job creation, workforce preparedness, agriculture and infrastructure for local governments, community colleges and nonprofits."),
    ("Dogwood Health Trust", "https://dht.org/grantmaking/",
     "Grants to improve health and well-being in western North Carolina, including organizational capacity building, collaboration and innovation after Hurricane Helene, community supports for older adults, housing, education and economic opportunity."),
    ("Kate B. Reynolds Charitable Trust", "https://kbr.org/",
     "Grants for health improvement and early childhood in North Carolina, with a focus on rural and low-wealth communities and health equity."),
    ("Z. Smith Reynolds Foundation", "https://www.zsr.org/",
     "Grants to North Carolina nonprofits working on community economic development, democracy and civic engagement, environment, and racial equity."),
    ("The Duke Endowment", "https://www.dukeendowment.org/",
     "Grants in North Carolina and South Carolina for health care, child and family well-being, higher education, and rural United Methodist churches."),
]

# Funders that only give in part of the state: url -> organizations inside that area
REGIONAL = {"https://dht.org/grantmaking/": {"buncombe_county", "city_asheville"}}

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE_RE = re.compile(rf"\b({MONTHS})\.? (\d{{1,2}}),? (20\d\d)\b")
DEADLINE_WORDS = ("deadline", "due", "applications must be received", "accepted through", "closes", "submit by")


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (NC-Grants-Matchmaker)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _text(raw):
    raw = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", raw)
    t = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n\n", t).strip()


def read_directory():
    """(name, url, section) for each program the NC.gov directory links to."""
    raw = _get(DIRECTORY_URL)
    main = raw[raw.find("<main"):] if "<main" in raw else raw
    out, seen, section = [], set(), ""
    for m in re.finditer(r"<h[23][^>]*>(.*?)</h[23]>|<a[^>]+href=\"(https?://[^\"]+)\"[^>]*>(.*?)</a>", main, re.S):
        if m.group(1):
            section = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
            continue
        url, name = m.group(2), html.unescape(re.sub(r"<[^>]+>", "", m.group(3))).strip()
        if len(name) < 8 or url in seen or "nc.gov/your-government" in url or "privacy" in url.lower():
            continue
        seen.add(url)
        out.append((name, url, section))
    return out


def sync_legislative(conn, max_age_hours=24 * 7):
    """Grants written into the current state budget (OSBM Legislative Grants Database)."""
    if db.is_fresh(conn, "nc-legislative", "database", max_age_hours):
        return 0
    try:
        raw = _get(LEGISLATIVE_URL)
    except Exception as e:
        db.log_fetch(conn, "nc-legislative", "database", 0, ok=False, error=e)
        conn.commit()
        return 0
    tables = re.findall(r"<table.*?</table>", raw, re.S)
    if not tables:
        return 0
    tb = max(tables, key=len)
    cell = lambda c: html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
    header = [cell(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", tb, re.S)]
    conn.execute("DELETE FROM raw_records WHERE source='nc-legislative'")
    n = 0
    for r in re.findall(r"<tr[^>]*>(.*?)</tr>", tb, re.S):
        cells = [cell(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)]
        if len(cells) != len(header) or not cells[0]:
            continue
        row = dict(zip(header, cells))
        row["source_url"] = LEGISLATIVE_URL
        db.upsert_raw(conn, "nc-legislative", hashlib.sha1(json.dumps(row, sort_keys=True).encode()).hexdigest()[:16], row)
        n += 1
    db.log_fetch(conn, "nc-legislative", "database", n, ok=True)
    conn.commit()
    return n


def legislative_for(conn, profile):
    """State budget grants directed to this organization (or, for a county, within it)."""
    rows = [json.loads(r["payload"]) for r in conn.execute("SELECT payload FROM raw_records WHERE source='nc-legislative'")]
    name = profile.get("name", "")
    cat = profile.get("category")
    if cat == "county":
        county = re.sub(r"\s+County.*$", "", name).strip()
        hits = [r for r in rows if r.get("County", "").lower() == county.lower()]
    elif cat == "municipal":
        town = re.sub(r"\s*(&|and)\s.*$", "", name).replace("City of ", "").replace("Town of ", "").strip().lower()
        hits = [r for r in rows if re.match(rf"(city|town) of {re.escape(town)}\b", r.get("Organization Receiving Funding", "").lower())]
    elif cat == "education":
        key = re.sub(r"\s*(&|and)\s.*$", "", name).strip().lower()
        hits = [r for r in rows if key and key in r.get("Organization Receiving Funding", "").lower()]
    else:
        hits = []
    amt = lambda r: int(re.sub(r"[^\d]", "", next((v for k, v in r.items() if k.startswith("FY")), "0")) or 0)
    return sorted(hits, key=lambda r: -amt(r))


def find_deadlines(text):
    """Future dates that appear next to deadline wording, with the sentence they came from."""
    today = datetime.now().date()
    found = []
    for m in DATE_RE.finditer(text):
        around = text[max(0, m.start() - 160): m.end() + 40].lower()
        if not any(w in around for w in DEADLINE_WORDS):
            continue
        try:
            d = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date()
        except ValueError:
            continue
        if d >= today:
            start = max(text.rfind(".", 0, m.start()) + 1, m.start() - 200)
            end = text.find(".", m.end())
            quote = re.sub(r"\s+", " ", text[start: end + 1 if end != -1 else m.end() + 80]).strip()
            found.append({"date": d.isoformat(), "quote": quote[:300]})
    return sorted(found, key=lambda x: x["date"])[:3]


def sync(conn, max_age_hours=24 * 7, max_workers=4):
    """Directory and program pages, refreshed weekly. Returns (programs, pages read, pages failed)."""
    if db.is_fresh(conn, "nc-state", "directory", max_age_hours):
        return 0, 0, 0
    try:
        programs = [(n, u, "North Carolina state program") for n, u, _sec in read_directory()]
        programs += [(n, u, "Private foundation (NC)") for n, u, _d in FOUNDATIONS]
    except Exception as e:
        db.log_fetch(conn, "nc-state", "directory", 0, ok=False, error=e)
        conn.commit()
        return 0, 0, 0

    described = {u: d for _n, u, d in FOUNDATIONS}

    def fetch(p):
        try:
            text, err = _text(_get(p[1])), None
        except Exception as e:
            text, err = None, str(e)[:200]
        if p[1] in described:  # use the funder's own description when its site can't be read
            text = described[p[1]] + ("\n\n" + text if text else "")
        return p, text, err

    read = failed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for (name, url, section), text, err in ex.map(fetch, programs):
            pid = hashlib.sha1(url.encode()).hexdigest()[:12]
            rec = {"id": pid, "name": name, "url": url, "agency_section": section, "kind": section,
                   "page_read": err is None,
                   "text": (text or "")[:20000], "error": err, "fetched_at": db.now(),
                   "deadlines": find_deadlines(text or ""), "source": "NC.gov Grant Opportunities directory"}
            db.upsert_raw(conn, "nc-state-program", pid, rec)
            read += err is None
            failed += err is not None
    db.log_fetch(conn, "nc-state", "directory", len(programs), ok=True)
    conn.commit()
    return len(programs), read, failed


def score(conn, profiles, per_office=10):
    import pipeline
    conn.executescript(SCHEMA)
    recs = [json.loads(r["payload"]) for r in conn.execute("SELECT payload FROM raw_records WHERE source='nc-state-program'")]
    pseudo = [{"id": r["id"], "title": r["name"], "description": (r.get("text") or "")[:4000], "eligibility": [],
               "status": "program", "_rec": r} for r in recs if r.get("text")]
    pipeline.build_similarity_index(pseudo)
    for org_id, profile in profiles.items():
        rows = []
        for p in pseudo:
            foundation = p["_rec"].get("kind") == "Private foundation (NC)"
            if foundation and p["_rec"]["url"] in REGIONAL and org_id not in REGIONAL[p["_rec"]["url"]]:
                continue  # regional funder outside its area
            meta = pipeline.calculate_score(p, profile)
            fit = meta["score_breakdown"]["relevancy"]
            regional = foundation and p["_rec"]["url"] in REGIONAL  # in-area regional funder: list it
            # Foundation descriptions are short and broad, so a lower fit is enough to list them
            if regional or fit >= (25 if foundation else 40):
                rows.append((fit, p, meta, foundation))
        rows.sort(key=lambda x: -x[0])
        conn.execute("DELETE FROM state_program_matches WHERE office_id=?", (org_id,))
        keep = [r for r in rows if not r[3]][:per_office] + [r for r in rows if r[3]]
        for fit, p, meta, _f in keep:
            r = p["_rec"]
            conn.execute("INSERT OR REPLACE INTO state_program_matches VALUES (?, ?, ?, ?)", (org_id, r["id"], fit, json.dumps({
                "id": r["id"], "name": r["name"], "url": r["url"], "agency_section": r["agency_section"],
                "kind": r.get("kind", "North Carolina state program"), "page_read": r.get("page_read", True),
                "deadlines": r.get("deadlines", []), "fit": fit, "matched_goal_title": meta.get("matched_goal_title"),
                "shared_words": meta.get("shared_words"), "fetched_at": r["fetched_at"],
            })))
    conn.commit()


def for_office(conn, office_id):
    conn.executescript(SCHEMA)
    return [json.loads(r["data"]) for r in conn.execute(
        "SELECT data FROM state_program_matches WHERE office_id=? ORDER BY fit DESC", (office_id,))]


def run(conn=None):
    import pipeline
    conn = conn or db.connect()
    n, read, failed = sync(conn)
    sync_legislative(conn)
    score(conn, pipeline.load_agency_profiles())
    return n, read, failed


if __name__ == "__main__":
    n, read, failed = run()
    print(f"NC state programs: {n} in directory, {read} pages read, {failed} could not be read")
