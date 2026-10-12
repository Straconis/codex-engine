"""Cloud formatting must be opt-in, preserve text and keep credentials private."""
import io
import json
import sys
import urllib.error
import pytest
from codex_engine import ai_format, deepseek
from codex_engine.settings import AppSettings, SettingsStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    # Test storage behavior independently of Windows DPAPI availability.
    import codex_engine.settings as settings
    monkeypatch.setattr(settings, "protect", lambda key: "encrypted:" + key[::-1])
    monkeypatch.setattr(settings, "unprotect", lambda value: value.removeprefix("encrypted:")[::-1])
    return SettingsStore(tmp_path / "settings.json")


def test_existing_settings_and_key_do_not_enable_cloud(store):
    store.path.write_text('{"ai_enabled": true, "manage_ollama": false}')
    s = store.update({"deepseek_api_key": "test-private-key"})
    assert s.ai_provider == "local" and not s.deepseek_enabled
    assert s.deepseek_api_key == "test-private-key"
    assert "test-private-key" not in store.path.read_text()
    assert "deepseek_key_encrypted" not in s.public()
    assert "deepseek_api_key" not in s.public()
    assert s.public()["deepseek_has_key"]
    store.update({"model": "another-model"})
    assert store.load().deepseek_api_key == "test-private-key"
    store.update({"deepseek_api_key": ""})
    assert not store.load().public()["deepseek_has_key"]


def test_dpapi_round_trip():
    if sys.platform != "win32":
        pytest.skip("Windows DPAPI")
    from codex_engine.secrets import protect, unprotect
    value = protect("test-only-secret")
    assert "test-only-secret" not in value
    assert unprotect(value) == "test-only-secret"


def response(text, finish="stop"):
    event = {"choices": [{"delta": {"content": text}, "finish_reason": finish}]}
    return io.BytesIO(("data: " + json.dumps(event) + "\n\ndata: [DONE]\n").encode())


def test_streaming_uses_fixed_endpoint_and_checks_text(monkeypatch):
    calls = []
    def open_request(req, **kwargs):
        calls.append(req)
        return response("## Original words.")
    monkeypatch.setattr(deepseek.urllib.request, "urlopen", open_request)
    client = deepseek.DeepSeekClient("test-key", "deepseek-flash")
    result = ai_format.format_markdown("Original words.", client=client,
        config=ai_format.AIConfig(model=client.cache_model, max_attempts=2))
    assert result.markdown == "## Original words." and result.formatted == 1
    assert result.model == "deepseek/deepseek-flash"
    assert calls[0].full_url == "https://api.deepseek.com/chat/completions"
    payload = json.loads(calls[0].data)
    assert payload["model"] == "deepseek-flash" and payload["stream"]
    assert payload["thinking"] == {"type": "disabled"}


def test_rewritten_output_rejected_after_two_attempts(monkeypatch):
    calls = []
    def open_request(req, **kwargs):
        calls.append(req)
        return response("Completely different text.")
    monkeypatch.setattr(deepseek.urllib.request, "urlopen", open_request)
    client = deepseek.DeepSeekClient("test-key", "deepseek-flash")
    result = ai_format.format_markdown("Original words.", client=client,
        config=ai_format.AIConfig(model=client.cache_model, max_attempts=2))
    assert result.markdown == "Original words." and result.failed
    assert len(calls) == 2


@pytest.mark.parametrize("code", [401, 402, 429, 500])
def test_errors_do_not_expose_credentials(monkeypatch, code):
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError("https://api.deepseek.com", code, "test-private-key", {}, None)
    monkeypatch.setattr(deepseek.urllib.request, "urlopen", fail)
    with pytest.raises(ai_format.AIUnavailable) as exc:
        deepseek.DeepSeekClient("test-private-key", "deepseek-flash").chat("m", [])
    assert "test-private-key" not in str(exc.value)
    if code == 402:
        assert "billing" in str(exc.value)


