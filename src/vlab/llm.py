"""OpenRouter client (Nemotron 3 Ultra free by default)."""
from __future__ import annotations

from . import config


def get_api_key() -> str:
    return config.OPENROUTER_API_KEY.strip()


def check_llm(max_tokens: int = 16) -> tuple[bool, str]:
    """Minimal live call to validate key + model. Returns (ok, message)."""
    key = get_api_key()
    if not key:
        return False, "OPENROUTER_API_KEY is not set (env or .env)."
    try:
        from openai import OpenAI
    except ImportError:
        return False, "openai package not installed. Run `uv sync`."
    try:
        client = OpenAI(base_url=config.OPENROUTER_BASE_URL, api_key=key)
        resp = client.chat.completions.create(
            model=config.DEFAULT_MODEL,
            messages=[{"role": "user", "content": "Reply with: vlab-ok"}],
            max_tokens=max_tokens,
            extra_headers={
                "HTTP-Referer": __import__("os").getenv(
                    "OPENROUTER_APP_URL", "http://localhost"
                ),
                "X-Title": __import__("os").getenv(
                    "OPENROUTER_APP_NAME", "AI-VerilogLab"
                ),
            },
        )
        text = (resp.choices[0].message.content or "").strip()[:200]
        return True, f"model={config.DEFAULT_MODEL} reply={text!r}"
    except Exception as e:  # noqa: BLE001 - surface to CLI
        return False, f"{type(e).__name__}: {e}"[:500]
