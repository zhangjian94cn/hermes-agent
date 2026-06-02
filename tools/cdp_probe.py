"""Small CDP endpoint probing helpers shared by App Operator tools.

The goal is to keep identity checks in one place. Some Electron apps expose a
generic Chrome-looking ``/json/version`` payload while the real app identity is
only visible in ``/json/list`` targets.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Sequence


DEFAULT_VERSION_FIELDS = ("User-Agent", "Browser", "webSocketDebuggerUrl")
DEFAULT_TARGET_FIELDS = ("title", "url", "webSocketDebuggerUrl")


def _clip_text(value: Any, limit: int = 2_000) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 80)] + f"\n...[clipped {len(text) - limit + 80} chars]"


def _clip_payload(value: Any, limit: int = 6_000) -> Any:
    if isinstance(value, str):
        return _clip_text(value, limit)
    if isinstance(value, list):
        if not value:
            return []
        return [_clip_payload(item, max(1_000, limit // len(value))) for item in value]
    if isinstance(value, dict):
        return {str(key): _clip_payload(item, limit) for key, item in value.items()}
    return value


def normalize_cdp_endpoint(value: str) -> str:
    endpoint = str(value or "").strip().rstrip("/")
    for suffix in ("/json/version", "/json/list"):
        if endpoint.endswith(suffix):
            return endpoint[: -len(suffix)]
    return endpoint


def endpoint_to_version_url(endpoint_or_url: str) -> str:
    return f"{normalize_cdp_endpoint(endpoint_or_url)}/json/version"


def endpoint_to_targets_url(endpoint_or_url: str) -> str:
    return f"{normalize_cdp_endpoint(endpoint_or_url)}/json/list"


def identity_matches(
    payload: Any,
    keywords: Iterable[str],
    *,
    fields: Sequence[str],
) -> bool:
    if not isinstance(payload, dict):
        return False
    normalized_keywords = [str(item).lower() for item in keywords if str(item or "").strip()]
    if not normalized_keywords:
        return False
    identity = " ".join(str(payload.get(field) or "") for field in fields).lower()
    return any(keyword in identity for keyword in normalized_keywords)


def is_inspectable_cdp_target(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and bool(item.get("webSocketDebuggerUrl"))
        and item.get("type") in {None, "page", "webview"}
    )


def target_matches_identity(item: Any, keywords: Iterable[str]) -> bool:
    return is_inspectable_cdp_target(item) and identity_matches(
        item,
        keywords,
        fields=DEFAULT_TARGET_FIELDS,
    )


def _fetch_json_or_raw(url: str, *, timeout: float, max_bytes: int) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        raw = response.read(max_bytes).decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": _clip_text(raw)}


def _identity_source(version_identity: bool, target_identity: bool) -> str | None:
    if version_identity and target_identity:
        return "version+target"
    if version_identity:
        return "version"
    if target_identity:
        return "target"
    return None


def probe_cdp_endpoint(
    endpoint_or_url: str,
    *,
    app_name: str,
    identity_keywords: Iterable[str],
    timeout: float = 1.0,
    require_inspectable_target: bool = True,
) -> Dict[str, Any]:
    """Probe a CDP endpoint and confirm that it belongs to an expected app.

    A probe is successful only when the endpoint is reachable, app identity is
    visible in either the version payload or an inspectable target, and, by
    default, at least one inspectable page/webview target exists.
    """

    endpoint = normalize_cdp_endpoint(endpoint_or_url)
    version_url = endpoint_to_version_url(endpoint)
    targets_url = endpoint_to_targets_url(endpoint)
    keywords = tuple(str(item).lower() for item in identity_keywords if str(item or "").strip())

    try:
        version_payload = _fetch_json_or_raw(version_url, timeout=timeout, max_bytes=64_000)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        return {
            "ok": False,
            "endpoint": endpoint,
            "url": version_url,
            "error": str(exc),
            "version": {"ok": False, "url": version_url, "error": str(exc)},
        }

    version_identity = identity_matches(
        version_payload,
        keywords,
        fields=DEFAULT_VERSION_FIELDS,
    )
    version = {
        "ok": True,
        "url": version_url,
        "payload": version_payload,
        "identity": version_identity,
    }

    try:
        targets_payload = _fetch_json_or_raw(targets_url, timeout=timeout, max_bytes=256_000)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        return {
            "ok": False,
            "endpoint": endpoint,
            "url": version_url,
            "error": str(exc),
            "payload": version_payload,
            "version": version,
            "targets": {"ok": False, "url": targets_url, "error": str(exc)},
        }

    if not isinstance(targets_payload, list):
        return {
            "ok": False,
            "endpoint": endpoint,
            "url": version_url,
            "error": "CDP target list is not an array.",
            "payload": version_payload,
            "version": version,
            "targets": {"ok": False, "url": targets_url, "payload": targets_payload},
        }

    inspectable_targets = [item for item in targets_payload if is_inspectable_cdp_target(item)]
    matching_targets = [item for item in inspectable_targets if target_matches_identity(item, keywords)]
    target_identity = bool(matching_targets)
    identity = {
        "version": version_identity,
        "target": target_identity,
        "source": _identity_source(version_identity, target_identity),
    }
    targets = {
        "ok": bool(inspectable_targets) or not require_inspectable_target,
        "url": targets_url,
        "target_count": len(targets_payload),
        "inspectable_count": len(inspectable_targets),
        "matching_target_count": len(matching_targets),
        "targets": _clip_payload(inspectable_targets),
        "matching_targets": _clip_payload(matching_targets),
    }

    ok = bool(identity["source"]) and (targets["ok"] or not require_inspectable_target)
    result: Dict[str, Any] = {
        "ok": ok,
        "endpoint": endpoint,
        "url": version_url,
        "payload": version_payload,
        "target_count": len(targets_payload),
        "inspectable_count": len(inspectable_targets),
        "matching_target_count": len(matching_targets),
        "identity": identity,
        "version": version,
        "targets": targets,
    }
    if not targets["ok"]:
        result["error"] = f"No inspectable {app_name} page targets found at CDP endpoint."
    elif not identity["source"]:
        result["error"] = (
            f"CDP endpoint is reachable but does not identify as {app_name} "
            "in /json/version or /json/list."
        )
    return result


def probe_codex_cdp_endpoint(endpoint_or_url: str, *, timeout: float = 1.0) -> Dict[str, Any]:
    result = probe_cdp_endpoint(
        endpoint_or_url,
        app_name="Codex",
        identity_keywords=("codex",),
        timeout=timeout,
        require_inspectable_target=True,
    )
    result["codex_target_count"] = result.get("matching_target_count", 0)
    version = result.get("version")
    if isinstance(version, dict):
        version["codex_identity"] = bool(version.get("identity"))
    targets = result.get("targets")
    if isinstance(targets, dict):
        targets["codex_target_count"] = targets.get("matching_target_count", 0)
        targets["codex_targets"] = targets.get("matching_targets", [])
    return result


__all__ = [
    "endpoint_to_targets_url",
    "endpoint_to_version_url",
    "identity_matches",
    "is_inspectable_cdp_target",
    "normalize_cdp_endpoint",
    "probe_cdp_endpoint",
    "probe_codex_cdp_endpoint",
    "target_matches_identity",
]
