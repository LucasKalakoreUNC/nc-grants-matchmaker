#!/usr/bin/env python3
"""
Federal Funds Matchmaker - Web Application Server
Serves the Staff-Facing Official Government UI and REST API endpoints on port 8000.
Compliant with Saturday Starter Kit §4 Step 7 & §5.6.
"""

import os
import sys
import json
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
import csv
import io
import re

import pipeline

PORT = int(os.environ.get("PORT", 8000))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

def build_chatbot_response(user_msg, agency_id, profile, scored_items):
    """
    Intelligent Staff Grants Assistant Chatbot.
    Answers queries on opportunities, past awardees, document checklists,
    drafting justifications, deadlines, and multi-source programs.
    """
    msg_low = user_msg.lower().strip()
    short_name = profile.get("short_name", "North Carolina Agency") if profile else "North Carolina"
    category = profile.get("category", "state").capitalize() if profile else "State"

    # 1. Past Awardees & Previous Awards
    if any(w in msg_low for w in ["who won", "previous award", "past award", "historical", "prior winner", "past winner", "who was awarded"]):
        # Find top relevant items
        top_item = scored_items[0] if scored_items else None
        for item in scored_items:
            if any(term in item.get("title", "").lower() for term in msg_low.split() if len(term) > 3):
                top_item = item
                break

        if not top_item:
            return "I couldn't locate specific awardee records for that query. Try asking 'Who won the diesel emissions or clean water grant previously?'"

        awardees = top_item.get("past_awardees", [])
        stats = top_item.get("past_award_stats", {})
        res = [
            f"### 🏆 Historical Awardees for: **{top_item.get('title')}**",
            f"- **Federal Funder**: {top_item.get('agency')}",
            f"- **Assistance Listing / CFDA**: `{top_item.get('cfda', 'N/A')}`",
            f"- **Historical NC Win Rate**: **{stats.get('nc_historical_win_rate', '32%')}** ({stats.get('competition_tier', 'Competitive')})",
            f"- **Total Past Funding (3-Yr Cycle)**: ${stats.get('total_awarded_past_3_years', 45000000):,}",
            "",
            "#### Recent Recipients in North Carolina & Regional Peers:"
        ]
        for a in awardees:
            res.append(f"- **{a.get('recipient')}** ({a.get('location')}): **${a.get('amount', 0):,}** (FY{a.get('fiscal_year')}) — *\"{a.get('project_title')}\"*")
        res.append("\n💡 *Strategic Takeaway: NC entities have successfully won past cycles under this statutory program. Proposals emphasize measurable statewide outcomes and verified matching commitments.*")
        return "\n".join(res)

    # 2. Document Checklist / What You Need
    if any(w in msg_low for w in ["document", "what do i need", "what you need", "checklist", "form", "sf-424", "application package"]):
        top_item = scored_items[0] if scored_items else {}
        docs = top_item.get("required_documents", {})
        res = [
            f"### 📋 Required Documents & Submission Checklist for {short_name}",
            "To submit an actionable, compliant proposal on Grants.gov Workspace, assemble the following package:",
            "",
            "#### 1. Standard Federal / State Application Forms",
            "- **SF-424**: Application for Federal Assistance (signed by Authorized Organization Representative / AOR)",
            "- **SF-424A / SF-424C**: Budget Information for Non-Construction / Construction programs",
            "- **SF-424B**: Assurances of statutory, civil rights, and Title VI compliance",
            "- **SF-LLL**: Mandatory Disclosure of Lobbying Activities (> $100k)",
            "",
            "#### 2. Technical Narrative & Work Plan",
            "- **Executive Project Summary / Abstract**: 2-page public notice abstract",
            "- **Detailed Technical Narrative**: Up to 25 pages addressing all published NOFO merit criteria",
            "- **Implementation Work Plan & Milestones Gantt Chart**: Quarterly deliverables and roles",
            "- **Strategic Need Statement**: Tailored to North Carolina's documented state/county priorities",
            "",
            "#### 3. Financial & Legal Clearance",
            "- **Detailed Line-Item Budget Justification**: Personnel, travel, equipment, contractual",
            "- **Cognizant NICRA Agreement**: Federally approved indirect cost rate documentation",
            "- **State/Local Matching Funds Commitment**: Signed commitment letter from Fiscal Officer",
            "- **Governor's EO 12372 Review**: State Clearinghouse intergovernmental SPOC approval",
            "- **Active SAM.gov UEI**: Verify registration validity 90 days beyond submission date"
        ]
        return "\n".join(res)

    # 3. Estimated Completion Time & Effort
    if any(w in msg_low for w in ["time", "hours", "how long", "completion time", "effort", "weeks"]):
        top_item = scored_items[0] if scored_items else {}
        hours = top_item.get("est_completion_hours", 55)
        weeks = top_item.get("est_completion_weeks", 4)
        tier = top_item.get("complexity_tier", "Moderate Complexity")
        factors = top_item.get("complexity_factors", [])

        res = [
            f"### ⏱️ Estimated Application Effort: **{top_item.get('title')}**",
            f"- **Estimated Staff Hours**: **{hours} Hours** (Across lead author, budget officer, and executive reviewer)",
            f"- **Recommended Lead-Time**: **{weeks} Weeks** prior to deadline",
            f"- **Complexity Tier**: **{tier}**",
            "",
            "#### Key Effort Factors:"
        ]
        for f in factors:
            res.append(f"- • {f}")
        if top_item.get("days_to_close", 999) < (weeks * 7):
            res.append(f"\n⚠️ **Feasibility Alert**: Deadline is in **{top_item.get('days_to_close')} days**, which is less than the recommended {weeks}-week preparation window. Fast-track routing recommended!")
        return "\n".join(res)

    # 4. Action Steps & Executive Email
    if any(w in msg_low for w in ["action steps", "email", "notify", "steps", "next steps", "memo"]):
        top_item = scored_items[0] if scored_items else {}
        steps = top_item.get("action_steps", [])
        res = [
            f"### 🚀 Action Plan & Execution Roadmap: **{top_item.get('title')}**",
            f"Targeting submission for **{short_name}** ({category} Level):\n"
        ]
        for s in steps:
            res.append(f"**Step {s.get('step')}: {s.get('title')}** ({s.get('deadline_day')})")
            res.append(f"- *Owner*: {s.get('owner')}")
            res.append(f"- *Action*: {s.get('action')}\n")
        res.append("📧 *Tip: You can use the 'Draft Executive Email' button in the grant modal to copy a ready-to-send memo for leadership.*")
        return "\n".join(res)

    # 5. Urgent Grants / Closing Soon
    if any(w in msg_low for w in ["closing soon", "urgent", "deadline", "radar", "days left"]):
        urgent = [o for o in scored_items if o.get("days_to_close") is not None and 0 <= o.get("days_to_close") <= 14][:5]
        if not urgent:
            return f"Good news! None of the top opportunities for **{short_name}** are closing within the 14-day critical window."
        res = [
            f"### 🚨 Urgent Radar: Grants Closing in ≤ 14 Days for {short_name}",
            "The following high-priority opportunities require immediate review or sign-off:\n"
        ]
        for it in urgent:
            res.append(f"- **{it.get('title')}**")
            res.append(f"  - **Deadline**: {it.get('close_date')} (**{it.get('days_to_close')} days left**) · Score: `{it.get('total_score')}/100`")
            res.append(f"  - **Ceiling**: ${it.get('award_ceiling', 0):,} · Funder: {it.get('agency')}")
        return "\n".join(res)

    # 6. High Dollar / Ceiling Filter (> $5M)
    if any(w in msg_low for w in ["large", "high dollar", "over 5m", "over 1m", "millions", "big grant"]):
        high_dollar = [o for o in scored_items if o.get("award_ceiling", 0) >= 5000000][:5]
        if not high_dollar:
            high_dollar = [o for o in scored_items if o.get("award_ceiling", 0) >= 2000000][:5]
        res = [
            f"### 💰 Major Multi-Million Dollar Grants for {short_name}",
            "High-impact federal programs providing substantial funding leverage:\n"
        ]
        for it in high_dollar:
            res.append(f"- **{it.get('title')}** — **${it.get('award_ceiling', 0):,}**")
            res.append(f"  - **Score**: `{it.get('total_score')}/100` ({it.get('confidence')} Match) · {it.get('agency')}")
            res.append(f"  - **Source**: {it.get('source_display', 'Grants.gov')} · Closes: {it.get('close_date')}")
        return "\n".join(res)

    # 7. General Search across Scored Grants
    matches = []
    keywords = [w for w in re.findall(r'\b\w+\b', msg_low) if len(w) > 3 and w not in ["what", "which", "about", "there", "grant", "grants", "funds", "federal", "state"]]
    for item in scored_items:
        text = f"{item.get('title')} {item.get('agency')} {item.get('description')} {item.get('matched_goal_title')}".lower()
        if any(k in text for k in keywords):
            matches.append(item)
        if len(matches) >= 4:
            break

    if matches:
        res = [
            f"### 🔍 Found {len(matches)} Relevant Opportunities for \"{user_msg}\":",
            f"Evaluated against **{short_name}** strategic goals:\n"
        ]
        for it in matches:
            res.append(f"1. **{it.get('title')}**")
            res.append(f"   - **Score**: `{it.get('total_score')}/100` ({it.get('confidence')} Fit) | **Ceiling**: ${it.get('award_ceiling', 0):,}")
            res.append(f"   - **Strategic Goal**: {it.get('matched_goal_title')} ({it.get('matched_goal_citation', '')})")
            res.append(f"   - **Source**: {it.get('source_display', 'Grants.gov')} | Effort: ~{it.get('est_completion_hours', 40)} hrs")
        res.append("\n👉 *Click on any grant in the table to view the full Explanation, Document Checklist, Sample Draft, and Past Awardees!*")
        return "\n".join(res)

    # Default fallback greeting & orientation
    top = scored_items[0] if scored_items else {}
    return (
        f"### 🤖 NC Grants Strategic AI Assistant\n"
        f"I am actively monitoring **{len(scored_items)} federal and state opportunities** for **{short_name}** ({category} Level).\n\n"
        f"**Top Ranked Opportunity Right Now:**\n"
        f"- **Title**: *{top.get('title', 'N/A')}*\n"
        f"- **Score**: **{top.get('total_score', '90.0')}/100** ({top.get('confidence', 'High')} Strategic Alignment)\n"
        f"- **Award Ceiling**: ${top.get('award_ceiling', 0):,} | **Closes in**: {top.get('days_to_close', 'N/A')} days\n\n"
        f"**You can ask me:**\n"
        f"- *\"Who was awarded previous awards for this grant?\"*\n"
        f"- *\"What documents and forms do I need to prepare?\"*\n"
        f"- *\"What are the action steps and draft notification email?\"*\n"
        f"- *\"Show me grants closing in the next 14 days.\"*\n"
        f"- *\"Find opportunities with award ceilings over $5M.\"*"
    )

