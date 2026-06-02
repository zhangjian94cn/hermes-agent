"""Fast Antigravity control through OpenCLI/CDP.

Hermes also has a generic ``computer_use`` tool, but Antigravity exposes a
Chrome DevTools Protocol port. For common Antigravity tasks, CDP is much
cheaper than screenshot/accessibility loops and avoids returning huge UI
snapshots to the model.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

DEFAULT_CDP_VERSION_URL = "http://127.0.0.1:9234/json/version"
DEFAULT_DEVTOOLS_ACTIVE_PORT_FILE = (
    Path.home() / "Library" / "Application Support" / "Antigravity" / "DevToolsActivePort"
)
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_TEXT_CHARS = 20_000
MAX_NESTED_TEXT_CHARS = 12_000


ANTIGRAVITY_OPENCLI_SCHEMA = {
    "name": "antigravity_opencli",
    "description": (
        "Fast OpenCLI/CDP control for Google Antigravity. Ensure CDP is "
        "available first; this tool detects Antigravity's dynamic "
        "DevToolsActivePort and routes OpenCLI through OPENCLI_CDP_ENDPOINT. "
        "Use this before "
        "computer_use whenever the task is to inspect, read from, send to, "
        "or manage Antigravity. Do not fall back to computer_use for ordinary "
        "Antigravity requests when the CDP gate fails; report the structured "
        "failure and fix CDP first. If an action "
        "returns ok=false with fallback_allowed=false or "
        "do_not_use_computer_use=true, do not call computer_use, terminal, "
        "execute_code, search_files, or read_file; use another "
        "antigravity_opencli action such as state/models/wait/read or report "
        "the structured error. Do not inspect adapter source after a "
        "structured failure unless the user explicitly asked for debugging."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "health",
                    "status",
                    "read",
                    "send",
                    "new",
                    "extract_code",
                    "model",
                    "state",
                    "conversations",
                    "models",
                    "open_conversation",
                    "wait",
                    "stop",
                    "ask",
                    "dump",
                ],
                "description": (
                    "Antigravity operation to run through OpenCLI/CDP. Use "
                    "conversations for conversation lists and ask for "
                    "one-shot tasks. Do not use dump for ordinary user "
                    "requests; dump is diagnostic-only."
                ),
            },
            "message": {
                "type": "string",
                "description": "Message to send when action='send'.",
            },
            "last": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 5,
                "description": "Number of recent messages to read when action='read'.",
            },
            "model": {
                "type": "string",
                "description": "Target model name when action='model' or action='ask'.",
            },
            "target": {
                "type": "string",
                "description": "Conversation id or visible title when action='open_conversation'.",
            },
            "new_conversation": {
                "type": "boolean",
                "default": False,
                "description": "Start a new Antigravity conversation before sending when action='ask'.",
            },
            "wait": {
                "type": "boolean",
                "default": False,
                "description": "Wait for Antigravity to reply when action='ask'.",
            },
            "read_last": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 5,
                "description": "Number of recent messages to return when action='state', action='wait', or action='ask'.",
            },
            "timeout": {
                "type": "number",
                "minimum": 1,
                "maximum": 120,
                "default": DEFAULT_TIMEOUT_SECONDS,
                "description": "Maximum seconds to wait for the OpenCLI command.",
            },
            "diagnostic": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Required for action='dump'. Set true only for selector/DOM "
                    "debugging. For conversation lists use action='conversations'."
                ),
            },
        },
        "required": ["action"],
    },
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _local_opencli_command() -> Optional[List[str]]:
    """Return the repo-local OpenCLI command if this checkout has one."""
    node = shutil.which("node")
    if not node:
        return None

    main_js = _repo_root() / "community-opencli" / "dist" / "src" / "main.js"
    if main_js.exists():
        return [node, str(main_js)]
    return None


def _resolve_opencli_command() -> Optional[List[str]]:
    """Resolve the OpenCLI command, preferring the local checkout."""
    env_value = (
        os.getenv("HERMES_ANTIGRAVITY_OPENCLI")
        or os.getenv("HERMES_OPENCLI_BIN")
        or ""
    ).strip()
    if env_value:
        return shlex.split(env_value)

    local = _local_opencli_command()
    if local:
        return local

    binary = shutil.which("opencli")
    if binary:
        return [binary]
    return None


def _cdp_version_url() -> str:
    env_url = (os.getenv("HERMES_ANTIGRAVITY_CDP_URL") or "").strip()
    if env_url:
        return env_url
    dynamic = _detect_antigravity_cdp_endpoint()
    if dynamic:
        return f"{dynamic}/json/version"
    return DEFAULT_CDP_VERSION_URL


def _detect_antigravity_cdp_endpoint() -> Optional[str]:
    env_endpoint = (os.getenv("OPENCLI_CDP_ENDPOINT") or "").strip().rstrip("/")
    if env_endpoint:
        return env_endpoint

    port_file = Path(
        os.getenv("ANTIGRAVITY_DEVTOOLS_ACTIVE_PORT_FILE")
        or DEFAULT_DEVTOOLS_ACTIVE_PORT_FILE
    )
    try:
        port = port_file.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    if not port.isdigit():
        return None
    endpoint = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(f"{endpoint}/json/version", timeout=1.0) as response:
            if 200 <= int(response.status) < 300:
                return endpoint
    except (OSError, urllib.error.URLError, TimeoutError):
        return None
    return None


def _cdp_status(timeout: float = 1.5) -> Dict[str, Any]:
    url = _cdp_version_url()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            raw = response.read(64_000).decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": _clip_text(raw, 2_000)}
        return {"ok": True, "url": url, "payload": payload}
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        return {"ok": False, "url": url, "error": str(exc)}


def check_antigravity_requirements() -> bool:
    """Return True when OpenCLI is available.

    CDP is still required before execution, but keeping the tool visible lets
    Hermes return a structured CDP gate result instead of falling back to a
    screenshot/control loop.
    """
    return bool(_resolve_opencli_command())


def _normalize_action(action: Any) -> str:
    normalized = str(action or "").strip().lower().replace("-", "_")
    return {
        "conversation": "conversations",
        "list": "conversations",
        "list_conversation": "conversations",
        "list_conversations": "conversations",
        "conversation_list": "conversations",
        "history": "conversations",
        "open": "open_conversation",
        "open_conversations": "open_conversation",
    }.get(normalized, normalized)


def _timeout_value(args: Dict[str, Any]) -> float:
    try:
        timeout = float(args.get("timeout") or DEFAULT_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS
    return max(1.0, min(120.0, timeout))


def _last_value(args: Dict[str, Any]) -> int:
    try:
        last = int(args.get("last") or 5)
    except (TypeError, ValueError):
        last = 5
    return max(1, min(50, last))


def _read_last_value(args: Dict[str, Any]) -> int:
    try:
        value = int(args.get("read_last") or args.get("read-last") or args.get("last") or 5)
    except (TypeError, ValueError):
        value = 5
    return max(1, min(50, value))


def _bool_value(args: Dict[str, Any], name: str, default: bool = False) -> bool:
    value = args.get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _build_opencli_command(action: str, args: Dict[str, Any]) -> List[str]:
    base = _resolve_opencli_command()
    if not base:
        raise RuntimeError(
            "OpenCLI was not found. Install opencli or set HERMES_ANTIGRAVITY_OPENCLI."
        )

    subcommand = {
        "status": "status",
        "read": "read",
        "send": "send",
        "new": "new",
        "extract_code": "extract-code",
        "model": "model",
        "state": "state",
        "conversations": "conversations",
        "models": "models",
        "open_conversation": "open",
        "wait": "wait",
        "stop": "stop",
        "ask": "ask",
        "dump": "dump",
    }.get(action)
    if not subcommand:
        raise ValueError(f"Unsupported action: {action}")

    command = [*base, "antigravity", subcommand]
    if action == "read":
        command.extend(["--last", str(_last_value(args)), "--format", "json"])
    elif action == "send":
        message = str(args.get("message") or "").strip()
        if not message:
            raise ValueError("message is required when action='send'")
        command.extend([message, "--format", "json"])
    elif action == "model":
        model = str(args.get("model") or "").strip()
        if not model:
            raise ValueError("model is required when action='model'")
        command.extend([model, "--format", "json"])
    elif action == "state":
        command.extend(["--last", str(_read_last_value(args)), "--format", "json"])
    elif action == "open_conversation":
        target = str(
            args.get("target")
            or args.get("conversation_id")
            or args.get("conversation")
            or args.get("title")
            or ""
        ).strip()
        if not target:
            raise ValueError("target is required when action='open_conversation'")
        command.extend([target, "--format", "json"])
    elif action == "wait":
        command.extend(
            [
                "--timeout",
                str(int(_timeout_value(args))),
                "--read-last",
                str(_read_last_value(args)),
                "--format",
                "json",
            ]
        )
    elif action == "ask":
        message = str(args.get("message") or "").strip()
        if not message:
            raise ValueError("message is required when action='ask'")
        command.append(message)
        model = str(args.get("model") or "").strip()
        if model:
            command.extend(["--model", model])
        if _bool_value(args, "new_conversation"):
            command.extend(["--new-conversation", "true"])
        if _bool_value(args, "wait"):
            command.extend(["--wait", "true"])
        command.extend(
            [
                "--timeout",
                str(int(_timeout_value(args))),
                "--read-last",
                str(_read_last_value(args)),
                "--format",
                "json",
            ]
        )
    elif action in {
        "status",
        "new",
        "extract_code",
        "conversations",
        "models",
        "stop",
    }:
        command.extend(["--format", "json"])
    elif action == "dump":
        if not _bool_value(args, "diagnostic"):
            raise ValueError(
                "action='dump' is diagnostic-only. For Antigravity conversation "
                "lists use action='conversations'; do not use execute_code or "
                "terminal to read /tmp/antigravity-snapshot.json."
            )
        command.extend(["--format", "json"])
    return command


def _opencli_env() -> Dict[str, str]:
    env = dict(os.environ)
    env.setdefault("OPENCLI_CDP_TARGET", "antigravity")
    dynamic_endpoint = _detect_antigravity_cdp_endpoint()
    if dynamic_endpoint:
        env.setdefault("OPENCLI_CDP_ENDPOINT", dynamic_endpoint)
    env.setdefault("CI", "1")
    env.setdefault("NO_COLOR", "1")
    return env


def _run_opencli(command: List[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_opencli_env(),
        cwd=str(Path.home()),
        check=False,
    )


def _ensure_cdp_available(action: str, args: Dict[str, Any]) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    cdp = _cdp_status()
    if cdp.get("ok"):
        return cdp, None

    base = _resolve_opencli_command()
    if not base:
        return cdp, {
            "ok": False,
            "attempted": False,
            "reason": "OpenCLI was not found.",
        }

    command = [*base, "antigravity", "status", "--format", "json"]
    timeout = max(30.0, min(120.0, _timeout_value(args)))
    try:
        completed = _run_opencli(command, timeout)
    except subprocess.TimeoutExpired as exc:
        refreshed = _cdp_status()
        return refreshed, {
            "ok": False,
            "attempted": True,
            "action": action,
            "command": _display_command(command),
            "timeout": timeout,
            "stdout": _clip_text((exc.stdout or "") if isinstance(exc.stdout, str) else ""),
            "stderr": _clip_text((exc.stderr or "") if isinstance(exc.stderr, str) else ""),
            "cdp_before": cdp,
            "cdp_after": refreshed,
        }

    refreshed = _cdp_status()
    return refreshed, {
        "ok": bool(refreshed.get("ok")),
        "attempted": True,
        "action": action,
        "command": _display_command(command),
        "exit_code": completed.returncode,
        "stdout": _clip_text(completed.stdout),
        "stderr": _clip_text(completed.stderr),
        "payload": _clip_payload(_parse_json_payload(completed.stdout)),
        "cdp_before": cdp,
        "cdp_after": refreshed,
    }


def _clip_text(value: str, limit: int = MAX_TEXT_CHARS) -> str:
    if len(value) <= limit:
        return value
    omitted = len(value) - limit
    return f"{value[:limit]}\n...[truncated {omitted} chars]"


def _clip_payload(value: Any, limit: int = MAX_NESTED_TEXT_CHARS) -> Any:
    if isinstance(value, str):
        return _clip_text(value, limit)
    if isinstance(value, list):
        return [_clip_payload(item, limit) for item in value[:50]]
    if isinstance(value, dict):
        return {str(k): _clip_payload(v, limit) for k, v in value.items()}
    return value


def _parse_json_payload(stdout: str) -> Any:
    text = stdout.strip()
    if not text:
        return None

    decoder = json.JSONDecoder()
    for idx, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            parsed, _end = decoder.raw_decode(text[idx:])
            return parsed
        except json.JSONDecodeError:
            continue
    return None


_CONVERSATION_PILL_RE = re.compile(
    r"data-testid=convo-pill-([a-fA-F0-9-]+)>(.*?)</span>"
)
_PROJECT_LABEL_RE = re.compile(r"^\s*(?:\[\d+\])?<div>([^<]+)</div>\s*$")
_SEE_ALL_RE = re.compile(r"<button[^>]*>See all \((\d+)\)</button>")
_SPAN_TEXT_RE = re.compile(r"<span>([^<]+)</span>")
_MODEL_RE = re.compile(r"aria-label=Select model, current: ([^>/]+?)\s*/?>")


def _read_text_file(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _snapshot_text(raw: Any) -> str:
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith('"'):
            try:
                decoded = json.loads(stripped)
                if isinstance(decoded, str):
                    return decoded
            except json.JSONDecodeError:
                pass
        return raw
    return json.dumps(raw, ensure_ascii=False)


def _clean_label(value: str, limit: int = 240) -> str:
    cleaned = html.unescape(re.sub(r"\s+", " ", value).strip())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 3].rstrip()}..."


def _snapshot_header(text: str) -> Dict[str, Any]:
    current: Dict[str, Any] = {}
    for line in text.splitlines()[:20]:
        if line.startswith("url: "):
            url = line.removeprefix("url: ").strip()
            current["url"] = url
            match = re.search(r"/c/([a-fA-F0-9-]+)", urllib.parse.urlparse(url).path)
            if match:
                current["conversation_id"] = match.group(1)
        elif line.startswith("title: "):
            current["title"] = _clean_label(line.removeprefix("title: "))

    model_match = _MODEL_RE.search(text)
    if model_match:
        current["model"] = _clean_label(model_match.group(1), limit=120)
    return current


def _project_name_at(lines: List[str], index: int) -> Optional[str]:
    match = _PROJECT_LABEL_RE.match(lines[index])
    if not match:
        return None

    lookback = "\n".join(lines[max(0, index - 8) : index])
    if "role=button" not in lookback or "aria-expanded=" not in lookback:
        return None

    name = _clean_label(match.group(1), limit=120)
    return name or None


def _next_span_text(lines: List[str], start: int, max_lines: int = 8) -> Optional[str]:
    for line in lines[start + 1 : start + 1 + max_lines]:
        match = _SPAN_TEXT_RE.search(line)
        if match:
            return _clean_label(match.group(1), limit=40)
    return None


def _extract_antigravity_conversations_from_snapshot(text: str) -> Dict[str, Any]:
    snapshot = _snapshot_text(text)
    lines = snapshot.splitlines()
    current = _snapshot_header(snapshot)
    current_id = current.get("conversation_id")
    projects: List[Dict[str, Any]] = []
    current_project: Optional[Dict[str, Any]] = None

    def ensure_project(name: str) -> Dict[str, Any]:
        nonlocal current_project
        current_project = {"name": name, "conversations": []}
        projects.append(current_project)
        return current_project

    for index, line in enumerate(lines):
        project_name = _project_name_at(lines, index)
        if project_name:
            ensure_project(project_name)
            continue

        see_all = _SEE_ALL_RE.search(line)
        if see_all and current_project is not None:
            current_project["see_all_count"] = int(see_all.group(1))
            continue

        conversation = _CONVERSATION_PILL_RE.search(line)
        if not conversation:
            continue

        project = current_project or ensure_project("Unknown")
        conversation_id = conversation.group(1)
        item = {
            "id": conversation_id,
            "title": _clean_label(conversation.group(2)),
            "time": _next_span_text(lines, index),
        }
        if current_id and conversation_id == current_id:
            item["current"] = True
        project["conversations"].append(item)

    visible_projects = [
        project
        for project in projects
        if project.get("conversations") or project.get("see_all_count") is not None
    ]
    visible_count = sum(len(project["conversations"]) for project in visible_projects)
    return {
        "current": current,
        "visible_conversation_count": visible_count,
        "project_count": len(visible_projects),
        "projects": visible_projects,
        "note": "Only conversations visible in the Antigravity sidebar snapshot are listed.",
    }


def _dump_file_paths(payload: Any) -> Dict[str, Optional[str]]:
    item: Any = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(item, dict):
        return {"htmlFile": None, "snapFile": None}
    return {
        "htmlFile": item.get("htmlFile"),
        "snapFile": item.get("snapFile"),
    }


def _conversation_result_from_dump(payload: Any) -> Dict[str, Any]:
    files = _dump_file_paths(payload)
    snap_file = files.get("snapFile")
    if not snap_file:
        return {
            "ok": False,
            "error": "OpenCLI dump did not return snapFile; cannot list conversations.",
            "dump_files": files,
        }

    try:
        raw_snapshot = _read_text_file(snap_file)
    except OSError as exc:
        return {
            "ok": False,
            "error": f"Failed to read Antigravity snapshot: {exc}",
            "dump_files": files,
        }

    data = _extract_antigravity_conversations_from_snapshot(raw_snapshot)
    data["ok"] = True
    data["dump_files"] = files
    return data


HIGH_LEVEL_ACTIONS = {
    "status",
    "state",
    "conversations",
    "models",
    "model",
    "open_conversation",
    "new",
    "read",
    "send",
    "wait",
    "stop",
    "ask",
    "extract_code",
}

STRUCTURED_FAILURE_GUIDANCE = (
    "OpenCLI/CDP handled this request and returned a structured Antigravity "
    "state or validation failure. Do not call computer_use, terminal, or "
    "execute_code for this result; use antigravity_opencli "
    "state/models/wait/read/conversations, adjust the requested arguments, "
    "or report the structured error. For ordinary user requests, do not "
    "inspect repository source with search_files/read_file unless the user "
    "explicitly asks to debug or patch the adapter."
)

UNAVAILABLE_GUIDANCE = (
    "Antigravity CDP must be available before running App E2E operations. "
    "Run the OpenCLI ensure-cdp/status path first; do not silently fall back "
    "to computer_use for ordinary Antigravity requests."
)

MODEL_FAILURE_HINTS = (
    "model",
    "not found",
    "not verified",
    "unavailable",
    "currently generating",
    "生成",
    "模型",
)

FOLLOWUP_GUARD_TTL_SECONDS = 90
BLOCKED_STRUCTURED_FALLBACK_TOOLS = {
    "computer_use",
    "execute_code",
    "read_file",
    "search_files",
    "terminal",
}

_STRUCTURED_FAILURE_FOLLOWUP_GUARDS: Dict[str, Dict[str, Any]] = {}


def _guard_key(task_id: str = "", session_id: str = "") -> str:
    if session_id:
        return f"session:{session_id}"
    if task_id:
        return f"task:{task_id}"
    return "global"


def _looks_like_explicit_debug_request(user_task: Optional[str]) -> bool:
    text = str(user_task or "").lower()
    if not text.strip():
        return False
    debug_terms = (
        "debug",
        "diagnose",
        "troubleshoot",
        "source",
        "adapter",
        "patch",
        "fix",
        "investigate",
        "排查",
        "调试",
        "源码",
        "代码",
        "修复",
        "改一下",
        "看日志",
    )
    return any(term in text for term in debug_terms)


def _looks_like_antigravity_followup_request(user_task: Optional[str]) -> bool:
    text = str(user_task or "").lower()
    if not text.strip():
        return True
    antigravity_terms = (
        "antigravity",
        "anti gravity",
        "opencli",
        "openclaw",
        "app operator",
        "gemini",
        "claude",
        "opus",
        "sonnet",
        "模型",
        "切模型",
        "新对话",
        "发 hello",
        "发hello",
        "哈喽",
        "hello",
    )
    unrelated_app_terms = (
        "codex",
        "uu",
        "u u",
        "远程",
        "微信",
        "飞书",
        "浏览器",
        "chrome",
        "powerpoint",
        "word",
    )
    if any(term in text for term in unrelated_app_terms) and not any(term in text for term in antigravity_terms):
        return False
    return any(term in text for term in antigravity_terms)


def clear_antigravity_opencli_followup_guard(task_id: str = "", session_id: str = "") -> None:
    _STRUCTURED_FAILURE_FOLLOWUP_GUARDS.pop(_guard_key(task_id, session_id), None)


def update_antigravity_opencli_followup_guard(
    result: str | Dict[str, Any],
    *,
    task_id: str = "",
    session_id: str = "",
) -> None:
    try:
        payload = json.loads(result) if isinstance(result, str) else result
    except json.JSONDecodeError:
        return
    if not isinstance(payload, dict):
        return

    key = _guard_key(task_id, session_id)
    if payload.get("ok") is not False:
        _STRUCTURED_FAILURE_FOLLOWUP_GUARDS.pop(key, None)
        return

    should_block = (
        payload.get("fallback_allowed") is False
        and (
            payload.get("do_not_use_computer_use") is True
            or payload.get("do_not_search_or_read_source") is True
            or payload.get("do_not_debug_adapter") is True
        )
    )
    if not should_block:
        _STRUCTURED_FAILURE_FOLLOWUP_GUARDS.pop(key, None)
        return

    _STRUCTURED_FAILURE_FOLLOWUP_GUARDS[key] = {
        "created_at": time.monotonic(),
        "action": payload.get("action"),
        "reason": payload.get("reason") or payload.get("error") or payload.get("guidance"),
        "next_actions": payload.get("next_actions") or [],
    }


def antigravity_opencli_followup_block_message(
    function_name: str,
    *,
    task_id: str = "",
    session_id: str = "",
    user_task: Optional[str] = None,
) -> Optional[str]:
    if function_name not in BLOCKED_STRUCTURED_FALLBACK_TOOLS:
        return None
    if _looks_like_explicit_debug_request(user_task):
        return None
    if not _looks_like_antigravity_followup_request(user_task):
        _STRUCTURED_FAILURE_FOLLOWUP_GUARDS.pop(_guard_key(task_id, session_id), None)
        return None

    key = _guard_key(task_id, session_id)
    guard = _STRUCTURED_FAILURE_FOLLOWUP_GUARDS.get(key)
    if not guard:
        return None
    age = time.monotonic() - float(guard.get("created_at") or 0)
    if age > FOLLOWUP_GUARD_TTL_SECONDS:
        _STRUCTURED_FAILURE_FOLLOWUP_GUARDS.pop(key, None)
        return None

    return (
        "Blocked by antigravity_opencli structured-failure guard: the previous "
        "Antigravity operation already returned a structured OpenCLI/CDP "
        "failure with fallback_allowed=false. Do not use computer_use, "
        "execute_code, terminal, search_files, or read_file for this ordinary "
        "Antigravity request. Use antigravity_opencli "
        "state/models/wait/read/conversations, follow "
        f"next_actions={guard.get('next_actions')}, or report the structured "
        f"error. Previous action={guard.get('action')} reason={guard.get('reason')}"
    )


def _failure_text(value: Any) -> str:
    if isinstance(value, dict):
        parts: List[str] = []
        for key in ("error", "reason", "message", "currentModel", "current_model", "model"):
            item = value.get(key)
            if item:
                parts.append(str(item))
        steps = value.get("steps")
        if isinstance(steps, list):
            for step in steps:
                if isinstance(step, dict) and step.get("ok") is False:
                    parts.append(_failure_text(step))
        return " ".join(parts)
    if isinstance(value, list):
        return " ".join(_failure_text(item) for item in value)
    return str(value or "")


def _next_actions_for_failure(action: str, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    text = _failure_text(payload).lower()
    next_actions: List[Dict[str, Any]] = []

    next_actions.append({"action": "state", "reason": "Confirm current Antigravity state."})
    if "currently generating" in text or "生成" in text:
        next_actions.extend(
            [
                {"action": "wait", "reason": "Wait until the current response finishes."},
                {"action": "stop", "reason": "Stop generation only if the user asked to interrupt it."},
            ]
        )
    if action in {"model", "ask"} or any(hint in text for hint in MODEL_FAILURE_HINTS):
        next_actions.append({"action": "models", "reason": "Refresh the model menu through OpenCLI/CDP."})
    if action in {"send", "ask"}:
        next_actions.append({"action": "read", "reason": "Read recent messages to confirm whether the prompt was accepted."})
    if action == "conversations":
        next_actions.append({"action": "state", "reason": "Read current conversation state without a DOM dump."})
    return next_actions


def _mark_structured_failure(result: Dict[str, Any]) -> Dict[str, Any]:
    result.setdefault("fallback_allowed", False)
    result.setdefault("do_not_use_computer_use", True)
    result.setdefault("do_not_debug_adapter", True)
    result.setdefault("do_not_search_or_read_source", True)
    result.setdefault("fallback", None)
    result.setdefault("fallback_reason", "structured_opencli_result")
    result.setdefault("guidance", STRUCTURED_FAILURE_GUIDANCE)
    result.setdefault("next_actions", _next_actions_for_failure(str(result.get("action") or ""), result))
    return result


def _display_command(command: List[str]) -> str:
    return shlex.join(command)


def _health_payload() -> Dict[str, Any]:
    command = _resolve_opencli_command()
    cdp = _cdp_status()
    ok = bool(command) and bool(cdp.get("ok"))
    return {
        "ok": ok,
        "opencli": {
            "ok": bool(command),
            "command": _display_command(command) if command else None,
        },
        "cdp": cdp,
        "preferred_over": "computer_use",
        "fallback_allowed": False,
        "fallback": None,
        "guidance": (
            UNAVAILABLE_GUIDANCE
            if not ok
            else "Use antigravity_opencli actions for Antigravity operations; do not use computer_use for ordinary requests."
        ),
    }


def handle_antigravity_opencli(args: Dict[str, Any], **_kwargs: Any) -> str:
    action = _normalize_action(args.get("action"))
    if action == "health":
        return tool_result(_health_payload())

    cdp, cdp_ensure = _ensure_cdp_available(action, args)
    if not cdp.get("ok"):
        return tool_error(
            "Antigravity CDP is unavailable after ensure-cdp; do not run the E2E operation yet.",
            ok=False,
            action=action,
            cdp=cdp,
            cdp_ensure=cdp_ensure,
            fallback_allowed=False,
            fallback=None,
            do_not_use_computer_use=True,
            do_not_debug_adapter=True,
            do_not_search_or_read_source=True,
            guidance=UNAVAILABLE_GUIDANCE,
        )

    try:
        command = _build_opencli_command(action, args)
    except (RuntimeError, ValueError) as exc:
        message = str(exc)
        fallback_allowed = "OpenCLI was not found" in message or message.startswith("Unsupported action")
        return tool_error(
            message,
            ok=False,
            action=action,
            fallback_allowed=fallback_allowed,
            do_not_use_computer_use=not fallback_allowed,
            do_not_debug_adapter=not fallback_allowed,
            do_not_search_or_read_source=not fallback_allowed,
            fallback="computer_use" if fallback_allowed else None,
            guidance=UNAVAILABLE_GUIDANCE if fallback_allowed else STRUCTURED_FAILURE_GUIDANCE,
            next_actions=[]
            if fallback_allowed
            else _next_actions_for_failure(action, {"error": message}),
        )

    try:
        completed = _run_opencli(command, _timeout_value(args))
    except subprocess.TimeoutExpired as exc:
        return tool_error(
            f"OpenCLI timed out after {_timeout_value(args):.1f}s",
            ok=False,
            action=action,
            command=_display_command(command),
            stdout=_clip_text((exc.stdout or "") if isinstance(exc.stdout, str) else ""),
            stderr=_clip_text((exc.stderr or "") if isinstance(exc.stderr, str) else ""),
            fallback_allowed=False,
            do_not_use_computer_use=True,
            do_not_debug_adapter=True,
            do_not_search_or_read_source=True,
            fallback=None,
            guidance=STRUCTURED_FAILURE_GUIDANCE,
            next_actions=_next_actions_for_failure(
                action,
                {"error": f"OpenCLI timed out after {_timeout_value(args):.1f}s"},
            ),
        )

    parsed = _parse_json_payload(completed.stdout)
    if action == "conversations" and isinstance(parsed, list):
        conversation_result = (
            _conversation_result_from_dump(parsed)
            if completed.returncode == 0
            else {"ok": False}
        )
        result = {
            "ok": completed.returncode == 0 and conversation_result.get("ok") is True,
            "action": action,
            "source": "opencli-cdp",
            "cdp_ensure": cdp_ensure,
            "command": _display_command(command),
            "exit_code": completed.returncode,
            "stderr": _clip_text(completed.stderr),
            **conversation_result,
        }
    elif action in HIGH_LEVEL_ACTIONS and isinstance(parsed, dict):
        clipped = _clip_payload(parsed)
        result = {
            "ok": completed.returncode == 0 and clipped.get("ok", True) is not False,
            "action": action,
            "source": "opencli-cdp",
            "cdp_ensure": cdp_ensure,
            "command": _display_command(command),
            "exit_code": completed.returncode,
            "stderr": _clip_text(completed.stderr),
            **clipped,
        }
    else:
        result = {
            "ok": completed.returncode == 0,
            "action": action,
            "source": "opencli-cdp",
            "cdp_ensure": cdp_ensure,
            "command": _display_command(command),
            "exit_code": completed.returncode,
            "payload": _clip_payload(parsed) if parsed is not None else None,
            "stdout": _clip_text(completed.stdout) if parsed is None else "",
            "stderr": _clip_text(completed.stderr),
        }
    if completed.returncode != 0:
        result["error"] = "OpenCLI command failed"
        result["fallback_allowed"] = False
        result["do_not_use_computer_use"] = True
        result["do_not_debug_adapter"] = True
        result["do_not_search_or_read_source"] = True
        result["fallback"] = None
        result["fallback_reason"] = "opencli_command_failed"
        result["guidance"] = STRUCTURED_FAILURE_GUIDANCE
        logger.debug("antigravity_opencli failed: %s", result)
    if result.get("ok") is False:
        _mark_structured_failure(result)
    return tool_result(result)


registry.register(
    name="antigravity_opencli",
    toolset="antigravity",
    schema=ANTIGRAVITY_OPENCLI_SCHEMA,
    handler=lambda args, **kw: handle_antigravity_opencli(args, **kw),
    check_fn=check_antigravity_requirements,
    requires_env=[],
    description=ANTIGRAVITY_OPENCLI_SCHEMA["description"],
    emoji="🛰️",
    max_result_size_chars=60_000,
)


__all__ = [
    "ANTIGRAVITY_OPENCLI_SCHEMA",
    "check_antigravity_requirements",
    "clear_antigravity_opencli_followup_guard",
    "handle_antigravity_opencli",
    "update_antigravity_opencli_followup_guard",
    "antigravity_opencli_followup_block_message",
]
