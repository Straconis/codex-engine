"""Verified downloads, local inference, settings changes, and process teardown."""
import dataclasses
import hashlib
import io
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
import pytest
from codex_engine import local_manager as lm
from codex_engine.local_catalog import DEFAULT_MODEL, MODEL
from codex_engine.settings import SettingsStore
from codex_engine import ai_format

PAYLOAD = b"GGUF-test-model"

@pytest.fixture
def small_asset(monkeypatch):
    asset=dataclasses.replace(MODEL,size=len(PAYLOAD),sha256=hashlib.sha256(PAYLOAD).hexdigest())
    monkeypatch.setattr(lm,"CATALOG",{DEFAULT_MODEL:asset})
    return asset


def manager(tmp_path, **settings):
    store=SettingsStore(tmp_path/"settings.json")
    store.update({"models_dir":str(tmp_path/"models"),**settings})
    return lm.LocalModelManager(store,tmp_path,tmp_path/"logs")


def install(m, asset):
    path=m.models_folder()/asset.filename
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(PAYLOAD)
    stat=path.stat()
    path.with_suffix(".verified.json").write_text(json.dumps({"sha256":asset.sha256,"size":stat.st_size,"mtime_ns":stat.st_mtime_ns}))
    return path


def test_no_model_does_not_download_or_require_ollama(tmp_path,monkeypatch):
    m=manager(tmp_path)
    monkeypatch.setattr(lm.urllib.request,"urlopen",lambda *a,**k:pytest.fail("No automatic model downloads"))
    m.ensure_running()
    assert m.status()["state"]=="missing_model"
    assert "install" in m.status()["error"]
    assert not m.status()["available"]


def test_old_ollama_setting_migrates_to_local(tmp_path):
    store=SettingsStore(tmp_path/"settings.json")
    store.path.write_text('{"ai_provider":"ollama","manage_ollama":true,"model":"qwen2.5:1.5b"}')
    assert store.load().ai_provider=="local"
    assert store.load().local_model==DEFAULT_MODEL


def test_download_verified_before_use_and_duplicate_reuses_file(tmp_path,small_asset,monkeypatch):
    m=manager(tmp_path,ai_enabled=False)
    monkeypatch.setattr(lm.urllib.request,"urlopen",lambda *a,**k:io.BytesIO(PAYLOAD))
    pull=lm.Download(DEFAULT_MODEL,total=len(PAYLOAD))
    m._download(pull,m.models_folder())
    assert pull.done and not pull.error and m.installed_models()==[DEFAULT_MODEL]
    monkeypatch.setattr(lm.urllib.request,"urlopen",lambda *a,**k:pytest.fail("Already downloaded"))
    assert m.pull(DEFAULT_MODEL).done
    assert not list(m.models_folder().glob("*.part"))


def test_bad_download_never_replaces_existing_model(tmp_path,small_asset,monkeypatch):
    m=manager(tmp_path,ai_enabled=False)
    path=install(m,small_asset)
    monkeypatch.setattr(lm.urllib.request,"urlopen",lambda *a,**k:io.BytesIO(b"bad-model"))
    pull=lm.Download(DEFAULT_MODEL,total=len(PAYLOAD))
    m._download(pull,m.models_folder())
    assert pull.done and pull.error and path.read_bytes()==PAYLOAD
    assert m.installed_models()==[DEFAULT_MODEL]
    assert not list(m.models_folder().glob("*.part"))


def test_cancel_download_cleans_partial_and_does_not_install(tmp_path,small_asset,monkeypatch):
    m=manager(tmp_path)
    m._download_cancel.set()
    monkeypatch.setattr(lm.urllib.request,"urlopen",lambda *a,**k:io.BytesIO(PAYLOAD))
    pull=lm.Download(DEFAULT_MODEL,total=len(PAYLOAD))
    m._download(pull,m.models_folder())
    assert pull.done and "cancelled" in pull.error
    assert m.installed_models()==[] and not list(m.models_folder().glob("*.part"))


def test_changed_file_no_longer_counts_as_verified(tmp_path,small_asset):
    m=manager(tmp_path)
    path=install(m,small_asset)
    path.write_bytes(b"tampered"+PAYLOAD)
    assert m.installed_models()==[]


def test_missing_runtime_is_an_app_repair_not_an_ollama_install(tmp_path,small_asset,monkeypatch):
    m=manager(tmp_path)
    install(m,small_asset)
    monkeypatch.setattr(lm,"runtime_path",lambda:tmp_path/"not-present.exe")
    m.ensure_running()
    assert m.status()["state"]=="error"
    assert "Reinstall Codex Engine" in m.status()["error"]
    assert "Ollama" not in m.status()["error"]


class FakeManager(lm.LocalModelManager):
    def _command(self,exe,model,port,key_file):
        return [sys.executable,str(Path(__file__).with_name("fake_local_server.py")),"--port",str(port),"--key-file",str(key_file)]


def test_managed_runtime_auth_stream_and_shutdown(tmp_path,small_asset,monkeypatch):
    store=SettingsStore(tmp_path/"settings.json")
    m=FakeManager(store,tmp_path,tmp_path/"logs")
    install(m,small_asset)
    monkeypatch.setattr(lm,"runtime_path",lambda:Path(sys.executable))
    try:
        m.ensure_running(timeout=15)
        assert m.status()["available"]
        assert m._url.startswith("http://127.0.0.1:")
        req=urllib.request.Request(m._url+"/v1/chat/completions",data=b'{}',headers={"Content-Type":"application/json"})
        with pytest.raises(urllib.error.HTTPError) as exc:urllib.request.urlopen(req)
        assert exc.value.code==401
        result=ai_format.format_markdown("Original words.",client=m.client(),config=m.config())
        assert result.markdown=="## Original words." and result.formatted==1
        proc=m._proc
        store.update({"ai_enabled":False})
        m.apply(start=False)
        assert proc.poll() is not None and not m.status()["available"]
        assert not (tmp_path/"local-runtime.key").exists()
    finally:m.stop()


def test_unknown_model_never_becomes_a_path(tmp_path):
    m=manager(tmp_path)
    with pytest.raises(ValueError):m.pull("../../outside.gguf")
