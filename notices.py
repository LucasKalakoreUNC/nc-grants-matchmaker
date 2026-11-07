#!/usr/bin/env python3
"""
Funding notice documents: download, read, and record exact requirements.

Step 1 - download (no API key needed)
    sync_notice_files() downloads each grant's notice files from Grants.gov
    (the full announcement PDF, templates, FAQs) once, stores them under
    data/notices/<grant id>/, and saves their text page by page in the
    database. A file is only downloaded again if Grants.gov lists a new
    attachment for it.

Step 2 - requirements
    Each grant's notice is read and turned into a structured list of what
    the application must include, each item citing the file and page it
    came from. Two ways to fill it:
      * import:  load requirements already extracted (for example, read
                 during a working session) from a JSON file.
      * extract: have Claude read the notices automatically (needs
                 ANTHROPIC_API_KEY); meant for a weekly job that only
                 processes new or changed notices.

Run:  .venv/bin/python notices.py download
      .venv/bin/python notices.py dump <grant id>          # print notice text
      .venv/bin/python notices.py import <file.json>       # {grant id: requirements}
      .venv/bin/python notices.py extract [--limit N] [--ids 123,456]
      .venv/bin/python notices.py status
"""

import io
import os
import re
import sys
import json
import zipfile
import hashlib
import argparse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import db

NOTICE_DIR = os.path.join(db.DATA_DIR, "notices")
# One JSON file per grant, committed to git. This is the shared record of what
# each notice requires; the local database is rebuilt from it.
REQUIREMENTS_DIR = os.path.join(db.DATA_DIR, "requirements")
DOWNLOAD_URL = "https://apply07.grants.gov/grantsws/rest/opportunity/att/download/{}"
MODEL = "claude-opus-5-5"
TEXT_TYPES = {"pdf", "docx", "txt", "htm", "html"}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS notice_files (
    opportunity_id  TEXT NOT NULL,
    attachment_id   TEXT NOT NULL,
    file_name       TEXT,
    description     TEXT,
    file_type       TEXT,
    size_bytes      INTEGER,
    url             TEXT,
    local_path      TEXT,
    pages           INTEGER,
    text            TEXT,          -- extracted text with [page N] markers
    fetched_at      TEXT,
    error           TEXT,
    PRIMARY KEY (opportunity_id, attachment_id)
);
CREATE TABLE IF NOT EXISTS notice_requirements (
    opportunity_id  TEXT PRIMARY KEY,
    model           TEXT,          -- who/what read the notice
    source_hash     TEXT,          -- hash of the notice text that was read
    data            TEXT,          -- structured requirements (JSON)
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    extracted_at    TEXT,
    error           TEXT
);
"""


def connect():
    conn = db.connect()
    conn.executescript(SCHEMA_SQL)
    return conn


# ---------------------------------------------------------------- step 1: files

def attachments_for(detail):
    out = []
    for folder in detail.get("synopsisAttachmentFolders") or []:
        for a in folder.get("synopsisAttachments") or []:
            out.append({
                "attachment_id": str(a["id"]),
                "file_name": a.get("fileName") or a.get("linkUrl") or f"attachment-{a['id']}",
                "description": a.get("fileDescription") or folder.get("folderType") or "",
                "file_type": (a.get("fileExt") or os.path.splitext(a.get("fileName", ""))[1].lstrip(".")).lower(),
                "size_bytes": a.get("fileLobSize"),
                "url": DOWNLOAD_URL.format(a["id"]),
            })
    return out


def extract_text(data, file_type):
    """Return (text with page markers, page count)."""
    if file_type == "pdf":
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(data))
        pages = []
        for n, page in enumerate(reader.pages, 1):
            try:
                pages.append(f"[page {n}]\n{page.extract_text() or ''}")
            except Exception:
                pages.append(f"[page {n}]\n(unreadable page)")
        return "\n".join(pages), len(reader.pages)
    if file_type == "docx":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
        xml = re.sub(r"</w:p>", "\n", xml)
        return re.sub(r"<[^>]+>", "", xml), None
    if file_type in ("txt", "htm", "html"):
        return re.sub(r"<[^>]+>", " ", data.decode("utf-8", "ignore")), None
    return None, None


def _download(job):
    opp_id, att = job
    folder = os.path.join(NOTICE_DIR, opp_id)
    os.makedirs(folder, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._ ()-]+", "_", att["file_name"])[:150]
    path = os.path.join(folder, f"{att['attachment_id']}_{safe}")
    try:
        req = urllib.request.Request(att["url"], headers={"User-Agent": "NC-Grants-Matchmaker/2.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        with open(path, "wb") as f:
            f.write(data)
        text, pages = (None, None)
        if att["file_type"] in TEXT_TYPES:
            try:
                text, pages = extract_text(data, att["file_type"])
            except Exception as e:
                return opp_id, att, path, None, None, None
        return opp_id, att, path, text, pages, None
    except Exception as e:
        return opp_id, att, None, None, None, str(e)[:300]


def sync_notice_files(conn, max_workers=6):
    """Download notice files we don't have yet. Returns (downloaded, already_had, failed)."""
    have = {(r[0], r[1]) for r in conn.execute(
        "SELECT opportunity_id, attachment_id FROM notice_files WHERE error IS NULL")}
    open_ids = {r[0] for r in conn.execute("SELECT id FROM opportunities WHERE status != 'closed'")}
    jobs, already = [], 0
    for oid, payload in conn.execute(
            "SELECT source_record_id, payload FROM raw_records WHERE source='grants.gov-detail'"):
        if oid not in open_ids:
            continue
        for att in attachments_for(json.loads(payload)):
            if (oid, att["attachment_id"]) in have:
                already += 1
            else:
                jobs.append((oid, att))
    done = failed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for oid, att, path, text, pages, err in ex.map(_download, jobs):
            conn.execute(
                "INSERT OR REPLACE INTO notice_files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (oid, att["attachment_id"], att["file_name"], att["description"], att["file_type"],
                 att["size_bytes"], att["url"], os.path.relpath(path, db.BASE_DIR) if path else None,
                 pages, text, db.now(), err))
            done += err is None
            failed += err is not None
            if (done + failed) % 100 == 0:
                conn.commit()
                print(f"  ... {done + failed}/{len(jobs)} files", flush=True)
    conn.commit()
    return done, already, failed


