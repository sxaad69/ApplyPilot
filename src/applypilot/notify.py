"""Telegram notifications for ApplyPilot pipeline events.

Sends messages via the Telegram Bot API when configured in the environment:

    TELEGRAM_BOT_TOKEN=<bot token from @BotFather>
    TELEGRAM_CHAT_ID=<chat or channel id>

Messages use HTML parse mode. If either variable is missing, the notifier
is a silent no-op (no exceptions, no network calls). Per-job messages are
also gated behind `notify.tailored` / `notify.rejected` env flags (both
default to enabled) so users can mute per-job spam while keeping the
run summary.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    """Sends HTML-formatted Telegram messages. No-op when unconfigured."""

    def __init__(self, token: str | None = None, chat_id: str | None = None,
                 timeout: float = 10.0) -> None:
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID", "")
        self._client = httpx.Client(timeout=timeout)

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _flag(self, name: str) -> bool:
        raw = os.environ.get(f"notify.{name}", "1")
        return raw.lower() not in ("0", "false", "no", "off")

    def send(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a raw message. Returns True if delivered (or disabled)."""
        if not self.enabled:
            return False
        try:
            resp = self._client.post(
                _API.format(token=self.token),
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
            )
            if resp.status_code == 200:
                return True
            log.warning("Telegram send failed (HTTP %s): %s", resp.status_code, resp.text[:200])
        except httpx.HTTPError as exc:
            log.warning("Telegram send error: %s", exc)
        return False

    def send_summary(self, total: int, new: int, tailored: int, rejected: int,
                     **extra: int) -> None:
        """Send a run-completion summary."""
        if not self.enabled:
            return
        lines = [
            "<b>ApplyPilot run complete</b>",
            "",
            f"Found: <b>{total}</b>",
            f"New: <b>{new}</b>",
            f"Tailored: <b>{tailored}</b>",
            f"Rejected: <b>{rejected}</b>",
        ]
        for label, value in extra.items():
            if value is not None:
                lines.append(f"{label}: <b>{value}</b>")
        self.send("\n".join(lines))

    def send_tailored(self, title: str, company: str, score, resume_path: str) -> None:
        """Notify about a successfully tailored job."""
        if not self.enabled or not self._flag("tailored"):
            return
        score_str = str(score) if score is not None else "?"
        self.send(
            f"🟢 Tailored: {_esc(title)} at {_esc(company)} | "
            f"Fit: {score_str}/10 | {_esc(resume_path)}"
        )

    def send_rejected(self, title: str, company: str, score, min_score: int) -> None:
        """Notify about a job rejected for falling below the fit threshold."""
        if not self.enabled or not self._flag("rejected"):
            return
        score_str = str(score) if score is not None else "?"
        self.send(
            f"🔴 Rejected: {_esc(title)} at {_esc(company)} | "
            f"Fit: {score_str}/10 (below threshold {min_score})"
        )

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


def _esc(text) -> str:
    """Escape HTML-sensitive characters for Telegram HTML parse mode."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# Module-level singleton — used by pipeline stages
# ---------------------------------------------------------------------------

_notifier: TelegramNotifier | None = None


def get_notifier() -> TelegramNotifier:
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier()
    return _notifier


notifier = get_notifier()
