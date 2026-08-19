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

## COMPUTE

Clusters, libraries, and job runs. **CLI only** — these cost money, so there are
no model tools for them and nothing in a conversation can start billable compute
via a tool call. Every billable mutation confirms interactively; `--yes` skips
the prompt (and is required in a non-interactive shell, where the default is to
refuse).

### `hermes databricks cluster <verb>`

```bash
hermes databricks cluster list [--mine]      # flags every RUNNING cluster
hermes databricks cluster show <cluster_id>  # includes termination_reason
hermes databricks cluster policies           # policy ids, needed by create
hermes databricks cluster node-types [--limit N]   # cheapest first
hermes databricks cluster versions [--all]        # LTS runtimes by default

hermes databricks cluster create <name> \
    --spark-version 17.3.x-scala2.13 --node-type Standard_D4ds_v5 \
    --policy <policy_id> [--workers N] [--autotermination M] [--wait]

hermes databricks cluster start|restart|stop <cluster_id> [--no-wait]
```

| Flag | Default | Meaning |
|---|---|---|
| `--policy` | — | **Required** on `create`. A policy caps size and cost |
| `--workers` | `0` | `0` = single node (sets the required spark_conf + tag) |
| `--autotermination` | `30` | Idle minutes before auto-stop. **Below 10 is refused** |
| `--security-mode` | — | e.g. `SINGLE_USER`, `USER_ISOLATION` |
| `--single-user` | — | Owning principal for `SINGLE_USER` mode (an email or service-principal id) |
| `--wait` / `--no-wait` | varies | `start`/`restart` wait by default; `stop` does not |
| `--yes` | off | Skip the confirmation prompt |

`start` on a RUNNING cluster and `stop` on a TERMINATED one are no-ops, not
errors — the raw API 400s on both. `restart` on a TERMINATED cluster falls
through to `start`, since the intent is the same.

### `hermes databricks library <verb>`

```bash
hermes databricks library create-volume <catalog> <schema> <name>
hermes databricks library upload <local_path> /Volumes/<cat>/<schema>/<vol>/<file>
hermes databricks library ls /Volumes/<cat>/<schema>/<vol>
hermes databricks library rm /Volumes/.../<file> [--yes]

hermes databricks library install <cluster_id> --jar /Volumes/.../lib.jar
hermes databricks library status <cluster_id>       # the REAL verdict
hermes databricks library uninstall <cluster_id> --jar /Volumes/.../lib.jar
```

`install` accepts exactly one of `--jar`, `--whl`, `--pypi`, `--maven`, requires
the cluster to be RUNNING, and waits for a terminal install state by default —
`libraries/install` returns HTTP 200 before installing anything, so `--no-wait`
means you *must* follow up with `status`. `upload` verifies the remote byte count
against the local one and fails on a mismatch. `uninstall` only marks the library
for removal; restart the cluster to actually drop it.

### `hermes databricks job <verb>`

```bash
# ephemeral job cluster (default): created for the run, torn down after it
hermes databricks job submit --name smoke \
    --spark-version 17.3.x-scala2.13 --node-type Standard_D4ds_v5 \
    --python-file /Workspace/Users/<you>/job.py

# reuse a warm cluster for iteration (it keeps billing afterwards)
hermes databricks job submit --cluster <cluster_id> \
    --main-class com.x.Main --jar /Volumes/.../lib.jar --param --flag

hermes databricks job show <run_id>       # state + explicit `succeeded`
hermes databricks job output <run_id>     # notebook result, logs, stack trace
hermes databricks job list [--active]     # --active = still billing
hermes databricks job cancel <run_id>
```

| Flag | Meaning |
|---|---|
| `--cluster` | Reuse an existing cluster instead of an ephemeral one |
| `--spark-version` + `--node-type` | Ephemeral cluster spec (mutually exclusive with `--cluster`) |
| `--notebook` / `--python-file` / `--main-class` / `--sql-query-id` | The task; exactly one |
| `--jar` / `--whl` / `--pypi` / `--maven` | Task libraries, repeatable |
| `--param` | Task parameter, repeatable |
| `--timeout` | Run timeout in seconds (default 3600) |
| `--no-wait` | Submit and return the run id instead of polling |

Two traps the reference cannot make disappear, only surface:

- **`--python-file` must be a `/Workspace/...` path, not `/Volumes/...`.** A
  Volume python_file fails with a permissions error even when you own the
  volume; `submit` rejects it up front. JAR libraries from `/Volumes` are fine.
- **`--sql-query-id` takes a saved query id, not SQL text.** Inline SQL fails
  with `Missing required field: query_id`; use `query`/`fetch` for ad-hoc SQL.

Exit code is 0 only when the run reports `result_state: SUCCESS`. A run that
merely *finished* (`TERMINATED` + `FAILED`) exits non-zero and prints the
failure summary plus the stack trace.

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
