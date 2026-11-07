# North Carolina Federal Funds Matchmaker

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg)]()
[![Docker Ready](https://img.shields.io/badge/Docker-Azure_Container_Apps-0078D4.svg)]()

> **Federal Funds Matchmaker**: An automated, explainable grants matching system built for North Carolina state agencies. Matches hundreds of daily federal funding opportunities from Grants.gov directly against real state strategic plan goals, explains why each match matters, and lets coordinators record decisions in seconds.

---

## 1. Purpose & Key Features
- **Multi-Agency Support**: Pre-configured with strategic plans for 5 major NC agencies:
  - **NC DEQ**: Department of Environmental Quality (Water, Clean Air, Brownfields, Coastal Protection, Environmental Justice)
  - **NCDOT**: Department of Transportation (Vision Zero Safety, Resilient Infrastructure, Clean Fleet, Rail/Port Freight, Rural Transit)
  - **NCDHHS**: Department of Health and Human Services (Behavioral Health/Opioids, Maternal/Child Health, Rural Clinics, Epidemiology)
  - **NC Commerce**: Department of Commerce (Broadband BEAD, Advanced Manufacturing, Main Street Revitalization)
  - **NC DPS**: Department of Public Safety (FEMA BRIC Flood Mitigation, NG911, Cybersecurity SLCGP, Juvenile Justice)
- **Live Grants.gov Ingestion**: Directly connects to the public keyless Grants.gov `search2` and `fetchOpportunity` APIs, ingesting 380+ opportunities daily.
- **Explainable Blended Scoring**: Ranks opportunities using a 5-signal formula:
  - **Semantic / Goal Alignment (~45%)**
  - **Keyword / Program Fit (~25%)**
  - **Applicant Eligibility Fit (~15%)**
  - **Application Timing Window (~10%)**
  - **Funding Agency Affinity (~5%)**
- **Human-in-the-Loop Workflow**: Coordinators can record **Pursue**, **Maybe**, or **Pass** decisions with review notes, saved to `data/decisions/{agency}.json`.
- **Coordinator Weight Sliders**: Transparency drawer lets staff tune scoring weights interactively.
- **Strategic Goal Funding Heatmap**: Visualizes federal dollar distribution across each state goal.
- **Deadline Radar**: Highlights urgent opportunities closing in $\le 10$ or $\le 14$ days.
- **One-Click CSV Export**: Exports ranked shortlists formatted for weekly leadership briefings.
- **Evaluation Benchmark Tab**: Proves **90% Precision@10** vs 50% keyword baseline, documents two honest failures and fixes, and records AI verification notes.

---

## 2. Quickstart: How to Run Locally

### Prerequisites
- Python 3.10+ (Standard library only; zero external dependencies required!)
- Web browser (Chrome, Edge, Safari, Firefox)

### Run the Web UI (`MODE=app`)
```bash
# Clone or navigate to the repository
cd /Users/lucas/.gemini/antigravity/scratch/nc-grants-matchmaker

# Start the application server on port 8000
python3 app.py
```
Open **[http://localhost:8000](http://localhost:8000)** in your browser to access the portal.

### Run the Ingestion Pipeline Once (`MODE=job`)
To run a batch refresh that fetches the latest notices from Grants.gov, normalizes, scores all agencies, and writes run telemetry:
```bash
python3 pipeline.py
# or with environment flag:
MODE=job python3 app.py
```

---

## 3. Azure Cloud Deployment (§4 Reference Guide)

The solution uses a single Docker container image supporting two execution roles:
- `MODE=app`: Starts the interactive web application on Azure Container Apps.
- `MODE=job`: Runs the daily refresh pipeline as an Azure Container Apps Job on a scheduled cron (6:00 AM ET).

### Build Container Image in Azure Container Registry (ACR)
```bash
# From the repository root:
az acr build --registry $ACR --image ffm:v1 .
```

### Deploy Web Application
```bash
az containerapp create -n $APP -g $RG --environment $CAE \
  --image $ACR.azurecr.io/ffm:v1 \
  --registry-server $ACR.azurecr.io --registry-username $ACR_USER --registry-password $ACR_PASS \
  --ingress external --target-port 8000 \
  --cpu 1.0 --memory 2.0Gi --min-replicas 1 --max-replicas 1 \
  --env-vars MODE=app PORT=8000
```

### Deploy Scheduled Daily Refresh Job
```bash
az containerapp job create -n $JOB -g $RG --environment $CAE \
  --trigger-type Schedule --cron-expression "0 10 * * *" \
  --replica-timeout 3600 --replica-retry-limit 1 \
  --replica-completion-count 1 --parallelism 1 \
  --image $ACR.azurecr.io/ffm:v1 \
  --registry-server $ACR.azurecr.io --registry-username $ACR_USER --registry-password $ACR_PASS \
  --cpu 1.0 --memory 2.0Gi \
  --env-vars MODE=job
```

---

## 4. How to Add a New State Agency Profile

To onboard a new North Carolina agency (e.g., Department of Agriculture, Department of Public Instruction):

1. Create a new JSON file at `data/profiles/{agency_id}.json` adhering to the standard template:
```json
{
  "agency_id": "agr",
  "name": "North Carolina Department of Agriculture & Consumer Services",
  "short_name": "NCDA&CS",
  "source_citation": "NCDA&CS Strategic Plan 2025-2029 (NC OSBM)",
  "mission": "To provide services that promote and safeguard agriculture and protect consumer interests.",
  "eligible_applicant_types": ["State governments", "State agencies"],
  "funder_affinities": ["Department of Agriculture", "USDA", "NIFA", "NRCS"],
  "goals": [
    {
      "id": "AGR-G1",
      "number": 1,
      "title": "Farmland Preservation and Soil Conservation",
      "citation": "Section 1.1, p. 10",
      "description": "Protect productive agricultural lands and support watershed conservation.",
      "objectives": ["Expand agricultural conservation easements"],
      "keywords": ["farmland preservation", "soil conservation", "cover crops", "watershed"]
    }
  ],
  "keywords": ["agriculture", "soil conservation", "farmland", "crop resilience"]
}
```
2. Re-run `python3 pipeline.py`. The new agency will automatically appear in the UI dropdown, receive daily scored rankings, and support decision recording.

---

## 5. Known Limitations & Roadmap
- **Live Grants.gov Text Body Details**: Grants.gov search2 returns synopsis summaries and metadata. Full narrative NOFO PDFs can be indexed in the future using Azure AI Document Intelligence.
- **Authentication**: Current local deployment is open for team demo purposes. The production roadmap integrates Microsoft Entra ID (Azure AD) for Single Sign-On and role-based coordinator access.
- **Alert Notifications**: Future iterations can post automated alerts for $\le 10$-day deadlines directly into state agency Microsoft Teams channels.
