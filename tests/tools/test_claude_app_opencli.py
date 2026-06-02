from __future__ import annotations

import json
import subprocess

import pytest

from tools import claude_app_tool


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["opencli"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _current_patch_status():
    return {
        "patched_copy_required": True,
        "patched_path": "/Users/zjah/Applications/Claude-CDP.app",
        "patched_installed": True,
        "patched_copy_current": True,
        "patched_copy_reason": "current",
        "source": {
            "path": "/Applications/Claude.app",
            "version": "1.0.0",
            "app_asar_sha256": "source-sha",
        },
        "patched_source": {
            "version": "1.0.0",
            "app_asar_sha256": "source-sha",
            "stamp_path": "/Users/zjah/Applications/Claude-CDP.app/Contents/Resources/.opencli-cdp-source.json",
            "stamp_error": None,
        },
    }


def _stale_patch_status():
    status = _current_patch_status()
    status.update(
        {
            "patched_copy_current": False,
            "patched_copy_reason": "source-version-or-asar-sha-mismatch",
        }
    )
    status["source"] = {
        "path": "/Applications/Claude.app",
        "version": "2.0.0",
        "app_asar_sha256": "new-source-sha",
    }
    return status


def _trusted_auth_status():
    return {
        "ok": True,
        "expected_bundle_id": "com.anthropic.claudefordesktop",
        "expected_team_identifier": "Q6L2SF6YDW",
        "source_codesign": {
            "identifier": "com.anthropic.claudefordesktop",
            "team_identifier": "Q6L2SF6YDW",
            "trusted_for_claude_auth": True,
        },
        "patched_codesign": {
            "identifier": "com.anthropic.claudefordesktop",
            "team_identifier": "Q6L2SF6YDW",
            "trusted_for_claude_auth": True,
        },
        "patched_copy": _current_patch_status(),
        "policy": "cdp_process_must_keep_anthropic_team_identifier_for_profile_auth",
    }


def _untrusted_auth_status():
    status = _trusted_auth_status()
    status["ok"] = False
    status["patched_codesign"] = {
        "identifier": "com.anthropic.claudefordesktop",
        "team_identifier": "not set",
        "trusted_for_claude_auth": False,
    }
    return status


def _verified_login_reuse_status():
    return {
        "ok": True,
        "source": "opencli-status",
        "login": "Yes",
        "status": "Connected",
        "has_composer": "Yes",
        "mode": "Code",
        "url": "https://claude.ai/epitaxy",
    }


def _failed_login_reuse_status():
    return {
        "ok": False,
        "source": "opencli-status",
        "login": "No",
        "status": "Connected",
        "has_composer": "No",
        "error": "Claude App CDP login is 'No', expected 'Yes'",
    }


@pytest.fixture(autouse=True)
def _default_trusted_auth(monkeypatch):
    monkeypatch.setattr(claude_app_tool, "_auth_identity_status", _trusted_auth_status)
    monkeypatch.setattr(claude_app_tool, "_login_reuse_status", _verified_login_reuse_status)


def test_health_reports_opencli_and_cdp(monkeypatch):
    monkeypatch.setattr(claude_app_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(
        claude_app_tool,
        "_cdp_status",
        lambda timeout=1.5: {"ok": True, "url": "http://127.0.0.1:9242/json/version"},
    )
    monkeypatch.setattr(claude_app_tool, "_patched_copy_status", _current_patch_status)

    result = json.loads(claude_app_tool.handle_claude_app_opencli({"action": "health"}))

    assert result["ok"] is True
    assert result["opencli"]["ok"] is True
    assert result["cdp"]["ok"] is True
    assert result["patched_copy_current"] is True
    assert result["cdp_auth_identity_trusted"] is True
    assert result["opencli_route"] == "claude-app"
    assert result["fallback_allowed"] is False


def test_mutating_action_allows_untrusted_auth_identity_when_login_reuse_is_verified(monkeypatch):
    calls = []
    monkeypatch.setattr(claude_app_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(claude_app_tool, "_cdp_status", lambda: {"ok": True})
    monkeypatch.setattr(claude_app_tool, "_patched_copy_status", _current_patch_status)
    monkeypatch.setattr(claude_app_tool, "_auth_identity_status", _untrusted_auth_status)
    monkeypatch.setattr(
        claude_app_tool,
        "_run_opencli",
        lambda command, timeout: calls.append((command, timeout)) or _completed(stdout='[{"response":"ok"}]'),
    )

    result = json.loads(
        claude_app_tool.handle_claude_app_opencli(
            {"action": "ask", "message": "Hello", "project": "zhangjian-skills"}
        )
    )

    assert result["ok"] is True
    assert result["source"] == "opencli-cdp"
    assert calls


def test_mutating_action_blocks_when_login_reuse_is_not_verified(monkeypatch):
    monkeypatch.setattr(claude_app_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(claude_app_tool, "_cdp_status", lambda: {"ok": True})
    monkeypatch.setattr(claude_app_tool, "_patched_copy_status", _current_patch_status)
    monkeypatch.setattr(claude_app_tool, "_auth_identity_status", _untrusted_auth_status)
    monkeypatch.setattr(claude_app_tool, "_login_reuse_status", _failed_login_reuse_status)

    result = json.loads(
        claude_app_tool.handle_claude_app_opencli(
            {"action": "ask", "message": "Hello", "project": "zhangjian-skills"}
        )
    )

    assert result["ok"] is False
    assert result["blocked_reason"] == "BLOCKED_CLAUDE_APP_LOGIN_PROMPT_REUSE_EXISTING_PROFILE_ONLY"
    assert result["fallback_allowed"] is False
    assert result["login_reuse_verified"] is False
    assert result["auth_identity"]["patched_codesign"]["team_identifier"] == "not set"


def test_cdp_unavailable_allows_computer_use_fallback(monkeypatch):
    monkeypatch.setattr(claude_app_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(claude_app_tool, "_cdp_status", lambda: {"ok": False, "error": "connection refused"})

    result = json.loads(claude_app_tool.handle_claude_app_opencli({"action": "status"}))

    assert result["ok"] is False
    assert result["fallback_allowed"] is True
    assert result["fallback"] == "computer_use"
    assert "app=Claude" in result["guidance"]


def test_build_send_command_targets_claude_app(monkeypatch):
    monkeypatch.setattr(claude_app_tool, "_resolve_opencli_command", lambda: ["opencli"])

    command = claude_app_tool._build_opencli_command(
        "send",
        {"message": "Hello", "new": True, "project": "Zhangjian Skills"},
    )

    assert command == [
        "opencli",
        "claude-app",
        "send",
        "Hello",
        "--project",
        "Zhangjian Skills",
        "--new",
        "true",
        "--mode",
        "code",
        "--format",
        "json",
    ]


def test_build_ask_command_composes_options(monkeypatch):
    monkeypatch.setattr(claude_app_tool, "_resolve_opencli_command", lambda: ["opencli"])

    command = claude_app_tool._build_opencli_command(
        "ask",
        {
            "message": "Hello",
            "new": True,
            "project": "Zhangjian Skills",
            "model": "opus",
            "think": True,
            "timeout": 7,
        },
    )

    assert command == [
        "opencli",
        "claude-app",
        "ask",
        "Hello",
        "--new",
        "true",
        "--project",
        "Zhangjian Skills",
        "--model",
        "opus",
        "--mode",
        "code",
        "--think",
        "true",
        "--timeout",
        "7",
        "--format",
        "json",
    ]


def test_build_send_and_ask_commands_can_select_conversation(monkeypatch):
    monkeypatch.setattr(claude_app_tool, "_resolve_opencli_command", lambda: ["opencli"])

    assert claude_app_tool._build_opencli_command(
        "send",
        {"message": "Hello", "conversation_id": "local-session"},
    ) == [
        "opencli",
        "claude-app",
        "send",
        "Hello",
        "--conversation",
        "local-session",
        "--mode",
        "code",
        "--format",
        "json",
    ]
    assert claude_app_tool._build_opencli_command(
        "ask",
        {"message": "Hello", "conversation_id": "local-session"},
    ) == [
        "opencli",
        "claude-app",
        "ask",
        "Hello",
        "--conversation",
        "local-session",
        "--mode",
        "code",
        "--timeout",
        "20",
        "--format",
        "json",
    ]


def test_build_project_commands(monkeypatch):
    monkeypatch.setattr(claude_app_tool, "_resolve_opencli_command", lambda: ["opencli"])

    assert claude_app_tool._build_opencli_command("projects", {"limit": 5}) == [
        "opencli",
        "claude-app",
        "projects",
        "--limit",
        "5",
        "--format",
        "json",
    ]
    assert claude_app_tool._build_opencli_command("project", {"project": "Zhangjian Skills"}) == [
        "opencli",
        "claude-app",
        "project",
        "Zhangjian Skills",
        "--mode",
        "code",
        "--format",
        "json",
    ]
    assert claude_app_tool._build_opencli_command("new", {"project": "Zhangjian Skills"}) == [
        "opencli",
        "claude-app",
        "new",
        "--project",
        "Zhangjian Skills",
        "--mode",
        "code",
        "--format",
        "json",
    ]


def test_build_new_code_command_can_select_folder_model_and_effort(monkeypatch):
    monkeypatch.setattr(claude_app_tool, "_resolve_opencli_command", lambda: ["opencli"])

    assert claude_app_tool._build_opencli_command(
        "new",
        {
            "folder": "/Users/zjah/Documents/code/zhangjian-skills",
            "model": "haiku",
            "effort": "high",
            "branch": "codex/hermes-e2e",
        },
    ) == [
        "opencli",
        "claude-app",
        "new",
        "--folder",
        "/Users/zjah/Documents/code/zhangjian-skills",
        "--branch",
        "codex/hermes-e2e",
        "--model",
        "haiku",
        "--effort",
        "high",
        "--mode",
        "code",
        "--format",
        "json",
    ]


def test_build_ask_new_code_command_can_select_folder_model_and_effort(monkeypatch):
    monkeypatch.setattr(claude_app_tool, "_resolve_opencli_command", lambda: ["opencli"])

    assert claude_app_tool._build_opencli_command(
        "ask",
        {
            "message": "Hello",
            "new": True,
            "folder": "/Users/zjah/Documents/code/zhangjian-skills",
            "model": "haiku",
            "effort": "high",
            "timeout": 30,
        },
    ) == [
        "opencli",
        "claude-app",
        "ask",
        "Hello",
        "--new",
        "true",
        "--folder",
        "/Users/zjah/Documents/code/zhangjian-skills",
        "--model",
        "haiku",
        "--effort",
        "high",
        "--mode",
        "code",
        "--timeout",
        "30",
        "--format",
        "json",
    ]


def test_build_commands_accept_explicit_claude_app_mode(monkeypatch):
    monkeypatch.setattr(claude_app_tool, "_resolve_opencli_command", lambda: ["opencli"])

    assert claude_app_tool._build_opencli_command(
        "ask",
        {"message": "Hello", "project": "Zhangjian Skills", "mode": "chat"},
    ) == [
        "opencli",
        "claude-app",
        "ask",
        "Hello",
        "--project",
        "Zhangjian Skills",
        "--mode",
        "chat",
        "--timeout",
        "20",
        "--format",
        "json",
    ]


def test_ensure_cdp_reports_existing_endpoint(monkeypatch):
    monkeypatch.setattr(
        claude_app_tool,
        "_cdp_status",
        lambda timeout=1.5: {"ok": True, "url": "http://127.0.0.1:9242/json/version"},
    )
    monkeypatch.setattr(claude_app_tool, "_patched_copy_status", _current_patch_status)

    result = json.loads(claude_app_tool.handle_claude_app_opencli({"action": "ensure_cdp"}))

    assert result["ok"] is True
    assert result["action"] == "ensure_cdp"
    assert result["endpoint"] == "http://127.0.0.1:9242"
    assert result["patched_copy_current"] is True
    assert result["restarted"] is False


def test_ensure_cdp_blocks_stale_existing_endpoint_without_restart(monkeypatch):
    monkeypatch.setattr(
        claude_app_tool,
        "_cdp_status",
        lambda timeout=1.5: {"ok": True, "url": "http://127.0.0.1:9242/json/version"},
    )
    monkeypatch.setattr(claude_app_tool, "_patched_copy_status", _stale_patch_status)

    result = json.loads(claude_app_tool.handle_claude_app_opencli({"action": "ensure_cdp"}))

    assert result["ok"] is False
    assert result["requires_restart"] is True
    assert result["stale_patched_copy"] is True
    assert result["patched_copy"]["patched_copy_current"] is False


def test_ensure_cdp_refreshes_stale_existing_endpoint_with_restart_force(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        claude_app_tool,
        "_cdp_status",
        lambda timeout=1.5: {"ok": True, "url": "http://127.0.0.1:9242/json/version"},
    )
    monkeypatch.setattr(claude_app_tool, "_patched_copy_status", _stale_patch_status)
    monkeypatch.setattr(claude_app_tool, "_is_claude_app_running", lambda: True)
    wrapper = tmp_path / "claude-app-opencli.sh"
    wrapper.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    monkeypatch.setattr(claude_app_tool, "_wrapper_script_path", lambda: wrapper)

    def fake_run(command, **_kwargs):
        calls.append(command)
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "endpoint": "http://127.0.0.1:9555",
                    "message": "Claude App CDP ready",
                    **_current_patch_status(),
                }
            )
        )

    monkeypatch.setattr(claude_app_tool.subprocess, "run", fake_run)

    result = json.loads(
        claude_app_tool.handle_claude_app_opencli(
            {"action": "ensure_cdp", "restart": True, "force": True, "port": 9555}
        )
    )

    assert result["ok"] is True
    assert result["restarted"] is True
    assert result["endpoint"] == "http://127.0.0.1:9555"
    assert result["patched_copy_current"] is True
    assert calls == [
        [
            "bash",
            str(wrapper),
            "ensure-cdp",
            "--json",
            "--port",
            "9555",
            "--restart",
            "--force",
        ]
    ]


def test_ensure_cdp_requires_explicit_restart_when_running(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_app_tool, "DEFAULT_APP_PATH", tmp_path)
    monkeypatch.setattr(claude_app_tool, "_cdp_status", lambda timeout=1.5: {"ok": False})
    monkeypatch.setattr(claude_app_tool, "_is_claude_app_running", lambda: True)

    result = json.loads(claude_app_tool.handle_claude_app_opencli({"action": "ensure_cdp"}))

    assert result["ok"] is False
    assert result["requires_restart"] is True
    assert "restart=true" in result["error"]


def test_ensure_cdp_restart_invokes_wrapper_with_port(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(claude_app_tool, "DEFAULT_APP_PATH", tmp_path)
    monkeypatch.setattr(claude_app_tool, "_cdp_status", lambda timeout=1.5: {"ok": False})
    monkeypatch.setattr(claude_app_tool, "_is_claude_app_running", lambda: True)
    wrapper = tmp_path / "claude-app-opencli.sh"
    wrapper.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    monkeypatch.setattr(claude_app_tool, "_wrapper_script_path", lambda: wrapper)

    def fake_run(command, **_kwargs):
        calls.append(command)
        return _completed(
            stdout='{"ok":true,"endpoint":"http://127.0.0.1:9555","message":"Claude App CDP ready"}'
        )

    monkeypatch.setattr(
        claude_app_tool.subprocess,
        "run",
        fake_run,
    )

    result = json.loads(
        claude_app_tool.handle_claude_app_opencli(
            {"action": "ensure_cdp", "restart": True, "port": 9555}
        )
    )

    assert result["ok"] is True
    assert result["restarted"] is True
    assert result["endpoint"] == "http://127.0.0.1:9555"
    assert calls == [
        ["bash", str(wrapper), "ensure-cdp", "--json", "--port", "9555", "--restart"]
    ]


def test_detail_requires_conversation_id(monkeypatch):
    monkeypatch.setattr(claude_app_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(claude_app_tool, "_cdp_status", lambda: {"ok": True})
    monkeypatch.setattr(claude_app_tool, "_patched_copy_status", _current_patch_status)

    result = json.loads(claude_app_tool.handle_claude_app_opencli({"action": "detail"}))

    assert result["ok"] is False
    assert "conversation_id is required" in result["error"]
    assert result["fallback_allowed"] is False


def test_dump_requires_diagnostic(monkeypatch):
    monkeypatch.setattr(claude_app_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(claude_app_tool, "_cdp_status", lambda: {"ok": True})
    monkeypatch.setattr(claude_app_tool, "_patched_copy_status", _current_patch_status)

    result = json.loads(claude_app_tool.handle_claude_app_opencli({"action": "dump"}))

    assert result["ok"] is False
    assert "diagnostic-only" in result["error"]
    assert result["fallback_allowed"] is False


def test_handle_read_returns_opencli_payload(monkeypatch):
    calls = []
    monkeypatch.setattr(claude_app_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(claude_app_tool, "_cdp_status", lambda: {"ok": True})
    monkeypatch.setattr(claude_app_tool, "_patched_copy_status", _current_patch_status)

    def fake_run(command, timeout):
        calls.append((command, timeout))
        return _completed(stdout=json.dumps([{"Index": 0, "Role": "assistant", "Text": "Hi"}]))

    monkeypatch.setattr(claude_app_tool, "_run_opencli", fake_run)

    result = json.loads(claude_app_tool.handle_claude_app_opencli({"action": "read", "timeout": 4}))

    assert result["ok"] is True
    assert result["source"] == "opencli-cdp"
    assert result["payload"] == [{"Index": 0, "Role": "assistant", "Text": "Hi"}]
    assert calls == [(["opencli", "claude-app", "read", "--format", "json"], 4.0)]


def test_handle_read_blocks_stale_patched_copy_before_opencli(monkeypatch):
    calls = []
    monkeypatch.setattr(claude_app_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(
        claude_app_tool,
        "_cdp_status",
        lambda timeout=1.5: {"ok": True, "url": "http://127.0.0.1:9242/json/version"},
    )
    monkeypatch.setattr(claude_app_tool, "_patched_copy_status", _stale_patch_status)
    monkeypatch.setattr(claude_app_tool, "_run_opencli", lambda command, timeout: calls.append(command))

    result = json.loads(claude_app_tool.handle_claude_app_opencli({"action": "read"}))

    assert result["ok"] is False
    assert result["requires_restart"] is True
    assert result["stale_patched_copy"] is True
    assert result["patched_copy"]["patched_copy_current"] is False
    assert calls == []


def test_handle_ask_uses_process_timeout_buffer(monkeypatch):
    calls = []
    monkeypatch.setattr(claude_app_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(claude_app_tool, "_cdp_status", lambda: {"ok": True})
    monkeypatch.setattr(claude_app_tool, "_patched_copy_status", _current_patch_status)

    def fake_run(command, timeout):
        calls.append((command, timeout))
        return _completed(stdout=json.dumps([{"response": "Two plus two equals four."}]))

    monkeypatch.setattr(claude_app_tool, "_run_opencli", fake_run)

    result = json.loads(
        claude_app_tool.handle_claude_app_opencli(
            {
                "action": "ask",
                "message": "What is 2 plus 2?",
                "project": "zhangjian-skills",
                "timeout": 45,
            }
        )
    )

    assert result["ok"] is True
    assert result["source"] == "opencli-cdp"
    assert result["payload"] == [{"response": "Two plus two equals four."}]
    assert calls == [
        (
            [
                "opencli",
                "claude-app",
                "ask",
                "What is 2 plus 2?",
                "--project",
                "zhangjian-skills",
                "--mode",
                "code",
                "--timeout",
                "45",
                "--format",
                "json",
            ],
            90.0,
        )
    ]


def test_toolset_contains_claude_app_tool():
    from toolsets import TOOLSETS, _HERMES_CORE_TOOLS, resolve_toolset

    assert "claude_app_opencli" in _HERMES_CORE_TOOLS
    assert TOOLSETS["claude-app"]["tools"] == ["claude_app_opencli"]
    assert resolve_toolset("claude-app") == ["claude_app_opencli"]
