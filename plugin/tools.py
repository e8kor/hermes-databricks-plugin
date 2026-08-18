"""Tool handlers for the databricks plugin.

Contract (Hermes plugin tools): every handler returns a JSON **string** for
both success and failure, never raises, and accepts ``**kwargs``.

Design rule that shapes this module: rows are expensive context.  The default
path (``databricks_query``) caps what it returns and tells the model when the
platform dropped data; the bulk path (``databricks_fetch``) returns a summary
plus a path and leaves the data on disk.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import api
from . import profiles
from .config import (
    DatabricksAuthError,
    DatabricksConfigError,
    auth_kind,
    default_workspace_name,
    get_token,
    list_workspaces,
    resolve_workspace,
)

# Inline row ceiling. Even when the platform would happily return more, a wide
# result serialized into the conversation is what actually breaks a session.
_DEFAULT_MAX_ROWS = 100
_HARD_MAX_ROWS = 1000
_MAX_PREVIEW_ROWS = 50
_MAX_CELL_CHARS = 500


def _ok(payload: Dict[str, Any]) -> str:
    return json.dumps({"success": True, **payload}, default=str)


def _err(message: str, **extra: Any) -> str:
    return json.dumps({"success": False, "error": message, **extra}, default=str)


def _truncate_cell(value: Any) -> Any:
    """Clip a single cell so one giant blob can't dominate the response."""
    if isinstance(value, str) and len(value) > _MAX_CELL_CHARS:
        return value[:_MAX_CELL_CHARS] + f"…[{len(value)} chars total]"
    return value


def _rows_as_dicts(columns: List[str], rows: List[List[Any]]) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        if columns:
            out.append({c: _truncate_cell(v) for c, v in zip(columns, row)})
        else:
            out.append({"values": [_truncate_cell(v) for v in row]})
    return out


def _export_dir() -> Path:
    """Profile-aware export directory (never a hardcoded ~/.hermes)."""
    try:
        from hermes_constants import get_hermes_home
        base = Path(get_hermes_home())
    except Exception:
        base = Path.home() / ".hermes"
    return base / "databricks-exports"