def html_to_text(raw):
    import html as _html
    raw = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", raw)
    text = _html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


NIH_GUIDE = {"PA": "pa-files", "PAR": "pa-files", "PAS": "pa-files", "RFA": "rfa-files", "NOT": "notice-files"}


def announcement_url(detail, number):
    """Where the full announcement lives when Grants.gov has no attachment."""
    syn = detail.get("synopsis") or detail.get("forecast") or {}
    url = (syn.get("fundingDescLinkUrl") or "").strip()
    if url.startswith("http"):
        return url.replace("http://", "https://", 1)
    prefix = (number or "").split("-")[0]
    if prefix in NIH_GUIDE and re.match(r"^[A-Z]+-[A-Z]{0,3}-?\d{2}-\d{3}$", number or ""):
        return f"https://grants.nih.gov/grants/guide/{NIH_GUIDE[prefix]}/{number}.html"
    return None


def _fetch_page(job):
    opp_id, url = job
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (NC-Grants-Matchmaker)"})
        with urllib.request.urlopen(req, timeout=45) as r:
            ctype = r.headers.get("Content-Type", "")
            data = r.read()
        if "pdf" in ctype or url.lower().endswith(".pdf"):
            text, pages = extract_text(data, "pdf")
        else:
            text, pages = html_to_text(data.decode("utf-8", "ignore")), None
        if not text or len(text) < 500:
            return opp_id, url, None, None, "page had too little text"
        return opp_id, url, text, pages, None
    except Exception as e:
        return opp_id, url, None, None, str(e)[:300]


