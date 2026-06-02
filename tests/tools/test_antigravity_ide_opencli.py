from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tools import antigravity_ide_tool


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["python3", "antigravity-ide-agentapi.py"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_status_invokes_read_only_ide_helper(monkeypatch, tmp_path: Path):
    helper = tmp_path / "antigravity-ide-agentapi.py"
    helper.write_text("# helper\n", encoding="utf-8")
    calls = []

    monkeypatch.setattr(antigravity_ide_tool, "_script_path", lambda: helper)
    monkeypatch.setattr(antigravity_ide_tool, "_python_bin", lambda: "python3")

    def fake_run(command, timeout):
        calls.append((command, timeout))
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "action": "status",
                    "source": "opencli-agentapi-ide",
                    "capabilities": {"new_conversation": False},
                }
            )
        )

    monkeypatch.setattr(antigravity_ide_tool, "_run_helper", fake_run)

    result = json.loads(antigravity_ide_tool.handle_antigravity_ide_opencli({"action": "status", "last": 1}))

    assert result["ok"] is True
    assert result["source"] == "opencli-agentapi-ide"
    assert result["capabilities"]["new_conversation"] is False
    assert calls == [(["python3", str(helper), "status", "--limit", "1", "--format", "json"], 25.0)]


def test_new_conversation_uses_helper_cdp_route(monkeypatch, tmp_path: Path):
    helper = tmp_path / "antigravity-ide-agentapi.py"
    helper.write_text("# helper\n", encoding="utf-8")
    calls = []

    monkeypatch.setattr(antigravity_ide_tool, "_script_path", lambda: helper)
    monkeypatch.setattr(antigravity_ide_tool, "_python_bin", lambda: "python3")

    def fake_run(command, timeout):
        calls.append((command, timeout))
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "action": "ask",
                    "source": "opencli-cdp-ide",
                    "transport": "cdp",
                    "new_conversation": True,
                    "conversation_id": "new-123",
                    "reply": "hello OK",
                }
            ),
        )

    monkeypatch.setattr(antigravity_ide_tool, "_run_helper", fake_run)

    result = json.loads(
        antigravity_ide_tool.handle_antigravity_ide_opencli(
            {"action": "ask", "message": "hello", "new_conversation": True, "wait": True, "timeout": 12}
        )
    )

    assert result["ok"] is True
    assert result["source"] == "opencli-cdp-ide"
    assert result["new_conversation"] is True
    assert result["conversation_id"] == "new-123"
    assert calls == [
        (
            [
                "python3",
                str(helper),
                "ask",
                "hello",
                "--wait",
                "true",
                "--timeout",
                "12",
                "--read-last",
                "8",
                "--format",
                "json",
                "--new-conversation",
                "true",
            ],
            17.0,
        )
    ]


def test_send_existing_conversation_uses_conversation_id(monkeypatch, tmp_path: Path):
    helper = tmp_path / "antigravity-ide-agentapi.py"
    helper.write_text("# helper\n", encoding="utf-8")
    calls = []

    monkeypatch.setattr(antigravity_ide_tool, "_script_path", lambda: helper)
    monkeypatch.setattr(antigravity_ide_tool, "_python_bin", lambda: "python3")

    def fake_run(command, timeout):
        calls.append((command, timeout))
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "action": "send",
                    "source": "opencli-agentapi-ide",
                    "conversation_id": "abc-123",
                    "reply": "done",
                }
            )
        )

    monkeypatch.setattr(antigravity_ide_tool, "_run_helper", fake_run)

    result = json.loads(
        antigravity_ide_tool.handle_antigravity_ide_opencli(
            {
                "action": "send",
                "conversation_id": "abc-123",
                "message": "hello",
                "wait": True,
                "timeout": 9,
                "last": 4,
            }
        )
    )

    assert result["ok"] is True
    assert result["reply"] == "done"
    assert calls == [
        (
            [
                "python3",
                str(helper),
                "send",
                "hello",
                "--wait",
                "true",
                "--timeout",
                "9",
                "--read-last",
                "4",
                "--format",
                "json",
                "--conversation-id",
                "abc-123",
            ],
            14.0,
        )
    ]


def test_toolset_contains_antigravity_ide_tool():
    from toolsets import TOOLSETS, _HERMES_CORE_TOOLS, resolve_toolset

    assert "antigravity_ide_opencli" in _HERMES_CORE_TOOLS
    assert TOOLSETS["antigravity-ide"]["tools"] == ["antigravity_ide_opencli"]
    assert resolve_toolset("antigravity-ide") == ["antigravity_ide_opencli"]
