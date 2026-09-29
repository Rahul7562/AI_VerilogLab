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


# Free-tier OpenRouter models are individually rate-limited, and the
# rate limit is per-model: a 429 on the primary is not a signal that the
# whole key is out of budget. Rotating to the next model keeps long
# generation runs alive instead of failing every candidate at once.
#
# Ordered by verified RTL-generation quality: these four were confirmed to
# return parseable JSON with both modules for a counter prompt. Models that
# returned prose or empty content are kept last so they are only reached
# when everything better is rate-limited.
#
# Override with VLAB_LLM_FALLBACKS (comma-separated, may be empty to
# disable rotation).
FALLBACK_MODELS: tuple[str, ...] = (
    "cohere/north-mini-code:free",
    "inclusionai/ling-3.0-flash-sante:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "liquid/lfm-2.5-2.6b:free",
    "poolside/laguna-xs-2.1:free",
    "qwen/qwen3.8-27b:free",
    "google/gemma-4-26b-a4b-it:free",
)


def fallback_models() -> list[str]:
    raw = os.getenv("VLAB_LLM_FALLBACKS")
    if raw is not None:
        return [m.strip() for m in raw.split(",") if m.strip()]
    return list(FALLBACK_MODELS)


def _is_model_scoped_error(msg: str) -> bool:
    """True when the failure is this model's fault, not the key's."""
    low = msg.lower()
    if "401" in low or "invalid api key" in low or "unauthorized" in low:
        return False
    if "402" in low or "insufficient credits" in low or "insufficient_quota" in low:
        return False
    return (
        "429" in msg
        or "rate limit" in low
        or "temporarily rate" in low
        or "503" in msg
        or "502" in msg
        or "504" in msg
        or "502_bad_gateway" in low
        or "no endpoints available" in low
        or "overloaded" in low
    )


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

    NOTE: OpenRouter rejects a request carrying BOTH `effort` and
    `max_tokens` inside `reasoning` (HTTP 400), so exactly one is sent.
    """
    effort = os.getenv("VLAB_REASONING_EFFORT", "low").strip().lower()
    if effort in ("off", "none", "disabled"):
        return None
    if effort == "exclude":
        return {"reasoning": {"exclude": True}}
    if effort == "tokens":
        try:
            budget = int(os.getenv("VLAB_REASONING_MAX_TOKENS", "512"))
        except ValueError:
            budget = 512
        return {"reasoning": {"max_tokens": budget}} if budget > 0 else None
    if effort:
        return {"reasoning": {"effort": effort}}
    return None


def chat(
    messages: list[dict[str, str]],
    max_tokens: int = 2048,
    temperature: float = 0.2,
    timeout: int = 90,
    retries: int | None = None,
) -> str:
    """Single chat completion via OpenRouter. Raises on failure.

    retries None = VLAB_LLM_RETRIES (default 2) attempts PER MODEL. A
    model-scoped failure (429 / 5xx / no endpoints) rotates to the next
    model in the fallback pool instead of sleeping through the same
    rate limit, so one saturated free model cannot end a whole run.
    """
    if retries is None:
        retries = _default_retries()
    key = get_api_key()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set (env or .env).")
    from openai import OpenAI

    client = OpenAI(
        base_url=os.getenv(
            "OPENROUTER_BASE_URL", config.OPENROUTER_BASE_URL
        ),
        api_key=key,
        timeout=timeout,
    )
    extra: dict[str, Any] = {}
    reasoning = _reasoning_params()
    if reasoning:
        extra["extra_body"] = reasoning

    primary = get_model()
    queue: list[str] = [primary]
    if _fallbacks_enabled():
        for m in fallback_models():
            if m and m != primary and m not in queue:
                queue.append(m)

    last_err: Exception | None = None
    last_model = primary
    for model in queue:
        boosted = False  # one free budget bump on length cutoffs
        attempts_left = retries + 1
        while attempts_left > 0:
            _throttle()
            try:
                resp = client.chat.completions.create(
                    model=model,
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
                        continue  # thinking ate the budget; retry bigger
                    last_err = RuntimeError(
                        f"empty model reply (finish={finish})"
                    )
                    last_model = model
                    if _is_empty_reply(finish):
                        break  # this model cannot answer; try the next one
                    attempts_left -= 1
                    continue
                return text.strip()
            except Exception as e:  # noqa: BLE001 - retry transient, surface rest
                last_err = e
                last_model = model
                msg = str(e)
                if extra and "400" in msg and (
                    "reasoning" in msg.lower() or "only one of" in msg.lower()
                ):
                    extra = {}  # provider rejected the param; retry bare
                    continue
                if "401" in msg or "auth" in msg.lower() or "api key" in msg.lower():
                    raise RuntimeError(f"OpenRouter auth failed: {msg}"[:400])
                if _is_model_scoped_error(msg):
                    used = (retries + 1) - attempts_left
                    if attempts_left > 1:
                        # brief backoff before trying the same model again
                        time.sleep(
                            min(20.0, _retry_after_seconds(e, 2.0 * (2**used)))
                        )
                    attempts_left -= 1
                    continue
                attempts_left -= 1
    # Truncate only the provider error, never the prefix: cutting the whole
    # concatenation to 500 chars discarded the most diagnostic part of it.
    raise RuntimeError(
        f"LLM call failed (tried {len(queue)} model(s), last={last_model}): "
        f"{str(last_err)[:400]}"
    )


def _fallbacks_enabled() -> bool:
    return bool(fallback_models())


def _is_empty_reply(finish: str | None) -> bool:
    """Empty content with no length cutoff means the model produced nothing
    usable for this prompt — rotating beats retrying the same model."""
    return finish != "length"


def check_llm(max_tokens: int = 64, probe_models: bool = False) -> tuple[bool, str]:
    """Minimal live call to validate key + model. Returns (ok, message).

    probe_models=True additionally sweeps the fallback pool and reports
    which models are currently answering — the single most useful signal
    when a free tier is rate-limiting.
    """
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
    if not probe_models:
        return True, f"model={get_model()} reply={text[:200]!r}"
    return True, f"model={get_model()} reply={text[:120]!r}\n" + _probe_pool()


def _probe_model(model: str, timeout: int = 45) -> tuple[str, str]:
    """One tiny call against a specific model. Returns (model, verdict)."""
    try:
        from openai import OpenAI

        client = OpenAI(
            base_url=os.getenv("OPENROUTER_BASE_URL", config.OPENROUTER_BASE_URL),
            api_key=get_api_key(),
            timeout=timeout,
        )
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with exactly: vlab-ok"}],
            max_tokens=64,
            temperature=0.0,
            extra_headers=_headers(),
        )
        txt = (resp.choices[0].message.content if resp.choices else "") or ""
        return model, "ok" if txt.strip() else "empty"
    except Exception as e:  # noqa: BLE001 - probe never raises
        msg = str(e)
        if "429" in msg or "rate limit" in msg.lower():
            return model, "429"
        if "403" in msg or "forbidden" in msg.lower():
            return model, "403"
        return model, f"err {msg[:60]}"


def _probe_pool() -> str:
    import concurrent.futures

    models = [get_model(), *[m for m in fallback_models() if m != get_model()]]
    lines: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for model, verdict in ex.map(lambda m: _probe_model(m), models):
            lines.append(f"  {'OK ' if verdict == 'ok' else '-- '}{model:55s} {verdict}")
    return "model availability:\n" + "\n".join(lines)