def get_official_resources():
    return {
        "standard_forms": [
            {
                "code": "SF-424",
                "name": "Application for Federal Assistance",
                "omb_number": "4040-0004",
                "category": "Core Application",
                "description": "Official standard cover form required for all federal discretionary grant and cooperative agreement applications.",
                "when_required": "Mandatory for all federal grant applications submitted via Grants.gov Workspace.",
                "key_sections": ["Section 8: Applicant Entity & UEI", "Section 18: Estimated Funding Breakdown", "Section 19: Executive Order 12372 Review Status"],
                "download_url": "https://www.grants.gov/forms/forms-repository/sf-424-family"
            },
            {
                "code": "SF-424A",
                "name": "Budget Information for Non-Construction Programs",
                "omb_number": "4040-0006",
                "category": "Budget & Finance",
                "description": "Standard multi-column budget form outlining federal share vs. non-federal matching commitment by cost object classification.",
                "when_required": "Required for non-construction grant applications requesting personnel, equipment, or travel funding.",
                "key_sections": ["Section A: Budget Summary by Grant Program", "Section B: Budget Categories (Personnel, Fringe, Travel, Supplies, Indirect)", "Section C: Non-Federal Matching Resources"],
                "download_url": "https://www.grants.gov/forms/forms-repository/sf-424-family"
            },
            {
                "code": "SF-424C",
                "name": "Budget Information for Construction Programs",
                "omb_number": "4040-0008",
                "category": "Budget & Finance",
                "description": "Standard budget schedule for infrastructure, brick-and-mortar facility rehabilitation, and land acquisition.",
                "when_required": "Required for capital improvement projects (e.g. water plants, transit facilities, highway spans).",
                "key_sections": ["Administrative & Legal Expenses", "Architectural & Engineering Fees", "Site Work & Demolition", "Contingencies"],
                "download_url": "https://www.grants.gov/forms/forms-repository/sf-424-family"
            },
            {
                "code": "SF-424B",
                "name": "Assurances for Non-Construction Programs",
                "omb_number": "4040-0007",
                "category": "Legal & Assurances",
                "description": "Statutory legal assurances certifying compliance with Title VI of Civil Rights Act, Section 504 of Rehabilitation Act, and federal labor standards.",
                "when_required": "Mandatory assurance attachment on all non-construction submissions.",
                "key_sections": ["Civil Rights Assurances", "Hatch Act Compliance", "Environmental Standards"],
                "download_url": "https://www.grants.gov/forms/forms-repository/sf-424-family"
            },
            {
                "code": "SF-LLL",
                "name": "Disclosure of Lobbying Activities",
                "omb_number": "4040-0013",
                "category": "Legal & Assurances",
                "description": "Statutory disclosure under 31 U.S.C. 1352 reporting payments to lobbying entities regarding federal award actions.",
                "when_required": "Mandatory for all federal grants and contracts exceeding $100,000.",
                "key_sections": ["Type of Federal Action", "Name and Address of Lobbying Entity", "Payment Basis and Amount"],
                "download_url": "https://www.grants.gov/forms/forms-repository/sf-424-family"
            },
            {
                "code": "NC-EO12372",
                "name": "NC State Clearinghouse SPOC Review Notice",
                "omb_number": "State Form OSBM-SPOC",
                "category": "State Clearances",
                "description": "North Carolina Department of Administration intergovernmental review notification for federal financial assistance.",
                "when_required": "Required for federal programs subject to Executive Order 12372 (environmental, infrastructure, regional planning).",
                "key_sections": ["State Application Identifier (SAI)", "Regional Planning Council Review", "Environmental Assessment Sign-off"],
                "download_url": "https://www.osbm.nc.gov"
            }
        ],
        "registration_systems": [
            {
                "system": "SAM.gov",
                "title": "System for Award Management (SAM.gov)",
                "purpose": "Official federal registry for all entities doing business with or receiving financial assistance from the U.S. government.",
                "action_required": "Must maintain an ACTIVE entity registration and 12-character Unique Entity Identifier (UEI). Registrations expire annually.",
                "lead_time": "Allow 10 to 15 business days for validation of entity legal name and physical address.",
                "link": "https://sam.gov",
                "guide_steps": [
                    "Check entity registration status using legal agency name or CAGE code.",
                    "Ensure SAM.gov banking information (EFT) matches state treasury records.",
                    "Confirm active status 60 days before major proposal submission deadlines."
                ]
            },
            {
                "system": "Grants.gov",
                "title": "Grants.gov Workspace System",
                "purpose": "Central federal portal to find and submit discretionary grant applications across all 26 federal grant-making agencies.",
                "action_required": "Designate Authorized Organization Representatives (AOR) authorized to digitally sign federal proposals.",
                "lead_time": "Allow 3 to 5 business days for E-Business Point of Contact (EBiz POC) to approve AOR privileges.",
                "link": "https://www.grants.gov",
                "guide_steps": [
                    "Create user profile linked to your agency's official .gov email.",
                    "Request role of Authorized Organization Representative (AOR) under agency UEI.",
                    "Test Workspace creation and form pre-fill 14 days before close date."
                ]
            },
            {
                "system": "NC eGrants",
                "title": "North Carolina State eGrants Portal",
                "purpose": "State clearinghouse and funding portal for North Carolina state-administered grants, passthrough formula funding, and recovery programs.",
                "action_required": "Local governments, universities, and non-profits must register under the NC Department of Information Technology (NCDIT) state identity.",
                "lead_time": "1 to 2 business days for agency coordinator sign-off.",
                "link": "https://egrants.nc.gov",
                "guide_steps": [
                    "Obtain NCID user account through your state or local IT coordinator.",
                    "Request agency role in the NC eGrants Enterprise Directory.",
                    "Access state matching fund opportunities and legislative grant programs."
                ]
            }
        ],
        "compliance_guides": [
            {
                "topic": "Uniform Guidance (2 CFR 200)",
                "title": "Federal Uniform Administrative Requirements, Cost Principles & Audit Requirements",
                "key_takeaways": [
                    "Allowable Costs (§200.403): Must be necessary, reasonable, allocable, and documented.",
                    "Internal Controls (§200.303): Maintain written financial and procurement policies compliant with state and federal thresholds.",
                    "Single Audit Threshold (§200.501): Entities expending $750,000 or more in federal awards during fiscal year must conduct independent Single Audit."
                ]
            },
            {
                "topic": "Indirect Costs & NICRA",
                "title": "Negotiated Indirect Cost Rate Agreements (NICRA)",
                "key_takeaways": [
                    "Cognizant Federal Agency: Rate negotiated with the federal agency providing the preponderance of direct federal funding.",
                    "De Minimis Rate: Entities without a negotiated rate may elect to charge a de minimis rate of 10% of Modified Total Direct Costs (MTDC).",
                    "State Match Feasibility: Indirect costs may sometimes be applied toward non-federal cost-share matching with grantor approval."
                ]
            },
            {
                "topic": "Justice40 Initiative & Distressed Communities",
                "title": "Environmental Justice & Distressed County Preferences",
                "key_takeaways": [
                    "Federal Justice40: Mandates 40% of overall benefits of certain federal investments flow to disadvantaged communities.",
                    "NC Tier Rankings: State annual economic distress rankings (Tier 1, Tier 2, Tier 3) determine local matching waiver eligibility.",
                    "Climate and Economic Justice Screening Tool (CEJST): Use official federal mapping tool to verify tract qualification."
                ]
            }
        ],
        "state_contacts": [
            {
                "agency": "NC Office of State Budget and Management (OSBM)",
                "role": "Federal Grants Oversight & Compliance Clearinghouse",
                "email": "grants@osbm.nc.gov",
                "phone": "(984) 236-0600",
                "address": "116 W. Jones Street, Raleigh, NC 27603"
            },
            {
                "agency": "NC Department of Administration — State Clearinghouse",
                "role": "Executive Order 12372 Intergovernmental Review (SPOC)",
                "email": "state.clearinghouse@doa.nc.gov",
                "phone": "(984) 236-0100",
                "address": "1301 Mail Service Center, Raleigh, NC 27699"
            },
            {
                "agency": "NC Small Business & Technology Development Center (SBTDC)",
                "role": "SBIR/STTR Federal Proposal Acceleration Office",
                "email": "tech@sbtdc.org",
                "phone": "(919) 715-7272",
                "address": "5 W. Hargett Street, Suite 600, Raleigh, NC 27601"
            }
        ]
    }

