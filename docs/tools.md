# Tool reference

Four tools, in the `databricks` toolset. Every handler returns a JSON **string**
with a `success` boolean; failures return `success: false` and an `error` string
rather than raising.

`databricks_status` is always registered. The other three are gated on
configuration (`check_fn`), so on an unconfigured machine they're absent from the
schema rather than present-and-failing.

**Four is the whole list, deliberately.** Clusters, libraries and job runs are
supported by this plugin but exposed **only** through the CLI
(`hermes databricks cluster|library|job`, see [cli.md](cli.md)) — they are not
tools and cannot be invoked by a model. Two reasons: every tool schema ships on
every API call whether or not the agent touches Databricks that turn, and these
operations spend money. An agent that needs them runs the CLI through its
`terminal` tool, guided by the `databricks-compute` skill, which keeps the
confirmation prompts in the loop. See
[SECURITY.md](../SECURITY.md#spend-is-part-of-the-threat-model).

- [Choosing between query and fetch](#choosing-between-query-and-fetch)
- [databricks_status](#databricks_status)
- [databricks_catalog](#databricks_catalog)
- [databricks_query](#databricks_query)
- [databricks_fetch](#databricks_fetch)

## Choosing between query and fetch

| Situation | Tool |
|---|---|
| Aggregate, count, lookup, schema check | `databricks_query` |
| A few hundred rows you actually want to read | `databricks_query` |
| Unknown result size | `databricks_fetch` |
| Anything you'll post-process with `terminal` / `execute_code` | `databricks_fetch` |
| Millions of rows, or multi-GB | `databricks_fetch` |
| Browsing what data exists | `databricks_catalog` (no warehouse needed) |

The failure mode being avoided: a wide result serialized into the conversation is
what actually breaks a session. `databricks_fetch` returns counts and a path; the
rows stay on disk.

## databricks_status

Reports configuration and, by default, proves a credential resolves. This is the
diagnostic — run it first when anything Databricks-shaped fails, to tell a config
problem from a query problem.

**Parameters**

| Name | Type | Default | Meaning |
|---|---|---|---|
| `workspace` | string | default workspace | Which workspace to verify |
| `verify` | boolean | `true` | Actually mint a token and call the API |

**Response (configured)**

```json
{
  "success": true,
  "configured": true,
  "default_workspace": "main",
  "workspaces": [
    {
      "name": "main",
      "host": "https://adb-123.18.azuredatabricks.net",
      "warehouse_id": "(auto-discover)",
      "auth": "cli-profile:adb-123",
      "is_default": true,
      "cli_profile": "adb-123"
    }
  ],
  "cli": {
    "installed": true,
    "config_file": "/home/you/.databrickscfg",
    "profiles": [
      {"name": "adb-123", "host": "https://...", "auth_type": "databricks-cli",
       "valid": true, "default": true}
    ]
  },
  "verification": {
    "workspace": "main",
    "token_resolved": true,
    "token_chars": 838,
    "mint_ms": 150,
    "catalog": "databricks_ws",
    "user": "you@example.com",
    "api_reachable": true
  }
}
```

Notes on reading it:

- `auth` is the mechanism, not the credential: `cli-profile:<name>`, `key_cmd`,
  `token_env`, `none`, or `unconfigured`. This field is usually the answer.
- The `host` shown is the **resolved** host. A profile-based workspace has no
  literal host in config, and printing `host: None` would look broken.
- `source: databricks-cli-default-profile` on a workspace means nothing was
  configured — it exists only because the CLI had a usable default profile.
- With `verify: true`, `token_resolved` and `api_reachable` separate "cannot get a
  credential" from "credential works but the API rejected us".

When no workspace is available at all, `configured` is `false` and `hint` contains
the specific next command — install the CLI, log in, or run
`hermes databricks setup`.

## databricks_catalog

Browse Unity Catalog metadata without running SQL. The list levels use the Unity
Catalog REST API, so they work even when no SQL warehouse is warm.

**Parameters**

| Name | Type | Meaning |
|---|---|---|
| `catalog` | string | Catalog to inspect. Omit to list all catalogs |
| `schema` | string | Schema to inspect (requires `catalog`) |
| `table` | string | Table to describe (requires `catalog` and `schema`) |
| `workspace` | string | Workspace name. Omit for the default |

Four levels, chosen by which arguments you pass:

| Arguments | `level` | Returns |
|---|---|---|
| none | `catalogs` | `catalogs: [name, ...]`, `count` |
| `catalog` | `schemas` | `schemas: [name, ...]`, `count` |
| `catalog` + `schema` | `tables` | `tables: [{name, full_name, type, column_count, columns}]` |
| `catalog` + `schema` + `table` | `table` | Full column list with types |

**All pages are read.** Unity Catalog list endpoints return one page plus a
`next_page_token`; a caller reading only the first response reports a partial
catalog as complete. Verified live: `samples.tpcds_sf1` at 3 rows/page needs 8
pages for its 24 tables.

**`columns: []` does not mean "no columns".** For some securable kinds — notably
`TABLE_DELTASHARING`, which covers all of `samples.tpch` and `samples.tpcds_*` —
Unity Catalog returns no column metadata at all. The listing therefore reports
`column_count` explicitly, plus:

```json
{
  "columns_unavailable_for": 8,
  "note": "8 of 24 table(s) expose no columns through Unity Catalog (common for Delta Sharing tables). Pass the 'table' argument to describe one — that path falls back to DESCRIBE TABLE and does return the schema."
}
```

The single-table path falls back to `DESCRIBE TABLE` and reports which source the
schema came from via `columns_source` (`unity-catalog` or `describe-table`). That
fallback costs a warehouse round-trip, which is why it's only done for one named
table and never per-row while listing a schema.

## databricks_query

Run SQL and return rows inline. For small results meant to be read directly.

**Parameters**

| Name | Type | Default | Meaning |
|---|---|---|---|
| `query` | string | — | **Required.** The SQL statement |
| `max_rows` | integer | `100` | Display cap; hard maximum `1000` |
| `catalog` | string | — | Default catalog for the statement |
| `schema` | string | — | Default schema for the statement |
| `workspace` | string | — | Workspace name |
| `timeout_seconds` | number | `300` | Max wait for completion |

**Response**

```json
{
  "success": true,
  "columns": ["vendor_id", "trip_distance"],
  "row_count": 100,
  "rows": [{"vendor_id": "1", "trip_distance": 2.4}],
  "total_rows_available": 21932,
  "statement_id": "01f0...",
  "capped_by_max_rows": 100,
  "warning": "Showing the first 100 rows only. ..."
}
```

Two different kinds of "you are not seeing everything", reported separately:

| Field | Meaning |
|---|---|
| `capped_by_max_rows` | *This tool* stopped reading at your cap. Raise `max_rows` or fetch |
| `truncated_by_platform` | **Databricks dropped rows** to fit its inline size limit. The result is incomplete |

`truncated_by_platform` is the dangerous one: the API reports the statement as
`SUCCEEDED` while silently dropping rows. It's surfaced as data loss with a
pointer to `databricks_fetch`.

A result too large for the inline path at all comes back as a failure with
`too_large_for_inline: true` and a `next_step` naming `databricks_fetch` — not as a
SQL error, because the query was fine.

Individual cells longer than 500 characters are clipped with a
`…[N chars total]` marker, so one blob column can't dominate the response.

## databricks_fetch

Run SQL and stream the **complete** result to a file, returning only a summary.
Uses Databricks `EXTERNAL_LINKS` chunked delivery, so there's no inline size
ceiling; rows are written as they arrive, so a multi-GB result never lands in
memory or in context.

**Parameters**

| Name | Type | Default | Meaning |
|---|---|---|---|
| `query` | string | — | **Required.** The SQL statement |
| `path` | string | auto | Destination file. Relative paths resolve inside the export directory |
| `format` | string | `csv` | `csv` or `jsonl` |
| `preview_rows` | integer | `5` | Leading rows echoed as a sanity check; max `50` |
| `row_limit` | integer | — | Server-side cap on rows fetched |
| `catalog` | string | — | Default catalog |
| `schema` | string | — | Default schema |
| `workspace` | string | — | Workspace name |
| `timeout_seconds` | number | `600` | Max wait for completion |

**Response**

```json
{
  "success": true,
  "path": "/home/you/.hermes/databricks-exports/query-20260819-102314.csv",
  "format": "csv",
  "rows_written": 100000,
  "bytes_on_disk": 8431022,
  "columns": ["vendor_id", "trip_distance"],
  "total_rows": 100000,
  "total_bytes": 8123456,
  "total_chunks": 4,
  "truncated": false,
  "statement_id": "01f0...",
  "elapsed_seconds": 1.8,
  "preview": [{"vendor_id": "1", "trip_distance": 2.4}],
  "note": "Full result is on disk — analyze it with terminal or execute_code rather than reading the whole file into context."
}
```

Check `rows_written` against `total_rows`: the first is what actually reached disk,
the second is what the platform said the result contained. They should match, and
a mismatch is a bug worth reporting.

Auto-generated paths are `query-YYYYmmdd-HHMMSS.<fmt>` under
`$HERMES_HOME/databricks-exports/`. Nothing is cleaned up automatically.

Presigned chunk URLs carry their own auth in the query string, so no
`Authorization` header is sent when downloading them — adding one makes S3/ADLS
reject the request. If a chunk download 403s, the link most likely expired
mid-transfer on a very large export; re-run with a `row_limit`, or narrower SQL.
