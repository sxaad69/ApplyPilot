"""Smoke tests for the production-hardening changes (db, config, fetchers, notify).

APPLYPILOT_DIR is set at import time so that applypilot's config module (which
captures APP_DIR / DB_PATH / SEARCH_CONFIG_PATH once at import) points into an
isolated temp dir for the whole test session.
"""

import os
import tempfile
from pathlib import Path

os.environ["APPLYPILOT_DIR"] = tempfile.mkdtemp(prefix="applypilot-test-")

SAMPLE_JOB = {
    "title": "Backend Engineer",
    "company": "Acme Corp",
    "location": "Remote",
    "url": "https://example.com/jobs/backend-1",
    "description": "Python backend engineer. Build APIs with FastAPI.",
    "salary": "",
    "external_id": "acme-1",
    "source": "Arbeitnow",
}


def test_job_id_is_md5_of_url():
    from applypilot.db import JobDatabase

    jid = JobDatabase.job_id("https://example.com/jobs/x")
    assert len(jid) == 32  # md5 hexdigest
    assert JobDatabase.job_id("https://example.com/jobs/x") == jid
    assert JobDatabase.job_id("https://example.com/jobs/x") != JobDatabase.job_id(
        "https://example.com/jobs/y"
    )


def test_upsert_deduplicates_and_status_lifecycle():
    from applypilot.database import init_db

    init_db()
    from applypilot.db import JobDatabase

    db = JobDatabase()
    inserted, _ = db.upsert_job(SAMPLE_JOB)
    assert inserted is True
    dup, _ = db.upsert_job(SAMPLE_JOB)
    assert dup is False

    row = db.get_by_url(SAMPLE_JOB["url"])
    assert row["status"] == "new"
    assert row["company"] == "Acme Corp"
    assert row["external_id"] == "acme-1"
    assert row["full_description"]  # description is promoted to full_description

    db.mark_scored(SAMPLE_JOB["url"], fit_score=8, min_score=7)
    assert db.get_by_url(SAMPLE_JOB["url"])["status"] == "scored"

    db.mark_tailored(SAMPLE_JOB["url"], path="/tmp/r.pdf")
    assert db.get_by_url(SAMPLE_JOB["url"])["status"] == "tailored"

    db.mark_cover_lettered(SAMPLE_JOB["url"], path="/tmp/cl.txt")
    assert db.get_by_url(SAMPLE_JOB["url"])["status"] == "cover_lettered"

    stats = db.get_status_stats()
    assert stats["cover_lettered"] == 1
    assert len(db.list_jobs("cover_lettered")) == 1


def test_score_below_threshold_rejects():
    from applypilot.database import init_db

    init_db()
    from applypilot.db import JobDatabase

    db = JobDatabase()
    job = dict(SAMPLE_JOB, url="https://example.com/jobs/low")
    db.upsert_job(job)
    db.mark_scored(job["url"], fit_score=4, min_score=7)
    assert db.get_by_url(job["url"])["status"] == "rejected"
    assert db.get_status_stats()["rejected"] == 1


def test_min_fit_score_default_and_env():
    from applypilot.config import get_min_fit_score

    os.environ.pop("MIN_FIT_SCORE", None)
    assert get_min_fit_score() == 7
    os.environ["MIN_FIT_SCORE"] = "6"
    assert get_min_fit_score() == 6
    os.environ["MIN_FIT_SCORE"] = "garbage"
    assert get_min_fit_score() == 7
    os.environ.pop("MIN_FIT_SCORE", None)


def test_telegram_notifier_disabled_noop():
    from applypilot.notify import TelegramNotifier

    n = TelegramNotifier(token="", chat_id="")
    assert n.enabled is False
    assert n.send("hello") is False
    n.send_summary(total=1, new=1, tailored=0, rejected=0)  # must not raise
    n.close()


def test_fetch_helpers():
    from applypilot import fetchers_api

    # whitespace collapse used by every fetcher
    assert fetchers_api._clean("<p>  Build React apps  </p>") == "<p> Build React apps </p>"
    assert fetchers_api._clean("  Stripe  ") == "Stripe"
    assert fetchers_api._clean("") == ""
    assert fetchers_api._clean(None) == ""

    # built-in sources are registered (arbeitnow + remoteok always-on)
    names = {name for name, _ in fetchers_api._all_fetchers()}
    assert {"Arbeitnow", "RemoteOK"} <= names


def test_rss_feed_urls_read_searches_yaml():
    from applypilot import config

    config.SEARCH_CONFIG_PATH.write_text(
        "queries:\n  - query: python\napi_sources:\n  rss:\n"
        "    - https://example.com/feed.xml\n    - https://other.com/rss\n"
    )
    from applypilot import fetchers_api

    assert fetchers_api.rss_feed_urls() == [
        "https://example.com/feed.xml",
        "https://other.com/rss",
    ]
