"""OpenRouter client (Nemotron 3 Ultra free by default)."""
from __future__ import annotations

import os

from . import config


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


def chat(
    messages: list[dict[str, str]],
    max_tokens: int = 2048,
    temperature: float = 0.2,
    timeout: int = 90,
) -> str:
    """Single chat completion via OpenRouter. Raises on failure."""
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
    resp = client.chat.completions.create(
        model=get_model(),
        messages=messages,  # type: ignore[arg-type]
        max_tokens=max_tokens,
        temperature=temperature,
        extra_headers=_headers(),
    )
    return (resp.choices[0].message.content or "").strip()


def check_llm(max_tokens: int = 16) -> tuple[bool, str]:
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
