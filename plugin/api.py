"""Databricks SQL Statement Execution API client.

Two result dispositions, and the distinction is the whole point of this module:

* ``INLINE`` — rows come back in the JSON response.  Databricks caps this at
  ~25 MiB and, critically, signals overflow with ``manifest.truncated: true``
  buried mid-payload.  A caller that ignores that flag silently loses rows.
* ``EXTERNAL_LINKS`` — the response carries presigned URLs to chunk files.
  No inline ceiling; a 500k-row / 157 MB result arrives as 24 chunks with
  ``truncated: false``.  This is the only honest path for "big data", and it
  streams to DISK rather than into the model's context window.

Every response is checked for truncation.  A truncated INLINE result is
reported as such, never quietly returned as if complete.
"""

from __future__ import annotations

import csv
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .config import get_token, resolve_workspace

# The API rejects wait_timeout above 50s; beyond that a statement goes async
# and must be polled.
_MAX_WAIT_TIMEOUT_SECONDS = 50
_POLL_INTERVAL_SECONDS = 2.0
_HTTP_TIMEOUT_SECONDS = 300
_TERMINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"}


class DatabricksAPIError(RuntimeError):
    """A Databricks REST call failed."""


class InlineLimitExceeded(DatabricksAPIError):
    """An INLINE statement exceeded the platform's 25 MiB result cap.

    Distinct from a generic failure because the remedy is specific and
    automatic: retry the same query with EXTERNAL_LINKS.
    """


def _request(
    workspace: Optional[str],
    path: str,
    payload: Optional[dict] = None,
    method: str = "POST",
) -> dict:
    """Call the workspace REST API and return the decoded JSON body."""
    name, entry = resolve_workspace(workspace)
    token = get_token(name)
    url = f"{entry['host']}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "hermes-databricks-plugin/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = (exc.read() or b"").decode("utf-8", "replace")[:600]
        except Exception:
            pass
        raise DatabricksAPIError(
            f"Databricks API {method} {path} failed: HTTP {exc.code}. {body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise DatabricksAPIError(
            f"Databricks API {method} {path} unreachable: {exc.reason}"
        ) from exc


def resolve_warehouse_id(workspace: Optional[str] = None) -> str:
    """Return the configured warehouse id, else the first RUNNING one."""
    _, entry = resolve_workspace(workspace)
    configured = str(entry.get("warehouse_id") or "").strip()
    if configured:
        return configured

    body = _request(workspace, "/api/2.0/sql/warehouses", method="GET")
    warehouses = body.get("warehouses") or []
    if not warehouses:
        raise DatabricksAPIError(
            "no SQL warehouses available in this workspace; set "
            "databricks.workspaces.<name>.warehouse_id explicitly"
        )
    running = [w for w in warehouses if str(w.get("state")).upper() == "RUNNING"]
    chosen = (running or warehouses)[0]
    return str(chosen.get("id"))


def column_names(manifest: dict) -> List[str]:
    """Column names from a statement manifest."""
    schema = (manifest or {}).get("schema") or {}
    return [str(c.get("name")) for c in (schema.get("columns") or [])]


def normalize_rows(data_array: Any) -> List[List[Any]]:
    """Normalize the two row shapes the platform emits for JSON_ARRAY.

    Managed-MCP style returns ``[{"values": [{"string_value": "x"}]}]`` while
    the direct statement API returns plain ``[["x"]]``.
    """
    rows: List[List[Any]] = []
    for row in data_array or []:
        if isinstance(row, dict):
            out: List[Any] = []
            for cell in row.get("values") or []:
                if isinstance(cell, dict):
                    if cell.get("is_null"):
                        out.append(None)
                    elif "string_value" in cell:
                        out.append(cell.get("string_value"))
                    else:
                        out.append(next(iter(cell.values()), None))
                else:
                    out.append(cell)
            rows.append(out)
        elif isinstance(row, list):
            rows.append(row)
    return rows


