"""Workspace config + credential resolution for the databricks plugin.

Auth mirrors the model-provider ``key_cmd`` contract (see
``agent/command_token_source.py``): a command PRINTS a token, so the
credential is derived rather than stored.  This matters concretely here —
``databricks auth token`` mints a bearer that lives **3600s**.  A static PAT
copied into ``.env`` is the fallback, not the default.

There are three ways to describe a workspace, and they compose — an explicit
setting always beats a derived one:

1. **Nothing at all.**  With no ``databricks:`` section, the plugin adopts the
   Databricks CLI's default profile.  If the CLI is logged in, the plugin works.
2. **Name a CLI profile.**  ``profile: my-workspace`` derives the host and the
   token command from ``~/.databrickscfg``, so re-running
   ``databricks auth login`` is picked up with no config edit.
3. **Spell everything out.**  ``host`` + ``auth.key_cmd`` / ``auth.token_env``,
   for CI or a service principal that has no CLI profile.

Config lives in ``config.yaml`` under ``databricks:`` (never ``.env`` — that
holds secret VALUES only, per the Hermes config policy)::

    databricks:
      default_workspace: main
      workspaces:
        main:
          profile: adb-123           # CLI profile; host+auth derived from it
          warehouse_id: b81d2...     # optional; auto-discovered
        ci:
          host: https://adb-123.18.azuredatabricks.net
          auth:
            token_env: DATABRICKS_TOKEN

Token caching mirrors the provider path: honour the helper's advertised
``expires_in``/``expiry`` and re-mint shortly BEFORE expiry, so a request can
never be signed with a token that dies in flight.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from typing import Any, Dict, Optional, Tuple

from . import profiles as _profiles

# Re-mint slightly before the stated expiry so a token can't die mid-request.
# 60s matches agent/command_token_source.py's leeway.
_REFRESH_LEEWAY_SECONDS = 60.0
# A helper reads a local credential cache and answers in milliseconds;
# anything near this budget is hung, not slow.
_MINT_TIMEOUT_SECONDS = 15
# When a helper advertises no expiry we cannot cache for the life of the
# process, so re-mint on a bounded window instead.
_NO_TTL_REFRESH_SECONDS = 900.0

_CONFIG_SECTION = "databricks"

# Name reported for the workspace synthesized from the CLI's default profile
# when the user has configured nothing.
_IMPLICIT_WORKSPACE = "cli-default"

# workspace name -> (token, expires_at_monotonic)
_token_cache: Dict[str, Tuple[str, float]] = {}
_cache_lock = threading.Lock()


class DatabricksConfigError(RuntimeError):
    """Workspace is missing or misconfigured."""


class DatabricksAuthError(RuntimeError):
    """Credential resolution failed."""


def _load_raw_config() -> Dict[str, Any]:
    """Read the ``databricks:`` section from the active profile's config."""
    try:
        from hermes_cli.config import load_config
    except Exception as exc:  # pragma: no cover - defensive
        raise DatabricksConfigError(f"could not load Hermes config: {exc}") from exc
    cfg = load_config() or {}
    section = cfg.get(_CONFIG_SECTION)
    return section if isinstance(section, dict) else {}


def _implicit_workspace() -> Dict[str, Dict[str, Any]]:
    """Synthesize a workspace from the CLI's default profile, if there is one.

    This is what makes the plugin usable with an empty config: a user who has
    already run ``databricks auth login`` should not have to describe their
    workspace a second time.
    """
    name = _profiles.default_profile_name()
    if not name:
        return {}
    profile = _profiles.get_profile(name)
    host = (profile or {}).get("host")
    if not host:
        return {}
    return {
        _IMPLICIT_WORKSPACE: {
            "profile": name,
            "host": host,
            "_implicit": True,
        }
    }


def list_workspaces() -> Dict[str, Dict[str, Any]]:
    """Configured workspaces, or one derived from the CLI's default profile."""
    raw = _load_raw_config()
    ws = raw.get("workspaces")
    if isinstance(ws, dict) and ws:
        return ws
    return _implicit_workspace()


def default_workspace_name() -> Optional[str]:
    """Name of the default workspace: explicit, else the sole entry."""
    raw = _load_raw_config()
    explicit = raw.get("default_workspace")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    names = list(list_workspaces().keys())
    return names[0] if len(names) == 1 else None


