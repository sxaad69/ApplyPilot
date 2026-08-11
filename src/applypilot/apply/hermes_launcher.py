"""Hermes + Playwright MCP job application launcher.

Spawns `hermes chat -q` with the apply-to-ats skill, captures the strict JSON
response the skill emits, parses it, and maps to ApplyPilot's DB status strings.

The skill (apply-to-ats v2+) returns exactly one JSON object:
  success -> {"outcome":"success","confirmation_text":"...","screenshot":"..."}
  blocked -> {"outcome":"blocked","reason":"...","error":"...","screenshot":"..."}
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import datetime

log = logging.getLogger(__name__)


def _build_prompt(job: dict, resume_pdf: str, dry_run: bool = False) -> str:
    """Build the Hermes prompt: skill invocation + job data + strict JSON return.

    Explicitly names the Playwright MCP tools so the model uses them (they
    launch their own browser) rather than Hermes' native browser_cdp tool
    (which points at a possibly-dead CDP endpoint).
    """
    lines = ["/apply-to-ats", ""]
    if dry_run:
        lines.append("DRY RUN: fill and upload but DO NOT submit.")
    else:
        lines.append("Submit this application.")
    lines += [
        "",
        "Use the Playwright MCP browser tools ONLY (tools named mcp__playwright__*).",
        "Do NOT use browser_cdp, browser_navigate (native), or any native browser tools.",
        "For navigation use mcp__playwright__browser_navigate.",
        "For uploading the resume use mcp__playwright__browser_file_upload with the resume path.",
        "For filling fields use mcp__playwright__browser_fill_form or mcp__playwright__browser_type.",
        "",
        f"Job: {job.get('title')} at {job.get('site')}",
        f"Application URL: {job.get('application_url') or job.get('url')}",
        f"Resume PDF: {resume_pdf}",
        "",
        "After completing, return EXACTLY ONE JSON object per the skill format.",
        "Do not add any text outside the JSON.",
    ]
    return "\n".join(lines)


def _parse_result_output(output: str) -> dict:
    """Extract the strict JSON object from Hermes' final output.

    Falls back to a block/unknown outcome if no JSON is found.
    """
    # Find the last {...} block (strict JSON is the entire response).
    start = output.rfind("{")
    end = output.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(output[start:end + 1])
            if isinstance(data, dict) and "outcome" in data:
                return data
        except json.JSONDecodeError:
            pass

    # No JSON -> unknown. Search for known signals in raw text.
    lowered = output.lower()
    if "success" in lowered or "thank you for your application" in lowered:
        return {"outcome": "success", "confirmation_text": output[-500:], "screenshot": None}
    if "spam" in lowered or "flagged" in lowered or "couldn't submit" in lowered:
        return {"outcome": "blocked", "reason": "spam flag", "error": output[-500:], "screenshot": None}
    return {"outcome": "blocked", "reason": "unknown", "error": output[-500:], "screenshot": None}


def _map_to_status(result: dict, dry_run: bool = False) -> str:
    """Map the parsed JSON to ApplyPilot's DB status strings."""
    if dry_run:
        return "dry_run"
    outcome = result.get("outcome", "blocked")
    if outcome == "success":
        return "applied"
    reason = str(result.get("reason", "unknown")).lower()
    if reason in ("spam flag", "spam_flag"):
        return "failed:spam_flag"
    if reason == "unclear":
        return "failed:unclear"
    return "failed:" + (reason or "unknown")


def apply_one_hermes(job: dict, resume_pdf: str, worker_id: int = 0,
                     dry_run: bool = False, timeout: int = 600) -> tuple[str, dict]:
    """Run one job application via Hermes + Playwright MCP.

    Returns:
        (status_string, detail) where detail includes confirmation_text, error,
        screenshot, and duration_ms.
    """
    from applypilot.apply.chrome import reset_worker_dir
    from applypilot.apply.dashboard import add_event, update_state

    prompt = _build_prompt(job, resume_pdf, dry_run=dry_run)

    # Hermes CLI — same profile/config as the gateway (uses Playwright MCP).
    hermes_bin = os.environ.get("HERMES_BIN", "hermes")
    # Ensure hermes (in ~/.local/bin) and npx (in /usr/local/bin) are on PATH.
    local_bin = os.path.expanduser("~/.local/bin")
    cmd = [
        hermes_bin,
        "chat", "-q", prompt,
    ]

    worker_dir = reset_worker_dir(worker_id)
    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("site", ""), score=job.get("fit_score", 0),
                 start_time=time.time(), actions=0, last_action="starting")
    add_event(f"[W{worker_id}] Hermes apply: {job['title'][:40]} @ {job.get('site', '')}")

    start = time.time()
    output = ""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(worker_dir),
            check=False,
            env={**os.environ, "PATH": f"{local_bin}:/usr/local/bin:{os.environ.get('PATH', '')}"},
        )
        output = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        add_event(f"[W{worker_id}] TIMEOUT after {timeout}s")
        return "failed:timeout", {"duration_ms": int((time.time() - start) * 1000)}

    elapsed_ms = int((time.time() - start) * 1000)
    result = _parse_result_output(output)
    status = _map_to_status(result, dry_run=dry_run)

    detail = {
        "status": status,
        "duration_ms": elapsed_ms,
        "confirmation_text": result.get("confirmation_text"),
        "error": result.get("error"),
        "reason": result.get("reason"),
        "screenshot": result.get("screenshot"),
        "raw_output": output[-2000:],
    }

    add_event(f"[W{worker_id}] {status.upper()} ({elapsed_ms // 1000}s): {job['title'][:30]}")
    update_state(worker_id, status=status, last_action=f"{status} ({elapsed_ms // 1000}s)")

    # Persist apply result + screenshot path to the job.
    _persist_to_db(job, status, detail)
    return status, detail


def _persist_to_db(job: dict, status: str, detail: dict) -> None:
    """Write apply outcome + screenshot path to the jobs row."""
    from applypilot.database import get_connection

    conn = get_connection()
    now = datetime.now().astimezone().isoformat()
    url = job.get("url")
    if not url:
        return
    apply_status = "applied" if status == "applied" else ("error" if status.startswith("failed") else status)
    conn.execute(
        "UPDATE jobs SET apply_status=?, applied_at=?, apply_error=?, "
        "apply_attempts=COALESCE(apply_attempts,0)+1, apply_duration_ms=?, "
        "agent_id='hermes', screenshot_path=? WHERE url=?",
        (apply_status, now if status == "applied" else None,
         detail.get("error") or detail.get("confirmation_text"), detail.get("duration_ms"),
         detail.get("screenshot"), url),
    )
    conn.commit()
    log.info("DB updated for %s: %s (screenshot=%s)", url, status, detail.get("screenshot"))
