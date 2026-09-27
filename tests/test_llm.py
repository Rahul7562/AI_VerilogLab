"""LLM client edge cases (mocked OpenAI, no network)."""
import sys
import types

import pytest


def _fake_openai(choices, **kwargs):
    mod = types.ModuleType("openai")

    class FakeCompletions:
        def create(self, **kw):
            resp = types.SimpleNamespace(choices=choices)
            return resp

    class FakeChat:
        completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, **kw):
            self.chat = FakeChat()

    mod.OpenAI = FakeOpenAI
    return mod


def test_chat_rejects_empty_choices(monkeypatch):
    import vlab.llm as llm_mod

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "openai", _fake_openai(None))
    with pytest.raises(RuntimeError, match="LLM call failed"):
        llm_mod.chat([{"role": "user", "content": "hi"}], retries=0)
    monkeypatch.setitem(sys.modules, "openai", _fake_openai([]))
    with pytest.raises(RuntimeError, match="LLM call failed"):
        llm_mod.chat([{"role": "user", "content": "hi"}], retries=0)


def test_chat_trims_and_returns(monkeypatch):
    import vlab.llm as llm_mod

    msg = types.SimpleNamespace(content="  hello  ")
    choice = types.SimpleNamespace(message=msg, finish_reason="stop")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "openai", _fake_openai([choice]))
    assert llm_mod.chat([{"role": "user", "content": "hi"}]) == "hello"


def test_chat_no_retry_on_auth(monkeypatch):
    import vlab.llm as llm_mod

    calls = []

    class BoomCompletions:
        @staticmethod
        def create(**kw):
            calls.append(1)
            raise RuntimeError("401 unauthorized bad API key")

    mod = types.ModuleType("openai")
    mod.OpenAI = lambda **kw: types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=BoomCompletions))
    monkeypatch.setenv("OPENROUTER_API_KEY", "bad-key")
    monkeypatch.setitem(sys.modules, "openai", mod)
    with pytest.raises(RuntimeError):
        llm_mod.chat([{"role": "user", "content": "hi"}], retries=3)
    assert len(calls) == 1  # no retry on auth errors


def test_reasoning_params_default_and_off(monkeypatch):
    import vlab.llm as llm_mod

    monkeypatch.delenv("VLAB_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("VLAB_REASONING_MAX_TOKENS", raising=False)
    p = llm_mod._reasoning_params()
    assert p == {"reasoning": {"effort": "low", "max_tokens": 512}}
    monkeypatch.setenv("VLAB_REASONING_EFFORT", "off")
    assert llm_mod._reasoning_params() is None


def test_chat_passes_reasoning_and_falls_back(monkeypatch):
    import vlab.llm as llm_mod

    seen = []
    msg = types.SimpleNamespace(content="ok")
    choice = types.SimpleNamespace(message=msg, finish_reason="stop")

    class FakeCompletions:
        def create(self, **kw):
            seen.append(kw)
            if kw.get("extra_body") and len(seen) == 1:
                raise RuntimeError("400 bad request: reasoning not supported")
            return types.SimpleNamespace(choices=[choice])

    mod = types.ModuleType("openai")
    mod.OpenAI = lambda **kw: types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=FakeCompletions()))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.delenv("VLAB_REASONING_EFFORT", raising=False)
    monkeypatch.setitem(sys.modules, "openai", mod)
    assert llm_mod.chat([{"role": "user", "content": "hi"}], retries=0) == "ok"
    assert seen[0].get("extra_body") == {"reasoning": {"effort": "low", "max_tokens": 512}}
    assert "extra_body" not in seen[1]  # fallback went bare
