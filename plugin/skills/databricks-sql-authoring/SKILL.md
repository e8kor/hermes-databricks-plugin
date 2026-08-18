---
name: databricks-sql-authoring
description: Write cheap, correct SQL for Databricks warehouses.
version: 1.0.0
author: Eugene Korniichuk
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [databricks, sql, unity-catalog, cost, performance]
    category: data
    related_skills: [databricks, databricks-bulk-export]
---

# Databricks SQL Authoring Skill

Write SQL for a Databricks SQL warehouse that answers the question without
scanning more than it must. Covers dialect specifics, exploration order and cost
control; it does not cover DDL, ingestion, or pipeline authoring.

## When to Use

- Composing any query for `databricks_query` or `databricks_fetch`.
- A query is slow, expensive, or scanning far more data than expected.
- Exploring an unfamiliar schema.
- Porting SQL from Postgres/MySQL/Snowflake to Databricks.

## Prerequisites

- A working workspace (`databricks_status`).
- Table names confirmed with `databricks_catalog` — not guessed.

## Quick Reference

| Task | Databricks SQL |
|---|---|
| Fully qualify | `catalog.schema.table` |
| Current context | `SELECT current_catalog(), current_schema()` |
| Cheap row count | `SELECT count(*) FROM t` |
| Schema of a table | `DESCRIBE TABLE t` |
| Table size/history | `DESCRIBE DETAIL t`, `DESCRIBE HISTORY t` |
| Sample rows cheaply | `SELECT * FROM t LIMIT 10` |
| Generate test rows | `SELECT id FROM range(1000)` |
| Date math | `date_add`, `date_trunc`, `current_date()` |
| Safe division | `try_divide(a, b)` |
| Safe cast | `try_cast(x AS int)` |
| Quote an identifier | backticks: `` `odd name` `` |

## Procedure

1. **Always three-part-name your tables.** `catalog.schema.table` removes any
   dependence on session context, which differs between the plugin's default
   catalog and your notebook's.

2. **Explore in this order:** `databricks_catalog` for the table list, then
   `databricks_catalog` with `table` for columns, then `SELECT * ... LIMIT 10` for
   actual values. Reading real values early prevents wrong assumptions about
   nulls, units and encodings.

3. **Filter on partition/clustering columns when they exist.** A date-partitioned
   fact table scanned without a date predicate reads everything. `DESCRIBE DETAIL`
   shows partition columns and size.

4. **Project only the columns you need.** These are columnar tables; `SELECT *`
   on a wide table costs real money and buys nothing when you want three columns.

5. **Aggregate server-side.** Return the answer, not the raw rows. One
   `GROUP BY` beats exporting a million rows to count them locally.

6. **Use `LIMIT` while iterating**, then remove it for the real run. Do not use
   `LIMIT` as a substitute for a `WHERE` clause — it caps output, not scan.

7. **Prefer `try_*` functions over raw casts** on dirty data. `try_cast` and
   `try_divide` yield NULL instead of failing the whole statement, which turns a
   hard error into an inspectable result.

8. **Check the plan when something is slow.** `EXPLAIN <query>` through
   `databricks_query` shows whether a filter pushed down or a scan went wide.

## Pitfalls

- **Databricks is not Postgres.** No `SERIAL`, no `RETURNING`; use backticks not
  double quotes for identifiers; string literals are single-quoted. Porting SQL
  verbatim from another engine is a common source of confusing errors.
- **Identifiers are case-insensitive but values are not.** `WHERE status = 'ACTIVE'`
  will not match `'active'`. Normalize with `lower()` when unsure.
- **`count(col)` skips NULLs; `count(*)` does not.** This silently changes
  answers on nullable columns.
- **Integer division truncates.** Cast or use `try_divide` for a ratio.
- **A cold warehouse adds ~30s to the first statement**, which looks like a slow
  query but is startup. Measure the second run.
- **`LIMIT` does not reduce cost on its own.** The engine may still scan widely
  to satisfy ordering or filtering.
- **Timestamps carry a session timezone.** Use `date_trunc` and explicit
  timezone handling rather than string slicing for date grouping.
- **A query that works in a notebook may fail here** if it relied on a `USE
  CATALOG`/`USE SCHEMA` set earlier in that session. Pass `catalog`/`schema`
  arguments, or fully qualify.

## Verification

```
# context and dialect sanity
hermes databricks query "SELECT current_catalog() AS cat, current_schema() AS sch"

# confirm the table before querying it
hermes databricks catalogs samples nyctaxi trips

# cheap shape check, then the real aggregate
hermes databricks query "SELECT count(*) AS n FROM samples.nyctaxi.trips"
hermes databricks query "SELECT date_trunc('month', tpep_pickup_datetime) AS m, count(*) AS trips, round(avg(fare_amount),2) AS avg_fare FROM samples.nyctaxi.trips GROUP BY 1 ORDER BY 1"

# null-safety helpers behave as documented
hermes databricks query "SELECT try_divide(1,0) AS d, try_cast('x' AS int) AS c"
```
