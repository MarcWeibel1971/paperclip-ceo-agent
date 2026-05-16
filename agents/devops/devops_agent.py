import os
import sys
import json
import requests
from datetime import datetime, timezone

# ── Configuration ─────────────────────────────────────────────────────────────
PAPERCLIP_API_KEY = os.environ.get("PAPERCLIP_API_KEY")
PAPERCLIP_BASE_URL = os.environ.get("PAPERCLIP_BASE_URL", "https://paperclip-production-15fc.up.railway.app")
COMPANY_ID = os.environ.get("PAPERCLIP_COMPANY_ID", "403e0e85-73a1-48c9-9db4-90fdd4ad984e")
AGENT_ID = os.environ.get("PAPERCLIP_AGENT_ID", "88667ec1-f9cf-4675-a648-c0499c776e90")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")

# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)

def output_result(stop_reason: str, summary: str):
    print(json.dumps({"stopReason": stop_reason, "summary": summary}))

# ── Paperclip API ─────────────────────────────────────────────────────────────

def get_assigned_issues() -> list:
    if not PAPERCLIP_API_KEY:
        return []
    try:
        r = requests.get(
            f"{PAPERCLIP_BASE_URL}/api/companies/{COMPANY_ID}/issues",
            headers={"Authorization": f"Bearer {PAPERCLIP_API_KEY}"},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            issues = data if isinstance(data, list) else data.get("issues", [])
            return [i for i in issues if i.get("assigneeAgentId") == AGENT_ID]
    except Exception as e:
        log(f"WARNING: Could not fetch assigned issues: {e}")
    return []

def post_comment(issue_id: str, body: str) -> bool:
    try:
        r = requests.post(
            f"{PAPERCLIP_BASE_URL}/api/issues/{issue_id}/comments",
            headers={"Authorization": f"Bearer {PAPERCLIP_API_KEY}", "Content-Type": "application/json"},
            json={"body": body},
            timeout=15,
        )
        return r.status_code in (200, 201)
    except Exception as e:
        log(f"WARNING: Could not post comment: {e}")
        return False

def set_issue_status(issue_id: str, status: str) -> bool:
    try:
        r = requests.patch(
            f"{PAPERCLIP_BASE_URL}/api/issues/{issue_id}",
            headers={"Authorization": f"Bearer {PAPERCLIP_API_KEY}", "Content-Type": "application/json"},
            json={"status": status},
            timeout=15,
        )
        return r.status_code == 200
    except Exception as e:
        log(f"WARNING: Could not update issue status: {e}")
        return False

# ── OpenAI ────────────────────────────────────────────────────────────────────

def call_openai(system_prompt: str, user_prompt: str) -> str:
    if not OPENAI_API_KEY:
        return ""
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt}
        ],
        "max_tokens": 1500,
        "temperature": 0.3,
    }
    try:
        url = f"{OPENAI_BASE_URL.rstrip('/')}/chat/completions"
        resp = requests.post(
            url,
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

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    log("DevOps Engineer Agent started.")

    # Loop-prevention
    wake_reason = os.environ.get("PAPERCLIP_WAKE_REASON", "heartbeat")
    if wake_reason in {"issue_commented", "comment_added", "automation", "workflow_triggered"}:
        log(f"Woken by '{wake_reason}' — skipping to prevent loop.")
        output_result("end_turn", f"Skipped: wake_reason={wake_reason}")
        sys.exit(0)

    # Fetch issues
    assigned_issues = get_assigned_issues()
    
    # Filter out auto-generated productivity reviews
    SKIP_ORIGIN_KINDS = {"issue_productivity_review", "productivity-review"}
    SKIP_TITLE_PATTERNS = ["review productivity", "productivity review"]
    assigned_issues = [
        i for i in assigned_issues
        if i.get("originKind") not in SKIP_ORIGIN_KINDS
        and not any(p in (i.get("title") or "").lower() for p in SKIP_TITLE_PATTERNS)
    ]

    log(f"State: {len(assigned_issues)} assigned issues.")

    # ── 1. Proactive Health Checks ─────────────────────────────────────────────
    health_status = "OK"
    try:
        r = requests.get("https://paperclip-production-15fc.up.railway.app", timeout=5)
        if r.status_code != 200:
            health_status = f"WARNING (HTTP {r.status_code})"
    except Exception as e:
        health_status = f"ERROR ({e})"
    log(f"System Health: {health_status}")

    if not assigned_issues:
        log("No active DevOps tasks assigned.")
        output_result("end_turn", f"Health: {health_status}. No issues to process.")
        sys.exit(0)

    # ── 2. Analyze up to 2 issues ──────────────────────────────────────────────
    commented = 0
    system_prompt = """Du bist der DevOps Engineer von Pensionierung Plus (pensionierung-plus.ch).
Deine Aufgaben umfassen:
- Server-Wartung (Railway, Cloud-PCs)
- Deployment-Pipelines und CI/CD
- Datenbank-Backups und Performance
- Behebung von Ausfällen (Health Check Alerts)

Analysiere das zugewiesene Issue aus DevOps-Sicht.
Gib eine klare, technische Diagnose und schlage konkrete Befehle oder Scripts zur Lösung vor.
Antworte auf Deutsch.
"""

    for issue in assigned_issues[:2]:
        issue_id = issue.get("id")
        if not issue_id:
            continue

        user_prompt = f"""Bitte analysiere folgendes Issue aus DevOps-Sicht:
Titel: {issue.get('title')}
Beschreibung: {issue.get('description', 'Keine Beschreibung vorhanden.')}
Aktueller System Health: {health_status}
"""
        
        log(f"Analyzing issue: {issue.get('identifier', issue_id)}")
        analysis = call_openai(system_prompt, user_prompt)
        
        if analysis:
            comment_body = (
                f"**DevOps Analyse — {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC**\n\n"
                f"{analysis}\n\n"
                f"*System Health: {health_status}*"
            )
            if post_comment(issue_id, comment_body):
                log(f"Posted DevOps analysis on {issue.get('identifier', issue_id)}")
                set_issue_status(issue_id, "in_progress")
                commented += 1

    summary = f"Analysed {len(assigned_issues)} issue(s), commented on {commented}. Health: {health_status}"
    log(f"DevOps Agent complete. {summary}")
    output_result("end_turn", summary)
    sys.exit(0)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL ERROR: {e}")
        output_result("error", str(e))
        sys.exit(1)
