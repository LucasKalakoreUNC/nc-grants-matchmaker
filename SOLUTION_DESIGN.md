# Solution Design Document: Federal Funds Matchmaker

**Project:** North Carolina Federal Funds Matchmaker  
**Event:** Solveathon Final Challenge · Saturday Starter Kit  
**Target User:** State Grants Coordinators (NC DEQ, NCDOT, NCDHHS, NC Commerce, NC DPS)  

---

## 1. Problem Statement & Executive Summary
State grants staff across North Carolina state agencies manually review hundreds of complex federal funding announcements each week on Grants.gov and SAM.gov. Because this manual process is time-consuming and error-prone, agencies frequently miss high-value discretionary opportunities or struggle to prioritize which notices fit their strategic plans.

The **Federal Funds Matchmaker** is an automated, explainable decision-support portal that:
1. Ingests federal notices daily from the Grants.gov Search API.
2. Normalizes opportunities into a standard schema.
3. Scores each opportunity against the agency’s real strategic plan goals using a transparent, multi-signal formula (Semantic fit, Keyword overlap, Applicant eligibility, Application timing, and Funder affinity).
4. Explains each ranking with a grounded "Why It Matches" rationale citing the agency’s specific strategic plan goal and page.
5. Enables staff to log "Pursue", "Maybe", or "Pass" decisions in seconds and export shortlists to CSV for weekly executive briefings.

---

## 2. Logical Architecture & Data Flow

```
                      +-----------------------------------+
                      |   EXTERNAL DATA SOURCES           |
                      |   • Grants.gov Search2 API        |
                      |   • NC OSBM Agency Strategic Plans|
                      +-----------------+-----------------+
                                        |
                                        v
  +-----------------------------------------------------------------------+
  |                           AZURE COMPUTE                               |
  |                                                                       |
  |  Container Apps Job: "daily-refresh"                                  |
  |  (Cron: 6:00 AM ET / 10:00 UTC)                                       |
  |  [ Ingest ] ──> [ Normalize & De-dup ] ──> [ Score & Summarize ]      |
  |                                                     |                 |
  |                                                     v                 |
  |  Container App: "web-ui"                                              |
  |  (1 vCPU / 2 GiB Consumption, Port 8000)                              |
  |  [ Ranked List ]  [ Detail Panel ]  [ Decisions ]  [ CSV Export ]     |
  |  [ Goal Heatmap]  [ Deadline Radar ] [ Evaluation ] [ Weight Sliders ]|
  +-------------------------------------+---------------------------------+
                                        | Reads / Writes JSON
                                        v
  +-----------------------------------------------------------------------+
  |                 AZURE STORAGE (ffm-data Container)                    |
  |                                                                       |
  |  • profiles/{agency}.json      • scored/{agency}/latest.json          |
  |  • raw/{date}/grants.json      • decisions/{agency}.json              |
  |  • opportunities/latest.json   • eval/labels.json                     |
  |                                • runs/{timestamp}.json                |
  +-----------------------------------------------------------------------+
```

---

## 3. Data Contract Specification (§5.0)

All data exchange between ingestion, normalization, scoring, and UI roles uses standardized JSON contracts:

| Path | Written By | Read By | Schema / Contents |
| :--- | :--- | :--- | :--- |
| `profiles/{agency}.json` | Agency Profile Builder (seed) | Scoring, Web App | Agency mission, 4–8 goals with citations, objectives, 25–40 keywords, eligible applicant types, funder affinities. |
| `raw/{yyyy-mm-dd}/grants.json` | Daily Ingestion Job | Normalization | Untouched raw Grants.gov API responses. |
| `opportunities/latest.json` | Normalization Step | Scoring Engine | Minimum clean schema: `id`, `source`, `number`, `title`, `agency`, `status`, `open_date`, `close_date`, `days_to_close`, `eligibility`, `category`, `award_ceiling`, `description`, `url`, `ingested_at`. |
| `scored/{agency}/latest.json` | Scoring & Summarization | Web UI | Ranked opportunities with `total_score`, `confidence` (High/Medium/Low), `score_breakdown`, `matched_goal_id`, `matched_goal_title`, `matched_goal_citation`, `reason`, and `ai_summary`. |
| `decisions/{agency}.json` | Web App (Staff actions) | Web App, Evaluation | Recorded decisions: `opportunity_id`, `status` (Pursue/Maybe/Pass), `decided_by`, `decided_at`, `notes`. |
| `eval/labels.json` | Evaluation Benchmark | Web App, Judges | 25 hand-labeled ground-truth items with Precision@10, coverage wins, and agreement metrics. |
| `runs/{timestamp}.json` | Daily Ingestion Job | Web App | Job telemetry: timestamp, duration, raw records pulled, normalized unique count, breakdown by agency. |