def generate_weekly_digest(agency_id, email="staff.member@nc.gov"):
    """
    Generates automated weekly briefing containing:
    1. Specific action steps required this week (deadlines, documents, approvals)
    2. Top 3 strategic grant recommendations tailored to the selected agency/jurisdiction.
    """
    profiles = pipeline.load_agency_profiles()
    profile = profiles.get(agency_id, profiles.get("deq", {}))
    short_name = profile.get("short_name", "North Carolina Agency")
    name = profile.get("name", "State of North Carolina")
    citation = profile.get("source_citation", "NC State Strategic Plan")

    scored_file = os.path.join(DATA_DIR, "scored", agency_id, "latest.json")
    items = []
    if os.path.exists(scored_file):
        try:
            with open(scored_file, "r", encoding="utf-8") as f:
                items = json.load(f)
        except Exception:
            items = []

    top_3 = items[:3]
    urgent_items = [it for it in items if it.get("days_to_close") is not None and 0 <= it.get("days_to_close") <= 14][:2]

    actions_this_week = []
    if urgent_items:
        for it in urgent_items:
            actions_this_week.append({
                "deadline": f"{it.get('close_date')} ({it.get('days_to_close')} days left)",
                "opportunity": it.get("title"),
                "priority": "CRITICAL DEADLINE THIS WEEK",
                "action": f"Obtain Authorized Organization Representative (AOR) sign-off on Grants.gov Workspace package for {it.get('number')}.",
                "required_documents": it.get("key_document_tags", ["SF-424", "Project Narrative", "Budget Justification"])
            })
    else:
        top_opp = items[0] if items else {}
        actions_this_week.append({
            "deadline": "This Friday by 5:00 PM ET",
            "opportunity": top_opp.get("title", "Top Ranked Discretionary Program"),
            "priority": "STRATEGIC MILESTONE",
            "action": f"Convene proposal authors and verify state/local 20% match allocation with fiscal officers.",
            "required_documents": ["SF-424 Application", "Project Narrative Draft", "State Match Commitment Letter"]
        })
        actions_this_week.append({
            "deadline": "Next Tuesday by 12:00 PM ET",
            "opportunity": "All Upcoming Q4 Grant Applications",
            "priority": "PRE-AWARD REGISTRATION CHECK",
            "action": "Check SAM.gov active Unique Entity Identifier (UEI) registration to confirm validity beyond 90-day submission horizon.",
            "required_documents": ["Active SAM.gov UEI", "CAGE Code Verification"]
        })

    subject = f"[WEEKLY GRANTS BRIEFING] {short_name}: Actions Due This Week & Top Strategic Recommendations"
    
    body = (
        f"STATE OF NORTH CAROLINA — FEDERAL FUNDS STRATEGIC CLEARINGHOUSE\n"
        f"AUTOMATED WEEKLY ACTION BRIEFING & STRATEGIC RECOMMENDATIONS\n"
        f"Date: Monday, November 9, 2026 | Delivery: Weekly Monday Digest (7:00 AM ET)\n"
        f"Recipient: {email} | Jurisdiction: {name}\n"
        f"Governing Strategic Plan: {citation}\n"
        f"=======================================================================\n\n"
        f"PART 1: SPECIFIC ACTIONS YOU SHOULD TAKE THIS WEEK\n"
        f"-----------------------------------------------------------------------\n"
    )

    for idx, act in enumerate(actions_this_week, 1):
        body += (
            f"Action #{idx} [{act['priority']}]: Due {act['deadline']}\n"
            f"Opportunity: {act['opportunity']}\n"
            f"Specific Action: {act['action']}\n"
            f"Required Documents: {', '.join(act['required_documents'])}\n\n"
        )

    body += (
        f"PART 2: TOP 3 STRATEGIC RECOMMENDATIONS FOR {short_name.upper()}\n"
        f"-----------------------------------------------------------------------\n"
    )

    for idx, opp in enumerate(top_3, 1):
        body += (
            f"Match #{idx}: {opp.get('title')}\n"
            f"- Match Score: {opp.get('total_score')}/100 ({opp.get('confidence')} Fit)\n"
            f"- Award Ceiling: ${opp.get('award_ceiling', 0):,} | Funder: {opp.get('agency')}\n"
            f"- Deadline: {opp.get('close_date')} ({opp.get('days_to_close')} days left)\n"
            f"- Strategic Alignment: {opp.get('matched_goal_title')} ({opp.get('matched_goal_citation')})\n"
            f"- Documents Needed: {opp.get('required_documents_summary', 'SF-424, Narrative, Budget')}\n"
            f"- Official Notice Link: {opp.get('url')}\n\n"
        )

    body += (
        f"PART 3: CLEARINGHOUSE & TECHNICAL ASSISTANCE REMINDERS\n"
        f"-----------------------------------------------------------------------\n"
        f"- State Intergovernmental Clearinghouse (EO 12372) review takes 14 calendar days.\n"
        f"- For state matching fund questions, contact NC OSBM Grants Team at grants@osbm.nc.gov.\n"
        f"- Portal Dashboard: http://localhost:8000\n"
    )

    return {
        "agency_id": agency_id,
        "agency_name": name,
        "short_name": short_name,
        "recipient_email": email,
        "subject": subject,
        "delivery_schedule": "Every Monday at 7:00 AM ET",
        "actions_count": len(actions_this_week),
        "actions_this_week": actions_this_week,
        "top_recommendations": top_3,
        "formatted_memo": body
    }

