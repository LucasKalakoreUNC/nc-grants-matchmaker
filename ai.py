#!/usr/bin/env python3
"""
Claude-assisted steps (need ANTHROPIC_API_KEY, or an `ant auth login` profile).

  profile  Turn an organization's published strategic plan (PDF or web page)
           into data/profiles/<id>.json: goals, objectives, page citations and
           search phrases. This is how a new town or county is added.

           .venv/bin/python ai.py profile town_cary --name "Town of Cary" \
               --category municipal --url https://.../strategic-plan.pdf

  review   For each organization's top-scoring grants, judge whether the grant
           really serves one of its goals and write one plain sentence on how
           the organization could use it. Results go to
           data/reviews/<org>/<grant>.json and are shown on the grant page.
           A review is redone only if the grant or the profile changed.

           .venv/bin/python ai.py review --top 20 [--org nc_dit]

Both write plain JSON files that can be committed, like data/requirements/.
"""

import os
import re
import json
import hashlib
import argparse
import urllib.request

import db
import notices

MODEL = "claude-opus-5-5"
PROFILES_DIR = os.path.join(db.DATA_DIR, "profiles")
REVIEWS_DIR = os.path.join(db.DATA_DIR, "reviews")
PLANS_DIR = os.path.join(db.DATA_DIR, "plans")

CATEGORY_TYPES = {
    "state": ["State governments"],
    "county": ["County governments", "Special district governments"],
    "municipal": ["City or township governments", "Special district governments"],
    "education": ["Public and State controlled institutions of higher education"],
    "business": ["Small businesses"],
}


def client():
    import anthropic
    return anthropic.Anthropic()


def provider():
    """Which AI service is configured: Azure OpenAI first, then Anthropic, else None."""
    if os.environ.get("AZURE_OPENAI_ENDPOINT") and os.environ.get("AZURE_OPENAI_API_KEY") and os.environ.get("AZURE_OPENAI_DEPLOYMENT"):
        return "azure-openai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return None


def azure_settings():
    """Endpoint, deployment and API version. Accepts either the base endpoint
    (https://name.openai.azure.com/) or the full "Target URI" the portal shows
    (.../openai/deployments/<name>/chat/completions?api-version=...)."""
    from urllib.parse import urlparse, parse_qs
    raw = os.environ["AZURE_OPENAI_ENDPOINT"].strip()
    u = urlparse(raw)
    base = f"{u.scheme}://{u.netloc}/"
    m = re.search(r"/openai/deployments/([^/?]+)", u.path)
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "").strip() or (m.group(1) if m else "")
    if m and not os.environ.get("AZURE_OPENAI_DEPLOYMENT_OVERRIDE"):
        deployment = m.group(1)  # the deployment in the pasted URI is the one that exists
    # Reasoning models (gpt-5 family, o-series) need a newer API version than older GPT models.
    default = "2025-04-01-preview" if re.match(r"(gpt-5|o\d)", deployment or "") else "2024-10-21"
    version = os.environ.get("AZURE_OPENAI_API_VERSION") or (parse_qs(u.query).get("api-version") or [default])[0]
    return base, deployment, version


def _ask_azure(system, user, schema, max_tokens):
    from openai import AzureOpenAI
    base, deployment, version = azure_settings()
    c = AzureOpenAI(azure_endpoint=base, api_key=os.environ["AZURE_OPENAI_API_KEY"].strip(), api_version=version, timeout=120)
    r = c.chat.completions.create(
        model=deployment,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_schema", "json_schema": {"name": "result", "schema": schema, "strict": True}},
        max_completion_tokens=max_tokens,
    )
    choice = r.choices[0]
    if choice.finish_reason == "length":
        raise RuntimeError("The answer was cut off")
    if getattr(choice.message, "refusal", None):
        raise RuntimeError("The model declined this request")
    return json.loads(choice.message.content)


