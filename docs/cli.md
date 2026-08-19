# CLI and slash command reference

Two front ends over the same handlers, grouped as a lifecycle:
**CONNECT → EXPLORE → QUERY**.

- `hermes databricks <action>` — full flags, useful in a shell or a script
- `/databricks <action>` — the same verbs inside a Hermes session, rendered as
  tables rather than JSON

Both call the same functions the model's tools call, so what you see in a terminal
is what the agent sees.

> Cluster, library, and job-run management (`compute`, `job` verbs) is in
> development on `main` and not covered here yet; see [CHANGELOG.md](../CHANGELOG.md).

## CONNECT

### `hermes databricks status`

Show workspaces, the auth mechanism in use, visible CLI profiles, and verify that
a credential actually resolves. Run this first when anything fails.

```bash
hermes databricks status
```

Exit code is non-zero when verification reports an error, so it works in a
healthcheck.

### `hermes databricks workspaces`

The same report **without** the live credential check — no token minted, no API
call. Useful when you only want to see what's configured.

### `hermes databricks profiles`

List the Databricks CLI profiles available to adopt, with the CLI's own validity
verdict.

```bash
hermes databricks profiles
hermes databricks profiles --refresh          # bypass the 300s cache
hermes databricks profiles --warehouses       # also list each profile's warehouses
```

| Flag | Meaning |
|---|---|
| `--refresh` | Re-query the CLI instead of using the cached listing |
| `--warehouses` | Also list SQL warehouses per profile (slower — one call each) |

The cache exists because `databricks auth profiles` contacts every workspace to
validate; it takes seconds. Use `--refresh` right after
`databricks auth login`.

### `hermes databricks setup`

Configure a workspace. Usually this is just naming a CLI profile.

```bash
hermes databricks setup --profile prod --default
hermes databricks setup --name dev --profile dev-workspace
hermes databricks setup --name ci --host https://... --token-env DATABRICKS_TOKEN
```

| Flag | Default | Meaning |
|---|---|---|
| `--name` | `main` | Workspace name in config |
| `--profile` | — | CLI profile to derive host and auth from |
| `--host` | — | Workspace URL; optional when `--profile` is given |
| `--token-env` | — | Use a static token from this `.env` variable instead |
| `--warehouse-id` | — | Pin a SQL warehouse (otherwise auto-discovered) |
| `--default` | off | Make this the default workspace |

Values are written through `hermes config set`, never by hand-editing
`config.yaml`. After writing, setup invalidates the profile cache and runs the
`status` verification, so a broken setup fails immediately with a non-zero exit
rather than at first query.

Refusals are deliberate and specific: no `--profile` and no `--host` prints the
available profiles; a named profile that doesn't exist prints the
`databricks auth login --profile ...` command that would create it; a profile the
CLI reports as invalid gets a warning rather than silent acceptance.

## EXPLORE

### `hermes databricks catalogs [catalog] [schema] [table]`

Browse Unity Catalog. Positional depth chooses the level, so there are no flags to
remember:

```bash
hermes databricks catalogs                             # all catalogs
hermes databricks catalogs samples                     # schemas in `samples`
hermes databricks catalogs samples tpch                # tables in `samples.tpch`
hermes databricks catalogs samples tpch lineitem       # that table's columns
```

| Flag | Meaning |
|---|---|
| `--workspace` | Target a non-default workspace |

The list levels use the Unity Catalog REST API, so they work with no warm
warehouse. Describing a single table may fall back to `DESCRIBE TABLE` — see
[tools.md](tools.md#databricks_catalog) for why.

## QUERY

### `hermes databricks query <sql>`

Run SQL and print rows, capped.

```bash
hermes databricks query "SELECT count(*) FROM samples.nyctaxi.trips"
hermes databricks query "SELECT * FROM t LIMIT 500" --max-rows 500
hermes databricks query "SELECT * FROM trips" --json
```

| Flag | Default | Meaning |
|---|---|---|
| `--max-rows` | `100` | Row cap (hard maximum 1000) |
| `--catalog` | — | Default catalog for the statement |
| `--schema` | — | Default schema for the statement |
| `--workspace` | — | Target workspace |
| `--json` | off | Emit the raw JSON payload instead of a table |

Exit code is non-zero on failure. `--json` is the one to use in scripts — the table
renderer is for humans and clips wide values.

### `hermes databricks fetch <sql>`

Stream a complete result to a file. No size ceiling.

```bash
hermes databricks fetch "SELECT * FROM samples.nyctaxi.trips" --format csv
hermes databricks fetch "SELECT * FROM big_table" --path /tmp/out.jsonl --format jsonl
hermes databricks fetch "SELECT * FROM big_table" --row-limit 1000000
```

| Flag | Default | Meaning |
|---|---|---|
| `--path` | auto | Destination file; relative paths resolve under the export dir |
| `--format` | `csv` | `csv` or `jsonl` |
| `--row-limit` | — | Server-side cap on rows fetched |
| `--preview-rows` | `5` | Leading rows to print as a sanity check |
| `--catalog` | — | Default catalog |
| `--schema` | — | Default schema |
| `--workspace` | — | Target workspace |
| `--json` | off | Emit the raw JSON summary |

Auto-generated paths land in `$HERMES_HOME/databricks-exports/` as
`query-YYYYmmdd-HHMMSS.<fmt>`. Check `rows_written` against `total_rows` in the
output — they should match.

## Slash command

```
/databricks help
/databricks status
/databricks profiles [--refresh]
/databricks catalogs [catalog] [schema] [table]
/databricks query <sql>
/databricks fetch <sql>
```

Same handlers, table-rendered output, no flags beyond `--refresh`. When you need a
flag (`--max-rows`, `--format`, `--path`), use the `hermes databricks` form through
`terminal`, or call the tool directly.

Unknown subcommands print the help text rather than failing silently.
