"""OpenRouter client (default model from config; free-tier friendly)."""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from . import config

# client-side throttle: minimum seconds between LLM calls so bursts of
# gen/repair calls never trip provider rate limits (default 3s).
_throttle_lock = threading.Lock()
_last_call_ts = 0.0


def _min_interval() -> float:
    try:
        return max(0.0, float(os.getenv("VLAB_LLM_MIN_INTERVAL", "3.0")))
    except ValueError:
        return 3.0


def _throttle() -> None:
    global _last_call_ts
    gap = _min_interval()
    if gap <= 0:
        return
    with _throttle_lock:
        now = time.monotonic()
        wait = gap - (now - _last_call_ts)
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _last_call_ts = now


def get_api_key() -> str:
    # read live from env so tests can monkeypatch OPENROUTER_API_KEY
    return os.getenv("OPENROUTER_API_KEY", config.OPENROUTER_API_KEY).strip()


def get_model() -> str:
    return os.getenv("OPENROUTER_MODEL", config.DEFAULT_MODEL).strip()


def _headers() -> dict[str, str]:
    return {
        "HTTP-Referer": os.getenv("OPENROUTER_APP_URL", "http://localhost"),
        "X-Title": os.getenv("OPENROUTER_APP_NAME", "AI-VerilogLab"),
    }


def _default_retries() -> int:
    try:
        return max(0, int(os.getenv("VLAB_LLM_RETRIES", "2")))
    except ValueError:
        return 2


def _retry_after_seconds(err: Exception, default: float) -> float:
    """Best-effort Retry-After extraction from provider errors."""
    try:
        resp = getattr(err, "response", None)
        headers = getattr(resp, "headers", None) or {}
        for key in ("retry-after", "Retry-After", "retry_after"):
            if key in headers:
                return max(1.0, min(300.0, float(headers[key])))
    except (TypeError, ValueError):
        pass
    return default


def _reasoning_params() -> dict[str, Any] | None:
    """Reasoning controls for thinking models (env-overridable).

    VLAB_REASONING_EFFORT values:
      off     - send no reasoning parameter (model default; thinking on)
      exclude - ask the provider to strip reasoning (closest to no thinking)
      low|medium|high - cap thinking effort (default: low)
    """
    effort = os.getenv("VLAB_REASONING_EFFORT", "low").strip().lower()
    if effort in ("off", "none", "disabled"):
        return None
    if effort == "exclude":
        return {"reasoning": {"exclude": True}}
    params: dict[str, Any] = {}
    if effort:
        params["effort"] = effort
    try:
        params["max_tokens"] = int(os.getenv("VLAB_REASONING_MAX_TOKENS", "512"))
    except ValueError:
        pass
    return {"reasoning": params} if params else None


def chat(
    messages: list[dict[str, str]],
    max_tokens: int = 2048,
    temperature: float = 0.2,
    timeout: int = 90,
    retries: int | None = None,
) -> str:
    """Single chat completion via OpenRouter. Raises on failure.

    retries None = VLAB_LLM_RETRIES (default 2). 429s back off with
    Retry-After/exponential waits; length cutoffs get one free budget boost.
    """
    if retries is None:
        retries = _default_retries()
    key = get_api_key()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set (env or .env).")
    _throttle()
    from openai import OpenAI

    client = OpenAI(
        base_url=os.getenv(
            "OPENROUTER_BASE_URL", config.OPENROUTER_BASE_URL
        ),
        api_key=key,
        timeout=timeout,
    )
    last_err: Exception | None = None
    extra: dict[str, Any] = {}
    reasoning = _reasoning_params()
    if reasoning:
        extra["extra_body"] = reasoning
    boosted = False  # one free retry with doubled budget on length cutoffs
    attempts_left = retries + 1
    while attempts_left > 0:
        try:
            resp = client.chat.completions.create(
                model=get_model(),
                messages=messages,  # type: ignore[arg-type]
                max_tokens=max_tokens,
                temperature=temperature,
                extra_headers=_headers(),
                **extra,  # type: ignore[arg-type]
            )
            choices = resp.choices or []
            text = (choices[0].message.content if choices else None) or ""
            if not text.strip():
                finish = getattr(
                    choices[0] if choices else None, "finish_reason", "?"
                )
                if finish == "length" and not boosted and max_tokens < 8192:
                    max_tokens = min(8192, max_tokens * 2)
                    boosted = True
                    continue  # thinking ate the budget; retry bigger, free
                raise RuntimeError(f"empty model reply (finish={finish})")
            return text.strip()
        except Exception as e:  # noqa: BLE001 - retry transient, surface rest
            last_err = e
            msg = str(e)
            if extra and "reasoning" in msg.lower() and "400" in msg:
                extra = {}  # provider rejected the param; free retry, bare
                continue
            if "401" in msg or "auth" in msg.lower() or "api key" in msg.lower():
                break
            if "429" in msg or "rate limit" in msg.lower():
                used = (retries + 1) - attempts_left
                wait = _retry_after_seconds(e, min(120.0, 15.0 * (2**used)))
                time.sleep(wait)
                attempts_left -= 1
                continue
            attempts_left -= 1
    raise RuntimeError(f"LLM call failed after {retries + 1} attempt(s): {last_err}"[:500])


def check_llm(max_tokens: int = 64) -> tuple[bool, str]:
    """Minimal live call to validate key + model. Returns (ok, message)."""
    if not get_api_key():
        return False, "OPENROUTER_API_KEY is not set (env or .env)."
    try:
        text = chat(
            [{"role": "user", "content": "Reply with exactly: vlab-ok"}],
            max_tokens=max_tokens,
            temperature=0.0,
        )
    except ImportError:
        return False, "openai package not installed. Run `uv sync`."
    except Exception as e:  # noqa: BLE001 - surface to CLI
        return False, f"{type(e).__name__}: {e}"[:500]
    return True, f"model={get_model()} reply={text[:200]!r}"