def get_official_resources():
    return {
        "standard_forms": [
            {
                "code": "SF-424",
                "name": "Application for Federal Assistance",
                "omb_number": "4040-0004",
                "category": "Core Application",
                "description": "Official standard cover form required for all federal discretionary grant and cooperative agreement applications.",
                "when_required": "Mandatory for all federal grant applications submitted via Grants.gov Workspace.",
                "key_sections": ["Section 8: Applicant Entity & UEI", "Section 18: Estimated Funding Breakdown", "Section 19: Executive Order 12372 Review Status"],
                "download_url": "https://www.grants.gov/forms/forms-repository/sf-424-family"
            },
            {
                "code": "SF-424A",
                "name": "Budget Information for Non-Construction Programs",
                "omb_number": "4040-0006",
                "category": "Budget & Finance",
                "description": "Standard multi-column budget form outlining federal share vs. non-federal matching commitment by cost object classification.",
                "when_required": "Required for non-construction grant applications requesting personnel, equipment, or travel funding.",
                "key_sections": ["Section A: Budget Summary by Grant Program", "Section B: Budget Categories (Personnel, Fringe, Travel, Supplies, Indirect)", "Section C: Non-Federal Matching Resources"],
                "download_url": "https://www.grants.gov/forms/forms-repository/sf-424-family"
            },
            {
                "code": "SF-424C",
                "name": "Budget Information for Construction Programs",
                "omb_number": "4040-0008",
                "category": "Budget & Finance",
                "description": "Standard budget schedule for infrastructure, brick-and-mortar facility rehabilitation, and land acquisition.",
                "when_required": "Required for capital improvement projects (e.g. water plants, transit facilities, highway spans).",
                "key_sections": ["Administrative & Legal Expenses", "Architectural & Engineering Fees", "Site Work & Demolition", "Contingencies"],
                "download_url": "https://www.grants.gov/forms/forms-repository/sf-424-family"
            },
            {
                "code": "SF-424B",
                "name": "Assurances for Non-Construction Programs",
                "omb_number": "4040-0007",
                "category": "Legal & Assurances",
                "description": "Statutory legal assurances certifying compliance with Title VI of Civil Rights Act, Section 504 of Rehabilitation Act, and federal labor standards.",
                "when_required": "Mandatory assurance attachment on all non-construction submissions.",
                "key_sections": ["Civil Rights Assurances", "Hatch Act Compliance", "Environmental Standards"],
                "download_url": "https://www.grants.gov/forms/forms-repository/sf-424-family"
            },
            {
                "code": "SF-LLL",
                "name": "Disclosure of Lobbying Activities",
                "omb_number": "4040-0013",
                "category": "Legal & Assurances",
                "description": "Statutory disclosure under 31 U.S.C. 1352 reporting payments to lobbying entities regarding federal award actions.",
                "when_required": "Mandatory for all federal grants and contracts exceeding $100,000.",
                "key_sections": ["Type of Federal Action", "Name and Address of Lobbying Entity", "Payment Basis and Amount"],
                "download_url": "https://www.grants.gov/forms/forms-repository/sf-424-family"
            },
            {
                "code": "NC-EO12372",
                "name": "NC State Clearinghouse SPOC Review Notice",
                "omb_number": "State Form OSBM-SPOC",
                "category": "State Clearances",
                "description": "North Carolina Department of Administration intergovernmental review notification for federal financial assistance.",
                "when_required": "Required for federal programs subject to Executive Order 12372 (environmental, infrastructure, regional planning).",
                "key_sections": ["State Application Identifier (SAI)", "Regional Planning Council Review", "Environmental Assessment Sign-off"],
                "download_url": "https://www.osbm.nc.gov"
            }
        ],
        "registration_systems": [
            {
                "system": "SAM.gov",
                "title": "System for Award Management (SAM.gov)",
                "purpose": "Official federal registry for all entities doing business with or receiving financial assistance from the U.S. government.",
                "action_required": "Must maintain an ACTIVE entity registration and 12-character Unique Entity Identifier (UEI). Registrations expire annually.",
                "lead_time": "Allow 10 to 15 business days for validation of entity legal name and physical address.",
                "link": "https://sam.gov",
                "guide_steps": [
                    "Check entity registration status using legal agency name or CAGE code.",
                    "Ensure SAM.gov banking information (EFT) matches state treasury records.",
                    "Confirm active status 60 days before major proposal submission deadlines."
                ]
            },
            {
                "system": "Grants.gov",
                "title": "Grants.gov Workspace System",
                "purpose": "Central federal portal to find and submit discretionary grant applications across all 26 federal grant-making agencies.",
                "action_required": "Designate Authorized Organization Representatives (AOR) authorized to digitally sign federal proposals.",
                "lead_time": "Allow 3 to 5 business days for E-Business Point of Contact (EBiz POC) to approve AOR privileges.",
                "link": "https://www.grants.gov",
                "guide_steps": [
                    "Create user profile linked to your agency's official .gov email.",
                    "Request role of Authorized Organization Representative (AOR) under agency UEI.",
                    "Test Workspace creation and form pre-fill 14 days before close date."
                ]
            },
            {
                "system": "NC eGrants",
                "title": "North Carolina State eGrants Portal",
                "purpose": "State clearinghouse and funding portal for North Carolina state-administered grants, passthrough formula funding, and recovery programs.",
                "action_required": "Local governments, universities, and non-profits must register under the NC Department of Information Technology (NCDIT) state identity.",
                "lead_time": "1 to 2 business days for agency coordinator sign-off.",
                "link": "https://egrants.nc.gov",
                "guide_steps": [
                    "Obtain NCID user account through your state or local IT coordinator.",
                    "Request agency role in the NC eGrants Enterprise Directory.",
                    "Access state matching fund opportunities and legislative grant programs."
                ]
            }
        ],
        "compliance_guides": [
            {
                "topic": "Uniform Guidance (2 CFR 200)",
                "title": "Federal Uniform Administrative Requirements, Cost Principles & Audit Requirements",
                "key_takeaways": [
                    "Allowable Costs (§200.403): Must be necessary, reasonable, allocable, and documented.",
                    "Internal Controls (§200.303): Maintain written financial and procurement policies compliant with state and federal thresholds.",
                    "Single Audit Threshold (§200.501): Entities expending $750,000 or more in federal awards during fiscal year must conduct independent Single Audit."
                ]
            },
            {
                "topic": "Indirect Costs & NICRA",
                "title": "Negotiated Indirect Cost Rate Agreements (NICRA)",
                "key_takeaways": [
                    "Cognizant Federal Agency: Rate negotiated with the federal agency providing the preponderance of direct federal funding.",
                    "De Minimis Rate: Entities without a negotiated rate may elect to charge a de minimis rate of 10% of Modified Total Direct Costs (MTDC).",
                    "State Match Feasibility: Indirect costs may sometimes be applied toward non-federal cost-share matching with grantor approval."
                ]
            },
            {
                "topic": "Justice40 Initiative & Distressed Communities",
                "title": "Environmental Justice & Distressed County Preferences",
                "key_takeaways": [
                    "Federal Justice40: Mandates 40% of overall benefits of certain federal investments flow to disadvantaged communities.",
                    "NC Tier Rankings: State annual economic distress rankings (Tier 1, Tier 2, Tier 3) determine local matching waiver eligibility.",
                    "Climate and Economic Justice Screening Tool (CEJST): Use official federal mapping tool to verify tract qualification."
                ]
            }
        ],
        "state_contacts": [
            {
                "agency": "NC Office of State Budget and Management (OSBM)",
                "role": "Federal Grants Oversight & Compliance Clearinghouse",
                "email": "grants@osbm.nc.gov",
                "phone": "(984) 236-0600",
                "address": "116 W. Jones Street, Raleigh, NC 27603"
            },
            {
                "agency": "NC Department of Administration — State Clearinghouse",
                "role": "Executive Order 12372 Intergovernmental Review (SPOC)",
                "email": "state.clearinghouse@doa.nc.gov",
                "phone": "(984) 236-0100",
                "address": "1301 Mail Service Center, Raleigh, NC 27699"
            },
            {
                "agency": "NC Small Business & Technology Development Center (SBTDC)",
                "role": "SBIR/STTR Federal Proposal Acceleration Office",
                "email": "tech@sbtdc.org",
                "phone": "(919) 715-7272",
                "address": "5 W. Hargett Street, Suite 600, Raleigh, NC 27601"
            }
        ]
    }

