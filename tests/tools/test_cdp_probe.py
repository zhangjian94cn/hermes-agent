from __future__ import annotations

import json
import urllib.error

from tools import cdp_probe


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self, _limit=-1):
        return json.dumps(self.payload).encode("utf-8")


def test_normalize_cdp_endpoint_accepts_version_or_list_urls():
    assert cdp_probe.normalize_cdp_endpoint("http://127.0.0.1:9222/json/version") == "http://127.0.0.1:9222"
    assert cdp_probe.normalize_cdp_endpoint("http://127.0.0.1:9222/json/list") == "http://127.0.0.1:9222"
    assert cdp_probe.endpoint_to_version_url("http://127.0.0.1:9222/json/list") == "http://127.0.0.1:9222/json/version"


def test_probe_cdp_endpoint_accepts_target_identity_when_version_is_generic(monkeypatch):
    def fake_urlopen(url, timeout=1.0):
        if url == "http://127.0.0.1:9222/json/version":
            return _FakeResponse({"Browser": "Chrome/148.0.7778.179", "User-Agent": "Chrome/148 Safari/537.36"})
        if url == "http://127.0.0.1:9222/json/list":
            return _FakeResponse(
                [
                    {
                        "type": "page",
                        "title": "Codex",
                        "url": "app://-/index.html",
                        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/test",
                    }
                ]
            )
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(cdp_probe.urllib.request, "urlopen", fake_urlopen)

    status = cdp_probe.probe_cdp_endpoint(
        "http://127.0.0.1:9222",
        app_name="Codex",
        identity_keywords=("codex",),
    )

    assert status["ok"] is True
    assert status["identity"] == {"version": False, "target": True, "source": "target"}
    assert status["matching_target_count"] == 1


def test_probe_cdp_endpoint_requires_inspectable_target_even_when_version_matches(monkeypatch):
    def fake_urlopen(url, timeout=1.0):
        if url == "http://127.0.0.1:9222/json/version":
            return _FakeResponse({"User-Agent": "Codex/26 Electron/42"})
        if url == "http://127.0.0.1:9222/json/list":
            return _FakeResponse([])
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(cdp_probe.urllib.request, "urlopen", fake_urlopen)

    status = cdp_probe.probe_cdp_endpoint(
        "http://127.0.0.1:9222",
        app_name="Codex",
        identity_keywords=("codex",),
    )

    assert status["ok"] is False
    assert status["identity"] == {"version": True, "target": False, "source": "version"}
    assert status["inspectable_count"] == 0
    assert "No inspectable Codex page targets" in status["error"]
