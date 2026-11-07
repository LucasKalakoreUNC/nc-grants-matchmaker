#!/usr/bin/env python3
"""
Federal assistance programs from SAM.gov Assistance Listings (no API key needed).

Grants.gov only shows competitions that are open or forecast. Many federal
programs reach North Carolina by formula or through state pass-through and
never post there. This module:

  1. downloads the list of active Assistance Listings (weekly),
  2. scores each program's purpose against every organization's goals,
  3. fetches full program details (objective, restrictions, who can apply,
     past award range, yearly obligations) for programs behind current
     grants and each organization's best-fitting programs (monthly),
  4. links each grant to its program so the grant page can show it.

Every value comes from the program's official SAM.gov record, and the page
links back to it.

Run:  python3 programs.py          (also called from pipeline.py)
"""

import re
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import db

SEARCH_URL = "https://sam.gov/api/prod/sgs/v1/search/?index=cfda&page={page}&size=100&mode=search&is_active=true"
DETAIL_URL = "https://sam.gov/api/prod/fac/v1/programs/{id}"
VIEW_URL = "https://sam.gov/fal/{id}/view"

SCHEMA = """
CREATE TABLE IF NOT EXISTS program_matches (
    office_id   TEXT NOT NULL,
    cfda        TEXT NOT NULL,
    fit         REAL NOT NULL,
    data        TEXT NOT NULL,
    PRIMARY KEY (office_id, cfda)
);
"""


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (NC-Grants-Matchmaker)", "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def sync_listings(conn, max_age_hours=24 * 7):
    """Active Assistance Listings (summary records). Returns number stored."""
    if db.is_fresh(conn, "sam.gov", "listings", max_age_hours):
        return 0
    n, page, pages = 0, 0, 1
    while page < pages:
        try:
            d = _get(SEARCH_URL.format(page=page))
        except Exception as e:
            db.log_fetch(conn, "sam.gov", "listings", n, ok=False, error=e)
            conn.commit()
            return n
        pages = d.get("page", {}).get("totalPages", 0)
        for r in d.get("_embedded", {}).get("results", []):
            if r.get("programNumber"):
                db.upsert_raw(conn, "sam.gov-listing", r["programNumber"], r)
                n += 1
        page += 1
        time.sleep(0.3)
    db.log_fetch(conn, "sam.gov", "listings", n, ok=True)
    conn.commit()
    return n


UNIT = {"million": 1e6, "m": 1e6, "billion": 1e9, "b": 1e9, "thousand": 1e3, "k": 1e3}
AMOUNT = r"\$\s?([\d,]+(?:\.\d+)?)\s*(million|billion|thousand|m|b|k)?\b"


def _money(num, unit=None):
    return int(float(num.replace(",", "")) * UNIT.get((unit or "").lower(), 1))


def parse_award_range(text):
    """Award range and average as the agency states them in the listing, if it does."""
    text = text or ""
    out = {}
    m = re.search(AMOUNT + r"\s*(?:-|–|to)\s*" + AMOUNT, text, re.I)
    if m:
        out["low"], out["high"] = _money(m.group(1), m.group(2)), _money(m.group(3), m.group(4))
    m = re.search(r"average[^$\n]{0,30}" + AMOUNT, text, re.I)
    if m:
        out["average"] = _money(m.group(1), m.group(2))
    # Ignore values too small to be award amounts (e.g. a misread "$80" from "$80 million" variants)
    return {k: v for k, v in out.items() if v >= 1000}


def normalize(detail, listing_id):
    d = detail.get("data", detail)
    fin = d.get("financial") or {}
    elig = d.get("eligibility") or {}
    totals = {}
    for ob in fin.get("obligations") or []:
        for v in ob.get("values") or []:
            kind = "actual" if v.get("actual") is not None else "estimate" if v.get("estimate") is not None else None
            if kind:
                key = (v.get("year"), kind)
                totals[key] = totals.get(key, 0) + (v.get(kind) or 0)
    yearly = sorted(({"year": k[0], "kind": k[1], "amount": v} for k, v in totals.items()),
                    key=lambda x: (x["year"] or 0, x["kind"]))
    website = d.get("website")
    return {
        "cfda": d.get("programNumber"),
        "title": d.get("title"),
        "objective": (d.get("objective") or "").strip(),
        "uses_restrictions": (((elig.get("usage") or {}).get("restrictions") or {}).get("description") or "").strip(),
        "who_can_apply": ((elig.get("applicant") or {}).get("description") or "").strip(),
        "award_range": parse_award_range(fin.get("description")),
        "financial_note": (fin.get("description") or "").strip()[:1200],
        "yearly_obligations": yearly[-4:],
        "website": (website.get("primary") or "").strip() if isinstance(website, dict) else "",
        "sam_url": VIEW_URL.format(id=listing_id),
        "source": "SAM.gov Assistance Listing",
    }