def _auto_path(fmt: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return _export_dir() / f"query-{stamp}.{fmt}"


def _safe_dest(path_arg: Optional[str], fmt: str) -> Path:
    if not path_arg:
        return _auto_path(fmt)
    dest = Path(str(path_arg)).expanduser()
    if not dest.is_absolute():
        dest = _export_dir() / dest
    return dest


def databricks_status(args: dict, **kwargs) -> str:
    """Report configuration and, optionally, prove a credential resolves."""
    workspace = (args.get("workspace") or "").strip() or None
    verify = args.get("verify")
    verify = True if verify is None else bool(verify)

    try:
        workspaces = list_workspaces()
    except Exception as exc:
        return _err(f"could not read config: {exc}")

    if not workspaces:
        cli_installed = bool(profiles.cli_path())
        found = profiles.list_profiles()
        return _ok({
            "configured": False,
            "workspaces": [],
            "cli": {
                "installed": cli_installed,
                "config_file": str(profiles.config_file_path()),
                "profiles": [p.get("name") for p in found],
            },
            "hint": (
                "No Databricks workspace available.\n"
                + (
                    "The databricks CLI is not on PATH. Install it, then run:\n"
                    "  databricks auth login --host <workspace-url>\n"
                    if not cli_installed
                    else "The CLI has no usable profile. Log in with:\n"
                    "  databricks auth login --host <workspace-url>\n"
                )
                + "After logging in the default profile is picked up "
                "automatically — no Hermes config needed.\n"
                "To pin a specific profile instead:\n"
                "  hermes databricks setup --profile <profile-name>"
            ),
        })

    entries = []
    for name, cfg in sorted(workspaces.items()):
        cfg = cfg if isinstance(cfg, dict) else {}
        # Report the RESOLVED view: a profile-based workspace has no literal
        # host in config, and printing "host: None" would look broken.
        try:
            _, resolved = resolve_workspace(name)
        except Exception:
            resolved = cfg
        entry = {
            "name": name,
            "host": resolved.get("host") or cfg.get("host"),
            "warehouse_id": resolved.get("warehouse_id") or "(auto-discover)",
            "auth": auth_kind(name),
            "is_default": name == default_workspace_name(),
        }
        if cfg.get("profile"):
            entry["cli_profile"] = cfg["profile"]
        if cfg.get("_implicit"):
            # Be explicit that nothing was configured — this workspace exists
            # only because the CLI had a usable default profile.
            entry["source"] = "databricks-cli-default-profile"
        entries.append(entry)

    result: Dict[str, Any] = {
        "configured": True,
        "default_workspace": default_workspace_name(),
        "workspaces": entries,
    }

    try:
        cli_profiles = profiles.list_profiles()
        result["cli"] = {
            "installed": bool(profiles.cli_path()),
            "config_file": str(profiles.config_file_path()),
            "profiles": [
                {
                    "name": p.get("name"),
                    "host": p.get("host"),
                    "auth_type": p.get("auth_type"),
                    "valid": p.get("valid"),
                    "default": p.get("default"),
                }
                for p in cli_profiles
            ],
        }
    except Exception as exc:  # pragma: no cover - diagnostics must not fail
        result["cli"] = {"error": str(exc)[:200]}

    if verify:
        target = workspace or default_workspace_name()
        check: Dict[str, Any] = {"workspace": target}
        try:
            started = time.monotonic()
            token = get_token(target)
            check["token_resolved"] = True
            check["token_chars"] = len(token)
            check["mint_ms"] = round((time.monotonic() - started) * 1000)
            body = api.execute_statement(
                "SELECT current_catalog() AS catalog, current_user() AS user",
                workspace=target,
            )
            rows = list(api.iter_all_rows(body, workspace=target))
            if rows:
                check["catalog"] = rows[0][0]
                check["user"] = rows[0][1]
            check["api_reachable"] = True
        except (DatabricksConfigError, DatabricksAuthError) as exc:
            check["token_resolved"] = False
            check["error"] = str(exc)
        except api.DatabricksAPIError as exc:
            check["api_reachable"] = False
            check["error"] = str(exc)
        except Exception as exc:  # pragma: no cover - defensive
            check["error"] = f"unexpected: {exc}"
        result["verification"] = check

    return _ok(result)


def databricks_query(args: dict, **kwargs) -> str:
    """Run SQL and return a capped set of rows inline."""
    query = str(args.get("query") or "").strip()
    if not query:
        return _err("query is required")

    try:
        max_rows = int(args.get("max_rows") or _DEFAULT_MAX_ROWS)
    except (TypeError, ValueError):
        max_rows = _DEFAULT_MAX_ROWS
    max_rows = max(1, min(max_rows, _HARD_MAX_ROWS))

    try:
        timeout = float(args.get("timeout_seconds") or 300.0)
    except (TypeError, ValueError):
        timeout = 300.0

    workspace = (args.get("workspace") or "").strip() or None

    try:
        body = api.execute_statement(
            query,
            workspace=workspace,
            disposition="INLINE",
            catalog=(args.get("catalog") or "").strip() or None,
            schema=(args.get("schema") or "").strip() or None,
            timeout_seconds=timeout,
        )
    except (DatabricksConfigError, DatabricksAuthError) as exc:
        return _err(str(exc), hint="run databricks_status to check configuration")
    except api.InlineLimitExceeded as exc:
        # Not a broken query — the result is simply too big for the inline path.
        return _err(
            str(exc),
            too_large_for_inline=True,
            next_step=(
                "call databricks_fetch with this same query to stream the full "
                "result to a file"
            ),
        )
    except api.DatabricksAPIError as exc:
        return _err(str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        return _err(f"unexpected failure: {exc}")

    stats = api.result_stats(body)
    columns = stats.get("columns") or []

    rows: List[List[Any]] = []
    capped = False
    try:
        for row in api.iter_all_rows(body, workspace=workspace):
            if len(rows) >= max_rows:
                capped = True
                break
            rows.append(row)
    except api.DatabricksAPIError as exc:
        return _err(f"reading rows failed: {exc}")

    payload: Dict[str, Any] = {
        "columns": columns,
        "row_count": len(rows),
        "rows": _rows_as_dicts(columns, rows),
        "total_rows_available": stats.get("total_rows"),
        "statement_id": stats.get("statement_id"),
    }

    # Two very different kinds of "you are not seeing everything".
    if stats.get("truncated"):
        payload["truncated_by_platform"] = True
        payload["warning"] = (
            "Databricks dropped rows to fit the inline size limit — this result "
            "is INCOMPLETE. Re-run with databricks_fetch to get the full set."
        )
    if capped:
        payload["capped_by_max_rows"] = max_rows
        payload.setdefault("warning", (
            f"Showing the first {max_rows} rows only. Raise max_rows, or use "
            "databricks_fetch to write the whole result to a file."
        ))

    return _ok(payload)


def databricks_fetch(args: dict, **kwargs) -> str:
    """Stream a complete result set to disk; return a summary and a path."""
    query = str(args.get("query") or "").strip()
    if not query:
        return _err("query is required")

    fmt = str(args.get("format") or "csv").lower()
    if fmt not in {"csv", "jsonl"}:
        return _err("format must be 'csv' or 'jsonl'")

    try:
        preview_rows = int(args.get("preview_rows", 5))
    except (TypeError, ValueError):
        preview_rows = 5
    preview_rows = max(0, min(preview_rows, _MAX_PREVIEW_ROWS))

    try:
        timeout = float(args.get("timeout_seconds") or 600.0)
    except (TypeError, ValueError):
        timeout = 600.0

    row_limit = args.get("row_limit")
    try:
        row_limit = int(row_limit) if row_limit else None
    except (TypeError, ValueError):
        row_limit = None

    workspace = (args.get("workspace") or "").strip() or None
    dest = _safe_dest(args.get("path"), fmt)

    try:
        started = time.monotonic()
        body = api.execute_statement(
            query,
            workspace=workspace,
            disposition="EXTERNAL_LINKS",
            row_limit=row_limit,
            catalog=(args.get("catalog") or "").strip() or None,
            schema=(args.get("schema") or "").strip() or None,
            timeout_seconds=timeout,
        )
        summary = api.download_to_file(body, dest, workspace=workspace, fmt=fmt)
        summary["elapsed_seconds"] = round(time.monotonic() - started, 1)
    except (DatabricksConfigError, DatabricksAuthError) as exc:
        return _err(str(exc), hint="run databricks_status to check configuration")
    except api.DatabricksAPIError as exc:
        return _err(str(exc))
    except OSError as exc:
        return _err(f"could not write {dest}: {exc}")
    except Exception as exc:  # pragma: no cover - defensive
        return _err(f"unexpected failure: {exc}")

    preview: List[Dict[str, Any]] = []
    if preview_rows and summary.get("rows_written"):
        try:
            columns = summary.get("columns") or []
            with open(dest, "r", encoding="utf-8") as fh:
                if fmt == "csv":
                    import csv as _csv
                    reader = _csv.reader(fh)
                    next(reader, None)  # header
                    for i, row in enumerate(reader):
                        if i >= preview_rows:
                            break
                        preview.append({c: _truncate_cell(v)
                                        for c, v in zip(columns, row)})
                else:
                    for i, line in enumerate(fh):
                        if i >= preview_rows:
                            break
                        rec = json.loads(line)
                        preview.append({k: _truncate_cell(v) for k, v in rec.items()})
        except Exception:
            preview = []

    summary["preview"] = preview
    summary["note"] = (
        "Full result is on disk — analyze it with terminal or execute_code rather "
        "than reading the whole file into context."
    )
    return _ok(summary)


def databricks_catalog(args: dict, **kwargs) -> str:
    """List catalogs, schemas, tables, or describe one table."""
    catalog = (args.get("catalog") or "").strip() or None
    schema = (args.get("schema") or "").strip() or None
    table = (args.get("table") or "").strip() or None
    workspace = (args.get("workspace") or "").strip() or None

    if schema and not catalog:
        return _err("schema requires catalog")
    if table and not (catalog and schema):
        return _err("table requires both catalog and schema")

    try:
        result = api.list_catalog(
            workspace=workspace, catalog=catalog, schema=schema, table=table
        )
    except (DatabricksConfigError, DatabricksAuthError) as exc:
        return _err(str(exc), hint="run databricks_status to check configuration")
    except api.DatabricksAPIError as exc:
        return _err(str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        return _err(f"unexpected failure: {exc}")

    return _ok(result)
