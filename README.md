# NC Grants Matchmaker

A tool for North Carolina state and local grant offices. It checks federal
funding listings every day, compares each grant with an organization's
strategic plan, and shows staff which grants are worth their time, what the
application requires, and when it is due.

Built for the UNC Solveathon Final Challenge.

## What staff see

- **Opportunities**: grants ranked for their organization, with the award
  size, the typical past award, estimated staff hours, and deadline.
- **Grant page**: why it fits (quoting the grant and the matched strategic-plan
  goal), what the application requires (read from the funding notice, with
  page numbers), key dates, who can apply, past North Carolina recipients, and
  links to every source.
- **Decisions**: Pursue, Watch or Pass, with notes, shared across the office.
- **How this works**: plain-language guide to the ranking and the data.

## Run it locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python pipeline.py      # build or refresh the database (first run ~10 min)
python3 app.py                    # http://localhost:8000
```

`pipeline.py` only downloads what is new or changed, so later runs take
seconds to a few minutes. Set `FORCE_REFRESH=1` to re-pull every search.

## Where the data comes from

| Data | Source | Refreshed |
|---|---|---|
| Grant listings and forecasts | Grants.gov `search2` API | Daily (searches older than 20 hours) |
| Grant details: description, amounts, eligibility, cost share, contact | Grants.gov `fetchOpportunity` API | Once per grant, again if it changes |
| Funding notice files (PDF and others) | Grants.gov attachments | Once per file |
| Past North Carolina awards; typical award size per program | USAspending.gov API | Weekly per program |
| Federal programs (purpose, restrictions, award ranges, spending) | SAM.gov Assistance Listings | Weekly list, monthly details |
| Organization goals | Agency strategic plans posted by NC OSBM (`data/plans/`) | When plans are updated |
| Application requirements per grant | Read from the funding notice (`data/requirements/`) | When a notice is read |

Everything is public. The database (`data/matchmaker.db`, SQLite) is not
committed because it is large; it is rebuilt from these sources.

## How grants are ranked

```
Score = Fit x (0.6 + 0.4 x Practical / 100)
```

**Fit** (0-100) measures how closely the grant's title and description match
the key phrases of the organization's strategic-plan goals. A grant with no
real connection cannot score well. **Practical** (0-100) combines
eligibility (30%), award size (25%), time before the deadline compared with
estimated effort (25%), staff effort (15%), and familiar funder (5%).
Strong match is 70 or higher; good match is 55 to 69. Details are in the
app's "How this works" page and in `pipeline.py` (`calculate_score`).

## Files

| File | Purpose |
|---|---|
| `app.py` | Web server and JSON API |
| `public/index.html` | The staff-facing page |
| `pipeline.py` | Pull, normalize, enrich, and score grants |
| `db.py` | SQLite database and download cache |
| `notices.py` | Download funding notices; record and check requirements |
| `programs.py` | Federal programs from SAM.gov Assistance Listings |
| `ai.py` | Build a profile from a plan; write "how you could use it" reviews (needs an Anthropic API key) |
| `data/profiles/` | One JSON file per organization (goals, keywords, eligibility) |
| `data/requirements/` | One JSON file per grant: what the application requires |
| `NOTICE_READING_GUIDE.md` | Instructions for people or agents reading notices |

## Organization profiles

16 organizations use the goals and objectives from their published strategic
plans (state agencies from the
[OSBM site](https://www.osbm.nc.gov/operational-excellence/strategic-planning/agency-strategic-plans),
plus Wake County, Raleigh, Asheville, NC State and NC A&T), cited by page and
linked from the app. The rest are labeled drafts. To add a town or county from
its published plan:

```bash
.venv/bin/python ai.py profile town_cary --name "Town of Cary" --category municipal --url <plan PDF or page>
```

or edit `data/profiles/<id>.json` (copy an existing file) and run `pipeline.py`.

## Deploying

The Docker image runs either the site (`MODE=app`) or a one-time refresh
(`MODE=job`). Because the database is a single SQLite file, the simplest
deployment is one container that serves the site and refreshes itself:

```bash
docker build -t nc-grants .
docker run -p 8000:8000 -e REFRESH_HOURS=24 -v grants-data:/app/data nc-grants
```

`REFRESH_HOURS` runs the pipeline in the background on that interval. Keep
`/app/data` on persistent storage so decisions and downloads survive restarts.

## Known limits

- Fit is keyword-based. It quotes its evidence, but can miss grants that use
  different wording.
- Most funding notices have not been read yet; those grants show a general
  checklist until they are (see `NOTICE_READING_GUIDE.md`).
- NIH and NSF grants often post their full notice on the agency's own site,
  not Grants.gov.
- No login yet: anyone with the link can record decisions.
