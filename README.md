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
| NC state grant programs | NC.gov Grant Opportunities directory (official) | Weekly |
| NC foundations (Golden LEAF, Dogwood, Kate B. Reynolds, Z. Smith Reynolds, Duke Endowment) | Each funder's own site and description | Weekly |
| State budget grants directed to an organization | OSBM Legislative Grants Database | Weekly |
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
| `state_programs.py` | NC state grant programs from the NC.gov directory |
| `ai.py` | Build a profile from a plan; write "how you could use it" reviews (needs an Anthropic API key) |
| `data/profiles/` | One JSON file per organization (goals, keywords, eligibility) |
| `data/requirements/` | One JSON file per grant: what the application requires |
| `NOTICE_READING_GUIDE.md` | Instructions for people or agents reading notices |

## Organization profiles

16 organizations use the goals and objectives from their published strategic
plans (state agencies from the
[OSBM site](https://www.osbm.nc.gov/operational-excellence/strategic-planning/agency-strategic-plans),
plus Wake County, Raleigh, Asheville, NC State and NC A&T), cited by page and
linked from the app. The rest are labeled drafts. General profiles ("Any NC nonprofit", "Any NC
county government", "Any NC city or town", "Any NC public school district",
"Any NC tribal government") let any organization without its own profile see
what its type of organization can apply for. To add a town or county from
its published plan:

```bash
.venv/bin/python ai.py profile town_cary --name "Town of Cary" --category municipal --url <plan PDF or page>
```

or edit `data/profiles/<id>.json` (copy an existing file) and run `pipeline.py`.

## Deploying (Azure)

The repository has everything needed to build and run the service:
`Dockerfile`, `start.sh`, `requirements.txt`, and a compressed database
snapshot (`data/seed/matchmaker.db.gz`, ~21 MB) that is unpacked on first
start so the site has data immediately. The container then refreshes the data
every `REFRESH_HOURS` hours (default 24).

**Azure App Service (how this repository deploys today)**

Every push to `main` runs `.github/workflows/main_ncgrantfinder.yml`, which
deploys the code to the `ncgrantfinder` Web App (Python 3.11) with the start
command `sh start.sh`. On first start, `start.sh` unpacks the database
snapshot into `/home/data/matchmaker.db` (App Service keeps `/home` across
restarts and deploys, so staff decisions survive), then starts `app.py`,
which refreshes the data every 24 hours.

If the app does not start, check in the Azure portal that **Configuration >
General settings > Startup Command** is `sh start.sh`, and that the stack is
Python 3.11 or newer. Optional app settings: `REFRESH_HOURS` (default 24) and
`ANTHROPIC_API_KEY` (only for `ai.py`).

**Azure Container Apps (alternative, uses the Dockerfile)**

```bash
# from the repository root
az acr build --registry <your-acr> --image nc-grants:latest .
az containerapp create -n nc-grants -g <resource-group> --environment <env> \
  --image <your-acr>.azurecr.io/nc-grants:latest \
  --ingress external --target-port 8000 \
  --min-replicas 1 --max-replicas 1 --cpu 1.0 --memory 2.0Gi \
  --env-vars REFRESH_HOURS=24
```

Settings that matter:

| Setting | Value | Why |
|---|---|---|
| Target port | 8000 | The app listens on `$PORT` (default 8000) |
| Replicas | exactly 1 | The database is one SQLite file; two copies would disagree |
| Memory | 2 GiB | The refresh reads PDFs and scores ~500 grants x 34 organizations |
| `REFRESH_HOURS` | 24 | How often to pull new data (0 turns it off) |
| `ANTHROPIC_API_KEY` | optional secret | Only for `ai.py` and `notices.py extract` |

Storage: without a mounted volume, staff decisions are lost when the container
restarts (the snapshot is unpacked again). To keep them, mount an Azure Files
share at `/app/data` and copy the repository's `data/` folder into it once.

**Azure App Service (Web App for Containers)** also works: point it at the
same image and set `WEBSITES_PORT=8000`.

**Locally with Docker**

```bash
docker build -t nc-grants .
docker run -p 8000:8000 nc-grants
```

## Known limits

- Fit is keyword-based. It quotes its evidence, but can miss grants that use
  different wording.
- Most funding notices have not been read yet; those grants show a general
  checklist until they are (see `NOTICE_READING_GUIDE.md`).
- NIH and NSF grants often post their full notice on the agency's own site,
  not Grants.gov.
- No login yet: anyone with the link can record decisions.
