"""
Lead Developer Execution Agent — Pensionierung Plus
Analysiert Issues, liest relevante Code-Dateien, generiert Fixes und öffnet PRs.
"""
import os
import sys
import json
import re
import subprocess
import tempfile
import shutil
import requests
from datetime import datetime, timezone

# ── Configuration ─────────────────────────────────────────────────────────────
PAPERCLIP_API_KEY = os.environ.get("PAPERCLIP_API_KEY")
PAPERCLIP_BASE_URL = os.environ.get("PAPERCLIP_BASE_URL", "https://paperclip-production-15fc.up.railway.app")
COMPANY_ID = os.environ.get("PAPERCLIP_COMPANY_ID", "403e0e85-73a1-48c9-9db4-90fdd4ad984e")
AGENT_ID = os.environ.get("PAPERCLIP_AGENT_ID", "646c9e94-3adf-43ce-a8f0-c8378d807fb2")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = "Marc-Weibel-Consulting-GmbH/finanzplan"
GITHUB_API = "https://api.github.com"

# Notification settings
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "marc.weibel@weibel-mueller.ch")
PAPERCLIP_APP_URL = "https://paperclip-production-15fc.up.railway.app"

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

def call_openai(system_prompt: str, user_prompt: str, model: str = "gpt-4o-mini", max_tokens: int = 2000) -> str:
    if not OPENAI_API_KEY:
        return ""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt}
        ],
        "max_tokens": max_tokens,
        "temperature": 0.2,
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
            timeout=120,
        )
        if resp.status_code != 200:
            log(f"WARNING: OpenAI API error: {resp.status_code} {resp.text[:200]}")
            return ""
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log(f"WARNING: OpenAI call failed: {e}")
        return ""

# ── GitHub API ────────────────────────────────────────────────────────────────

def github_headers() -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def get_file_content(path: str) -> str:
    """Fetch a file from the finanzplan repo via GitHub API."""
    try:
        r = requests.get(
            f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}",
            headers=github_headers(),
            timeout=15,
        )
        if r.status_code == 200:
            import base64
            content = r.json().get("content", "")
            return base64.b64decode(content).decode("utf-8", errors="replace")
    except Exception as e:
        log(f"WARNING: Could not fetch {path}: {e}")
    return ""

def get_repo_tree() -> list:
    """Get the full file tree of the finanzplan repo."""
    try:
        r = requests.get(
            f"{GITHUB_API}/repos/{GITHUB_REPO}/git/trees/main?recursive=1",
            headers=github_headers(),
            timeout=15,
        )
        if r.status_code == 200:
            return [item["path"] for item in r.json().get("tree", []) if item["type"] == "blob"]
    except Exception as e:
        log(f"WARNING: Could not fetch repo tree: {e}")
    return []

def create_branch(branch_name: str) -> bool:
    """Create a new branch from main."""
    try:
        # Get main SHA
        r = requests.get(
            f"{GITHUB_API}/repos/{GITHUB_REPO}/git/ref/heads/main",
            headers=github_headers(),
            timeout=15,
        )
        if r.status_code != 200:
            log(f"WARNING: Could not get main SHA: {r.status_code}")
            return False
        sha = r.json()["object"]["sha"]

        # Create branch
        r2 = requests.post(
            f"{GITHUB_API}/repos/{GITHUB_REPO}/git/refs",
            headers=github_headers(),
            json={"ref": f"refs/heads/{branch_name}", "sha": sha},
            timeout=15,
        )
        if r2.status_code in (200, 201):
            log(f"Created branch: {branch_name}")
            return True
        log(f"WARNING: Could not create branch: {r2.status_code} {r2.text[:200]}")
    except Exception as e:
        log(f"WARNING: create_branch error: {e}")
    return False

