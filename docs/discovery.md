# Job Discovery — Source Inventory & Status

This document is the complete, authoritative inventory of every job-discovery source
in ApplyPilot: what it is, whether it works, how it's wired, what failed, and what
has never been tested. It was written from a live read of the codebase and a real
discovery run on 2026-08-08.

> **Status legend**
> - ✅ WORKING — produced jobs in a real run / proven path
> - ⚠️ UNPROVEN — configured but never successfully exercised end-to-end
> - ❌ FAILED — known broken or blocked
> - 🔧 CONFIG MISMATCH — config file keys don't match what the code reads
> - 💤 DORMANT — code exists but is disabled / not reachable

---

## 1. Discovery entry point & how sources stack

`applypilot run discover` → `pipeline._run_discover()` runs **four sub-sources in
sequence** today — the four sub-sources now run **concurrently** (thread-per-source)
via `pipeline._run_discover()`:

```
1. JobSpy full crawl      (discovery/jobspy.py)   -- also internally threaded (workers)
2. Workday corporate      (discovery/workday.py)  -- threaded per-employer
3. Free API sources       (fetchers_api.py)       -- Arbeitnow, RemoteOK
4. SmartExtract (AI)      (discovery/smartextract.py)  -- DISABLED (2026-08-08)
```

**SmartExtract is DISABLED** (2026-08-08): it scraped custom sites from a sites.yaml
that doesn't exist → found 0 jobs, wasted LLM tokens on its judge filter, and hit a
429 rate-limit stall (bad Retry-After header parsed as ~7h sleep — fixed in llm.py by
capping retry wait at 60s). Re-enable only if a sites.yaml is ever created.

Everything is driven by **`~/.applypilot/searches.yaml`** (via `config.load_search_config()`).

---

## 2. JobSpy (main engine) — `discovery/jobspy.py`

Scrapes job boards via the `jobspy` PyPI library (`.venv/lib/python3.11/site-packages/jobspy/`).

### Source matrix (JobSpy library scrapers present)

> **Verification probes run 2026-08-08** — every board tested with `results_wanted=5`.
> All non-working sources were **commented out** in searches.yaml `boards:` (code kept
> intact, re-enable later to re-test). See §9.

| Board | Site key | Status | Evidence / notes |
|---|---|---|---|
| LinkedIn | `linkedin` | ✅ WORKING | **56 jobs** in DB. Probe: 5/5. Slow (~2 min/query). Uses `linkedin_fetch_description=True` to get descriptions during scrape (LinkedIn pages are login-walled for the enrich stage). |
| Indeed | `indeed` | ✅ WORKING | **33 jobs** in DB. Probe: 5/5. Needs per-location `country_indeed` for non-US global search (see §8, currently a single `country: USA`). |
| ZipRecruiter | `zip_recruiter` | ❌ FAILED | **HTTP 403 forbidden** every request — Cloudflare anti-bot (`{"error_code":"forbidden aa",...}` + `CFRAY`). **Commented out** in boards 2026-08-08. |
| Glassdoor | `glassdoor` | ❌ FAILED | **Not a location problem.** Location lookup `findPopularLocationAjax.htm` → 400 for every string (SF/Berlin/London/Dubai/Remote/US); hardcoded remote-location ID bypass → 403 on the GraphQL job search. Anti-bot blocks the scraper entirely. **Commented out.** Only path = residential proxy or newer scraper. |
| Google | `google` | ❌ FAILED | Probe: **0 jobs** (returns nothing, no error). **Commented out.** |
| Bayt | `bayt` | ❌ FAILED | Probe: **403 Forbidden** on `bayt.com/en/international/...` for all locations incl. Dubai/Riyadh. Anti-bot. MENA source — **commented out**; re-test with proxy later. |
| Naukri | `naukri` | ❌ FAILED | Probe: **406 recaptcha required**. **Commented out.** |
| BDJobs | `bdjobs` | ❌ FAILED | Probe: library bug — `BDJobs.__init__() got an unexpected keyword argument 'user_agent'`. **Commented out.** |
| Bayt/Naukri/BDJobs scrapers | — | 💤 DORMANT | Code still present in jobspy lib; disabled in config. |

### JobSpy runtime behavior observed (2026-08-08 run)

- **46 search combinations** (23 queries × 2 locations) at 100 results/site, 72h window.
- LinkedIn finished slowly; **~1-2 min per query** with description fetch enabled.
- ZipRecruiter errored on **every** query (403).
- **Remote-location crash bug:** when `is_remote=True`, JobSpy's `Country.from_string()`
  raised `ValueError: Invalid country string: 'monaco'/'ghana'/'bosnia and herzegovina'`
  on remote listings, killing the ENTIRE query even when LinkedIn succeeded.
  → **FIXED (2026-08-08):** patched `.venv/.../jobspy/model.py` `Country.from_string` to
  return `None` instead of raising. NOTE: this is a **venv patch** — it will be lost on
  reinstall. Should be re-applied or vendored.
- `_full_crawl` runs searches **sequentially** (`for s in searches:` at jobspy.py:418).