def ask(system, user, schema, max_tokens=16000):
    """One structured request. Returns the parsed JSON, or raises with the reason."""
    if provider() == "azure-openai":
        return _ask_azure(system, user, schema, min(max_tokens, 16000))
    with client().messages.stream(
        model=MODEL,
        max_tokens=max_tokens,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
        system=system,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        raise RuntimeError("The model declined this request")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("The answer was cut off; try again with a larger max_tokens")
    return json.loads(next(b.text for b in msg.content if b.type == "text"))


# ---------------------------------------------------------------- profile

PROFILE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["plan_title", "mission", "goals"],
    "properties": {
        "plan_title": {"type": "string", "description": "Official plan name and years."},
        "mission": {"type": "string"},
        "goals": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["title", "description", "page", "objectives", "keywords"],
                "properties": {
                    "title": {"type": "string", "description": "Short name for the goal, in the plan's words."},
                    "description": {"type": "string", "description": "The goal statement as written in the plan."},
                    "page": {"type": "string", "description": "Page number from the [page N] markers, or the section heading."},
                    "objectives": {"type": "array", "items": {"type": "string"}, "description": "Objectives under this goal, as written."},
                    "keywords": {"type": "array", "items": {"type": "string"},
                                 "description": "6-12 lowercase phrases a federal grant notice would use for this kind of work, e.g. 'stormwater', 'affordable housing', 'cybersecurity'. Specific phrases, not generic words."},
                },
            },
        },
    },
}

PROFILE_SYSTEM = """You turn a North Carolina organization's strategic plan into a matching profile for a federal grants tool.
- Copy goals and objectives as the plan states them. Do not invent goals.
- Cite the page for each goal using the [page N] markers when present.
- Internal goals (staff morale, office operations) should get only narrow keywords so they don't attract unrelated grants.
- Keywords translate each goal into the language federal grant notices use."""


def fetch_plan_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (NC-Grants-Matchmaker)"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data, ctype = r.read(), r.headers.get("Content-Type", "")
    if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf") or data[:4] == b"%PDF":
        text, _ = notices.extract_text(data, "pdf")
    else:
        text = notices.html_to_text(data.decode("utf-8", "ignore"))
    if not text or len(text) < 500:
        raise RuntimeError("Could not read enough text from the plan (the site may block automated downloads)")
    return text