def commit_file(branch_name: str, path: str, content: str, message: str) -> bool:
    """Commit a file change to a branch."""
    import base64
    try:
        # Get current file SHA (if exists)
        r = requests.get(
            f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}?ref={branch_name}",
            headers=github_headers(),
            timeout=15,
        )
        file_sha = r.json().get("sha") if r.status_code == 200 else None

        payload = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch_name,
        }
        if file_sha:
            payload["sha"] = file_sha

        r2 = requests.put(
            f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}",
            headers=github_headers(),
            json=payload,
            timeout=15,
        )
        if r2.status_code in (200, 201):
            log(f"Committed {path} to {branch_name}")
            return True
        log(f"WARNING: Could not commit {path}: {r2.status_code} {r2.text[:300]}")
    except Exception as e:
        log(f"WARNING: commit_file error: {e}")
    return False

def create_pull_request(branch_name: str, title: str, body: str, draft: bool = True) -> dict:
    """Create a PR (draft by default) and return {url, number}."""
    try:
        r = requests.post(
            f"{GITHUB_API}/repos/{GITHUB_REPO}/pulls",
            headers=github_headers(),
            json={
                "title": title,
                "body": body,
                "head": branch_name,
                "base": "main",
                "draft": draft,
            },
            timeout=15,
        )
        if r.status_code in (200, 201):
            data = r.json()
            pr_url = data.get("html_url", "")
            pr_number = data.get("number", 0)
            log(f"Created {'Draft ' if draft else ''}PR #{pr_number}: {pr_url}")
            return {"url": pr_url, "number": pr_number}
        log(f"WARNING: Could not create PR: {r.status_code} {r.text[:300]}")
    except Exception as e:
        log(f"WARNING: create_pull_request error: {e}")
    return {"url": "", "number": 0}


