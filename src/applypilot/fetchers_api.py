"""API-based job discovery sources (no scraping required).

Implements fetchers for free/public job APIs and normalizes everything to
ApplyPilot's internal job schema before storing via JobDatabase (dedup by
URL hash, so API jobs never collide with scraped ones).

Sources:
  - Arbeitnow  (no auth)          https://www.arbeitnow.com/api/job-board-api
  - RemoteOK   (no auth)          https://remoteok.com/api
  - Adzuna     (optional)         needs ADZUNA_APP_ID + ADZUNA_APP_KEY
  - USAJobs    (optional)         needs USAJOBS_API_KEY
  - The Muse   (optional)         needs THE_MUSE_API_KEY
  - RSS/Atom   (optional)         custom feed URLs from searches.yaml / RSS_FEEDS

Each fetcher returns a list of normalized job dicts:

    {
        "title": str, "company": str, "location": str, "url": str,
        "description": str, "salary": str,
        "external_id": str, "source": str,
    }
"""

from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx

from applypilot import config
from applypilot.db import JobDatabase

log = logging.getLogger(__name__)

_TIMEOUT = 30.0
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ApplyPilot/0.3"

# Max jobs stored per source per run (keeps runs bounded)
MAX_PER_SOURCE = 200


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def _clean(text: str | None) -> str:
    if not text:
        return ""
    s = str(text)
    s = s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"')
    s = re.sub(r"<[^>]+>", " ", s)  # strip HTML tags
    return re.sub(r"\s+", " ", s).strip()


