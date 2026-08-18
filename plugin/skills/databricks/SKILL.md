---
name: databricks
description: Query Unity Catalog and export large result sets.
version: 1.2.0
author: Eugene Korniichuk
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [databricks, sql, unity-catalog, data, export]
    category: data
    related_skills: [databricks-sql-authoring, databricks-bulk-export, databricks-troubleshooting]
---

# Databricks Skill

Query Databricks Unity Catalog from Hermes and move result sets of any size.
Credentials are minted per call from the Databricks CLI, so nothing long-lived
is stored. This skill covers picking the right tool for the result size; it does
not cover cluster or job administration.

## When to Use

- Answering a question that needs data from Unity Catalog.
- Exporting a table or query result for local analysis.
- Discovering what catalogs, schemas, or tables exist.
- Diagnosing a Databricks auth or connectivity failure.

## Prerequisites

- The `databricks` CLI on PATH, logged in:
  `databricks auth login --host <workspace-url>`. Tokens live 1 hour and are
  re-minted automatically.
- No Hermes config is required — the CLI's default profile is adopted
  automatically. Check with `databricks_status`; see `databricks-workspace-setup`
  to pin a specific profile or add a second workspace.

## Quick Reference

| Need | Tool |
|---|---|
| Is auth working? | `databricks_status` |
| What tables exist? | `databricks_catalog` |
| What columns does table X have? | `databricks_catalog` with `table` |
| A number, an aggregate, a few rows | `databricks_query` |
| A whole table, unknown size, or data to process | `databricks_fetch` |

## Procedure

1. **Discover before querying.** Call `databricks_catalog` with no arguments for
   catalogs, then with `catalog`, then with `catalog` + `schema` for tables.
   Guessing table names wastes a warehouse spin-up.

2. **When a table listing shows `column_count: 0`, ask for that table by name.**
   Pass `table` alongside `catalog` and `schema`. Unity Catalog does not report
   columns for some table kinds (see Pitfalls); the single-table path falls back
   to `DESCRIBE TABLE` and returns the real schema.

3. **Choose the tool by result size, not by habit.**
   - `databricks_query` returns rows inline, capped by `max_rows` (default 100).
     Use it for aggregates and lookups.
   - `databricks_fetch` streams the complete result to a csv/jsonl file and
     returns only a summary plus a small preview. Use it for anything large, any
     full table, or any result you intend to process.

4. **Never widen `max_rows` to move bulk data.** The cap protects the
   conversation, and the platform enforces a hard 25 MiB inline ceiling
   regardless. If a query is too big, `databricks_query` fails with
   `too_large_for_inline: true` — switch to `databricks_fetch`, do not retry
   with a bigger cap.

5. **Analyze exported files with `terminal` or `execute_code`**, not by reading
   them whole with `read_file`. A 150 MB csv defeats the purpose of exporting it.
   Aggregate first, then read the aggregate.

6. **On failure, run `databricks_status` first.** It separates a config/auth
   problem from a bad query, and lists the CLI profiles it can see. See
   `databricks-troubleshooting` for specific failures.

## Pitfalls

- **Unity Catalog reports zero columns for some tables, and it is not an
  error.** Observed live on the whole `samples.tpch` / `samples.tpcds_*`
  families: the REST `tables` endpoint returns `columns: []` even on a
  single-table GET, because their `securable_kind` is `TABLE_DELTASHARING` and
  column metadata is populated lazily. An empty list does NOT mean "no
  columns" — read `column_count` and describe the table by name when it is 0.
  Running `DESCRIBE TABLE` warms the cache, so the listing reports columns for
  that table afterwards.
- **Unity Catalog list endpoints paginate.** `catalogs`, `schemas` and `tables`
  each return a page plus a `next_page_token`; reading only the first response
  reports a partial catalog as complete. All three levels follow every page
  here — if you call the REST API directly, do the same.
- **`truncated: true` is silent data loss.** A moderately oversized INLINE
  result still reports SUCCEEDED while dropping rows. `databricks_query`
  surfaces this as a warning — treat it as an incomplete answer and re-run with
  `databricks_fetch`, never present it as the full picture.
- **Presigned chunk URLs carry their own auth and expire.** Do not add an
  Authorization header when downloading them, and do not sit on a statement
  result for a long time before fetching it.
- **A cold warehouse costs ~30s on the first query.** That is startup latency,
  not a hang.

## Verification

```
# credential + connectivity + visible CLI profiles, in one call
hermes databricks status

# smallest possible real query
hermes databricks query "SELECT current_catalog(), current_user()"

# catalog browsing at every level, including one table's columns
hermes databricks catalogs
hermes databricks catalogs samples
hermes databricks catalogs samples tpch
hermes databricks catalogs samples tpch lineitem   # expect 16 columns

# prove the bulk path writes a real file
hermes databricks fetch "SELECT id FROM range(100000)" --format csv
wc -l <the printed path>     # expect 100001 (header + rows)
```
