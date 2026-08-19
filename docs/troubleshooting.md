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

## When it's not the plugin

Rule out the CLI first — if these fail, the plugin has nothing to work with:

```bash
databricks auth profiles -o json          # what the plugin sees
databricks auth token -p <profile>        # what the plugin mints
databricks warehouses list -p <profile>   # whether a warehouse exists
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
