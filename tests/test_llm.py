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

    # The default cooldown abandons a model after 2 consecutive 429s, which
    # would rotate before the third call this test expects. Pin it high so
    # this test exercises the backoff schedule, not the rotation policy
    # (which test_repeated_429_abandons_a_saturated_model covers).
    monkeypatch.setenv("VLAB_LLM_429_COOLDOWN", "99")
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
    # throttle sleeps are real here; neutralise only the inter-call gate
    monkeypatch.setattr(llm_mod, "_throttle", lambda *a, **k: None)
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


def test_throttle_spacing(monkeypatch):
    import vlab.llm as llm_mod

    monkeypatch.delenv("VLAB_LLM_MIN_INTERVAL", raising=False)
    monkeypatch.delenv("VLAB_LLM_RPS", raising=False)
    assert llm_mod._min_interval() == 3.0
    sleeps = []
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: sleeps.append(s))
    now = [1000.0]
    monkeypatch.setattr(llm_mod.time, "monotonic", lambda: now[0])
    # this test drives the GLOBAL gate, so the ledger must not veto with a
    # 3s wait of its own
    monkeypatch.setattr(llm_mod, "_last_call_by_model", {})
    monkeypatch.setattr(llm_mod, "_last_call_ts", 999.0)  # 1s ago -> wait ~2s
    llm_mod._throttle()
    assert sleeps and abs(sleeps[0] - 2.0) < 0.01
    now[0] = 1000.0
    monkeypatch.setattr(llm_mod, "_last_call_ts", 990.0)  # 10s ago -> no wait
    llm_mod._throttle()
    assert len(sleeps) == 1


def test_reasoning_params_default_and_off(monkeypatch):
    import vlab.llm as llm_mod

    monkeypatch.delenv("VLAB_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("VLAB_REASONING_MAX_TOKENS", raising=False)
    p = llm_mod._reasoning_params()
    # OpenRouter 400s when effort and max_tokens are BOTH present, so the
    # default must send exactly one.
    assert p == {"reasoning": {"effort": "low"}}
    monkeypatch.setenv("VLAB_REASONING_EFFORT", "off")
    assert llm_mod._reasoning_params() is None
    monkeypatch.setenv("VLAB_REASONING_EFFORT", "exclude")
    assert llm_mod._reasoning_params() == {"reasoning": {"exclude": True}}
    # token-budget mode is opt-in and sends max_tokens alone
    monkeypatch.setenv("VLAB_REASONING_EFFORT", "tokens")
    monkeypatch.setenv("VLAB_REASONING_MAX_TOKENS", "256")
    assert llm_mod._reasoning_params() == {"reasoning": {"max_tokens": 256}}
    for effort in ("low", "medium", "high"):
        monkeypatch.setenv("VLAB_REASONING_EFFORT", effort)
        r = llm_mod._reasoning_params()["reasoning"]
        assert set(r) == {"effort"}, f"{effort} must send effort only"


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
    assert seen[0].get("extra_body") == {"reasoning": {"effort": "low"}}
    assert "extra_body" not in seen[1]  # fallback went bare


def test_chat_rotates_models_on_429(monkeypatch):
    """A rate-limited model must not end the run: rotate to the next one."""
    import vlab.llm as llm_mod

    used, sleeps = [], []

    class Rotating:
        def create(self, **kw):
            model = kw["model"]
            used.append(model)
            if model == "primary/model:free":
                raise RuntimeError("429 poolside/model is temporarily rate-limited")
            msg = types.SimpleNamespace(content="ok-from-backup")
            return types.SimpleNamespace(choices=[
                types.SimpleNamespace(message=msg, finish_reason="stop")])

    mod = types.ModuleType("openai")
    mod.OpenAI = lambda **kw: types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=Rotating()))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_MODEL", "primary/model:free")
    monkeypatch.setenv("VLAB_LLM_FALLBACKS", "backup/a:free,backup/b:free")
    monkeypatch.setenv("VLAB_REASONING_EFFORT", "off")
    monkeypatch.setitem(sys.modules, "openai", mod)
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: sleeps.append(s))

    assert llm_mod.chat([{"role": "user", "content": "hi"}], retries=0) == \
        "ok-from-backup"
    assert used == ["primary/model:free", "backup/a:free"]


def test_chat_does_not_rotate_on_auth_or_quota(monkeypatch):
    """401/402 are key-level: the error must surface, not be masked by rotation."""
    import vlab.llm as llm_mod

    class Boom:
        def create(self, **kw):
            raise RuntimeError("402 insufficient credits on this key")

    mod = types.ModuleType("openai")
    mod.OpenAI = lambda **kw: types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=Boom()))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("VLAB_LLM_FALLBACKS", "backup/a:free")
    monkeypatch.setenv("VLAB_REASONING_EFFORT", "off")
    monkeypatch.setitem(sys.modules, "openai", mod)
    with pytest.raises(RuntimeError) as ei:
        llm_mod.chat([{"role": "user", "content": "hi"}], retries=0)
    # the real cause is preserved in the message for the user to act on
    assert "insufficient credits" in str(ei.value)


def test_fallbacks_can_be_disabled(monkeypatch):
    import vlab.llm as llm_mod

    monkeypatch.setenv("VLAB_LLM_FALLBACKS", "")
    assert llm_mod.fallback_models() == []
    assert not llm_mod._fallbacks_enabled()
    monkeypatch.setenv("VLAB_LLM_FALLBACKS", "a:free, b:free ,")
    assert llm_mod.fallback_models() == ["a:free", "b:free"]


def test_is_model_scoped_error():
    import vlab.llm as llm_mod

    assert llm_mod._is_model_scoped_error("Error code: 429 - rate limited")
    assert llm_mod._is_model_scoped_error("503 no endpoints available")
    assert not llm_mod._is_model_scoped_error("401 invalid api key")
    assert not llm_mod._is_model_scoped_error("402 insufficient credits")
    assert not llm_mod._is_model_scoped_error("400 bad request")