def build_profile(org_id, name, category, url, short_name=None):
    text = fetch_plan_text(url)
    os.makedirs(PLANS_DIR, exist_ok=True)
    with open(os.path.join(PLANS_DIR, f"{org_id}.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    out = ask(PROFILE_SYSTEM, f"Organization: {name} ({category})\nPlan source: {url}\n\nPlan text:\n\n{text}", PROFILE_SCHEMA)
    goals = [{
        "id": f"{org_id.upper()}-G{n}", "number": n, "title": g["title"],
        "citation": f"{out['plan_title']}, p. {g['page']}" if str(g["page"]).strip().isdigit() else f"{out['plan_title']}, {g['page']}",
        "description": g["description"], "objectives": g["objectives"] or [g["description"]],
        "keywords": [k.lower() for k in g["keywords"]],
    } for n, g in enumerate(out["goals"], 1)]
    path = os.path.join(PROFILES_DIR, f"{org_id}.json")
    old = json.load(open(path)) if os.path.exists(path) else {}
    profile = {
        "agency_id": org_id, "category": category, "name": name,
        "short_name": short_name or old.get("short_name") or name,
        "source_citation": out["plan_title"], "source_url": url, "mission": out["mission"],
        "eligible_applicant_types": old.get("eligible_applicant_types") or CATEGORY_TYPES.get(category, []),
        "funder_affinities": old.get("funder_affinities", []),
        "goals": goals,
        "keywords": sorted({k for g in goals for k in g["keywords"][:3]}),
        "built_by": f"{MODEL} from the published plan; review before relying on it",
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)
    return path, len(goals)


# ---------------------------------------------------------------- review

REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["real_fit", "goal_number", "how_to_use", "concerns", "confidence"],
    "properties": {
        "real_fit": {"type": "boolean", "description": "True only if the organization could credibly apply and the work serves one of its goals."},
        "goal_number": {"type": "integer", "description": "Number of the best-matching goal, or 0 if none."},
        "how_to_use": {"type": "string", "description": "One or two plain sentences: what this organization could do with this money and which goal it advances. If not a real fit, say why in one sentence."},
        "concerns": {"type": "string", "description": "Eligibility limits, matching funds, or mismatches to check; '' if none."},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
}

REVIEW_SYSTEM = """You help grant staff at North Carolina public organizations decide which federal grants are worth reading.
Given an organization's strategic-plan goals and one grant, judge whether the grant would actually help this organization reach one of its goals, and whether this kind of organization can apply.
Be concrete and skeptical: a shared keyword is not a fit. Write for office staff, in plain language. Use only the information given."""


def _inputs(conn, grant_id, req):
    funded = [a.get("activity") for a in (req or {}).get("funded_activities", [])]
    return {"funded_activities": funded, "eligibility_summary": ((req or {}).get("eligibility") or {}).get("summary")}


def review_hash(profile, grant, extra):
    key = json.dumps([profile.get("goals"), grant.get("title"), grant.get("description"), grant.get("eligibility"), extra],
                     sort_keys=True, default=str)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def review_grant(profile, grant, extra):
    goals = "\n".join(f"{g['number']}. {g['title']}: {g['description']}" for g in profile["goals"])
    funded = extra["funded_activities"]
    user = (f"Organization: {profile['name']} ({profile['category']})\nGoals:\n{goals}\n\n"
            f"Grant: {grant['title']} ({grant.get('number')}), {grant.get('agency')}\n"
            f"Who can apply (Grants.gov): {', '.join(grant.get('eligibility') or []) or 'not listed'}\n"
            f"Eligibility notes: {grant.get('eligibility_text') or extra['eligibility_summary'] or 'none'}\n"
            f"Description: {grant.get('description')}\n"
            + (f"What the money can pay for (from the notice): {'; '.join(funded)}\n" if funded else ""))
    return ask(REVIEW_SYSTEM, user, REVIEW_SCHEMA, max_tokens=4000)


def review_path(org_id, grant_id):
    return os.path.join(REVIEWS_DIR, org_id, f"{grant_id}.json")


def run_reviews(top, only_org=None):
    import pipeline
    conn = notices.connect()
    profiles = pipeline.load_agency_profiles()
    done = skipped = failed = 0
    for org_id, profile in sorted(profiles.items()):
        if only_org and org_id != only_org:
            continue
        for grant in db.scored_for_office(conn, org_id)[:top]:
            extra = _inputs(conn, grant["id"], notices.requirements_for(conn, grant["id"]))
            h = review_hash(profile, grant, extra)
            path = review_path(org_id, grant["id"])
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    if json.load(f).get("input_hash") == h:
                        skipped += 1
                        continue
            try:
                result = review_grant(profile, grant, extra)
            except Exception as e:
                print(f"  {org_id} / {grant['id']}: {e}")
                failed += 1
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({**result, "input_hash": h, "reviewed_by": MODEL, "reviewed_at": db.now()}, f, indent=2, ensure_ascii=False)
            done += 1
            print(f"  {org_id} / {grant['title'][:60]}: {'fit' if result['real_fit'] else 'not a fit'}")
    print(f"Reviews: {done} new, {skipped} unchanged, {failed} failed")


def review_for(org_id, grant_id):
    """Saved review for the grant page, or None."""
    path = review_path(org_id, grant_id)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- application drafts

DRAFT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["sections", "fill_in"],
    "properties": {
        "sections": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["heading", "guidance", "draft"],
            "properties": {
                "heading": {"type": "string", "description": "Section name as the notice calls it, or a standard name."},
                "guidance": {"type": "string", "description": "Page limit, what reviewers score, and the notice page, if known; else ''."},
                "draft": {"type": "string", "description": "First-draft text. Unknown facts as [bracketed blanks]."},
            }}},
        "fill_in": {"type": "array", "items": {"type": "string"}, "description": "Every [blank] the team must fill in."},
    },
}

DRAFT_SYSTEM = """You write first drafts of grant application sections for a North Carolina public organization.
Rules:
- Draft only the written sections (abstract or summary, statement of need, goals and objectives, project description or approach, evaluation plan, sustainability, budget justification outline). Never draft standard forms, registrations, audits, resumes, letters, or documents the organization must already have.
- When the notice's required sections and page limits are given, use its headings in its order, and put the page limit and scoring weight in "guidance".
- Use only facts provided. Never invent numbers, names, dates, partners, past results or local statistics: write a [bracketed blank] such as [number of residents served] instead, and list every blank in fill_in.
- Tie the need and objectives to the organization's strategic-plan goal, citing it by name.
- Plain, confident language. Short paragraphs. No marketing tone."""

DRAFTS_SQL = """CREATE TABLE IF NOT EXISTS app_drafts (
    office_id TEXT NOT NULL, item_id TEXT NOT NULL, input_hash TEXT NOT NULL,
    data TEXT NOT NULL, model TEXT, created_at TEXT NOT NULL,
    PRIMARY KEY (office_id, item_id))"""


