"""Fast Codex App control through OpenCLI/CDP.

The generic ``computer_use`` tool can drive Codex visually, but common Codex
operations are cheaper and more reliable through the app's CDP endpoint plus
the repo-local OpenCLI adapter. This tool exposes one Hermes entrypoint with
structured actions so messaging agents do not need to write temporary code,
parse screenshots, or dump large DOM snapshots for normal tasks.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.cdp_probe import (
    normalize_cdp_endpoint as _normalize_cdp_endpoint,
    probe_codex_cdp_endpoint as _shared_probe_codex_cdp_endpoint,
)
from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

FALLBACK_CDP_PORTS_WHEN_CONFIG_MISSING = ("9222", "9238", "9239")
DEFAULT_DEVTOOLS_ACTIVE_PORT_FILE = (
    Path.home() / "Library" / "Application Support" / "Codex" / "DevToolsActivePort"
)
DEFAULT_HERMES_MANAGEMENT_CONFIG = (
    Path(__file__).resolve().parents[2].parent
    / "skills"
    / "my"
    / "ai-tools"
    / "hermes-management"
    / "config.yaml"
)
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_TEXT_CHARS = 20_000
MAX_NESTED_TEXT_CHARS = 12_000
LOCAL_ABSOLUTE_PATH_RE = re.compile(
    r"(?P<path>/(?:Users|Volumes|private|tmp|var|opt|home|mnt|workspace)[^\s`'\"，。；;,\)\]\}]+)"
)


CODEX_OPENCLI_SCHEMA = {
    "name": "codex_opencli",
    "description": (
        "Fast OpenCLI/CDP control for the Codex App. Use this before "
        "computer_use whenever the task is to inspect, read from, send to, "
        "or manage Codex App conversations. Fall back to computer_use only "
        "when this tool explicitly returns fallback_allowed=true because "
        "OpenCLI is unavailable or the requested action is unsupported. "
        "When CDP is unavailable, call action='ensure_cdp' first. "
        "If an action returns ok=false with fallback_allowed=false or "
        "do_not_use_computer_use=true, do not call computer_use; use another "
        "codex_opencli action such as state/models/wait/stop or report the "
        "structured error. Do not inspect adapter source after a structured "
        "failure unless the user explicitly asked for debugging."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "health",
                    "ensure_cdp",
                    "status",
                    "state",
                    "projects",
                    "conversations",
                    "models",
                    "model",
                    "open_conversation",
                    "new",
                    "read",
                    "send",
                    "ask",
                    "wait",
                    "stop",
                    "export",
                    "extract_diff",
                    "dump",
                ],
                "description": (
                    "Codex App operation to run through OpenCLI/CDP. Use "
                    "ensure_cdp to start a CDP-enabled Codex instance before "
                    "projects/conversations for conversation lists and ask "
                    "for one-shot tasks. Do not use dump for ordinary user "
                    "requests; dump is diagnostic-only."
                ),
            },
            "message": {
                "type": "string",
                "description": "Message to send when action='send' or action='ask'.",
            },
            "model": {
                "type": "string",
                "description": "Target model name when action='model' or action='ask'.",
            },
            "project": {
                "type": "string",
                "description": "Codex project label or path for project/conversation selection.",
            },
            "conversation": {
                "type": "string",
                "description": "Visible conversation title for selection.",
            },
            "thread_id": {
                "type": "string",
                "description": "Exact Codex thread id for selection, usually local:<uuid>.",
            },
            "index": {
                "type": "integer",
                "minimum": 1,
                "description": "1-based conversation index within the selected project.",
            },
            "target": {
                "type": "string",
                "description": "Conversation title or thread id when action='open_conversation'.",
            },
            "new_conversation": {
                "type": "boolean",
                "default": False,
                "description": "Start a new Codex conversation before sending when action='ask'.",
            },
            "wait": {
                "type": "boolean",
                "default": False,
                "description": "Wait for Codex to reply when action='ask'.",
            },
            "last": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 5,
                "description": "Number of recent messages to read when action='read'.",
            },
            "read_last": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 5,
                "description": "Number of recent messages to return when action='state', action='wait', or action='ask'.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "Optional max conversations per project when action='projects'.",
            },
            "output": {
                "type": "string",
                "description": "Output file for action='export'.",
            },
            "timeout": {
                "type": "number",
                "minimum": 1,
                "maximum": 120,
                "default": DEFAULT_TIMEOUT_SECONDS,
                "description": "Maximum seconds to wait for the OpenCLI command.",
            },
            "port": {
                "type": "integer",
                "minimum": 1024,
                "maximum": 65535,
                "description": "Override the configured preferred CDP port when action='ensure_cdp'.",
            },
            "restart": {
                "type": "boolean",
                "default": False,
                "description": "For action='ensure_cdp', allow quitting Codex before relaunching if a side-by-side CDP copy cannot be started.",
            },
            "force": {
                "type": "boolean",
                "default": False,
                "description": "For action='ensure_cdp', allow a forceful process cleanup after graceful quit fails.",
            },
            "force_new": {
                "type": "boolean",
                "default": False,
                "description": (
                    "For action='ensure_cdp', launch or select a side-by-side "
                    "Codex CDP endpoint even if another Codex CDP endpoint is "
                    "already ready. This is intended for isolated new-conversation tests."
                ),
            },
            "isolate_if_busy": {
                "type": "boolean",
                "default": False,
                "description": (
                    "For action='ask' with new_conversation=true, use a "
                    "side-by-side CDP endpoint if the current Codex endpoint is "
                    "generating or has unsent composer text. Defaults to false "
                    "because reusing the existing logged-in Codex profile is preferred."
                ),
            },
            "auto_ensure_cdp": {
                "type": "boolean",
                "default": True,
                "description": (
                    "For high-level actions, automatically launch/recover a "
                    "CDP-enabled Codex instance before running the requested "
                    "operation when CDP is unavailable or has no inspectable target."
                ),
            },
            "diagnostic": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Required for action='dump'. Set true only for selector/DOM "
                    "debugging. For project/conversation lists use action='projects'."
                ),
            },
        },
        "required": ["action"],
    },
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _hermes_management_config_path() -> Path:
    return Path(os.getenv("HERMES_MANAGEMENT_CONFIG") or DEFAULT_HERMES_MANAGEMENT_CONFIG).expanduser()


def _load_hermes_management_config() -> Dict[str, Any]:
    path = _hermes_management_config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore
            parsed = yaml.safe_load(text) or {}
        except Exception:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _codex_cdp_config() -> Dict[str, Any]:
    data = _load_hermes_management_config()
    cdp = (((data.get("app_control") or {}).get("codex") or {}).get("cdp") or {})
    return cdp if isinstance(cdp, dict) else {}


def _configured_preferred_cdp_port() -> int:
    env_port = (os.getenv("HERMES_CODEX_CDP_PORT") or "").strip()
    if env_port:
        try:
            port = int(env_port)
            if 1024 <= port <= 65535:
                return port
        except ValueError:
            pass
    try:
        port = int(_codex_cdp_config().get("preferred_port") or FALLBACK_CDP_PORTS_WHEN_CONFIG_MISSING[0])
    except (TypeError, ValueError):
        port = int(FALLBACK_CDP_PORTS_WHEN_CONFIG_MISSING[0])
    if 1024 <= port <= 65535:
        return port
    return int(FALLBACK_CDP_PORTS_WHEN_CONFIG_MISSING[0])


def _configured_cdp_ports() -> List[int]:
    ports: List[int] = []

    def add(value: Any) -> None:
        try:
            port = int(value)
        except (TypeError, ValueError):
            return
        if 1024 <= port <= 65535 and port not in ports:
            ports.append(port)

    add(_configured_preferred_cdp_port())
    for item in _codex_cdp_config().get("fallback_ports") or FALLBACK_CDP_PORTS_WHEN_CONFIG_MISSING:
        add(item)
    if not ports:
        add(FALLBACK_CDP_PORTS_WHEN_CONFIG_MISSING[0])
    return ports


def _configured_devtools_active_port_file() -> Path:
    value = (
        os.getenv("CODEX_DEVTOOLS_ACTIVE_PORT_FILE")
        or str(_codex_cdp_config().get("devtools_active_port_file") or "")
        or str(DEFAULT_DEVTOOLS_ACTIVE_PORT_FILE)
    )
    return Path(value).expanduser()


def _configured_cdp_wait_seconds() -> float:
    env_wait = (os.getenv("HERMES_CODEX_CDP_WAIT_SECONDS") or "").strip()
    if env_wait:
        try:
            return max(1.0, min(120.0, float(env_wait)))
        except ValueError:
            pass
    try:
        return max(1.0, min(120.0, float(_codex_cdp_config().get("wait_seconds") or 15)))
    except (TypeError, ValueError):
        return 15.0


def _local_opencli_command() -> Optional[List[str]]:
    node = shutil.which("node")
    if not node:
        return None
    main_js = _repo_root() / "community-opencli" / "dist" / "src" / "main.js"
    if main_js.exists():
        return [node, str(main_js)]
    return None


def _resolve_opencli_command() -> Optional[List[str]]:
    env_value = (
        os.getenv("HERMES_CODEX_OPENCLI")
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


def _endpoint_port(endpoint_or_url: str) -> Optional[int]:
    parsed = urllib.parse.urlparse(_normalize_cdp_endpoint(endpoint_or_url))
    try:
        return int(parsed.port) if parsed.port is not None else None
    except ValueError:
        return None


def _read_codex_devtools_endpoint() -> Optional[str]:
    port_file = _configured_devtools_active_port_file()
    try:
        port = port_file.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    if not port.isdigit():
        return None
    return f"http://127.0.0.1:{port}"


def _candidate_codex_cdp_endpoints() -> List[str]:
    candidates: List[str] = []

    def add(endpoint: Optional[str]) -> None:
        if not endpoint:
            return
        normalized = _normalize_cdp_endpoint(endpoint)
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    add(os.getenv("HERMES_CODEX_CDP_URL") or "")
    add(os.getenv("OPENCLI_CDP_ENDPOINT") or "")
    add(_read_codex_devtools_endpoint())
    for port in _configured_cdp_ports():
        add(f"http://127.0.0.1:{port}")
    return candidates


def _probe_codex_cdp_endpoint(endpoint: str, timeout: float) -> Dict[str, Any]:
    return _shared_probe_codex_cdp_endpoint(endpoint, timeout=timeout)


def _detect_codex_cdp_endpoint() -> Optional[str]:
    for endpoint in _candidate_codex_cdp_endpoints():
        status = _probe_codex_cdp_endpoint(endpoint, timeout=1.0)
        if status.get("ok"):
            return str(status.get("endpoint") or endpoint)
    return None

def _cdp_status(timeout: float = 1.5) -> Dict[str, Any]:
    checked: List[Dict[str, Any]] = []
    fallback_url = f"http://127.0.0.1:{_configured_preferred_cdp_port()}/json/version"
    endpoints = _candidate_codex_cdp_endpoints() or [_normalize_cdp_endpoint(fallback_url)]
    for endpoint in endpoints:
        status = _probe_codex_cdp_endpoint(endpoint, timeout=timeout)
        if status.get("ok"):
            status["checked_urls"] = checked + [{"url": status.get("url"), "endpoint": status.get("endpoint"), "ok": True}]
            return status
        checked.append({
            "url": status.get("url"),
            "endpoint": status.get("endpoint"),
            "ok": False,
            "error": status.get("error"),
        })
    first = checked[0] if checked else {"url": fallback_url, "error": "no CDP candidates"}
    return {
        "ok": False,
        "endpoint": first.get("endpoint") or _normalize_cdp_endpoint(str(first.get("url") or fallback_url)),
        "url": first.get("url", fallback_url),
        "error": first.get("error", "Codex CDP unavailable"),
        "checked_urls": checked,
    }


def check_codex_requirements() -> bool:
    """Return True when Hermes can expose the Codex control tool.

    CDP may be down precisely when the model needs ``ensure_cdp``. Do not hide
    the whole toolset just because the recoverable runtime endpoint is absent.
    """
    return bool(_resolve_opencli_command())


def _normalize_action(action: Any) -> str:
    normalized = str(action or "").strip().lower().replace("-", "_")
    return {
        "ensure-cdp": "ensure_cdp",
        "conversation": "conversations",
        "list": "projects",
        "list_conversation": "projects",
        "list_conversations": "projects",
        "conversation_list": "projects",
        "history": "projects",
        "open": "open_conversation",
        "open_conversations": "open_conversation",
        "extract_diff": "extract_diff",
        "extract-diff": "extract_diff",
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


def _infer_existing_local_project_path(text: str) -> Optional[str]:
    for match in LOCAL_ABSOLUTE_PATH_RE.finditer(str(text or "")):
        raw = match.group("path").rstrip(".,;:，。；：")
        try:
            path = Path(raw).expanduser()
        except (OSError, ValueError):
            continue
        if path.is_file():
            path = path.parent
        if path.is_dir():
            return str(path)
    return None


def _with_inferred_codex_project(action: str, args: Dict[str, Any]) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    if action != "ask" or not _bool_value(args, "new_conversation"):
        return args, None
    if str(args.get("project") or "").strip():
        return args, None
    project = _infer_existing_local_project_path(str(args.get("message") or ""))
    if not project:
        return args, None
    updated = dict(args)
    updated["project"] = project
    return updated, {"source": "message_path", "project": project}


def _append_selection_args(command: List[str], args: Dict[str, Any]) -> None:
    project = str(args.get("project") or "").strip()
    conversation = str(args.get("conversation") or "").strip()
    thread_id = str(args.get("thread_id") or args.get("thread-id") or "").strip()
    index = args.get("index")
    if project:
        command.extend(["--project", project])
    if conversation:
        command.extend(["--conversation", conversation])
    if thread_id:
        command.extend(["--thread-id", thread_id])
    if index not in (None, ""):
        command.extend(["--index", str(index)])


def _has_selection(args: Dict[str, Any]) -> bool:
    return any(str(args.get(name) or "").strip() for name in ("project", "conversation", "thread_id", "thread-id", "index", "target"))


def _build_opencli_command(action: str, args: Dict[str, Any]) -> List[str]:
    base = _resolve_opencli_command()
    if not base:
        raise RuntimeError("OpenCLI was not found. Install opencli or set HERMES_CODEX_OPENCLI.")

    subcommand = {
        "status": "status",
        "state": "state",
        "projects": "conversations",
        "conversations": "conversations",
        "models": "models",
        "model": "model",
        "open_conversation": "open",
        "new": "new",
        "read": "read",
        "send": "send",
        "ask": "ask",
        "wait": "wait",
        "stop": "stop",
        "export": "export",
        "extract_diff": "extract-diff",
        "dump": "dump",
    }.get(action)
    if not subcommand:
        raise ValueError(f"Unsupported action: {action}")

    command = [*base, "codex", subcommand]
    if action == "state":
        command.extend(["--last", str(_read_last_value(args)), "--format", "json"])
    elif action in {"projects", "conversations"}:
        limit = args.get("limit")
        if limit not in (None, ""):
            command.extend(["--limit", str(limit)])
        command.extend(["--format", "json"])
    elif action == "model":
        model = str(args.get("model") or "").strip()
        if not model:
            raise ValueError("model is required when action='model'")
        command.extend([model, "--format", "json"])
    elif action == "open_conversation":
        target = str(args.get("target") or "").strip()
        if target:
            command.append(target)
        _append_selection_args(command, args)
        if not _has_selection(args):
            raise ValueError("target, project, conversation, thread_id, or index is required when action='open_conversation'")
        command.extend(["--format", "json"])
    elif action == "new":
        project = str(args.get("project") or "").strip()
        if project:
            command.extend(["--project", project])
        command.extend(["--format", "json"])
    elif action == "read":
        _append_selection_args(command, args)
        command.extend(["--last", str(_read_last_value(args)), "--format", "json"])
    elif action == "send":
        message = str(args.get("message") or "").strip()
        if not message:
            raise ValueError("message is required when action='send'")
        command.append(message)
        _append_selection_args(command, args)
        command.extend(["--format", "json"])
    elif action == "ask":
        message = str(args.get("message") or "").strip()
        if not message:
            raise ValueError("message is required when action='ask'")
        command.append(message)
        model = str(args.get("model") or "").strip()
        if model:
            command.extend(["--model", model])
        _append_selection_args(command, args)
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
    elif action == "wait":
        command.extend([
            "--timeout",
            str(int(_timeout_value(args))),
            "--read-last",
            str(_read_last_value(args)),
            "--format",
            "json",
        ])
    elif action == "export":
        output = str(args.get("output") or "").strip()
        if output:
            command.extend(["--output", output])
        command.extend(["--format", "json"])
    elif action == "dump":
        if not _bool_value(args, "diagnostic"):
            raise ValueError(
                "action='dump' is diagnostic-only. For Codex project or "
                "conversation lists use action='projects' or action='conversations'; "
                "do not use execute_code or terminal to read /tmp/codex-snapshot.json."
            )
        command.extend(["--format", "json"])
    elif action in {"status", "models", "stop", "extract_diff"}:
        command.extend(["--format", "json"])
    return command


def _opencli_env(cdp_endpoint: Optional[str] = None) -> Dict[str, str]:
    env = dict(os.environ)
    env.setdefault("OPENCLI_CDP_TARGET", "codex")
    if cdp_endpoint:
        env["OPENCLI_CDP_ENDPOINT"] = _normalize_cdp_endpoint(cdp_endpoint)
    else:
        dynamic_endpoint = _detect_codex_cdp_endpoint()
        if dynamic_endpoint:
            env.setdefault("OPENCLI_CDP_ENDPOINT", dynamic_endpoint)
    env.setdefault("CI", "1")
    env.setdefault("NO_COLOR", "1")
    return env


def _run_opencli(
    command: List[str],
    timeout: float,
    cdp_endpoint: Optional[str] = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_opencli_env(cdp_endpoint=cdp_endpoint),
        cwd=str(Path.home()),
        check=False,
    )


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


HIGH_LEVEL_ACTIONS = {
    "state",
    "projects",
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
}

STRUCTURED_FAILURE_GUIDANCE = (
    "OpenCLI/CDP handled this request and returned a structured Codex state "
    "or validation failure. Do not call computer_use for this result; use "
    "codex_opencli state/models/wait/stop/read, adjust the requested "
    "arguments, or report the structured error. For ordinary user requests, "
    "do not inspect repository source with search_files/read_file unless the "
    "user explicitly asks to debug or patch the adapter."
)

UNAVAILABLE_GUIDANCE = (
    "OpenCLI/CDP is unavailable. computer_use is allowed only for this "
    "availability fallback or for unsupported UI outside the OpenCLI action set."
)

MODEL_FAILURE_HINTS = (
    "model",
    "not found",
    "not verified",
    "unavailable",
    "currently generating",
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


def _looks_like_codex_followup_request(user_task: Optional[str]) -> bool:
    text = str(user_task or "").lower()
    if not text.strip():
        return True
    codex_terms = (
        "codex",
        "code x",
        "opencli",
        "openclaw",
        "app operator",
        "gpt",
        "gpt-",
        "5.4",
        "5.5",
        "模型",
        "切模型",
        "新对话",
        "发 hello",
        "发hello",
        "哈喽",
        "hello",
    )
    unrelated_app_terms = (
        "uu",
        "u u",
        "远程",
        "微信",
        "飞书",
        "浏览器",
        "chrome",
        "antigravity",
        "powerpoint",
        "word",
    )
    if any(term in text for term in unrelated_app_terms) and not any(term in text for term in codex_terms):
        return False
    return any(term in text for term in codex_terms)


def clear_codex_opencli_followup_guard(task_id: str = "", session_id: str = "") -> None:
    """Clear the Codex OpenCLI follow-up guard for tests and session cleanup."""
    _STRUCTURED_FAILURE_FOLLOWUP_GUARDS.pop(_guard_key(task_id, session_id), None)


def update_codex_opencli_followup_guard(
    result: str | Dict[str, Any],
    *,
    task_id: str = "",
    session_id: str = "",
) -> None:
    """Record whether a Codex OpenCLI result should block fallback tooling.

    A structured OpenCLI failure with ``fallback_allowed=false`` is already a
    complete result for ordinary Codex user intent. Without this short-lived
    guard, weak models often ignore the guidance and start reading adapter
    source or switching to computer_use anyway.
    """
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


def codex_opencli_followup_block_message(
    function_name: str,
    *,
    task_id: str = "",
    session_id: str = "",
    user_task: Optional[str] = None,
) -> Optional[str]:
    """Return a block message for invalid fallback after Codex OpenCLI failure."""
    if function_name not in BLOCKED_STRUCTURED_FALLBACK_TOOLS:
        return None
    if _looks_like_explicit_debug_request(user_task):
        return None
    if not _looks_like_codex_followup_request(user_task):
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
        "Blocked by codex_opencli structured-failure guard: the previous "
        "Codex App operation already returned a structured OpenCLI/CDP "
        "failure with fallback_allowed=false. Do not use computer_use, "
        "execute_code, terminal, search_files, or read_file for this ordinary "
        "Codex request. Use codex_opencli state/models/wait/stop/read, follow "
        f"next_actions={guard.get('next_actions')}, or report the structured "
        f"error. Previous action={guard.get('action')} reason={guard.get('reason')}"
    )


def _failure_text(value: Any) -> str:
    if isinstance(value, dict):
        parts: List[str] = []
        for key in ("error", "reason", "message", "currentModel", "current_model"):
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

    if action == "ask" and ("timed out" in text or "timeout" in text):
        return [
            {"action": "state", "reason": "Confirm whether the previous Codex message was sent before the timeout."},
            {"action": "wait", "reason": "Wait/read the current Codex response; do not send a duplicate ask yet."},
        ]

    if "currently generating" in text or action in {"model", "ask"}:
        next_actions.append({"action": "state", "reason": "Confirm current Codex state."})
    if "currently generating" in text:
        next_actions.extend(
            [
                {"action": "wait", "reason": "Wait until the current response finishes."},
                {"action": "stop", "reason": "Stop generation only if the user asked to interrupt it."},
            ]
        )
    if action in {"model", "ask"} or any(hint in text for hint in MODEL_FAILURE_HINTS):
        next_actions.append({"action": "models", "reason": "Refresh the model menu through OpenCLI/CDP."})
    if action == "ask":
        next_actions.append(
            {
                "action": "ask",
                "reason": "Retry only after correcting model/conversation arguments or waiting for Codex.",
            }
        )

    if not next_actions:
        next_actions.append({"action": "state", "reason": "Inspect current Codex state through OpenCLI/CDP."})
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


def _state_for_endpoint(endpoint: Optional[str], timeout: float = 8.0) -> Dict[str, Any]:
    try:
        command = _build_opencli_command("state", {"read_last": 1})
        completed = _run_opencli(command, timeout, cdp_endpoint=endpoint)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    parsed = _parse_json_payload(completed.stdout)
    if isinstance(parsed, dict):
        return {
            "ok": completed.returncode == 0 and parsed.get("ok", True) is not False,
            "exit_code": completed.returncode,
            **_clip_payload(parsed),
        }
    return {
        "ok": False,
        "exit_code": completed.returncode,
        "stdout": _clip_text(completed.stdout),
        "stderr": _clip_text(completed.stderr),
    }


def _state_has_busy_current_context(state: Dict[str, Any]) -> bool:
    composer = state.get("composer") if isinstance(state.get("composer"), dict) else {}
    return bool(state.get("is_generating") or composer.get("hasText"))


def _maybe_prepare_isolated_new_conversation_endpoint(
    action: str,
    args: Dict[str, Any],
    cdp: Dict[str, Any],
) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
    if action != "ask" or not _bool_value(args, "new_conversation"):
        return None, None
    if not _bool_value(args, "isolate_if_busy"):
        return None, None

    current_endpoint = str(cdp.get("endpoint") or "")
    current_state = _state_for_endpoint(current_endpoint, timeout=min(8.0, _timeout_value(args)))
    if not _state_has_busy_current_context(current_state):
        return None, None

    ensure_args = {
        **args,
        "force_new": True,
        "timeout": max(15.0, min(_timeout_value(args), 30.0)),
    }
    isolated = _ensure_cdp_payload(ensure_args)
    if not isolated.get("ok"):
        return None, {
            "ok": False,
            "action": action,
            "error": (
                "Current Codex endpoint is busy and isolated new-conversation "
                "CDP recovery failed."
            ),
            "current_state": current_state,
            "auto_ensure_cdp": isolated,
            "fallback_allowed": False,
            "fallback": None,
            "do_not_use_computer_use": True,
            "do_not_debug_adapter": True,
            "do_not_search_or_read_source": True,
            "guidance": (
                "Do not send the F-07 new-conversation prompt to the busy "
                "current Codex endpoint. Retry after the current generation "
                "finishes or allow an explicit restart if a stale process holds "
                "all configured CDP ports."
            ),
            "next_actions": [
                {"action": "wait", "reason": "Wait for current Codex generation to finish."},
                {"action": "ensure_cdp", "reason": "Retry with force_new=true or restart=true only if allowed."},
            ],
        }

    endpoint = ""
    isolated_cdp = isolated.get("cdp")
    if isinstance(isolated_cdp, dict):
        endpoint = str(isolated_cdp.get("endpoint") or "")
    if not endpoint:
        endpoint = _detect_codex_cdp_endpoint() or ""
    return endpoint or None, {
        "ok": True,
        "current_state": current_state,
        "auto_ensure_cdp": isolated,
    }


def _codex_port_value(args: Dict[str, Any]) -> int:
    try:
        port = int(args.get("port") or _configured_preferred_cdp_port())
    except (TypeError, ValueError):
        port = _configured_preferred_cdp_port()
    if port < 1024 or port > 65535:
        return _configured_preferred_cdp_port()
    return port


def _codex_port_candidates(args: Dict[str, Any]) -> List[int]:
    values = [_codex_port_value(args)]
    values.extend(_configured_cdp_ports())
    deduped: List[int] = []
    for value in values:
        if value not in deduped:
            deduped.append(value)
    return deduped


def _filter_existing_cdp_port(candidates: List[int], before: Dict[str, Any]) -> List[int]:
    existing_port = _endpoint_port(str(before.get("endpoint") or before.get("url") or ""))
    if not existing_port:
        return candidates
    filtered = [port for port in candidates if port != existing_port]
    return filtered or candidates


def _launch_codex_cdp(port: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["open", "-n", "-a", "Codex", "--args", f"--remote-debugging-port={port}"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _quit_codex(force: bool = False) -> Dict[str, Any]:
    result: Dict[str, Any] = {"attempted": True, "force": force}
    try:
        graceful = subprocess.run(
            ["osascript", "-e", 'tell application "Codex" to quit'],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        result["graceful"] = {
            "returncode": graceful.returncode,
            "stdout": _clip_text(graceful.stdout),
            "stderr": _clip_text(graceful.stderr),
        }
    except Exception as exc:
        result["graceful"] = {"returncode": None, "error": str(exc)}
    if force:
        try:
            forced = subprocess.run(
                ["pkill", "-TERM", "-f", "/Applications/Codex.app/Contents/MacOS/Codex"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            result["forced"] = {
                "returncode": forced.returncode,
                "stdout": _clip_text(forced.stdout),
                "stderr": _clip_text(forced.stderr),
            }
        except Exception as exc:
            result["forced"] = {"returncode": None, "error": str(exc)}
    return result


def _wait_for_codex_cdp(
    timeout: float,
    preferred_ports: List[int],
    *,
    include_existing_candidates: bool = True,
) -> Dict[str, Any]:
    deadline = time.monotonic() + max(1.0, timeout)
    last: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        candidates = [f"http://127.0.0.1:{port}" for port in preferred_ports]
        if include_existing_candidates:
            candidates.extend(_candidate_codex_cdp_endpoints())
        for endpoint in dict.fromkeys(candidates):
            status = _probe_codex_cdp_endpoint(endpoint, timeout=1.0)
            last = status
            if status.get("ok"):
                return status
        time.sleep(0.5)
    return {
        "ok": False,
        "error": "Timed out waiting for Codex CDP page target.",
        "last_status": last,
    }


def _ensure_cdp_payload(args: Dict[str, Any]) -> Dict[str, Any]:
    before = _cdp_status()
    force_new = _bool_value(args, "force_new")
    if before.get("ok") and not force_new:
        return {
            "ok": True,
            "action": "ensure_cdp",
            "source": "codex-cdp",
            "status": "already_ready",
            "launched": False,
            "cdp": before,
            "fallback_allowed": False,
            "fallback": None,
        }

    timeout = _timeout_value(args)
    restart = _bool_value(args, "restart")
    force = _bool_value(args, "force")
    candidates = _codex_port_candidates(args)
    if force_new:
        candidates = _filter_existing_cdp_port(candidates, before)
    wait_seconds = _configured_cdp_wait_seconds()
    launches: List[Dict[str, Any]] = []

    if restart:
        quit_result = _quit_codex(force=force)
        time.sleep(1.0)
    else:
        quit_result = None

    for port in candidates:
        completed = _launch_codex_cdp(port)
        launches.append({
            "port": port,
            "command": _display_command(["open", "-n", "-a", "Codex", "--args", f"--remote-debugging-port={port}"]),
            "returncode": completed.returncode,
            "stdout": _clip_text(completed.stdout),
            "stderr": _clip_text(completed.stderr),
        })
        if completed.returncode != 0:
            continue
        if force_new:
            ready = _wait_for_codex_cdp(
                timeout=min(timeout, wait_seconds),
                preferred_ports=[port],
                include_existing_candidates=False,
            )
        else:
            ready = _wait_for_codex_cdp(timeout=min(timeout, wait_seconds), preferred_ports=[port])
        if ready.get("ok"):
            return {
                "ok": True,
                "action": "ensure_cdp",
                "source": "codex-cdp",
                "status": "launched",
                "launched": True,
                "port": port,
                "cdp": ready,
                "before": before,
                "quit": quit_result,
                "launches": launches,
                "fallback_allowed": False,
                "fallback": None,
            }

    return {
        "ok": False,
        "action": "ensure_cdp",
        "source": "codex-cdp",
        "status": "unavailable",
        "error": "Unable to launch a Codex CDP instance with an inspectable page target.",
        "before": before,
        "quit": quit_result,
        "launches": launches,
        "fallback_allowed": False,
        "fallback": None,
        "guidance": (
            "Run codex_opencli action='ensure_cdp' with restart=true if a stale "
            "Codex process is holding the debug port, then retry state/status."
        ),
        "next_actions": [{"action": "ensure_cdp", "reason": "Retry with restart=true if the user allows quitting Codex."}],
    }


def _health_payload() -> Dict[str, Any]:
    command = _resolve_opencli_command()
    cdp = _cdp_status()
    ok = bool(command) and bool(cdp.get("ok"))
    opencli_ok = bool(command)
    cdp_ok = bool(cdp.get("ok"))
    return {
        "ok": ok,
        "opencli": {
            "ok": opencli_ok,
            "command": _display_command(command) if command else None,
        },
        "cdp": cdp,
        "preferred_over": "computer_use",
        "fallback_allowed": not opencli_ok,
        "fallback": "computer_use" if not opencli_ok else None,
        "guidance": (
            "Use codex_opencli actions for Codex App operations; do not use computer_use for ordinary Codex requests."
            if ok
            else (
                "Call codex_opencli action='ensure_cdp' to launch a Codex CDP instance, then retry health/state."
                if opencli_ok and not cdp_ok
                else UNAVAILABLE_GUIDANCE
            )
        ),
        "next_actions": (
            [{"action": "ensure_cdp", "reason": "Launch or recover the Codex CDP page target."}]
            if opencli_ok and not cdp_ok
            else []
        ),
    }


def handle_codex_opencli(args: Dict[str, Any], **_kwargs: Any) -> str:
    action = _normalize_action(args.get("action"))
    if action == "health":
        return tool_result(_health_payload())
    if action == "ensure_cdp":
        return tool_result(_ensure_cdp_payload(args))
    effective_args, inferred_project = _with_inferred_codex_project(action, args)

    auto_ensure_result: Optional[Dict[str, Any]] = None
    cdp = _cdp_status()
    if not cdp.get("ok") and action in HIGH_LEVEL_ACTIONS and _bool_value(effective_args, "auto_ensure_cdp", True):
        auto_ensure_result = _ensure_cdp_payload(effective_args)
        if auto_ensure_result.get("ok"):
            cdp = auto_ensure_result.get("cdp") if isinstance(auto_ensure_result.get("cdp"), dict) else _cdp_status()

        if cdp.get("ok"):
            logger.info(
                "codex_opencli auto ensure_cdp recovered CDP for action=%s status=%s",
                action,
                auto_ensure_result.get("status") if isinstance(auto_ensure_result, dict) else None,
            )
        else:
            next_actions = [
                {"action": "ensure_cdp", "reason": "Launch or recover a Codex CDP instance with an inspectable page target."},
                {"action": action, "reason": "Retry the original Codex operation after ensure_cdp succeeds."},
            ]
            if isinstance(auto_ensure_result, dict) and auto_ensure_result.get("next_actions"):
                next_actions = auto_ensure_result.get("next_actions") + next_actions
            return tool_error(
                "Codex App CDP is unavailable or has no inspectable page target; automatic ensure_cdp did not recover it.",
                ok=False,
                action=action,
                cdp=cdp,
                auto_ensure_cdp=auto_ensure_result,
                fallback_allowed=False,
                fallback=None,
                do_not_use_computer_use=True,
                do_not_debug_adapter=True,
                do_not_search_or_read_source=True,
                guidance=(
                    "codex_opencli tried to launch a CDP-enabled Codex instance "
                    "automatically. If this failed because an existing Codex "
                    "process is stale, retry action='ensure_cdp' with "
                    "restart=true only when the user allows quitting Codex."
                ),
                next_actions=next_actions,
            )

    if not cdp.get("ok"):
        return tool_error(
            "Codex App CDP is unavailable or has no inspectable page target; run codex_opencli action='ensure_cdp' before retrying this Codex operation.",
            ok=False,
            action=action,
            cdp=cdp,
            fallback_allowed=False,
            fallback=None,
            do_not_use_computer_use=True,
            do_not_debug_adapter=True,
            do_not_search_or_read_source=True,
            guidance=(
                "Call codex_opencli with action='ensure_cdp' to launch a "
                "CDP-enabled Codex instance, then retry the requested action. "
                "Only use computer_use after ensure_cdp fails or a user-visible "
                "permission/system dialog must be handled."
            ),
            next_actions=[
                {"action": "ensure_cdp", "reason": "Launch or recover a Codex CDP instance with an inspectable page target."},
                {"action": action, "reason": "Retry the original Codex operation after ensure_cdp succeeds."},
            ],
        )

    selected_cdp_endpoint: Optional[str] = None
    isolated_context: Optional[Dict[str, Any]] = None
    selected_cdp_endpoint, isolated_context = _maybe_prepare_isolated_new_conversation_endpoint(action, effective_args, cdp)
    if isinstance(isolated_context, dict) and isolated_context.get("ok") is False:
        return tool_error(
            str(isolated_context.get("error") or "Unable to prepare isolated Codex CDP endpoint."),
            **isolated_context,
        )

    try:
        command = _build_opencli_command(action, effective_args)
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
        if selected_cdp_endpoint:
            completed = _run_opencli(command, _timeout_value(effective_args), cdp_endpoint=selected_cdp_endpoint)
        else:
            completed = _run_opencli(command, _timeout_value(effective_args))
    except subprocess.TimeoutExpired as exc:
        return tool_error(
            f"OpenCLI timed out after {_timeout_value(effective_args):.1f}s",
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
                {"error": f"OpenCLI timed out after {_timeout_value(effective_args):.1f}s"},
            ),
        )

    parsed = _parse_json_payload(completed.stdout)
    if action in HIGH_LEVEL_ACTIONS and isinstance(parsed, dict):
        clipped = _clip_payload(parsed)
        result = {
            "ok": completed.returncode == 0 and clipped.get("ok", True) is not False,
            "action": action,
            "source": "opencli-cdp",
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
            "command": _display_command(command),
            "exit_code": completed.returncode,
            "payload": _clip_payload(parsed) if parsed is not None else None,
            "stdout": _clip_text(completed.stdout) if parsed is None else "",
            "stderr": _clip_text(completed.stderr),
        }
    if isinstance(auto_ensure_result, dict):
        result["auto_ensure_cdp"] = _clip_payload(auto_ensure_result)
    if isinstance(inferred_project, dict):
        result["inferred_project"] = inferred_project
    if isinstance(isolated_context, dict):
        result["isolated_new_conversation_cdp"] = _clip_payload(isolated_context)
        if selected_cdp_endpoint:
            result["selected_cdp_endpoint"] = selected_cdp_endpoint
    if completed.returncode != 0:
        result["error"] = "OpenCLI command failed"
        result["fallback_allowed"] = False
        result["do_not_use_computer_use"] = True
        result["do_not_debug_adapter"] = True
        result["do_not_search_or_read_source"] = True
        result["fallback"] = None
        result["fallback_reason"] = "opencli_command_failed"
        result["guidance"] = STRUCTURED_FAILURE_GUIDANCE
        logger.debug("codex_opencli failed: %s", result)
    if result.get("ok") is False:
        _mark_structured_failure(result)
    return tool_result(result)


registry.register(
    name="codex_opencli",
    toolset="codex",
    schema=CODEX_OPENCLI_SCHEMA,
    handler=lambda args, **kw: handle_codex_opencli(args, **kw),
    check_fn=check_codex_requirements,
    requires_env=[],
    description=CODEX_OPENCLI_SCHEMA["description"],
    emoji="🧭",
    max_result_size_chars=60_000,
)


__all__ = [
    "CODEX_OPENCLI_SCHEMA",
    "codex_opencli_followup_block_message",
    "check_codex_requirements",
    "clear_codex_opencli_followup_guard",
    "handle_codex_opencli",
    "update_codex_opencli_followup_guard",
]