def sync_linked_notices(conn, max_workers=4):
    """For grants with no Grants.gov attachments, fetch the full announcement
    page the listing links to (NSF, NIH, HUD, HRSA and others). Once per grant."""
    have = {r[0] for r in conn.execute("SELECT DISTINCT opportunity_id FROM notice_files")}
    jobs = []
    for oid, number, payload in conn.execute(
            "SELECT o.id, o.number, r.payload FROM opportunities o JOIN raw_records r "
            "ON r.source='grants.gov-detail' AND r.source_record_id=o.id WHERE o.status != 'closed'"):
        if oid in have:
            continue
        url = announcement_url(json.loads(payload), number)
        if url:
            jobs.append((oid, url))
    done = failed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for oid, url, text, pages, err in ex.map(_fetch_page, jobs):
            name = url.rstrip("/").split("/")[-1][:120] or "announcement"
            conn.execute("INSERT OR REPLACE INTO notice_files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (oid, "link", name, "Full announcement (agency website)", "pdf" if pages else "html",
                          None, url, None, pages, text, db.now(), err))
            done += err is None
            failed += err is not None
    conn.commit()
    return done, failed


def files_for(conn, opp_id):
    rows = conn.execute(
        "SELECT attachment_id, file_name, description, file_type, size_bytes, url, pages, "
        "text IS NOT NULL AS readable FROM notice_files WHERE opportunity_id=? AND error IS NULL ORDER BY file_name",
        (opp_id,))
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- step 2: requirements

REQUIREMENTS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["required_documents", "deadlines", "eligibility", "cost_share", "review_criteria",
                 "submission_limits", "important_notes"],
    "properties": {
        "required_documents": {
            "type": "array",
            "description": "Every form, narrative, attachment, letter or registration the applicant must submit or complete.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "category", "required", "details", "source"],
                "properties": {
                    "name": {"type": "string"},
                    "category": {"type": "string", "enum": ["Standard forms", "Project narrative", "Budget", "Attachments", "Letters and partnerships", "Registrations", "Other"]},
                    "required": {"type": "boolean"},
                    "details": {"type": "string"},
                    "source": {"type": "string"},
                },
            },
        },
        "deadlines": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False, "required": ["what", "when", "source"],
                "properties": {"what": {"type": "string"}, "when": {"type": "string"}, "source": {"type": "string"}},
            },
        },
        "eligibility": {
            "type": "object", "additionalProperties": False, "required": ["summary", "source"],
            "properties": {"summary": {"type": "string"}, "source": {"type": "string"}},
        },
        "cost_share": {
            "type": "object", "additionalProperties": False, "required": ["required", "details", "source"],
            "properties": {
                "required": {"type": "string", "enum": ["yes", "no", "not stated"]},
                "details": {"type": "string"}, "source": {"type": "string"},
            },
        },
        "review_criteria": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False, "required": ["criterion", "weight", "source"],
                "properties": {"criterion": {"type": "string"}, "weight": {"type": "string"}, "source": {"type": "string"}},
            },
        },
        "funded_activities": {
            "type": "array",
            "description": "What the money can pay for, in the notice's own terms, so staff can see how their organization could use it.",
            "items": {
                "type": "object", "additionalProperties": False, "required": ["activity", "source"],
                "properties": {"activity": {"type": "string"}, "source": {"type": "string"}},
            },
        },
        "submission_limits": {"type": "string"},
        "important_notes": {"type": "array", "items": {"type": "string"}},
    },
}

SYSTEM_PROMPT = """You read federal funding notices (NOFOs) for grant staff at North Carolina state and local government offices. Your job is to list exactly what an applicant must prepare and submit, taken only from the notice text you are given.

Rules:
- Use only what the notice says. Do not add documents that are commonly required elsewhere unless this notice requires them.
- For every item, cite where it came from as "<file name> p. <page>", using the [page N] markers in the text. If a file has no page markers, cite the file name and section heading.
- If the notice text does not cover something (for example no cost share is mentioned), say "not stated" rather than guessing.
- Write in plain language a non-specialist can follow. Keep each detail to one or two sentences.
- In funded_activities, list what the money can pay for (eligible activities and allowable costs) and anything it explicitly cannot pay for, so staff can judge how their organization would use it.
- Notices often point to a separate agency application guide for standard forms; when they do, list the forms the notice names and note the guide in important_notes."""