def draft_inputs(conn, office_id, item_id):
    """Everything the draft is based on (all from stored, sourced data)."""
    import pipeline
    profile = pipeline.load_agency_profiles().get(office_id)
    if not profile:
        raise ValueError("Unknown organization")
    grant = next((g for g in db.scored_for_office(conn, office_id) if g["id"] == item_id), None)
    if not grant:
        raise ValueError("Unknown grant")
    req = notices.requirements_for(conn, item_id) or {}
    goal = next((g for g in profile["goals"] if g.get("title") == grant.get("matched_goal_title")), profile["goals"][0])
    return {
        "organization": profile["name"], "category": profile.get("category"),
        "goal": {"title": goal["title"], "citation": goal.get("citation"), "objectives": goal.get("objectives", [])[:4]},
        "grant": {"title": grant["title"], "number": grant.get("number"), "agency": grant.get("agency"),
                  "description": (grant.get("description") or "")[:3000], "award_ceiling": grant.get("award_ceiling"),
                  "deadline": grant.get("close_date"), "fit_evidence": [e["text"] for e in grant.get("fit_evidence", [])][:3]},
        "notice": {
            "sections": [{"name": d["name"], "details": d.get("details"), "source": d.get("source")}
                         for d in req.get("required_documents", []) if d.get("verified") is not False
                         and d.get("category") in ("Project narrative", "Budget", "Other")],
            "review_criteria": [{"criterion": c["criterion"], "weight": c.get("weight"), "source": c.get("source")}
                                for c in req.get("review_criteria", []) if c.get("verified") is not False],
            "funded_activities": [a.get("activity") for a in req.get("funded_activities", [])],
            "cost_share": req.get("cost_share"),
            "eligibility": req.get("eligibility"),
            "key_dates": [{"what": x.get("what"), "when": x.get("when"), "source": x.get("source")}
                          for x in req.get("deadlines", []) if x.get("verified") is not False],
        },
    }


_RECENT = []  # timestamps of new drafts written by this server process


def get_or_create_draft(conn, office_id, item_id):
    """Cached draft for this organization and grant; written once, redone only if its inputs change."""
    conn.execute(DRAFTS_SQL)
    inputs = draft_inputs(conn, office_id, item_id)
    h = hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()[:16]
    row = conn.execute("SELECT * FROM app_drafts WHERE office_id=? AND item_id=?", (office_id, item_id)).fetchone()
    if row and row["input_hash"] == h:
        out = json.loads(row["data"]); out.update(cached=True, created_at=row["created_at"], model=row["model"])
        return out
    if not provider():
        raise RuntimeError("AI drafting is not configured")
    # Cost guard for a public site: limit how many NEW drafts are written per hour (saved ones are free).
    import time
    now = time.time()
    _RECENT[:] = [t for t in _RECENT if now - t < 3600]
    if len(_RECENT) >= int(os.environ.get("AI_DRAFTS_PER_HOUR", "30")):
        raise ValueError("The hourly limit for new drafts has been reached. Please try again later.")
    _RECENT.append(now)
    result = ask(DRAFT_SYSTEM, "Write the draft from these facts (JSON):\n" + json.dumps(inputs, default=str, indent=1),
                 DRAFT_SCHEMA, max_tokens=16000)  # room for a reasoning model's hidden thinking plus the draft
    model = os.environ.get("AZURE_OPENAI_DEPLOYMENT") if provider() == "azure-openai" else MODEL
    conn.execute("INSERT OR REPLACE INTO app_drafts VALUES (?, ?, ?, ?, ?, ?)",
                 (office_id, item_id, h, json.dumps(result), model, db.now()))
    conn.commit()
    result.update(cached=False, created_at=db.now(), model=model)
    return result


# ---------------------------------------------------------------- one required document at a time

DOC_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "guidance", "draft", "fill_in"],
    "properties": {
        "title": {"type": "string"},
        "guidance": {"type": "string", "description": "Page limit, format and what reviewers look for, from the notice; else ''."},
        "draft": {"type": "string", "description": "The document's first draft, with headings as the notice requires. Unknown facts as [bracketed blanks]."},
        "fill_in": {"type": "array", "items": {"type": "string"}},
    },
}

DOC_SYSTEM = DRAFT_SYSTEM.replace(
    "Draft only the written sections (abstract or summary, statement of need, goals and objectives, project description or approach, evaluation plan, sustainability, budget justification outline). Never draft standard forms, registrations, audits, resumes, letters, or documents the organization must already have.",
    "Draft exactly ONE required document: the one named in the request. Follow what the notice says about it (contents, headings, page limit, format). "
    "For a budget justification, give the structure and reasoning with every amount as a [blank]. "
    "For a letter of support or commitment, write a template the partner can adapt, with names and commitments as [blanks].")

