"""Databricks plugin for Hermes Agent.

Query Unity Catalog with short-lived OAuth credentials and read result sets of
any size without flooding the conversation.

Why this exists alongside the Databricks managed MCP servers: the managed
``/api/2.0/mcp/sql`` endpoint takes a static bearer in a config header and
returns rows inline, so (a) the credential cannot be refreshed mid-session and
(b) a large result is truncated or refused.  This plugin resolves credentials
per call through ``key_cmd`` (the same contract Hermes model providers use for
enterprise gateways) and streams big results to disk via EXTERNAL_LINKS.
"""

from __future__ import annotations

from pathlib import Path

from . import tools as _tools
from .config import is_configured
from .schemas import (
    DATABRICKS_CATALOG_SCHEMA,
    DATABRICKS_FETCH_SCHEMA,
    DATABRICKS_QUERY_SCHEMA,
    DATABRICKS_STATUS_SCHEMA,
)

__version__ = "1.2.0"

# Bundled skills, registered read-only as ``databricks:<name>``.  The first is
# the entry point; the rest are loaded on demand for a specific job.
_SKILLS = (
    "databricks",
    "databricks-workspace-setup",
    "databricks-sql-authoring",
    "databricks-bulk-export",
    "databricks-compute",
    "databricks-troubleshooting",
)

# Handlers are gated on configuration EXCEPT databricks_status, which must stay
# reachable precisely when things are misconfigured — it is the diagnostic.
_GATED = (
    (DATABRICKS_QUERY_SCHEMA, _tools.databricks_query),
    (DATABRICKS_FETCH_SCHEMA, _tools.databricks_fetch),
    (DATABRICKS_CATALOG_SCHEMA, _tools.databricks_catalog),
)


def _check_configured() -> bool:
    """Gate: at least one workspace with a host and some auth."""
    try:
        return is_configured()
    except Exception:
        return False


def register(ctx) -> None:
    """Wire tools, slash command, CLI, and the bundled skill."""
    ctx.register_tool(
        name=DATABRICKS_STATUS_SCHEMA["name"],
        toolset="databricks",
        schema=DATABRICKS_STATUS_SCHEMA,
        handler=_tools.databricks_status,
    )

    for schema, handler in _GATED:
        ctx.register_tool(
            name=schema["name"],
            toolset="databricks",
            schema=schema,
            handler=handler,
            check_fn=_check_configured,
        )

    try:
        from .slash import handle_slash
        ctx.register_command(
            "databricks",
            handle_slash,
            "Databricks: status, catalogs, query, fetch to file",
            args_hint="status | catalogs [catalog] [schema] | query <sql> | fetch <sql>",
        )
    except Exception:
        pass

    try:
        from .cli import setup_parser, handle_cli
        ctx.register_cli_command(
            "databricks",
            "Manage Databricks workspaces and run queries",
            setup_parser,
            handle_cli,
        )
    except Exception:
        pass

    skills_root = Path(__file__).parent / "skills"
    for skill_name in _SKILLS:
        skill_path = skills_root / skill_name / "SKILL.md"
        if not skill_path.exists():
            continue
        try:
            ctx.register_skill(skill_name, skill_path)
        except Exception:
            # A skill failing to register must never take the tools down with it.
            pass