def _get_json(url: str, params: dict | None = None, headers: dict | None = None) -> dict | list:
    resp = httpx.get(url, params=params, headers=headers, timeout=_TIMEOUT,
                     follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


def _search_queries() -> list[str]:
    """Search keywords from env (SEARCH_KEYWORDS) or searches.yaml queries."""
    kw = os.environ.get("SEARCH_KEYWORDS", "")
    if kw.strip():
        return [q.strip() for q in kw.split(",") if q.strip()]

    try:
        cfg = config.load_search_config()
        return [q["query"] for q in cfg.get("queries", []) if q.get("query")]
    except Exception:
        return ["software engineer"]


def _search_location() -> str:
    loc = os.environ.get("SEARCH_LOCATION", "").strip()
    if loc:
        return loc
    try:
        cfg = config.load_search_config()
        locs = cfg.get("locations", [])
        if locs:
            return locs[0].get("location", "Remote")
    except Exception:
        pass
    return "Remote"


# ---------------------------------------------------------------------------
# Source fetchers
# ---------------------------------------------------------------------------

def fetch_arbeitnow() -> list[dict]:
    """Arbeitnow job-board API (no auth)."""
    data = _get_json("https://www.arbeitnow.com/api/job-board-api")
    jobs = []
    for item in (data.get("data", []) if isinstance(data, dict) else [])[:MAX_PER_SOURCE]:
        jobs.append({
            "title": _clean(item.get("title")),
            "company": _clean(item.get("company_name")),
            "location": _clean(item.get("location")),
            "url": item.get("url") or item.get("job_url"),
            "description": _clean(item.get("description")),
            "salary": item.get("salary") or "",
            "external_id": str(item.get("slug", "")),
            "source": "Arbeitnow",
        })
    return [j for j in jobs if j["url"] and j["title"]]


def fetch_remoteok() -> list[dict]:
    """RemoteOK API (no auth). Returns remote developer jobs."""
    data = _get_json("https://remoteok.com/api", headers={"Accept": "application/json"})
    # First element is a message/version object
    if isinstance(data, list) and data and isinstance(data[0], dict) and "id" not in data[0]:
        data = data[1:]
    jobs = []
    for item in (data if isinstance(data, list) else [])[:MAX_PER_SOURCE]:
        if not isinstance(item, dict):
            continue
        tags = ", ".join(t for t in item.get("tags", []) if t) if isinstance(item.get("tags"), list) else ""
        jobs.append({
            "title": _clean(item.get("position")),
            "company": _clean(item.get("company")),
            "location": "Remote" if item.get("location") == "anywhere" else _clean(item.get("location")) or "Remote",
            "url": item.get("url"),
            "description": _clean(item.get("description")),
            "salary": _clean(item.get("salary")) or "",
            "external_id": str(item.get("id", "")),
            "source": "RemoteOK",
        })
        if tags:
            jobs[-1]["description"] = f"{jobs[-1]['description']}\nTags: {tags}".strip()
    return [j for j in jobs if j["url"] and j["title"]]


def fetch_adzuna() -> list[dict]:
    """Adzuna Jobs API (needs ADZUNA_APP_ID + ADZUNA_APP_KEY)."""
    app_id = os.environ.get("ADZUNA_APP_ID", "")
    app_key = os.environ.get("ADZUNA_APP_KEY", "")
    if not app_id or not app_key:
        log.debug("Adzuna skipped: ADZUNA_APP_ID/ADZUNA_APP_KEY not set")
        return []
    country = os.environ.get("ADZUNA_COUNTRY", "us")
    jobs: list[dict] = []
    for query in _search_queries()[:3]:
        data = _get_json(
            f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
            params={
                "app_id": app_id,
                "app_key": app_key,
                "what": query,
                "results_per_page": min(50, MAX_PER_SOURCE),
                "content-type": "application/json",
            },
        )
        for item in data.get("results", []):
            jobs.append({
                "title": _clean(item.get("title")),
                "company": _clean(item.get("company", {}).get("display_name")) if isinstance(item.get("company"), dict) else _clean(item.get("company")),
                "location": _clean(item.get("location", {}).get("display_name")) if isinstance(item.get("location"), dict) else _clean(item.get("location")),
                "url": item.get("redirect_url") or item.get("url"),
                "description": _clean(item.get("description")),
                "salary": _adzuna_salary(item),
                "external_id": str(item.get("id", "")),
                "source": "Adzuna",
            })
    return [j for j in jobs if j["url"] and j["title"]]


def _adzuna_salary(item: dict) -> str:
    if not item:
        return ""
    min_sal = item.get("salary_min")
    max_sal = item.get("salary_max")
    if min_sal or max_sal:
        return f"{min_sal or 0}-{max_sal or '?'}"
    return ""


def fetch_usajobs() -> list[dict]:
    """USAJobs API (needs USAJOBS_API_KEY)."""
    api_key = os.environ.get("USAJOBS_API_KEY", "")
    if not api_key:
        log.debug("USAJobs skipped: USAJOBS_API_KEY not set")
        return []
    jobs: list[dict] = []
    for query in _search_queries()[:3]:
        data = _get_json(
            "https://data.usajobs.gov/api/search",
            params={"Keyword": query, "ResultsPerPage": min(100, MAX_PER_SOURCE)},
            headers={
                "Host": "data.usajobs.gov",
                "User-Agent": "applypilot-production-fork",
                "Authorization-Key": api_key,
            },
        )
        for item in (data.get("SearchResult", {}).get("SearchResultItems", []) or []):
            pos = item.get("MatchedObjectDescriptor", {})
            jobs.append({
                "title": _clean(pos.get("PositionTitle")),
                "company": _clean(pos.get("DepartmentName")) or _clean(pos.get("OrganizationName")),
                "location": _clean(", ".join(
                    f"{l.get('CityName', '')}, {l.get('CountrySubdivisionCode', '')}"
                    for l in (pos.get("PositionLocation") or [])
                )),
                "url": pos.get("PositionURI"),
                "description": _clean(
                    f"{pos.get('QualificationSummary', '')} {pos.get('UserArea', {}).get('Details', {}).get('JobSummary', '')}"
                ),
                "salary": _usajobs_salary(pos),
                "external_id": str(pos.get("PositionID", "")),
                "source": "USAJobs",
            })
    return [j for j in jobs if j["url"] and j["title"]]


def _usajobs_salary(pos: dict) -> str:
    loc = (pos.get("PositionLocation") or [{}])[0]
    min_sal = loc.get("MinimumRange")
    max_sal = loc.get("MaximumRange")
    if min_sal or max_sal:
        return f"{min_sal or 0}-{max_sal or '?'}"
    return ""


def fetch_the_muse() -> list[dict]:
    """The Muse Jobs API (needs THE_MUSE_API_KEY)."""
    api_key = os.environ.get("THE_MUSE_API_KEY", "")
    if not api_key:
        log.debug("The Muse skipped: THE_MUSE_API_KEY not set")
        return []
    jobs: list[dict] = []
    for query in _search_queries()[:3]:
        data = _get_json(
            "https://www.themuse.com/api/public/jobs",
            params={"query": query, "page": 1, "location": _search_location()},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        for item in (data.get("results", []) if isinstance(data, dict) else [])[:MAX_PER_SOURCE]:
            company = ""
            if item.get("company"):
                company = item["company"].get("name", "")
            locs = ", ".join(l.get("name", "") for l in (item.get("locations") or []) if l.get("name"))
            salary = _clean(item.get("salary", {}).get("content")) if isinstance(item.get("salary"), dict) else ""
            jobs.append({
                "title": _clean(item.get("name")),
                "company": _clean(company),
                "location": _clean(locs) or "Remote",
                "url": item.get("refs", {}).get("landing_page") if isinstance(item.get("refs"), dict) else None,
                "description": _clean(item.get("contents")),
                "salary": salary,
                "external_id": str(item.get("id", "")),
                "source": "Muse",
            })
    return [j for j in jobs if j["url"] and j["title"]]


def fetch_rss(feed_url: str) -> list[dict]:
    """Generic RSS 2.0 / Atom feed parser."""
    resp = httpx.get(feed_url, timeout=_TIMEOUT, follow_redirects=True,
                     headers={"User-Agent": _UA})
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    jobs: list[dict] = []

    def _get(elem, tag: str, namespaces: dict | None = None) -> str:
        node = elem.find(tag, namespaces) if namespaces else elem.find(tag)
        return _clean(node.text) if node is not None and node.text else ""

    for entry in root.iter("item"):
        title = _get(entry, "title")
        url = _get(entry, "link")
        desc = _get(entry, "description")
        if title and url:
            jobs.append({
                "title": title,
                "company": _feed_company(feed_url),
                "location": "",
                "url": url,
                "description": desc,
                "salary": "",
                "external_id": _get(entry, "guid"),
                "source": f"RSS:{_feed_host(feed_url)}",
            })

    for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
        title = _get(entry, "{http://www.w3.org/2005/Atom}title")
        url = _get(entry, "{http://www.w3.org/2005/Atom}id")
        if not url:
            link = entry.find("{http://www.w3.org/2005/Atom}link")
            url = link.get("href") if link is not None else ""
        desc = _get(entry, "{http://www.w3.org/2005/Atom}content")
        if not desc:
            desc = _get(entry, "{http://www.w3.org/2005/Atom}summary")
        if title and url:
            jobs.append({
                "title": title,
                "company": _feed_company(feed_url),
                "location": "",
                "url": url,
                "description": desc,
                "salary": "",
                "external_id": _get(entry, "{http://www.w3.org/2005/Atom}id"),
                "source": f"RSS:{_feed_host(feed_url)}",
            })

    return [j for j in jobs if j["url"] and j["title"]]


def _feed_host(feed_url: str) -> str:
    from urllib.parse import urlparse
    host = urlparse(feed_url).hostname or "feed"
    return host.replace("www.", "")


def _feed_company(feed_url: str) -> str:
    from urllib.parse import urlparse
    host = urlparse(feed_url).hostname or ""
    parts = host.replace("www.", "").split(".")
    return parts[0].title() if parts and parts[0] else ""


def rss_feed_urls() -> list[str]:
    """Custom RSS/Atom feed URLs from searches.yaml `api_sources.rss` or RSS_FEEDS env."""
    urls: list[str] = []
    env = os.environ.get("RSS_FEEDS", "")
    if env.strip():
        urls.extend(u.strip() for u in env.split(",") if u.strip())
    try:
        cfg = config.load_search_config()
        api_sources = cfg.get("api_sources", {}) or {}
        for feed in (api_sources.get("rss", []) or []):
            if isinstance(feed, str):
                urls.append(feed)
            elif isinstance(feed, dict) and feed.get("url"):
                urls.append(feed["url"])
    except Exception:
        pass
    return urls


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _all_fetchers() -> list[tuple[str, callable]]:
    """Return configured fetchers: (name, callable).

    Verified working 2026-08-08: Arbeitnow + RemoteOK (no keys).
    Adzuna / USAJobs / Muse are dormant -- they only activate when their
    env keys are set. Keys were empty during verification, so they are
    UNVERIFIED, not failing. Re-enable by setting the keys in ~/.applypilot/.env.
    RSS feeds only activate when feeds are configured (RSS_FEEDS env or
    searches.yaml api_sources.rss); none configured -> dormant.
    """
    fetchers: list[tuple[str, callable]] = [
        ("Arbeitnow", fetch_arbeitnow),
        ("RemoteOK", fetch_remoteok),
    ]
    if os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY"):
        fetchers.append(("Adzuna", fetch_adzuna))
    if os.environ.get("USAJOBS_API_KEY"):
        fetchers.append(("USAJobs", fetch_usajobs))
    if os.environ.get("THE_MUSE_API_KEY"):
        fetchers.append(("TheMuse", fetch_the_muse))
    for url in rss_feed_urls():
        fetchers.append((f"RSS:{_feed_host(url)}", lambda u=url: fetch_rss(u)))
    return fetchers


def run_api_discovery(db: JobDatabase | None = None) -> dict:
    """Run every configured API source and store new jobs (dedup by URL hash).

    Returns:
        {"new": int, "existing": int, "errors": int, "sources": [name,...]}
    """
    db = db or JobDatabase()
    fetchers = _all_fetchers()
    if not fetchers:
        log.info("No API sources configured (Adzuna/USAJobs/Muse/RSS optional). "
                 "Arbeitnow + RemoteOK always run.")
        # Still run the always-on sources even if env missing (they are always on)

    new = 0
    existing = 0
    errors = 0
    sources: list[str] = []
    started = datetime.now(timezone.utc)

    for name, fetcher in fetchers:
        try:
            jobs = fetcher()
            sources.append(name)
            source_new = 0
            for job in jobs:
                inserted, _ = db.upsert_job(job, site=name, strategy="api")
                if inserted:
                    source_new += 1
            new += source_new
            existing += len(jobs) - source_new
            log.info("API source %-12s -> %d jobs (+%d new, %d dupes)",
                     name, len(jobs), source_new, len(jobs) - source_new)
        except httpx.HTTPStatusError as e:
            errors += 1
            log.warning("API source %s failed (HTTP %s): %s", name, e.response.status_code, e)
        except Exception as e:
            errors += 1
            log.warning("API source %s failed: %s", name, e)

    log.info("API discovery done in %.1fs: +%d new, %d dupes, %d source errors",
             (datetime.now(timezone.utc) - started).total_seconds(),
             new, existing, errors)

    return {"new": new, "existing": existing, "errors": errors, "sources": sources}
