"""Fast Antigravity control through OpenCLI/CDP.

Hermes also has a generic ``computer_use`` tool, but Antigravity exposes a
Chrome DevTools Protocol port. For common Antigravity tasks, CDP is much
cheaper than screenshot/accessibility loops and avoids returning huge UI
snapshots to the model.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

DEFAULT_CDP_VERSION_URL = "http://127.0.0.1:9234/json/version"
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_TEXT_CHARS = 20_000
MAX_NESTED_TEXT_CHARS = 12_000


ANTIGRAVITY_OPENCLI_SCHEMA = {
    "name": "antigravity_opencli",
    "description": (
        "Fast OpenCLI/CDP control for Google Antigravity. Use this before "
        "computer_use whenever the task is to inspect, read from, send to, "
        "or manage Antigravity. Fall back to computer_use only when this "
        "tool reports that CDP/OpenCLI is unavailable or the requested action "
        "is unsupported."
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
                    "dump",
                ],
                "description": "Antigravity operation to run through OpenCLI/CDP.",
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
                "description": "Target model name when action='model'.",
            },
            "timeout": {
                "type": "number",
                "minimum": 1,
                "maximum": 120,
                "default": DEFAULT_TIMEOUT_SECONDS,
                "description": "Maximum seconds to wait for the OpenCLI command.",
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
    return (os.getenv("HERMES_ANTIGRAVITY_CDP_URL") or DEFAULT_CDP_VERSION_URL).strip()


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
    """Return True when both OpenCLI and Antigravity CDP are available."""
    return bool(_resolve_opencli_command()) and bool(_cdp_status().get("ok"))


def _normalize_action(action: Any) -> str:
    return str(action or "").strip().lower().replace("-", "_")


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
    elif action in {"status", "new", "extract_code", "dump"}:
        command.extend(["--format", "json"])
    return command


def _opencli_env() -> Dict[str, str]:
    env = dict(os.environ)
    env.setdefault("OPENCLI_CDP_TARGET", "antigravity")
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


def _display_command(command: List[str]) -> str:
    return shlex.join(command)


def _health_payload() -> Dict[str, Any]:
    command = _resolve_opencli_command()
    cdp = _cdp_status()
    return {
        "ok": bool(command) and bool(cdp.get("ok")),
        "opencli": {
            "ok": bool(command),
            "command": _display_command(command) if command else None,
        },
        "cdp": cdp,
        "preferred_over": "computer_use",
        "fallback": "Use computer_use only if OpenCLI or Antigravity CDP is unavailable.",
    }


def handle_antigravity_opencli(args: Dict[str, Any], **_kwargs: Any) -> str:
    action = _normalize_action(args.get("action"))
    if action == "health":
        return tool_result(_health_payload())

    cdp = _cdp_status()
    if not cdp.get("ok"):
        return tool_error(
            "Antigravity CDP is unavailable; start Antigravity with port 9234 or fall back to computer_use.",
            ok=False,
            action=action,
            cdp=cdp,
            fallback="computer_use",
        )

    try:
        command = _build_opencli_command(action, args)
    except (RuntimeError, ValueError) as exc:
        return tool_error(str(exc), ok=False, action=action)

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
            fallback="computer_use",
        )

    parsed = _parse_json_payload(completed.stdout)
    result: Dict[str, Any] = {
        "ok": completed.returncode == 0,
        "action": action,
        "source": "opencli-cdp",
        "command": _display_command(command),
        "exit_code": completed.returncode,
        "payload": _clip_payload(parsed) if parsed is not None else None,
        "stdout": _clip_text(completed.stdout) if parsed is None else "",
        "stderr": _clip_text(completed.stderr),
    }
    if completed.returncode != 0:
        result["error"] = "OpenCLI command failed"
        result["fallback"] = "computer_use"
        logger.debug("antigravity_opencli failed: %s", result)
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
    "handle_antigravity_opencli",
]
