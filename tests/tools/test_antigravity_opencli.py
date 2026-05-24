from __future__ import annotations

import json
import subprocess

from tools import antigravity_tool


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["opencli"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_health_reports_opencli_and_cdp(monkeypatch):
    monkeypatch.setattr(antigravity_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(
        antigravity_tool,
        "_cdp_status",
        lambda timeout=1.5: {"ok": True, "url": "http://127.0.0.1:9234/json/version"},
    )

    result = json.loads(antigravity_tool.handle_antigravity_opencli({"action": "health"}))

    assert result["ok"] is True
    assert result["opencli"]["ok"] is True
    assert result["cdp"]["ok"] is True
    assert result["preferred_over"] == "computer_use"


def test_build_read_command_uses_json_and_last(monkeypatch):
    monkeypatch.setattr(antigravity_tool, "_resolve_opencli_command", lambda: ["opencli"])

    command = antigravity_tool._build_opencli_command("read", {"last": 2})

    assert command == ["opencli", "antigravity", "read", "--last", "2", "--format", "json"]


def test_send_requires_message(monkeypatch):
    monkeypatch.setattr(antigravity_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(antigravity_tool, "_cdp_status", lambda: {"ok": True})

    result = json.loads(antigravity_tool.handle_antigravity_opencli({"action": "send"}))

    assert result["ok"] is False
    assert "message is required" in result["error"]


def test_handle_read_parses_json_payload(monkeypatch):
    calls = []
    monkeypatch.setattr(antigravity_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(antigravity_tool, "_cdp_status", lambda: {"ok": True})

    def fake_run(command, timeout):
        calls.append((command, timeout))
        return _completed(stdout='[{"role":"assistant","content":"ready"}]')

    monkeypatch.setattr(antigravity_tool, "_run_opencli", fake_run)

    result = json.loads(
        antigravity_tool.handle_antigravity_opencli(
            {"action": "read", "last": 1, "timeout": 3}
        )
    )

    assert result["ok"] is True
    assert result["payload"] == [{"role": "assistant", "content": "ready"}]
    assert calls == [(["opencli", "antigravity", "read", "--last", "1", "--format", "json"], 3.0)]


def test_parse_json_payload_skips_non_json_prefix():
    parsed = antigravity_tool._parse_json_payload(
        'status line\n[{"role":"user","content":"hello"}]\nupdate notice'
    )

    assert parsed == [{"role": "user", "content": "hello"}]


def test_toolset_contains_fast_antigravity_tool():
    from toolsets import TOOLSETS, _HERMES_CORE_TOOLS, resolve_toolset

    assert "antigravity_opencli" in _HERMES_CORE_TOOLS
    assert TOOLSETS["antigravity"]["tools"] == ["antigravity_opencli"]
    assert resolve_toolset("antigravity") == ["antigravity_opencli"]
