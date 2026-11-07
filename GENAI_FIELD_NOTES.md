# GenAI Field Notes: Federal Funds Matchmaker

**Event:** Solveathon Final Challenge · Saturday Starter Kit  
**Team Lane:** Matching, AI & Story  
**Focus:** Grounded AI Prompts, Verification Strategies, and Error Analysis  

---

## 1. Tools & Models Used
- **Azure OpenAI Service / LLM Foundation:** OpenAI GPT-4o-mini & `text-embedding-3-small` for semantic similarity scoring, grounded summarization, and strategic goal extraction.
- **Grants.gov REST API:** `https://api.grants.gov/v1/api/search2` and `fetchOpportunity` endpoints.
- **Python 3 / Node.js Runtime:** Pipeline automation, scoring engine, and staff-facing web server.

---

## 2. Key Prompts Engineered

### Prompt 1: Agency Strategic Plan Profile Extraction (§5.1)
*Used to convert public NC OSBM strategic plan documents into standardized JSON profiles.*

```
You are an expert state public policy analyst and grants manager.
Given the following excerpts from the North Carolina state agency's official Strategic Plan:
Agency: North Carolina Department of Environmental Quality (DEQ)
Source Document: NC DEQ Strategic Plan FY 2025-2029

Extract a structured JSON profile adhering to this exact schema:
{
  "agency_id": "deq",
  "name": "North Carolina Department of Environmental Quality",
  "short_name": "NC DEQ",
  "source_citation": "<Document title, version, and page range>",
  "mission": "<Direct verbatim mission statement>",
  "goals": [
    {
      "id": "DEQ-G<N>",
      "number": <N>,
      "title": "<Goal Title>",
      "citation": "<Section and page reference>",
      "description": "<Concise 2-sentence scope of goal>",
      "objectives": ["<Objective 1>", "<Objective 2>", ...],
      "keywords": ["<20-40 specific program and domain terms>"]
    }
  ]
}

STRICT RULE: Every goal MUST include an exact section and page citation. Do not extrapolate or summarize vaguely.
```

### Prompt 2: Opportunity Summarization & Grounded "Why It Matches" Rationale (§5.5)
*Used to synthesize high-scoring federal notices without hallucinating terms or requirements.*

```
You are an executive grants coordinator briefing the North Carolina state leadership.
Given:
1. Federal Funding Opportunity Title: {{opp.title}}
2. Sponsoring Federal Agency: {{opp.agency}}
3. Assistance Listing (CFDA): {{opp.cfda}}
4. Synopsis Excerpt: {{opp.description}}
5. Matched State Strategic Goal: {{matched_goal.title}} (Citation: {{matched_goal.citation}})
6. Matched State Objective: {{matched_goal.objective}}

Write a concise briefing with two distinct sections:
1. "ai_summary": A 2-sentence executive summary stating what the program funds, estimated ceiling, and closing timeframe.
2. "reason": A plain-English justification explaining precisely why this grant advances the matched state goal and citing the specific objective.

STRICT CONSTRAINTS:
- Do NOT guess match percentages or cost-sharing requirements.
- If cost sharing is not explicitly confirmed in the source synopsis, output "Cost share not stated; verify in NOFO".
- State facts directly; avoid marketing adjectives (e.g. "groundbreaking", "transformative").
```

### Prompt 3: False-Positive Boundary Diagnosis (§6)
*Used during evaluation to analyze edge cases where keyword density was high but operational relevance was low.*

```
Analyze why this grant was scored as a match by keyword filters:
Title: "Limited Competition: Superfund Hazardous Substance Research and Training Program (P42 Clinical Trial Optional)"
Target Agency: NC DEQ (Division of Waste Management / Superfund Branch)

Identify:
1. What superficial keywords caused the false match?
2. What operational reason makes this unsuited for state agency staff?
3. What rule or eligibility filter would cleanly separate this from genuine state brownfield grants?
```

---

## 3. Where AI Was Wrong & How We Caught It (§7.3 & §8.3)

### Incident 1: Hallucination of Mandatory State Cash Match Requirements
- **What the AI Output:** During early test runs of Prompt 2, the model generated summaries stating: *"This opportunity requires a mandatory 50% non-federal state matching contribution from the North Carolina General Assembly."*
- **Why It Happened:** The model generalized from certain municipal grant programs where 50/50 cost sharing is common, assuming all state infrastructure grants mandate heavy local matches.
- **How We Caught It:** A team reviewer spot-checked 5 AI-generated summaries against the actual Grants.gov synopsis fields (`costSharing` and `applicantEligibilityDesc`). For the specific clean water and air grants, federal law funded up to 100% of project costs with 0% state match required.
- **The Engineering Fix:** We constrained Prompt 2 to forbid match percentage inferences. We introduced a deterministic rule in `pipeline.py` that parses the official boolean `costSharing` field and explicitly tags: *"Review NOFO to verify statutory match requirements"* rather than allowing the LLM to speculate.

### Incident 2: Inappropriate Geographic Targeting (Great Lakes vs Atlantic Coastal)
- **What the AI Output:** The model scored a Fish & Wildlife Service grant titled *"Aquatic Invasive Species Grants to Great Lakes States and Tribes"* into the High Confidence tier (81 pts) for NC DEQ Goal 4 (Coastal & Estuarine Protection) due to terms like "invasive species", "fisheries", and "water quality".
- **How We Caught It:** A human coordinator noted that North Carolina is on the Atlantic Seaboard, not the Great Lakes basin.
- **The Engineering Fix:** We added geographic entity detection in `pipeline.py` that checks for specific regional identifiers ("Great Lakes", "Pacific Northwest", "Columbia River", "Upper Missouri") and downgrades opportunities when the program is restricted outside North Carolina's Southeast / Mid-Atlantic region.
