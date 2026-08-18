---
name: databricks-bulk-export
description: Export big Databricks results to disk and analyze them.
version: 1.0.0
author: Eugene Korniichuk
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [databricks, export, csv, jsonl, bulk, data]
    category: data
    related_skills: [databricks, databricks-sql-authoring]
---

# Databricks Bulk Export Skill

Move a large Databricks result out of the warehouse and analyze it locally
without ever putting the rows in the conversation. Covers the export-then-analyze
loop; it does not cover writing data back to Databricks.

## When to Use

- A query returns more rows than you want to read (roughly: more than a screen).
- You need a whole table, or a result whose size you cannot predict.
- `databricks_query` returned `too_large_for_inline: true` or
  `truncated_by_platform: true`.
- You intend to aggregate, join, or plot the result with local tooling.

## Prerequisites

- A working workspace — verify with `databricks_status`.
- Enough local disk for the result. Check the expected size first with a
  `SELECT count(*)` via `databricks_query`.

## Quick Reference

| Goal | Call |
|---|---|
| Full table to csv | `databricks_fetch` with `query` |
| Machine-readable, nested-safe | `format: "jsonl"` |
| Cap the export server-side | `row_limit: N` |
| Choose the destination | `path: "/abs/path.csv"` |
| Skip the inline preview | `preview_rows: 0` |

## Procedure

1. **Size it before you pull it.** `SELECT count(*)` through `databricks_query`
   costs one cheap round-trip and tells you whether this is a 10-row answer or a
   10-GB export.

2. **Push work into SQL, not into the export.** Aggregate, filter and project in
   the query. Exporting a wide table to compute one average locally wastes the
   warehouse, the network and the disk. Select only the columns you need.

3. **Export with `databricks_fetch`.** It uses Databricks `EXTERNAL_LINKS`
   chunked delivery, so there is no inline size ceiling; rows stream to disk as
   they arrive and never land in the context window. You get back row count,
   byte size, chunk count, the path, and a small preview.

4. **Pick the format deliberately.** `csv` for spreadsheet and `pandas` work;
   `jsonl` when values contain commas/newlines or you want one self-describing
   record per line.

5. **Verify the export before analyzing it.** Compare `rows_written` against the
   `count(*)` from step 1, and check the file with `terminal`:
   `wc -l <path>` (csv has one extra header line).

6. **Analyze on disk with `terminal` or `execute_code`.** Never `read_file` a
   multi-MB export — that undoes the whole point. Aggregate first, then read the
   small aggregate.

7. **Clean up.** Exports accumulate under `<HERMES_HOME>/databricks-exports/`.
   Remove ones you no longer need; they can be large.

## Pitfalls

- **`read_file` on a big export defeats the purpose.** The file exists precisely
  so the rows stay out of the context window. Use `terminal`/`execute_code`.
- **`rows_written` is the number that matters**, not the preview length. If it
  disagrees with your `count(*)`, investigate before trusting the file.
- **A csv line count is rows + 1.** The header is written when the result has
  columns; `jsonl` has no header, so its line count equals the row count.
- **Presigned chunk URLs expire.** Export and consume in the same run rather than
  holding a statement result open for a long time.
- **`row_limit` is a server-side cap, not a page.** It truncates the export
  deliberately; do not use it to paginate — order and re-filter in SQL instead.
- **Relative `path` values land under the exports directory**, not the current
  working directory. Pass an absolute path when you need a specific location.
- **Disk is not free.** A "quick look" at a fact table can be tens of GB. Size
  it first.

## Verification

```
# small, exact, checkable
hermes databricks fetch "SELECT id, id*id AS sq FROM range(2000)" --format csv
wc -l <printed path>        # expect 2001

# the number the tool reports must match the source
hermes databricks query "SELECT count(*) AS n FROM samples.nyctaxi.trips"
hermes databricks fetch "SELECT * FROM samples.nyctaxi.trips" --format csv
# rows_written must equal n

# jsonl shape
hermes databricks fetch "SELECT id FROM range(5)" --format jsonl
```
