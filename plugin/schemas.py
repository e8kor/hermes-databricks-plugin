"""Tool schemas for the databricks plugin (what the model sees)."""

from __future__ import annotations

DATABRICKS_STATUS_SCHEMA = {
    "name": "databricks_status",
    "description": (
        "Show Databricks plugin status: configured workspaces, which is default, "
        "the auth mechanism in use (key_cmd OAuth minting vs a static token), and "
        "whether a credential currently resolves. Use this first when a Databricks "
        "call fails, to tell a config problem from a query problem."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workspace": {
                "type": "string",
                "description": "Workspace name to check. Omit for the default.",
            },
            "verify": {
                "type": "boolean",
                "description": (
                    "Actually resolve a token and call the API to prove the "
                    "credential works. Default true."
                ),
            },
        },
    },
}

DATABRICKS_QUERY_SCHEMA = {
    "name": "databricks_query",
    "description": (
        "Run a SQL query against Databricks Unity Catalog and return rows inline. "
        "For SMALL result sets meant to be read directly (aggregates, lookups, "
        "schema checks, a few hundred rows). Rows are capped by max_rows so a wide "
        "result cannot flood the context; if the platform itself dropped rows the "
        "response says truncated=true and tells you to use databricks_fetch. "
        "For anything large, or any result you want to process as a file, use "
        "databricks_fetch instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The SQL statement to run."},
            "max_rows": {
                "type": "integer",
                "description": (
                    "Maximum rows to return inline (default 100, hard cap 1000). "
                    "This is a display cap, applied on top of the query's own LIMIT."
                ),
            },
            "catalog": {"type": "string", "description": "Default catalog for the query."},
            "schema": {"type": "string", "description": "Default schema for the query."},
            "workspace": {
                "type": "string",
                "description": "Workspace name. Omit for the default.",
            },
            "timeout_seconds": {
                "type": "number",
                "description": "Max seconds to wait for completion (default 300).",
            },
        },
        "required": ["query"],
    },
}

DATABRICKS_FETCH_SCHEMA = {
    "name": "databricks_fetch",
    "description": (
        "Run a SQL query and stream the COMPLETE result set to a file on disk "
        "(csv or jsonl), returning only a summary: row count, byte size, columns "
        "and the path. This is the big-data path — it uses Databricks "
        "EXTERNAL_LINKS chunked delivery, so it has no inline size ceiling and "
        "handles multi-million-row / multi-GB results without ever putting them in "
        "the context window. Use it whenever the result is large, unknown in size, "
        "or you intend to analyze it with terminal/execute_code afterwards."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The SQL statement to run."},
            "path": {
                "type": "string",
                "description": (
                    "Destination file path. Omit to auto-generate one under the "
                    "Hermes home 'databricks-exports' directory."
                ),
            },
            "format": {
                "type": "string",
                "enum": ["csv", "jsonl"],
                "description": "Output format (default csv).",
            },
            "preview_rows": {
                "type": "integer",
                "description": (
                    "How many leading rows to include in the response as a sanity "
                    "check (default 5, max 50). The full data stays on disk."
                ),
            },
            "row_limit": {
                "type": "integer",
                "description": "Optional server-side cap on rows fetched.",
            },
            "catalog": {"type": "string", "description": "Default catalog for the query."},
            "schema": {"type": "string", "description": "Default schema for the query."},
            "workspace": {
                "type": "string",
                "description": "Workspace name. Omit for the default.",
            },
            "timeout_seconds": {
                "type": "number",
                "description": "Max seconds to wait for completion (default 600).",
            },
        },
        "required": ["query"],
    },
}

DATABRICKS_CATALOG_SCHEMA = {
    "name": "databricks_catalog",
    "description": (
        "Browse Unity Catalog metadata without running SQL: list catalogs, or the "
        "schemas in a catalog, or the tables in a schema, or the full column list "
        "of one named table. Use this to discover what data exists before writing "
        "a query. The list levels read the Unity Catalog REST API and work even "
        "when no SQL warehouse is warm; describing a single table may fall back "
        "to DESCRIBE TABLE, because Unity Catalog reports no columns at all for "
        "some table kinds (e.g. Delta Sharing)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "catalog": {
                "type": "string",
                "description": "Catalog to inspect. Omit to list all catalogs.",
            },
            "schema": {
                "type": "string",
                "description": (
                    "Schema to inspect (requires catalog). Omit to list the "
                    "catalog's schemas."
                ),
            },
            "table": {
                "type": "string",
                "description": (
                    "Table to describe (requires catalog and schema). Returns "
                    "every column with its type. Use this when a table listing "
                    "shows column_count 0."
                ),
            },
            "workspace": {
                "type": "string",
                "description": "Workspace name. Omit for the default.",
            },
        },
    },
}

ALL_SCHEMAS = [
    DATABRICKS_STATUS_SCHEMA,
    DATABRICKS_QUERY_SCHEMA,
    DATABRICKS_FETCH_SCHEMA,
    DATABRICKS_CATALOG_SCHEMA,
]
