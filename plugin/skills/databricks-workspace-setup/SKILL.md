---
name: databricks-workspace-setup
description: Connect Databricks via CLI profiles; multi-workspace.
version: 1.0.0
author: Eugene Korniichuk
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [databricks, setup, auth, oauth, profiles, config]
    category: data
    related_skills: [databricks, databricks-troubleshooting]
---

# Databricks Workspace Setup Skill

Wire Hermes to one or more Databricks workspaces using the Databricks CLI as the
source of truth for connection identity. Covers auth choices and multi-workspace
config; it does not cover querying (see the `databricks` skill).

## When to Use

- First-time connection, or `databricks_status` reports nothing configured.
- Adding a second workspace (dev/prod, or two clouds).
- Moving off a hardcoded host/token onto a CLI profile.
- Setting up CI or a service principal that has no CLI profile.

## Prerequisites

- The `databricks` CLI on PATH. Install:
  <https://docs.databricks.com/dev-tools/cli/install.html>
- Verify with `terminal`: `databricks version` (v0.240+ supports
  `auth profiles -o json`, which this plugin uses for discovery).

## Quick Reference

| Situation | Config needed |
|---|---|
| One workspace, CLI logged in | **none** — default profile is adopted |
| Pin a specific profile | `profile: <name>` |
| Two workspaces | one entry each + `default_workspace` |
| CI / service principal | `host` + `auth.token_env` |
| Non-standard token command | `host` + `auth.key_cmd` |

## Procedure

1. **Log in with the CLI first.** This is the step that actually creates the
   credential:
   ```
   databricks auth login --host https://<workspace>.cloud.databricks.com
   ```
   For a named profile: `--profile prod`.

2. **Check what Hermes can see.** `hermes databricks profiles` lists every CLI
   profile with its host, auth type, and whether the CLI considers it `valid`.
   Add `--warehouses` to also list SQL warehouse ids.

3. **Stop here if one profile is enough.** With a single valid default profile
   and no `databricks:` section, the plugin adopts it automatically. Confirm with
   `hermes databricks status` — the workspace shows as `cli-default`.

4. **Pin a profile when there are several**, so a CLI default change cannot
   silently repoint your queries:
   ```
   hermes databricks setup --profile prod --default
   ```
   This writes only `databricks.workspaces.main.profile` — no host, no token.

5. **Add more workspaces** by giving each a name, then set the default:
   ```
   hermes databricks setup --name dev  --profile dev-workspace
   hermes databricks setup --name prod --profile prod-workspace --default
   ```
   Pass `workspace: "dev"` to any tool call to target a non-default one.

6. **Pin a warehouse only if you must.** Omitted, the plugin picks the first
   RUNNING warehouse. Pin it with `--warehouse-id <id>` when the workspace has
   several and cost/routing matters.

7. **For CI or a service principal**, use a static token in the profile's `.env`
   (secrets belong in `.env`, all other settings in `config.yaml`):
   ```
   hermes databricks setup --name ci --host https://... --token-env DATABRICKS_TOKEN
   ```

8. **Verify against the live workspace**, not just the file:
   `hermes databricks status` must print `credential ok` and the connected user.

## Resolution Order

Per workspace, most explicit first. An explicit setting always beats a derived
one, so naming a profile purely for its host while authenticating with a PAT is
valid:

1. `auth.token_env` / `auth.key_cmd` if present → used as-is
2. else `profile` → `databricks auth token -p <profile>`
3. `host` if present → used as-is; else the profile's host

Workspace selection: explicit `workspace` argument → `default_workspace` → the
sole configured workspace → the CLI's default profile.

## Pitfalls

- **`.env` is for secrets only.** Hosts, profile names, warehouse ids and
  workspace names go in `config.yaml` (`hermes databricks setup` does this).
  Only a static token belongs in `.env`.
- **A profile named in config must exist.** If it does not, every tool fails with
  a config error naming the missing profile — deliberately, rather than silently
  falling back to a different workspace.
- **Account-level profiles cannot run SQL.** Profiles whose host is an
  `accounts.` console URL are skipped when auto-picking a default; they have no
  SQL warehouses.
- **`databricks auth profiles` calls every workspace to validate**, so it takes
  seconds, not milliseconds. Results are cached for 5 minutes; use
  `--refresh` after a fresh login.
- **`DATABRICKS_CONFIG_PROFILE` and `DATABRICKS_CONFIG_FILE` are honoured.** If
  a workspace resolves to something unexpected, check those env vars before
  suspecting the config.
- **A token minted by `databricks auth token` lives 1 hour.** It is re-minted
  automatically 60s before expiry — never copy one into config as a static
  token.

## Verification

```
databricks auth profiles -o json          # the CLI's own view
hermes databricks profiles --warehouses   # what Hermes sees, plus warehouses
hermes databricks status                  # resolved workspace + live credential check
hermes databricks query "SELECT current_user() AS u"
```
