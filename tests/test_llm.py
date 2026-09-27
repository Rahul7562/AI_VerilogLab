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


def test_chat_backs_off_on_429(monkeypatch):
    import vlab.llm as llm_mod

    calls, sleeps = [], []

    class R429Completions:
        def create(self, **kw):
            calls.append(kw)
            if len(calls) < 3:
                err = RuntimeError("429 rate limited, slow down")
                err.response = types.SimpleNamespace(headers={"retry-after": "7"})
                raise err
            msg = types.SimpleNamespace(content="late-ok")
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=msg, finish_reason="stop")])

    mod = types.ModuleType("openai")
    mod.OpenAI = lambda **kw: types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=R429Completions()))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("VLAB_REASONING_EFFORT", "off")
    monkeypatch.setitem(sys.modules, "openai", mod)
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: sleeps.append(s))
    assert llm_mod.chat([{"role": "user", "content": "hi"}], retries=3) == "late-ok"
    assert sleeps == [7.0, 7.0]  # Retry-After honored, no busy retry


def test_chat_boosts_budget_on_length_cutoff(monkeypatch):
    import vlab.llm as llm_mod

    seen_max = []
    msg = types.SimpleNamespace(content="finally")

    class LenCompletions:
        def create(self, **kw):
            seen_max.append(kw.get("max_tokens"))
            if len(seen_max) == 1:
                return types.SimpleNamespace(choices=[
                    types.SimpleNamespace(message=types.SimpleNamespace(content=" "),
                                          finish_reason="length")])
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=msg, finish_reason="stop")])

    mod = types.ModuleType("openai")
    mod.OpenAI = lambda **kw: types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=LenCompletions()))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("VLAB_REASONING_EFFORT", "off")
    monkeypatch.setitem(sys.modules, "openai", mod)
    assert llm_mod.chat([{"role": "user", "content": "hi"}], retries=0,
                        max_tokens=100) == "finally"
    assert seen_max == [100, 200]  # one free retry with doubled budget


def test_default_retries_env(monkeypatch):
    import vlab.llm as llm_mod

    monkeypatch.delenv("VLAB_LLM_RETRIES", raising=False)
    assert llm_mod._default_retries() == 2
    monkeypatch.setenv("VLAB_LLM_RETRIES", "0")
    assert llm_mod._default_retries() == 0
    monkeypatch.setenv("VLAB_LLM_RETRIES", "bogus")
    assert llm_mod._default_retries() == 2


def test_reasoning_params_default_and_off(monkeypatch):
    import vlab.llm as llm_mod

    monkeypatch.delenv("VLAB_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("VLAB_REASONING_MAX_TOKENS", raising=False)
    p = llm_mod._reasoning_params()
    assert p == {"reasoning": {"effort": "low", "max_tokens": 512}}
    monkeypatch.setenv("VLAB_REASONING_EFFORT", "off")
    assert llm_mod._reasoning_params() is None
    monkeypatch.setenv("VLAB_REASONING_EFFORT", "exclude")
    assert llm_mod._reasoning_params() == {"reasoning": {"exclude": True}}


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
