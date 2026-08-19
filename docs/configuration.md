# Configuration

Connection identity lives in the **Databricks CLI**, not duplicated in Hermes
config. That's the whole idea: `databricks auth login` already knows your host and
holds a refreshable credential, and re-running it should not require editing
anything here.

Everything below goes in `config.yaml` under `databricks:` — never in `.env`.
`.env` holds secret *values* only.

- [The three shapes](#the-three-shapes)
- [Resolution order](#resolution-order)
- [Workspace selection](#workspace-selection)
- [Config reference](#config-reference)
- [Profile discovery](#profile-discovery)
- [SQL warehouse selection](#sql-warehouse-selection)
- [Export directory](#export-directory)

## The three shapes

They compose, and an explicit setting always beats a derived one.

### 1. Nothing at all

```bash
databricks auth login --host https://<workspace>.cloud.databricks.com
hermes databricks status
```

With no `databricks:` section, the plugin adopts the CLI's default profile.
`status` reports the workspace as `cli-default` and marks its
`source: databricks-cli-default-profile`, so you can tell "adopted" from
"configured" at a glance.

Good for a single workspace on a laptop. The risk is that changing the CLI's
default profile silently repoints your queries — which is what shape 2 fixes.

### 2. Name a CLI profile

```bash
hermes databricks profiles                            # what's available
hermes databricks setup --profile prod --default
```

That writes exactly one line of connection config — the profile name. No host, no
token:

```yaml
databricks:
  default_workspace: main
  workspaces:
    main:
      profile: prod
```

Host and auth are both derived from `~/.databrickscfg`, so re-running
`databricks auth login` needs no config edit. This is the recommended shape.

### 3. Spell everything out

For CI, or a service principal with no CLI profile. Secrets go in `.env`,
referenced by variable name:

```bash
hermes databricks setup --name ci --host https://... --token-env DATABRICKS_TOKEN
```

```yaml
databricks:
  workspaces:
    ci:
      host: https://adb-123.18.azuredatabricks.net
      auth:
        token_env: DATABRICKS_TOKEN
```

`.env`:

```
DATABRICKS_TOKEN=dapi...
```

A pasted `Bearer ` prefix is stripped automatically — otherwise the request would
send `Bearer Bearer dapi...` and 401 for a reason nobody enjoys debugging.

Instead of a static token you can name a command that prints one, which is the
better option for an enterprise gateway or a short-lived federated credential:

```yaml
databricks:
  workspaces:
    ci:
      host: https://...
      auth:
        key_cmd: "my-cred-helper print-token --scope databricks"
```

`key_cmd` accepts either a bare token on stdout, or the JSON shape
`databricks auth token` prints (`access_token` + `expires_in`). When a TTL is
advertised it's honoured, minus a 60s safety margin; when it isn't, the token is
re-minted every 15 minutes.

## Multiple workspaces

```bash
hermes databricks setup --name dev  --profile dev-workspace
hermes databricks setup --name prod --profile prod-workspace --default
```

```yaml
databricks:
  default_workspace: prod
  workspaces:
    dev:
      profile: dev-workspace
    prod:
      profile: prod-workspace
```

Pass `workspace: "dev"` to any tool call, or `--workspace dev` on the CLI, to
target a non-default one. Token caches are per workspace, so switching between
them costs nothing.

## Resolution order

Per workspace, most explicit first. Because these are independent, you can name a
profile purely for its host and still authenticate with a PAT:

1. `auth.token_env` / `auth.key_cmd` if present → used as-is
2. else `profile` → `databricks auth token -p <profile>`
3. `host` if present → used as-is; else the profile's host

If a workspace names a profile that doesn't exist, the error lists the profiles
that do and tells you the `databricks auth login --profile ...` command to create
it — rather than failing later as an opaque 401.

## Workspace selection

In order:

1. explicit `workspace` argument (tool call or `--workspace`)
2. `databricks.default_workspace`
3. the sole configured workspace, if there is exactly one
4. the CLI's default profile (the implicit `cli-default` workspace)

With several workspaces configured and no default, calls fail with the list of
names rather than guessing. That's deliberate: guessing which workspace to run
production SQL against is not a feature.

## Config reference

```yaml
databricks:
  default_workspace: main        # optional; required when >1 workspace
  workspaces:
    main:
      profile: adb-123           # CLI profile; host + auth derived from it
      host: https://...          # optional when profile is set
      warehouse_id: b81d2...     # optional; auto-discovered when omitted
      auth:                      # optional when profile is set
        token_env: DATABRICKS_TOKEN   # name of an env var in .env
        key_cmd: "cmd that prints a token"
```

| Key | Type | Meaning |
|---|---|---|
| `default_workspace` | string | Which workspace to use when none is named |
| `workspaces.<name>.profile` | string | Databricks CLI profile to derive host + auth from |
| `workspaces.<name>.host` | string | Workspace URL; overrides the profile's host |
| `workspaces.<name>.warehouse_id` | string | Pin a SQL warehouse instead of auto-discovering |
| `workspaces.<name>.auth.token_env` | string | Env var **name** holding a static token |
| `workspaces.<name>.auth.key_cmd` | string | Shell command that prints a token |

Set values with `hermes config set` (or `hermes databricks setup`, which shells
out to it) rather than hand-editing `config.yaml`.

## Profile discovery

Profiles are read via `databricks auth profiles -o json`, not by parsing
`~/.databrickscfg`. That gets environment overrides, account-vs-workspace host
classification, and the CLI's own `valid` verdict for free. Ini parsing exists
only as a fallback for when the CLI isn't installed at all.

Two consequences worth knowing:

- **Listings are cached for 300 seconds.** `auth profiles` contacts every
  workspace to validate, so it takes seconds, not milliseconds. Pass `--refresh`
  after logging in to bypass the cache.
- **Account-console hosts (`accounts.*`) are skipped when auto-picking.** They
  have no SQL warehouses, so adopting one would produce a workspace that can
  never run a query.

## SQL warehouse selection

If `warehouse_id` isn't set, a warehouse is discovered from the workspace.
`hermes databricks profiles --warehouses` shows what each profile can see, with
size and state, so you can pin one deliberately:

```bash
hermes databricks profiles --warehouses
hermes databricks setup --profile prod --warehouse-id b81d255c0b207753 --default
```

Pinning matters when a workspace has several warehouses at different sizes — the
plugin doesn't know which one you're willing to pay for.

## Export directory

`databricks_fetch` writes to `$HERMES_HOME/databricks-exports/`, resolved through
`get_hermes_home()`, so each Hermes profile keeps its own exports. Relative paths
passed to the tool are resolved inside that directory; absolute paths are used
as-is. Nothing is cleaned up automatically — see [SECURITY.md](../SECURITY.md).