def generate_weekly_digest(agency_id, email="staff.member@nc.gov"):
    """
    Generates automated weekly briefing containing:
    1. Specific action steps required this week (deadlines, documents, approvals)
    2. Top 3 strategic grant recommendations tailored to the selected agency/jurisdiction.
    """
    profiles = pipeline.load_agency_profiles()
    profile = profiles.get(agency_id, profiles.get("deq", {}))
    short_name = profile.get("short_name", "North Carolina Agency")
    name = profile.get("name", "State of North Carolina")
    citation = profile.get("source_citation", "NC State Strategic Plan")

    scored_file = os.path.join(DATA_DIR, "scored", agency_id, "latest.json")
    items = []
    if os.path.exists(scored_file):
        try:
            with open(scored_file, "r", encoding="utf-8") as f:
                items = json.load(f)
        except Exception:
            items = []

    top_3 = items[:3]
    urgent_items = [it for it in items if it.get("days_to_close") is not None and 0 <= it.get("days_to_close") <= 14][:2]

    actions_this_week = []
    if urgent_items:
        for it in urgent_items:
            actions_this_week.append({
                "deadline": f"{it.get('close_date')} ({it.get('days_to_close')} days left)",
                "opportunity": it.get("title"),
                "priority": "CRITICAL DEADLINE THIS WEEK",
                "action": f"Obtain Authorized Organization Representative (AOR) sign-off on Grants.gov Workspace package for {it.get('number')}.",
                "required_documents": it.get("key_document_tags", ["SF-424", "Project Narrative", "Budget Justification"])
            })
    else:
        top_opp = items[0] if items else {}
        actions_this_week.append({
            "deadline": "This Friday by 5:00 PM ET",
            "opportunity": top_opp.get("title", "Top Ranked Discretionary Program"),
            "priority": "STRATEGIC MILESTONE",
            "action": f"Convene proposal authors and verify state/local 20% match allocation with fiscal officers.",
            "required_documents": ["SF-424 Application", "Project Narrative Draft", "State Match Commitment Letter"]
        })
        actions_this_week.append({
            "deadline": "Next Tuesday by 12:00 PM ET",
            "opportunity": "All Upcoming Q4 Grant Applications",
            "priority": "PRE-AWARD REGISTRATION CHECK",
            "action": "Check SAM.gov active Unique Entity Identifier (UEI) registration to confirm validity beyond 90-day submission horizon.",
            "required_documents": ["Active SAM.gov UEI", "CAGE Code Verification"]
        })

    subject = f"[WEEKLY GRANTS BRIEFING] {short_name}: Actions Due This Week & Top Strategic Recommendations"
    
    body = (
        f"STATE OF NORTH CAROLINA — FEDERAL FUNDS STRATEGIC CLEARINGHOUSE\n"
        f"AUTOMATED WEEKLY ACTION BRIEFING & STRATEGIC RECOMMENDATIONS\n"
        f"Date: Monday, November 9, 2026 | Delivery: Weekly Monday Digest (7:00 AM ET)\n"
        f"Recipient: {email} | Jurisdiction: {name}\n"
        f"Governing Strategic Plan: {citation}\n"
        f"=======================================================================\n\n"
        f"PART 1: SPECIFIC ACTIONS YOU SHOULD TAKE THIS WEEK\n"
        f"-----------------------------------------------------------------------\n"
    )

    for idx, act in enumerate(actions_this_week, 1):
        body += (
            f"Action #{idx} [{act['priority']}]: Due {act['deadline']}\n"
            f"Opportunity: {act['opportunity']}\n"
            f"Specific Action: {act['action']}\n"
            f"Required Documents: {', '.join(act['required_documents'])}\n\n"
        )

    body += (
        f"PART 2: TOP 3 STRATEGIC RECOMMENDATIONS FOR {short_name.upper()}\n"
        f"-----------------------------------------------------------------------\n"
    )

    for idx, opp in enumerate(top_3, 1):
        body += (
            f"Match #{idx}: {opp.get('title')}\n"
            f"- Match Score: {opp.get('total_score')}/100 ({opp.get('confidence')} Fit)\n"
            f"- Award Ceiling: ${opp.get('award_ceiling', 0):,} | Funder: {opp.get('agency')}\n"
            f"- Deadline: {opp.get('close_date')} ({opp.get('days_to_close')} days left)\n"
            f"- Strategic Alignment: {opp.get('matched_goal_title')} ({opp.get('matched_goal_citation')})\n"
            f"- Documents Needed: {opp.get('required_documents_summary', 'SF-424, Narrative, Budget')}\n"
            f"- Official Notice Link: {opp.get('url')}\n\n"
        )

    body += (
        f"PART 3: CLEARINGHOUSE & TECHNICAL ASSISTANCE REMINDERS\n"
        f"-----------------------------------------------------------------------\n"
        f"- State Intergovernmental Clearinghouse (EO 12372) review takes 14 calendar days.\n"
        f"- For state matching fund questions, contact NC OSBM Grants Team at grants@osbm.nc.gov.\n"
        f"- Portal Dashboard: http://localhost:8000\n"
    )

    return {
        "agency_id": agency_id,
        "agency_name": name,
        "short_name": short_name,
        "recipient_email": email,
        "subject": subject,
        "delivery_schedule": "Every Monday at 7:00 AM ET",
        "actions_count": len(actions_this_week),
        "actions_this_week": actions_this_week,
        "top_recommendations": top_3,
        "formatted_memo": body
    }