### Config keys JobSpy actually reads

| Code reads | Where | Current searches.yaml value | Match? |
|---|---|---|---|
| `search_cfg.get("queries")` | _full_crawl | `queries:` (23 items) | ✅ |
| `search_cfg.get("locations")` | _full_crawl | `locations:` (2 items) | ✅ (but see `location_labels` below) |
| `cfg.get("sites")` | run_discovery | — | 🔧 **MISMATCH — yaml uses `boards:` not `sites:`** |
| `cfg.get("location_labels")` | run_discovery | — | 🔧 **MISMATCH — yaml has `locations:` but no `location_labels:`** |
| `cfg.get("tiers")` | run_discovery | — | 🔧 not present |
| `search_cfg.get("location_accept")` | _load_location_config | — | 🔧 **MISMATCH — yaml uses `location.accept_patterns`** |
| `search_cfg.get("location_reject_non_remote")` | _load_location_config | — | 🔧 **MISMATCH — yaml uses `location.reject_patterns`** |
| `search_cfg.get("glassdoor_location_map")` | _full_crawl | — | 🔧 not present |
| `defaults.results_per_site` | run_discovery | 100 | ✅ |
| `defaults.hours_old` | run_discovery | 72 | ✅ |
| `defaults.country_indeed` | _run_one_search | — (yaml has top-level `country: "USA"`) | 🔧 **MISMATCH — country is top-level, not under defaults** |

