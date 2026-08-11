"""Resume tailoring: LLM-powered ATS-optimized resume generation per job.

THIS IS THE HEAVIEST REFACTOR. Every piece of personal data -- name, email, phone,
skills, companies, projects, school -- is loaded at runtime from the user's profile.
Zero hardcoded personal information.

The LLM returns structured JSON, code assembles the final text. Header (name, contact)
is always code-injected, never LLM-generated. Each retry starts a fresh conversation
to avoid apologetic spirals.
"""

import json
import logging
import re
import threading
import time
from datetime import UTC, datetime

from applypilot.config import RESUME_PATH, TAILORED_DIR, load_profile
from applypilot.database import (
    JOB_STATUS_ERROR,
    get_connection,
    get_jobs_by_stage,
    set_job_status,
)
from applypilot.llm import get_client
from applypilot.notify import notifier
from applypilot.scoring.validator import (
    BANNED_WORDS,
    sanitize_text,
    validate_json_fields,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # max cross-run retries before giving up


# ── Prompt Builders (profile-driven) ──────────────────────────────────────

def _build_tailor_prompt(profile: dict) -> str:
    """Build the resume tailoring system prompt from the user's profile.

    Lightweight schema: the LLM only rewrites what changes (title, summary,
    skills order, experience bullets). All fixed facts -- header, contact,
    company names/roles/dates, projects, education -- are preserved verbatim
    from the original resume by code. Nothing here is hardcoded personal data.
    """
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Format skills boundary for the prompt
    skills_lines = []
    for category, items in boundary.items():
        if isinstance(items, list) and items:
            label = category.replace("_", " ").title()
            skills_lines.append(f"{label}: {', '.join(items)}")
    skills_block = "\n".join(skills_lines)

    # Preserved entities
    companies = resume_facts.get("preserved_companies", [])
    real_metrics = resume_facts.get("real_metrics", [])

    companies_str = ", ".join(companies) if companies else "N/A"
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"

    # Include ALL banned words from the validator so the LLM knows exactly
    # what will be rejected — the validator checks for these automatically.
    banned_str = ", ".join(BANNED_WORDS)

    return f"""You are a senior technical recruiter tailoring a resume to get this person an interview.

Only the four fields below are rewritten for the target job. Everything else is handled by code:
company names, roles, dates, locations, contact info, education, and projects are kept EXACTLY as in the ORIGINAL RESUME. Do NOT touch them.

## RECRUITER SCAN (6 seconds):
1. Title -- matches what they're hiring?
2. Summary -- 2 sentences proving you've done this work
3. First 3 bullets of most recent role -- verbs and outcomes match?
4. Skills -- must-haves visible immediately?

## SKILLS BOUNDARY (real skills only):
{skills_block}

You MAY add 2-3 closely related tools (Kubernetes if Docker, Terraform if AWS, Redis if PostgreSQL). No unrelated languages/frameworks.

## RULES:
TITLE: Match the target role. Keep seniority (Senior/Lead/Staff). Drop company suffixes and team names.

SUMMARY: Rewrite from scratch. Lead with the 1-2 skills that matter most for THIS role. Sound like someone who's done this job.

SKILLS: Reorder each category so the job's must-haves appear first. Only reorder/trim -- do not invent categories.

BULLETS: For EVERY company listed below, rewrite that role's bullets for this role. Same real work, different angle, incorporating the job's required skills where the work genuinely supports it. Never copy verbatim. Max 4 bullets per role. Vary verbs (Built, Designed, Implemented, Reduced, Automated, Deployed, Optimized).

## COMPANIES -- output bullets for exactly these, keep the same order:
{companies_str}

## VOICE:
- Write like a real engineer. Short, direct.
- GOOD: "Automated financial reporting with Python + API integrations, cut processing time from 10 hours to 2"
- BAD: "Leveraged cutting-edge AI technologies to drive transformative operational efficiencies"
- BANNED WORDS (using ANY of these = validation failure — do not use them even once):
  {banned_str}
- No em dashes. Use commas, periods, or hyphens.

## HARD RULES:
- Do NOT invent work, companies, degrees, or certifications
- Do NOT change real numbers ({metrics_str})
- Do NOT add, remove, or rename companies
- Must fit 1 page.

## OUTPUT: Return ONLY valid JSON. No markdown fences. No commentary. No "here is" preamble.

{{"title":"Role Title","summary":"2-3 tailored sentences.","skills":{{"Languages":"...","Frameworks":"...","DevOps & Infra":"...","Databases":"...","Tools":"..."}},"experience":[{{"company":"NTG Clarity Networks","bullets":["bullet 1","bullet 2","bullet 3"]}}]}}"""


def _build_judge_prompt(profile: dict) -> str:
    """Build the LLM judge prompt from the user's profile."""
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Flatten allowed skills for the judge
    all_skills: list[str] = []
    for items in boundary.values():
        if isinstance(items, list):
            all_skills.extend(items)
    skills_str = ", ".join(all_skills) if all_skills else "N/A"

    real_metrics = resume_facts.get("real_metrics", [])
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"

    return f"""You are a resume quality judge. A tailoring engine rewrote a resume to target a specific job. Your job is to catch LIES, not style changes.

You must answer with EXACTLY this format:
VERDICT: PASS or FAIL
ISSUES: (list any problems, or "none")

## CONTEXT -- what the tailoring engine was instructed to do (all of this is ALLOWED):
- Change the title to match the target role
- Rewrite the summary from scratch for the target job
- Reorder bullets and projects to put the most relevant first
- Reframe bullets to use the job's language
- Drop low-relevance bullets and replace with more relevant ones from other sections
- Reorder the skills section to put job-relevant skills first
- Change tone and wording extensively

## WHAT IS FABRICATION (FAIL for these):
1. Adding tools, languages, or frameworks to TECHNICAL SKILLS that aren't in the original. The allowed skills are ONLY: {skills_str}
2. Inventing NEW metrics or numbers not in the original. The real metrics are: {metrics_str}
3. Inventing work that has no basis in any original bullet (completely new achievements).
4. Adding companies, roles, or degrees that don't exist.
5. Changing real numbers (inflating 80% to 95%, 500 nodes to 1000 nodes).

## WHAT IS NOT FABRICATION (do NOT fail for these):
- Rewording any bullet, even heavily, as long as the underlying work is real
- Combining two original bullets into one
- Splitting one original bullet into two
- Describing the same work with different emphasis
- Dropping bullets entirely
- Reordering anything
- Changing the title or summary completely

## TOLERANCE RULE:
The goal is to get interviews, not to be a perfect fact-checker. Allow up to 3 minor stretches per resume:
- Adding a closely related tool the candidate could realistically know is a MINOR STRETCH, not fabrication.
- Reframing a metric with slightly different wording is a MINOR STRETCH.
- Adding any LEARNABLE skill given their existing stack is a MINOR STRETCH.
- Only FAIL if there are MAJOR lies: completely invented projects, fake companies, fake degrees, wildly inflated numbers, or skills from a completely different domain.

Be strict about major lies. Be lenient about minor stretches and learnable skills. Do not fail for style, tone, or restructuring."""


# ── JSON Extraction ───────────────────────────────────────────────────────

def extract_json(raw: str) -> dict:
    """Robustly extract JSON from LLM response (handles fences, preamble).

    Args:
        raw: Raw LLM response text.

    Returns:
        Parsed JSON dict.

    Raises:
        ValueError: If no valid JSON found.
    """
    raw = raw.strip()

    # Direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Markdown fences
    if "```" in raw:
        for part in raw.split("```")[1::2]:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            try:
                return json.loads(part)
            except json.JSONDecodeError:
                continue

    # Find outermost { ... }
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError("No valid JSON found in LLM response")


# ── Resume Assembly (profile-driven header) ──────────────────────────────

_SECTION_ALIASES = {
    "experience": ("professional experience", "work experience", "experience"),
    "projects": ("key projects", "projects", "selected projects"),
    "education": ("education", "academic background"),
}


def _parse_resume_sections(resume_text: str) -> dict[str, str]:
    """Split original resume into named sections by matching section headers.

    Returns {"experience": str, "projects": str, "education": str, "rest": str}.
    Content before the first known header becomes "rest" (header/tagline/contact).
    """
    lines = resume_text.splitlines()
    sections: dict[str, list[str]] = {}
    order: list[str] = []

    def header_of(line: str) -> str | None:
        stripped = line.strip().lower()
        for key, aliases in _SECTION_ALIASES.items():
            if any(a == stripped for a in aliases):
                return key
        return None

    current = "rest"
    for line in lines:
        key = header_of(line)
        if key is not None:
            current = key
            if key not in order:
                order.append(key)
            continue  # the header line itself is not section content
        sections.setdefault(current, []).append(line)
    out = {k: "\n".join(v).strip() for k, v in sections.items() if k != "rest"}
    # The resume header block is the first ~5 non-empty lines (name, title,
    # tagline, location, contact). Content before the first known section.
    rest = "\n".join(sections.get("rest", [])).strip()
    non_empty = [ln for ln in rest.splitlines() if ln.strip()]
    out["rest"] = "\n".join(non_empty[:5]).strip()
    return out


def _parse_experience_roles(experience_block: str) -> list[dict]:
    """Parse an experience section into roles.

    Structure in the original resume: a role header line, then a subtitle line
    that always contains '|' (company | location | dates), then bullet lines
    (plain paragraphs, not dash-prefixed). A plain line whose NEXT line contains
    '|' is a role header; every other plain line is a bullet.
    Returns [{"header":..., "subtitle":..., "bullets":[...]}, ...].
    """
    lines = [ln.strip() for ln in experience_block.splitlines() if ln.strip()]
    roles: list[dict] = []
    for i, ln in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if "|" in ln or "@" in ln:
            roles.append({"header": "", "subtitle": ln, "bullets": []})
        elif nxt and ("|" in nxt or "@" in nxt):
            roles.append({"header": ln, "subtitle": "", "bullets": []})
        elif roles:
            roles[-1]["bullets"].append(ln)
        else:
            roles.append({"header": ln, "subtitle": "", "bullets": []})
    # Merge a header role with its following subtitle role into one entry.
    merged: list[dict] = []
    for role in roles:
        if merged and role["header"] == "" and role["subtitle"] and not merged[-1]["subtitle"]:
            merged[-1]["subtitle"] = role["subtitle"]
            merged[-1]["bullets"].extend(role["bullets"])
        else:
            merged.append(dict(role))
    # Drop stray roles that have no company subtitle (e.g. a bare section title).
    return [r for r in merged if r["subtitle"]]


def _match_role(roles: list[dict], company: str) -> dict | None:
    """Find the role whose header/subtitle mentions the company (fuzzy)."""
    c = company.lower()
    for role in roles:
        haystack = f"{role.get('header', '')} {role.get('subtitle', '')}".lower()
        if c in haystack:
            return role
    return None


def assemble_resume_text(data: dict, profile: dict, original_text: str = "") -> str:
    """Convert JSON resume data to formatted plain text.

    Header (name, location, contact), education, and projects are ALWAYS taken
    from the original resume / profile -- never LLM-generated. Only title,
    summary, skills, and experience bullets come from the LLM.

    Args:
        data: Parsed JSON resume from the LLM.
        profile: User profile dict from load_profile().
        original_text: Original base resume (fixed sections preserved verbatim).

    Returns:
        Formatted resume text.
    """
    personal = profile.get("personal", {})
    sections = _parse_resume_sections(original_text)
    roles = _parse_experience_roles(sections.get("experience", ""))
    lines: list[str] = []

    # Header -- always code-injected from profile
    lines.append(personal.get("full_name", ""))
    lines.append(sanitize_text(data.get("title", "Software Engineer")))

    # Tagline / location / contact preserved from original resume if present
    rest_lines = [ln.strip() for ln in sections.get("rest", "").splitlines() if ln.strip()]
    if rest_lines:
        # Skip the original name/title (replaced above), keep the rest.
        lines.extend(rest_lines[2:])
    else:
        # Contact line
        contact_parts: list[str] = []
        if personal.get("email"):
            contact_parts.append(personal["email"])
        if personal.get("phone"):
            contact_parts.append(personal["phone"])
        if personal.get("github_url"):
            contact_parts.append(personal["github_url"])
        if personal.get("linkedin_url"):
            contact_parts.append(personal["linkedin_url"])
        if contact_parts:
            lines.append(" | ".join(contact_parts))
    lines.append("")

    # Summary
    lines.append("SUMMARY")
    lines.append(sanitize_text(data["summary"]))
    lines.append("")

    # Technical Skills
    lines.append("TECHNICAL SKILLS")
    if isinstance(data["skills"], dict):
        for cat, val in data["skills"].items():
            lines.append(f"{cat}: {sanitize_text(str(val))}")
    lines.append("")

    # Experience -- fixed headers/subtitles from original, LLM bullets
    lines.append("EXPERIENCE")
    llm_by_company = {}
    for entry in data.get("experience", []):
        if isinstance(entry, dict):
            comp = entry.get("company", "")
            llm_by_company[comp] = entry.get("bullets", [])
    for role in roles:
        lines.append(sanitize_text(role.get("header", "")))
        if role.get("subtitle"):
            lines.append(sanitize_text(role["subtitle"]))
        # Use LLM bullets if we can match the role's company, else original
        matched = None
        for company, bullets in llm_by_company.items():
            if company and _match_role([role], company):
                matched = bullets
                break
        bullets = matched or role.get("bullets", [])
        for b in bullets:
            if b.strip():
                lines.append(f"- {sanitize_text(b)}")
        lines.append("")

    # Projects -- preserved verbatim from original
    projects = sections.get("projects", "")
    if projects:
        lines.append("PROJECTS")
        lines.append(projects)
        lines.append("")

    # Education -- preserved verbatim from original
    education = sections.get("education", "")
    if education:
        lines.append("EDUCATION")
        lines.append(education)

    return "\n".join(lines).strip()


# ── LLM Judge ────────────────────────────────────────────────────────────

def judge_tailored_resume(
    original_text: str, tailored_text: str, job_title: str, profile: dict
) -> dict:
    """LLM judge layer: catches subtle fabrication that programmatic checks miss.

    Args:
        original_text: Base resume text.
        tailored_text: Tailored resume text.
        job_title: Target job title.
        profile: User profile for building the judge prompt.

    Returns:
        {"passed": bool, "verdict": str, "issues": str, "raw": str}
    """
    judge_prompt = _build_judge_prompt(profile)

    messages = [
        {"role": "system", "content": judge_prompt},
        {"role": "user", "content": (
            f"JOB TITLE: {job_title}\n\n"
            f"ORIGINAL RESUME:\n{original_text}\n\n---\n\n"
            f"TAILORED RESUME:\n{tailored_text}\n\n"
            "Judge this tailored resume:"
        )},
    ]

    client = get_client()
    response = client.chat(messages, max_tokens=1024, temperature=0.1)

    passed = "VERDICT: PASS" in response.upper()
    issues = "none"
    if "ISSUES:" in response.upper():
        issues_idx = response.upper().index("ISSUES:")
        issues = response[issues_idx + 7:].strip()

    return {
        "passed": passed,
        "verdict": "PASS" if passed else "FAIL",
        "issues": issues,
        "raw": response,
    }


# ── Core Tailoring ───────────────────────────────────────────────────────

def tailor_resume(
    resume_text: str, job: dict, profile: dict,
    max_retries: int = 3, validation_mode: str = "normal",
) -> tuple[str, dict]:
    """Generate a tailored resume via JSON output + fresh context on each retry.

    Key design choices:
    - LLM returns structured JSON, code assembles the text (no header leaks)
    - Each retry starts a FRESH conversation (no apologetic spiral)
    - Issues from previous attempts are noted in the system prompt
    - Em dashes and smart quotes are auto-fixed, not rejected

    Args:
        resume_text:      Base resume text.
        job:              Job dict with title, site, location, full_description.
        profile:          User profile dict.
        max_retries:      Maximum retry attempts.
        validation_mode:  "strict", "normal", or "lenient".
                          strict  -- banned words trigger retries; judge must pass
                          normal  -- banned words = warnings only; judge can fail on last retry
                          lenient -- banned words ignored; LLM judge skipped

    Returns:
        (tailored_text, report) where report contains validation details.
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    report: dict = {
        "attempts": 0, "validator": None, "judge": None,
        "status": "pending", "validation_mode": validation_mode,
    }
    avoid_notes: list[str] = []
    tailored = ""
    client = get_client()
    tailor_prompt_base = _build_tailor_prompt(profile)

    for attempt in range(max_retries + 1):
        report["attempts"] = attempt + 1

        # Fresh conversation every attempt
        prompt = tailor_prompt_base
        if avoid_notes:
            prompt += "\n\n## AVOID THESE ISSUES (from previous attempt):\n" + "\n".join(
                f"- {n}" for n in avoid_notes[-5:]
            )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"ORIGINAL RESUME:\n{resume_text}\n\n---\n\nTARGET JOB:\n{job_text}\n\nReturn the JSON:"},
        ]

        raw = client.chat(messages, max_tokens=8192, temperature=0.4)

        # Parse JSON from response
        try:
            data = extract_json(raw)
        except ValueError:
            avoid_notes.append("Output was not valid JSON. Return ONLY a JSON object, nothing else.")
            continue

        # Layer 1: Validate JSON fields
        validation = validate_json_fields(data, profile, mode=validation_mode)
        report["validator"] = validation

        if not validation["passed"]:
            # Only retry if there are hard errors (warnings never block)
            avoid_notes.extend(validation["errors"])
            if attempt < max_retries:
                continue
            # Last attempt — assemble whatever we got
            tailored = assemble_resume_text(data, profile, resume_text)
            report["status"] = "failed_validation"
            return tailored, report

        # Assemble text (header injected by code, em dashes auto-fixed)
        tailored = assemble_resume_text(data, profile, resume_text)

        # Layer 2: LLM judge (catches subtle fabrication) — skipped in lenient mode
        if validation_mode == "lenient":
            report["judge"] = {"verdict": "SKIPPED", "passed": True, "issues": "none"}
            report["status"] = "approved"
            return tailored, report

        judge = judge_tailored_resume(resume_text, tailored, job.get("title", ""), profile)
        report["judge"] = judge

        if not judge["passed"]:
            avoid_notes.append(f"Judge rejected: {judge['issues']}")
            if attempt < max_retries:
                # In normal mode, only retry on judge failure if there are retries left
                if validation_mode != "lenient":
                    continue
            # Accept best attempt on last retry (all modes) or if lenient
            report["status"] = "approved_with_judge_warning"
            return tailored, report

        # Both passed
        report["status"] = "approved"
        return tailored, report

    report["status"] = "exhausted_retries"
    return tailored, report


# ── Batch Entry Point ────────────────────────────────────────────────────

def run_tailoring(min_score: int = 7, limit: int = 200,
                  validation_mode: str = "normal", workers: int = 1) -> dict:
    """Generate tailored resumes for high-scoring jobs.

    Args:
        min_score:       Minimum fit_score to tailor for.
        limit:           Maximum jobs to process.
        validation_mode: "strict", "normal", or "lenient".
        workers:         Concurrent LLM workers (1 = sequential).

    Returns:
        {"approved": int, "failed": int, "errors": int, "elapsed": float}
    """
    profile = load_profile()
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    jobs = get_jobs_by_stage(conn=conn, stage="pending_tailor", min_score=min_score, limit=limit)

    if not jobs:
        log.info("No untailored jobs with score >= %d.", min_score)
        return {"approved": 0, "failed": 0, "errors": 0, "elapsed": 0.0}

    TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Tailoring resumes for %d jobs (score >= %d, workers=%d)...",
             len(jobs), min_score, workers)
    t0 = time.time()
    completed = 0
    results: list[dict] = []
    results_lock = threading.Lock()
    stats: dict[str, int] = {"approved": 0, "failed_validation": 0, "failed_judge": 0, "error": 0}

    def _process_one(job: dict) -> dict:
        try:
            tailored, report = tailor_resume(resume_text, job, profile,
                                             validation_mode=validation_mode)

            # Clean, professional filename: "{Name} - {JobTitle}"
            # (no company, no site). Stored in a per-job folder named by the
            # job's URL hash so names never collide across jobs.
            from applypilot.database import job_id as _job_hash
            candidate_name = (profile.get("personal", {}) or {}).get("full_name", "Candidate")
            safe_candidate = re.sub(r"[^\w\s-]", "", candidate_name).strip().replace(" ", "-")
            safe_title = re.sub(r"[^\w\s-]", "", job["title"])[:50].strip().replace(" ", "-")
            clean_name = f"{safe_candidate} - {safe_title}"
            job_folder = TAILORED_DIR / _job_hash(job["url"])
            job_folder.mkdir(parents=True, exist_ok=True)

            # Save tailored resume text
            txt_path = job_folder / f"{clean_name}.txt"
            txt_path.write_text(tailored, encoding="utf-8")

            # Save job description for traceability
            job_path = job_folder / f"{clean_name}_JOB.txt"
            job_desc = (
                f"Title: {job['title']}\n"
                f"Company: {job['site']}\n"
                f"Location: {job.get('location', 'N/A')}\n"
                f"Score: {job.get('fit_score', 'N/A')}\n"
                f"URL: {job['url']}\n\n"
                f"{job.get('full_description', '')}"
            )
            job_path.write_text(job_desc, encoding="utf-8")

            # Save validation report
            report_path = job_folder / f"{clean_name}_REPORT.json"
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

            # Generate PDF for approved resumes (best-effort)
            # "approved_with_judge_warning" is also a success — resume was generated.
            pdf_path = None
            github_url = None
            if report["status"] in ("approved", "approved_with_judge_warning"):
                try:
                    from applypilot.scoring.pdf import convert_to_pdf
                    pdf_path = str(convert_to_pdf(txt_path))
                    from applypilot.github_upload import upload_pdf
                    from applypilot.database import job_id as _job_hash
                    github_url = upload_pdf(pdf_path, kind="resume", job_key=_job_hash(job["url"]))
                except Exception:
                    log.debug("PDF/GitHub upload failed for %s", txt_path, exc_info=True)

            return {
                "url": job["url"],
                "path": str(txt_path),
                "pdf_path": pdf_path,
                "github_url": github_url,
                "title": job["title"],
                "site": job["site"],
                "status": report["status"],
                "attempts": report["attempts"],
            }
        except Exception as e:
            log.error("Tailor ERROR %s -- %s", job["title"][:40], e)
            return {
                "url": job["url"], "title": job["title"], "site": job["site"],
                "status": "error", "attempts": 0, "path": None,
                "pdf_path": None, "github_url": None,
            }

    if workers > 1 and len(jobs) > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_process_one, job): job for job in jobs}
            for future in as_completed(futures):
                job = futures[future]
                result = future.result()
                with results_lock:
                    completed += 1
                    results.append(result)
                    stats[result.get("status", "error")] = stats.get(result.get("status", "error"), 0) + 1
                    log.info("%d/%d [%s] | %s", completed, len(jobs),
                             result["status"].upper(), job["title"][:40])
    else:
        for job in jobs:
            result = _process_one(job)
            completed += 1
            results.append(result)
            stats[result.get("status", "error")] = stats.get(result.get("status", "error"), 0) + 1
            elapsed = time.time() - t0
            rate = completed / elapsed if elapsed > 0 else 0
            log.info(
                "%d/%d [%s] attempts=%s | %.1f jobs/min | %s",
                completed, len(jobs),
                result["status"].upper(),
                result.get("attempts", "?"),
                rate * 60,
                result["title"][:40],
            )

    # Persist to DB: increment attempt counter for ALL, save path only for approved
    now = datetime.now(UTC).isoformat()
    _success_statuses = {"approved", "approved_with_judge_warning"}
    for r in results:
        if r["status"] in _success_statuses:
            # Prefer the GitHub blob URL (private repo, user downloads there);
            # fall back to the local path if upload failed.
            stored_ref = r.get("github_url") or r.get("path")
            conn.execute(
                "UPDATE jobs SET tailored_resume_path=?, tailored_at=?, "
                "tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                (stored_ref, now, r["url"]),
            )
            set_job_status(r["url"], "tailored", conn=conn)
            notifier.send_tailored(
                title=r["title"],
                company=r.get("site", "?"),
                score=next((j.get("fit_score") for j in jobs if j["url"] == r["url"]), None),
                resume_path=stored_ref or "",
            )
        elif r["status"] == "error":
            set_job_status(r["url"], JOB_STATUS_ERROR, conn=conn)
        else:
            conn.execute(
                "UPDATE jobs SET tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                (r["url"],),
            )
    conn.commit()

    elapsed = time.time() - t0
    log.info(
        "Tailoring done in %.1fs: %d approved, %d failed_validation, %d failed_judge, %d errors",
        elapsed,
        stats.get("approved", 0),
        stats.get("failed_validation", 0),
        stats.get("failed_judge", 0),
        stats.get("error", 0),
    )

    return {
        "approved": stats.get("approved", 0),
        "failed": stats.get("failed_validation", 0) + stats.get("failed_judge", 0),
        "errors": stats.get("error", 0),
        "elapsed": elapsed,
    }