---

## 4. Transparent Scoring Algorithm (§5.4)

Each opportunity is scored on a transparent 0–100 scale using five weighted signals:

$$\text{Score} = (S_{\text{semantic}} \times 0.45) + (S_{\text{keyword}} \times 0.25) + (S_{\text{eligibility}} \times 0.15) + (S_{\text{timing}} \times 0.10) + (S_{\text{affinity}} \times 0.05)$$

1. **Semantic / Goal Alignment (45%):** Measures text similarity and concept overlap between the federal opportunity and each individual goal's objectives and description. Identifies the highest-scoring goal.
2. **Keyword / Program Fit (25%):** Density of the agency profile's specialized keywords appearing in the title and description.
3. **Applicant Eligibility Fit (15%):** Verifies that State Governments or public agencies are eligible entities under federal grant guidelines.
4. **Timing & Application Window (10%):** Grants with ≥25 days receive 100 points; 14–24 days receive 80 points; ≤14 days receive 30–55 points and trigger the **Urgent Deadline Radar** warning. Forecasted grants receive 90 points and are placed on the **Forecasted Watchlist**.
5. **Funding Agency Affinity (5%):** Awards bonus points when the funding agency historically aligns with the state agency (e.g., EPA/NOAA for DEQ; FHWA/FTA for NCDOT; CDC/SAMHSA for NCDHHS; EDA/NTIA for Commerce; FEMA/CISA for DPS).

### Interactive Coordinator Tuning (§8.1)
Coordinators can adjust these five weights dynamically via the UI slider drawer to prioritize short-term timing or specific grant types, triggering instant client-side or server-side recalculation.

---

## 5. Threat Modeling, Failure Modes & Mitigations

| Threat / Risk | Failure Scenario | Engineering Mitigation Implemented |
| :--- | :--- | :--- |
| **False Positives (Wasted Reviewer Time)** | Generic keyword overlap (e.g., "water" or "sediment") matches irrelevant academic or specialized vessel grants. | Strict applicant-type eligibility filtering; score breakdown transparency showing individual signal weights; low confidence band filtered out by default. |
| **False Negatives (Missed Millions)** | An important opportunity uses non-standard vocabulary (e.g., "solute transport" instead of "drinking water"). | Broad ingestion pull (no keyword filter); multi-term goal synonym expansion; coverage monitor proving recall over keyword baseline. |
| **GenAI Hallucination** | LLM invents non-existent grant deadlines or assumes mandatory state matching fund requirements. | Strictly grounded prompt templates requiring the model to cite only verified JSON fields (`costSharing`, `closeDate`, `awardCeiling`) and output "Not stated" when data is absent. |
| **External API Rate Limits or Outages** | Grants.gov API experiences downtime during a Monday morning state review session. | Snapshot fallback caching: `opportunities/latest.json` is always available locally, ensuring zero downtime for reviewers. |
| **Credential Security** | Cloud connection strings or API keys accidentally committed to source repositories. | Strict separation of config: zero credentials in source files; secrets passed exclusively via Azure Container Apps secret references (`STORAGE_CONN`, `AOAI_KEY`). |

---

## 6. Pilot Implementation & Scaling Roadmap (§7.2)

- **60-Day Pilot (Months 1–2):** Deploy to NC DEQ and NCDOT. Pair one grants coordinator per agency for weekly triage. Success target: ≥5 hours saved per week per coordinator, ≥90% Precision@10, and ≥2 high-value opportunities pursued that manual keyword scans missed.
- **Scale Across State Government (Months 3–6):** Onboard remaining state agencies (DHHS, Commerce, DPS, DPI, Agriculture) using the standardized `profiles/{agency}.json` template.
- **Enterprise Hardening (Months 6+):** Integrate Microsoft Entra ID (Azure AD) for role-based access control, transition JSON state to Azure AI Search & Cosmos DB, and automate weekly summary digests via Microsoft Teams and Outlook webhooks.