def notice_text(conn, opp_id):
    rows = conn.execute(
        "SELECT attachment_id, file_name, description, text FROM notice_files WHERE opportunity_id=? AND text IS NOT NULL "
        "ORDER BY CASE WHEN lower(description) LIKE '%full%' OR lower(file_name) LIKE '%nofo%' OR lower(file_name) LIKE '%foa%' THEN 0 ELSE 1 END, file_name",
        (opp_id,)).fetchall()
    # Grants.gov keeps every revision of the announcement ("Foa_Content_of_..."); read only the newest.
    foa = [r for r in rows if r["file_name"].lower().startswith("foa_content_of")]
    newest = max(foa, key=lambda r: int(r["attachment_id"])) if foa else None
    rows = [r for r in rows if r not in foa or r is newest]
    parts = [f"===== FILE: {r['file_name']} ({r['description']}) =====\n{r['text']}" for r in rows if (r["text"] or "").strip()]
    return "\n\n".join(parts)


FOCUS_TERMS = ("eligib", "page limit", "must submit", "must include", "required", "attachment", "narrative",
               "budget", "deadline", "due date", "cost shar", "matching", "review criteria", "points", "sf-424",
               "letter of", "registration", "sam.gov", "funding restriction", "allowable", "unallowable",
               "may be used", "may not be used", "eligible activities", "maximum", "font", "single-spaced")


# Standard notice section headings; a page with one is always kept.
FOCUS_HEADINGS = ("funding restriction", "eligibility information", "eligible applicants", "content and form",
                  "review criteria", "cost sharing", "application and submission", "submission dates",
                  "allowable costs", "unallowable costs", "required registrations")


def focus_text(text, min_hits=5):
    """Keep only pages about requirements (eligibility, contents, deadlines,
    budget, review, allowable costs). Typically 40-70% of a notice."""
    pages = re.split(r"(?=\[page \d+\])", text)
    keep = []
    for p in pages:
        low = p.lower()
        if p.startswith("=====") or "[page" not in p[:12] or any(h in low for h in FOCUS_HEADINGS) \
                or sum(low.count(t) for t in FOCUS_TERMS) >= min_hits:
            keep.append(p)
    return "".join(keep)


def text_hash(conn, opp_id):
    return hashlib.sha256(notice_text(conn, opp_id).encode("utf-8")).hexdigest()


