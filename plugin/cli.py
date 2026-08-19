"""``hermes databricks`` CLI subcommand tree.

Grouped as a lifecycle: CONNECT (setup, status, workspaces) → EXPLORE
(catalogs) → QUERY (query, fetch).
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any, Dict

from . import profiles as _profiles
from . import slash as _slash
from . import tools as _tools
from .compute_cli import add_compute_parsers, handle_compute_cli


def setup_parser(subparser) -> None:
    """Build the argparse tree. ``subparser`` is the ``databricks`` parser."""
    actions = subparser.add_subparsers(dest="databricks_action")

    # ── CONNECT ──────────────────────────────────────────────────────────
    setup_p = actions.add_parser(
        "setup", help="[CONNECT] Configure a workspace (usually just a CLI profile)"
    )
    setup_p.add_argument("--name", default="main", help="Workspace name (default: main)")
    setup_p.add_argument(
        "--profile",
        help="Databricks CLI profile to use; host and auth are derived from it",
    )
    setup_p.add_argument(
        "--host",
        help="Workspace URL. Optional when --profile is given (derived from it)",
    )
    setup_p.add_argument("--token-env", help="Use a static token from this .env var instead")
    setup_p.add_argument("--warehouse-id", help="SQL warehouse id (optional; auto-discovered)")
    setup_p.add_argument("--default", action="store_true", help="Make this the default workspace")

    actions.add_parser("status", help="[CONNECT] Show workspaces and verify credentials")
    actions.add_parser("workspaces", help="[CONNECT] List configured workspaces")

    prof_p = actions.add_parser(
        "profiles", help="[CONNECT] List Databricks CLI profiles available to adopt"
    )
    prof_p.add_argument(
        "--refresh", action="store_true", help="Re-query the CLI instead of using cache"
    )
    prof_p.add_argument(
        "--warehouses", action="store_true",
        help="Also list each profile's SQL warehouses (slower)",
    )

    # ── EXPLORE ──────────────────────────────────────────────────────────
    cat_p = actions.add_parser("catalogs", help="[EXPLORE] Browse Unity Catalog")
    cat_p.add_argument("catalog", nargs="?", help="Catalog to inspect")
    cat_p.add_argument("schema", nargs="?", help="Schema to inspect")
    cat_p.add_argument("table", nargs="?", help="Table to describe (columns + types)")
    cat_p.add_argument("--workspace", help="Workspace name")

    # ── QUERY ────────────────────────────────────────────────────────────
    q_p = actions.add_parser("query", help="[QUERY] Run SQL, print rows (capped)")
    q_p.add_argument("sql", help="SQL statement")
    q_p.add_argument("--max-rows", type=int, default=100, help="Row cap (default 100)")
    q_p.add_argument("--catalog", help="Default catalog")
    q_p.add_argument("--schema", help="Default schema")
    q_p.add_argument("--workspace", help="Workspace name")
    q_p.add_argument("--json", action="store_true", help="Emit raw JSON")

    f_p = actions.add_parser(
        "fetch", help="[QUERY] Stream a full result set to a file (no size limit)"
    )
    f_p.add_argument("sql", help="SQL statement")
    f_p.add_argument("--path", help="Destination file (default: auto under HERMES_HOME)")
    f_p.add_argument("--format", choices=["csv", "jsonl"], default="csv")
    f_p.add_argument("--row-limit", type=int, help="Server-side row cap")
    f_p.add_argument("--preview-rows", type=int, default=5, help="Rows to preview")
    f_p.add_argument("--catalog", help="Default catalog")
    f_p.add_argument("--schema", help="Default schema")
    f_p.add_argument("--workspace", help="Workspace name")
    f_p.add_argument("--json", action="store_true", help="Emit raw JSON")

    # ── COMPUTE / LIBRARIES / JOBS ────────────────────────────────────────
    add_compute_parsers(actions)


def _config_set(key: str, value: str) -> bool:
    """Persist a config value via the Hermes CLI (never hand-edit config.yaml)."""
    result = subprocess.run(
        ["hermes", "config", "set", key, value, "--force"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        print(f"  ✗ failed to set {key}: {result.stderr.strip()[:200]}")
        return False
    return True


def _cmd_setup(args) -> int:
    name = args.name
    host = (args.host or "").strip().rstrip("/")
    profile = (args.profile or "").strip()

    if not profile and not host:
        print("✗ give at least one of --profile (recommended) or --host")
        print("  available CLI profiles:")
        found = _profiles.list_profiles()
        for p in found:
            print(f"    {p.get('name')}  {p.get('host') or '(no host)'}")
        if not found:
            print("    (none — run: databricks auth login --host <url>)")
        return 1

    if profile:
        entry = _profiles.get_profile(profile)
        if entry is None:
            known = ", ".join(str(p.get("name")) for p in _profiles.list_profiles())
            print(f"✗ Databricks CLI profile {profile!r} not found "
                  f"(available: {known or 'none'})")
            print(f"  create it with: databricks auth login --profile {profile} "
                  "--host <url>")
            return 1
        if entry.get("valid") is False:
            print(f"⚠ profile {profile!r} exists but the CLI reports it as invalid; "
                  "its credential may have expired")
        if not host and not entry.get("host"):
            print(f"✗ profile {profile!r} has no host; pass --host explicitly")
            return 1

    base = f"databricks.workspaces.{name}"
    ok = True
    if profile:
        ok &= _config_set(f"{base}.profile", profile)
    if host:
        ok &= _config_set(f"{base}.host", host)
    if args.token_env:
        ok &= _config_set(f"{base}.auth.token_env", args.token_env)
    if args.warehouse_id:
        ok &= _config_set(f"{base}.warehouse_id", args.warehouse_id)
    if args.default:
        ok &= _config_set("databricks.default_workspace", name)

    if not ok:
        return 1
    print(f"✓ workspace {name!r} configured")
    if profile and not args.token_env:
        print(f"  auth derives from CLI profile {profile!r} — nothing stored")
    print("  verifying credential...")
    _profiles.invalidate_profile_cache()
    payload = json.loads(_tools.databricks_status({"workspace": name}))
    print(_slash._render_status(payload))
    check = payload.get("verification") or {}
    return 1 if check.get("error") else 0


def _cmd_profiles(args) -> int:
    """Show what the Databricks CLI already knows, so setup is a choice not a form."""
    found = _profiles.list_profiles(refresh=bool(args.refresh))
    if not _profiles.cli_path():
        print("✗ the 'databricks' CLI is not on PATH")
        print("  install: https://docs.databricks.com/dev-tools/cli/install.html")
    print(f"Config file: {_profiles.config_file_path()}")
    if not found:
        print("\nNo CLI profiles found. Create one with:")
        print("  databricks auth login --host <workspace-url>")
        return 1

    print(f"\nProfiles ({len(found)}):")
    for p in found:
        flags = []
        if p.get("default"):
            flags.append("default")
        if p.get("valid") is True:
            flags.append("valid")
        elif p.get("valid") is False:
            flags.append("INVALID")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        print(f"  {p.get('name')}{suffix}")
        print(f"      host      {p.get('host') or '(none)'}")
        if p.get("auth_type"):
            print(f"      auth_type {p['auth_type']}")
        if args.warehouses:
            for w in _profiles.warehouse_ids(str(p.get("name"))):
                print(f"      warehouse {w['id']}  {w['name']}  ({w['state']})")

    print("\nAdopt one with:")
    print("  hermes databricks setup --profile <name> --default")
    print("Or configure nothing at all — the CLI's default profile is used "
          "automatically.")
    return 0


def _print_or_json(payload: Dict[str, Any], renderer, as_json: bool) -> int:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    elif payload.get("success"):
        print(renderer(payload))
    else:
        print(_slash._fail(payload))
    return 0 if payload.get("success") else 1


def handle_cli(args) -> int:
    """Dispatch ``hermes databricks <action>``."""
    action = getattr(args, "databricks_action", None)

    # Compute actions (cluster/library/job) live in their own module because
    # they spend money and carry a confirmation layer.
    handled = handle_compute_cli(args)
    if handled is not None:
        return handled

    if action == "setup":
        return _cmd_setup(args)

    if action == "profiles":
        return _cmd_profiles(args)

    if action in {"status", "workspaces", None}:
        payload = json.loads(_tools.databricks_status(
            {"verify": action == "status"}
        ))
        return _print_or_json(payload, _slash._render_status, False)

    if action == "catalogs":
        payload = json.loads(_tools.databricks_catalog({
            "catalog": args.catalog,
            "schema": args.schema,
            "table": args.table,
            "workspace": args.workspace,
        }))
        return _print_or_json(payload, _slash._render_catalog, False)

    if action == "query":
        payload = json.loads(_tools.databricks_query({
            "query": args.sql,
            "max_rows": args.max_rows,
            "catalog": args.catalog,
            "schema": args.schema,
            "workspace": args.workspace,
        }))
        return _print_or_json(payload, _slash._render_query, args.json)

    if action == "fetch":
        payload = json.loads(_tools.databricks_fetch({
            "query": args.sql,
            "path": args.path,
            "format": args.format,
            "row_limit": args.row_limit,
            "preview_rows": args.preview_rows,
            "catalog": args.catalog,
            "schema": args.schema,
            "workspace": args.workspace,
        }))
        return _print_or_json(payload, _slash._render_fetch, args.json)

    print("Unknown action. Run: hermes databricks --help")
    return 1
