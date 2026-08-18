# hermes-databricks-plugin

Query Databricks Unity Catalog from [Hermes Agent](https://github.com/NousResearch/hermes-agent) — with credentials derived from your Databricks CLI profiles and a bulk path that keeps multi-GB results out of the model's context window.

## Why this exists

Databricks ships managed MCP servers, and for small lookups they work fine. Two things pushed me to write a plugin instead:

- **The credential can't refresh.** The managed `/api/2.0/mcp/sql` endpoint takes a static bearer in a config header, so a long session outlives its token.
- **Large results are truncated or refused.** Asking the managed `sql` endpoint for 20,000 rows returned 9,166 of them. This plugin's inline path returned all 20,000, and its bulk path streams a 100,000-row export to disk in under two seconds.

So: tokens are minted **per call** from your CLI profile (1-hour OAuth, re-minted 60s before expiry), and anything big goes to a file via Databricks `EXTERNAL_LINKS` chunked delivery instead of into the conversation.

## What you get

**4 tools**

| Tool | Purpose |
|---|---|
| `databricks_status` | Workspaces, auth mechanism, visible CLI profiles, live credential check |
| `databricks_catalog` | Browse catalogs → schemas → tables → one table's columns |
| `databricks_query` | Run SQL, rows returned inline, capped so a wide result can't flood context |
| `databricks_fetch` | Stream a complete result to csv/jsonl on disk; returns a summary + path |

**5 skills** (registered as `databricks:<name>`, loaded explicitly with `skill_view`)

| Skill | Covers |
|---|---|
| `databricks` | Entry point: choosing the right tool for the result size |
| `databricks-workspace-setup` | CLI profiles, multi-workspace, CI/service principals |
| `databricks-sql-authoring` | Databricks SQL dialect, exploration order, cost control |
| `databricks-bulk-export` | The export-then-analyze-on-disk loop |
| `databricks-troubleshooting` | Symptom → cause table for auth/permission/size failures |

**A CLI** (`hermes databricks ...`) and a slash command (`/databricks ...`), both covering the same lifecycle: CONNECT → EXPLORE → QUERY.

## Requirements

- Hermes Agent
- Python 3.9+ (standard library only — no third-party dependencies)
- The [Databricks CLI](https://docs.databricks.com/dev-tools/cli/install.html), v0.240 or newer

## Install

```bash
git clone https://github.com/e8kor/hermes-databricks-plugin.git
cd hermes-databricks-plugin
./install.sh
```

Then enable it and restart Hermes:

```bash
hermes plugins enable databricks
```

`install.sh` copies `plugin/` to `$HERMES_HOME/plugins/databricks` (honouring
`HERMES_HOME`, so it installs into the active profile). Pass `--symlink` to
symlink instead of copy, which is what you want if you intend to hack on it.

## Setup

### The short version

```bash
databricks auth login --host https://<your-workspace>.cloud.databricks.com
hermes databricks status
```

That's it. With a logged-in CLI and **no Hermes config at all**, the plugin
adopts the CLI's default profile. `status` will show the workspace as
`cli-default`.

### Pinning a profile

If you have several profiles, pin one so a CLI default change can't silently
repoint your queries:

```bash
hermes databricks profiles              # see what's available
hermes databricks setup --profile prod --default
```

That writes exactly one line of config — the profile name. No host, no token:

```yaml
databricks:
  default_workspace: main
  workspaces:
    main:
      profile: prod
```

### Multiple workspaces

```bash
hermes databricks setup --name dev  --profile dev-workspace
hermes databricks setup --name prod --profile prod-workspace --default
```

Pass `workspace: "dev"` to any tool call to target a non-default one.

### CI or a service principal (no CLI profile)

Secrets belong in `.env`; everything else in `config.yaml`:

```bash
hermes databricks setup --name ci --host https://... --token-env DATABRICKS_TOKEN
```

### Resolution order

Per workspace, most explicit first — an explicit setting always beats a derived
one, so you can name a profile purely for its host and still authenticate with a
PAT:

1. `auth.token_env` / `auth.key_cmd` if present → used as-is
2. else `profile` → `databricks auth token -p <profile>`
3. `host` if present → used as-is; else the profile's host

Workspace selection: explicit `workspace` argument → `default_workspace` → the
sole configured workspace → the CLI's default profile.

## Usage

```bash
# CONNECT
hermes databricks status
hermes databricks profiles --warehouses

# EXPLORE
hermes databricks catalogs
hermes databricks catalogs samples
hermes databricks catalogs samples tpch
hermes databricks catalogs samples tpch lineitem     # one table's columns

# QUERY
hermes databricks query "SELECT count(*) FROM samples.nyctaxi.trips"
hermes databricks fetch "SELECT * FROM samples.nyctaxi.trips" --format csv
```

The same verbs exist as `/databricks status`, `/databricks catalogs ...`,
`/databricks query <sql>`, `/databricks fetch <sql>`.

## Design notes

Things that are deliberate, and worth knowing before you change them:

- **`databricks_status` is never gated on configuration.** It's the diagnostic —
  it has to work precisely when everything else is broken. The other three tools
  are gated, so they vanish rather than fail confusingly.
- **Credential errors never echo the helper's output.** A `key_cmd` can print a
  token on failure, or embed a secret in its arguments. The error tells you to run
  the command yourself instead.
- **`truncated: true` is treated as data loss.** Databricks reports a
  moderately-oversized inline result as `SUCCEEDED` while dropping rows. The tool
  surfaces that as a warning and points at `databricks_fetch`.
- **Unity Catalog list endpoints are paginated in full.** Reading only the first
  page reports a partial catalog as complete — verified: `samples.tpcds_sf1` at 3
  rows/page needs 8 pages for 24 tables.
- **`columns: []` from Unity Catalog does not mean "no columns".** For
  `TABLE_DELTASHARING` tables (all of `samples.tpch`, `samples.tpcds_*`) UC
  returns no column metadata even on a single-table GET. The single-table path
  falls back to `DESCRIBE TABLE`; the listing reports `column_count` explicitly
  so an empty list can't be misread.
- **Exports go to `$HERMES_HOME/databricks-exports/`,** profile-aware via
  `get_hermes_home()` — never a hardcoded `~/.hermes`.

## Tests

Standard library + pytest only. No network calls; the API layer is exercised
against recorded response shapes.

```bash
python -m pytest tests/ -q
```

## License

MIT — see [LICENSE](LICENSE).