def save_requirements(conn, opp_id, data, reader):
    """Validate the basic shape and store requirements for one grant."""
    missing = [k for k in REQUIREMENTS_SCHEMA["required"] if k not in data]
    if missing:
        raise ValueError(f"{opp_id}: missing fields {missing}")
    conn.execute("INSERT OR REPLACE INTO notice_requirements VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (opp_id, reader, text_hash(conn, opp_id), json.dumps(data), None, None, db.now(), None))


def extract_requirements(conn, opp_id, client=None, force=False):
    """Read one grant's notice with the Claude API and store the structured requirements."""
    import anthropic
    text = notice_text(conn, opp_id)
    if not text.strip():
        return "no-text"
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    row = conn.execute("SELECT source_hash, error FROM notice_requirements WHERE opportunity_id=?", (opp_id,)).fetchone()
    if row and row["source_hash"] == h and not row["error"] and not force:
        return "unchanged"

    opp = conn.execute("SELECT title, number, agency FROM opportunities WHERE id=?", (opp_id,)).fetchone()
    client = client or anthropic.Anthropic()
    with client.messages.stream(
        model=MODEL,
        max_tokens=32000,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": REQUIREMENTS_SCHEMA}},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content":
                   f"Grant: {opp['title']} ({opp['number']}), {opp['agency']}\n\n"
                   f"Funding notice files:\n\n{text}\n\n"
                   "List what this application requires, following the rules."}],
    ) as stream:
        msg = stream.get_final_message()

    err, data = None, None
    if msg.stop_reason == "refusal":
        err = "Model declined to process this notice"
    elif msg.stop_reason == "max_tokens":
        err = "Output was cut off before finishing"
    else:
        data = next(b.text for b in msg.content if b.type == "text")
        json.loads(data)
    conn.execute("INSERT OR REPLACE INTO notice_requirements VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (opp_id, MODEL, h, data, msg.usage.input_tokens, msg.usage.output_tokens, db.now(), err))
    conn.commit()
    if data:
        write_requirements_file(opp_id, json.loads(data), MODEL)
    return "error" if err else "extracted"


def write_requirements_file(opp_id, data, reader):
    os.makedirs(REQUIREMENTS_DIR, exist_ok=True)
    with open(os.path.join(REQUIREMENTS_DIR, f"{opp_id}.json"), "w", encoding="utf-8") as f:
        json.dump({"opportunity_id": opp_id, "read_by": reader, "read_at": db.now(), **data}, f, indent=2, ensure_ascii=False)


def load_requirements_dir(conn):
    """Load every data/requirements/<id>.json into the database. Returns count loaded."""
    if not os.path.isdir(REQUIREMENTS_DIR):
        return 0
    n = 0
    for name in sorted(os.listdir(REQUIREMENTS_DIR)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(REQUIREMENTS_DIR, name), encoding="utf-8") as f:
            data = json.load(f)
        oid = str(data.pop("opportunity_id", name[:-5]))
        if not conn.execute("SELECT 1 FROM opportunities WHERE id=?", (oid,)).fetchone():
            continue
        reader = data.pop("read_by", "unknown")
        read_at = data.pop("read_at", None)
        save_requirements(conn, oid, data, reader)
        if read_at:
            conn.execute("UPDATE notice_requirements SET extracted_at=? WHERE opportunity_id=?", (read_at, oid))
        n += 1
    conn.commit()
    return n


BOILERPLATE_MARKERS = ("Detailed budget breakdown and justification explaining all direct costs",
                       "Standard federal grant application cover form submitted through Grants.gov")
_STOP = set("the and for with from that this must your will each all any are not into than more such per".split())


def _check_item(text_low, *fields):
    """True if an item's specific numbers and main words appear in the notice text."""
    blob = " ".join(f for f in fields if f)
    nums = set(re.findall(r"\$?\d[\d,]*(?:\.\d+)?%?", blob))
    nums = {n for n in nums if len(n.strip("$%,")) and n not in ("1", "2", "3")}
    for n in nums:
        bare = n.replace("$", "").replace(",", "")
        if n.lower() not in text_low and bare not in text_low.replace(",", ""):
            return False
    words = [w for w in re.findall(r"[a-z]{4,}", (fields[0] or "").lower()) if w not in _STOP]
    if words and sum(w in text_low for w in words) < max(1, len(words) // 2):
        return False
    return True


def verify_requirements(conn, opp_id, data):
    """Mark each item as found / not found in the stored notice text.
    Returns None if the file is a generic template or the grant has no notice text."""
    text = notice_text(conn, opp_id).lower()
    docs = data.get("required_documents") or []
    if not text.strip() or any(m in (x.get("details") or "") for x in docs for m in BOILERPLATE_MARKERS):
        return None
    for x in docs:
        x["verified"] = _check_item(text, x.get("name"), x.get("details"))
    for x in data.get("deadlines") or []:
        x["verified"] = _check_item(text, x.get("what"), x.get("when"))
    for x in data.get("review_criteria") or []:
        x["verified"] = _check_item(text, x.get("criterion"), x.get("weight"))
    data["verified_count"] = sum(1 for x in docs if x.get("verified"))
    data["item_count"] = len(docs)
    return data


def requirements_for(conn, opp_id):
    row = conn.execute("SELECT * FROM notice_requirements WHERE opportunity_id=?", (opp_id,)).fetchone()
    if not row or not row["data"]:
        return None
    out = verify_requirements(conn, opp_id, json.loads(row["data"]))
    if out is None:
        return None
    out["read_by"] = row["model"]
    out["read_at"] = row["extracted_at"]
    out["out_of_date"] = row["source_hash"] != text_hash(conn, opp_id)
    return out


def priority_ids(conn):
    """Grants with readable notices and no requirements yet, best matches first."""
    rows = conn.execute("""
        SELECT nf.opportunity_id, MAX(m.total_score) AS best
        FROM notice_files nf JOIN matches m ON m.opportunity_id = nf.opportunity_id
        JOIN opportunities o ON o.id = nf.opportunity_id
        WHERE nf.text IS NOT NULL AND o.status != 'closed'
          AND nf.opportunity_id NOT IN (SELECT opportunity_id FROM notice_requirements WHERE data IS NOT NULL)
        GROUP BY nf.opportunity_id ORDER BY best DESC""").fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["download", "dump", "import", "load", "extract", "status", "queue"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--ids", default="")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--focus", action="store_true", help="dump: only pages about requirements")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--reader", default="Claude (working session)")
    args = ap.parse_args()
    conn = connect()

    if args.command == "download":
        done, already, failed = sync_notice_files(conn)
        print(f"Notice files: {done} downloaded, {already} already stored, {failed} failed")
        got, bad = sync_linked_notices(conn)
        print(f"Announcements on agency websites: {got} fetched, {bad} could not be fetched")

    elif args.command == "load":
        print(f"Loaded {load_requirements_dir(conn)} requirement files from data/requirements/")

    elif args.command == "dump":
        text = notice_text(conn, args.arg)
        print(focus_text(text) if args.focus else text)

    elif args.command == "queue":
        ids = priority_ids(conn)
        print("\n".join(ids[: args.limit] if args.limit else ids))

    elif args.command == "import":
        with open(args.arg, encoding="utf-8") as f:
            payload = json.load(f)
        for oid, data in payload.items():
            save_requirements(conn, oid, data, args.reader)   # validates before writing
            write_requirements_file(oid, data, args.reader)
        conn.commit()
        print(f"Saved requirements for {len(payload)} grants to data/requirements/")

    elif args.command == "extract":
        import anthropic
        client = anthropic.Anthropic()
        ids = [i for i in args.ids.split(",") if i] or priority_ids(conn)
        if args.limit:
            ids = ids[: args.limit]

        def run(oid):
            try:
                return oid, extract_requirements(connect(), oid, client, force=args.force)
            except anthropic.AuthenticationError:
                return oid, "error: no valid Anthropic credentials (set ANTHROPIC_API_KEY)"
            except Exception as e:
                return oid, f"error: {str(e)[:120]}"

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for n, (oid, res) in enumerate(ex.map(run, ids), 1):
                print(f"[{n}/{len(ids)}] {oid}: {res}", flush=True)

    else:
        q = lambda sql: conn.execute(sql).fetchone()[0]
        print(json.dumps({
            "grants_with_notice_files": q("SELECT COUNT(DISTINCT opportunity_id) FROM notice_files WHERE error IS NULL"),
            "grants_with_readable_text": q("SELECT COUNT(DISTINCT opportunity_id) FROM notice_files WHERE text IS NOT NULL"),
            "files": q("SELECT COUNT(*) FROM notice_files WHERE error IS NULL"),
            "failed_files": q("SELECT COUNT(*) FROM notice_files WHERE error IS NOT NULL"),
            "requirements_recorded": q("SELECT COUNT(*) FROM notice_requirements WHERE data IS NOT NULL"),
        }, indent=2))


if __name__ == "__main__":
    main()
