"""
Unified LLM client for ApplyPilot — OpenAI-compatible only.

Provider resolution (from environment):
  OPENAI_BASE_URL + OPENAI_API_KEY + LLM_MODEL  -> any OpenAI-compatible
                                                  endpoint (OmniRoute, DeepSeek,
                                                  Qwen, Ollama, llama.cpp, ...)
  OPENAI_API_KEY alone                          -> api.openai.com
  LLM_URL (legacy local alias)                  -> local OpenAI-compatible server

LLM_MODEL overrides the model name for any provider. Defaults to
"deepseek-chat" when no model is configured.

NO Gemini. NO Claude API. All five AI stages (Discover, Enrich, Score,
Tailor, Cover Letter) route through this client.
"""

import logging
import os
import time

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

def _detect_provider() -> tuple[str, str, str]:
    """Return (base_url, model, api_key) based on environment variables.

    Reads env at call time (not module import time) so that load_env() called
    in _bootstrap() is always visible here.
    """
    openai_base = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    legacy_url = os.environ.get("LLM_URL", "").rstrip("/")
    model_override = os.environ.get("LLM_MODEL", "")

    if openai_base:
        return (
            openai_base,
            model_override or "deepseek-chat",
            openai_key,
        )

    if openai_key:
        return (
            "https://api.openai.com/v1",
            model_override or "gpt-4o-mini",
            openai_key,
        )

    if legacy_url:
        return (
            legacy_url,
            model_override or "local-model",
            os.environ.get("LLM_API_KEY", ""),
        )

    raise RuntimeError(
        "No LLM provider configured. "
        "Set OPENAI_BASE_URL (and OPENAI_API_KEY) in your environment, e.g. "
        "OPENAI_BASE_URL=http://localhost:20128/v1 with LLM_MODEL=deepseek-chat."
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_MAX_RETRIES = 5
_TIMEOUT = 120  # seconds

# Base wait on first 429/503 (doubles each retry, caps at 60s).
_RATE_LIMIT_BASE_WAIT = 10


class LLMClient:
    """Thin OpenAI-compatible chat completions client with retry/backoff."""

    def __init__(self, base_url: str, model: str, api_key: str) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self._client = httpx.Client(timeout=_TIMEOUT)

    # -- OpenAI-compat API --------------------------------------------------

    def _chat_compat(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the OpenAI-compatible endpoint."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        return self._handle_compat_response(resp)

    @staticmethod
    def _handle_compat_response(resp: httpx.Response) -> str:
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    # -- public API ---------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        """Send a chat completion request and return the assistant message text."""
        # Qwen3 optimization: prepend /no_think to skip chain-of-thought
        # reasoning, saving tokens on structured extraction tasks.
        if "qwen" in self.model.lower() and messages:
            first = messages[0]
            if first.get("role") == "user" and not first["content"].startswith("/no_think"):
                messages = [{"role": first["role"], "content": f"/no_think\n{first['content']}"}] + messages[1:]

        for attempt in range(_MAX_RETRIES):
            try:
                return self._chat_compat(messages, temperature, max_tokens)

            except httpx.HTTPStatusError as exc:
                resp = exc.response
                if resp.status_code in (429, 503) and attempt < _MAX_RETRIES - 1:
                    # Respect Retry-After header if provided.
                    retry_after = (
                        resp.headers.get("Retry-After")
                        or resp.headers.get("X-RateLimit-Reset-Requests")
                    )
                    if retry_after:
                        try:
                            wait = float(retry_after)
                        except (ValueError, TypeError):
                            wait = _RATE_LIMIT_BASE_WAIT * (2 ** attempt)
                    else:
                        wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)

                    log.warning(
                        "LLM rate limited (HTTP %s). Waiting %ds before retry %d/%d.",
                        resp.status_code, wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

            except httpx.TimeoutException:
                if attempt < _MAX_RETRIES - 1:
                    wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)
                    log.warning(
                        "LLM request timed out, retrying in %ds (attempt %d/%d)",
                        wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

        raise RuntimeError("LLM request failed after all retries")

    def ask(self, prompt: str, **kwargs) -> str:
        """Convenience: single user prompt -> assistant response."""
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def ping(self, timeout: int = 15) -> dict:
        """Test connectivity to the configured endpoint.

        Prefers the /models list endpoint (cheap, no tokens consumed).
        Falls back to a minimal chat completion for servers that don't
        expose /models (e.g. some OmniRoute/llama.cpp setups).

        Returns:
            {"ok": bool, "status": int|None, "detail": str, "model": str}
        """
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        # 1) Try GET /models
        try:
            resp = self._client.get(
                f"{self.base_url}/models",
                headers=headers,
                timeout=timeout,
            )
            if resp.status_code == 200:
                data = resp.json()
                models = data.get("data", []) if isinstance(data, dict) else []
                names = [m.get("id") for m in models if isinstance(m, dict)]
                hint = ""
                if self.model not in names and names:
                    hint = f" (available: {', '.join(names[:8])})"
                return {
                    "ok": True,
                    "status": 200,
                    "detail": f"connected to {self.base_url}",
                    "model": self.model + hint,
                }
            if resp.status_code in (401, 403):
                return {
                    "ok": False,
                    "status": resp.status_code,
                    "detail": f"/models returned {resp.status_code} — check OPENAI_API_KEY",
                    "model": self.model,
                }
            # 404/405/etc — fall through to a chat test
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "status": None,
                "detail": f"cannot reach {self.base_url}: {exc}",
                "model": self.model,
            }

        # 2) Fallback: minimal chat completion (1 token)
        try:
            resp = self._client.post(
                f"{self.base_url}/chat/completions",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                    "temperature": 0.0,
                },
                headers=headers,
                timeout=timeout,
            )
            if resp.status_code == 200:
                return {
                    "ok": True,
                    "status": 200,
                    "detail": f"connected to {self.base_url} via chat completion",
                    "model": self.model,
                }
            return {
                "ok": False,
                "status": resp.status_code,
                "detail": f"chat completion returned {resp.status_code}: {resp.text[:200]}",
                "model": self.model,
            }
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "status": None,
                "detail": f"cannot reach {self.base_url}: {exc}",
                "model": self.model,
            }

    def close(self) -> None:
        self._client.close()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: LLMClient | None = None


def get_client() -> LLMClient:
    """Return (or create) the module-level LLMClient singleton."""
    global _instance
    if _instance is None:
        base_url, model, api_key = _detect_provider()
        log.info("LLM provider: %s  model: %s", base_url, model)
        _instance = LLMClient(base_url, model, api_key)
    return _instance