def _poll_until_terminal(
    workspace: Optional[str], statement_id: str, timeout_seconds: float
) -> dict:
    """Poll a statement until terminal state or the deadline."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        body = _request(
            workspace, f"/api/2.0/sql/statements/{statement_id}", method="GET"
        )
        state = str(((body.get("status") or {}).get("state")) or "").upper()
        if state in _TERMINAL_STATES:
            return body
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise DatabricksAPIError(
        f"statement {statement_id} still running after {timeout_seconds:g}s; "
        "narrow the query or raise timeout_seconds"
    )


def _check_succeeded(body: dict) -> dict:
    """Raise unless the statement succeeded.

    Inline overflow has TWO distinct behaviours, and both must be surfaced:

    * moderate overflow → SUCCEEDED with ``manifest.truncated: true`` (rows
      silently dropped — see :func:`is_truncated`);
    * hard overflow (>26214400 bytes) → FAILED with "Inline byte limit
      exceeded".

    The second arrives as a generic failure, so it is re-labelled here to point
    at the chunked path instead of looking like a broken query.
    """
    status = body.get("status") or {}
    state = str(status.get("state") or "").upper()
    if state != "SUCCEEDED":
        err = status.get("error") or {}
        message = err.get("message") or f"statement ended in state {state}"
        if "inline byte limit" in message.lower():
            raise InlineLimitExceeded(
                "result exceeds the 25 MiB inline limit. Re-run with "
                "disposition=EXTERNAL_LINKS (the databricks_fetch tool) to "
                "stream the full result to disk."
            )
        raise DatabricksAPIError(f"query {state}: {message}")
    return body


def execute_statement(
    query: str,
    *,
    workspace: Optional[str] = None,
    disposition: str = "INLINE",
    row_limit: Optional[int] = None,
    catalog: Optional[str] = None,
    schema: Optional[str] = None,
    timeout_seconds: float = 300.0,
) -> dict:
    """Run ``query`` and return the terminal statement body.

    ``disposition`` is ``INLINE`` (small results, rows in the response) or
    ``EXTERNAL_LINKS`` (large results, presigned chunk URLs).
    """
    disposition = (disposition or "INLINE").upper()
    if disposition not in {"INLINE", "EXTERNAL_LINKS"}:
        raise ValueError("disposition must be INLINE or EXTERNAL_LINKS")

    payload: Dict[str, Any] = {
        "statement": query,
        "warehouse_id": resolve_warehouse_id(workspace),
        "disposition": disposition,
        "format": "JSON_ARRAY",
        "wait_timeout": f"{_MAX_WAIT_TIMEOUT_SECONDS}s",
        "on_wait_timeout": "CONTINUE",
    }
    if row_limit and row_limit > 0:
        payload["row_limit"] = int(row_limit)
    if catalog:
        payload["catalog"] = catalog
    if schema:
        payload["schema"] = schema

    body = _request(workspace, "/api/2.0/sql/statements", payload)
    state = str(((body.get("status") or {}).get("state")) or "").upper()
    if state not in _TERMINAL_STATES:
        statement_id = str(body.get("statement_id") or "")
        if not statement_id:
            raise DatabricksAPIError("API returned no statement_id to poll")
        body = _poll_until_terminal(workspace, statement_id, timeout_seconds)
    return _check_succeeded(body)


def is_truncated(body: dict) -> bool:
    """True when the platform dropped rows to fit the inline size cap.

    This flag sits inside ``manifest`` and is the single most dangerous thing
    to ignore in this API: the response still says SUCCEEDED.
    """
    return bool((body.get("manifest") or {}).get("truncated"))


def result_stats(body: dict) -> Dict[str, Any]:
    """Summarize a statement result without carrying its rows."""
    manifest = body.get("manifest") or {}
    return {
        "statement_id": body.get("statement_id"),
        "columns": column_names(manifest),
        "total_rows": manifest.get("total_row_count"),
        "total_bytes": manifest.get("total_byte_count"),
        "total_chunks": manifest.get("total_chunk_count"),
        "truncated": is_truncated(body),
    }


def _fetch_chunk_index(
    workspace: Optional[str], statement_id: str, chunk_index: int
) -> dict:
    """Fetch one chunk's external-link descriptor set."""
    return _request(
        workspace,
        f"/api/2.0/sql/statements/{statement_id}/result/chunks/{chunk_index}",
        method="GET",
    )


