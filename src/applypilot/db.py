"""High-level JobDatabase wrapper around the ApplyPilot SQLite store.

Exposes a small, stable API for the pipeline stages (Discover, Score,
Tailor, Cover Letter) to persist jobs and move them through the status
lifecycle:

    new -> scored -> tailored -> cover_lettered
             --> rejected (below fit threshold)
    any stage failure -> error

The underlying table lives in `applypilot.database` (url is the primary
key; `id` is the MD5 hash of the URL used for deduplication and stable
identity). This class is the recommended way for new pipeline code to
talk to the store.
"""

from __future__ import annotations

import logging

from applypilot.database import (
    JOB_STATUS_COVER_LETTERED,
    JOB_STATUS_ERROR,
    JOB_STATUS_NEW,
    JOB_STATUS_REJECTED,
    JOB_STATUS_SCORED,
    JOB_STATUS_TAILORED,
    VALID_JOB_STATUSES,
    get_connection,
    get_jobs_by_status,
    get_status_stats,
    job_id as _md5_id,
    set_job_status,
)

log = logging.getLogger(__name__)

__all__ = [
    "JOB_STATUS_COVER_LETTERED",
    "JOB_STATUS_ERROR",
    "JOB_STATUS_NEW",
    "JOB_STATUS_REJECTED",
    "JOB_STATUS_SCORED",
    "JOB_STATUS_TAILORED",
    "VALID_JOB_STATUSES",
    "JobDatabase",
]


class JobDatabase:
    """Wrapper over the ApplyPilot SQLite jobs table.

    All methods are safe to call from worker threads (each thread uses its
    own SQLite connection under the hood).
    """

    def __init__(self, db_path=None) -> None:
        self._db_path = db_path

    @property
    def _conn(self):
        return get_connection(self._db_path)

    # -- identity ----------------------------------------------------------

    @staticmethod
    def job_id(url: str) -> str:
        """Stable id for a job URL (MD5 hash)."""
        return _md5_id(url)

    # -- lifecycle transitions ---------------------------------------------

    def mark_scored(self, url: str, fit_score: int | None = None,
                    min_score: int | None = None) -> None:
        """Move a job to `scored`, or `rejected` if below the fit threshold.

        Args:
            url: Job URL.
            fit_score: The 1-10 score just computed (persisted here too).
            min_score: Threshold below which the job is rejected.
                       Defaults to 7 when fit_score is provided.
        """
        conn = self._conn
        if fit_score is not None:
            conn.execute(
                "UPDATE jobs SET fit_score = ?, status = ? WHERE url = ?",
                (fit_score, JOB_STATUS_SCORED, url),
            )
            conn.commit()
        status = JOB_STATUS_SCORED
        if fit_score is not None and min_score is not None and fit_score < min_score:
            status = JOB_STATUS_REJECTED
        set_job_status(url, status, conn=conn)

    def mark_tailored(self, url: str, path: str | None = None) -> None:
        """Mark a job as tailored and record its tailored resume path."""
        conn = self._conn
        if path:
            conn.execute(
                "UPDATE jobs SET tailored_resume_path = ? WHERE url = ?",
                (path, url),
            )
            conn.commit()
        set_job_status(url, JOB_STATUS_TAILORED, conn=conn)

    def mark_cover_lettered(self, url: str, path: str | None = None) -> None:
        """Mark a job as cover_lettered and record its cover letter path."""
        conn = self._conn
        if path:
            conn.execute(
                "UPDATE jobs SET cover_letter_path = ? WHERE url = ?",
                (path, url),
            )
            conn.commit()
        set_job_status(url, JOB_STATUS_COVER_LETTERED, conn=conn)

    def mark_rejected(self, url: str) -> None:
        """Mark a job as rejected (below fit threshold)."""
        set_job_status(url, JOB_STATUS_REJECTED, conn=self._conn)

    def mark_error(self, url: str) -> None:
        """Mark a job as errored (failed at some pipeline stage)."""
        set_job_status(url, JOB_STATUS_ERROR, conn=self._conn)

    # -- queries -----------------------------------------------------------

    def get_status_stats(self) -> dict[str, int]:
        """Count of jobs per pipeline status (0-filled for all statuses)."""
        return get_status_stats(conn=self._conn)

    def list_jobs(self, status: str, limit: int = 100) -> list[dict]:
        """List jobs in a given status, best fit score first."""
        return get_jobs_by_status(status, conn=self._conn, limit=limit)

    def get_by_url(self, url: str) -> dict | None:
        """Fetch a single job by URL, or None."""
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE url = ?", (url,)
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def upsert_job(self, job: dict, site: str | None = None,
                   strategy: str = "api") -> tuple[bool, dict]:
        """Insert a normalized job, skipping duplicates by URL hash.

        Args:
            job: Normalized job dict with keys: url, title, company,
                 location, description, salary, external_id, source.
            site: Source label (defaults to job['source'] or 'API').
            strategy: Extraction strategy label.

        Returns:
            (was_inserted, job_dict_with_db_id)
        """
        conn = self._conn
        url = job.get("url")
        if not url:
            return False, job

        existing = conn.execute(
            "SELECT 1 FROM jobs WHERE url = ?", (url,)
        ).fetchone()
        if existing:
            return False, job

        now = _now_iso()
        site_label = site or job.get("source") or "API"
        full_description = job.get("full_description") or job.get("description")
        conn.execute(
            "INSERT INTO jobs (id, external_id, company, status, created_at, updated_at, "
            "url, title, salary, description, full_description, application_url, "
            "location, site, strategy, discovered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _md5_id(url),
                job.get("external_id"),
                job.get("company"),
                JOB_STATUS_NEW,
                now,
                now,
                url,
                job.get("title"),
                job.get("salary"),
                job.get("description"),
                full_description,
                job.get("application_url"),
                job.get("location"),
                site_label,
                strategy,
                now,
            ),
        )
        conn.commit()
        return True, job


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
