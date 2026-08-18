"""``/databricks`` slash command.

Renders aligned plain-text tables rather than the raw JSON the tool handlers
return, per the cross-plugin display convention.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from . import tools as _tools

_HELP = """/databricks — Databricks Unity Catalog

  /databricks status                    configured workspaces + credential check
  /databricks profiles                  Databricks CLI profiles available to adopt
  /databricks catalogs                  list catalogs
  /databricks catalogs <catalog>        list schemas in a catalog
  /databricks catalogs <cat> <schema>   list tables in a schema
  /databricks catalogs <cat> <sch> <tbl>  describe one table's columns
  /databricks query <sql>               run SQL, show rows inline (capped)
  /databricks fetch <sql>               stream full result to a file on disk

Large or unknown-size results belong in `fetch` — it has no size ceiling and
keeps the data out of the conversation."""


def _render_table(columns: List[str], rows: List[Dict[str, Any]], limit: int = 30) -> str:
    """Render rows as an aligned plain-text table."""
    if not rows:
        return "(no rows)"
    if not columns:
        columns = sorted({k for r in rows for k in r})
    shown = rows[:limit]

    def cell(value: Any) -> str:
        text = "" if value is None else str(value)
        return text if len(text) <= 40 else text[:37] + "..."

    widths = {
        c: max(len(c), *(len(cell(r.get(c))) for r in shown)) if shown else len(c)
        for c in columns
    }
    head = "  ".join(c.ljust(widths[c]) for c in columns)
    rule = "  ".join("-" * widths[c] for c in columns)
    body = [
        "  ".join(cell(r.get(c)).ljust(widths[c]) for c in columns) for r in shown
    ]
    out = [head, rule, *body]
    if len(rows) > limit:
        out.append(f"... {len(rows) - limit} more row(s) not shown")
    return "\n".join(out)


def _fail(payload: Dict[str, Any]) -> str:
    lines = [f"✗ {payload.get('error', 'unknown error')}"]
    for key in ("hint", "next_step"):
        if payload.get(key):
            lines.append(f"  → {payload[key]}")
    return "\n".join(lines)


def _render_status(payload: Dict[str, Any]) -> str:
    if not payload.get("configured"):
        return "No Databricks workspaces configured.\n\n" + str(payload.get("hint", ""))
    lines = ["Databricks workspaces:"]
    for ws in payload.get("workspaces", []):
        marker = "*" if ws.get("is_default") else " "
        lines.append(f" {marker} {ws.get('name')}")
        lines.append(f"     host      {ws.get('host')}")
        lines.append(f"     warehouse {ws.get('warehouse_id')}")
        lines.append(f"     auth      {ws.get('auth')}")
        if ws.get("source") == "databricks-cli-default-profile":
            lines.append("     (adopted from the Databricks CLI default profile — "
                         "nothing configured in Hermes)")
    cli = payload.get("cli") or {}
    cli_profiles = cli.get("profiles") or []
    if cli_profiles:
        lines.append("")
        lines.append(f"Databricks CLI profiles ({len(cli_profiles)}):")
        for p in cli_profiles:
            flags = []
            if p.get("default"):
                flags.append("default")
            if p.get("valid") is True:
                flags.append("valid")
            elif p.get("valid") is False:
                flags.append("INVALID")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            lines.append(f"  {p.get('name')}{suffix}  {p.get('host') or ''}")
    elif cli.get("installed") is False:
        lines.append("")
        lines.append("Databricks CLI: not installed")
    check = payload.get("verification") or {}
    if check:
        lines.append("")
        if check.get("error"):
            lines.append(f"  credential check FAILED: {check['error']}")
        else:
            lines.append(
                f"  credential ok — minted {check.get('token_chars')} chars "
                f"in {check.get('mint_ms')}ms"
            )
            if check.get("user"):
                lines.append(
                    f"  connected as {check['user']} (catalog {check.get('catalog')})"
                )
    return "\n".join(lines)


def _render_catalog(payload: Dict[str, Any]) -> str:
    level = payload.get("level")
    if level == "catalogs":
        names = payload.get("catalogs") or []
        return f"Catalogs ({len(names)}):\n" + "\n".join(f"  {n}" for n in names)
    if level == "schemas":
        names = payload.get("schemas") or []
        return (f"Schemas in {payload.get('catalog')} ({len(names)}):\n"
                + "\n".join(f"  {n}" for n in names))
    if level == "table":
        cols = payload.get("columns") or []
        lines = [f"{payload.get('full_name')}  [{payload.get('type')}]"]
        if payload.get("comment"):
            lines.append(f"  {payload['comment']}")
        lines.append(f"  {len(cols)} column(s) via {payload.get('columns_source')}:")
        width = max((len(str(c.get("name"))) for c in cols), default=0)
        for c in cols:
            lines.append(f"    {str(c.get('name')).ljust(width)}  {c.get('type') or ''}")
        return "\n".join(lines)

    tables = payload.get("tables") or []
    head = f"Tables in {payload.get('catalog')}.{payload.get('schema')} ({len(tables)}):"
    body = []
    for t in tables:
        cols = t.get("columns") or []
        body.append(f"  {t.get('name')}  [{t.get('type')}]")
        if cols:
            preview = ", ".join(cols[:8]) + ("..." if len(cols) > 8 else "")
            body.append(f"      {preview}")
        else:
            body.append("      (columns not exposed — describe this table by name)")
    out = head + "\n" + "\n".join(body)
    if payload.get("note"):
        out += f"\n\n⚠ {payload['note']}"
    return out


def _render_query(payload: Dict[str, Any]) -> str:
    out = [_render_table(payload.get("columns") or [], payload.get("rows") or [])]
    total = payload.get("total_rows_available")
    shown = payload.get("row_count")
    if total is not None:
        out.append(f"\n{shown} shown / {total} total")
    if payload.get("warning"):
        out.append(f"\n⚠ {payload['warning']}")
    return "\n".join(out)


def _render_fetch(payload: Dict[str, Any]) -> str:
    lines = [
        "Result written to disk:",
        f"  path    {payload.get('path')}",
        f"  format  {payload.get('format')}",
        f"  rows    {payload.get('rows_written')}",
        f"  size    {payload.get('bytes_on_disk')} bytes",
        f"  chunks  {payload.get('total_chunks')}",
        f"  elapsed {payload.get('elapsed_seconds')}s",
    ]
    preview = payload.get("preview") or []
    if preview:
        lines.append("\nPreview:")
        lines.append(_render_table(payload.get("columns") or [], preview))
    return "\n".join(lines)


def handle_slash(raw_args: str) -> str:
    """Dispatch ``/databricks <sub> ...``."""
    parts = (raw_args or "").strip().split(maxsplit=1)
    if not parts or parts[0] in {"help", "-h", "--help"}:
        return _HELP
    sub = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub == "status":
        payload = json.loads(_tools.databricks_status({}))
        return _render_status(payload) if payload.get("success") else _fail(payload)

    if sub == "profiles":
        from . import profiles as _profiles
        found = _profiles.list_profiles(refresh=rest.strip() == "--refresh")
        lines = [f"Databricks CLI config: {_profiles.config_file_path()}"]
        if not _profiles.cli_path():
            lines.append("✗ the 'databricks' CLI is not on PATH")
        if not found:
            lines.append("")
            lines.append("No CLI profiles. Create one with:")
            lines.append("  databricks auth login --host <workspace-url>")
            return "\n".join(lines)
        lines.append("")
        lines.append(f"Profiles ({len(found)}):")
        for p in found:
            flags = []
            if p.get("default"):
                flags.append("default")
            if p.get("valid") is True:
                flags.append("valid")
            elif p.get("valid") is False:
                flags.append("INVALID")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            lines.append(f"  {p.get('name')}{suffix}")
            lines.append(f"      {p.get('host') or '(no host)'}")
        lines.append("")
        lines.append("Adopt one: hermes databricks setup --profile <name> --default")
        return "\n".join(lines)

    if sub in {"catalogs", "catalog"}:
        bits = rest.split()
        args = {}
        if len(bits) >= 1:
            args["catalog"] = bits[0]
        if len(bits) >= 2:
            args["schema"] = bits[1]
        if len(bits) >= 3:
            args["table"] = bits[2]
        payload = json.loads(_tools.databricks_catalog(args))
        return _render_catalog(payload) if payload.get("success") else _fail(payload)

    if sub == "query":
        if not rest:
            return "Usage: /databricks query <sql>"
        payload = json.loads(_tools.databricks_query({"query": rest}))
        return _render_query(payload) if payload.get("success") else _fail(payload)

    if sub == "fetch":
        if not rest:
            return "Usage: /databricks fetch <sql>"
        payload = json.loads(_tools.databricks_fetch({"query": rest}))
        return _render_fetch(payload) if payload.get("success") else _fail(payload)

    return f"Unknown subcommand {sub!r}.\n\n{_HELP}"