NOT_DRAFTABLE = ("Standard forms", "Registrations")
NOT_DRAFTABLE_WORDS = ("clearinghouse", "sf-424", "sf424", "sf-lll", "sam.gov", "uei", "grants.gov registration", "login.gov", "era commons",
                       "audit", "indirect cost rate agreement", "resume", "biosketch", "curriculum vitae", "certification", "assurances")


def draftable(doc):
    name = ((doc.get("code") or "") + " " + (doc.get("name") or "")).lower()
    return doc.get("category") not in NOT_DRAFTABLE and not any(w in name for w in NOT_DRAFTABLE_WORDS)


def get_or_create_doc_draft(conn, office_id, item_id, doc_name):
    """Draft one required document from the notice's checklist; saved and reused."""
    conn.execute(DRAFTS_SQL)
    inputs = draft_inputs(conn, office_id, item_id)
    req = notices.requirements_for(conn, item_id) or {}
    doc = next((d for d in req.get("required_documents", []) if d.get("name") == doc_name), None)
    general = False
    if not doc:
        # No checked notice for this grant: fall back to the general federal checklist shown on the page
        row = conn.execute("SELECT data FROM opportunities WHERE id=?", (item_id,)).fetchone()
        rd = (json.loads(row["data"]).get("required_documents") or {}) if row else {}
        doc = next((d for group in rd.values() if isinstance(group, list) for d in group if d.get("name") == doc_name), None)
        general = doc is not None
    if not doc:
        raise ValueError("That document is not on this grant's requirements list")
    if not draftable(doc):
        raise ValueError("This is a standard form or registration; complete it on Grants.gov or with your records")
    inputs["document_to_draft"] = {"name": doc["name"], "category": doc.get("category"),
                                   "details": doc.get("details") or doc.get("notes"), "source": doc.get("source"),
                                   "required": doc.get("required")}
    if general:
        inputs["document_to_draft"]["note"] = ("No funding notice is available for this grant. Follow general federal practice for this "
                                               "document, say in guidance that the notice's own instructions take priority, and leave "
                                               "grant-specific requirements as [blanks].")
    key = item_id + "#" + hashlib.sha1(doc_name.encode()).hexdigest()[:10]
    h = hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()[:16]
    row = conn.execute("SELECT * FROM app_drafts WHERE office_id=? AND item_id=?", (office_id, key)).fetchone()
    if row and row["input_hash"] == h:
        out = json.loads(row["data"]); out.update(cached=True, created_at=row["created_at"], model=row["model"])
        return out
    if not provider():
        raise RuntimeError("AI drafting is not configured")
    import time
    now = time.time()
    _RECENT[:] = [t for t in _RECENT if now - t < 3600]
    if len(_RECENT) >= int(os.environ.get("AI_DRAFTS_PER_HOUR", "30")):
        raise ValueError("The hourly limit for new drafts has been reached. Please try again later.")
    _RECENT.append(now)
    result = ask(DOC_SYSTEM, "Draft this one document from these facts (JSON):\n" + json.dumps(inputs, default=str, indent=1),
                 DOC_SCHEMA, max_tokens=16000)
    model = os.environ.get("AZURE_OPENAI_DEPLOYMENT") if provider() == "azure-openai" else MODEL
    conn.execute("INSERT OR REPLACE INTO app_drafts VALUES (?, ?, ?, ?, ?, ?)", (office_id, key, h, json.dumps(result), model, db.now()))
    conn.commit()
    result.update(cached=False, created_at=db.now(), model=model)
    return result



# ---------------------------------------------------------------- chat

CHAT_SYSTEM = """You answer questions from North Carolina grant staff using ONLY the JSON data provided:
the organization's goals, its ranked list of current grants, its saved decisions, and (if present) the grant it has open.
Rules:
- Never invent grants, amounts, dates, eligibility or requirements. If the data does not contain the answer, say so
  plainly and point to the official notice link or the grant's page on this site.
- Name grants by their exact title. Give deadlines as written in the data. Say "expected" for forecasted dates.
- Keep answers short: a few sentences or a short list. Plain language, no emojis.
- Staff make the decisions. You can compare and explain, not decide for them.
- If no organization is selected, answer questions about how the site works and ask them to choose an organization.
How the site works: grants are ranked by Score = Fit x (0.6 + 0.4 x Practical / 100). Fit measures match with the
strategic plan's goals; Practical covers eligibility, award size, time before deadline, staff effort and familiar funder.
Staff mark grants Pursue, Watch, Pass or Submitted; Pursue and Watch can send deadline reminder emails."""

