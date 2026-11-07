#!/usr/bin/env python3
"""
Local grants database (SQLite).

Every source (Grants.gov, SAM.gov, Federal Register, USAspending, NC OpenBudget)
writes into the same tables, so we keep what we have already pulled instead of
re-downloading it on every run:

  fetch_log           - when each source/query was last pulled; used to skip
                        queries that are still fresh (see is_fresh()).
  raw_records         - the untouched payload from each source, keyed by
                        (source, source_record_id), with a content hash so
                        unchanged records are not re-processed.
  opportunities       - one normalized record per grant (full record kept in `data`).
  opportunity_sources - links a grant to every source that mentions it.
  programs            - Assistance Listing (CFDA) program-level details from SAM.gov.
  past_awards         - historical awards (USAspending), keyed by CFDA.
  matches             - each grant's score for each office.
  decisions           - Pursue / Watch / Pass per office.

The database file lives at data/matchmaker.db. If it does not exist, it is
built from the JSON files already in data/ (see bootstrap_from_json()).
"""

import os
import json
import sqlite3
import hashlib
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.environ.get("MATCHMAKER_DB", os.path.join(DATA_DIR, "matchmaker.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    base_url      TEXT,
    last_success  TEXT,
    last_error    TEXT
);

CREATE TABLE IF NOT EXISTS fetch_log (
    source        TEXT NOT NULL,
    query         TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    record_count  INTEGER NOT NULL DEFAULT 0,
    ok            INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (source, query)
);

CREATE TABLE IF NOT EXISTS raw_records (
    source            TEXT NOT NULL,
    source_record_id  TEXT NOT NULL,
    payload           TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL,
    last_changed      TEXT NOT NULL,
    PRIMARY KEY (source, source_record_id)
);

CREATE TABLE IF NOT EXISTS opportunities (
    id            TEXT PRIMARY KEY,
    number        TEXT,
    cfda          TEXT,
    title         TEXT,
    agency        TEXT,
    status        TEXT,            -- forecasted | posted | closed | archived
    open_date     TEXT,
    close_date    TEXT,
    award_ceiling INTEGER,
    url           TEXT,
    data          TEXT NOT NULL,   -- full normalized + enriched record (JSON)
    content_hash  TEXT NOT NULL,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    last_changed  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opp_cfda ON opportunities(cfda);
CREATE INDEX IF NOT EXISTS idx_opp_status ON opportunities(status);

CREATE TABLE IF NOT EXISTS opportunity_sources (
    opportunity_id    TEXT NOT NULL,
    source            TEXT NOT NULL,
    source_record_id  TEXT NOT NULL,
    source_url        TEXT,
    PRIMARY KEY (opportunity_id, source, source_record_id)
);

CREATE TABLE IF NOT EXISTS programs (
    cfda          TEXT PRIMARY KEY,
    title         TEXT,
    agency        TEXT,
    data          TEXT NOT NULL,
    fetched_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS past_awards (
    award_id      TEXT NOT NULL,
    source        TEXT NOT NULL,
    cfda          TEXT,
    recipient     TEXT,
    amount        REAL,
    fiscal_year   INTEGER,
    location      TEXT,
    data          TEXT NOT NULL,
    PRIMARY KEY (source, award_id)
);
CREATE INDEX IF NOT EXISTS idx_awards_cfda ON past_awards(cfda);

CREATE TABLE IF NOT EXISTS matches (
    office_id       TEXT NOT NULL,
    opportunity_id  TEXT NOT NULL,
    total_score     REAL NOT NULL,
    data            TEXT NOT NULL,  -- score details (reason, goal, breakdown...)
    scored_at       TEXT NOT NULL,
    PRIMARY KEY (office_id, opportunity_id)
);
CREATE INDEX IF NOT EXISTS idx_matches_office ON matches(office_id, total_score DESC);

CREATE TABLE IF NOT EXISTS decisions (
    office_id       TEXT NOT NULL,
    opportunity_id  TEXT NOT NULL,
    number          TEXT,
    title           TEXT,
    status          TEXT NOT NULL,
    notes           TEXT,
    decided_by      TEXT,
    decided_at      TEXT NOT NULL,
    PRIMARY KEY (office_id, opportunity_id)
);
"""

KNOWN_SOURCES = [
    ("grants.gov", "Grants.gov", "https://api.grants.gov/v1/api/"),
    ("sam.gov", "SAM.gov Assistance Listings", "https://api.sam.gov/"),
    ("federalregister", "Federal Register", "https://www.federalregister.gov/api/v1/"),
    ("usaspending", "USAspending.gov", "https://api.usaspending.gov/api/v2/"),
    ("nc-openbudget", "NC OSBM OpenBudget", "https://www.osbm.nc.gov/"),
]


def now():
    return datetime.now().isoformat(timespec="seconds")


def content_hash(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()


_initialized = False


def connect():
    """Open the database, creating and seeding it on first use."""
    global _initialized
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    if not _initialized:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT OR IGNORE INTO sources (id, name, base_url) VALUES (?, ?, ?)", KNOWN_SOURCES)
        conn.commit()
        empty = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 0
        _initialized = True
        if empty:
            bootstrap_from_json(conn)
    return conn


# ---------------------------------------------------------------- fetch cache

def is_fresh(conn, source, query, max_age_hours):
    """True if this source/query was pulled successfully within max_age_hours."""
    row = conn.execute(
        "SELECT fetched_at FROM fetch_log WHERE source=? AND query=? AND ok=1", (source, query)
    ).fetchone()
    if not row:
        return False
    return datetime.fromisoformat(row["fetched_at"]) > datetime.now() - timedelta(hours=max_age_hours)


def log_fetch(conn, source, query, record_count, ok=True, error=None):
    conn.execute(
        "INSERT OR REPLACE INTO fetch_log (source, query, fetched_at, record_count, ok) VALUES (?, ?, ?, ?, ?)",
        (source, query, now(), record_count, 1 if ok else 0),
    )
    if ok:
        conn.execute("UPDATE sources SET last_success=?, last_error=NULL WHERE id=?", (now(), source))
    else:
        conn.execute("UPDATE sources SET last_error=? WHERE id=?", (str(error)[:500], source))


# ---------------------------------------------------------------- raw records

def upsert_raw(conn, source, source_record_id, payload):
    """Store a raw record. Returns 'new', 'changed' or 'unchanged'."""
    h = content_hash(payload)
    ts = now()
    row = conn.execute(
        "SELECT content_hash FROM raw_records WHERE source=? AND source_record_id=?", (source, source_record_id)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO raw_records VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source, source_record_id, json.dumps(payload), h, ts, ts, ts),
        )
        return "new"
    if row["content_hash"] == h:
        conn.execute("UPDATE raw_records SET last_seen=? WHERE source=? AND source_record_id=?", (ts, source, source_record_id))
        return "unchanged"
    conn.execute(
        "UPDATE raw_records SET payload=?, content_hash=?, last_seen=?, last_changed=? WHERE source=? AND source_record_id=?",
        (json.dumps(payload), h, ts, ts, source, source_record_id),
    )
    return "changed"


def raw_records(conn, source):
    return [json.loads(r["payload"]) for r in conn.execute("SELECT payload FROM raw_records WHERE source=?", (source,))]


# ---------------------------------------------------------------- opportunities

def upsert_opportunity(conn, opp, source=None, source_record_id=None, source_url=None):
    """Insert or update a normalized grant. Returns 'new', 'changed' or 'unchanged'."""
    ts = now()
    h = content_hash({k: v for k, v in opp.items() if k not in ("ingested_at", "days_to_close")})
    row = conn.execute("SELECT content_hash FROM opportunities WHERE id=?", (opp["id"],)).fetchone()
    cols = (opp.get("number"), opp.get("cfda"), opp.get("title"), opp.get("agency"), opp.get("status"),
            opp.get("open_date"), opp.get("close_date"), opp.get("award_ceiling"), opp.get("url"), json.dumps(opp), h)
    if row is None:
        conn.execute(
            "INSERT INTO opportunities (number, cfda, title, agency, status, open_date, close_date, award_ceiling, url, data, content_hash, id, first_seen, last_seen, last_changed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", cols + (opp["id"], ts, ts, ts))
        result = "new"
    elif row["content_hash"] != h:
        conn.execute(
            "UPDATE opportunities SET number=?, cfda=?, title=?, agency=?, status=?, open_date=?, close_date=?, award_ceiling=?, url=?, data=?, content_hash=?, last_seen=?, last_changed=? WHERE id=?",
            cols + (ts, ts, opp["id"]))
        result = "changed"
    else:
        conn.execute("UPDATE opportunities SET last_seen=? WHERE id=?", (ts, opp["id"]))
        result = "unchanged"
    if source:
        conn.execute("INSERT OR IGNORE INTO opportunity_sources VALUES (?, ?, ?, ?)",
                     (opp["id"], source, source_record_id or opp["id"], source_url or opp.get("url")))
    return result


def mark_closed(conn, today_str):
    """Grants past their close date become 'closed' (kept for history, never deleted)."""
    cur = conn.execute(
        "UPDATE opportunities SET status='closed' WHERE status='posted' AND close_date GLOB '[0-9]*' AND close_date < ?",
        (today_str,))
    return cur.rowcount


def opportunities(conn, include_closed=False):
    sql = "SELECT data, status FROM opportunities" + ("" if include_closed else " WHERE status != 'closed'")
    out = []
    for r in conn.execute(sql):
        d = json.loads(r["data"])
        d["status"] = r["status"]
        out.append(d)
    return out


# ---------------------------------------------------------------- matches

def save_matches(conn, office_id, scored_items):
    ts = now()
    conn.executemany(
        "INSERT OR REPLACE INTO matches VALUES (?, ?, ?, ?, ?)",
        [(office_id, s["id"], s.get("total_score", 0), json.dumps(s), ts) for s in scored_items],
    )


def scored_for_office(conn, office_id, include_closed=False):
    """Full scored records for one office, best first (same shape as the old latest.json)."""
    sql = ("SELECT o.data AS opp, o.status AS status, m.data AS match FROM matches m "
           "JOIN opportunities o ON o.id = m.opportunity_id WHERE m.office_id=?"
           + ("" if include_closed else " AND o.status != 'closed'")
           + " ORDER BY m.total_score DESC")
    out = []
    for r in conn.execute(sql, (office_id,)):
        item = json.loads(r["opp"])
        item.update(json.loads(r["match"]))
        item["status"] = r["status"]
        out.append(item)
    return out


def scored_best(conn, office_ids):
    """Each open grant once, scored for whichever of office_ids it fits best."""
    if not office_ids:
        return []
    marks = ",".join("?" * len(office_ids))
    sql = f"""
        SELECT o.data AS opp, o.status AS status, m.data AS match, m.office_id AS office_id FROM (
            SELECT opportunity_id, office_id, data, total_score,
                   ROW_NUMBER() OVER (PARTITION BY opportunity_id ORDER BY total_score DESC) AS rn
            FROM matches WHERE office_id IN ({marks})
        ) m JOIN opportunities o ON o.id = m.opportunity_id
        WHERE m.rn = 1 AND o.status != 'closed'
        ORDER BY m.total_score DESC"""
    out = []
    for r in conn.execute(sql, list(office_ids)):
        item = json.loads(r["opp"])
        item.update(json.loads(r["match"]))
        item["status"] = r["status"]
        item["best_office_id"] = r["office_id"]
        out.append(item)
    return out


# ---------------------------------------------------------------- decisions

def get_decisions(conn, office_id):
    rows = conn.execute("SELECT * FROM decisions WHERE office_id=? ORDER BY decided_at DESC", (office_id,))
    return {"agency_id": office_id, "decisions": [
        {k: r[k] for k in ("opportunity_id", "number", "title", "status", "notes", "decided_by", "decided_at")}
        for r in rows]}


def top_per_office(conn, office_ids, per=5, min_score=0):
    """Each organization's own top-scoring open grants (a grant can appear under several)."""
    if not office_ids:
        return []
    marks = ",".join("?" * len(office_ids))
    sql = f"""
        SELECT o.data AS opp, o.status AS status, m.data AS match, m.office_id AS office_id FROM (
            SELECT mm.opportunity_id, mm.office_id, mm.data, mm.total_score,
                   ROW_NUMBER() OVER (PARTITION BY mm.office_id ORDER BY mm.total_score DESC) AS rn
            FROM matches mm JOIN opportunities oo ON oo.id = mm.opportunity_id
            WHERE mm.office_id IN ({marks}) AND oo.status != 'closed' AND mm.total_score >= ?
        ) m JOIN opportunities o ON o.id = m.opportunity_id
        WHERE m.rn <= ? ORDER BY m.office_id, m.total_score DESC"""
    out = []
    for r in conn.execute(sql, list(office_ids) + [min_score, per]):
        item = json.loads(r["opp"])
        item.update(json.loads(r["match"]))
        item["status"] = r["status"]
        item["best_office_id"] = r["office_id"]
        out.append(item)
    return out


def get_decisions_for(conn, office_ids):
    """Decisions from several offices (most recent first), each tagged with its office."""
    marks = ",".join("?" * len(office_ids)) or "''"
    rows = conn.execute(f"SELECT * FROM decisions WHERE office_id IN ({marks}) ORDER BY decided_at DESC", list(office_ids))
    return {"agency_id": "all", "decisions": [
        {k: r[k] for k in ("office_id", "opportunity_id", "number", "title", "status", "notes", "decided_by", "decided_at")}
        for r in rows]}


def save_decision(conn, office_id, d):
    conn.execute(
        "INSERT OR REPLACE INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (office_id, str(d["opportunity_id"]), d.get("number", ""), d.get("title", ""), d.get("status", "Pursue"),
         d.get("notes", ""), d.get("decided_by", ""), d.get("decided_at") or now()),
    )


# ---------------------------------------------------------------- programs / awards (enrichment sources)

def upsert_program(conn, cfda, title, agency, data):
    conn.execute("INSERT OR REPLACE INTO programs VALUES (?, ?, ?, ?, ?)", (cfda, title, agency, json.dumps(data), now()))


def upsert_award(conn, source, award_id, cfda, recipient, amount, fiscal_year, location, data):
    conn.execute("INSERT OR REPLACE INTO past_awards VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (award_id, source, cfda, recipient, amount, fiscal_year, location, json.dumps(data)))


def awards_for_cfda(conn, cfda, limit=10):
    rows = conn.execute("SELECT * FROM past_awards WHERE cfda=? ORDER BY fiscal_year DESC, amount DESC LIMIT ?", (cfda, limit))
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- stats

def stats(conn):
    q = lambda sql: conn.execute(sql).fetchone()[0]
    return {
        "opportunities": q("SELECT COUNT(*) FROM opportunities"),
        "open": q("SELECT COUNT(*) FROM opportunities WHERE status='posted'"),
        "forecasted": q("SELECT COUNT(*) FROM opportunities WHERE status='forecasted'"),
        "closed": q("SELECT COUNT(*) FROM opportunities WHERE status='closed'"),
        "raw_records": q("SELECT COUNT(*) FROM raw_records"),
        "programs": q("SELECT COUNT(*) FROM programs"),
        "past_awards": q("SELECT COUNT(*) FROM past_awards"),
        "decisions": q("SELECT COUNT(*) FROM decisions"),
        "sources": [dict(r) for r in conn.execute("SELECT * FROM sources")],
    }


# ---------------------------------------------------------------- one-time import of existing JSON files

def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def bootstrap_from_json(conn):
    """Seed an empty database with the raw Grants.gov search results committed in
    data/raw/ so a fresh checkout works offline. Run pipeline.py to build the rest."""
    raw_dir = os.path.join(DATA_DIR, "raw")
    if os.path.isdir(raw_dir):
        for day in sorted(os.listdir(raw_dir)):
            for hit in _load(os.path.join(raw_dir, day, "grants.json"), []):
                if hit.get("id"):
                    upsert_raw(conn, "grants.gov", str(hit["id"]), hit)

    conn.commit()
    print(f"[DB] Seeded {DB_PATH} from JSON files: {stats(conn)['opportunities']} opportunities.")


if __name__ == "__main__":
    c = connect()
    s = stats(c)
    s.pop("sources")
    print(json.dumps(s, indent=2))