def send_outlook_notification(issue_identifier: str, issue_title: str, pr_url: str, pr_number: int, analysis: str, changed_files: list) -> bool:
    """Send an Outlook email notification about a new Draft PR."""
    try:
        import subprocess
        files_list = "\n".join(f"  • {f}" for f in changed_files)
        subject = f"[Pensionierung Plus] Draft PR #{pr_number} bereit zur Prüfung — {issue_identifier}"
        body = (
            f"Guten Tag Marc,\n\n"
            f"Der Lead Developer Agent hat einen Code-Fix für folgendes Issue vorbereitet:\n\n"
            f"Issue: {issue_identifier} — {issue_title}\n"
            f"Analyse: {analysis}\n\n"
            f"Geänderte Dateien:\n{files_list}\n\n"
            f"Der Pull Request ist als DRAFT erstellt und wartet auf Ihre Freigabe:\n"
            f"{pr_url}\n\n"
            f"So geben Sie das Go:\n"
            f"1. Öffnen Sie den PR-Link oben\n"
            f"2. Prüfen Sie die Änderungen (Tab 'Files changed')\n"
            f"3. Klicken Sie 'Ready for review' → dann 'Merge pull request'\n"
            f"4. Railway deployt automatisch innerhalb von ~3 Minuten\n\n"
            f"Issue im Paperclip: {PAPERCLIP_APP_URL}/PEN/issues/{issue_identifier}\n\n"
            f"Mit freundlichen Grüssen\n"
            f"Lead Developer Agent — Pensionierung Plus"
        )

        input_json = json.dumps([{
            "to": [{"email": NOTIFY_EMAIL}],
            "subject": subject,
            "body": {"content": body, "contentType": "text"},
        }])

        result = subprocess.run(
            ["manus-mcp-cli", "tool", "call", "outlook_send_messages",
             "--server", "outlook-mail",
             "--input", json.dumps({"messages": [{
                 "to": [{"email": NOTIFY_EMAIL}],
                 "subject": subject,
                 "body": {"content": body, "contentType": "text"},
             }]})],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            log(f"Outlook notification sent to {NOTIFY_EMAIL}")
            return True
        log(f"WARNING: Outlook notification failed: {result.stderr[:200]}")
    except Exception as e:
        log(f"WARNING: send_outlook_notification error: {e}")
    return False

# ── Core Logic ────────────────────────────────────────────────────────────────

def find_relevant_files(issue_title: str, issue_desc: str, all_files: list) -> list:
    """Ask GPT to identify which files are relevant for this issue."""
    files_list = "\n".join(all_files[:300])  # limit to 300 files
    prompt = f"""Given this issue:
Title: {issue_title}
Description: {issue_desc}

And this list of files in the Next.js project:
{files_list}

Return a JSON array of up to 5 file paths that are most likely relevant to fix this issue.
Only return the JSON array, nothing else. Example: ["src/features/foo/Bar.tsx", "src/models/Schema.ts"]
"""
    result = call_openai(
        "You are a senior TypeScript/Next.js developer. Return only valid JSON.",
        prompt,
        model="gpt-4o-mini",
        max_tokens=300,
    )
    try:
        # Extract JSON array from response
        match = re.search(r'\[.*?\]', result, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception:
        pass
    return []

def generate_fix(issue_title: str, issue_desc: str, file_contents: dict) -> dict:
    """Ask GPT-4o to generate a code fix. Returns {filepath: new_content}."""
    files_context = ""
    for path, content in file_contents.items():
        # Limit each file to 200 lines
        lines = content.split("\n")[:200]
        files_context += f"\n\n### FILE: {path}\n```typescript\n" + "\n".join(lines) + "\n```"

    system = """Du bist der Lead Developer von Pensionierung Plus.
Das Projekt ist ein Next.js 15 App Router Projekt mit TypeScript, Tailwind v4, Drizzle ORM, Clerk Auth, oRPC.
Befolge die AGENTS.md Regeln: Named exports, absolute imports via @/, keine default exports ausser Next.js pages.
Conventional Commits: type: summary (feat|fix|refactor|...).
"""

    user = f"""Analysiere und behebe folgendes Issue:

**Titel:** {issue_title}
**Beschreibung:** {issue_desc}

**Relevante Dateien:**
{files_context}

Gib deine Antwort als JSON zurück mit folgender Struktur:
{{
  "analysis": "Kurze Analyse des Problems (2-3 Sätze)",
  "approach": "Lösungsansatz (2-3 Sätze)",
  "commit_message": "fix: kurze beschreibung was geändert wurde",
  "files": {{
    "pfad/zur/datei.tsx": "vollständiger neuer dateiinhalt"
  }}
}}

Wichtig:
- Nur Dateien ändern die wirklich geändert werden müssen
- Vollständigen Dateiinhalt liefern (nicht nur den geänderten Teil)
- Wenn keine Code-Änderung möglich ist, leeres "files" Objekt zurückgeben
"""

    result = call_openai(system, user, model="gpt-4o", max_tokens=4000)

    # Extract JSON from response
    try:
        match = re.search(r'\{.*\}', result, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        log(f"WARNING: Could not parse fix JSON: {e}")

    return {"analysis": result[:500], "approach": "", "commit_message": "fix: update", "files": {}}

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    log("Lead Developer Execution Agent started.")

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

    if not assigned_issues:
        log("No active development tasks assigned.")
        output_result("end_turn", "No issues to process.")
        sys.exit(0)

    # Check if GitHub token is available
    if not GITHUB_TOKEN:
        log("WARNING: No GITHUB_TOKEN — will analyse only, no PR creation.")

    # Get repo file tree once
    all_files = []
    if GITHUB_TOKEN:
        log("Fetching repo file tree...")
        all_files = get_repo_tree()
        log(f"Found {len(all_files)} files in repo.")

    commented = 0
    prs_created = 0

    for issue in assigned_issues[:1]:  # Process 1 issue at a time for quality
        issue_id = issue.get("id")
        issue_identifier = issue.get("identifier", issue_id)
        if not issue_id:
            continue

        title = issue.get("title", "")
        description = issue.get("description", "")
        log(f"Processing issue: {issue_identifier} — {title}")

        # Mark as in_progress immediately
        set_issue_status(issue_id, "in_progress")

        if GITHUB_TOKEN and all_files:
            # ── Execution Mode: Find files, generate fix, create PR ────────────
            log("Finding relevant files...")
            relevant_files = find_relevant_files(title, description, all_files)
            log(f"Relevant files: {relevant_files}")

            # Fetch file contents
            file_contents = {}
            for path in relevant_files:
                content = get_file_content(path)
                if content:
                    file_contents[path] = content
                    log(f"  Loaded: {path} ({len(content)} chars)")

            # Generate fix
            log("Generating code fix with GPT-4o...")
            fix = generate_fix(title, description, file_contents)

            analysis = fix.get("analysis", "")
            approach = fix.get("approach", "")
            commit_msg = fix.get("commit_message", "fix: update")
            changed_files = fix.get("files", {})

            log(f"Fix generated. Files to change: {list(changed_files.keys())}")

            # Create PR if there are file changes
            pr_url = ""
            if changed_files and GITHUB_TOKEN:
                # Create branch
                ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                branch_name = f"agent/lead-dev/{issue_identifier.lower()}-{ts}"

                if create_branch(branch_name):
                    # Commit all changed files
                    all_committed = True
                    for path, content in changed_files.items():
                        if not commit_file(branch_name, path, content, commit_msg):
                            all_committed = False
                            log(f"WARNING: Failed to commit {path}")

                    if all_committed:
                        # Create DRAFT PR (requires human approval before merge)
                        pr_body = (
                            f"## Lead Developer Agent — Automatischer Fix\n\n"
                            f"> **DRAFT PR** — Wartet auf Freigabe durch Marc Weibel\n"
                            f"> Um zu deployen: 'Ready for review' klicken → Merge → Railway deployt automatisch\n\n"
                            f"**Issue:** {issue_identifier} — {title}\n\n"
                            f"**Analyse:** {analysis}\n\n"
                            f"**Lösungsansatz:** {approach}\n\n"
                            f"**Geänderte Dateien:**\n"
                            + "\n".join(f"- `{p}`" for p in changed_files.keys())
                            + f"\n\n*Generiert von Lead Developer Agent am {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC*"
                        )
                        pr_result = create_pull_request(
                            branch_name,
                            f"{commit_msg} ({issue_identifier})",
                            pr_body,
                            draft=True,
                        )
                        pr_url = pr_result.get("url", "")
                        pr_number = pr_result.get("number", 0)
                        if pr_url:
                            prs_created += 1
                            # Send Outlook notification
                            send_outlook_notification(
                                issue_identifier, title, pr_url, pr_number,
                                analysis, list(changed_files.keys())
                            )

            # Post comment with results
            if pr_url:
                comment_body = (
                    f"**Lead Developer — Draft PR bereit zur Prüfung** — {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC\n\n"
                    f"**Analyse:** {analysis}\n\n"
                    f"**Lösungsansatz:** {approach}\n\n"
                    f"**Draft Pull Request:** {pr_url}\n\n"
                    f"**Geänderte Dateien:** {', '.join(f'`{p}`' for p in changed_files.keys())}\n\n"
                    f"**Nächster Schritt:** Bitte den PR prüfen und 'Ready for review' klicken → Merge → Railway deployt automatisch.\n\n"
                    f"*Eine Benachrichtigung wurde an {NOTIFY_EMAIL} gesendet.*"
                )
            else:
                comment_body = (
                    f"**Lead Developer Analyse — {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC**\n\n"
                    f"**Analyse:** {analysis}\n\n"
                    f"**Lösungsansatz:** {approach}\n\n"
                    + (f"*Keine Code-Änderungen generiert — manuelle Implementierung erforderlich.*" if not changed_files else f"*PR-Erstellung fehlgeschlagen. Bitte manuell prüfen.*")
                )
        else:
            # ── Analysis-only Mode (no GitHub token) ──────────────────────────
            analysis = call_openai(
                "Du bist der Lead Fullstack Developer von Pensionierung Plus. Analysiere Issues und schlage Lösungen vor. Antworte auf Deutsch.",
                f"Titel: {title}\nBeschreibung: {description}",
                model="gpt-4o-mini",
                max_tokens=1500,
            )
            comment_body = (
                f"**Lead Developer Analyse — {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC**\n\n"
                f"{analysis}"
            )

        if post_comment(issue_id, comment_body):
            log(f"Posted comment on {issue_identifier}")
            commented += 1

    summary = f"Processed {len(assigned_issues)} issue(s), commented on {commented}, PRs created: {prs_created}."
    log(f"Lead Developer Agent complete. {summary}")
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