def sync_details(conn, numbers, max_age_hours=24 * 30, max_workers=4):
    """Full records for the given program numbers; refreshed monthly."""
    ids = {r["source_record_id"]: json.loads(r["payload"]).get("_id") for r in conn.execute(
        "SELECT source_record_id, payload FROM raw_records WHERE source='sam.gov-listing'")}
    todo = [(n, ids[n]) for n in sorted(set(numbers)) if ids.get(n)
            and not db.is_fresh(conn, "sam.gov-program", n, max_age_hours)]

    def fetch(job):
        num, lid = job
        try:
            return num, lid, _get(DETAIL_URL.format(id=lid)), None
        except Exception as e:
            return num, lid, None, e

    ok = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for num, lid, detail, err in ex.map(fetch, todo):
            if detail is None:
                db.log_fetch(conn, "sam.gov-program", num, 0, ok=False, error=err)
                continue
            db.upsert_raw(conn, "sam.gov-program", num, detail)
            rec = normalize(detail, lid)
            db.upsert_program(conn, num, rec["title"], "", rec)
            db.log_fetch(conn, "sam.gov-program", num, 1, ok=True)
            ok += 1
    conn.commit()
    return ok


def program_record(conn, cfda):
    if not cfda:
        return None
    r = conn.execute("SELECT data FROM programs WHERE cfda=?", (cfda,)).fetchone()
    return json.loads(r["data"]) if r else None


def applicant_type_matches(text, profile):
    """Recognize applicant types in SAM descriptions, not geographic mentions.

    This is a type check only; the notice may impose agency-specific limits.
    In particular, "United States" does not mean state agencies can apply.
    """
    import pipeline
    text = re.sub(r"\s+", " ", (text or "").lower())
    category = profile.get("category", "state")
    if category == "state":
        general = (r"\bstate(?:s)?(?:\s*[/,]\s*(?:(?:and|or)\s+)?local|\s+(?:and|or)\s+local)?\s+government",
                   r"\bstate agenc(?:y|ies)\b",
                   r"\bstates?\s*,\s*(?:count(?:y|ies)|cit(?:y|ies))\b",
                   r"^states?\s+and\s+partnerships\b")
        if any(re.search(pattern, text) for pattern in general):
            return True
        # Use the profile's specific state-agency role, e.g. DPI is a SEA.
        types = [t.lower() for t in profile.get("eligible_applicant_types", [])
                 if t.lower().startswith("state ") and t.lower() != "state governments"]
        patterns = [re.escape(t[:-3]) + r"(?:y|ies)" if t.endswith("agencies") else re.escape(t) for t in types]
        return any(re.search(r"\b" + pattern + r"\b", text) for pattern in patterns)
    return any(re.search(r"\b" + re.escape(word), text)
               for word in pipeline.CATEGORY_ELIGIBILITY_WORDS.get(category, []))


def score_programs(conn, profiles, per_office=15):
    """Fit of every active, funded program's purpose to every organization's goals.
    Returns the program numbers worth fetching details for."""
    import pipeline
    conn.executescript(SCHEMA)
    listings = [json.loads(r["payload"]) for r in conn.execute("SELECT payload FROM raw_records WHERE source='sam.gov-listing'")]
    pseudo = [{"id": l["programNumber"], "title": l.get("title") or "", "description": l.get("objective") or "",
               "eligibility": [], "status": "program"} for l in listings if l.get("isFunded", True)]
    pipeline.build_similarity_index(pseudo)
    wanted = set()
    for org_id, profile in profiles.items():
        scored = []
        for p in pseudo:
            if pipeline.eligibility_fit(p, profile)[0] <= 10:
                continue  # e.g. tribal-only programs for non-tribal organizations
            meta = pipeline.calculate_score(p, profile)
            fit = meta["score_breakdown"]["relevancy"]
            if fit >= 40:
                scored.append((fit, p, meta))
        scored.sort(key=lambda x: -x[0])
        conn.execute("DELETE FROM program_matches WHERE office_id=?", (org_id,))
        for fit, p, meta in scored[:per_office]:
            conn.execute("INSERT OR REPLACE INTO program_matches VALUES (?, ?, ?, ?)", (org_id, p["id"], fit, json.dumps({
                "cfda": p["id"], "title": p["title"], "fit": fit,
                "matched_goal_title": meta.get("matched_goal_title"), "matched_goal_citation": meta.get("matched_goal_citation"),
                "fit_evidence": meta.get("fit_evidence"), "shared_words": meta.get("shared_words"),
            })))
            wanted.add(p["id"])
    conn.commit()
    return wanted


def programs_for_office(conn, office_id):
    """An organization's best-fitting programs, with details and whether a grant is open now."""
    conn.executescript(SCHEMA)
    open_cfdas = {}
    for r in conn.execute("SELECT id, cfda, title, status FROM opportunities WHERE status != 'closed' AND cfda != ''"):
        open_cfdas.setdefault(r["cfda"], []).append({"id": r["id"], "title": r["title"], "status": r["status"]})
    out = []
    for r in conn.execute("SELECT data FROM program_matches WHERE office_id=? ORDER BY fit DESC", (office_id,)):
        m = json.loads(r["data"])
        m["program"] = program_record(conn, m["cfda"])
        m["open_grants"] = open_cfdas.get(m["cfda"], [])
        out.append(m)
    return out


def run(conn=None):
    import pipeline
    conn = conn or db.connect()
    conn.executescript(SCHEMA)
    got = sync_listings(conn)
    profiles = pipeline.load_agency_profiles()
    wanted = score_programs(conn, profiles)
    grant_cfdas = {r[0] for r in conn.execute("SELECT DISTINCT cfda FROM opportunities WHERE status != 'closed' AND cfda != ''")}
    fetched = sync_details(conn, wanted | grant_cfdas)
    return got, len(wanted), fetched


if __name__ == "__main__":
    got, wanted, fetched = run()
    print(f"SAM.gov: {got} listings refreshed, {wanted} programs matched to organizations, {fetched} program details fetched")
