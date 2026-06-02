from __future__ import annotations

import json
import subprocess
import urllib.error

from tools import codex_tool


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self, _limit=-1):
        return json.dumps(self.payload).encode("utf-8")


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["opencli"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _clear_cdp_env(monkeypatch):
    monkeypatch.delenv("HERMES_CODEX_CDP_URL", raising=False)
    monkeypatch.delenv("HERMES_CODEX_CDP_PORT", raising=False)
    monkeypatch.delenv("HERMES_MANAGEMENT_CONFIG", raising=False)
    monkeypatch.delenv("OPENCLI_CDP_ENDPOINT", raising=False)
    monkeypatch.delenv("CODEX_DEVTOOLS_ACTIVE_PORT_FILE", raising=False)


def test_detect_cdp_endpoint_prefers_devtools_active_port(monkeypatch, tmp_path):
    _clear_cdp_env(monkeypatch)
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9222\n/devtools/browser/test\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_DEVTOOLS_ACTIVE_PORT_FILE", str(port_file))

    requested = []

    def fake_urlopen(url, timeout=1.0):
        requested.append(url)
        if url == "http://127.0.0.1:9222/json/version":
            return _FakeResponse({"User-Agent": "Codex/26 Electron/42"})
        if url == "http://127.0.0.1:9222/json/list":
            return _FakeResponse([{"type": "page", "webSocketDebuggerUrl": "ws://codex/page", "url": "app://-/index.html"}])
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(codex_tool.urllib.request, "urlopen", fake_urlopen)

    assert codex_tool._detect_codex_cdp_endpoint() == "http://127.0.0.1:9222"
    assert requested == [
        "http://127.0.0.1:9222/json/version",
        "http://127.0.0.1:9222/json/list",
    ]


def test_detect_cdp_endpoint_probes_known_ports_when_port_file_missing(monkeypatch, tmp_path):
    _clear_cdp_env(monkeypatch)
    monkeypatch.setenv("CODEX_DEVTOOLS_ACTIVE_PORT_FILE", str(tmp_path / "missing"))

    requested = []

    def fake_urlopen(url, timeout=1.0):
        requested.append(url)
        if url == "http://127.0.0.1:9222/json/version":
            return _FakeResponse({"User-Agent": "Codex/26 Electron/42"})
        if url == "http://127.0.0.1:9222/json/list":
            return _FakeResponse([{"type": "page", "webSocketDebuggerUrl": "ws://codex/page", "url": "app://-/index.html"}])
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(codex_tool.urllib.request, "urlopen", fake_urlopen)

    assert codex_tool._detect_codex_cdp_endpoint() == "http://127.0.0.1:9222"
    assert requested == [
        "http://127.0.0.1:9222/json/version",
        "http://127.0.0.1:9222/json/list",
    ]


def test_detect_cdp_endpoint_skips_non_codex_9222_and_uses_9238(monkeypatch, tmp_path):
    _clear_cdp_env(monkeypatch)
    monkeypatch.setenv("CODEX_DEVTOOLS_ACTIVE_PORT_FILE", str(tmp_path / "missing"))

    def fake_urlopen(url, timeout=1.0):
        if url == "http://127.0.0.1:9222/json/version":
            return _FakeResponse({"User-Agent": "Chrome/148 Safari/537.36"})
        if url == "http://127.0.0.1:9238/json/version":
            return _FakeResponse({"User-Agent": "Codex/26 Electron/42"})
        if url == "http://127.0.0.1:9238/json/list":
            return _FakeResponse([{"type": "page", "webSocketDebuggerUrl": "ws://codex/page", "url": "app://-/index.html"}])
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(codex_tool.urllib.request, "urlopen", fake_urlopen)

    assert codex_tool._detect_codex_cdp_endpoint() == "http://127.0.0.1:9238"


def test_detect_cdp_endpoint_skips_codex_endpoint_without_page_target(monkeypatch, tmp_path):
    _clear_cdp_env(monkeypatch)
    monkeypatch.setenv("CODEX_DEVTOOLS_ACTIVE_PORT_FILE", str(tmp_path / "missing"))

    def fake_urlopen(url, timeout=1.0):
        if url == "http://127.0.0.1:9222/json/version":
            return _FakeResponse({"User-Agent": "Codex/26 Electron/42"})
        if url == "http://127.0.0.1:9222/json/list":
            return _FakeResponse([])
        if url == "http://127.0.0.1:9238/json/version":
            return _FakeResponse({"User-Agent": "Codex/26 Electron/42"})
        if url == "http://127.0.0.1:9238/json/list":
            return _FakeResponse([{"type": "page", "webSocketDebuggerUrl": "ws://codex/page", "url": "app://-/index.html"}])
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(codex_tool.urllib.request, "urlopen", fake_urlopen)

    assert codex_tool._detect_codex_cdp_endpoint() == "http://127.0.0.1:9238"


def test_detect_cdp_endpoint_uses_hermes_management_config(monkeypatch, tmp_path):
    _clear_cdp_env(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text(
        json.dumps(
            {
                "app_control": {
                    "codex": {
                        "cdp": {
                            "preferred_port": 9555,
                            "fallback_ports": [9555, 9556],
                            "devtools_active_port_file": str(tmp_path / "missing"),
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_MANAGEMENT_CONFIG", str(config))
    requested = []

    def fake_urlopen(url, timeout=1.0):
        requested.append(url)
        if url == "http://127.0.0.1:9555/json/version":
            return _FakeResponse({"User-Agent": "Codex/26 Electron/42"})
        if url == "http://127.0.0.1:9555/json/list":
            return _FakeResponse([{"type": "page", "webSocketDebuggerUrl": "ws://codex/page", "url": "app://-/index.html"}])
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(codex_tool.urllib.request, "urlopen", fake_urlopen)

    assert codex_tool._detect_codex_cdp_endpoint() == "http://127.0.0.1:9555"
    assert requested == [
        "http://127.0.0.1:9555/json/version",
        "http://127.0.0.1:9555/json/list",
    ]


def test_health_reports_opencli_and_cdp(monkeypatch):
    monkeypatch.setattr(codex_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(
        codex_tool,
        "_cdp_status",
        lambda timeout=1.5: {"ok": True, "url": "http://127.0.0.1:9238/json/version"},
    )

    result = json.loads(codex_tool.handle_codex_opencli({"action": "health"}))

    assert result["ok"] is True
    assert result["opencli"]["ok"] is True
    assert result["cdp"]["ok"] is True
    assert result["preferred_over"] == "computer_use"
    assert result["fallback_allowed"] is False
    assert result["fallback"] is None


def test_health_with_cdp_down_keeps_tool_on_ensure_cdp_path(monkeypatch):
    monkeypatch.setattr(codex_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(codex_tool, "_cdp_status", lambda timeout=1.5: {"ok": False, "error": "no target"})

    result = json.loads(codex_tool.handle_codex_opencli({"action": "health"}))

    assert result["ok"] is False
    assert result["fallback_allowed"] is False
    assert result["fallback"] is None
    assert result["next_actions"][0]["action"] == "ensure_cdp"


def test_ensure_cdp_reports_existing_endpoint(monkeypatch):
    monkeypatch.setattr(
        codex_tool,
        "_cdp_status",
        lambda timeout=1.5: {"ok": True, "endpoint": "http://127.0.0.1:9222", "url": "http://127.0.0.1:9222/json/version"},
    )

    result = json.loads(codex_tool.handle_codex_opencli({"action": "ensure_cdp"}))

    assert result["ok"] is True
    assert result["action"] == "ensure_cdp"
    assert result["status"] == "already_ready"
    assert result["launched"] is False


def test_ensure_cdp_launches_side_by_side_instance(monkeypatch):
    launches = []
    monkeypatch.setattr(codex_tool, "_cdp_status", lambda timeout=1.5: {"ok": False, "error": "connection refused"})

    def fake_launch(port):
        launches.append(port)
        return _completed()

    monkeypatch.setattr(codex_tool, "_launch_codex_cdp", fake_launch)
    monkeypatch.setattr(
        codex_tool,
        "_wait_for_codex_cdp",
        lambda timeout, preferred_ports: {
            "ok": True,
            "endpoint": f"http://127.0.0.1:{preferred_ports[0]}",
            "url": f"http://127.0.0.1:{preferred_ports[0]}/json/version",
        },
    )

    result = json.loads(codex_tool.handle_codex_opencli({"action": "ensure_cdp", "port": 9555}))

    assert result["ok"] is True
    assert result["status"] == "launched"
    assert result["port"] == 9555
    assert launches == [9555]


def test_ensure_cdp_uses_configured_default_port(monkeypatch, tmp_path):
    _clear_cdp_env(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text(
        json.dumps(
            {
                "app_control": {
                    "codex": {
                        "cdp": {
                            "preferred_port": 9555,
                            "fallback_ports": [9555, 9556],
                            "devtools_active_port_file": str(tmp_path / "missing"),
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_MANAGEMENT_CONFIG", str(config))
    launches = []
    monkeypatch.setattr(codex_tool, "_cdp_status", lambda timeout=1.5: {"ok": False, "error": "connection refused"})

    def fake_launch(port):
        launches.append(port)
        return _completed()

    monkeypatch.setattr(codex_tool, "_launch_codex_cdp", fake_launch)
    monkeypatch.setattr(
        codex_tool,
        "_wait_for_codex_cdp",
        lambda timeout, preferred_ports: {
            "ok": True,
            "endpoint": f"http://127.0.0.1:{preferred_ports[0]}",
            "url": f"http://127.0.0.1:{preferred_ports[0]}/json/version",
        },
    )

    result = json.loads(codex_tool.handle_codex_opencli({"action": "ensure_cdp"}))

    assert result["ok"] is True
    assert result["port"] == 9555
    assert launches == [9555]


def test_ensure_cdp_force_new_skips_existing_endpoint_port(monkeypatch):
    launches = []
    wait_calls = []
    monkeypatch.setattr(
        codex_tool,
        "_cdp_status",
        lambda timeout=1.5: {
            "ok": True,
            "endpoint": "http://127.0.0.1:9222",
            "url": "http://127.0.0.1:9222/json/version",
        },
    )
    monkeypatch.setattr(codex_tool, "_configured_cdp_ports", lambda: [9222, 9238])

    def fake_launch(port):
        launches.append(port)
        return _completed()

    def fake_wait(timeout, preferred_ports, include_existing_candidates=True):
        wait_calls.append((preferred_ports, include_existing_candidates))
        return {
            "ok": True,
            "endpoint": f"http://127.0.0.1:{preferred_ports[0]}",
            "url": f"http://127.0.0.1:{preferred_ports[0]}/json/version",
        }

    monkeypatch.setattr(codex_tool, "_launch_codex_cdp", fake_launch)
    monkeypatch.setattr(codex_tool, "_wait_for_codex_cdp", fake_wait)

    result = json.loads(codex_tool.handle_codex_opencli({"action": "ensure_cdp", "force_new": True}))

    assert result["ok"] is True
    assert result["status"] == "launched"
    assert result["port"] == 9238
    assert launches == [9238]
    assert wait_calls == [([9238], False)]


def test_toolset_available_when_opencli_exists_even_if_cdp_is_down(monkeypatch):
    monkeypatch.setattr(codex_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(codex_tool, "_cdp_status", lambda: {"ok": False, "error": "connection refused"})

    assert codex_tool.check_codex_requirements() is True


def test_cdp_unavailable_routes_to_ensure_cdp_before_fallback(monkeypatch):
    monkeypatch.setattr(codex_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(codex_tool, "_cdp_status", lambda: {"ok": False, "error": "connection refused"})

    result = json.loads(codex_tool.handle_codex_opencli({"action": "state", "auto_ensure_cdp": False}))

    assert result["ok"] is False
    assert result["fallback_allowed"] is False
    assert result["fallback"] is None
    assert result["do_not_use_computer_use"] is True
    assert result["next_actions"][0]["action"] == "ensure_cdp"


def test_high_level_action_auto_ensures_cdp_before_opencli(monkeypatch):
    calls = []
    monkeypatch.setattr(codex_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(codex_tool, "_cdp_status", lambda timeout=1.5: {"ok": False, "error": "connection refused"})
    monkeypatch.setattr(
        codex_tool,
        "_ensure_cdp_payload",
        lambda args: {
            "ok": True,
            "action": "ensure_cdp",
            "status": "launched",
            "port": 9222,
            "cdp": {"ok": True, "endpoint": "http://127.0.0.1:9222"},
        },
    )

    def fake_run(command, timeout):
        calls.append((command, timeout))
        return _completed(stdout=json.dumps({"ok": True, "title": "Codex"}))

    monkeypatch.setattr(codex_tool, "_run_opencli", fake_run)

    result = json.loads(codex_tool.handle_codex_opencli({"action": "state", "timeout": 5}))

    assert result["ok"] is True
    assert result["action"] == "state"
    assert result["auto_ensure_cdp"]["status"] == "launched"
    assert calls == [(["opencli", "codex", "state", "--last", "5", "--format", "json"], 5.0)]


def test_high_level_action_reports_auto_ensure_failure(monkeypatch):
    monkeypatch.setattr(codex_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(codex_tool, "_cdp_status", lambda timeout=1.5: {"ok": False, "error": "connection refused"})
    monkeypatch.setattr(
        codex_tool,
        "_ensure_cdp_payload",
        lambda args: {
            "ok": False,
            "action": "ensure_cdp",
            "status": "unavailable",
            "error": "Unable to launch a Codex CDP instance with an inspectable page target.",
            "next_actions": [{"action": "ensure_cdp", "reason": "Retry with restart=true if allowed."}],
        },
    )

    result = json.loads(codex_tool.handle_codex_opencli({"action": "state"}))

    assert result["ok"] is False
    assert result["action"] == "state"
    assert result["auto_ensure_cdp"]["status"] == "unavailable"
    assert result["fallback_allowed"] is False
    assert result["do_not_use_computer_use"] is True
    assert result["next_actions"][0]["reason"] == "Retry with restart=true if allowed."


def test_build_state_command_uses_json_and_read_last(monkeypatch):
    monkeypatch.setattr(codex_tool, "_resolve_opencli_command", lambda: ["opencli"])

    command = codex_tool._build_opencli_command("state", {"read_last": 3})

    assert command == ["opencli", "codex", "state", "--last", "3", "--format", "json"]


def test_build_read_command_uses_json_and_read_last(monkeypatch):
    monkeypatch.setattr(codex_tool, "_resolve_opencli_command", lambda: ["opencli"])

    command = codex_tool._build_opencli_command("read", {"read_last": 4})

    assert command == ["opencli", "codex", "read", "--last", "4", "--format", "json"]


def test_build_projects_command_uses_compact_conversations(monkeypatch):
    monkeypatch.setattr(codex_tool, "_resolve_opencli_command", lambda: ["opencli"])

    command = codex_tool._build_opencli_command("projects", {})

    assert command == ["opencli", "codex", "conversations", "--format", "json"]


def test_build_projects_command_passes_limit(monkeypatch):
    monkeypatch.setattr(codex_tool, "_resolve_opencli_command", lambda: ["opencli"])

    command = codex_tool._build_opencli_command("projects", {"limit": 3})

    assert command == ["opencli", "codex", "conversations", "--limit", "3", "--format", "json"]


def test_build_open_conversation_command_uses_selection_args(monkeypatch):
    monkeypatch.setattr(codex_tool, "_resolve_opencli_command", lambda: ["opencli"])

    command = codex_tool._build_opencli_command(
        "open_conversation",
        {
            "project": "zhangjian-skills",
            "conversation": "Current task",
            "thread_id": "local:thread-1",
            "index": 2,
        },
    )

    assert command == [
        "opencli",
        "codex",
        "open",
        "--project",
        "zhangjian-skills",
        "--conversation",
        "Current task",
        "--thread-id",
        "local:thread-1",
        "--index",
        "2",
        "--format",
        "json",
    ]


def test_build_ask_command_composes_one_shot_options(monkeypatch):
    monkeypatch.setattr(codex_tool, "_resolve_opencli_command", lambda: ["opencli"])

    command = codex_tool._build_opencli_command(
        "ask",
        {
            "message": "Hello",
            "model": "gpt-5.4",
            "project": "/Users/zjah/Documents/code/zhangjian-skills",
            "new_conversation": True,
            "wait": True,
            "timeout": 9,
            "read_last": 2,
        },
    )

    assert command == [
        "opencli",
        "codex",
        "ask",
        "Hello",
        "--model",
        "gpt-5.4",
        "--project",
        "/Users/zjah/Documents/code/zhangjian-skills",
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


def test_ask_new_conversation_infers_project_from_message_path(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(codex_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(codex_tool, "_cdp_status", lambda: {"ok": True})

    def fake_run(command, timeout):
        calls.append((command, timeout))
        return _completed(stdout=json.dumps({"ok": True, "action": "ask", "reply": "ok"}))

    monkeypatch.setattr(codex_tool, "_run_opencli", fake_run)

    result = json.loads(
        codex_tool.handle_codex_opencli(
            {
                "action": "ask",
                "message": f"请在当前项目 {tmp_path} 里处理改动",
                "new_conversation": True,
                "wait": True,
            }
        )
    )

    assert result["ok"] is True
    assert result["inferred_project"] == {"source": "message_path", "project": str(tmp_path)}
    assert calls[0][0] == [
        "opencli",
        "codex",
        "ask",
        f"请在当前项目 {tmp_path} 里处理改动",
        "--project",
        str(tmp_path),
        "--new-conversation",
        "true",
        "--wait",
        "true",
        "--timeout",
        "20",
        "--read-last",
        "5",
        "--format",
        "json",
    ]


def test_ask_without_new_conversation_does_not_infer_project(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(codex_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(codex_tool, "_cdp_status", lambda: {"ok": True})

    def fake_run(command, timeout):
        calls.append(command)
        return _completed(stdout=json.dumps({"ok": True, "action": "ask", "reply": "ok"}))

    monkeypatch.setattr(codex_tool, "_run_opencli", fake_run)

    result = json.loads(
        codex_tool.handle_codex_opencli(
            {"action": "ask", "message": f"读取 {tmp_path}", "new_conversation": False}
        )
    )

    assert result["ok"] is True
    assert "inferred_project" not in result
    assert "--project" not in calls[0]


def test_build_new_command_can_select_project(monkeypatch):
    monkeypatch.setattr(codex_tool, "_resolve_opencli_command", lambda: ["opencli"])

    command = codex_tool._build_opencli_command(
        "new",
        {"project": "/Users/zjah/Documents/code/zhangjian-skills"},
    )

    assert command == [
        "opencli",
        "codex",
        "new",
        "--project",
        "/Users/zjah/Documents/code/zhangjian-skills",
        "--format",
        "json",
    ]


def test_send_requires_message(monkeypatch):
    monkeypatch.setattr(codex_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(codex_tool, "_cdp_status", lambda: {"ok": True})

    result = json.loads(codex_tool.handle_codex_opencli({"action": "send"}))

    assert result["ok"] is False
    assert "message is required" in result["error"]


def test_ask_requires_message(monkeypatch):
    monkeypatch.setattr(codex_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(codex_tool, "_cdp_status", lambda: {"ok": True})

    result = json.loads(codex_tool.handle_codex_opencli({"action": "ask"}))

    assert result["ok"] is False
    assert "message is required" in result["error"]
    assert result["fallback_allowed"] is False
    assert result["do_not_use_computer_use"] is True
    assert result["do_not_debug_adapter"] is True
    assert result["do_not_search_or_read_source"] is True


def test_dump_without_diagnostic_redirects_to_projects(monkeypatch):
    monkeypatch.setattr(codex_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(codex_tool, "_cdp_status", lambda: {"ok": True})

    result = json.loads(codex_tool.handle_codex_opencli({"action": "dump"}))

    assert result["ok"] is False
    assert result["action"] == "dump"
    assert "diagnostic-only" in result["error"]
    assert "projects" in result["error"]
    assert "/tmp/codex-snapshot.json" in result["error"]
    assert result["fallback_allowed"] is False
    assert result["do_not_use_computer_use"] is True


def test_handle_projects_returns_structured_result(monkeypatch):
    calls = []
    monkeypatch.setattr(codex_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(codex_tool, "_cdp_status", lambda: {"ok": True})

    def fake_run(command, timeout):
        calls.append((command, timeout))
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "project_count": 1,
                    "conversation_count": 1,
                    "projects": [
                        {
                            "name": "zhangjian-skills",
                            "conversations": [{"title": "Current task", "threadId": "local:thread-1"}],
                        }
                    ],
                }
            )
        )

    monkeypatch.setattr(codex_tool, "_run_opencli", fake_run)

    result = json.loads(codex_tool.handle_codex_opencli({"action": "projects", "timeout": 4}))

    assert result["ok"] is True
    assert result["action"] == "projects"
    assert result["conversation_count"] == 1
    assert result["projects"][0]["conversations"][0]["title"] == "Current task"
    assert "payload" not in result
    assert calls == [(["opencli", "codex", "conversations", "--format", "json"], 4.0)]


def test_handle_ask_returns_structured_result(monkeypatch):
    calls = []
    monkeypatch.setattr(codex_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(codex_tool, "_cdp_status", lambda: {"ok": True})

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

    monkeypatch.setattr(codex_tool, "_run_opencli", fake_run)

    result = json.loads(
        codex_tool.handle_codex_opencli(
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
                "codex",
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


def test_ask_new_conversation_busy_endpoint_uses_isolated_cdp(monkeypatch):
    ensure_calls = []
    run_calls = []
    monkeypatch.setattr(codex_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(
        codex_tool,
        "_cdp_status",
        lambda timeout=1.5: {"ok": True, "endpoint": "http://127.0.0.1:9222"},
    )
    monkeypatch.setattr(
        codex_tool,
        "_state_for_endpoint",
        lambda endpoint, timeout=8.0: {"ok": True, "is_generating": endpoint == "http://127.0.0.1:9222"},
    )

    def fake_ensure(args):
        ensure_calls.append(args)
        return {
            "ok": True,
            "action": "ensure_cdp",
            "status": "launched",
            "port": 9238,
            "cdp": {"ok": True, "endpoint": "http://127.0.0.1:9238"},
        }

    def fake_run(command, timeout, cdp_endpoint=None):
        run_calls.append((command, timeout, cdp_endpoint))
        return _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "action": "ask",
                    "source": "opencli-cdp",
                    "reply": "marker OK",
                    "state_after": {"project_path": "/Users/zjah/Documents/code/zhangjian-skills"},
                }
            )
        )

    monkeypatch.setattr(codex_tool, "_ensure_cdp_payload", fake_ensure)
    monkeypatch.setattr(codex_tool, "_run_opencli", fake_run)

    result = json.loads(
        codex_tool.handle_codex_opencli(
            {
                "action": "ask",
                "message": "Reply exactly with marker OK.",
                "new_conversation": True,
                "project": "/Users/zjah/Documents/code/zhangjian-skills",
                "wait": True,
                "timeout": 120,
                "isolate_if_busy": True,
            }
        )
    )

    assert result["ok"] is True
    assert result["selected_cdp_endpoint"] == "http://127.0.0.1:9238"
    assert result["isolated_new_conversation_cdp"]["auto_ensure_cdp"]["status"] == "launched"
    assert ensure_calls[0]["force_new"] is True
    assert run_calls[0][2] == "http://127.0.0.1:9238"


def test_handle_model_returns_structured_result(monkeypatch):
    monkeypatch.setattr(codex_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(codex_tool, "_cdp_status", lambda: {"ok": True})
    monkeypatch.setattr(
        codex_tool,
        "_run_opencli",
        lambda command, timeout: _completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "changed": False,
                    "currentModel": "gpt-5.5",
                    "state": {"model": "gpt-5.5"},
                }
            )
        ),
    )

    result = json.loads(codex_tool.handle_codex_opencli({"action": "model", "model": "gpt-5.5"}))

    assert result["ok"] is True
    assert result["action"] == "model"
    assert result["currentModel"] == "gpt-5.5"
    assert result["state"] == {"model": "gpt-5.5"}
    assert "payload" not in result


def test_model_structured_failure_blocks_computer_use(monkeypatch):
    monkeypatch.setattr(codex_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(codex_tool, "_cdp_status", lambda: {"ok": True})
    monkeypatch.setattr(
        codex_tool,
        "_run_opencli",
        lambda command, timeout: _completed(
            stdout=json.dumps(
                {
                    "ok": False,
                    "changed": False,
                    "currentModel": "gpt-5.5",
                    "reason": "Codex is currently generating. Use wait or stop before switching models.",
                }
            )
        ),
    )

    result = json.loads(codex_tool.handle_codex_opencli({"action": "model", "model": "gpt-5.4"}))

    assert result["ok"] is False
    assert result["fallback_allowed"] is False
    assert result["do_not_use_computer_use"] is True
    assert result["fallback"] is None
    assert result["do_not_debug_adapter"] is True
    assert result["do_not_search_or_read_source"] is True
    assert {item["action"] for item in result["next_actions"]} >= {"state", "models", "wait", "stop"}


def test_ask_validation_failure_blocks_computer_use(monkeypatch):
    monkeypatch.setattr(codex_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(codex_tool, "_cdp_status", lambda: {"ok": True})
    monkeypatch.setattr(
        codex_tool,
        "_run_opencli",
        lambda command, timeout: _completed(
            stdout=json.dumps(
                {
                    "ok": False,
                    "action": "ask",
                    "error": "Model switch was not verified. Current model: gpt-5.5",
                    "steps": [
                        {"name": "state", "ok": True},
                        {
                            "name": "model",
                            "ok": False,
                            "error": "Model switch was not verified. Current model: gpt-5.5",
                        },
                    ],
                }
            )
        ),
    )

    result = json.loads(
        codex_tool.handle_codex_opencli(
            {"action": "ask", "message": "Hello", "model": "gpt-5.4", "wait": True}
        )
    )

    assert result["ok"] is False
    assert result["fallback_allowed"] is False
    assert result["do_not_use_computer_use"] is True
    assert result["fallback"] is None
    assert result["do_not_debug_adapter"] is True
    assert result["do_not_search_or_read_source"] is True
    assert "Do not call computer_use" in result["guidance"]
    assert {item["action"] for item in result["next_actions"]} >= {"state", "models", "ask"}


def test_ask_timeout_recommends_state_or_wait_not_duplicate_ask(monkeypatch):
    monkeypatch.setattr(codex_tool, "_resolve_opencli_command", lambda: ["opencli"])
    monkeypatch.setattr(codex_tool, "_cdp_status", lambda: {"ok": True})

    def raise_timeout(command, timeout):
        raise subprocess.TimeoutExpired(command, timeout, output="", stderr="")

    monkeypatch.setattr(codex_tool, "_run_opencli", raise_timeout)

    result = json.loads(
        codex_tool.handle_codex_opencli(
            {"action": "ask", "message": "Hello", "wait": True, "timeout": 20}
        )
    )

    assert result["ok"] is False
    assert result["error"] == "OpenCLI timed out after 20.0s"
    assert [item["action"] for item in result["next_actions"]] == ["state", "wait"]
    assert "duplicate" in result["next_actions"][1]["reason"]


def test_parse_json_payload_skips_non_json_prefix():
    parsed = codex_tool._parse_json_payload(
        'status line\n{"ok": true, "project_count": 1}\nupdate notice'
    )

    assert parsed == {"ok": True, "project_count": 1}


def test_toolset_contains_fast_codex_tool():
    from toolsets import TOOLSETS, _HERMES_CORE_TOOLS, resolve_toolset

    assert "codex_opencli" in _HERMES_CORE_TOOLS
    assert TOOLSETS["codex"]["tools"] == ["codex_opencli"]
    assert resolve_toolset("codex") == ["codex_opencli"]
