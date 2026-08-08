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
sequence** (never in parallel today):

```
1. JobSpy full crawl      (discovery/jobspy.py)
2. Workday corporate      (discovery/workday.py)
3. SmartExtract (AI)      (discovery/smartextract.py)
4. Free API sources       (fetchers_api.py)
```

Everything is driven by **`~/.applypilot/searches.yaml`** (via `config.load_search_config()`).

---

## 2. JobSpy (main engine) — `discovery/jobspy.py`

Scrapes job boards via the `jobspy` PyPI library (`.venv/lib/python3.11/site-packages/jobspy/`).

### Source matrix (JobSpy library scrapers present)

| Board | Site key | Status | Evidence / notes |
|---|---|---|---|
| LinkedIn | `linkedin` | ✅ WORKING | **56 jobs** in DB from run. Slow (~2 min/query). Uses `linkedin_fetch_description=True` to get descriptions during scrape (LinkedIn pages are login-walled for the enrich stage). |
| Indeed | `indeed` | ✅ WORKING | **33 jobs** in DB. Needs per-location `country_indeed` for non-US global search (see §8, currently a single `country: USA`). |
| ZipRecruiter | `zip_recruiter` | ❌ FAILED | **HTTP 403 forbidden** on every request — Cloudflare anti-bot (error body `{"error_code":"forbidden aa",...}` + `CFRAY` header). **Removed from searches.yaml boards on 2026-08-08.** |
| Glassdoor | `glassdoor` | ⚠️ UNPROVEN | In boards list but run was killed before reaching it. Known fragile / bot-blocked in the wild. Glassdoor requires simplified location via `glassdoor_location_map`. |
| Google | `google` | ⚠️ UNPROVEN | In boards list; never observed to run. |
| Bayt | `bayt` | 💤 DORMANT | Scraper EXISTS in jobspy lib (`Site.BAYT`, `jobspy/bayt/`). **Not in boards list.** This is the key MENA (Gulf) source — Bayt is native UAE/KSA. Country controlled by the location string passed to its scraper. |
| Naukri | `naukri` | 💤 DORMANT | Scraper exists in lib (`Site.NAUKRI`). India-focused. Not configured. |
| BDJobs | `bdjobs` | 💤 DORMANT | Scraper exists in lib (`Site.BDJOBS`). Bangladesh-focused. Not configured. |

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
| Status | ⚠️ UNPROVEN this run (killed before reaching it) |
| What | AI-driven extraction of job data from target sites (`build_scrape_targets`) |
| Config | Reads `search_cfg.get("location_accept")` / `location_reject_non_remote` — 🔧 same mismatch as JobSpy |
| LLM usage | **YES** — uses `client.ask()` at smartextract.py:406 and :655 (costs tokens; the "where are we calling LLM crazy?" map included these) |
| Sites | Requires `sites.yaml` (`load_sites()`) — **no sites.yaml exists** (`~/.applypilot/sites.yaml` absent), so it will find **zero targets** until one is created |

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

## 9. Pending fixes (from this audit)

1. **🔧 Fix config mismatch** — searches.yaml uses `boards:`, `location.accept_patterns`,
   `location.reject_patterns`, top-level `country`; code reads `sites`, `location_accept`,
   `location_reject_non_remote`, `defaults.country_indeed`, `location_labels`, `tiers`.
   Either rename yaml keys to match code, or update code to read the yaml keys.
2. **Bayt** — add `bayt` to boards for MENA (after config fix so it's actually read).
3. **Per-location country** — for global Indeed, pass country per location (currently one global).
4. **ZipRecruiter** — removed from `boards:`; also should be removed from JobSpy default fallback list (`sites = ["indeed","linkedin","zip_recruiter"]` at jobspy.py:380).
5. **Venved jobspy patch** — `Country.from_string` patch is not reproducible; document or vendor it.
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

- Glassdoor (jobspy) — never completed a run
- Google (jobspy) — never observed
- Bayt, Naukri, BDJobs (jobspy) — dormant scrapers, never configured
- Adzuna, USAJobs, The Muse — keys never set
- RSS fetcher — no feeds configured
- Workday — never reached in a run; no employer URLs configured
- SmartExtract — never reached; no sites.yaml
- JobSpy with a proxy — no proxy configured
- `applypilot run --stream` (concurrent stage mode) — exists in pipeline.py, never exercised