def _download_external_link(url: str) -> List[List[Any]]:
    """Download one presigned chunk file and return its rows.

    The presigned URL carries its own auth in the query string, so NO
    Authorization header is sent — adding one makes S3/ADLS reject the request.
    """
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise DatabricksAPIError(
            f"chunk download failed: HTTP {exc.code} (presigned link may have expired)"
        ) from exc
    except urllib.error.URLError as exc:
        raise DatabricksAPIError(f"chunk download unreachable: {exc.reason}") from exc
    if not raw:
        return []
    return normalize_rows(json.loads(raw))


def iter_all_rows(
    body: dict, *, workspace: Optional[str] = None
) -> Iterator[List[Any]]:
    """Yield every row of a completed statement, following all chunks.

    Handles both dispositions: inline rows come straight from the body, while
    EXTERNAL_LINKS results are walked chunk by chunk via ``next_chunk_index``
    so memory stays bounded to one chunk at a time.
    """
    statement_id = str(body.get("statement_id") or "")
    result = body.get("result") or {}

    # Inline rows (present for INLINE, absent for EXTERNAL_LINKS).
    for row in normalize_rows(result.get("data_array")):
        yield row

    links = list(result.get("external_links") or [])
    seen: set[int] = set()
    while links:
        link = links.pop(0)
        idx = link.get("chunk_index")
        if isinstance(idx, int):
            if idx in seen:
                continue
            seen.add(idx)
        url = link.get("external_link")
        if url:
            for row in _download_external_link(url):
                yield row
        nxt = link.get("next_chunk_index")
        if isinstance(nxt, int) and nxt not in seen and statement_id:
            nxt_body = _fetch_chunk_index(workspace, statement_id, nxt)
            links.extend(nxt_body.get("external_links") or [])


def download_to_file(
    body: dict,
    dest: Path,
    *,
    workspace: Optional[str] = None,
    fmt: str = "csv",
) -> Dict[str, Any]:
    """Stream every row to ``dest`` (csv or jsonl); return a summary.

    Rows are written as they arrive so a multi-GB result never lands in memory
    or in the model's context.  The agent gets back counts and a path.
    """
    fmt = (fmt or "csv").lower()
    if fmt not in {"csv", "jsonl"}:
        raise ValueError("fmt must be 'csv' or 'jsonl'")

    cols = column_names(body.get("manifest") or {})
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    with open(dest, "w", encoding="utf-8", newline="") as fh:
        if fmt == "csv":
            writer = csv.writer(fh)
            if cols:
                writer.writerow(cols)
            for row in iter_all_rows(body, workspace=workspace):
                writer.writerow(row)
                written += 1
        else:
            for row in iter_all_rows(body, workspace=workspace):
                record = dict(zip(cols, row)) if cols else {"values": row}
                fh.write(json.dumps(record, default=str) + "\n")
                written += 1

    stats = result_stats(body)
    stats.update({
        "path": str(dest),
        "format": fmt,
        "rows_written": written,
        "bytes_on_disk": dest.stat().st_size,
    })
    return stats


def _q(value: str) -> str:
    """Escape a query-string value fully.

    ``quote`` defaults to ``safe="/"``, which leaves a slash inside a catalog or
    schema name (or an opaque page token) unescaped and silently corrupts the
    query string.  Nothing here is a path segment, so nothing is safe.
    """
    return urllib.parse.quote(str(value), safe="")


def _paginate(
    workspace: Optional[str], path: str, collection: str
) -> List[Dict[str, Any]]:
    """Collect every page of a Unity Catalog list endpoint.

    UC list endpoints return at most a page of results plus a
    ``next_page_token``; a caller that reads only the first response silently
    reports a partial catalog as if it were complete.  Proven live: listing
    ``samples.tpcds_sf1`` with ``max_results=3`` needs 8 pages for 24 tables.
    """
    items: List[Dict[str, Any]] = []
    token: Optional[str] = None
    seen_tokens: set[str] = set()
    while True:
        page_path = path
        if token:
            joiner = "&" if "?" in page_path else "?"
            # safe="" so an opaque token containing '/' is escaped too.
            page_path = f"{page_path}{joiner}page_token={_q(token)}"
        body = _request(workspace, page_path, method="GET")
        items.extend(body.get(collection) or [])
        token = body.get("next_page_token") or None
        # Defensive: a server that echoes the same token would loop forever.
        if not token or token in seen_tokens:
            return items
        seen_tokens.add(token)


