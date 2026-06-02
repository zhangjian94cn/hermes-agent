"""Antigravity IDE control through the current logged-in IDE profile.

This is deliberately separate from ``antigravity_opencli``. The standalone
Antigravity app and Antigravity IDE have different app bundles, app-data
directories, language-server processes, and CDP availability. Treating them as
one tool makes login/CDP failures look like duplicate-app confusion.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.registry import registry, tool_error, tool_result


DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_TEXT_CHARS = 20_000


ANTIGRAVITY_IDE_OPENCLI_SCHEMA = {
    "name": "antigravity_ide_opencli",
    "description": (
        "Control bridge for the currently running, logged-in "
        "Antigravity IDE profile. This is not the standalone Antigravity App. "
        "Use this to prove the IDE profile and conversation transcripts are "
        "reachable, send to a known existing IDE conversation, and create a "
        "new IDE conversation when the same normal profile exposes CDP. If CDP "
        "is unavailable, new-conversation requests return a structured blocked "
        "result instead of falling back to another app/profile."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["health", "status", "state", "ensure_cdp", "latest", "read", "metadata", "ask", "send", "new", "project_probe"],
                "description": "IDE operation. ensure_cdp can relaunch the current IDE profile with CDP; ask with new_conversation=true uses IDE CDP when available; send requires an existing conversation_id.",
            },
            "conversation_id": {
                "type": "string",
                "description": "Antigravity IDE conversation id for read/metadata or an existing send target.",
            },
            "target": {
                "type": "string",
                "description": "Alias for conversation_id when action='metadata'.",
            },
            "message": {
                "type": "string",
                "description": "Message for action='ask' or action='send'.",
            },
            "new_conversation": {
                "type": "boolean",
                "default": False,
                "description": "Create a new Antigravity IDE conversation via the current profile's CDP endpoint when available.",
            },
            "project": {
                "type": "string",
                "description": "Requested workspace path for future project-aware IDE operations.",
            },
            "project_id": {
                "type": "string",
                "description": "Known Antigravity IDE project id, if available.",
            },
            "model": {
                "type": "string",
                "description": "Requested model for future project-aware IDE operations.",
            },
            "wait": {
                "type": "boolean",
                "default": False,
                "description": "Preserved for ask compatibility.",
            },
            "last": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 8,
                "description": "Number of transcript steps or latest conversations to return.",
            },
            "timeout": {
                "type": "number",
                "minimum": 1,
                "maximum": 120,
                "default": DEFAULT_TIMEOUT_SECONDS,
                "description": "Maximum seconds for the helper command.",
            },
            "restart": {
                "type": "boolean",
                "default": False,
                "description": "For action='ensure_cdp', allow quitting and relaunching Antigravity IDE with --remote-debugging-port.",
            },
            "force": {
                "type": "boolean",
                "default": False,
                "description": "For action='ensure_cdp', allow terminating the Antigravity IDE Electron process after graceful quit.",
            },
            "port": {
                "type": "integer",
                "minimum": 1024,
                "maximum": 65535,
                "default": 9235,
                "description": "CDP port for action='ensure_cdp'.",
            },
        },
        "required": ["action"],
    },
}


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _script_path() -> Path:
    return (
        _workspace_root()
        / "skills"
        / "my"
        / "ai-tools"
        / "hermes-management"
        / "scripts"
        / "antigravity-ide-agentapi.py"
    )


def _python_bin() -> str:
    return os.getenv("PYTHON_BIN") or shutil.which("python3") or "python3"


def check_antigravity_ide_requirements() -> bool:
    return _script_path().exists() and bool(shutil.which(_python_bin()) or Path(_python_bin()).exists())


def _normalize_action(action: Any) -> str:
    normalized = str(action or "").strip().lower().replace("-", "_")
    return {
        "health": "health",
        "status": "status",
        "state": "state",
        "list": "latest",
        "conversations": "latest",
        "conversation": "latest",
        "conversation_list": "latest",
        "ensure-cdp": "ensure_cdp",
        "project-probe": "project_probe",
    }.get(normalized, normalized)


def _timeout_value(args: Dict[str, Any]) -> float:
    try:
        timeout = float(args.get("timeout") or DEFAULT_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS
    return max(1.0, min(120.0, timeout))


def _last_value(args: Dict[str, Any]) -> int:
    try:
        value = int(args.get("last") or 8)
    except (TypeError, ValueError):
        value = 8
    return max(1, min(50, value))


def _bool_flag(value: Any) -> str:
    return "true" if bool(value) else "false"


def _clip_text(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 80] + f"\n...[clipped {len(text) - limit + 80} chars]"


def _clip_payload(value: Any, limit: int = MAX_TEXT_CHARS) -> Any:
    if isinstance(value, str):
        return _clip_text(value, limit)
    if isinstance(value, list):
        return [_clip_payload(item, max(1_000, limit // max(1, len(value)))) for item in value]
    if isinstance(value, dict):
        return {str(key): _clip_payload(item, limit) for key, item in value.items()}
    return value


def _parse_json_payload(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            parsed, _ = decoder.raw_decode(text[index:])
            return parsed
        except json.JSONDecodeError:
            continue
    return None


def _build_helper_command(action: str, args: Dict[str, Any]) -> List[str]:
    script = _script_path()
    if not script.exists():
        raise RuntimeError(f"Antigravity IDE AgentAPI helper missing: {script}")

    base = [_python_bin(), str(script)]
    if action in {"health", "status", "state"}:
        return [*base, action, "--limit", str(_last_value(args)), "--format", "json"]
    if action == "ensure_cdp":
        command = [
            *base,
            "ensure-cdp",
            "--wait",
            str(int(_timeout_value(args))),
            "--format",
            "json",
        ]
        if args.get("restart"):
            command.append("--restart")
        if args.get("force"):
            command.append("--force")
        port = args.get("port")
        if port not in (None, ""):
            command.extend(["--port", str(port)])
        return command
    if action == "latest":
        return [*base, "latest", "--limit", str(_last_value(args)), "--format", "json"]
    if action == "read":
        conversation_id = str(args.get("conversation_id") or "").strip()
        if not conversation_id:
            raise ValueError("conversation_id is required for Antigravity IDE read")
        return [
            *base,
            "read",
            "--conversation-id",
            conversation_id,
            "--last",
            str(_last_value(args)),
            "--format",
            "json",
        ]
    if action == "metadata":
        conversation_id = str(args.get("conversation_id") or args.get("target") or "").strip()
        if not conversation_id:
            raise ValueError("conversation_id or target is required for Antigravity IDE metadata")
        return [*base, "metadata", conversation_id, "--timeout", str(int(_timeout_value(args))), "--format", "json"]
    if action == "new":
        return [*base, "new", "--format", "json"]
    if action == "project_probe":
        message = str(args.get("message") or "").strip() or "Reply exactly with IDE_PROJECT_PROBE OK."
        command = [
            *base,
            "project-probe",
            message,
            "--timeout",
            str(int(_timeout_value(args))),
            "--format",
            "json",
        ]
        project_id = str(args.get("project_id") or "").strip()
        if project_id:
            command.extend(["--project-id", project_id])
        return command
    if action in {"ask", "send"}:
        message = str(args.get("message") or "").strip()
        if not message:
            raise ValueError("message is required for Antigravity IDE ask")
        conversation_id = str(args.get("conversation_id") or "").strip()
        if action == "send" and not conversation_id:
            raise ValueError("conversation_id is required for Antigravity IDE send")
        command = [
            *base,
            action,
            message,
            "--wait",
            _bool_flag(args.get("wait")),
            "--timeout",
            str(int(_timeout_value(args))),
            "--read-last",
            str(_last_value(args)),
            "--format",
            "json",
        ]
        if action == "ask":
            command.extend(["--new-conversation", _bool_flag(args.get("new_conversation"))])
        option_names = [("conversation_id", "--conversation-id")]
        if action == "ask":
            option_names.extend([
                ("project", "--project"),
                ("project_id", "--project-id"),
                ("model", "--model"),
            ])
        for arg_name, cli_name in option_names:
            value = str(args.get(arg_name) or "").strip()
            if value:
                command.extend([cli_name, value])
        return command
    raise ValueError(f"Unsupported Antigravity IDE action: {action}")


def _run_helper(command: List[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def handle_antigravity_ide_opencli(args: Dict[str, Any], **_kwargs: Any) -> str:
    action = _normalize_action(args.get("action"))
    try:
        command = _build_helper_command(action, args)
    except (RuntimeError, ValueError) as exc:
        return tool_error(
            str(exc),
            ok=False,
            action=action,
            source="opencli-agentapi-ide",
            fallback_allowed=False,
        )

    try:
        completed = _run_helper(command, _timeout_value(args) + 5.0)
    except subprocess.TimeoutExpired as exc:
        return tool_error(
            f"Antigravity IDE helper timed out after {_timeout_value(args):.1f}s",
            ok=False,
            action=action,
            source="opencli-agentapi-ide",
            stdout=_clip_text((exc.stdout or "") if isinstance(exc.stdout, str) else ""),
            stderr=_clip_text((exc.stderr or "") if isinstance(exc.stderr, str) else ""),
            fallback_allowed=False,
        )

    parsed = _parse_json_payload(completed.stdout) or _parse_json_payload(completed.stderr)
    if isinstance(parsed, dict):
        result = _clip_payload(parsed)
        result.setdefault("ok", completed.returncode == 0)
        result.setdefault("action", action)
        result.setdefault("source", "opencli-agentapi-ide")
        result["exit_code"] = completed.returncode
        if completed.stderr.strip():
            result["stderr"] = _clip_text(completed.stderr)
        return tool_result(result)

    return tool_result(
        {
            "ok": completed.returncode == 0,
            "action": action,
            "source": "opencli-agentapi-ide",
            "exit_code": completed.returncode,
            "stdout": _clip_text(completed.stdout),
            "stderr": _clip_text(completed.stderr),
            "fallback_allowed": False,
        }
    )


registry.register(
    name="antigravity_ide_opencli",
    toolset="antigravity-ide",
    schema=ANTIGRAVITY_IDE_OPENCLI_SCHEMA,
    handler=lambda args, **kw: handle_antigravity_ide_opencli(args, **kw),
    check_fn=check_antigravity_ide_requirements,
    requires_env=[],
    description=ANTIGRAVITY_IDE_OPENCLI_SCHEMA["description"],
    emoji="🛰️",
    max_result_size_chars=60_000,
)


__all__ = [
    "ANTIGRAVITY_IDE_OPENCLI_SCHEMA",
    "check_antigravity_ide_requirements",
    "handle_antigravity_ide_opencli",
]
