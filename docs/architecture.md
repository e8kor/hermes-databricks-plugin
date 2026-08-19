# Architecture

Standard library only. ~4,300 lines across twelve modules, no third-party imports —
HTTP is `urllib.request`, JSON is `json`, CSV is `csv`. The plugin installs as a
directory into a Hermes profile, and requiring a `pip install` there would be a
support burden nobody wants.

- [Module map](#module-map)
- [Registration](#registration)
- [Request lifecycle](#request-lifecycle)
- [Credential resolution](#credential-resolution)
- [The two result paths](#the-two-result-paths)
- [Unity Catalog quirks](#unity-catalog-quirks)
- [Design invariants](#design-invariants)

## Module map

| Module | Responsibility |
|---|---|
| `__init__.py` | `register(ctx)` — wires tools, slash command, CLI, and skills |
| `schemas.py` | Tool schemas: what the model reads when choosing a tool |
| `tools.py` | Tool handlers. JSON string in, JSON string out, never raises |
| `api.py` | Databricks REST: statement execution, chunk streaming, Unity Catalog |
| `config.py` | Workspace resolution, token minting, the token cache |
| `profiles.py` | Databricks CLI profile discovery and warehouse listing |
| `compute.py` | Cluster lifecycle, library install, UC Volume file upload |
| `jobs.py` | Job runs: submit, poll, output. Judges on `result_state` |
| `cli.py` | The `hermes databricks` argparse tree |
| `compute_cli.py` | The `cluster`/`library`/`job` trees + spend confirmations |
| `slash.py` | `/databricks` dispatch plus the human-facing table renderers |

`compute.py` and `jobs.py` reuse `api._request`, so they inherit the same token
minting, HTTP error surfacing, and workspace resolution as the query path — no
second auth mechanism exists.

**Compute has no model tools, deliberately.** Clusters and job runs cost money,
so the entire `cluster`/`library`/`job` surface is CLI-only and lives in
`compute_cli.py` behind an interactive confirmation. Nothing in a conversation
can start billable compute via a tool call. This is the Footprint Ladder's rung 2
(CLI command + skill) rather than rung 6 (new core tool).

Dependency direction is one-way: `profiles` ← `config` ← `api` ← `tools` ←
`__init__`, with `cli` and `slash` sitting on top of `tools`. `compute` and
`jobs` sit beside `tools` on `api`; `compute_cli` sits on both. Nothing lower imports
anything higher.

## Registration

`register(ctx)` runs at plugin discovery:

- **`databricks_status` is registered unconditionally.** It is the diagnostic — it
  has to work precisely when everything else is broken.
- **The other three are gated** on `check_fn=_check_configured`. On an unconfigured
  machine they're absent from the schema entirely, rather than present and failing
  in a way the model then tries to work around.
- **The slash command, the CLI, and each skill register inside their own
  `try`/`except`.** A skill with a typo'd path must not take the tools down with
  it.

`_check_configured` delegates to `config.is_configured()`, which delegates to
`resolve_workspace()`. That indirection is the point: the gate has to agree with
what actually works, or a user with a perfectly good profile-only setup watches
their tools disappear.

## Request lifecycle

A `databricks_query` call:

```
tools.databricks_query
  └─ api.execute_statement
       ├─ config.resolve_workspace       name -> host + auth (profile-derived)
       ├─ config.get_token               cache hit, or mint via key_cmd
       ├─ api.resolve_warehouse_id       config, or discovered
       ├─ POST /api/2.0/sql/statements   wait_timeout=50s, on_wait_timeout=CONTINUE
       ├─ _poll_until_terminal           2s interval, until SUCCEEDED/FAILED/...
       └─ _check_succeeded               non-SUCCEEDED -> DatabricksAPIError
  └─ api.iter_all_rows                   inline rows, then chunk-by-chunk
  └─ cap at max_rows, flag truncation
```

`wait_timeout` is capped at 50s because that's the Databricks maximum; anything
longer is handled by polling rather than by asking the API to block. The statement
is submitted with `on_wait_timeout: CONTINUE`, so a slow query keeps running and
gets polled instead of being cancelled.

Two error classes, deliberately distinct:

- `DatabricksAPIError` — the platform said no
- `InlineLimitExceeded` (a subclass) — the query was fine, the *transport* was
  wrong. Handlers turn this into `too_large_for_inline: true` plus a `next_step`
  naming `databricks_fetch`, rather than reporting a SQL error

## Credential resolution

Auth mirrors the Hermes model-provider `key_cmd` contract: **a command prints a
token**, so the credential is derived rather than stored.

```
get_token(workspace)
  ├─ resolve_workspace -> auth dict
  ├─ auth.key_cmd?    -> cache lookup, else mint
  ├─ auth.token_env?  -> read from the profile secret scope (never cached)
  └─ neither          -> DatabricksAuthError naming both options
```

Cache behaviour:

| Case | TTL used |
|---|---|
| Helper advertises `expires_in` | `expires_in − 60s` |
| Helper advertises nothing | 900s |
| `token_env` static token | **not cached** |

The 60s leeway matches core's `agent/command_token_source.py`, and exists so a
request can never be signed with a token that dies in flight. Static tokens are
deliberately uncached: reading an env var is already free, and caching would mask a
token you rotated mid-session.

`_mint_via_command` never surfaces stdout, stderr, or the command string in an
error. A helper can print a token on failure, and a `key_cmd` may legitimately
embed a secret in its own arguments (`print-token --client-secret=...`). Errors
report the exit code and tell you to run the command yourself.

`_parse_mint_output` accepts a bare token (first line only, so a chatty helper
can't smuggle extra data) or the JSON shape `databricks auth token` prints.

## The two result paths

The whole plugin is shaped by one constraint: **rows are expensive context.**

| | `databricks_query` | `databricks_fetch` |
|---|---|---|
| Disposition | `INLINE` | `EXTERNAL_LINKS` |
| Rows land in | the response | a file on disk |
| Size ceiling | platform inline limit | none |
| Memory | bounded by `max_rows` | one chunk at a time |
| Model sees | rows, capped | counts, columns, path, 5-row preview |

`EXTERNAL_LINKS` returns presigned chunk URLs. Two things about downloading them:

- **No `Authorization` header is sent.** The presigned URL carries its own auth in
  the query string; adding a header makes S3/ADLS reject the request.
- **Chunks are followed via `next_chunk_index`,** with a `seen` set guarding
  against a server that repeats an index. Rows are written to the file as they
  arrive, so a multi-GB result never lands in memory.

## Unity Catalog quirks

Three behaviours the code exists to work around, each verified live rather than
assumed:

**Pagination is mandatory.** UC list endpoints return one page plus a
`next_page_token`. Reading only the first response reports a partial catalog as
complete. `_paginate` walks every page and guards against a repeated token.
Verified: `samples.tpcds_sf1` at `max_results=3` needs 8 pages for 24 tables.

**Query-string escaping must be total.** `urllib.parse.quote` defaults to
`safe="/"`, which leaves a slash inside a schema name or an opaque page token
unescaped and silently corrupts the query string. `_q` uses `safe=""` — nothing
here is a path segment, so nothing is safe.

**`columns: []` is not "no columns".** For `TABLE_DELTASHARING` securables (all of
`samples.tpch`, `samples.tpcds_*`) UC returns no column metadata even on a
single-table GET. `describe_table` falls back to `DESCRIBE TABLE` and reports
`columns_source` so you know which answered. Listings report `column_count` plus a
`columns_unavailable_for` count, so an empty list can't be misread as fact. The
fallback costs a warehouse round-trip, which is why it is only done for one named
table and never per-row while listing a schema.

## Design invariants

Break these and the plugin starts lying:

1. **Handlers return a JSON string and never raise.** Both success and failure.
2. **`databricks_status` is never config-gated.** It's the diagnostic.
3. **Unbounded results go to disk.** Or are capped and say they were capped.
4. **`truncated: true` is data loss, not success.** The API reports the statement
   as `SUCCEEDED` while dropping rows; that is the single most dangerous thing to
   ignore in this API.
5. **No credential, helper output, or command string in an error message.**
6. **Paths go through `get_hermes_home()`.** Never a hardcoded `~/.hermes` — each
   Hermes profile owns its own exports.
7. **Config in `config.yaml`, secret values in `.env`.** Mirrors core policy.
8. **Never claim an outcome you didn't observe.** Databricks APIs return 200 well
   before the thing is done. Poll to a terminal state, or return the raw observed
   state and say so.
