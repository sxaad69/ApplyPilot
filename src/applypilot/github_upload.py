"""Upload generated resume/cover PDFs to a private GitHub repo.

Uses the `gh` CLI (already authenticated as the user) so no API token needs
to be stored. PDFs are committed to a private repo and the raw blob URL is
returned, which gets stored in the DB (tailored_resume_path / cover_letter_path)
so the user can download them directly.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# Private repo where generated PDFs are stored.
# Override with GITHUB_PDF_REPO env if needed.
DEFAULT_REPO = "sxaad69/applypilot-resumes"

# Subfolders inside the repo.
RESUME_DIR = "resumes"
COVER_DIR = "covers"


def _gh(*args: str) -> str:
    """Run a `gh` CLI command, returning stdout."""
    cmd = ["gh"] + list(args)
    log.debug("Running: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _repo() -> str:
    import os
    return os.environ.get("GITHUB_PDF_REPO", DEFAULT_REPO)


def _ensure_repo() -> None:
    """Make sure the repo exists; create it (private) if missing."""
    try:
        _gh("repo", "view", _repo(), "--json", "nameWithOwner")
    except RuntimeError:
        log.info("Repo %s not found — creating private repo.", _repo())
        owner = _repo().split("/")[0]
        name = _repo().split("/")[1]
        _gh("repo", "create", name, "--private", "--owner", owner,
            "--description", "ApplyPilot generated resumes + cover letters")


def upload_pdf(local_path: str | Path, kind: str = "resume") -> str:
    """Upload a PDF to the private repo and return its GitHub blob URL.

    The repo is PRIVATE, so raw.githubusercontent.com 404s without auth.
    Returning the GitHub blob URL is correct here — the user is authenticated
    via `gh` and can open/download it directly in the GitHub UI.

    Args:
        local_path: Absolute path to the PDF file.
        kind: "resume" or "cover" — selects the repo subfolder.

    Returns:
        GitHub blob URL (https://github.com/.../blob/main/...) for download.
    """
    path = Path(local_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")

    subdir = RESUME_DIR if kind == "resume" else COVER_DIR
    _ensure_repo()

    # Unique-ish filename: job-derived stem + short hash to avoid collisions.
    stem = path.stem
    remote_path = f"{subdir}/{stem}.pdf"

    # If the file already exists, GitHub requires the current blob sha to update.
    existing_sha = None
    try:
        existing_sha = _gh(
            "api", f"repos/{_repo()}/contents/{remote_path}",
            "--jq", ".sha",
        )
    except RuntimeError:
        existing_sha = None  # doesn't exist yet — plain create

    args = [
        "api", f"repos/{_repo()}/contents/{remote_path}",
        "--method", "PUT",
        "-f", f"message=Add/update {remote_path}",
        "-f", f"content={_b64(path)}",
        "-f", "branch=main",
    ]
    if existing_sha:
        args += ["-f", f"sha={existing_sha}"]

    try:
        _gh(*args)
    except RuntimeError as e:
        log.warning("Upload via api failed (%s); retrying via CLI push.", e)
        _push_via_cli(remote_path, path)

    return f"https://github.com/{_repo()}/blob/main/{remote_path}"


def _b64(path: Path) -> str:
    import base64
    return base64.b64encode(path.read_bytes()).decode()


def _push_via_cli(remote_path: str, local_path: Path) -> None:
    """Fallback: clone, copy, commit, push. Slower but robust."""
    import os
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="applypilot-upload-"))
    env = dict(os.environ)
    try:
        _gh("repo", "clone", _repo(), str(tmp))
        dest = tmp / remote_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(local_path.read_bytes())

        subprocess.run(["git", "-C", str(tmp), "add", "."],
                       check=True, capture_output=True, env=env)
        subprocess.run(
            ["git", "-C", str(tmp), "commit", "-m", f"Add {remote_path}"],
            check=False, capture_output=True, env=env,  # may be "nothing to commit"
        )
        subprocess.run(["git", "-C", str(tmp), "push", "origin", "HEAD"],
                       check=True, capture_output=True, env=env)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
