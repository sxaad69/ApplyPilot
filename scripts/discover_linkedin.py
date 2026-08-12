"""LinkedIn-only discovery: scrape LinkedIn jobs only (no Workday/Indeed/APIs).

Runs JobSpy's LinkedIn scraper across the configured locations, throttled, so
it's gentle on LinkedIn's rate limits. Uses the user's profile/resume context.

Usage:
    python -m applypilot.scripts.discover_linkedin [--hours-old 168] [--limit 200]
"""

import argparse
import logging
import time

log = logging.getLogger("applypilot.discover_linkedin")

# LinkedIn-only search config (overrides searches.yaml boards/locations).
def _linkedin_config(hours_old: int, results_per_site: int) -> dict:
    return {
        "queries": [
            {"query": "SDET", "tier": 1},
            {"query": "QA automation engineer", "tier": 1},
            {"query": "quality engineer", "tier": 1},
            {"query": "test automation engineer", "tier": 1},
            {"query": "software development engineer in test", "tier": 1},
            {"query": "QA engineer", "tier": 2},
            {"query": "test engineer", "tier": 2},
            {"query": "automation engineer", "tier": 2},
            {"query": "software quality engineer", "tier": 2},
        ],
        "locations": [
            {"location": "Riyadh", "remote": False},
            {"location": "Dubai", "remote": False},
            {"location": "Remote", "remote": True},
            {"location": "Saudi Arabia", "remote": False},
            {"location": "United Arab Emirates", "remote": False},
        ],
        "boards": ["linkedin"],
        "defaults": {
            "results_per_site": results_per_site,
            "hours_old": hours_old,
        },
        "location": {
            "accept_patterns": [
                "Riyadh", "Saudi Arabia", "Dubai", "UAE", "United Arab Emirates",
                "Remote", "Anywhere", "Qatar", "Kuwait", "Oman", "Bahrain",
            ],
            "reject_patterns": ["India", "Philippines"],
        },
        "exclude_titles": [
            "senior director", "VP ", "vice president", "chief",
            "intern", "internship", "clearance required",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="LinkedIn-only job discovery")
    parser.add_argument("--hours-old", type=int, default=168, help="Only jobs newer than this many hours (default 168 = 7 days)")
    parser.add_argument("--results-per-site", type=int, default=100)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--query-delay", type=float, default=8.0, help="Seconds between queries (throttle)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    from applypilot.discovery.jobspy import run_discovery

    cfg = _linkedin_config(args.hours_old, args.results_per_site)
    log.info("LinkedIn-only discovery: %d queries x %d locations",
             len(cfg["queries"]), len(cfg["locations"]))
    log.info("Hours old: %d | Results/site: %d | Workers: %d | Delay: %.1fs",
             args.hours_old, args.results_per_site, args.workers, args.query_delay)

    t0 = time.time()
    stats = run_discovery(cfg=cfg, workers=args.workers, query_delay=args.query_delay)
    log.info("LinkedIn discovery done in %.1fs: %s", time.time() - t0, stats)


if __name__ == "__main__":
    main()
