"""Fast Claude desktop App control through OpenCLI/CDP.

``opencli claude`` controls claude.ai through Chrome Browser Bridge. This tool
is intentionally separate: it targets ``/Applications/Claude.app`` through the
repo-local ``opencli claude-app`` desktop adapter when Claude exposes CDP.
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import plistlib
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

DEFAULT_CDP_VERSION_URL = "http://127.0.0.1:9242/json/version"
DEFAULT_CDP_PORT = 9242
DEFAULT_APP_PATH = Path("/Applications/Claude.app")
DEFAULT_PROCESS_NAME = "Claude"
DEFAULT_CLAUDE_APP_MODE = "code"
DEFAULT_CDP_LOG_PATH = Path("/tmp/claude-app-opencli-cdp.log")
DEFAULT_DEVTOOLS_ACTIVE_PORT_FILE = (
    Path.home() / "Library" / "Application Support" / "Claude" / "DevToolsActivePort"
)
DEFAULT_PATCHED_APP_PATH = Path.home() / "Applications" / "Claude-CDP.app"
CLAUDE_APP_CDP_PATCH_VERSION = os.getenv("CLAUDE_APP_CDP_PATCH_VERSION", "5")
PATCH_STAMP_RELATIVE_PATH = Path("Contents/Resources/.opencli-cdp-source.json")
DEFAULT_WRAPPER_RELATIVE_PATH = (
    "skills/my/ai-tools/hermes-management/scripts/claude-app-opencli.sh"
)
EXPECTED_BUNDLE_ID = "com.anthropic.claudefordesktop"
EXPECTED_TEAM_IDENTIFIER = "Q6L2SF6YDW"
DEFAULT_TIMEOUT_SECONDS = 20.0
ASK_PROCESS_TIMEOUT_BUFFER_SECONDS = 45.0
MAX_TEXT_CHARS = 20_000
MAX_NESTED_TEXT_CHARS = 12_000


CLAUDE_APP_OPENCLI_SCHEMA = {
    "name": "claude_app_opencli",
    "description": (
        "OpenCLI/CDP control for the Claude desktop App. Use this only when "
        "the target is /Applications/Claude.app. For claude.ai through Chrome "
        "Browser Bridge, use the separate claude OpenCLI route; for CLI coding "
        "sessions, use claude inside tmux."
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
                    "projects",
                    "project",
                    "history",
                    "detail",
                    "new",
                    "read",
                    "send",
                    "ask",
                    "dump",
                    "screenshot",
                ],
                "description": "Claude desktop App operation to run through OpenCLI/CDP.",
            },
            "message": {
                "type": "string",
                "description": "Message to send when action='send' or action='ask'.",
            },
            "conversation_id": {
                "type": "string",
                "description": (
                    "Claude conversation/session id. Used by action='detail', and by "
                    "action='send'/'ask' to select a specific Chat conversation or Code session."
                ),
            },
            "project": {
                "type": "string",
                "description": (
                    "Claude project id or title. Used by action='project', or before "
                    "action='send'/'ask' to bind the App to a project. In Code mode "
                    "without new=true this selects a matching local Code session by "
                    "cwd/title/session id; with new=true or action='new' it is also accepted "
                    "as the new Code session folder selector."
                ),
            },
            "folder": {
                "type": "string",
                "description": (
                    "Local folder path or recent-folder name for a new Claude App Code session. "
                    "Use this with action='new' or action='ask'/'send' plus new=true."
                ),
            },
            "cwd": {
                "type": "string",
                "description": "Alias for folder when starting a new Claude App Code session.",
            },
            "workspace": {
                "type": "string",
                "description": "Alias for folder when starting a new Claude App Code session.",
            },
            "branch": {
                "type": "string",
                "description": "Optional branch/worktree label for a new Claude App Code session.",
            },
            "mode": {
                "type": "string",
                "enum": ["chat", "cowork", "code"],
                "default": DEFAULT_CLAUDE_APP_MODE,
                "description": (
                    "Claude App mode to select before project/conversation operations. "
                    "Default is 'code'; use 'chat' or 'cowork' only when explicitly needed."
                ),
            },
            "new": {
                "type": "boolean",
                "default": False,
                "description": "Start a new Claude App chat before sending; in Code mode this opens the Code new-session page.",
            },
            "restart": {
                "type": "boolean",
                "default": False,
                "description": "For action='ensure_cdp', allow quitting and relaunching Claude App if it is already running without CDP.",
            },
            "force": {
                "type": "boolean",
                "default": False,
                "description": "For action='ensure_cdp', allow SIGKILL if graceful quit does not finish.",
            },
            "port": {
                "type": "integer",
                "minimum": 1,
                "maximum": 65535,
                "default": DEFAULT_CDP_PORT,
                "description": "CDP port for action='ensure_cdp'.",
            },
            "model": {
                "type": "string",
                "enum": ["sonnet", "opus", "haiku"],
                "description": "Model key to select when action='new' or action='ask'/'send' with new=true.",
            },
            "effort": {
                "type": "string",
                "enum": ["low", "medium", "high", "extra_high", "xhigh", "max"],
                "description": "Claude App Code effort to select when starting a new Code session.",
            },
            "think": {
                "type": "boolean",
                "default": False,
                "description": "Enable Adaptive thinking when action='ask'.",
            },
            "file": {
                "type": "string",
                "description": "Optional file path to attach when action='ask'.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 20,
                "description": "Max rows when action='history' or action='projects'.",
            },
            "output": {
                "type": "string",
                "description": "Output file path for action='screenshot'.",
            },
            "timeout": {
                "type": "number",
                "minimum": 1,
                "maximum": 180,
                "default": DEFAULT_TIMEOUT_SECONDS,
                "description": "Maximum seconds to wait for OpenCLI or a Claude response.",
            },
            "diagnostic": {
                "type": "boolean",
                "default": False,
                "description": "Required for dump/screenshot diagnostic actions.",
            },
        },
        "required": ["action"],
    },
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


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
        os.getenv("HERMES_CLAUDE_APP_OPENCLI")
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


def _detect_claude_app_cdp_endpoint() -> Optional[str]:
    env_endpoint = (os.getenv("OPENCLI_CDP_ENDPOINT") or "").strip().rstrip("/")
    if env_endpoint:
        return env_endpoint

    port_file = Path(
        os.getenv("CLAUDE_APP_DEVTOOLS_ACTIVE_PORT_FILE")
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
            if 200 <= getattr(response, "status", 200) < 400:
                return endpoint
    except (OSError, urllib.error.URLError, TimeoutError):
        return None
    return None


def _cdp_version_url() -> str:
    env_url = (os.getenv("HERMES_CLAUDE_APP_CDP_URL") or "").strip()
    if env_url:
        return env_url
    dynamic_endpoint = _detect_claude_app_cdp_endpoint()
    if dynamic_endpoint:
        return f"{dynamic_endpoint}/json/version"
    return DEFAULT_CDP_VERSION_URL


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


def _patched_copy_required() -> bool:
    return os.getenv("CLAUDE_APP_CDP_PREPARE_PATCHED_COPY", "1") != "0"


def _patched_app_path() -> Path:
    return Path(
        os.getenv("CLAUDE_APP_CDP_PATCHED_APP_PATH") or DEFAULT_PATCHED_APP_PATH
    ).expanduser()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_claude_app_identity(app_path: Path = DEFAULT_APP_PATH) -> Dict[str, str]:
    info_plist = app_path / "Contents" / "Info.plist"
    asar_path = app_path / "Contents" / "Resources" / "app.asar"
    version = "unknown"
    if info_plist.exists():
        try:
            with info_plist.open("rb") as handle:
                version = str(plistlib.load(handle).get("CFBundleShortVersionString") or "unknown")
        except Exception:
            version = "unknown"
    asar_sha = ""
    if asar_path.exists():
        asar_sha = _sha256_file(asar_path)
    return {
        "path": str(app_path),
        "version": version,
        "app_asar_sha256": asar_sha,
    }


def _patched_copy_status() -> Dict[str, Any]:
    patched_app = _patched_app_path()
    stamp_path = patched_app / PATCH_STAMP_RELATIVE_PATH
    executable = patched_app / "Contents" / "MacOS" / DEFAULT_PROCESS_NAME
    source = _source_claude_app_identity(DEFAULT_APP_PATH)
    stamp: Dict[str, Any] = {}
    stamp_error: Optional[str] = None
    if stamp_path.exists():
        try:
            stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - diagnostics should not hide parse failures
            stamp_error = str(exc)

    required = _patched_copy_required()
    current = False
    reason = "current"
    if not required:
        current = True
        reason = "patched-copy-disabled"
    elif not executable.exists():
        reason = "missing-patched-executable"
    elif not stamp_path.exists():
        reason = "missing-source-stamp"
    elif stamp_error:
        reason = "invalid-source-stamp"
    elif not source.get("app_asar_sha256"):
        reason = "missing-source-asar"
    elif (
        stamp.get("source_version") == source.get("version")
        and stamp.get("source_asar_sha256") == source.get("app_asar_sha256")
        and str(stamp.get("patch_version") or "") == CLAUDE_APP_CDP_PATCH_VERSION
    ):
        current = True
        reason = "current"
    else:
        reason = "source-version-asar-sha-or-patch-version-mismatch"

    return {
        "patched_copy_required": required,
        "patched_path": str(patched_app),
        "patched_installed": patched_app.exists(),
        "patched_copy_current": current,
        "patched_copy_reason": reason,
        "source": source,
        "patched_source": {
            "version": stamp.get("source_version"),
            "app_asar_sha256": stamp.get("source_asar_sha256"),
            "stamp_path": str(stamp_path),
            "stamp_error": stamp_error,
            "patch_version": stamp.get("patch_version"),
            "expected_patch_version": CLAUDE_APP_CDP_PATCH_VERSION,
        },
    }


def _codesign_identity(app_path: Path) -> Dict[str, Any]:
    if not app_path.exists():
        return {"ok": False, "path": str(app_path), "error": "app_missing"}
    codesign = shutil.which("codesign") or "/usr/bin/codesign"
    try:
        completed = subprocess.run(
            [codesign, "-dv", "--verbose=4", str(app_path)],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "path": str(app_path), "error": str(exc)}

    text = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    identity: Dict[str, Any] = {
        "ok": completed.returncode == 0,
        "path": str(app_path),
        "identifier": "",
        "team_identifier": "",
        "authorities": [],
        "trusted_for_claude_auth": False,
    }
    authorities: List[str] = []
    for line in text.splitlines():
        if line.startswith("Identifier="):
            identity["identifier"] = line.split("=", 1)[1].strip()
        elif line.startswith("TeamIdentifier="):
            identity["team_identifier"] = line.split("=", 1)[1].strip()
        elif line.startswith("Authority="):
            authorities.append(line.split("=", 1)[1].strip())
    identity["authorities"] = authorities
    identity["trusted_for_claude_auth"] = (
        identity["identifier"] == EXPECTED_BUNDLE_ID
        and identity["team_identifier"] == EXPECTED_TEAM_IDENTIFIER
    )
    if not identity["ok"]:
        identity["diagnostic"] = _clip_text(text, 1_000)
    return identity


def _auth_identity_status() -> Dict[str, Any]:
    patched = _patched_copy_status()
    source = _codesign_identity(DEFAULT_APP_PATH)
    patched_identity = _codesign_identity(_patched_app_path())
    required = bool(patched.get("patched_copy_required"))
    trusted = (
        bool(source.get("trusted_for_claude_auth"))
        if not required
        else bool(patched_identity.get("trusted_for_claude_auth"))
    )
    return {
        "ok": trusted,
        "expected_bundle_id": EXPECTED_BUNDLE_ID,
        "expected_team_identifier": EXPECTED_TEAM_IDENTIFIER,
        "source_codesign": source,
        "patched_codesign": patched_identity,
        "patched_copy": patched,
        "blocking": False,
        "policy": "diagnostic_only_opencli_login_reuse_is_authoritative",
    }


def _login_reuse_status() -> Dict[str, Any]:
    command_base = _resolve_opencli_command()
    cdp = _cdp_status()
    if not command_base:
        return {
            "ok": False,
            "source": "opencli-status",
            "error": "OpenCLI was not found.",
            "cdp": cdp,
        }
    if not cdp.get("ok"):
        return {
            "ok": False,
            "source": "opencli-status",
            "error": "Claude App CDP is unavailable.",
            "cdp": cdp,
        }

    command = [*command_base, "claude-app", "status", "--format", "json"]
    try:
        completed = _run_opencli(command, 20.0)
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "source": "opencli-status",
            "command": _display_command(command),
            "error": "opencli claude-app status timed out",
            "stdout": _clip_text((exc.stdout or "") if isinstance(exc.stdout, str) else ""),
            "stderr": _clip_text((exc.stderr or "") if isinstance(exc.stderr, str) else ""),
            "cdp": cdp,
        }

    parsed = _parse_json_payload(completed.stdout)
    row: Dict[str, Any] = {}
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        row = parsed[0]
    elif isinstance(parsed, dict):
        row = parsed

    login = str(row.get("Login") or "").strip()
    status = str(row.get("Status") or "").strip()
    has_composer = str(row.get("HasComposer") or "").strip()
    verified = (
        completed.returncode == 0
        and login.lower() == "yes"
        and status.lower() in {"connected", "ok", ""}
    )
    result: Dict[str, Any] = {
        "ok": verified,
        "source": "opencli-status",
        "command": _display_command(command),
        "exit_code": completed.returncode,
        "login": login,
        "status": status,
        "has_composer": has_composer,
        "mode": row.get("Mode") or row.get("ModeKey") or "",
        "url": row.get("Url") or "",
        "title": row.get("Title") or "",
        "payload": _clip_payload(parsed) if parsed is not None else None,
        "stderr": _clip_text(completed.stderr),
        "cdp": cdp,
    }
    if not verified:
        if login:
            result["error"] = f"Claude App CDP login is {login!r}, expected 'Yes'"
        elif completed.returncode != 0:
            result["error"] = "opencli claude-app status failed"
        else:
            result["error"] = "opencli claude-app status did not expose Login=Yes"
    return result


def _is_patched_copy_current(status: Optional[Dict[str, Any]] = None) -> bool:
    status = status or _patched_copy_status()
    return bool(
        (not status.get("patched_copy_required"))
        or status.get("patched_copy_current")
    )


def _stale_patched_copy_payload(
    action: str,
    cdp: Optional[Dict[str, Any]] = None,
    status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    status = status or _patched_copy_status()
    payload: Dict[str, Any] = {
        "ok": False,
        "action": action,
        "error": (
            "Claude App CDP is reachable, but Claude-CDP.app is not based on "
            "the current /Applications/Claude.app. Run action='ensure_cdp' "
            "with restart=true and force=true before using Claude App OpenCLI."
        ),
        "requires_restart": True,
        "stale_patched_copy": True,
        "fallback_allowed": False,
        "fallback": None,
        "patched_copy": status,
    }
    if cdp is not None:
        payload["cdp"] = cdp
        payload["endpoint"] = _endpoint_from_version_url(str(cdp.get("url") or ""))
    return payload


def _login_reuse_failed_payload(
    action: str,
    cdp: Optional[Dict[str, Any]] = None,
    login_reuse: Optional[Dict[str, Any]] = None,
    auth: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    login_reuse = login_reuse or _login_reuse_status()
    auth = auth or _auth_identity_status()
    payload: Dict[str, Any] = {
        "ok": False,
        "action": action,
        "error": (
            "Claude-CDP.app did not prove reuse of the existing logged-in profile. "
            "OpenCLI status must report Login=Yes; do not ask the user to relogin."
        ),
        "blocked_reason": "BLOCKED_CLAUDE_APP_LOGIN_PROMPT_REUSE_EXISTING_PROFILE_ONLY",
        "requires_restart": False,
        "fallback_allowed": False,
        "fallback": None,
        "login_reuse_verified": False,
        "login_reuse": login_reuse,
        "auth_identity": auth,
    }
    if cdp is not None:
        payload["cdp"] = cdp
        payload["endpoint"] = _endpoint_from_version_url(str(cdp.get("url") or ""))
    return payload


def _endpoint_from_version_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return url.rsplit("/json", 1)[0].rstrip("/")


def _is_claude_app_running() -> bool:
    try:
        subprocess.run(
            ["pgrep", "-x", DEFAULT_PROCESS_NAME],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def _wait_for_claude_app_exit(timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _is_claude_app_running():
            return True
        time.sleep(0.2)
    return not _is_claude_app_running()


def _launch_claude_app_with_cdp(port: int) -> None:
    executable = DEFAULT_APP_PATH / "Contents" / "MacOS" / DEFAULT_PROCESS_NAME
    if not executable.exists():
        raise RuntimeError(f"Claude executable not found: {executable}")

    DEFAULT_CDP_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DEFAULT_CDP_LOG_PATH.open("ab") as log_file:
        subprocess.Popen(
            [str(executable), f"--remote-debugging-port={port}"],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
            close_fds=True,
        )


def _probe_cdp_version_url(version_url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(version_url, timeout=timeout) as response:
            return 200 <= getattr(response, "status", 200) < 400
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def _wait_for_cdp_version_url(version_url: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _probe_cdp_version_url(version_url):
            return True
        dynamic = _detect_claude_app_cdp_endpoint()
        if dynamic and _probe_cdp_version_url(f"{dynamic}/json/version"):
            return True
        time.sleep(0.5)
    return False


def _wrapper_script_path() -> Optional[Path]:
    env_path = (os.getenv("HERMES_CLAUDE_APP_WRAPPER") or "").strip()
    if env_path:
        path = Path(env_path).expanduser()
        return path if path.exists() else None

    candidate = _repo_root().parent / DEFAULT_WRAPPER_RELATIVE_PATH
    return candidate if candidate.exists() else None


def _run_wrapper_ensure_cdp(args: Dict[str, Any], port: int, restart: bool, force: bool) -> Dict[str, Any]:
    wrapper = _wrapper_script_path()
    if not wrapper:
        return {
            "ok": False,
            "action": "ensure_cdp",
            "endpoint": f"http://127.0.0.1:{port}",
            "error": (
                "Claude App CDP wrapper not found. Expected "
                f"{_repo_root().parent / DEFAULT_WRAPPER_RELATIVE_PATH} or set HERMES_CLAUDE_APP_WRAPPER."
            ),
            "source": "claude-app-opencli-wrapper",
        }

    command = ["bash", str(wrapper), "ensure-cdp", "--json", "--port", str(port)]
    if restart:
        command.append("--restart")
    if force:
        command.append("--force")

    env = dict(os.environ)
    env.setdefault("CLAUDE_APP_CDP_PORT", str(port))
    timeout = max(_timeout_value(args), 120.0)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=str(_repo_root().parent),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "action": "ensure_cdp",
            "endpoint": f"http://127.0.0.1:{port}",
            "source": "claude-app-opencli-wrapper",
            "command": _display_command(command),
            "error": f"Claude App CDP wrapper timed out after {timeout:.1f}s",
            "stdout": _clip_text((exc.stdout or "") if isinstance(exc.stdout, str) else ""),
            "stderr": _clip_text((exc.stderr or "") if isinstance(exc.stderr, str) else ""),
        }

    parsed = _parse_json_payload(completed.stdout)
    result: Dict[str, Any] = {
        "ok": completed.returncode == 0,
        "action": "ensure_cdp",
        "endpoint": f"http://127.0.0.1:{port}",
        "source": "claude-app-opencli-wrapper",
        "command": _display_command(command),
        "exit_code": completed.returncode,
        "payload": _clip_payload(parsed) if parsed is not None else None,
        "stdout": _clip_text(completed.stdout) if parsed is None else "",
        "stderr": _clip_text(completed.stderr),
        "running": _is_claude_app_running(),
        "restarted": restart,
        "launched": True,
    }
    if isinstance(parsed, dict):
        result["ok"] = bool(parsed.get("ok")) and completed.returncode == 0
        result["endpoint"] = str(parsed.get("endpoint") or result["endpoint"])
        result["message"] = parsed.get("message") or parsed.get("error") or parsed.get("message")
        for key in (
            "patched_copy_required",
            "patched_path",
            "patched_installed",
            "patched_copy_current",
            "patched_copy_reason",
            "source",
            "patched_source",
            "login_reuse_verified",
            "login_reuse",
        ):
            if key in parsed:
                result[key] = parsed[key]
        if "patched_copy_current" in parsed:
            result["patched_copy"] = {
                key: parsed.get(key)
                for key in (
                    "patched_copy_required",
                    "patched_path",
                    "patched_installed",
                    "patched_copy_current",
                    "patched_copy_reason",
                    "source",
                    "patched_source",
                )
            }
    if result["ok"]:
        result["cdp"] = _cdp_status()
    else:
        result["error"] = result.get("message") or "Claude App CDP wrapper failed"
    return result


def _ensure_cdp_payload(args: Dict[str, Any]) -> Dict[str, Any]:
    current = _cdp_status()
    patched = _patched_copy_status()
    auth = _auth_identity_status()
    stale_current_endpoint = current.get("ok") and not _is_patched_copy_current(patched)
    try:
        port = int(args.get("port") or DEFAULT_CDP_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_CDP_PORT
    port = max(1, min(65535, port))
    endpoint = f"http://127.0.0.1:{port}"
    restart = _bool_value(args, "restart")
    force = _bool_value(args, "force")

    if stale_current_endpoint:
        if restart:
            return _run_wrapper_ensure_cdp(args, port, restart=True, force=force)
        payload = _stale_patched_copy_payload("ensure_cdp", current, patched)
        payload["endpoint"] = _endpoint_from_version_url(str(current.get("url") or endpoint))
        return payload

    if current.get("ok"):
        login_reuse = _login_reuse_status()
        if not login_reuse.get("ok"):
            return _login_reuse_failed_payload("ensure_cdp", current, login_reuse, auth)
        return {
            "ok": True,
            "action": "ensure_cdp",
            "endpoint": _endpoint_from_version_url(str(current.get("url") or "")),
            "cdp": current,
            "patched_copy": patched,
            "patched_copy_current": patched.get("patched_copy_current"),
            "login_reuse_verified": True,
            "login_reuse": login_reuse,
            "auth_identity": auth,
            "cdp_auth_identity_trusted": bool(auth.get("ok")),
            "restarted": False,
            "launched": False,
            "message": "Claude App CDP already available.",
        }

    running = _is_claude_app_running()

    if not DEFAULT_APP_PATH.exists():
        return {
            "ok": False,
            "action": "ensure_cdp",
            "endpoint": endpoint,
            "running": running,
            "restarted": False,
            "launched": False,
            "error": f"Claude App not found at {DEFAULT_APP_PATH}",
        }

    if running:
        if not restart:
            return {
                "ok": False,
                "action": "ensure_cdp",
                "endpoint": endpoint,
                "running": True,
                "restarted": False,
                "launched": False,
                "error": (
                    "Claude App is running without CDP. Re-run with restart=true "
                    f"to quit and relaunch a CDP-enabled Claude-CDP.app copy on port {port}."
                ),
                "requires_restart": True,
            }

    return _run_wrapper_ensure_cdp(args, port, restart, force)


def check_claude_app_requirements() -> bool:
    """Expose the tool when repo-local OpenCLI is available."""
    return bool(_resolve_opencli_command())


def _normalize_action(action: Any) -> str:
    normalized = str(action or "").strip().lower().replace("-", "_")
    return {
        "ensure-cdp": "ensure_cdp",
        "conversation": "detail",
        "conversations": "history",
        "open": "detail",
        "open_project": "project",
        "list_projects": "projects",
        "state": "status",
    }.get(normalized, normalized)


def _timeout_value(args: Dict[str, Any]) -> float:
    try:
        timeout = float(args.get("timeout") or DEFAULT_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS
    return max(1.0, min(180.0, timeout))


def _opencli_process_timeout(action: str, args: Dict[str, Any]) -> float:
    response_timeout = _timeout_value(args)
    if action == "ask":
        return response_timeout + ASK_PROCESS_TIMEOUT_BUFFER_SECONDS
    return response_timeout


def _bool_value(args: Dict[str, Any], name: str, default: bool = False) -> bool:
    value = args.get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _mode_value(args: Dict[str, Any]) -> str:
    mode = str(args.get("mode") or DEFAULT_CLAUDE_APP_MODE).strip().lower()
    aliases = {
        "coding": "code",
        "co-work": "cowork",
        "co_work": "cowork",
        "co_working": "cowork",
    }
    mode = aliases.get(mode, mode)
    if mode not in {"chat", "cowork", "code"}:
        raise ValueError("mode must be one of: chat, cowork, code")
    return mode


def _folder_value(args: Dict[str, Any]) -> str:
    return str(
        args.get("folder")
        or args.get("cwd")
        or args.get("workspace")
        or ""
    ).strip()


def _append_code_new_options(command: List[str], args: Dict[str, Any]) -> None:
    folder = _folder_value(args)
    if folder:
        command.extend(["--folder", folder])
    branch = str(args.get("branch") or "").strip()
    if branch:
        command.extend(["--branch", branch])
    model = str(args.get("model") or "").strip()
    if model:
        command.extend(["--model", model])
    effort = str(args.get("effort") or "").strip()
    if effort:
        command.extend(["--effort", effort])


def _build_opencli_command(action: str, args: Dict[str, Any]) -> List[str]:
    base = _resolve_opencli_command()
    if not base:
        raise RuntimeError("OpenCLI was not found. Install opencli or set HERMES_CLAUDE_APP_OPENCLI.")

    subcommand = {
        "status": "status",
        "projects": "projects",
        "project": "project",
        "history": "history",
        "detail": "detail",
        "new": "new",
        "read": "read",
        "send": "send",
        "ask": "ask",
        "dump": "dump",
        "screenshot": "screenshot",
    }.get(action)
    if not subcommand:
        raise ValueError(f"Unsupported action: {action}")

    command = [*base, "claude-app", subcommand]
    if action == "projects":
        command.extend(["--limit", str(int(args.get("limit") or 20)), "--format", "json"])
    elif action == "project":
        project = str(args.get("project") or "").strip()
        if not project:
            raise ValueError("project is required when action='project'")
        command.extend([project, "--mode", _mode_value(args), "--format", "json"])
    elif action == "history":
        command.extend([
            "--limit",
            str(int(args.get("limit") or 20)),
            "--mode",
            _mode_value(args),
            "--format",
            "json",
        ])
    elif action == "detail":
        conversation_id = str(args.get("conversation_id") or args.get("id") or "").strip()
        if not conversation_id:
            raise ValueError("conversation_id is required when action='detail'")
        command.extend([conversation_id, "--format", "json"])
    elif action == "send":
        message = str(args.get("message") or "").strip()
        if not message:
            raise ValueError("message is required when action='send'")
        command.append(message)
        conversation_id = str(args.get("conversation_id") or args.get("id") or "").strip()
        if conversation_id:
            command.extend(["--conversation", conversation_id])
        project = str(args.get("project") or "").strip()
        if project:
            command.extend(["--project", project])
        if _bool_value(args, "new"):
            command.extend(["--new", "true"])
        _append_code_new_options(command, args)
        command.extend(["--mode", _mode_value(args)])
        command.extend(["--format", "json"])
    elif action == "ask":
        message = str(args.get("message") or "").strip()
        if not message:
            raise ValueError("message is required when action='ask'")
        command.append(message)
        if _bool_value(args, "new"):
            command.extend(["--new", "true"])
        conversation_id = str(args.get("conversation_id") or args.get("id") or "").strip()
        if conversation_id:
            command.extend(["--conversation", conversation_id])
        project = str(args.get("project") or "").strip()
        if project:
            command.extend(["--project", project])
        _append_code_new_options(command, args)
        command.extend(["--mode", _mode_value(args)])
        if _bool_value(args, "think"):
            command.extend(["--think", "true"])
        file_path = str(args.get("file") or "").strip()
        if file_path:
            command.extend(["--file", file_path])
        command.extend(["--timeout", str(int(_timeout_value(args))), "--format", "json"])
    elif action == "new":
        project = str(args.get("project") or "").strip()
        if project:
            command.extend(["--project", project])
        _append_code_new_options(command, args)
        command.extend(["--mode", _mode_value(args)])
        command.extend(["--format", "json"])
    elif action == "screenshot":
        if not _bool_value(args, "diagnostic"):
            raise ValueError("action='screenshot' is diagnostic-only; set diagnostic=true")
        output = str(args.get("output") or "").strip()
        if output:
            command.extend(["--output", output])
        command.extend(["--format", "json"])
    elif action == "dump":
        if not _bool_value(args, "diagnostic"):
            raise ValueError("action='dump' is diagnostic-only; set diagnostic=true")
        command.extend(["--format", "json"])
    else:
        command.extend(["--format", "json"])
    return command


def _opencli_env() -> Dict[str, str]:
    env = dict(os.environ)
    env.setdefault("OPENCLI_CDP_TARGET", "claude-app")
    dynamic_endpoint = _detect_claude_app_cdp_endpoint()
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


def _display_command(command: List[str]) -> str:
    return shlex.join(command)


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


def _health_payload() -> Dict[str, Any]:
    command = _resolve_opencli_command()
    cdp = _cdp_status()
    patched = _patched_copy_status()
    auth = _auth_identity_status()
    login_reuse = _login_reuse_status() if cdp.get("ok") else {
        "ok": False,
        "source": "opencli-status",
        "error": "Claude App CDP is unavailable.",
        "cdp": cdp,
    }
    ok = (
        bool(command)
        and bool(cdp.get("ok"))
        and _is_patched_copy_current(patched)
        and bool(login_reuse.get("ok"))
    )
    return {
        "ok": ok,
        "opencli": {
            "ok": bool(command),
            "command": _display_command(command) if command else None,
        },
        "cdp": cdp,
        "patched_copy": patched,
        "patched_copy_current": patched.get("patched_copy_current"),
        "auth_identity": auth,
        "cdp_auth_identity_trusted": bool(auth.get("ok")),
        "login_reuse_verified": bool(login_reuse.get("ok")),
        "login_reuse": login_reuse,
        "target_app": "/Applications/Claude.app",
        "bundle_id": "com.anthropic.claudefordesktop",
        "opencli_route": "claude-app",
        "fallback_allowed": not ok,
        "fallback": "computer_use" if not ok else None,
        "guidance": (
            "Claude-CDP.app has not proven logged-in profile reuse; require opencli claude-app status Login=Yes and do not ask for relogin."
            if cdp.get("ok") and not login_reuse.get("ok") else
            "Claude-CDP.app is stale relative to /Applications/Claude.app. Run "
            "claude_app_opencli action='ensure_cdp' with restart=true and force=true."
            if cdp.get("ok") and not _is_patched_copy_current(patched) else
            "Claude App CDP is unavailable. Use computer_use app=Claude for desktop control, "
            "or restart Claude App manually with --remote-debugging-port=9242 and retry claude_app_opencli."
            if not ok else
            "Use claude_app_opencli for Claude desktop App operations. Use opencli claude only for claude.ai/Chrome."
        ),
    }


def handle_claude_app_opencli(args: Dict[str, Any], **_kwargs: Any) -> str:
    action = _normalize_action(args.get("action"))
    if action == "health":
        return tool_result(_health_payload())
    if action == "ensure_cdp":
        return tool_result(_ensure_cdp_payload(args))

    cdp = _cdp_status()
    if not cdp.get("ok"):
        return tool_error(
            "Claude App CDP is unavailable; desktop fallback may use computer_use app=Claude.",
            ok=False,
            action=action,
            cdp=cdp,
            fallback_allowed=True,
            fallback="computer_use",
            guidance=(
                "Do not use opencli claude for Claude App. Either expose Claude App CDP "
                "for claude_app_opencli, or use computer_use locked to app=Claude."
            ),
        )

    patched = _patched_copy_status()
    if not _is_patched_copy_current(patched):
        if _bool_value(args, "restart"):
            ensure_result = _ensure_cdp_payload({**args, "action": "ensure_cdp"})
            if not ensure_result.get("ok"):
                return tool_error(
                    str(ensure_result.get("error") or "Claude App CDP refresh failed"),
                    **ensure_result,
                )
            cdp = _cdp_status()
            patched = _patched_copy_status()
        if not _is_patched_copy_current(patched):
            stale = _stale_patched_copy_payload(action, cdp, patched)
            return tool_error(str(stale["error"]), **stale)

    auth = _auth_identity_status()
    if action in {"ask", "send", "new", "project"}:
        login_reuse = _login_reuse_status()
        if not login_reuse.get("ok"):
            blocked = _login_reuse_failed_payload(action, cdp, login_reuse, auth)
            return tool_error(str(blocked["error"]), **blocked)

    try:
        command = _build_opencli_command(action, args)
    except (RuntimeError, ValueError) as exc:
        return tool_error(
            str(exc),
            ok=False,
            action=action,
            fallback_allowed=False,
            fallback=None,
        )

    try:
        process_timeout = _opencli_process_timeout(action, args)
        completed = _run_opencli(command, process_timeout)
    except subprocess.TimeoutExpired as exc:
        response_timeout = _timeout_value(args)
        process_timeout = _opencli_process_timeout(action, args)
        return tool_error(
            f"OpenCLI process timed out after {process_timeout:.1f}s",
            ok=False,
            action=action,
            command=_display_command(command),
            response_timeout_seconds=response_timeout,
            process_timeout_seconds=process_timeout,
            stdout=_clip_text((exc.stdout or "") if isinstance(exc.stdout, str) else ""),
            stderr=_clip_text((exc.stderr or "") if isinstance(exc.stderr, str) else ""),
            fallback_allowed=False,
        )

    parsed = _parse_json_payload(completed.stdout)
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
    if completed.returncode != 0:
        result["error"] = "OpenCLI command failed"
        result["fallback_allowed"] = False
        result["fallback"] = None
        logger.debug("claude_app_opencli failed: %s", result)
    return tool_result(result)


# ---------------------------------------------------------------------------
# Structured failure followup guard
# ---------------------------------------------------------------------------

FOLLOWUP_GUARD_TTL_SECONDS = 90

BLOCKED_STRUCTURED_FALLBACK_TOOLS = {
    "computer_use",
    "execute_code",
    "read_file",
    "search_files",
    "terminal",
}

_STRUCTURED_FAILURE_FOLLOWUP_GUARDS: dict = {}


def _guard_key(task_id: str = "", session_id: str = "") -> str:
    if session_id:
        return f"session:{session_id}"
    if task_id:
        return f"task:{task_id}"
    return "global"


def update_claude_app_opencli_followup_guard(
    result, *, task_id: str = "", session_id: str = ""
) -> None:
    """Set or clear the Claude App followup guard based on the tool result."""
    try:
        import json as _json
        payload = _json.loads(result) if isinstance(result, str) else result
    except Exception:
        return
    if not isinstance(payload, dict):
        return

    key = _guard_key(task_id, session_id)
    if payload.get("ok") is not False:
        _STRUCTURED_FAILURE_FOLLOWUP_GUARDS.pop(key, None)
        return

    should_block = payload.get("fallback_allowed") is False
    if not should_block:
        _STRUCTURED_FAILURE_FOLLOWUP_GUARDS.pop(key, None)
        return

    import time as _time
    _STRUCTURED_FAILURE_FOLLOWUP_GUARDS[key] = {
        "created_at": _time.monotonic(),
        "action": payload.get("action"),
        "reason": payload.get("reason") or payload.get("error") or "Claude App OpenCLI/CDP error",
        "next_actions": payload.get("next_actions") or [],
    }


def claude_app_opencli_followup_block_message(
    function_name: str,
    *,
    task_id: str = "",
    session_id: str = "",
) -> str | None:
    """Return a block message if ``function_name`` should be blocked by the
    Claude App followup guard."""
    if function_name not in BLOCKED_STRUCTURED_FALLBACK_TOOLS:
        return None

    key = _guard_key(task_id, session_id)
    guard = _STRUCTURED_FAILURE_FOLLOWUP_GUARDS.get(key)
    if guard is None:
        return None

    import time as _time
    if _time.monotonic() - guard["created_at"] > FOLLOWUP_GUARD_TTL_SECONDS:
        _STRUCTURED_FAILURE_FOLLOWUP_GUARDS.pop(key, None)
        return None

    reason = guard.get("reason") or "a structured Claude App OpenCLI/CDP error"
    return (
        "The previous claude_app_opencli call failed with a structured "
        f"error ({reason}). Do NOT fall back to {function_name}. "
        "Use claude_app_opencli with one of the suggested next actions "
        "or retry with different parameters. "
        "If CDP is broken, call claude_app_opencli action=ensure_cdp first."
    )


def clear_claude_app_opencli_followup_guard(
    task_id: str = "", session_id: str = ""
) -> None:
    key = _guard_key(task_id, session_id)
    _STRUCTURED_FAILURE_FOLLOWUP_GUARDS.pop(key, None)


registry.register(
    name="claude_app_opencli",
    toolset="claude-app",
    schema=CLAUDE_APP_OPENCLI_SCHEMA,
    handler=lambda args, **kw: handle_claude_app_opencli(args, **kw),
    check_fn=check_claude_app_requirements,
    requires_env=[],
    description=CLAUDE_APP_OPENCLI_SCHEMA["description"],
    emoji="🖥️",
    max_result_size_chars=60_000,
)


__all__ = [
    "CLAUDE_APP_OPENCLI_SCHEMA",
    "check_claude_app_requirements",
    "claude_app_opencli_followup_block_message",
    "clear_claude_app_opencli_followup_guard",
    "handle_claude_app_opencli",
    "update_claude_app_opencli_followup_guard",
]
