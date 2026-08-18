"""Databricks CLI profile discovery.

The Databricks CLI already stores what this plugin needs — host, auth type, and
a working credential helper — in ``~/.databrickscfg``.  Asking the user to
re-declare a host and a ``key_cmd`` in ``config.yaml`` duplicates that, and the
copy goes stale the moment they re-run ``databricks auth login`` against a
different workspace.

So the CLI is treated as the source of truth for *connection identity*, and
Hermes config only has to name a profile (or nothing at all, if there's a
default).  Discovery goes through the CLI itself (``databricks auth profiles``)
rather than parsing the ini file, because the CLI resolves env-var overrides,
account-vs-workspace hosts, and ``DATABRICKS_CONFIG_FILE`` — and it reports
whether a profile's credential actually ``valid``ates.

Parsing ``.databrickscfg`` directly is kept only as a fallback for when the CLI
is missing, so `hermes databricks profiles` can still explain the situation.
"""

from __future__ import annotations

import configparser
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# `auth profiles` validates every profile by calling each workspace, so it is
# slow (seconds), not milliseconds. Cache it for one process lifetime window.
_PROFILE_CACHE_SECONDS = 300.0
_PROFILE_LIST_TIMEOUT = 45
_DESCRIBE_TIMEOUT = 30

_cache_lock = threading.Lock()
_profiles_cache: Optional[List[Dict[str, Any]]] = None
_profiles_cache_at: float = 0.0


class DatabricksCliError(RuntimeError):
    """The Databricks CLI is unavailable or answered unusably."""


def cli_path() -> Optional[str]:
    """Absolute path to the ``databricks`` CLI, or None when absent."""
    return shutil.which("databricks")