CHAT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["answer", "grant_ids"],
    "properties": {
        "answer": {"type": "string", "description": "Plain-text answer."},
        "grant_ids": {"type": "array", "items": {"type": "string"},
                      "description": "IDs of grants from the data that the answer mentions, best first."},
    },
}
_CHAT_RECENT = []


def _grant_brief(g):
    return {k: g.get(k) for k in ("id", "title", "agency", "status", "open_date", "close_date", "expected_open_date",
                                  "expected_due_date", "award_ceiling", "award_floor", "typical_award",
                                  "cost_share_required", "total_score", "matched_goal_title", "eligibility_note",
                                  "est_completion_hours", "url") if g.get(k) not in (None, "", [])}


def chat(conn, office_id, question, history=None, grant_id=None):
    """Answer one question about an organization's grants from stored data only."""
    import time
    import pipeline
    if not provider():
        raise RuntimeError("AI chat is not configured")
    question = (question or "").strip()[:1000]
    if not question:
        raise ValueError("Type a question first.")
    now = time.time()
    _CHAT_RECENT[:] = [t for t in _CHAT_RECENT if now - t < 3600]
    if len(_CHAT_RECENT) >= int(os.environ.get("AI_CHAT_PER_HOUR", "60")):
        raise ValueError("The hourly limit for questions has been reached. Please try again later.")
    _CHAT_RECENT.append(now)

    profiles = pipeline.load_agency_profiles()
    data = {"today": db.nc_today().isoformat()}
    profile = profiles.get(office_id)
    if profile:
        today = data["today"]
        # Leave out grants the organization's type cannot apply for (e.g. tribal-only grants for a county).
        grants = [g for g in db.scored_for_office(conn, office_id)
                  if (not g.get("close_date") or g["close_date"] >= today)
                  and (g.get("score_breakdown") or {}).get("eligibility", 100) > 10][:40]
        data["organization"] = {"name": profile.get("name"), "plan": profile.get("source_citation"),
                                "goals": [{"title": g.get("title"), "description": g.get("description")}
                                          for g in profile.get("goals", [])]}
        data["ranked_grants"] = [_grant_brief(g) for g in grants]
        data["decisions"] = [{"grant_id": d["opportunity_id"], "title": d.get("title"), "status": d.get("status"),
                              "notes": d.get("notes")} for d in db.get_decisions(conn, office_id)["decisions"]]
        if grant_id:
            g = next((x for x in db.scored_for_office(conn, office_id, include_closed=True) if str(x["id"]) == str(grant_id)), None)
            if g:
                open_grant = _grant_brief(g)
                open_grant.update(description=(g.get("description") or "")[:4000],
                                  eligibility_text=(g.get("eligibility_text") or "")[:2000],
                                  why_it_fits=g.get("reason"), score_reasons=g.get("score_reasons"))
                try:
                    import notices
                    req = notices.requirements_for(conn, str(grant_id))
                    if req:
                        open_grant["requirements"] = req
                except Exception:
                    pass
                data["open_grant"] = open_grant
    turns = [{"role": t.get("role"), "text": str(t.get("text", ""))[:1500]}
             for t in (history or [])[-6:] if t.get("role") in ("user", "assistant")]
    user = ("Data (JSON):\n" + json.dumps(data, default=str)[:60000]
            + "\n\nEarlier in this conversation:\n" + json.dumps(turns)
            + "\n\nQuestion: " + question)
    out = ask(CHAT_SYSTEM, user, CHAT_SCHEMA, max_tokens=6000)
    known = {str(g["id"]): g for g in data.get("ranked_grants", [])}
    out["grants"] = [{"id": i, "title": known[i]["title"]} for i in map(str, out.get("grant_ids", [])) if i in known][:5]
    return out

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("profile")
    p.add_argument("org_id")
    p.add_argument("--name", required=True)
    p.add_argument("--category", required=True, choices=sorted(CATEGORY_TYPES))
    p.add_argument("--url", required=True)
    p.add_argument("--short-name")
    r = sub.add_parser("review")
    r.add_argument("--top", type=int, default=20)
    r.add_argument("--org")
    args = ap.parse_args()
    if args.cmd == "profile":
        path, n = build_profile(args.org_id, args.name, args.category, args.url, args.short_name)
        print(f"Wrote {path} with {n} goals. Check it, then run pipeline.py to score grants for it.")
    else:
        run_reviews(args.top, args.org)


if __name__ == "__main__":
    main()
