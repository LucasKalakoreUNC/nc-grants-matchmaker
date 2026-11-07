# Reading funding notices: instructions for an AI agent

Goal: for each grant, read its funding notice and record exactly what an
application must include, so grant staff don't have to read 50 pages to find
out. Results go in `data/requirements/<grant id>.json`, one file per grant.

**Only add or edit files in `data/requirements/`.** Don't change code, the
database, or other folders. That way this work never conflicts with work
happening elsewhere in the repo.

## Setup (once)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
python3 pipeline.py                      # builds data/matchmaker.db from public sources (~2 min first time)
.venv/bin/python notices.py download     # downloads notice files from Grants.gov (~5 min, ~350 MB)
```

## The loop

```bash
.venv/bin/python notices.py queue --limit 10     # next grants to read, best matches first
.venv/bin/python notices.py dump <grant id>      # print the notice text, with [page N] markers
```

Read the notice, write the result as JSON (format below) to a temporary file,
then save it:

```bash
.venv/bin/python notices.py import my_batch.json --reader "Antigravity (Gemini)"
```

`import` checks the format and writes `data/requirements/<id>.json`. Grants
that already have a file drop out of the queue. Commit the new files in
small batches (for example every 10 grants).

## Rules

1. Use only what the notice says. Don't add documents that are "usually"
   required unless this notice requires them.
2. Every item needs a `source`: file name and page, like
   `"Foa_Content_of_CDC-RFA-TP22-2201_122024.pdf p. 28"`. Use the `[page N]`
   markers from `dump`. For files without page markers, give the file name
   and section heading.
3. If the notice doesn't say something, write `"not stated"`. Never guess.
4. Plain language, one or two sentences per detail. Readers are office staff,
   not grant specialists.
5. Note anything that would get an application rejected (missing
   attachments, page limits, eligibility restrictions) in `important_notes`.
6. North Carolina matters: if eligibility depends on population, location or
   a named list, say which North Carolina organizations qualify.

## Format

Input to `import` is an object keyed by grant id. A complete example is
`data/requirements/337086.json` (drop its `opportunity_id`, `read_by` and
`read_at` lines; `import` adds those).

```json
{
  "<grant id>": {
    "required_documents": [
      {"name": "Project Narrative", "category": "Project narrative", "required": true,
       "details": "Max 20 pages, single spaced, 12-point font.", "source": "<file> p. 28"}
    ],
    "deadlines": [{"what": "Application due", "when": "February 11, 2027, 11:59 p.m. ET", "source": "<file> p. 3"}],
    "eligibility": {"summary": "...", "source": "<file> p. 24"},
    "cost_share": {"required": "yes | no | not stated", "details": "...", "source": "<file> p. 4"},
    "review_criteria": [{"criterion": "Approach", "weight": "33 points", "source": "<file> p. 36"}],
    "submission_limits": "not stated",
    "important_notes": ["..."]
  }
}
```

`category` must be one of: `Standard forms`, `Project narrative`, `Budget`,
`Attachments`, `Letters and partnerships`, `Registrations`, `Other`.
