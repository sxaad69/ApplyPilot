# Apply Layer Plan — Hermes Agent + DeepSeek V4 Flash

## 1. Why this stack

| Component | Choice | Cost |
|---|---|---|
| Agent harness | **Hermes Agent** (Nous Research, MIT) | Free, open source |
| LLM | **DeepSeek V4 Flash** via OpenCode Go | ~$0.14/M in, $0.28/M out; included in $10/mo Go plan |
| Browser control | **Browser Use** cloud MCP (via Nous Portal / Tool Gateway) or local Playwright | Portal subscription covers it |
| Orchestration | ApplyPilot pipeline (existing DB, statuses, PDFs) | Already built |

**vs. the current layer:** Claude Code (`claude --model sonnet`) driving Chrome via Playwright MCP —
locked to Anthropic, ~10-20× per-token cost, no memory/skill reuse across applications.

**Why it wins here:**
- **Cost:** the apply layer is the most token-hungry stage (long agent sessions, browser context,
  many tool calls). DeepSeek Flash makes each application cents instead of dollars.
- **No lock-in:** Hermes is model-agnostic (`hermes model`); swap to anything later.
- **Learning loop:** Hermes can build a reusable "job application form-filling" skill that improves
  across applications — Claude Code re-learns every fresh session.
- **Cron + messaging:** schedule "check & apply" and get Telegram reports, unattended.

## 2. Architecture

```
ApplyPilot DB (jobs with fit_score >= 7, tailored resume, cover letter, application_url)
        │
        ▼
  apply-stage launcher (Python)          ← adapts existing apply/launcher.py
        │  for each qualifying job:
        │    - reads job, tailored resume, cover letter, application_url
        │    - writes a Hermes skill/context bundle (form fields known from job desc)
        │    - invokes Hermes in headless mode
        ▼
  Hermes Agent (DeepSeek V4 Flash)
        │  - opens application_url in browser (Browser Use MCP)
        │  - fills form using profile + tailored resume + cover letter
        │  - screenshots / validates each step
        │  - reports outcome (applied / blocked / needs_manual)
        ▼
  ApplyPilot DB (apply_status, applied_at, apply_error)
        │
        ▼
  Telegram notification (existing notifier)
```

## 3. Phased rollout (safety-first)

### Phase 0 — Spike (half a day)
- Install Hermes Agent locally; point `hermes model` at OpenCode Go / DeepSeek V4 Flash.
- Configure a **browser MCP** (Browser Use via Nous Portal, or local Playwright MCP).
- Manually run ONE application end-to-end from the CLI. Watch it live. Verdict: does it fill
  a real form correctly? (This is the gating question — if the harness botches simple forms,
  stop and reconsider.)

### Phase 1 — Hermes as the executor behind the existing launcher
- Rewrite `apply/launcher.py`'s Claude Code subprocess call → Hermes headless call
  (`hermes run` / headless mode with a bounded instruction).
- Keep everything else (DB statuses, worker threads, dashboard, blocked-site list) unchanged.
- Apply to a **trial batch of 2-3** high-fit jobs only (scores ≥ 8 first). Review each result
  manually before touching real volume.

### Phase 2 — Teach the skill
- Capture what worked into a Hermes **skill** ("apply-to-job"): form-field mapping, profile
  snippets, resume/cover attachment, post-submit verification.
- Skill self-improves on feedback; future applications reuse it instead of re-learning.

### Phase 3 — Scale + automation
- Run apply on all jobs with `fit_score >= 8`, then ≥ 7.
- Add **cron** (Hermes built-in) for recurring discovery→tailor→apply cycles.
- Add **Telegram gateway** for apply reports (already supported by Hermes + ApplyPilot notifier).

## 4. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Hermes botches a real form | Phase 0 gate; trial batch of 2-3 first; keep `dry_run` available |
| Browser MCP cost / setup friction | Nous Portal Tool Gateway (one sub); or local Playwright MCP for testing |
| Harmless-but-wrong field (e.g. wrong date) | Screenshot-after-fill validation step in the skill; human review in Phases 0-1 |
| ATS/anti-bot blocks the browser | Same blocked-site list already in ApplyPilot; flag `needs_manual` instead of retrying |
| DeepSeek agent reliability vs Claude | Hermes harness handles the loop; if it underperforms, swap model only (`hermes model`) |

## 5. Success criteria
- 1 clean end-to-end application in Phase 0.
- 2-3 correct applications in Phase 1 (human-verified).
- ≥80% of ≥8-score jobs applied without manual intervention in Phase 3.

## 6. Explicitly deferred
- Auto-fill of employer-side questionnaires (capcha-like) — flag `needs_manual`.
- Legal review of ToS for automated applying — user's call.
- Multi-country ATS variations — handle per-site via the skill over time.