def resolve_workspace(name: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    """Resolve ``name`` (or the default) to ``(name, workspace_config)``.

    Any field the workspace does not state explicitly is filled in from its CLI
    profile: ``host`` from the profile's host, and ``auth.key_cmd`` from
    ``databricks auth token -p <profile>``.  Explicit config always wins, so a
    workspace can name a profile purely for its host and still use a PAT.
    """
    workspaces = list_workspaces()
    if not workspaces:
        raise DatabricksConfigError(
            "no Databricks workspace available. Either log in with the CLI "
            "(`databricks auth login --host <url>`) so the default profile can "
            "be adopted automatically, or run `hermes databricks setup`."
        )
    target = (name or "").strip() or default_workspace_name()
    if not target:
        raise DatabricksConfigError(
            "multiple workspaces configured and no default set. Pass workspace= "
            f"explicitly (available: {', '.join(sorted(workspaces))}) or set "
            "databricks.default_workspace"
        )
    entry = workspaces.get(target)
    if not isinstance(entry, dict):
        raise DatabricksConfigError(
            f"unknown workspace {target!r} "
            f"(configured: {', '.join(sorted(workspaces)) or 'none'})"
        )

    resolved = dict(entry)
    profile_name = str(resolved.get("profile") or "").strip()

    if profile_name:
        profile = _profiles.get_profile(profile_name)
        if profile is None:
            known = [str(p.get("name")) for p in _profiles.list_profiles()]
            raise DatabricksConfigError(
                f"workspace {target!r} references Databricks CLI profile "
                f"{profile_name!r}, which does not exist "
                f"(available: {', '.join(known) or 'none'}). Run "
                f"`databricks auth login --profile {profile_name} --host <url>` "
                "or point the workspace at a different profile."
            )
        if not str(resolved.get("host") or "").strip():
            host = str(profile.get("host") or "").strip()
            if not host:
                raise DatabricksConfigError(
                    f"Databricks CLI profile {profile_name!r} has no host; "
                    f"set databricks.workspaces.{target}.host explicitly"
                )
            resolved["host"] = host
        auth = resolved.get("auth")
        auth = dict(auth) if isinstance(auth, dict) else {}
        if not auth.get("key_cmd") and not auth.get("token_env"):
            auth["key_cmd"] = _profiles.key_cmd_for_profile(profile_name)
            auth["_derived_from_profile"] = profile_name
        resolved["auth"] = auth

    host = str(resolved.get("host") or "").strip().rstrip("/")
    if not host:
        raise DatabricksConfigError(
            f"workspace {target!r} has no 'host' configured and no 'profile' to "
            "derive one from"
        )
    resolved["host"] = host
    return target, resolved


def _parse_mint_output(stdout: str) -> Tuple[str, Optional[float]]:
    """Extract ``(token, ttl_seconds)`` from a helper's stdout.

    Accepts a bare token or the JSON shape ``databricks auth token`` prints
    (``access_token`` + ``expires_in``/``expiry``), matching the provider
    contract in ``agent/command_token_source.py``.
    """
    text = (stdout or "").strip()
    if not text:
        raise DatabricksAuthError("auth key_cmd produced no output")

    if text.lstrip().startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DatabricksAuthError(
                "auth key_cmd printed JSON that could not be parsed"
            ) from exc
        token = str(payload.get("access_token") or "").strip()
        if not token:
            raise DatabricksAuthError(
                "auth key_cmd JSON has no 'access_token' field"
            )
        ttl: Optional[float] = None
        raw_ttl = payload.get("expires_in")
        if raw_ttl is not None:
            try:
                ttl = float(raw_ttl)
            except (TypeError, ValueError):
                ttl = None
        return token, ttl

    # Bare token: first line only, so a chatty helper can't smuggle extra data.
    return text.splitlines()[0].strip(), None


def _mint_via_command(command: str, workspace: str) -> Tuple[str, Optional[float]]:
    """Run ``command`` and return ``(token, ttl)``.

    Never surfaces stdout/stderr or the command string in errors: a helper can
    print a token on failure, and a key_cmd may legitimately embed a secret
    (``print-token --client-secret=...``).  Same discipline as core.
    """
    try:
        completed = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=_MINT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise DatabricksAuthError(
            f"auth key_cmd for workspace {workspace!r} timed out after "
            f"{_MINT_TIMEOUT_SECONDS}s"
        ) from exc
    except OSError as exc:
        raise DatabricksAuthError(
            f"auth key_cmd for workspace {workspace!r} could not be executed: {exc}"
        ) from exc

    if completed.returncode != 0:
        raise DatabricksAuthError(
            f"auth key_cmd for workspace {workspace!r} exited "
            f"{completed.returncode}. Run that command by hand to see why "
            "(e.g. `databricks auth login` if the OAuth session expired)."
        )
    return _parse_mint_output(completed.stdout)


def _token_from_env(env_name: str, workspace: str) -> str:
    """Read a static token from the active profile's secret scope."""
    try:
        from agent.secret_scope import get_secret
        value = get_secret(env_name, "")
    except Exception:
        import os
        value = os.environ.get(env_name, "")
    token = str(value or "").strip()
    if not token:
        raise DatabricksAuthError(
            f"workspace {workspace!r} uses token_env={env_name} but that "
            f"variable is unset. Add it to the profile's .env file."
        )
    # A pasted token may carry the header prefix; sending "Bearer Bearer x" 401s.
    if token[:7].lower() == "bearer ":
        token = token[7:].strip()
    return token


def get_token(workspace: Optional[str] = None, *, force_refresh: bool = False) -> str:
    """Resolve a bearer token for ``workspace``, minting/caching as needed."""
    name, entry = resolve_workspace(workspace)
    auth = entry.get("auth")
    auth = auth if isinstance(auth, dict) else {}

    key_cmd = str(auth.get("key_cmd") or "").strip()
    token_env = str(auth.get("token_env") or "").strip()

    # Static tokens are not cached: reading an env var is already free, and
    # caching would mask a token the user rotated mid-session.
    if not key_cmd:
        if token_env:
            return _token_from_env(token_env, name)
        raise DatabricksAuthError(
            f"workspace {name!r} has no auth configured. Set either "
            "auth.key_cmd (preferred: short-lived OAuth) or auth.token_env."
        )

    now = time.monotonic()
    if not force_refresh:
        with _cache_lock:
            cached = _token_cache.get(name)
            if cached and cached[1] > now:
                return cached[0]

    token, ttl = _mint_via_command(key_cmd, name)
    if ttl and ttl > 0:
        expires_at = now + max(1.0, ttl - _REFRESH_LEEWAY_SECONDS)
    else:
        expires_at = now + _NO_TTL_REFRESH_SECONDS
    with _cache_lock:
        _token_cache[name] = (token, expires_at)
    return token


def auth_kind(workspace: Optional[str] = None) -> str:
    """Describe the configured auth mechanism without resolving a credential."""
    try:
        _, entry = resolve_workspace(workspace)
    except DatabricksConfigError:
        return "unconfigured"
    auth = entry.get("auth")
    auth = auth if isinstance(auth, dict) else {}
    if auth.get("_derived_from_profile"):
        return f"cli-profile:{auth['_derived_from_profile']}"
    if str(auth.get("key_cmd") or "").strip():
        return "key_cmd"
    if str(auth.get("token_env") or "").strip():
        return "token_env"
    return "none"


def invalidate_token_cache(workspace: Optional[str] = None) -> None:
    """Drop cached tokens (all, or one workspace)."""
    with _cache_lock:
        if workspace is None:
            _token_cache.clear()
        else:
            _token_cache.pop(workspace, None)


def is_configured() -> bool:
    """True when at least one workspace can produce a host and a credential.

    Resolution is delegated to :func:`resolve_workspace` so a profile-only
    workspace (and the implicit CLI-default one) counts as configured — the
    tool gate must agree with what actually works, or tools vanish for a user
    whose setup is fine.
    """
    for name in list_workspaces():
        try:
            _, entry = resolve_workspace(name)
        except DatabricksConfigError:
            continue
        auth = entry.get("auth")
        auth = auth if isinstance(auth, dict) else {}
        if entry.get("host") and (auth.get("key_cmd") or auth.get("token_env")):
            return True
    return False
