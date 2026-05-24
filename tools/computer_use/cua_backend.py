"""Cua-driver backend (macOS only).

Speaks MCP over stdio to `cua-driver`. The Python `mcp` SDK is async, so we
run a dedicated asyncio event loop on a background thread and marshal sync
calls through it.

Install: `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh)"`

After install, `cua-driver` is on $PATH and supports `cua-driver mcp` (stdio
transport) which is what we invoke.

The private SkyLight SPIs cua-driver uses (SLEventPostToPid, SLPSPostEvent-
RecordTo, _AXObserverAddNotificationAndCheckRemote) are not Apple-public and
can break on OS updates. Pin the installed version via `HERMES_CUA_DRIVER_
VERSION` if you want reproducibility across an OS bump.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from typing import Any, Dict, List, Optional, Tuple

from tools.computer_use.backend import (
    ActionResult,
    CaptureResult,
    ComputerUseBackend,
    UIElement,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Version pinning
# ---------------------------------------------------------------------------

PINNED_CUA_DRIVER_VERSION = os.environ.get("HERMES_CUA_DRIVER_VERSION", "0.5.0")

_CUA_DRIVER_CMD = os.environ.get("HERMES_CUA_DRIVER_CMD", "cua-driver")
_CUA_DRIVER_ARGS = ["mcp"]  # stdio MCP transport

# Regex to parse list_windows text output lines:
#   "- AppName (pid 12345) "Title" [window_id: 67890]"
_WINDOW_LINE_RE = re.compile(
    r'^-\s+(.+?)\s+\(pid\s+(\d+)\)\s+.*\[window_id:\s+(\d+)\]',
    re.MULTILINE,
)

# Regex to parse element lines from get_window_state AX tree markdown.
#
# Handles two output formats from different cua-driver versions:
#   Classic:  "  - [N] AXRole \"label\""
#   New:       "[N] AXRole (order) id=Label"
#
# Group 1: element index
# Group 2: AX role
# Group 3: quoted label (classic format)
# Group 4: id= label (new format)
_ELEMENT_LINE_RE = re.compile(
    r'^\s*(?:-\s+)?\[(\d+)\]\s+(\w+)(?:\s+"([^"]*)"|(?:\s+\(\d+\))?\s+id=([^\s\[\]]*))?' ,
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_macos() -> bool:
    return sys.platform == "darwin"


def _is_arm_mac() -> bool:
    return _is_macos() and platform.machine() == "arm64"


def cua_driver_binary_available() -> bool:
    """True if `cua-driver` is on $PATH or HERMES_CUA_DRIVER_CMD resolves."""
    return bool(shutil.which(_CUA_DRIVER_CMD))


def cua_driver_install_hint() -> str:
    return (
        "cua-driver is not installed. Install with one of:\n"
        "  hermes computer-use install\n"
        "Or run the upstream installer directly:\n"
        '  /bin/bash -c "$(curl -fsSL '
        'https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh)"\n'
        "Or run `hermes tools` and enable the Computer Use toolset to install it automatically."
    )


def _parse_windows_from_text(text: str) -> List[Dict[str, Any]]:
    """Parse window records from list_windows text output."""
    windows = []
    for m in _WINDOW_LINE_RE.finditer(text):
        windows.append({
            "app_name": m.group(1).strip(),
            "pid": int(m.group(2)),
            "window_id": int(m.group(3)),
            "off_screen": "[off-screen]" in m.group(0),
        })
    return windows


def _clean_app_name(name: str) -> str:
    return re.sub(r"^-\s+", "", str(name or "").strip())


def _matches_app(window: Dict[str, Any], app: str) -> bool:
    wanted = _clean_app_name(app).lower()
    if not wanted:
        return False
    app_name = _clean_app_name(window.get("app_name", "")).lower()
    bundle_id = str(window.get("bundle_id", "") or "").lower()
    return wanted in {app_name, bundle_id} or wanted in app_name or wanted in bundle_id


def _parse_elements_from_tree(markdown: str) -> List[UIElement]:
    """Parse UIElement list from get_window_state AX tree markdown.

    Handles both the classic ``"label"``-quoted format and the newer
    ``id=Label`` format introduced in cua-driver v0.1.6.
    """
    elements = []
    for m in _ELEMENT_LINE_RE.finditer(markdown):
        # group(3) = quoted label (classic); group(4) = id= label (new)
        label = m.group(3) or m.group(4) or ""
        elements.append(UIElement(
            index=int(m.group(1)),
            role=m.group(2),
            label=label,
            bounds=(0, 0, 0, 0),
        ))
    return elements


def _split_tree_text(full_text: str) -> Tuple[str, str]:
    """Split get_window_state text into (summary_line, tree_markdown)."""
    lines = full_text.split("\n", 1)
    summary = lines[0]
    tree = lines[1] if len(lines) > 1 else ""
    return summary, tree


def _parse_key_combo(keys: str) -> Tuple[Optional[str], List[str]]:
    """Parse a key string like 'cmd+s' into (key, modifiers).

    Returns (key, modifiers) where key is the non-modifier key and modifiers
    is a list of modifier names (cmd, shift, option, ctrl).
    """
    MODIFIER_NAMES = {"cmd", "command", "shift", "option", "alt", "ctrl", "control", "fn"}
    KEY_ALIASES = {"command": "cmd", "alt": "option", "control": "ctrl"}

    parts = [p.strip().lower() for p in re.split(r'[+\-]', keys) if p.strip()]
    modifiers = []
    key = None
    for part in parts:
        normalized = KEY_ALIASES.get(part, part)
        if normalized in MODIFIER_NAMES:
            modifiers.append(normalized)
        else:
            key = part  # last non-modifier wins
    return key, modifiers


# ---------------------------------------------------------------------------
# Asyncio bridge — one long-lived loop on a background thread
# ---------------------------------------------------------------------------

class _AsyncBridge:
    """Runs one asyncio loop on a daemon thread; marshals coroutines from the caller."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()

        def _run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._ready.set()
            try:
                self._loop.run_forever()
            finally:
                try:
                    self._loop.close()
                except Exception:
                    pass

        self._thread = threading.Thread(target=_run, daemon=True, name="cua-driver-loop")
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("cua-driver asyncio bridge failed to start")

    def run(self, coro, timeout: Optional[float] = 30.0) -> Any:
        from agent.async_utils import safe_schedule_threadsafe
        if not self._loop or not self._thread or not self._thread.is_alive():
            if asyncio.iscoroutine(coro):
                coro.close()
            raise RuntimeError("cua-driver bridge not started")
        fut = safe_schedule_threadsafe(coro, self._loop)
        if fut is None:
            raise RuntimeError("cua-driver bridge not started")
        return fut.result(timeout=timeout)

    def stop(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None
        self._loop = None


# ---------------------------------------------------------------------------
# MCP session (lazy, shared across tool calls)
# ---------------------------------------------------------------------------

class _CuaDriverSession:
    """Holds the mcp ClientSession. Spawned lazily; re-entered on drop."""

    def __init__(self, bridge: _AsyncBridge) -> None:
        self._bridge = bridge
        self._session = None
        self._exit_stack = None
        self._lock = threading.Lock()
        self._started = False

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("cua-driver session not started")

    async def _aenter(self) -> None:
        from contextlib import AsyncExitStack
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        if not cua_driver_binary_available():
            raise RuntimeError(cua_driver_install_hint())

        params = StdioServerParameters(
            command=_CUA_DRIVER_CMD,
            args=_CUA_DRIVER_ARGS,
            env={**os.environ},
        )
        stack = AsyncExitStack()
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._exit_stack = stack
        self._session = session

    async def _aexit(self) -> None:
        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except Exception as e:
                logger.warning("cua-driver shutdown error: %s", e)
        self._exit_stack = None
        self._session = None

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._bridge.start()
            self._bridge.run(self._aenter(), timeout=15.0)
            self._started = True

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            try:
                self._bridge.run(self._aexit(), timeout=5.0)
            finally:
                self._started = False

    async def _call_tool_async(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        result = await self._session.call_tool(name, args)
        return _extract_tool_result(result)

    def call_tool(self, name: str, args: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
        self._require_started()
        return self._bridge.run(self._call_tool_async(name, args), timeout=timeout)

    async def _list_tools_async(self) -> List[str]:
        result = await self._session.list_tools()
        tools = getattr(result, "tools", []) or []
        names: List[str] = []
        for tool in tools:
            name = getattr(tool, "name", None)
            if name:
                names.append(str(name))
        return names

    def list_tools(self, timeout: float = 10.0) -> List[str]:
        self._require_started()
        return self._bridge.run(self._list_tools_async(), timeout=timeout)


def _extract_tool_result(mcp_result: Any) -> Dict[str, Any]:
    """Convert an mcp CallToolResult into a plain dict.

    cua-driver returns a mix of text parts, image parts, and structuredContent.
    We flatten into:
      {
        "data": <text or parsed json>,
        "images": [b64, ...],
        "structuredContent": <dict|None>,
        "isError": bool,
      }
    structuredContent is populated from the MCP result's structuredContent field
    (MCP spec §2024-11-05+) and takes precedence for structured data like
    list_windows window arrays.
    """
    data: Any = None
    images: List[str] = []
    is_error = bool(getattr(mcp_result, "isError", False))
    structured: Optional[Dict] = getattr(mcp_result, "structuredContent", None) or None
    text_chunks: List[str] = []
    for part in getattr(mcp_result, "content", []) or []:
        ptype = getattr(part, "type", None)
        if ptype == "text":
            text_chunks.append(getattr(part, "text", "") or "")
        elif ptype == "image":
            b64 = getattr(part, "data", None)
            if b64:
                images.append(b64)
    if text_chunks:
        joined = "\n".join(t for t in text_chunks if t)
        try:
            data = json.loads(joined) if joined.strip().startswith(("{", "[")) else joined
        except json.JSONDecodeError:
            data = joined
    return {"data": data, "images": images, "structuredContent": structured, "isError": is_error}


# ---------------------------------------------------------------------------
# The backend itself
# ---------------------------------------------------------------------------

class CuaDriverBackend(ComputerUseBackend):
    """Default computer-use backend. macOS-only via cua-driver MCP."""

    def __init__(self) -> None:
        self._bridge = _AsyncBridge()
        self._session = _CuaDriverSession(self._bridge)
        # Sticky context — updated by capture(), used by action tools.
        self._active_pid: Optional[int] = None
        self._active_window_id: Optional[int] = None
        self._last_app: Optional[str] = None  # last app name targeted via capture/focus_app
        self._active_app: str = ""
        self._active_window_title: str = ""
        self._tool_names: Optional[set[str]] = None

    # ── Lifecycle ──────────────────────────────────────────────────
    def start(self) -> None:
        self._session.start()
        self._refresh_tool_names()

    def stop(self) -> None:
        try:
            self._session.stop()
        finally:
            self._bridge.stop()

    def is_available(self) -> bool:
        if not _is_macos():
            return False
        return cua_driver_binary_available()

    def _refresh_tool_names(self) -> None:
        try:
            self._tool_names = set(self._session.list_tools())
        except Exception as e:
            logger.warning("cua-driver tool discovery failed: %s", e)
            self._tool_names = None

    def _has_tool(self, name: str) -> bool:
        if self._tool_names is None:
            self._refresh_tool_names()
        return self._tool_names is None or name in self._tool_names

    def health(self) -> Dict[str, Any]:
        version = ""
        status = ""
        doctor = ""
        if cua_driver_binary_available():
            version = _run_cua_cli(["--version"], timeout=3.0)
            status = _run_cua_cli(["status"], timeout=3.0)
            doctor = _run_cua_cli(["doctor"], timeout=8.0)
        if self._tool_names is None:
            self._refresh_tool_names()
        tools = sorted(self._tool_names or [])
        mcp_permissions = self._probe_mcp_permissions()
        daemon_smoke = self._probe_daemon_smoke()
        doctor_probes = _parse_doctor_probes(doctor)
        diagnosis = _diagnose_health(
            mcp_permissions=mcp_permissions,
            daemon_smoke=daemon_smoke,
            doctor_probes=doctor_probes,
            tools=tools,
        )
        return {
            "available": self.is_available(),
            "backend": "cua-driver",
            "usable": diagnosis["usable"],
            "permission_status": diagnosis["permission_status"],
            "summary": diagnosis["summary"],
            "warnings": diagnosis["warnings"],
            "command": _CUA_DRIVER_CMD,
            "version": version.strip(),
            "status": status.strip(),
            "mcp_permissions": mcp_permissions,
            "daemon_smoke": daemon_smoke,
            "doctor_probes": doctor_probes,
            "doctor_note": (
                "Raw `cua-driver doctor` checks the current CLI process. In the "
                "Hermes gateway it can report TCC denials for Python.app even "
                "when the CuaDriver.app daemon is authorized. Prefer "
                "`usable`, `permission_status`, `mcp_permissions`, and "
                "`daemon_smoke` for control decisions."
            ),
            "doctor": doctor.strip(),
            "tools": tools,
            "active": {
                "app": self._active_app,
                "pid": self._active_pid,
                "window_id": self._active_window_id,
                "window_title": self._active_window_title,
            },
        }

    def _probe_mcp_permissions(self) -> Dict[str, Any]:
        if not self._has_tool("check_permissions"):
            return {
                "available": False,
                "ok": None,
                "accessibility": None,
                "screen_recording": None,
                "raw": "check_permissions tool is not available",
            }
        try:
            out = self._session.call_tool("check_permissions", {})
        except Exception as e:
            return {
                "available": True,
                "ok": False,
                "accessibility": None,
                "screen_recording": None,
                "error": str(e),
            }

        raw = out["data"] if isinstance(out.get("data"), str) else str(out.get("data", ""))
        accessibility = _parse_permission_line(raw, "Accessibility")
        screen_recording = _parse_permission_line(raw, "Screen Recording")
        known = [v for v in (accessibility, screen_recording) if v is not None]
        ok = bool(known) and all(known) and not bool(out.get("isError"))
        return {
            "available": True,
            "ok": ok,
            "accessibility": accessibility,
            "screen_recording": screen_recording,
            "raw": raw.strip(),
        }

    def _probe_daemon_smoke(self) -> Dict[str, Any]:
        if not self._has_tool("list_windows"):
            return {
                "ok": False,
                "list_windows_ok": False,
                "window_count": 0,
                "error": "list_windows tool is not available",
            }
        try:
            out = self._session.call_tool("list_windows", {"on_screen_only": True})
        except Exception as e:
            return {
                "ok": False,
                "list_windows_ok": False,
                "window_count": 0,
                "error": str(e),
            }

        count = 0
        sc = out.get("structuredContent") or {}
        raw_windows = sc.get("windows") if sc else None
        if raw_windows:
            count = len(raw_windows)
        elif isinstance(out.get("data"), str):
            m = re.search(r"Found\s+(\d+)\s+window", out["data"])
            if m:
                count = int(m.group(1))
            else:
                count = len(_parse_windows_from_text(out["data"]))
        ok = not bool(out.get("isError")) and count > 0
        return {
            "ok": ok,
            "list_windows_ok": ok,
            "window_count": count,
        }

    # ── Capture ────────────────────────────────────────────────────
    def capture(self, mode: str = "som", app: Optional[str] = None) -> CaptureResult:
        """Capture the frontmost on-screen window (optionally filtered by app name).

        Maps hermes `capture(mode, app)` → cua-driver `list_windows` +
        `get_window_state` (ax/som) or `screenshot` (vision).
        """
        # Step 1: enumerate on-screen windows to find target pid/window_id.
        lw_out = self._session.call_tool("list_windows", {"on_screen_only": True})

        # Prefer structuredContent.windows (MCP 2024-11-05+); fall back to
        # text-line parsing for older cua-driver builds.
        sc = lw_out.get("structuredContent") or {}
        raw_windows = sc.get("windows") if sc else None
        if raw_windows:
            windows = [
                {
                    "app_name": w.get("app_name", ""),
                    "pid": int(w["pid"]),
                    "window_id": int(w["window_id"]),
                    "off_screen": not w.get("is_on_screen", True),
                    "title": w.get("title", ""),
                    "z_index": w.get("z_index", 0),
                }
                for w in raw_windows
            ]
            # Sort by z_index descending (lowest z_index = frontmost on macOS).
            windows.sort(key=lambda w: w["z_index"])
        else:
            raw_text = lw_out["data"] if isinstance(lw_out["data"], str) else ""
            windows = _parse_windows_from_text(raw_text)

        if not windows:
            return CaptureResult(mode=mode, width=0, height=0, png_b64=None,
                                 elements=[], app="", window_title="", png_bytes_len=0)

        # Filter by app name (case-insensitive substring) if requested.
        # When the filter matches nothing, surface that explicitly instead of
        # silently capturing the frontmost window — on macOS the `app_name`
        # returned by list_windows is the localized name (e.g. "計算機"), so
        # `app="Calculator"` legitimately matches no windows on a non-English
        # system and the caller needs to retry with the localized name.
        if app:
            filtered = [w for w in windows if _matches_app(w, app)]
            if not filtered:
                return CaptureResult(
                    mode=mode, width=0, height=0, png_b64=None,
                    elements=[], app="",
                    window_title=(
                        f"<no on-screen window matched app={app!r}; "
                        f"call list_apps to see available app names "
                        f"(macOS reports localized names, e.g. '計算機' "
                        f"instead of 'Calculator')>"
                    ),
                    png_bytes_len=0,
                )
            windows = filtered

        # Pick first on-screen window (sorted by z_index / z-order above).
        target = next((w for w in windows if not w["off_screen"]), windows[0])
        self._active_pid = target["pid"]
        self._active_window_id = target["window_id"]
        app_name = _clean_app_name(target["app_name"])
        # Record the resolved app name so capture_after= follow-ups can re-target
        # the same app rather than falling back to the frontmost window.
        if app or not self._last_app:
            self._last_app = app_name
        self._active_app = app_name
        self._active_window_title = str(target.get("title", "") or "")

        # Step 2: capture.
        png_b64: Optional[str] = None
        elements: List[UIElement] = []
        width = height = 0
        window_title = ""

        if mode == "vision":
            # screenshot tool: just the PNG, no AX walk.
            sc_out = self._session.call_tool(
                "screenshot",
                {"window_id": self._active_window_id, "format": "jpeg", "quality": 85},
            )
            if sc_out["images"]:
                png_b64 = sc_out["images"][0]
        else:
            # get_window_state: AX tree + optional screenshot.
            gws_out = self._session.call_tool(
                "get_window_state",
                {"pid": self._active_pid, "window_id": self._active_window_id},
            )
            text = gws_out["data"] if isinstance(gws_out["data"], str) else ""
            summary, tree = _split_tree_text(text)

            # Parse element count from summary e.g. "✅ AppName — 42 elements, turn 3..."
            m = re.search(r'(\d+)\s+elements?', summary)
            if tree and not gws_out["images"]:
                # ax mode — no screenshot
                elements = _parse_elements_from_tree(tree)
            elif gws_out["images"]:
                png_b64 = gws_out["images"][0]
                elements = _parse_elements_from_tree(tree)

            # Extract window title from the AX tree first AXWindow line.
            wt = re.search(r'AXWindow\s+"([^"]+)"', tree)
            if wt:
                window_title = wt.group(1)
        if window_title:
            self._active_window_title = window_title

        png_bytes_len = 0
        if png_b64:
            try:
                png_bytes_len = len(base64.b64decode(png_b64, validate=False))
            except Exception:
                png_bytes_len = len(png_b64) * 3 // 4

        return CaptureResult(
            mode=mode,
            width=width,
            height=height,
            png_b64=png_b64,
            elements=elements,
            app=app_name,
            window_title=window_title,
            png_bytes_len=png_bytes_len,
        )

    # ── Pointer ────────────────────────────────────────────────────
    def click(
        self,
        *,
        element: Optional[int] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
        click_count: int = 1,
        modifiers: Optional[List[str]] = None,
    ) -> ActionResult:
        pid = self._active_pid
        if pid is None:
            return ActionResult(ok=False, action="click",
                                message="No active window — call capture() first.")

        # Choose tool based on button and click_count.
        if button == "right":
            tool = "right_click"
        elif click_count == 2:
            tool = "double_click"
        else:
            tool = "click"

        args: Dict[str, Any] = {"pid": pid}
        if element is not None:
            if self._active_window_id is None:
                return ActionResult(ok=False, action=tool,
                                    message="No active window_id for element_index click.")
            args["element_index"] = element
            args["window_id"] = self._active_window_id
        elif x is not None and y is not None:
            args["x"] = x
            args["y"] = y
        else:
            return ActionResult(ok=False, action=tool,
                                message="click requires element= or x/y.")
        if modifiers:
            args["modifier"] = modifiers

        return self._action(tool, args)

    def drag(
        self,
        *,
        from_element: Optional[int] = None,
        to_element: Optional[int] = None,
        from_xy: Optional[Tuple[int, int]] = None,
        to_xy: Optional[Tuple[int, int]] = None,
        button: str = "left",
        modifiers: Optional[List[str]] = None,
    ) -> ActionResult:
        pid = self._active_pid
        if pid is None:
            return ActionResult(ok=False, action="drag",
                                message="No active window — call capture() first.")
        args: Dict[str, Any] = {"pid": pid}
        if from_element is not None and to_element is not None:
            if self._active_window_id is None:
                return ActionResult(ok=False, action="drag",
                                    message="No active window_id for element-based drag.")
            args["from_element"] = from_element
            args["to_element"] = to_element
            args["window_id"] = self._active_window_id
        elif from_xy is not None and to_xy is not None:
            args["from_x"], args["from_y"] = int(from_xy[0]), int(from_xy[1])
            args["to_x"], args["to_y"] = int(to_xy[0]), int(to_xy[1])
        else:
            return ActionResult(ok=False, action="drag",
                                message="drag requires from_element/to_element or from_coordinate/to_coordinate.")
        return self._action("drag", args)

    def scroll(
        self,
        *,
        direction: str,
        amount: int = 3,
        element: Optional[int] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        modifiers: Optional[List[str]] = None,
    ) -> ActionResult:
        pid = self._active_pid
        if pid is None:
            return ActionResult(ok=False, action="scroll",
                                message="No active window — call capture() first.")
        args: Dict[str, Any] = {
            "pid": pid,
            "direction": direction,
            "amount": max(1, min(50, amount)),
        }
        if element is not None and self._active_window_id is not None:
            args["element_index"] = element
            args["window_id"] = self._active_window_id
        elif x is not None and y is not None:
            args["x"] = x
            args["y"] = y
        return self._action("scroll", args)

    # ── Keyboard ───────────────────────────────────────────────────
    def type_text(self, text: str, element: Optional[int] = None) -> ActionResult:
        pid = self._active_pid
        if pid is None:
            return ActionResult(ok=False, action="type_text",
                                message="No active window — call capture() first.")
        if self._has_tool("type_text"):
            args: Dict[str, Any] = {"pid": pid, "text": text}
            if element is not None:
                if self._active_window_id is None:
                    return ActionResult(ok=False, action="type_text",
                                        message="No active window_id for element_index type.")
                args["element_index"] = element
                args["window_id"] = self._active_window_id
            return self._action("type_text", args)
        if self._has_tool("type_text_chars"):
            return self._action("type_text_chars", {"pid": pid, "text": text})
        return ActionResult(
            ok=False,
            action="type_text",
            message="cua-driver exposes neither type_text nor type_text_chars.",
        )

    def key(self, keys: str) -> ActionResult:
        pid = self._active_pid
        if pid is None:
            return ActionResult(ok=False, action="key",
                                message="No active window — call capture() first.")

        key_name, modifiers = _parse_key_combo(keys)
        if not key_name:
            return ActionResult(ok=False, action="key",
                                message=f"Could not parse key from '{keys}'.")

        if modifiers:
            # hotkey requires at least one modifier + one key.
            return self._action("hotkey", {"pid": pid, "keys": modifiers + [key_name]})
        else:
            return self._action("press_key", {"pid": pid, "key": key_name})

    # ── Value setter ────────────────────────────────────────────────
    def set_value(self, value: str, element: Optional[int] = None) -> ActionResult:
        """Set a value on an element. Handles AXPopUpButton selects natively."""
        pid = self._active_pid
        window_id = self._active_window_id
        if pid is None or window_id is None:
            return ActionResult(ok=False, action="set_value",
                                message="No active window — call capture() first.")
        if element is None:
            return ActionResult(ok=False, action="set_value",
                                message="set_value requires element= (element index).")
        args: Dict[str, Any] = {
            "pid": pid,
            "window_id": window_id,
            "element_index": element,
            "value": value,
        }
        return self._action("set_value", args)

    # ── Introspection ──────────────────────────────────────────────
    def list_apps(self) -> List[Dict[str, Any]]:
        out = self._session.call_tool("list_apps", {})
        data = out["data"]
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("apps", [])
        # list_apps returns plain text — parse app lines.
        if isinstance(data, str):
            apps = []
            for line in data.splitlines():
                m = re.search(r'(.+?)\s+\(pid\s+(\d+)\)', line)
                if m:
                    apps.append({"name": _clean_app_name(m.group(1)), "pid": int(m.group(2))})
            return apps
        return []

    def focus_app(self, app: str, raise_window: bool = False) -> ActionResult:
        """Target an app for subsequent actions without stealing system focus.

        cua-driver background-automation never needs to bring a window to the
        front: capture(app=...) already selects the right window via
        list_windows. We implement focus_app as a pure window-selector —
        enumerate on-screen windows, find the best match for *app*, and store
        its pid/window_id so that subsequent click/type calls hit the right
        process.

        raise_window=True explicitly asks macOS to activate the app before
        selecting a window. The default still keeps background routing.
        """
        if raise_window:
            raised = _raise_app(app)
            if not raised:
                return ActionResult(ok=False, action="focus_app",
                                    message=f"Could not raise app '{app}'.")
            time.sleep(0.5)

        lw_out = self._session.call_tool("list_windows", {"on_screen_only": True})
        sc = lw_out.get("structuredContent") or {}
        raw_windows = sc.get("windows") if sc else None
        if raw_windows:
            windows = [
                {
                    "app_name": w.get("app_name", ""),
                    "bundle_id": w.get("bundle_id", ""),
                    "title": w.get("title", ""),
                    "pid": int(w["pid"]),
                    "window_id": int(w["window_id"]),
                    "z_index": w.get("z_index", 0),
                }
                for w in raw_windows
            ]
            windows.sort(key=lambda w: w["z_index"])
        else:
            raw_text = lw_out["data"] if isinstance(lw_out["data"], str) else ""
            windows = _parse_windows_from_text(raw_text)

        matched = [w for w in windows if _matches_app(w, app)]
        # Don't silently fall back to the frontmost window when the filter
        # matches nothing — that hides the real failure (often a localized
        # macOS app name mismatch, e.g. caller passed "Calculator" but
        # list_windows returns "計算機").
        target = matched[0] if matched else None
        if target:
            self._active_pid = target["pid"]
            self._active_window_id = target["window_id"]
            self._active_app = _clean_app_name(target["app_name"])
            self._active_window_title = str(target.get("title", "") or "")
            self._last_app = self._active_app  # preserve for capture_after= follow-ups
            return ActionResult(
                ok=True, action="focus_app",
                message=f"Targeted {self._active_app} (pid {self._active_pid}, "
                        f"window {self._active_window_id})"
                        + (" after raising window." if raise_window else " without raising window."),
            )
        return ActionResult(ok=False, action="focus_app",
                            message=f"No on-screen window found for app '{app}'.")

    # ── Internal ───────────────────────────────────────────────────
    def _action(self, name: str, args: Dict[str, Any]) -> ActionResult:
        try:
            out = self._session.call_tool(name, args)
        except Exception as e:
            logger.exception("cua-driver %s call failed", name)
            return ActionResult(ok=False, action=name, message=f"cua-driver error: {e}")
        ok = not out["isError"]
        message = ""
        data = out["data"]
        if isinstance(data, dict):
            message = str(data.get("message", ""))
        elif isinstance(data, str):
            message = data
        return ActionResult(ok=ok, action=name, message=message,
                            meta=data if isinstance(data, dict) else {})


def _run_cua_cli(args: List[str], timeout: float) -> str:
    try:
        proc = subprocess.run(
            [_CUA_DRIVER_CMD, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as e:
        return f"error: {e}"
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    return "\n".join(part for part in (out, err) if part)


def _parse_permission_line(text: str, label: str) -> Optional[bool]:
    needle = label.lower()
    for line in str(text or "").splitlines():
        lower = line.lower()
        if needle not in lower:
            continue
        if "denied" in lower or "not granted" in lower or "❌" in line:
            return False
        if "granted" in lower or "authorized" in lower or "✅" in line:
            return True
    return None


def _parse_doctor_probes(doctor: str) -> Dict[str, Optional[bool]]:
    return {
        "accessibility": _parse_permission_line(doctor, "Accessibility"),
        "screen_recording": _parse_permission_line(doctor, "Screen Recording"),
        "correct_bundle_attribution": _parse_permission_line(doctor, "Correct bundle attribution"),
        "ax_tree_smoke_test": _parse_permission_line(doctor, "AX tree smoke test"),
    }


def _diagnose_health(
    *,
    mcp_permissions: Dict[str, Any],
    daemon_smoke: Dict[str, Any],
    doctor_probes: Dict[str, Optional[bool]],
    tools: List[str],
) -> Dict[str, Any]:
    warnings: List[str] = []
    permissions_ok = mcp_permissions.get("ok") is True
    daemon_ok = daemon_smoke.get("ok") is True
    type_tool_ok = "type_text" in tools or "type_text_chars" in tools
    click_tool_ok = "click" in tools

    raw_doctor_denied = any(
        value is False
        for key, value in doctor_probes.items()
        if key in {"accessibility", "screen_recording", "ax_tree_smoke_test"}
    )
    if permissions_ok and raw_doctor_denied:
        warnings.append("raw_cli_doctor_tcc_mismatch")
    if permissions_ok and doctor_probes.get("correct_bundle_attribution") is False:
        warnings.append("raw_cli_doctor_bundle_attribution_warning")
    if not type_tool_ok:
        warnings.append("typing_tool_missing")
    if not click_tool_ok:
        warnings.append("click_tool_missing")

    usable = permissions_ok and daemon_ok and type_tool_ok and click_tool_ok
    if usable:
        permission_status = "ok"
        summary = (
            "CuaDriver daemon is usable. MCP check_permissions reports "
            "Accessibility and Screen Recording granted, daemon list_windows "
            "works, and click/type tools are available. Do not ask the user to "
            "re-authorize based only on raw CLI doctor output."
        )
    elif mcp_permissions.get("accessibility") is False or mcp_permissions.get("screen_recording") is False:
        permission_status = "denied"
        summary = (
            "CuaDriver daemon reports missing Accessibility or Screen Recording "
            "permission through MCP check_permissions."
        )
    elif not daemon_ok:
        permission_status = "daemon_unhealthy"
        summary = "CuaDriver daemon permission probe did not pass list_windows smoke test."
    elif not type_tool_ok or not click_tool_ok:
        permission_status = "tool_missing"
        summary = "CuaDriver daemon is reachable but required click/type tools are missing."
    else:
        permission_status = "unknown"
        summary = "CuaDriver health is inconclusive; inspect mcp_permissions and daemon_smoke."

    return {
        "usable": usable,
        "permission_status": permission_status,
        "summary": summary,
        "warnings": warnings,
    }


def _raise_app(app: str) -> bool:
    commands: List[List[str]] = []
    app = str(app or "").strip()
    if not app:
        return False
    if "." in app and "/" not in app:
        commands.append(["open", "-b", app])
    commands.append(["open", "-a", app])
    for command in commands:
        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=3.0, check=False)
        except Exception:
            continue
        if proc.returncode == 0:
            return True
    return False


def _parse_element(d: Dict[str, Any]) -> UIElement:
    bounds = d.get("bounds") or (0, 0, 0, 0)
    if isinstance(bounds, dict):
        bounds = (
            int(bounds.get("x", 0)),
            int(bounds.get("y", 0)),
            int(bounds.get("w", bounds.get("width", 0))),
            int(bounds.get("h", bounds.get("height", 0))),
        )
    elif isinstance(bounds, (list, tuple)) and len(bounds) == 4:
        bounds = tuple(int(v) for v in bounds)
    else:
        bounds = (0, 0, 0, 0)
    return UIElement(
        index=int(d.get("index", 0)),
        role=str(d.get("role", "") or ""),
        label=str(d.get("label", "") or ""),
        bounds=bounds,  # type: ignore[arg-type]
        app=str(d.get("app", "") or ""),
        pid=int(d.get("pid", 0) or 0),
        window_id=int(d.get("windowId", 0) or 0),
        attributes={k: v for k, v in d.items()
                    if k not in {"index", "role", "label", "bounds", "app", "pid", "windowId"}},
    )
