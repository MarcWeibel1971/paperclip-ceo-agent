#!/usr/bin/env python3
"""
Paperclip CEO Agent — Process Adapter Script (v2 — Upgraded)
=============================================================
Runs as a child process spawned by Paperclip's process adapter.

Environment variables injected by Paperclip:
  - PAPERCLIP_API_KEY    : API key for the Paperclip REST API
  - PAPERCLIP_AGENT_ID   : This agent's ID
  - PAPERCLIP_COMPANY_ID : The company/workspace ID
  - PAPERCLIP_BASE_URL   : Base URL of the Paperclip server

The script:
1. Reads open tasks/goals assigned to the CEO agent
2. Scans for development problems (blocked issues, missing roles, app health)
3. Autonomously creates issues for detected problems
4. Autonomously hires new agents (CFO, DevOps, etc.) if critical roles are missing
5. Calls OpenAI GPT-4o-mini for strategic analysis (Perplexity as fallback)
6. Posts a comment on the assigned issue
7. Outputs a JSON result with stopReason so Paperclip records the disposition
8. Exits with code 0 on success

Paperclip reads the last JSON line of stdout as resultJson.
"""

import os
import sys
import json
import requests
from datetime import datetime, timezone

# ── Configuration ──────────────────────────────────────────────────────────────
PAPERCLIP_API_KEY    = os.environ.get("PAPERCLIP_API_KEY",    "PAPERCLIP_KEY_PLACEHOLDER")
PAPERCLIP_AGENT_ID   = os.environ.get("PAPERCLIP_AGENT_ID",   "16945af7-227f-483e-9300-3f394477ad7a")
PAPERCLIP_COMPANY_ID = os.environ.get("PAPERCLIP_COMPANY_ID", "403e0e85-73a1-48c9-9db4-90fdd4ad984e")
PAPERCLIP_BASE_URL   = os.environ.get("PAPERCLIP_BASE_URL",   "https://paperclip-production-15fc.up.railway.app")
OPENAI_API_KEY       = os.environ.get("OPENAI_API_KEY", os.environ.get("OPENAI_KEY", ""))
OPENAI_BASE_URL      = os.environ.get("OPENAI_BASE_URL", os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1"))
PERPLEXITY_API_KEY   = os.environ.get("PERPLEXITY_API_KEY",   "PERPLEXITY_KEY_PLACEHOLDER")

# ── Desired org chart — CEO will hire these roles if they are missing ──────────
DESIRED_ROLES = [
    {
        "role":  "cfo",
        "name":  "CFO",
        "title": "Chief Financial Officer",
        "description": (
            "Verantwortlich für Finanzplanung, Budgetkontrolle und Investitionsstrategie "
            "von Pensionierung Plus. Überwacht alle finanziellen KPIs und Reporting."
        ),
    },
    {
        "role":  "devops",
        "name":  "DevOps Engineer",
        "title": "DevOps & Infrastructure Engineer",
        "description": (
            "Verantwortlich für CI/CD-Pipelines, Railway-Deployments, Monitoring und "
            "Infrastruktur-Sicherheit der Pensionierung-Plus-Plattform."
        ),
    },
    {
        "role":  "engineer",
        "name":  "Lead Developer",
        "title": "Lead Fullstack Developer",
        "description": (
            "Technische Gesamtverantwortung für die Finanzplanungsapp. Koordiniert "
            "Frontend (React), Backend (Node/Python) und Datenbankarchitektur."
        ),
    },
]

# ── Blocked-issue threshold that triggers a management alert ──────────────────
BLOCKED_ISSUE_ALERT_THRESHOLD = 5

paperclip_headers = {
    "Authorization": f"Bearer {PAPERCLIP_API_KEY}",
    "Content-Type": "application/json"
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def log(msg: str):
    """Print to stdout — Paperclip captures this as agent output."""
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)


def output_result(stop_reason: str, summary: str):
    """Output the final JSON result — Paperclip reads this as resultJson."""
    result = {"stopReason": stop_reason, "summary": summary}
    print(json.dumps(result), flush=True)


# ── Paperclip API helpers ──────────────────────────────────────────────────────

def get_all_issues() -> list:
    """Fetch ALL company issues (all statuses, all assignees)."""
    url = f"{PAPERCLIP_BASE_URL}/api/companies/{PAPERCLIP_COMPANY_ID}/issues"
    try:
        resp = requests.get(url, headers=paperclip_headers, timeout=15)
        if resp.status_code != 200:
            log(f"WARNING: Could not fetch issues: {resp.status_code}")
            return []
        data = resp.json()
        return data if isinstance(data, list) else data.get("issues", data.get("data", []))
    except Exception as e:
        log(f"WARNING: Error fetching issues: {e}")
        return []


def get_assigned_issues(all_issues: list) -> list:
    """Filter issues that are assigned to this CEO agent and are active."""
    active_statuses = {"backlog", "todo", "in_progress", "in-progress"}
    return [
        i for i in all_issues
        if (i.get("status") or "").lower() in active_statuses
        and i.get("assigneeAgentId") == PAPERCLIP_AGENT_ID
    ]


def get_goals() -> list:
    """Fetch active company goals."""
    url = f"{PAPERCLIP_BASE_URL}/api/companies/{PAPERCLIP_COMPANY_ID}/goals"
    try:
        resp = requests.get(url, headers=paperclip_headers, timeout=15)
        if resp.status_code != 200:
            return []
        data = resp.json()
        return data if isinstance(data, list) else data.get("goals", [])
    except Exception:
        return []


def get_agents() -> list:
    """Fetch all agents in the company."""
    url = f"{PAPERCLIP_BASE_URL}/api/companies/{PAPERCLIP_COMPANY_ID}/agents"
    try:
        resp = requests.get(url, headers=paperclip_headers, timeout=15)
        if resp.status_code != 200:
            log(f"WARNING: Could not fetch agents: {resp.status_code}")
            return []
        data = resp.json()
        return data if isinstance(data, list) else data.get("agents", [])
    except Exception as e:
        log(f"WARNING: Error fetching agents: {e}")
        return []


def create_issue(title: str, description: str, priority: str = "high") -> dict | None:
    """Create a new issue in the company backlog."""
    url = f"{PAPERCLIP_BASE_URL}/api/companies/{PAPERCLIP_COMPANY_ID}/issues"
    payload = {
        "title": title,
        "description": description,
        "priority": priority,
        "status": "todo",
    }
    try:
        resp = requests.post(url, headers=paperclip_headers, json=payload, timeout=15)
        if resp.status_code in (200, 201):
            issue = resp.json()
            log(f"Created issue: [{issue.get('identifier', '?')}] {title}")
            return issue
        else:
            log(f"WARNING: Could not create issue '{title}': {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log(f"WARNING: Error creating issue: {e}")
    return None


def hire_agent(role_cfg: dict) -> dict | None:
    """Create a new agent (hire a new employee) via the Paperclip API."""
    url = f"{PAPERCLIP_BASE_URL}/api/companies/{PAPERCLIP_COMPANY_ID}/agents"
    payload = {
        "name":  role_cfg["name"],
        "role":  role_cfg["role"],
        "title": role_cfg["title"],
    }
    try:
        resp = requests.post(url, headers=paperclip_headers, json=payload, timeout=15)
        if resp.status_code in (200, 201):
            agent = resp.json()
            log(f"Hired new employee: {role_cfg['name']} ({role_cfg['title']}) — ID: {agent.get('id')}")
            return agent
        else:
            log(f"WARNING: Could not hire {role_cfg['name']}: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log(f"WARNING: Error hiring agent: {e}")
    return None


def post_comment(issue_id: str, body: str) -> bool:
    """Post a comment on an issue."""
    url = f"{PAPERCLIP_BASE_URL}/api/issues/{issue_id}/comments"
    try:
        resp = requests.post(url, headers=paperclip_headers, json={"body": body}, timeout=15)
        return resp.status_code in (200, 201)
    except Exception:
        return False


def close_issue(issue_id: str) -> bool:
    """Mark an issue as done."""
    url = f"{PAPERCLIP_BASE_URL}/api/issues/{issue_id}"
    try:
        resp = requests.patch(url, headers=paperclip_headers, json={"status": "done"}, timeout=15)
        return resp.status_code in (200, 201)
    except Exception:
        return False


# ── AI helpers ─────────────────────────────────────────────────────────────────

def call_openai(system_prompt: str, user_prompt: str) -> str:
    """Call OpenAI GPT-4o-mini for CEO analysis."""
    if not OPENAI_API_KEY:
        return ""
    payload = {
        "model": "gpt-4.1-mini",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt}
        ],
        "max_tokens": 1500,
        "temperature": 0.7,
    }
    try:
        resp = requests.post(
            f"{OPENAI_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        if resp.status_code != 200:
            log(f"WARNING: OpenAI API error: {resp.status_code} {resp.text[:200]}")
            return ""
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log(f"WARNING: OpenAI call failed: {e}")
        return ""


def call_perplexity(system_prompt: str, user_prompt: str) -> str:
    """Call Perplexity AI (sonar model) — fallback."""
    if not PERPLEXITY_API_KEY:
        return ""
    payload = {
        "model": "sonar",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt}
        ],
        "max_tokens": 1500,
        "temperature": 0.7,
    }
    try:
        resp = requests.post(
            "https://api.perplexity.ai/chat/completions",
            headers={
                "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        if resp.status_code != 200:
            log(f"WARNING: Perplexity API error: {resp.status_code} {resp.text[:200]}")
            return ""
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log(f"WARNING: Perplexity call failed: {e}")
        return ""


# ── Autonomous actions ─────────────────────────────────────────────────────────

def run_hiring_check(existing_agents: list, existing_issues: list) -> list:
    """
    Compare the current org chart against DESIRED_ROLES.
    For each missing role: hire the agent AND create an onboarding issue.
    Returns list of newly hired agent dicts.
    """
    existing_roles = {(a.get("role") or "").lower() for a in existing_agents}
    hired = []

    for role_cfg in DESIRED_ROLES:
        if role_cfg["role"].lower() in existing_roles:
            log(f"Role '{role_cfg['role']}' already filled — skipping.")
            continue

        # Check if we already created a hiring issue for this role (avoid duplicates)
        hiring_title = f"Onboarding: {role_cfg['name']} einrichten"
        already_requested = any(
            hiring_title.lower() in (i.get("title") or "").lower()
            for i in existing_issues
        )
        if already_requested:
            log(f"Hiring issue for '{role_cfg['name']}' already exists — skipping duplicate.")
            continue

        log(f"Critical role missing: {role_cfg['name']}. Initiating hiring process...")
        agent = hire_agent(role_cfg)
        if agent:
            hired.append(agent)
            # Create an onboarding issue so the team knows a new agent was added
            create_issue(
                title=hiring_title,
                description=(
                    f"Der CEO hat automatisch einen neuen Mitarbeiter eingestellt:\n\n"
                    f"**Rolle:** {role_cfg['title']}\n"
                    f"**Aufgaben:** {role_cfg['description']}\n\n"
                    f"Bitte konfiguriere den Agenten (Adapter, Skills, Instructions) "
                    f"und weise ihm erste Aufgaben zu."
                ),
                priority="high",
            )

    return hired


def run_problem_detection(all_issues: list) -> list:
    """
    Scan all issues for operational problems and create alert issues if needed.
    Returns list of newly created alert issue dicts.
    """
    created = []
    existing_titles_lower = {(i.get("title") or "").lower() for i in all_issues}

    # ── 1. Too many blocked issues ─────────────────────────────────────────────
    blocked = [i for i in all_issues if (i.get("status") or "").lower() == "blocked"]
    if len(blocked) >= BLOCKED_ISSUE_ALERT_THRESHOLD:
        alert_title = f"Management Alert: {len(blocked)} blockierte Issues erfordern Massnahmen"
        if not any("management alert" in t and "blockiert" in t for t in existing_titles_lower):
            blocked_list = "\n".join(
                f"- [{i.get('identifier','?')}] {i.get('title','?')}"
                for i in blocked[:10]
            )
            issue = create_issue(
                title=alert_title,
                description=(
                    f"Es gibt aktuell **{len(blocked)} blockierte Issues**. "
                    f"Das übersteigt den Schwellenwert von {BLOCKED_ISSUE_ALERT_THRESHOLD}.\n\n"
                    f"**Blockierte Issues (Auswahl):**\n{blocked_list}\n\n"
                    f"**Empfehlung:** Sofortiges Team-Meeting zur Unblocking-Session. "
                    f"Prüfe Abhängigkeiten und weise Verantwortliche zu."
                ),
                priority="high",
            )
            if issue:
                created.append(issue)

    # ── 2. App health check ────────────────────────────────────────────────────
    # Check the Finanzplanungsapp (alis) on Railway
    app_urls_to_check = [
        ("Finanzplanungsapp (alis)", "https://alis-production.up.railway.app"),
        ("Paperclip Server",         PAPERCLIP_BASE_URL),
    ]
    for app_name, base_url in app_urls_to_check:
        try:
            resp = requests.get(base_url, timeout=10)
            if resp.status_code >= 500:
                alert_title = f"KRITISCH: {app_name} nicht erreichbar (HTTP {resp.status_code})"
                if not any(alert_title.lower() in t for t in existing_titles_lower):
                    issue = create_issue(
                        title=alert_title,
                        description=(
                            f"Der Health-Check für **{app_name}** hat einen Fehler zurückgegeben:\n\n"
                            f"- URL: `{base_url}`\n"
                            f"- HTTP Status: `{resp.status_code}`\n\n"
                            f"**Sofortmassnahme:** DevOps Engineer muss das Deployment prüfen "
                            f"und die Ursache des Fehlers beheben."
                        ),
                        priority="critical",
                    )
                    if issue:
                        created.append(issue)
        except requests.exceptions.ConnectionError:
            alert_title = f"KRITISCH: {app_name} nicht erreichbar (Connection Error)"
            if not any(alert_title.lower() in t for t in existing_titles_lower):
                issue = create_issue(
                    title=alert_title,
                    description=(
                        f"Der Health-Check für **{app_name}** ist fehlgeschlagen:\n\n"
                        f"- URL: `{base_url}`\n"
                        f"- Fehler: Connection refused / Timeout\n\n"
                        f"**Sofortmassnahme:** DevOps Engineer muss das Deployment prüfen."
                    ),
                    priority="critical",
                )
                if issue:
                    created.append(issue)
        except Exception:
            pass  # Non-critical errors are silently ignored

    return created


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    log("CEO Agent v2 started.")

    # ── Loop-prevention: skip if woken by own actions ─────────────────────────
    wake_reason = os.environ.get("PAPERCLIP_WAKE_REASON", "heartbeat")
    SKIP_REASONS = {
        "issue_commented",
        "comment_added",
        "automation",
        "sub_issue_created",
        "workflow_triggered",
        "source_scoped_recovery_action",
    }
    if wake_reason in SKIP_REASONS:
        log(f"Woken by '{wake_reason}' — skipping to prevent loop.")
        output_result("end_turn", f"Skipped: wake_reason={wake_reason}")
        sys.exit(0)

    # ── Fetch current state ───────────────────────────────────────────────────
    all_issues = get_all_issues()
    agents     = get_agents()
    goals      = get_goals()

    # Filter assigned issues (exclude auto-generated productivity reviews)
    SKIP_ORIGIN_KINDS   = {"issue_productivity_review", "productivity-review"}
    SKIP_TITLE_PATTERNS = ["review productivity", "productivity review"]
    assigned_issues = [
        i for i in get_assigned_issues(all_issues)
        if i.get("originKind") not in SKIP_ORIGIN_KINDS
        and not any(p in (i.get("title") or "").lower() for p in SKIP_TITLE_PATTERNS)
    ]

    log(f"State: {len(all_issues)} total issues, {len(assigned_issues)} assigned to CEO, "
        f"{len(agents)} agents, {len(goals)} goals.")

    # ── Autonomous Action 1: Hiring ───────────────────────────────────────────
    log("--- Running hiring check ---")
    hired = run_hiring_check(agents, all_issues)
    if hired:
        log(f"Hired {len(hired)} new employee(s): {[a.get('name') for a in hired]}")

    # ── Autonomous Action 2: Problem Detection ────────────────────────────────
    log("--- Running problem detection ---")
    alerts_created = run_problem_detection(all_issues)
    if alerts_created:
        log(f"Created {len(alerts_created)} alert issue(s).")

    # ── Strategic AI Analysis ─────────────────────────────────────────────────
    if not assigned_issues and not goals:
        log("No active tasks or goals assigned to CEO.")
        summary = (
            f"Hired {len(hired)} agent(s). "
            f"Created {len(alerts_created)} alert(s). "
            f"No assigned issues to analyse."
        )
        output_result("end_turn", summary)
        sys.exit(0)

    issues_text = "\n".join([
        f"- [{i.get('identifier', 'N/A')}] {i.get('title', 'Untitled')} "
        f"(Status: {i.get('status', 'unknown')}, Priorität: {i.get('priority', 'unknown')})"
        for i in assigned_issues
    ]) or "Keine zugewiesenen Issues."

    goals_text = "\n".join([
        f"- {g.get('title', 'Untitled')} (Status: {g.get('status', 'unknown')})"
        for g in goals
    ]) or "Keine aktiven Goals."

    agents_text = "\n".join([
        f"- {a.get('name', 'N/A')} ({a.get('title') or a.get('role', 'N/A')}) — Status: {a.get('status', 'unknown')}"
        for a in agents
    ]) or "Keine Agents."

    blocked_count = sum(1 for i in all_issues if (i.get("status") or "").lower() == "blocked")
    hiring_summary = (
        f"Neu eingestellt: {', '.join(a.get('name','?') for a in hired)}"
        if hired else "Keine neuen Einstellungen."
    )
    alerts_summary = (
        f"Neue Alerts: {', '.join(i.get('title','?') for i in alerts_created[:3])}"
        if alerts_created else "Keine neuen Alerts."
    )

    system_prompt = """Du bist der CEO-Agent von Pensionierung Plus (pensionierung-plus.ch), einer Schweizer Finanzplanungsplattform für Pensionierung, Vorsorge und Vermögensoptimierung.

Du hast bereits autonom folgende Massnahmen ergriffen:
- Fehlende Mitarbeiter eingestellt (Hiring Check)
- Entwicklungsprobleme erkannt und Issues erstellt (Problem Detection)

Deine weiteren Aufgaben:
- Analysiere die aktuellen Issues, Goals und das Team
- Priorisiere strategisch die wichtigsten Massnahmen
- Gib konkrete, handlungsorientierte Empfehlungen auf Deutsch
- Antworte präzise und professionell als CEO

Format deiner Antwort:
1. **Lagebeurteilung** (2-3 Sätze zur aktuellen Situation)
2. **Top-Prioritäten diese Woche** (max. 3 Punkte)
3. **Konkrete nächste Schritte** (max. 3 Aktionen mit Verantwortlichem)"""

    today = datetime.now(timezone.utc).strftime('%d.%m.%Y')
    user_prompt = f"""CEO Dashboard — {today}

**Team ({len(agents)} Agents):**
{agents_text}

**Zugewiesene Issues ({len(assigned_issues)}):**
{issues_text}

**Aktive Goals:**
{goals_text}

**Operative Lage:**
- Blockierte Issues gesamt: {blocked_count}
- Autonome Massnahmen heute: {hiring_summary} | {alerts_summary}

Bitte analysiere die Situation und gib deine strategische CEO-Einschätzung."""

    # Try OpenAI first, then Perplexity as fallback
    analysis = ""
    if OPENAI_API_KEY:
        log("Calling OpenAI GPT-4o-mini for CEO analysis...")
        analysis = call_openai(system_prompt, user_prompt)
        if analysis:
            log(f"OpenAI analysis received ({len(analysis)} chars).")
        else:
            log("OpenAI failed, trying Perplexity as fallback...")

    if not analysis and PERPLEXITY_API_KEY:
        log("Calling Perplexity AI for CEO analysis...")
        analysis = call_perplexity(system_prompt, user_prompt)
        if analysis:
            log(f"Perplexity analysis received ({len(analysis)} chars).")

    if not analysis:
        log("ERROR: No response from any AI provider.")
        output_result("error", "No response from AI providers")
        sys.exit(1)

    # ── Post comment on assigned issues (max 2) and set disposition ─────────────
    commented = 0
    # Find Lead Developer agent for delegation
    lead_dev_agent = next(
        (a for a in agents if (a.get("role") or "").lower() in ("engineer", "cto")
         or "lead" in (a.get("name") or "").lower()),
        None
    )
    for issue in assigned_issues[:2]:
        issue_id = issue.get("id")
        if not issue_id:
            continue

        # Build delegation note if we can reassign
        delegation_note = ""
        if lead_dev_agent:
            delegation_note = (
                f"\n\n**Delegation:** Diese Aufgabe wird dem Lead Developer "
                f"({lead_dev_agent.get('name', 'Lead Developer')}) zugewiesen."
            )

        comment_body = (
            f"**CEO Review — {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC**\n\n"
            f"{analysis}"
            f"{delegation_note}\n\n"
            f"---\n"
            f"*Autonome Massnahmen: {hiring_summary} | {alerts_summary}*"
        )
        if post_comment(issue_id, comment_body):
            log(f"Posted comment on {issue.get('identifier', issue_id)}")
            commented += 1

            # Set issue to in_progress and delegate to Lead Developer if available
            patch_data = {"status": "in_progress"}
            if lead_dev_agent:
                patch_data["assigneeAgentId"] = lead_dev_agent.get("id")
                log(f"Delegating {issue.get('identifier', issue_id)} to {lead_dev_agent.get('name')}")
            try:
                r = requests.patch(
                    f"{PAPERCLIP_BASE_URL}/api/issues/{issue_id}",
                    headers={"Authorization": f"Bearer {PAPERCLIP_API_KEY}", "Content-Type": "application/json"},
                    json=patch_data,
                    timeout=15,
                )
                if r.status_code == 200:
                    log(f"Issue {issue.get('identifier', issue_id)} set to in_progress")
                else:
                    log(f"WARNING: Could not update issue status: {r.status_code}")
            except Exception as e:
                log(f"WARNING: Issue update failed: {e}")
        else:
            log(f"WARNING: Could not post comment on {issue.get('identifier', issue_id)}")

    summary = (
        f"Analysed {len(assigned_issues)} issue(s), commented on {commented}. "
        f"Hired {len(hired)} agent(s). Created {len(alerts_created)} alert(s)."
    )
    log(f"CEO Agent v2 complete. {summary}")
    output_result("end_turn", summary)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        output_result("error", str(e))
        sys.exit(1)