def _uc_columns(table: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Column name/type pairs from a Unity Catalog table payload."""
    return [
        {"name": c.get("name"), "type": c.get("type_text") or c.get("type_name")}
        for c in (table.get("columns") or [])
        if c.get("name")
    ]


def describe_table(
    full_name: str, *, workspace: Optional[str] = None
) -> Dict[str, Any]:
    """Describe one table, falling back to SQL when Unity Catalog won't say.

    Unity Catalog REST returns an EMPTY ``columns`` list for some securable
    kinds — notably ``TABLE_DELTASHARING`` (the whole ``samples.tpch`` and
    ``samples.tpcds_*`` families).  That is indistinguishable from "no columns"
    to a caller, so a schema is recovered with ``DESCRIBE TABLE``, which does
    work on those tables.  The fallback costs a warehouse round-trip, which is
    why it is only done for a single named table and never per-row while
    listing a schema.
    """
    meta: Dict[str, Any] = {}
    try:
        meta = _request(
            workspace,
            f"/api/2.1/unity-catalog/tables/{urllib.parse.quote(full_name)}",
            method="GET",
        )
    except DatabricksAPIError:
        meta = {}

    columns = _uc_columns(meta)
    source = "unity-catalog" if columns else None

    if not columns:
        body = execute_statement(f"DESCRIBE TABLE {full_name}", workspace=workspace)
        for row in iter_all_rows(body, workspace=workspace):
            name = str(row[0] or "").strip() if row else ""
            # DESCRIBE appends a blank line then partition/detail sections.
            if not name or name.startswith("#"):
                break
            columns.append({
                "name": name,
                "type": (row[1] if len(row) > 1 else None),
                "comment": (row[2] if len(row) > 2 else None),
            })
        source = "describe-table"

    return {
        "level": "table",
        "full_name": meta.get("full_name") or full_name,
        "type": meta.get("table_type"),
        "securable_kind": meta.get("securable_kind"),
        "comment": meta.get("comment") or None,
        "column_count": len(columns),
        "columns": columns,
        "columns_source": source,
    }


def list_catalog(
    *,
    workspace: Optional[str] = None,
    catalog: Optional[str] = None,
    schema: Optional[str] = None,
    table: Optional[str] = None,
) -> Dict[str, Any]:
    """Browse Unity Catalog: catalogs → schemas → tables → one table.

    Uses the Unity Catalog REST API rather than SQL so it works without a
    warehouse being warm.  The exception is the single-table level, which may
    fall back to ``DESCRIBE TABLE`` (see :func:`describe_table`).
    """
    if catalog and schema and table:
        return describe_table(f"{catalog}.{schema}.{table}", workspace=workspace)

    if catalog and schema:
        rows = _paginate(
            workspace,
            f"/api/2.1/unity-catalog/tables?catalog_name={_q(catalog)}"
            f"&schema_name={_q(schema)}",
            "tables",
        )
        tables = []
        missing_columns = 0
        for t in rows:
            cols = _uc_columns(t)
            if not cols:
                missing_columns += 1
            tables.append({
                "name": t.get("name"),
                "full_name": t.get("full_name"),
                "type": t.get("table_type"),
                "column_count": len(cols),
                "columns": [c["name"] for c in cols],
            })
        result: Dict[str, Any] = {
            "level": "tables",
            "catalog": catalog,
            "schema": schema,
            "count": len(tables),
            "tables": tables,
        }
        if missing_columns:
            # Do not let an empty list read as "this table has no columns".
            result["columns_unavailable_for"] = missing_columns
            result["note"] = (
                f"{missing_columns} of {len(tables)} table(s) expose no columns "
                "through Unity Catalog (common for Delta Sharing tables). Pass "
                "the 'table' argument to describe one — that path falls back to "
                "DESCRIBE TABLE and does return the schema."
            )
        return result

    if catalog:
        rows = _paginate(
            workspace,
            f"/api/2.1/unity-catalog/schemas?catalog_name={_q(catalog)}",
            "schemas",
        )
        schemas = [s.get("name") for s in rows]
        return {"level": "schemas", "catalog": catalog,
                "count": len(schemas), "schemas": schemas}

    rows = _paginate(workspace, "/api/2.1/unity-catalog/catalogs", "catalogs")
    catalogs = [c.get("name") for c in rows]
    return {"level": "catalogs", "count": len(catalogs), "catalogs": catalogs}