class MatchmakerHandler(BaseHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def _send_json(self, data, status_code=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_csv(self, filename, csv_content):
        body = csv_content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, file_path, content_type="text/html; charset=utf-8"):
        if not os.path.exists(file_path):
            self.send_error(404, "File not found")
            return
        with open(file_path, "rb") as f:
            content = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # 1. API: List all profiles with categories (State, County, Education, Business, Municipal)
        if path == "/api/profiles":
            profiles = pipeline.load_agency_profiles()
            res = [
                {
                    "agency_id": k,
                    "name": v.get("name"),
                    "short_name": v.get("short_name"),
                    "category": v.get("category", "state"),
                    "citation": v.get("source_citation")
                }
                for k, v in profiles.items()
            ]
            self._send_json(res)
            return

        # 2. API: Integrated Grant Sources
        if path == "/api/sources":
            self._send_json([
                {"id": "all", "name": "All Integrated Sources (382+ Notices)"},
                {"id": "grants.gov", "name": "Grants.gov (Federal Notices)"},
                {"id": "sam.gov", "name": "SAM.gov Assistance Listings"},
                {"id": "nc-egrants", "name": "NC State eGrants Portal"},
                {"id": "philanthropy", "name": "NC Philanthropic Trust"}
            ])
            return

        # 2b. API: Official Grants Resources, Forms & Clearinghouse Directory
        if path == "/api/resources":
            self._send_json(get_official_resources())
            return

        # 2c. API: Weekly Digest Preview with Actions & Recommendations
        if path.startswith("/api/weekly-digest"):
            agency_id = path.replace("/api/weekly-digest", "").lstrip("/").strip() or "deq"
            self._send_json(generate_weekly_digest(agency_id))
            return

        # 2b. API: Official Grants Resources, Forms & Clearinghouse Directory
        if path == "/api/resources":
            self._send_json(get_official_resources())
            return

        # 2c. API: Weekly Digest Preview with Actions & Recommendations
        if path.startswith("/api/weekly-digest"):
            agency_id = path.replace("/api/weekly-digest", "").lstrip("/").strip() or "deq"
            self._send_json(generate_weekly_digest(agency_id))
            return

        # 3. API: Get specific agency profile
        if path.startswith("/api/profiles/"):
            agency_id = path.replace("/api/profiles/", "").strip()
            profile_file = os.path.join(DATA_DIR, "profiles", f"{agency_id}.json")
            if os.path.exists(profile_file):
                with open(profile_file, "r", encoding="utf-8") as f:
                    self._send_json(json.load(f))
            else:
                self._send_json({"error": "Profile not found"}, 404)
            return

        # 4. API: Get scored opportunities for agency
        if path.startswith("/api/scored/"):
            agency_id = path.replace("/api/scored/", "").strip()
            scored_file = os.path.join(DATA_DIR, "scored", agency_id, "latest.json")
            if os.path.exists(scored_file):
                with open(scored_file, "r", encoding="utf-8") as f:
                    self._send_json(json.load(f))
            else:
                self._send_json([], 200)
            return

        # 5. API: Get decisions for agency
        if path.startswith("/api/decisions/"):
            agency_id = path.replace("/api/decisions/", "").strip()
            dec_file = os.path.join(DATA_DIR, "decisions", f"{agency_id}.json")
            if os.path.exists(dec_file):
                with open(dec_file, "r", encoding="utf-8") as f:
                    self._send_json(json.load(f))
            else:
                self._send_json({"agency_id": agency_id, "decisions": []}, 200)
            return

        # 6. API: Get evaluation metrics (§6)
        if path == "/api/evaluation":
            eval_file = os.path.join(DATA_DIR, "eval", "labels.json")
            if os.path.exists(eval_file):
                with open(eval_file, "r", encoding="utf-8") as f:
                    self._send_json(json.load(f))
            else:
                self._send_json({"error": "Evaluation labels not found"}, 404)
            return

        # 7. API: Get run status / latest pipeline log
        if path == "/api/runs/latest":
            latest_run_file = os.path.join(DATA_DIR, "runs", "latest.json")
            if os.path.exists(latest_run_file):
                with open(latest_run_file, "r", encoding="utf-8") as f:
                    self._send_json(json.load(f))
            else:
                self._send_json({"status": "No runs recorded yet"}, 200)
            return

        # 8. API: Export shortlist to CSV (§5.6)
        if path.startswith("/api/export/"):
            parts = path.replace("/api/export/", "").split("/")
            agency_id = parts[0]
            scored_file = os.path.join(DATA_DIR, "scored", agency_id, "latest.json")
            if not os.path.exists(scored_file):
                self._send_json({"error": "No scored data found for agency"}, 404)
                return

            with open(scored_file, "r", encoding="utf-8") as f:
                items = json.load(f)

            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow([
                "Rank", "Total Score", "Confidence", "Scoring Equation", "Opportunity Title",
                "Opportunity Number", "Source", "Funding Agency", "Status",
                "Days to Close", "Close Date", "Award Ceiling ($)",
                "Estimated Effort (Hours)", "Complexity Tier", "Cost-Share Required",
                "Matched Strategic Goal", "Match Reason", "Past Awardees Summary", "Official URL"
            ])
            for i, it in enumerate(items[:50]):
                awardees_summary = "; ".join([f"{a.get('recipient')} (${a.get('amount', 0):,})" for a in it.get("past_awardees", [])[:2]])
                writer.writerow([
                    i + 1,
                    it.get("total_score", 0),
                    it.get("confidence", "Medium"),
                    it.get("equation_name", "Balanced Multi-Criteria"),
                    it.get("title", ""),
                    it.get("number", ""),
                    it.get("source_display", "Grants.gov"),
                    it.get("agency", ""),
                    it.get("status", ""),
                    it.get("days_to_close", ""),
                    it.get("close_date", ""),
                    f"${it.get('award_ceiling', 0):,}",
                    f"{it.get('est_completion_hours', 45)} hrs",
                    it.get("complexity_tier", "Standard"),
                    f"Yes ({it.get('cost_share_pct', 20)}%)" if it.get("cost_share_required") else "No (0% match)",
                    it.get("matched_goal_title", ""),
                    it.get("reason", ""),
                    awardees_summary,
                    it.get("url", "")
                ])
            filename = f"NC_{agency_id.upper()}_Federal_Grants_Shortlist_{datetime.now().strftime('%Y%m%d')}.csv"
            self._send_csv(filename, output.getvalue())
            return

        # 9. Web App Main HTML page
        if path == "/" or path == "/index.html":
            index_path = os.path.join(BASE_DIR, "public", "index.html")
            self._send_file(index_path, "text/html; charset=utf-8")
            return

        # Static assets
        safe_rel_path = path.lstrip("/")
        candidate = os.path.join(BASE_DIR, "public", safe_rel_path)
        if os.path.exists(candidate) and os.path.isfile(candidate):
            content_type = "text/plain"
            if candidate.endswith(".js"): content_type = "application/javascript"
            elif candidate.endswith(".css"): content_type = "text/css"
            elif candidate.endswith(".svg"): content_type = "image/svg+xml"
            self._send_file(candidate, content_type)
            return

        self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {}

        # 1. AI Strategic Chatbot Assistant Endpoint
        if path == "/api/chat":
            msg = payload.get("message", "").strip()
            agency_id = payload.get("agency_id", "deq")
            profiles = pipeline.load_agency_profiles()
            profile = profiles.get(agency_id, profiles.get("deq", {}))

            scored_file = os.path.join(DATA_DIR, "scored", agency_id, "latest.json")
            scored_items = []
            if os.path.exists(scored_file):
                with open(scored_file, "r", encoding="utf-8") as f:
                    scored_items = json.load(f)

            reply = build_chatbot_response(msg, agency_id, profile, scored_items)
            self._send_json({"reply": reply, "agency_id": agency_id})
            return

        # 1b. Weekly Action Plan & Digest Subscription Endpoint
        if path == "/api/subscribe":
            email = payload.get("email", "").strip()
            agency_id = payload.get("agency_id", "deq")
            role = payload.get("role", "State Agency Staff")
            
            sub_file = os.path.join(DATA_DIR, "subscribers.json")
            subs = []
            if os.path.exists(sub_file):
                try:
                    with open(sub_file, "r", encoding="utf-8") as f:
                        subs = json.load(f)
                except Exception:
                    subs = []

            # Save subscription record
            subs.append({
                "email": email,
                "agency_id": agency_id,
                "role": role,
                "subscribed_at": datetime.now().isoformat(),
                "frequency": "Weekly (Monday 7:00 AM ET)"
            })
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(sub_file, "w", encoding="utf-8") as f:
                json.dump(subs, f, indent=2)

            digest = generate_weekly_digest(agency_id, email)
            self._send_json({
                "success": True,
                "message": f"Successfully subscribed {email} for automated weekly action briefings and top recommendations!",
                "subscriber": email,
                "agency_id": agency_id,
                "digest": digest
            })
            return

        # 2. Save Staff Decision (§5.6 & §5.0)
        if path.startswith("/api/decisions/"):
            agency_id = path.replace("/api/decisions/", "").strip()
            dec_file = os.path.join(DATA_DIR, "decisions", f"{agency_id}.json")
            existing = {"agency_id": agency_id, "decisions": []}
            if os.path.exists(dec_file):
                try:
                    with open(dec_file, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                except Exception:
                    pass

            opp_id = str(payload.get("opportunity_id", ""))
            status = payload.get("status", "Pursue")
            notes = payload.get("notes", "")
            decided_by = payload.get("decided_by", "State Grants Coordinator")
            decided_at = datetime.now().isoformat()

            found = False
            for d in existing.get("decisions", []):
                if str(d.get("opportunity_id")) == opp_id:
                    d["status"] = status
                    d["notes"] = notes
                    d["decided_by"] = decided_by
                    d["decided_at"] = decided_at
                    found = True
                    break
            if not found:
                existing.setdefault("decisions", []).append({
                    "opportunity_id": opp_id,
                    "number": payload.get("number", ""),
                    "title": payload.get("title", ""),
                    "status": status,
                    "notes": notes,
                    "decided_by": decided_by,
                    "decided_at": decided_at
                })

            os.makedirs(os.path.join(DATA_DIR, "decisions"), exist_ok=True)
            with open(dec_file, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2)

            self._send_json({"success": True, "agency_id": agency_id, "saved_decision": payload})
            return

        # 3. Trigger Ingestion / Refresh Pipeline on Demand (§5.7)
        if path == "/api/pipeline/run":
            print(f"[API] Manual trigger of daily refresh pipeline initiated by user...")
            try:
                run_log = pipeline.run_pipeline()
                self._send_json({"success": True, "run_log": run_log})
            except Exception as e:
                self._send_json({"success": False, "error": str(e)}, 500)
            return

        # 4. Multi-Equation Scoring & Dynamic Sliders (§8.1 & Ranking Complexity)
        if path.startswith("/api/rescore/"):
            agency_id = path.replace("/api/rescore/", "").strip()
            scoring_model = payload.get("scoring_model", "balanced")
            weights = payload.get("weights", {
                "semantic": 0.45,
                "keyword": 0.25,
                "eligibility": 0.15,
                "timing": 0.10,
                "affinity": 0.05
            })
            profiles = pipeline.load_agency_profiles()
            profile = profiles.get(agency_id)
            if not profile:
                self._send_json({"error": "Profile not found"}, 404)
                return

            opps_file = os.path.join(DATA_DIR, "opportunities", "latest.json")
            if not os.path.exists(opps_file):
                self._send_json({"error": "No opportunities found"}, 404)
                return

            with open(opps_file, "r", encoding="utf-8") as f:
                normalized = json.load(f)

            rescored = []
            for opp in normalized:
                score_meta = pipeline.calculate_score(opp, profile, weights, scoring_model=scoring_model)
                item = dict(opp)
                item.update(score_meta)
                rescored.append(item)

            rescored.sort(key=lambda x: x["total_score"], reverse=True)
            self._send_json({
                "agency_id": agency_id,
                "scoring_model_used": scoring_model,
                "weights_used": weights,
                "count": len(rescored),
                "items": rescored
            })
            return

        self.send_error(404, "Not Found")

def run_server():
    os.makedirs(os.path.join(BASE_DIR, "public"), exist_ok=True)
    server_address = ("", PORT)
    httpd = HTTPServer(server_address, MatchmakerHandler)
    print(f"\n=======================================================")
    print(f" North Carolina Federal Funds Matchmaker Web UI running!")
    print(f" Local URL: http://localhost:{PORT}")
    print(f" Mode: {os.environ.get('MODE', 'app')}")
    print(f" Jurisdiction Profiles Active: 10 (State, County, Education, Business, Municipal)")
    print(f" Multi-Source Feeds: Grants.gov, SAM.gov, NC eGrants, Philanthropy")
    print(f"=======================================================\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        httpd.server_close()

if __name__ == "__main__":
    mode = os.environ.get("MODE", "app")
    if mode == "job":
        pipeline.run_pipeline()
    else:
        run_server()