def test_cancelled_call_sends_nothing(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("Cancelled call must not make a request")
    monkeypatch.setattr(deepseek.urllib.request, "urlopen", fail)
    with pytest.raises(ai_format.AICancelled):
        deepseek.DeepSeekClient("key", "model").chat("m", [], should_stop=lambda: True)


def test_balance_check_sends_no_text(monkeypatch):
    def open_request(req, **kwargs):
        assert req.full_url == "https://api.deepseek.com/user/balance"
        assert req.data is None
        return io.BytesIO(b'{"is_available":true,"balance_infos":[{"currency":"USD","total_balance":"1.25"}]}')
    monkeypatch.setattr(deepseek.urllib.request, "urlopen", open_request)
    result = deepseek.DeepSeekClient("key", "model").balance()
    assert result == {"available": True, "balance": "USD 1.25"}


def test_api_opt_in_status_and_disable(store, monkeypatch):
    from fastapi.testclient import TestClient
    from codex_engine import app as module
    monkeypatch.setattr(module, "settings_store", store)
    monkeypatch.setattr(module.local_ai, "apply", lambda **kwargs: None)
    monkeypatch.setattr(module.local_ai, "ensure_running", lambda: None)
    def fail(*args, **kwargs):
        pytest.fail("Status and disabled formatting must never contact DeepSeek")
    monkeypatch.setattr(deepseek.urllib.request, "urlopen", fail)
    client = TestClient(module.app, base_url="http://127.0.0.1")
    headers = {"X-Codex-Engine-Client": "1"}
    result = client.put("/api/settings", headers=headers, json={"ai_provider": "deepseek", "deepseek_api_key": "key"})
    assert result.status_code == 200
    assert not client.get("/api/ai/status").json()["available"]
    original = store.load()
    store.update({"deepseek_enabled": True})
    assert client.get("/api/ai/status").json()["available"]
    original = store.load()
    assert module._deepseek_still_enabled(original)
    store.update({"deepseek_enabled": False})
    assert not module._deepseek_still_enabled(original)
    assert not client.get("/api/ai/status").json()["available"]
    assert "deepseek_key_encrypted" not in client.get("/api/settings").json()["settings"]


def test_incomplete_stream_is_rejected(monkeypatch):
    data=b'data: {"choices":[{"delta":{"content":"Original words."},"finish_reason":null}]}\n\n'
    monkeypatch.setattr(deepseek.urllib.request,"urlopen",lambda *a,**k:io.BytesIO(data))
    with pytest.raises(ai_format.AIUnavailable,match="complete"):
        deepseek.DeepSeekClient("key","model").chat("m",[])


def test_cancel_after_last_reply_never_accepts_or_caches_it():
    stop=[False]
    class Client:
        def list_models(self):return ["model"]
        def chat(self,*args,**kwargs):
            stop[0]=True
            return "# Original words."
    with pytest.raises(ai_format.AICancelled):
        ai_format.format_markdown("Original words.",client=Client(),config=ai_format.AIConfig(model="model"),should_stop=lambda:stop[0])


def test_estimate_uses_peak_rates_and_unknown_model_is_explicit():
    from codex_engine import cost_estimate as ce
    data=ce.estimate(["Original words."],"deepseek-flash")
    assert data["estimated_usd"]>0
    assert data["higher_estimate_usd"]>=data["estimated_usd"]
    assert ce.estimate(["text"],"unknown-model")["estimated_usd"] is None


def test_approval_is_bound_to_identity_single_use_and_expires(monkeypatch):
    from codex_engine import cost_estimate as ce
    q=ce.issue((1,2,"hash"),["text"],"deepseek-flash")
    assert ce.consume(q["approval_token"],(1,2,"hash"))
    assert not ce.consume(q["approval_token"],(1,2,"hash"))
    q=ce.issue((1,2,"hash"),["text"],"deepseek-flash")
    assert not ce.consume(q["approval_token"],(1,3,"hash"))
    q=ce.issue((1,2,"hash"),["text"],"deepseek-flash")
    now=ce.time.monotonic()
    monkeypatch.setattr(ce.time,"monotonic",lambda:now+ce.TTL+1)
    assert not ce.consume(q["approval_token"],(1,2,"hash"))