def config_file_path() -> Path:
    """Where the CLI keeps its profiles, honouring DATABRICKS_CONFIG_FILE."""
    override = os.environ.get("DATABRICKS_CONFIG_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".databrickscfg"


def _run_cli(args: List[str], timeout: int) -> str:
    """Run the CLI and return stdout, or raise DatabricksCliError."""
    exe = cli_path()
    if not exe:
        raise DatabricksCliError(
            "the 'databricks' CLI is not on PATH. Install it "
            "(https://docs.databricks.com/dev-tools/cli/install.html) or "
            "configure the workspace host and auth explicitly."
        )
    try:
        completed = subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise DatabricksCliError(
            f"`databricks {' '.join(args)}` timed out after {timeout}s"
        ) from exc
    except OSError as exc:
        raise DatabricksCliError(f"could not run the databricks CLI: {exc}") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise DatabricksCliError(
            f"`databricks {' '.join(args)}` exited {completed.returncode}: "
            f"{detail[:300]}"
        )
    return completed.stdout


def _parse_cfg_file() -> List[Dict[str, Any]]:
    """Fallback profile list read straight from the ini file.

    Used only when the CLI cannot be invoked.  It cannot report ``valid``,
    so that key is deliberately absent rather than guessed at.
    """
    path = config_file_path()
    if not path.exists():
        return []
    parser = configparser.ConfigParser()
    try:
        parser.read(path)
    except configparser.Error:
        return []

    profiles: List[Dict[str, Any]] = []
    for section in parser.sections():
        # The CLI's own bookkeeping section is not a profile.
        if section == "__settings__":
            continue
        host = (parser.get(section, "host", fallback="") or "").strip()
        if not host:
            continue
        profiles.append({
            "name": section,
            "host": host.rstrip("/"),
            "auth_type": (parser.get(section, "auth_type", fallback="") or "").strip()
            or None,
            "source": "config-file",
        })
    return profiles


def list_profiles(*, refresh: bool = False) -> List[Dict[str, Any]]:
    """Every CLI profile, with host/auth_type/valid where known.

    Never raises: a missing CLI degrades to reading the ini file, and a missing
    ini file yields an empty list.  Callers decide how loudly to complain.
    """
    global _profiles_cache, _profiles_cache_at

    now = time.monotonic()
    if not refresh:
        with _cache_lock:
            if _profiles_cache is not None and (
                now - _profiles_cache_at < _PROFILE_CACHE_SECONDS
            ):
                return list(_profiles_cache)

    profiles: List[Dict[str, Any]] = []
    try:
        raw = _run_cli(["auth", "profiles", "-o", "json"], _PROFILE_LIST_TIMEOUT)
        payload = json.loads(raw or "{}")
        for entry in payload.get("profiles") or []:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            profiles.append({
                "name": entry.get("name"),
                "host": str(entry.get("host") or "").rstrip("/") or None,
                "auth_type": entry.get("auth_type"),
                "cloud": entry.get("cloud"),
                "workspace_id": entry.get("workspace_id"),
                "account_id": entry.get("account_id"),
                "valid": entry.get("valid"),
                "default": bool(entry.get("default")),
                "source": "cli",
            })
    except (DatabricksCliError, json.JSONDecodeError):
        profiles = _parse_cfg_file()

    with _cache_lock:
        _profiles_cache = list(profiles)
        _profiles_cache_at = now
    return list(profiles)


def get_profile(name: str) -> Optional[Dict[str, Any]]:
    """One profile by name, or None."""
    target = (name or "").strip()
    if not target:
        return None
    for profile in list_profiles():
        if profile.get("name") == target:
            return profile
    return None


def default_profile_name() -> Optional[str]:
    """The profile to use when the user named none.

    Preference order, most explicit first: ``DATABRICKS_CONFIG_PROFILE``, the
    profile the CLI marks default, a lone workspace profile, then the literal
    ``DEFAULT`` section.  Returns None when the choice would be a guess between
    several equally plausible profiles — better to ask than to query the wrong
    workspace.
    """
    env_choice = os.environ.get("DATABRICKS_CONFIG_PROFILE", "").strip()
    if env_choice:
        return env_choice

    profiles = list_profiles()
    if not profiles:
        return None

    for profile in profiles:
        if profile.get("default") and profile.get("host"):
            return str(profile["name"])

    # Account-level profiles can't run SQL; ignore them when picking for the user.
    workspace_profiles = [
        p for p in profiles if p.get("host") and not _is_account_host(str(p["host"]))
    ]
    if len(workspace_profiles) == 1:
        return str(workspace_profiles[0]["name"])

    for profile in profiles:
        if profile.get("name") == "DEFAULT" and profile.get("host"):
            return "DEFAULT"
    return None


def _is_account_host(host: str) -> bool:
    """True for an accounts-console host, which has no SQL warehouses."""
    return "accounts." in (host or "").lower()


def key_cmd_for_profile(name: str) -> str:
    """The token-minting command for a CLI profile.

    ``databricks auth token`` prints JSON with ``access_token`` and
    ``expires_in``, which is exactly the shape the credential layer already
    understands, so no special-casing is needed downstream.
    """
    safe = (name or "").strip()
    if not safe:
        raise ValueError("profile name is required")
    return f'databricks auth token -p "{safe}"'


def warehouse_ids(profile: Optional[str] = None) -> List[Dict[str, Any]]:
    """SQL warehouses visible to a profile, RUNNING first.

    Returns [] rather than raising when the CLI can't answer — warehouse
    discovery is a convenience, and the REST path can still auto-discover.
    """
    args = ["warehouses", "list", "-o", "json"]
    if profile:
        args += ["-p", profile]
    try:
        raw = _run_cli(args, _DESCRIBE_TIMEOUT)
        payload = json.loads(raw or "[]")
    except (DatabricksCliError, json.JSONDecodeError):
        return []

    items = payload if isinstance(payload, list) else payload.get("warehouses") or []
    warehouses = [
        {
            "id": w.get("id"),
            "name": w.get("name"),
            "state": w.get("state"),
            "serverless": bool(w.get("enable_serverless_compute")),
        }
        for w in items
        if isinstance(w, dict) and w.get("id")
    ]
    warehouses.sort(key=lambda w: 0 if str(w.get("state")).upper() == "RUNNING" else 1)
    return warehouses


def describe_profile(name: str) -> Dict[str, Any]:
    """Resolved auth detail for a profile, for diagnostics.

    ``auth describe`` is the CLI's own explanation of how it would authenticate
    and where each setting came from, which is far more useful than a guess when
    a credential misbehaves.
    """
    try:
        raw = _run_cli(["auth", "describe", "-p", name, "-o", "json"], _DESCRIBE_TIMEOUT)
        payload = json.loads(raw or "{}")
    except (DatabricksCliError, json.JSONDecodeError) as exc:
        return {"profile": name, "error": str(exc)[:300]}

    details = payload.get("details") or {}
    return {
        "profile": name,
        "status": payload.get("status"),
        "username": payload.get("username"),
        "auth_type": details.get("auth_type"),
        "host": details.get("host"),
    }


def invalidate_profile_cache() -> None:
    """Forget the cached profile list (used after a login/setup)."""
    global _profiles_cache, _profiles_cache_at
    with _cache_lock:
        _profiles_cache = None
        _profiles_cache_at = 0.0
