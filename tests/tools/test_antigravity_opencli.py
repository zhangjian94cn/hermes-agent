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


def test_build_conversations_command_uses_dump_json(monkeypatch):
    monkeypatch.setattr(antigravity_tool, "_resolve_opencli_command", lambda: ["opencli"])

    command = antigravity_tool._build_opencli_command("conversations", {})

    assert command == ["opencli", "antigravity", "conversations", "--format", "json"]


def test_build_dump_requires_diagnostic(monkeypatch):
    monkeypatch.setattr(antigravity_tool, "_resolve_opencli_command", lambda: ["opencli"])

    try:
        antigravity_tool._build_opencli_command("dump", {})
    except ValueError as exc:
        assert "diagnostic-only" in str(exc)
        assert "conversations" in str(exc)
    else:
        raise AssertionError("dump without diagnostic=true should fail")


def test_build_dump_allows_explicit_diagnostic(monkeypatch):
    monkeypatch.setattr(antigravity_tool, "_resolve_opencli_command", lambda: ["opencli"])

    command = antigravity_tool._build_opencli_command("dump", {"diagnostic": True})

    assert command == ["opencli", "antigravity", "dump", "--format", "json"]


def test_build_state_command_uses_read_last(monkeypatch):
    monkeypatch.setattr(antigravity_tool, "_resolve_opencli_command", lambda: ["opencli"])

    command = antigravity_tool._build_opencli_command("state", {"read_last": 3})

    assert command == ["opencli", "antigravity", "state", "--last", "3", "--format", "json"]


def test_build_open_conversation_command_uses_target(monkeypatch):
    monkeypatch.setattr(antigravity_tool, "_resolve_opencli_command", lambda: ["opencli"])

    command = antigravity_tool._build_opencli_command(
        "open_conversation",
        {"target": "Reviewing Project Pull Requests"},
    )

    assert command == [
        "opencli",
        "antigravity",
        "open",
        "Reviewing Project Pull Requests",
        "--format",
        "json",
    ]


def test_build_ask_command_composes_one_shot_options(monkeypatch):
    monkeypatch.setattr(antigravity_tool, "_resolve_opencli_command", lambda: ["opencli"])

    command = antigravity_tool._build_opencli_command(
        "ask",
        {
            "message": "Hello",
            "model": "Gemini 3.5 Flash Medium",
            "new_conversation": True,
            "wait": True,
            "timeout": 9,
            "read_last": 2,
        },
    )

    assert command == [
        "opencli",
        "antigravity",
        "ask",
        "Hello",
        "--model",
        "Gemini 3.5 Flash Medium",
        "--new-conversation",
        "true",
        "--wait",
        "true",
        "--timeout",
        "9",
        "--read-last",
        "2",
        "--format",
        "json",
    ]


