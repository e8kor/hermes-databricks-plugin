# Troubleshooting

Start here, always:

```bash
hermes databricks status
```

It reports workspaces, the auth mechanism, visible CLI profiles, and whether a
credential actually resolves — and it's the one tool that is never gated on
configuration, so it works precisely when everything else is broken.

Read three fields first:

| Field | Tells you |
|---|---|
| `auth` | The mechanism: `cli-profile:<name>`, `key_cmd`, `token_env`, `none`, `unconfigured` |
| `verification.token_resolved` | Whether a credential could be obtained at all |
| `verification.api_reachable` | Whether the credential was accepted by the API |

`token_resolved: false` is a **local** problem — config or login. `token_resolved:
true` with `api_reachable: false` is a **remote** problem — permissions, host, or
warehouse.

## Symptom → cause

| Symptom | Likely cause | Fix |
|---|---|---|
| The `databricks_*` tools don't exist in the agent's toolset | No workspace resolves, so the gated tools are hidden by design | `hermes databricks status` — only `databricks_status` should be present. Log in or run `setup` |
| `no Databricks workspace available` | No config **and** no usable CLI default profile | `databricks auth login --host <url>`, or `hermes databricks setup --profile <name>` |
| `multiple workspaces configured and no default set` | More than one workspace, no `default_workspace` | `hermes databricks setup --name <n> --profile <p> --default`, or pass `--workspace` |
| `references Databricks CLI profile 'x', which does not exist` | Config points at a profile you deleted or renamed | The error lists what exists; re-point the workspace or `databricks auth login --profile x --host <url>` |
| `auth key_cmd ... exited 1` | The OAuth session expired | `databricks auth login` (re-login), then `hermes databricks profiles --refresh` |
| `auth key_cmd ... timed out after 15s` | The helper is hung, not slow — it reads a local cache and should answer in milliseconds | Run the command by hand; check for an interactive prompt or a network-blocked token endpoint |
| `token_env=X but that variable is unset` | Config names an env var that isn't in the profile's `.env` | Add `X=...` to `$HERMES_HOME/.env` |
| HTTP 401 | Expired credential, or a `Bearer ` prefix problem | Re-login. A pasted `Bearer ` prefix is stripped automatically, so a persistent 401 after re-login is a permissions issue |
| HTTP 403 | The identity lacks a Unity Catalog grant or warehouse `CAN_USE` | Check grants on the catalog/schema/table and the warehouse's permissions |
| Profile listed but `valid: false` | The CLI itself considers the credential dead | `databricks auth login --profile <name>`, then `--refresh` |
| Profile exists but no warehouses | It's an account-console host (`accounts.*`) | Use a workspace host. Account hosts have no SQL warehouses and are skipped when auto-picking |
| Query hangs, then times out | The warehouse was cold and is still starting, or the query is genuinely slow | `hermes databricks profiles --warehouses` to see state. Raise `timeout_seconds`, or start the warehouse first |
| `truncated_by_platform: true` | **Databricks silently dropped rows** while reporting `SUCCEEDED` | Re-run the same SQL through `databricks_fetch`. Do not trust the partial result |
| `too_large_for_inline: true` | The result exceeds the inline transport, not a SQL error | Use `databricks_fetch` (same query, no changes needed) |
| Results look short but no warning | You hit the display cap | Check `capped_by_max_rows` and `total_rows_available`; raise `max_rows` (hard cap 1000) or fetch |
| `columns: []` on a table listing | Unity Catalog exposes no column metadata for that securable kind (Delta Sharing) | Pass the `table` argument to describe one table — that path falls back to `DESCRIBE TABLE`. See `columns_unavailable_for` in the response |
| A table listing looks incomplete | Not a pagination bug in the plugin — every page is read | Compare `count` against the workspace UI; if they differ, that's worth an issue |
| `chunk download failed: HTTP 403` | A presigned chunk link expired mid-transfer on a very large export | Re-run with `--row-limit`, or narrow the SQL. Presigned links are short-lived by design |
| `could not write <path>` | Directory permissions, or a read-only mount | Pass `--path` somewhere writable; the default is `$HERMES_HOME/databricks-exports/` |
| `rows_written` < `total_rows` | Real bug — the export is incomplete | Please open an issue with both numbers and the `statement_id` |
| Exports appear in the wrong place | You're in a different Hermes profile than you think | Exports are per-profile via `get_hermes_home()`. Check with `hermes -p <name> databricks status` |
| CLI changes don't take effect | Python changes need a restart | Restart Hermes. With `install.sh --symlink` the repo *is* the live plugin; without it, re-run `install.sh` |
| `the 'databricks' CLI is not on PATH` | Not installed, or installed for another shell | [Install it](https://docs.databricks.com/dev-tools/cli/install.html), or configure `--host` + `--token-env` instead — the CLI is optional |
| `profiles` is slow | Expected — `auth profiles` contacts every workspace to validate | Results cache for 300s. Only pass `--refresh` when you just logged in |

## Compute: clusters, libraries, job runs

These failures deserve their own table because several of them **report the wrong
cause**. The message names permissions or a missing file when the real problem is
something else entirely.

| Symptom | Likely cause | Fix |
|---|---|---|
| `Cannot read the python file /Volumes/… lacks the required permissions` | **Not a permissions problem.** A `spark_python_task` cannot load its `python_file` from a UC Volume at all — verified while owning the volume, holding explicit `READ_VOLUME`, and running as `SINGLE_USER` with that identity | Import the script as a workspace file and pass that path: `databricks workspace import --format AUTO --file job.py /Users/<you>/job.py`, then `--python-file /Workspace/Users/<you>/job.py`. `job submit` now refuses a Volume path up front |
| `Missing required field: query_id` | `--sql-query-id` takes a **saved query id**, not SQL text | Use `hermes databricks query <sql>` for ad-hoc SQL; a job run is the wrong tool for it |
| Library "installed" but the class is missing at runtime | `libraries/install` returns HTTP 200 **before installing anything** — the verdict lands later | `hermes databricks library status <cluster_id>`. If you passed `--no-wait`, this check is mandatory, not optional |
| `cluster … is TERMINATED; start it before installing libraries` | An install onto a stopped cluster is *accepted* by the API and then silently ignored until the next start | `hermes databricks cluster start <id>`, then install |
| Library still loaded after `uninstall` | Uninstall only marks it for removal; the running JVM keeps it | `hermes databricks cluster restart <id>` |
| `upload size mismatch … treat the remote file as corrupt` | A partial upload. The Files API accepts it, and the JAR then fails on an executor with an opaque class-loading error | Re-run the upload. The byte check is what turned a late, confusing failure into an immediate one |
| HTTP 403 on `library upload` | Workspace-admin does **not** imply Unity Catalog `WRITE VOLUME`; the two systems are independent | Use a volume you own: `hermes databricks library create-volume <catalog> <schema> hermes_libs` |
| `permissions/volume/<fqn>` returns `{}` for a volume you created | Ownership is not a privilege assignment | Grant explicitly if something downstream needs it: `databricks grants update volume <fqn> …` |
| A Volume directory looks empty but has files | You read the legacy DBFS keys. The Files API returns `name`/`file_size`, not `path`/`size` — `databricks fs ls --output json` yields nulls on Volumes | `hermes databricks library ls /Volumes/…`, or `GET /api/2.0/fs/directories/<path>` |
| Run finished but the result is wrong / nothing happened | `life_cycle_state: TERMINATED` with `result_state: FAILED` is the normal shape of "your Spark code threw" | `hermes databricks job show <run_id>` reports `succeeded` explicitly; `job output <run_id>` has the stack trace |
| `run … is a multi-task run; fetch output per task run id` | `runs/get-output` rejects a **parent** run id | Use the task run id the error lists |
| `pass either --cluster … or --spark-version …, not both` | Ambiguous compute: reusing a warm cluster and creating a new one mean different money | Pick one. This is refused locally, before any API call |
| Cluster starts, then every task fails to schedule | A single-node cluster missing its required settings | Use `--workers 0`, which sets `spark.databricks.cluster.profile`, `spark.master`, and the `ResourceClass` tag for you |
| `autotermination_minutes must be >= 10` | You passed a value below 10, or 0 | `0` means **never terminate** and bills until stopped by hand. Pick a real idle timeout |
| `--policy is required` | `cluster create` refuses to run without a cluster policy | `hermes databricks cluster policies` — a policy is the only spend limit the workspace can actually enforce |
| `refusing in a non-interactive shell; pass --yes` | A billable action was invoked from cron/CI/a pipe | Intentional: it fails closed. Add `--yes` only if you genuinely mean to provision compute unattended |
| A run takes ~5 minutes before doing anything | Ephemeral job-cluster startup, not a hang | Reuse a warm cluster with `--cluster <id>` while iterating |
| Custom JAR won't load on serverless | Serverless jobs compute does not accept custom JAR libraries | Use a classic cluster — ephemeral is fine |
| Something is still billing | An all-purpose cluster stays up until autotermination fires | `hermes databricks cluster list` flags every RUNNING cluster; `job list --active` shows live runs. Stop with `cluster stop <id>` (never gated on confirmation) |

## When it's not the plugin

Rule out the CLI first — if these fail, the plugin has nothing to work with:

```bash
databricks auth profiles -o json          # what the plugin sees
databricks auth token -p <profile>        # what the plugin mints
databricks warehouses list -p <profile>   # whether a warehouse exists
databricks clusters list -p <profile>     # whether compute APIs are reachable
```

If `databricks auth token` fails, that's a Databricks CLI or workspace problem, not
a plugin problem — the plugin runs exactly that command.

## Reporting a bug

Open an issue with the bug report template and attach:

- `hermes databricks status` output (redact the host if you must; keep `auth`)
- The plugin version from `plugin/plugin.yaml`
- Databricks CLI version, cloud (AWS/Azure/GCP), and the table kind if relevant
  (Delta, Delta Sharing, view)
- For a wrong-result bug: the `statement_id`, which lets the behaviour be traced in
  the workspace's query history

For anything security-shaped, don't open an issue — see [SECURITY.md](../SECURITY.md).