> **CONSEQUENCE OF THE MISMATCH (important):** because `run_discovery` reads `sites`,
> `location_labels`, and `tiers` (which don't exist in searches.yaml), the run fell back
> to JobSpy **defaults**: `sites = ["indeed", "linkedin", "zip_recruiter"]` (NOT the
> `boards:` list which included glassdoor/google), and used default locations. The
> `boards:`, `location.*`, and `country:` settings in searches.yaml were **partially
> ignored**. The `zip_recruiter` removal we made in `boards:` did NOT actually affect the
> JobSpy run because `boards:` isn't read — the code defaulted to its own site list.

---

## 3. Free API sources — `fetchers_api.py`

Pure HTTP JSON/RSS fetchers, no scraping. All normalized and deduped by URL hash in DB.

| Source | Function | Auth | Status | Notes |
|---|---|---|---|---|
| Arbeitnow | `fetch_arbeitnow` | none | ✅ WORKING | **5 jobs in DB.** Germany/EU focus. No key. |
| RemoteOK | `fetch_remoteok` | none | ✅ WORKING | **0 jobs stored** but fetcher proven (returns remote dev jobs; empty on this run). No key. |
| Adzuna | `fetch_adzuna` | `ADZUNA_APP_ID` + `ADZUNA_APP_KEY` | ⚠️ UNPROVEN | **Keys empty in .env.** Multi-country (`ADZUNA_COUNTRY`, default `us`). Free tier. Loops `_search_queries()[:3]`. |
| USAJobs | `fetch_usajobs` | `USAJOBS_API_KEY` | ⚠️ UNPROVEN | **Key empty in .env.** US-only, free. Requires `Host` + custom `User-Agent` + `Authorization-Key` headers. |
| The Muse | `fetch_the_muse` | `THE_MUSE_API_KEY` | ⚠️ UNPROVEN | **Key empty in .env.** US/EU, free tier. |
| RSS/Atom | `fetch_rss` | none | ⚠️ UNPROVEN | Parses RSS 2.0 + Atom. Feeds from `RSS_FEEDS` env or `searches.yaml api_sources.rss` (⚠️ that key does NOT exist in current searches.yaml). |

### `_all_fetchers()` logic
- Arbeitnow + RemoteOK **always** run.
- Adzuna/USAJobs/Muse only added if their env keys are non-empty.
- RSS fetchers added per feed URL.
- `MAX_PER_SOURCE = 200` jobs cap per source per run.

### Known gaps / quirks (API path)
- Adzuna has no MENA (no UAE/KSA). Country loop must be added for real global coverage.
- No global-remote filter except RemoteOK.
- RSS feeds are the only free way to reach MENA (GulfTalent, Bayt RSS, etc.) and are not configured.

---

## 4. Workday corporate scraper — `discovery/workday.py`

| Item | Detail |
|---|---|
| Status | ⚠️ UNPROVEN this run (killed before reaching it) |
| What | Scrapes Workday-hosted employer career portals (many large corps use Workday) |
| Config | Reads `search_cfg.get("queries")`; defaults to tier 1-2 queries |
| workers | Accepts `workers` param for parallel site scraping |
| Caveat | Each Workday employer needs a **specific URL/tenant**; if none configured it finds nothing. No employer list is configured. |

---

## 5. SmartExtract (AI-powered scraping) — `discovery/smartextract.py`

| Item | Detail |
|---|---|
| Status | 🚫 **DISABLED 2026-08-08** |
| What | AI-driven extraction of job data from custom target sites (`build_scrape_targets`) |
| Why disabled | Requires `sites.yaml` (`load_sites()`) — **none exists**, so it found 0 targets; wasted LLM tokens on its judge filter; hit a 429 rate-limit stall (~7h from bad Retry-After header, since fixed) |
| LLM usage | **YES** — uses `client.ask()` at smartextract.py:406 and :655 (this is what caused the 429) |
| Re-enable | Only if a `sites.yaml` is ever created; code kept intact in pipeline.py (commented out) |

---

## 6. Enrichment stage (descriptions) — `enrichment/detail.py`

Not a "source," but needed to understand where descriptions come from.

- Three-tier extraction per job: deterministic → JS/browser → **LLM** (`extract_with_llm`).
- Used for jobs discovered WITHOUT a description (e.g. Indeed/Glassdoor via JobSpy).
- **LinkedIn jobs get descriptions during discovery** (`linkedin_fetch_description`), NOT here — because LinkedIn URLs are login-walled.
- Tier 3 (LLM) is a fallback and costs tokens.

---

## 7. Proven DB counts after 2026-08-08 discovery run (killed at 10/46 queries)

| Source | Count |
|---|---|
| LinkedIn | 56 |
| Indeed | 33 |
| Arbeitnow | 5 |
| **Total** | **94** (89 new during run, 5 pre-existing) |

---

## 8. Global coverage gap analysis (US + MENA + EU + AUS/NZ)

| Region | Working now | Missing |
|---|---|---|
| US | LinkedIn, Indeed, Arbeitnow, (RemoteOK) | — |
| EU (DE/UK/etc.) | Arbeitnow (DE), Indeed (if country set), LinkedIn | Adzuna `de`/`gb` (key needed) |
| MENA (UAE/KSA) | LinkedIn (partial) | **Bayt** (dormant — add to boards), RSS feeds (GulfTalent/Bayt), no API covers Gulf well |
| AUS/NZ | LinkedIn, Indeed (needs country) | Adzuna `au` |
| Global remote | RemoteOK, LinkedIn, Indeed | — |

---

## 9. Status & pending fixes (as of 2026-08-08 verification)

**VERIFIED WORKING (kept enabled):** LinkedIn, Indeed (jobspy); Arbeitnow, RemoteOK (API).

**VERIFIED FAILED (commented out in config, code kept — re-test later):**
Glassdoor, Google, Bayt, Naukri, BDJobs (jobspy); ZipRecruiter.
**DORMANT / UNVERIFIED (need keys or config):** Adzuna, USAJobs, Muse (API keys empty),
RSS (no feeds), Workday (no employer URLs), SmartExtract (no sites.yaml).

Remaining fixes:
1. ~~Fix config mismatch~~ — **DONE (commit 03a1465)**: code now reads `boards:`,
   `location.accept_patterns/reject_patterns`, top-level `country`.
2. **Bayt for MENA** — commented out (403 anti-bot). Re-enable + test with a
   residential proxy later; it's the key Gulf source.
3. **Per-location country** — for global Indeed, pass country per location (currently one global).
4. **ZipRecruiter** — commented out; also excluded from JobSpy fallback site lists (jobspy.py).
5. **Vendored jobspy patch** — `Country.from_string` patch lives only in .venv; not reproducible.
   Should be vendored/re-applied on reinstall (remote jobs crash without it).
6. **Global locations** — expand `locations:` (Dubai, Riyadh, London, Berlin, Sydney, Auckland) + accept patterns.
7. **Strict freshness** — `hours_old` 72 → 24 (LinkedIn `f_TPR` already supports it); add a hard `posted_at` cutoff in the store path as backup.
8. **Runtime** — concurrency plan (thread-per-source in `_run_discover`; small JobSpy pool for different sites only). See below.
9. **Adzuna keys** — free signup needed to enable multi-country API coverage.
10. **sites.yaml** — SmartExtract needs one to find any targets.

---

## 10. Concurrency plan (threads) — agreed direction

| Level | Change | Risk | Win |
|---|---|---|---|
| L1 | Run the 4 discover sub-sources in parallel (thread-per-source) | Low (different sites) | ~4× wall-time reduction |
| L2 | Small thread pool over JobSpy queries (2-3) | Medium (same-site parallel → bot risk) | Hours → ~30-45 min |
| — | Required safety | — | SQLite WAL + per-thread connections; per-source rate-limit semaphores; per-thread Playwright contexts (memory ~300MB each) |

---

## 11. Untested / never-run inventory (explicit)

- Adzuna, USAJobs, The Muse — keys never set (dormant, unverified)
- RSS fetcher — no feeds configured (one probe URL failed: malformed feed)
- Workday — never reached in a run; no employer URLs configured
- SmartExtract — never reached; no sites.yaml
- JobSpy with a proxy — no proxy configured (would unlock Glassdoor/Bayt/ZipRecruiter?)
- `applypilot run --stream` (concurrent stage mode) — exists in pipeline.py, never exercised
- `glassdoor_location_map` config key — never used (Glassdoor disabled before it mattered)
- JobSpy with a proxy — no proxy configured
- `applypilot run --stream` (concurrent stage mode) — exists in pipeline.py, never exercised