def test_send_requires_message(monkeypatch):
    monkeypatch.setattr(antigravity_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(antigravity_tool, "_cdp_status", lambda: {"ok": True})

    result = json.loads(antigravity_tool.handle_antigravity_opencli({"action": "send"}))

    assert result["ok"] is False
    assert "message is required" in result["error"]


def test_ask_requires_message(monkeypatch):
    monkeypatch.setattr(antigravity_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(antigravity_tool, "_cdp_status", lambda: {"ok": True})

    result = json.loads(antigravity_tool.handle_antigravity_opencli({"action": "ask"}))

    assert result["ok"] is False
    assert "message is required" in result["error"]


def test_dump_without_diagnostic_redirects_to_conversations(monkeypatch):
    monkeypatch.setattr(antigravity_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(antigravity_tool, "_cdp_status", lambda: {"ok": True})

    result = json.loads(antigravity_tool.handle_antigravity_opencli({"action": "dump"}))

    assert result["ok"] is False
    assert result["action"] == "dump"
    assert "diagnostic-only" in result["error"]
    assert "conversations" in result["error"]
    assert "/tmp/antigravity-snapshot.json" in result["error"]


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


def test_handle_ask_returns_structured_result(monkeypatch):
    calls = []
    monkeypatch.setattr(antigravity_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(antigravity_tool, "_cdp_status", lambda: {"ok": True})

    def fake_run(command, timeout):
        calls.append((command, timeout))
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "action": "ask",
                    "steps": [{"name": "send", "ok": True}],
                    "state_before": {"title": "Before"},
                    "state_after": {"title": "After"},
                    "reply": "Hello response",
                }
            )
        )

    monkeypatch.setattr(antigravity_tool, "_run_opencli", fake_run)

    result = json.loads(
        antigravity_tool.handle_antigravity_opencli(
            {"action": "ask", "message": "Hello", "wait": True}
        )
    )

    assert result["ok"] is True
    assert result["action"] == "ask"
    assert result["steps"] == [{"name": "send", "ok": True}]
    assert result["reply"] == "Hello response"
    assert "payload" not in result
    assert calls == [
        (
            [
                "opencli",
                "antigravity",
                "ask",
                "Hello",
                "--wait",
                "true",
                "--timeout",
                "20",
                "--read-last",
                "5",
                "--format",
                "json",
            ],
            20.0,
        )
    ]


def test_failed_model_command_disallows_computer_use_fallback(monkeypatch):
    monkeypatch.setattr(antigravity_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(antigravity_tool, "_cdp_status", lambda: {"ok": True})

    def fake_run(command, timeout):
        return _completed(
            stdout=json.dumps(
                {
                    "ok": False,
                    "reason": "Model switch was not verified. Current model: Claude Opus 4.6",
                    "availableModels": ["Claude Opus 4.6", "Gemini 3.5 Flash (Medium)"],
                }
            ),
            returncode=1,
        )

    monkeypatch.setattr(antigravity_tool, "_run_opencli", fake_run)

    result = json.loads(
        antigravity_tool.handle_antigravity_opencli(
            {"action": "model", "model": "Gemini 3.5 Flash Medium"}
        )
    )

    assert result["ok"] is False
    assert result["fallback_allowed"] is False
    assert result["do_not_use_computer_use"] is True
    assert result["do_not_search_or_read_source"] is True
    assert result["fallback"] is None
    assert {item["action"] for item in result["next_actions"]} >= {"state", "models"}


def test_parse_json_payload_skips_non_json_prefix():
    parsed = antigravity_tool._parse_json_payload(
        'status line\n[{"role":"user","content":"hello"}]\nupdate notice'
    )

    assert parsed == [{"role": "user", "content": "hello"}]


def test_extract_conversations_from_snapshot():
    snapshot = """
url: https://127.0.0.1:60857/c/aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa?section=1
title: Active Session
viewport: 1352x847
---
  [10]<div role=button tabindex=0 aria-expanded=true />
    <div />
      <div>zhangjian-skills</div>
  [12]<span data-testid=convo-pill-aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa>Active Session</span>
    <div />
      <span>12m</span>
  [14]<span data-testid=convo-pill-bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb>Older &amp; Escaped</span>
    <div />
      <span>1d</span>
  [15]<button>See all (32)</button>
  [20]<div role=button tabindex=0 aria-expanded=true />
    <div />
      <div>lingxi-ai-framework</div>
  [22]<span data-testid=convo-pill-cccccccc-cccc-4ccc-cccc-cccccccccccc>Reviewing PRs</span>
    <div />
      <span>2d</span>
  [30]<button aria-label=Select model, current: Gemini 3.5 Flash Medium />
"""

    result = antigravity_tool._extract_antigravity_conversations_from_snapshot(
        json.dumps(snapshot)
    )

    assert result["current"]["conversation_id"] == "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"
    assert result["current"]["model"] == "Gemini 3.5 Flash Medium"
    assert result["visible_conversation_count"] == 3
    assert result["projects"][0]["name"] == "zhangjian-skills"
    assert result["projects"][0]["see_all_count"] == 32
    assert result["projects"][0]["conversations"][0]["current"] is True
    assert result["projects"][0]["conversations"][1]["title"] == "Older & Escaped"
    assert result["projects"][1]["name"] == "lingxi-ai-framework"


def test_handle_conversations_returns_compact_structured_result(monkeypatch):
    calls = []
    monkeypatch.setattr(antigravity_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(antigravity_tool, "_cdp_status", lambda: {"ok": True})

    def fake_run(command, timeout):
        calls.append((command, timeout))
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "visible_conversation_count": 1,
                    "projects": [
                        {
                            "name": "zhangjian-skills",
                            "conversations": [
                                {
                                    "id": "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa",
                                    "title": "Active Session",
                                    "time": "12m",
                                }
                            ],
                        }
                    ],
                }
            )
        )

    monkeypatch.setattr(antigravity_tool, "_run_opencli", fake_run)

    result = json.loads(
        antigravity_tool.handle_antigravity_opencli(
            {"action": "list_conversations", "timeout": 4}
        )
    )

    assert result["ok"] is True
    assert result["action"] == "conversations"
    assert result["visible_conversation_count"] == 1
    assert result["projects"][0]["conversations"][0]["title"] == "Active Session"
    assert "payload" not in result
    assert calls == [(["opencli", "antigravity", "conversations", "--format", "json"], 4.0)]


def test_toolset_contains_fast_antigravity_tool():
    from toolsets import TOOLSETS, _HERMES_CORE_TOOLS, resolve_toolset

    assert "antigravity_opencli" in _HERMES_CORE_TOOLS
    assert TOOLSETS["antigravity"]["tools"] == ["antigravity_opencli"]
    assert resolve_toolset("antigravity") == ["antigravity_opencli"]
